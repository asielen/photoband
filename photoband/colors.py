"""Color conversion: template colors are sRGB hex; output pixels are in the file's
own encoding (bit depth, channel count, ICC profile)."""
from __future__ import annotations

import io
import struct
from functools import lru_cache
from typing import Callable, Dict, Optional, Tuple

import numpy as np
from PIL import Image, ImageCms


def parse_hex(s: str) -> Tuple[int, int, int]:
    s = (s or "#000000").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    try:
        return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return 0, 0, 0


_SRGB = ImageCms.createProfile("sRGB")
_INTENT = ImageCms.Intent.RELATIVE_COLORIMETRIC


# --------------------------------------------------------------------------
# ICC helpers
# --------------------------------------------------------------------------

def icc_space(icc: Optional[bytes]) -> str:
    """The profile's data color space from the header ("RGB", "GRAY", "CMYK", ...), or ""."""
    if not icc or len(icc) < 24:
        return ""
    try:
        return icc[16:20].decode("ascii", "replace").strip().upper()
    except Exception:
        return ""


def icc_matches(icc: Optional[bytes], channels: int) -> bool:
    """True if ``icc`` describes data with this many channels (alpha included)."""
    sp = icc_space(icc)
    if channels in (1, 2):
        return sp == "GRAY"
    if channels in (3, 4):
        return sp == "RGB"
    return False


def _profile(icc: bytes):
    return ImageCms.ImageCmsProfile(io.BytesIO(icc))


# test colors for the identity check: gray ramp, primaries, secondaries and a few mixes
_TEST_RGB = np.array(
    [(v, v, v) for v in (0, 1, 8, 32, 64, 100, 128, 160, 200, 230, 254, 255)]
    + [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255),
       (128, 0, 0), (0, 128, 0), (0, 0, 128), (200, 120, 40), (40, 120, 200), (31, 42, 68),
       (217, 198, 160), (139, 26, 26), (12, 200, 90)], dtype=np.uint8)


@lru_cache(maxsize=64)
def _is_srgb_cached(icc: bytes) -> bool:
    sp = icc_space(icc)
    try:
        src = _profile(icc)
        if sp == "GRAY":
            vals = np.arange(0, 256, 5, dtype=np.uint8)
            im = Image.fromarray(vals.reshape(1, -1), "L")
            out = np.array(ImageCms.profileToProfile(im, src, _SRGB, renderingIntent=_INTENT,
                                                     outputMode="RGB")).reshape(-1, 3).astype(int)
            return bool(np.abs(out - vals[:, None].astype(int)).max() <= 1)
        if sp == "RGB":
            im = Image.fromarray(_TEST_RGB.reshape(1, -1, 3), "RGB")
            out = np.array(ImageCms.profileToProfile(im, src, _SRGB, renderingIntent=_INTENT,
                                                     outputMode="RGB")).reshape(-1, 3).astype(int)
            return bool(np.abs(out - _TEST_RGB.astype(int)).max() <= 1)
        return False
    except Exception:
        return False


def _is_srgb(icc: Optional[bytes]) -> bool:
    """True if pixels tagged with ``icc`` can be treated as sRGB (decided by content: the
    profile must map test colors to sRGB as an identity within +-1). No profile is sRGB.
    Unreadable profiles count as sRGB (nothing better can be done with them)."""
    if not icc:
        return True
    try:
        _profile(icc)
    except Exception:
        return True
    return _is_srgb_cached(bytes(icc))


@lru_cache(maxsize=64)
def _transform(icc: bytes, out_mode: str):
    dst = _profile(icc)
    return ImageCms.buildTransform(_SRGB, dst, "RGB", out_mode, renderingIntent=_INTENT)


# --------------------------------------------------------------------------
# Analytic matrix/TRC math (for 16-bit precision; LUT profiles fall back to LittleCMS)
# --------------------------------------------------------------------------

def _s15(b: bytes) -> float:
    return struct.unpack(">i", b)[0] / 65536.0


def _tags(icc: bytes) -> Dict[bytes, bytes]:
    n = struct.unpack(">I", icc[128:132])[0]
    out = {}
    for i in range(n):
        sig, off, size = struct.unpack(">4sII", icc[132 + 12 * i:144 + 12 * i])
        out[sig] = icc[off:off + size]
    return out


def _xyz(body: bytes) -> np.ndarray:
    if body[:4] != b"XYZ ":
        raise ValueError("not XYZ")
    return np.array([_s15(body[8:12]), _s15(body[12:16]), _s15(body[16:20])])


def _curve(body: bytes) -> Callable[[np.ndarray], np.ndarray]:
    """Forward tone curve device (0..1) -> linear (0..1)."""
    typ = body[:4]
    if typ == b"curv":
        n = struct.unpack(">I", body[8:12])[0]
        if n == 0:
            return lambda x: x
        if n == 1:
            g = struct.unpack(">H", body[12:14])[0] / 256.0
            return lambda x: np.power(x, g)
        table = np.frombuffer(body[12:12 + 2 * n], dtype=">u2").astype(np.float64) / 65535.0
        xs = np.linspace(0, 1, n)
        return lambda x: np.interp(x, xs, table)
    if typ == b"para":
        ft = struct.unpack(">H", body[8:10])[0]
        k = {0: 1, 1: 3, 2: 4, 3: 5, 4: 7}[ft]
        p = [_s15(body[12 + 4 * i:16 + 4 * i]) for i in range(k)]
        g = p[0]
        if ft == 0:
            return lambda x: np.power(x, g)
        if ft == 1:
            _, a, b = p
            return lambda x: np.where(x >= -b / a, np.power(np.maximum(a * x + b, 0), g), 0.0)
        if ft == 2:
            _, a, b, c = p
            return lambda x: np.where(x >= -b / a, np.power(np.maximum(a * x + b, 0), g) + c, c)
        if ft == 3:
            _, a, b, c, d = p
            return lambda x: np.where(x >= d, np.power(np.maximum(a * x + b, 0), g), c * x)
        _, a, b, c, d, e, f = p
        return lambda x: np.where(x >= d, np.power(np.maximum(a * x + b, 0), g) + e, c * x + f)
    raise ValueError("unsupported curve type")


def _inverse(fwd: Callable[[np.ndarray], np.ndarray]) -> Callable[[np.ndarray], np.ndarray]:
    xs = np.linspace(0.0, 1.0, 65536 * 2 + 1)
    ys = np.maximum.accumulate(np.clip(fwd(xs), 0, None))  # monotonic for interp
    return lambda y: np.interp(np.clip(y, 0, None), ys, xs)


class _Shaper:
    """Matrix/TRC (RGB) or TRC (gray) profile, device <-> PCS XYZ (D50)."""

    def __init__(self, icc: bytes):
        t = _tags(icc)
        if any(k in t for k in (b"A2B0", b"A2B1", b"B2A0", b"B2A1")):
            raise ValueError("LUT profile")
        self.gray = icc_space(icc) == "GRAY"
        if self.gray:
            self.trc = [_curve(t[b"kTRC"])]
            self.M = None
        else:
            self.trc = [_curve(t[k]) for k in (b"rTRC", b"gTRC", b"bTRC")]
            self.M = np.stack([_xyz(t[k]) for k in (b"rXYZ", b"gXYZ", b"bXYZ")], axis=1)
        self.inv = [_inverse(f) for f in self.trc]

    def to_xyz(self, dev: np.ndarray) -> np.ndarray:
        lin = np.array([f(v) for f, v in zip(self.trc, dev)])
        return self.M @ lin

    @property
    def white(self) -> np.ndarray:
        """PCS XYZ of device white (gray: only Y is meaningful)."""
        if self.gray:
            return np.array([1.0, 1.0, 1.0])
        return self.M @ np.ones(3)

    def convert_to(self, dst: "_Shaper", dev: np.ndarray) -> np.ndarray:
        """Device values (0..1) in this profile -> device values in ``dst`` (relative
        colorimetric: white maps to white exactly despite s15Fixed16 rounding)."""
        xyz = self.to_xyz(dev) * (dst.white / self.white)
        return dst.from_xyz(xyz)

    def from_xyz(self, xyz: np.ndarray) -> np.ndarray:
        if self.gray:
            return np.clip(self.inv[0](np.array([xyz[1]])), 0, 1)
        lin = np.clip(np.linalg.solve(self.M, xyz), 0, 1)
        return np.clip(np.array([f(v) for f, v in zip(self.inv, lin)]), 0, 1)


@lru_cache(maxsize=1)
def _srgb_shaper() -> _Shaper:
    return _Shaper(ImageCms.ImageCmsProfile(_SRGB).tobytes())


@lru_cache(maxsize=64)
def _analytic(icc: bytes) -> Optional[_Shaper]:
    """A validated analytic converter for ``icc``, or None (use LittleCMS 8-bit)."""
    try:
        dst = _Shaper(icc)
        src = _srgb_shaper()
        mode = "L" if dst.gray else "RGB"
        tr = _transform(icc, mode)
        im = Image.fromarray(_TEST_RGB.reshape(1, -1, 3), "RGB")
        ref = np.array(ImageCms.applyTransform(im, tr)).reshape(len(_TEST_RGB), -1).astype(int)
        for rgb, r in zip(_TEST_RGB, ref):
            v = src.convert_to(dst, rgb.astype(np.float64) / 255.0)
            # LittleCMS approximates steep inverse curves near black; allow a little there
            tol = np.where(r <= 8, 3, 1)
            if (np.abs(np.round(v * 255).astype(int) - r) > tol).any():
                return None
        return dst
    except Exception:
        return None


def _luma(rgb) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def srgb_to_file(hexcolor: str, channels: int, dtype: np.dtype, icc: Optional[bytes]) -> np.ndarray:
    """One pixel value (length ``channels``) for this color in the file's space.
    Alpha channels are set fully opaque. 16-bit files get 16-bit-precise values for
    matrix/TRC profiles."""
    rgb = parse_hex(hexcolor)
    gray = channels in (1, 2)
    dt = np.dtype(dtype)
    maxv = int(np.iinfo(dt).max)
    vals = None  # floats in 0..1, color channels only
    if icc and icc_matches(icc, channels) and not _is_srgb(icc):
        icc = bytes(icc)
        shaper = _analytic(icc) if dt == np.uint16 else None
        if shaper is not None:
            vals = _srgb_shaper().convert_to(shaper, np.array(rgb, dtype=np.float64) / 255.0)
        else:
            try:
                mode = "L" if gray else "RGB"
                out = ImageCms.applyTransform(Image.new("RGB", (1, 1), rgb), _transform(icc, mode))
                vals = np.array(out).reshape(-1).astype(np.float64) / 255.0
            except Exception:
                vals = None
    if vals is None:
        if gray:
            vals = np.array([_luma(rgb) / 255.0])
        else:
            vals = np.array(rgb, dtype=np.float64) / 255.0
    if gray and vals.size == 3:
        vals = np.array([_luma(vals)])
    color = [int(np.clip(round(float(v) * maxv), 0, maxv)) for v in vals]
    if channels == 2:
        color = [color[0], maxv]
    elif channels == 4:
        color = color[:3] + [maxv]
    return np.array(color, dtype=dt)


def to_display_srgb8(arr: np.ndarray, icc: Optional[bytes], mode: str = "RGB") -> np.ndarray:
    """8-bit sRGB RGB array for previews."""
    a = arr
    if a.dtype == np.uint16:
        a = ((a.astype(np.uint32) + 128) // 257).astype(np.uint8)
    elif a.dtype != np.uint8:
        a = np.clip(a, 0, 255).astype(np.uint8)
    c = a.shape[2]
    if mode == "CMYK" and c >= 4:
        im = Image.fromarray(np.ascontiguousarray(a[:, :, :4]), "CMYK")
        if icc:
            try:
                src = _profile(icc)
                im = ImageCms.profileToProfile(im, src, _SRGB, outputMode="RGB")
                return np.array(im)
            except Exception:
                pass
        return np.array(im.convert("RGB"))
    if c in (1, 2):
        g = np.ascontiguousarray(a[:, :, 0])
        if icc and icc_space(icc) == "GRAY" and not _is_srgb(icc):
            try:
                im = ImageCms.profileToProfile(Image.fromarray(g, "L"), _profile(icc), _SRGB,
                                               renderingIntent=_INTENT, outputMode="RGB")
                return np.array(im)
            except Exception:
                pass
        return np.dstack([g, g, g])
    rgb = np.ascontiguousarray(a[:, :, :3])
    if icc and icc_space(icc) == "RGB" and not _is_srgb(icc):
        try:
            src = _profile(icc)
            im = ImageCms.profileToProfile(Image.fromarray(rgb, "RGB"), src, _SRGB,
                                           renderingIntent=_INTENT, outputMode="RGB")
            rgb = np.array(im)
        except Exception:
            pass
    return rgb
