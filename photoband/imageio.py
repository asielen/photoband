"""Pixel I/O: decode TIFF/JPEG/PNG to numpy, keep everything needed to write
the same format back (depth, channels, compression, ICC, DPI).

Arrays are always H x W x C (C = 1, 2, 3 or 4), dtype uint8 or uint16,
and upright (EXIF orientation applied) once :func:`upright` has been called.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import struct
import threading
import zlib
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = None  # large scans are the point of the app

SUPPORTED_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".png"}


# single decodes/encodes use every core (tifffile's default is half of them)
_WORKERS = max(1, os.cpu_count() or 1)


class ImageError(Exception):
    pass


@dataclass
class ImageInfo:
    path: str
    format: str                 # "TIFF" | "JPEG" | "PNG"
    width: int                  # stored (not upright) size
    height: int
    channels: int               # channels read_pixels returns (extra samples beyond alpha dropped)
    dtype: str                  # "uint8" | "uint16"
    mode: str                   # "RGB" | "RGBA" | "L" | "LA" | "CMYK" | other
    compression: str = "none"   # tiff: none|lzw|deflate|packbits|jpeg|zstd|lzma|other:<code>
    predictor: int = 1
    extrasamples: Tuple[int, ...] = ()
    miniswhite: bool = False
    icc: Optional[bytes] = None
    dpi: Optional[Tuple[float, float]] = None   # in pixels per inch
    resolution_unit: int = 2    # TIFF: 1 none, 2 inch, 3 cm
    raw_resolution: Optional[Tuple[float, float]] = None  # as stored, in resolution_unit
    orientation: int = 1
    pages: int = 1
    pages_exact: bool = True    # False: more pages than were counted (pages is a lower bound)
    tiled: bool = False
    bigtiff: bool = False
    size_bytes: int = 0
    mtime_ns: int = 0
    # the file's id (inode): with size and mtime, what caches of this file are keyed by. A save
    # replaces the file (a new id) and can keep its modified time and size (a copy re-saved with
    # the source's dates, a caption of the same length in an uncompressed TIFF)
    file_id: int = 0
    save_blocked: Optional[str] = None     # reason saving is not possible
    notes: List[str] = field(default_factory=list)   # shown when the file is opened
    # source details that saving changes (reported in the save log by write_image)
    compression_code: int = 1
    samples: int = 0            # stored samples per pixel
    planar: bool = False        # TIFF PlanarConfiguration = separate
    dropped_samples: int = 0    # extra samples not carried (beyond gray+alpha / RGB+alpha)
    palette: bool = False       # indexed color expanded to RGB(A)
    src_bits: int = 0           # stored bits per sample when below 8 (PNG 1/2/4-bit gray)
    trns: Optional[Tuple[int, ...]] = None  # PNG tRNS color converted to alpha
    png_chunks: Dict[str, bytes] = field(default_factory=dict)  # gAMA/cHRM/sRGB bodies
    decode_bytes: int = 0       # bytes the raw decode allocates when known (TIFF: all samples)

    @property
    def upright_size(self) -> Tuple[int, int]:
        if self.orientation in (5, 6, 7, 8):
            return self.height, self.width
        return self.width, self.height

    @property
    def upright_dpi(self) -> Optional[Tuple[float, float]]:
        """(horizontal, vertical) DPI of the upright image: X and Y swapped for
        orientations 5-8 (the stored X runs vertically once turned)."""
        if self.dpi and self.orientation in (5, 6, 7, 8):
            return self.dpi[1], self.dpi[0]
        return self.dpi

    @property
    def upright_raw_resolution(self) -> Optional[Tuple[float, float]]:
        r = self.raw_resolution
        if r and self.orientation in (5, 6, 7, 8):
            return r[1], r[0]
        return r

    @property
    def bits(self) -> int:
        return 16 if self.dtype == "uint16" else 8

    @property
    def lossless(self) -> bool:
        return self.format in ("TIFF", "PNG") and not (self.format == "TIFF" and self.compression == "jpeg")

    def to_json(self) -> dict:
        d = asdict(self)
        d["icc"] = bool(self.icc)
        d["png_chunks"] = sorted(self.png_chunks)
        d["upright_width"], d["upright_height"] = self.upright_size
        # the UI reads dpi[0] as horizontal: report it for the upright image
        d["stored_dpi"] = d["dpi"]
        d["dpi"] = self.upright_dpi
        d["bits"] = self.bits
        return d


def file_stat(path: str) -> Tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def is_tifffile_shape_description(s) -> bool:
    """True if ``s`` is the JSON ImageDescription tifffile writes for "shaped" files,
    e.g. ``{"shape": [120, 90, 3]}``. Such a description describes the old pixel array
    and must not be copied into a file with a different size."""
    if isinstance(s, (bytes, bytearray)):
        try:
            s = bytes(s).decode("utf-8")
        except UnicodeDecodeError:
            return False
    if not isinstance(s, str):
        return False
    t = s.strip().rstrip("\0").strip()
    if not (t.startswith("{") and t.endswith("}")):
        return False
    try:
        d = json.loads(t)
    except ValueError:
        return False
    if not isinstance(d, dict) or "shape" not in d:
        return False
    shp = d["shape"]
    return isinstance(shp, list) and all(isinstance(v, int) for v in shp)


def _damaged(path: str, e: Exception) -> ImageError:
    msg = str(e).strip() or type(e).__name__
    return ImageError(f"Cannot read {os.path.basename(path)}: the file is damaged, incomplete or "
                      f"not a supported image ({msg[:160]})")


# --------------------------------------------------------------------------
# Probing (header only where possible)
# --------------------------------------------------------------------------

_TIFF_COMP_NAMES = {1: "none", 5: "lzw", 8: "deflate", 32946: "deflate", 32773: "packbits",
                    7: "jpeg", 6: "jpeg", 34925: "lzma", 50000: "zstd"}
# lossless codecs written back as they were (tifffile + imagecodecs can encode them)
_TIFF_KEEP = {1, 5, 8, 32946, 32773, 34925, 50000}


def _tiff_compression_name(code) -> str:
    c = int(code)
    return _TIFF_COMP_NAMES.get(c, f"other:{c}")


_PROBE_CACHE_SIZE = 512
_probe_cache: "OrderedDict[tuple, ImageInfo]" = OrderedDict()
_probe_lock = threading.Lock()


def probe(path: str) -> ImageInfo:
    """Header information for ``path``. Results are cached by (path, size, mtime_ns)
    in a bounded LRU; every call returns its own copy."""
    try:
        st = os.stat(path)
    except OSError as e:
        raise ImageError(f"Cannot open {os.path.basename(path)}: {e.strerror or e}")
    size, mtime = st.st_size, st.st_mtime_ns
    # inode and ctime too: a file replaced within the mtime granularity of the file system
    # (2 s on FAT, coarse on some shares) with the same size is still seen as changed
    key = (os.path.abspath(path), path, size, mtime, st.st_ino, st.st_ctime_ns)
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None:
            _probe_cache.move_to_end(key)
            return copy.deepcopy(hit)
    info = _probe(path, size, mtime)
    info.file_id = st.st_ino
    with _probe_lock:
        _probe_cache[key] = copy.deepcopy(info)
        while len(_probe_cache) > _PROBE_CACHE_SIZE:
            _probe_cache.popitem(last=False)
    return info


def clear_probe_cache() -> None:
    with _probe_lock:
        _probe_cache.clear()


def _probe(path: str, size: int, mtime: int) -> ImageInfo:
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError as e:
        raise ImageError(f"Cannot open {os.path.basename(path)}: {e.strerror or e}")
    try:
        if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
            info = _probe_tiff(path)
        elif head[:3] == b"\xff\xd8\xff":
            info = _probe_pil(path, "JPEG")
        elif head[:8] == b"\x89PNG\r\n\x1a\n":
            info = _probe_png(path)
        else:
            raise ImageError(f"Unsupported file type: {os.path.basename(path)}")
    except ImageError:
        raise
    except MemoryError:
        raise
    except Exception as e:
        raise _damaged(path, e)
    info.size_bytes, info.mtime_ns = size, mtime
    return info


MAX_TIFF_SAMPLES = 8
_PAGE_SCAN_LIMIT = 64   # IFDs looked at to count pages (a file can chain 100 000s)


def _count_pages(tf) -> Tuple[int, bool]:
    """(real pages, exact): pages that are not reduced-resolution copies, walking the IFD
    chain lazily and stopping after _PAGE_SCAN_LIMIT IFDs (a file can chain 100 000s).
    Only "1" vs "more than 1" changes behaviour; the count is for messages and is a
    lower bound when ``exact`` is False."""
    pages = tf.pages
    real = 0
    for i in range(_PAGE_SCAN_LIMIT + 1):
        if i == _PAGE_SCAN_LIMIT:
            return max(1, real), False
        try:
            p = pages[i]
        except IndexError:
            break
        except Exception:
            # a damaged IFD further down the chain: what was read so far stands
            log.debug("TIFF page %d unreadable while counting pages", i, exc_info=True)
            break
        if p is None:
            break
        if not (int(getattr(p, "subfiletype", 0) or 0) & 1):
            real += 1
    return max(1, real), True


def _probe_tiff(path: str) -> ImageInfo:
    import tifffile
    with tifffile.TiffFile(path) as tf:
        page = tf.pages[0]
        tags = page.tags
        photometric = int(page.photometric)
        spp = int(page.samplesperpixel)
        bps = page.bitspersample
        dtype = np.dtype(page.dtype).name if page.dtype is not None else "unknown"
        extras = tuple(int(x) for x in (page.extrasamples or ()))
        code = int(page.compression)
        name = os.path.basename(path)
        if spp > MAX_TIFF_SAMPLES:
            raise ImageError(f"{name} has {spp} samples per pixel; Photoband opens TIFFs with up to "
                             f"{MAX_TIFF_SAMPLES} (gray, RGB or CMYK, plus alpha and a few extra channels).")
        if dtype not in ("uint8", "uint16", "bool"):
            # bool (1-bit) stays: it opens for preview and is blocked for saving below
            kind = {"f": "floating-point", "i": "signed integer", "u": "integer", "c": "complex"}.get(
                np.dtype(page.dtype).kind if page.dtype is not None else "", "unusual")
            bits = (np.dtype(page.dtype).itemsize * 8) if page.dtype is not None else bps
            raise ImageError(f"{name} stores {bits}-bit {kind} samples; Photoband opens 8 and 16-bit "
                             f"integer TIFFs only.")
        info = ImageInfo(
            path=path, format="TIFF", width=int(page.imagewidth), height=int(page.imagelength),
            channels=spp, dtype=dtype, mode="RGB",
            compression=_tiff_compression_name(code), compression_code=code,
            predictor=int(page.predictor or 1), extrasamples=extras,
            tiled=bool(page.is_tiled), bigtiff=bool(tf.is_bigtiff),
            samples=spp, planar=spp > 1 and int(page.planarconfig) == 2,
        )
        info.pages, info.pages_exact = _count_pages(tf)
        # what page.asarray() really allocates (all samples, at the real item size)
        info.decode_bytes = (int(page.imagewidth) * int(page.imagelength) * spp
                             * np.dtype(page.dtype).itemsize)
        if photometric in (0, 1):
            info.miniswhite = photometric == 0
            info.channels = min(spp, 2)
            info.mode = "L" if info.channels == 1 else "LA"
        elif photometric == 2:
            info.channels = min(spp, 4)
            info.mode = "RGB" if info.channels == 3 else "RGBA"
            if spp < 3:
                info.save_blocked = "This TIFF color layout is not supported for saving."
        elif photometric == 5:
            info.mode = "CMYK"
            info.save_blocked = "CMYK TIFFs can be previewed but not saved in this version."
        elif photometric == 6:
            info.mode = "RGB"
            info.channels = 3
            if info.compression != "jpeg":
                # samples are stored as raw YCbCr; writing them back tagged RGB would change colors
                info.save_blocked = ("YCbCr TIFFs without JPEG compression can be previewed but not saved "
                                     "in this version. Convert the file to RGB in another app first.")
        elif photometric == 3:
            info.mode = "P"
            info.save_blocked = "Palette (indexed-color) TIFFs are not supported for saving."
        else:
            info.mode = f"photometric{photometric}"
            info.save_blocked = "This TIFF color type is not supported for saving."
        if info.mode != "CMYK" and photometric in (0, 1, 2):
            info.dropped_samples = max(0, spp - info.channels)
            info.extrasamples = extras[:max(0, info.channels - (1 if info.channels <= 2 else 3))]
        if isinstance(bps, tuple):
            bps = bps[0]
        if bps not in (8, 16):
            info.save_blocked = f"{bps}-bit TIFFs are not supported for saving (8 and 16-bit only)."
        if 34675 in tags:
            info.icc = bytes(tags[34675].value)
        if 274 in tags:
            try:
                info.orientation = int(tags[274].value)
            except Exception:
                pass
        unit = int(tags[296].value) if 296 in tags else 2
        info.resolution_unit = unit
        if 282 in tags and 283 in tags:
            xr = _rational(tags[282].value)
            yr = _rational(tags[283].value)
            info.raw_resolution = (xr, yr)
            if unit == 3:
                info.dpi = (xr * 2.54, yr * 2.54)
            elif unit == 2:
                info.dpi = (xr, yr)
        if info.compression == "jpeg":
            info.notes.append("JPEG-compressed TIFF is written with Deflate compression")
        elif code not in _TIFF_KEEP:
            info.notes.append(f"Unusual TIFF compression ({info.compression}) is written with Deflate")
        if info.tiled:
            info.notes.append("Tiled TIFF is written as striped TIFF")
        if info.bigtiff:
            info.notes.append("BigTIFF is written as classic TIFF when it fits")
        if info.planar:
            info.notes.append("Planar (separate) samples are written interleaved")
        if info.dropped_samples:
            info.notes.append(f"{info.dropped_samples} extra sample channel(s) are not kept")
        if info.pages > 1:
            n = f"{info.pages}" if info.pages_exact else f"{info.pages}+"
            info.notes.append(f"Multi-page TIFF ({n} pages): only the first page is shown")
    return info


def _rational(v) -> float:
    if isinstance(v, tuple) and len(v) == 2:
        return v[0] / v[1] if v[1] else 0.0
    return float(v)


def _probe_pil(path: str, fmt: str) -> ImageInfo:
    with Image.open(path) as im:
        mode = im.mode
        info = ImageInfo(path=path, format=fmt, width=im.width, height=im.height,
                         channels=len(im.getbands()), dtype="uint8", mode=mode)
        info.icc = im.info.get("icc_profile")
        dpi = im.info.get("dpi")
        if dpi and dpi[0] and dpi[1]:
            info.dpi = (float(dpi[0]), float(dpi[1]))
        try:
            ori = im.getexif().get(274)
            if ori:
                info.orientation = int(ori)
        except Exception:
            pass
        has_trns = "transparency" in im.info
    if mode == "CMYK":
        info.save_blocked = "CMYK JPEGs can be previewed but not saved in this version."
    if mode in ("P", "PA"):
        info.palette = True
        info.channels = 4 if (has_trns or mode == "PA") else 3
        info.mode = "RGBA" if info.channels == 4 else "RGB"
        info.notes.append("Palette (indexed) colors are written as RGB")
    if mode in ("1", "I", "F", "I;16", "I;16B"):
        if mode.startswith("I;16"):
            info.dtype, info.mode, info.channels = "uint16", "L", 1
        else:
            info.save_blocked = f"{mode} images are not supported for saving."
    return info


def _png_chunks(path: str) -> Tuple[tuple, Dict[bytes, bytes]]:
    """IHDR fields and the ancillary chunks before the first IDAT."""
    out: Dict[bytes, bytes] = {}
    with open(path, "rb") as fh:
        fh.seek(8)
        ihdr = None
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                raise ImageError("PNG ends before its image data")
            n, typ = struct.unpack(">I4s", hdr)
            if typ in (b"IDAT", b"IEND"):
                break
            body = fh.read(n)
            fh.seek(4, 1)
            if typ == b"IHDR":
                ihdr = struct.unpack(">IIBBBBB", body[:13])
            elif typ not in out:
                out[typ] = body
    if ihdr is None:
        raise ImageError("PNG has no header")
    return ihdr, out


def _probe_png(path: str) -> ImageInfo:
    ihdr, chunks = _png_chunks(path)
    w, h, depth, ctype = ihdr[0], ihdr[1], ihdr[2], ihdr[3]
    if ctype == 3:
        info = _probe_pil(path, "PNG")
    else:
        base = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
        if base is None:
            raise ImageError(f"Invalid PNG color type {ctype}")
        with Image.open(path) as im:
            info = ImageInfo(path=path, format="PNG", width=w, height=h, channels=base,
                             dtype="uint16" if depth == 16 else "uint8", mode="L")
            info.icc = im.info.get("icc_profile")
            dpi = im.info.get("dpi")
            if dpi and dpi[0] and dpi[1]:
                info.dpi = (float(dpi[0]), float(dpi[1]))
            try:
                ori = im.getexif().get(274)
                if ori:
                    info.orientation = int(ori)
            except Exception:
                pass
        if depth < 8:
            info.src_bits = depth
            info.notes.append(f"{depth}-bit grayscale is written as 8-bit")
        trns = chunks.get(b"tRNS")
        if trns and ctype in (0, 2):
            k = 1 if ctype == 0 else 3
            if len(trns) >= 2 * k:
                info.trns = struct.unpack(">" + "H" * k, trns[:2 * k])
                info.channels += 1
                info.notes.append("PNG transparency color (tRNS) is read as an alpha channel")
        info.mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[info.channels]
    info.png_chunks = {k.decode("ascii"): v for k, v in chunks.items() if k in (b"gAMA", b"cHRM", b"sRGB")}
    return info


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------

def read_pixels(info: ImageInfo, check_size: bool = True) -> np.ndarray:
    """Decode the first image at full resolution. Returns H x W x C, stored orientation.

    Refuses (ImageError) images whose decoded size exceeds this machine's budget
    (:func:`photoband.decodegate.check_budget`) unless ``check_size`` is False."""
    if check_size:
        from .decodegate import check_budget
        check_budget(info)
    try:
        return _read_pixels(info)
    except ImageError:
        raise
    except MemoryError:
        raise
    except Exception as e:
        raise _damaged(info.path, e)


def _ycbcr_to_rgb(arr: np.ndarray, page) -> np.ndarray:
    tags = page.tags
    sub = tuple(tags[530].value) if 530 in tags else (2, 2)
    if tuple(sub) != (1, 1):
        raise ImageError("Subsampled YCbCr TIFFs without JPEG compression cannot be opened.")
    def rationals(tag, n):
        v = list(tags[tag].value)
        if len(v) == 2 * n and all(isinstance(x, int) for x in v):  # flat numerator/denominator pairs
            v = [(v[i], v[i + 1]) for i in range(0, 2 * n, 2)]
        return [_rational(x) for x in v]

    lr, lg, lb = rationals(529, 3) if 529 in tags else (0.299, 0.587, 0.114)
    mx = float(np.iinfo(arr.dtype).max)
    half = (mx + 1) / 2
    rbw = rationals(532, 6) if 532 in tags else [0, mx, half, mx, half, mx]
    crange = half - 1  # TIFF CodingRange for chroma (127 for 8-bit)
    a = arr[:, :, :3].astype(np.float64)
    y = (a[:, :, 0] - rbw[0]) * mx / max(1e-9, rbw[1] - rbw[0])
    cb = (a[:, :, 1] - rbw[2]) * crange / max(1e-9, rbw[3] - rbw[2])
    cr = (a[:, :, 2] - rbw[4]) * crange / max(1e-9, rbw[5] - rbw[4])
    r = cr * (2 - 2 * lr) + y
    b = cb * (2 - 2 * lb) + y
    g = (y - lb * b - lr * r) / lg
    out = np.dstack([r, g, b])
    return np.clip(np.round(out), 0, mx).astype(arr.dtype)


def _read_pixels(info: ImageInfo) -> np.ndarray:
    if info.format == "TIFF":
        import tifffile
        with tifffile.TiffFile(info.path) as tf:
            page = tf.pages[0]
            arr = page.asarray(maxworkers=_WORKERS)
            if arr.ndim == 3 and int(page.planarconfig) == 2 and arr.shape[0] == int(page.samplesperpixel):
                arr = np.moveaxis(arr, 0, -1)
            if arr.ndim == 2:
                arr = arr[:, :, None]
            if int(page.photometric) == 6 and info.compression != "jpeg":
                arr = _ycbcr_to_rgb(arr, page)
        if info.mode != "CMYK" and arr.shape[2] > info.channels:
            arr = arr[:, :, :info.channels]
        if info.miniswhite:
            # invert the gray channel only; alpha keeps its meaning
            arr = arr.copy()
            arr[:, :, 0] = np.iinfo(arr.dtype).max - arr[:, :, 0]
        return np.ascontiguousarray(arr)
    if info.format == "PNG" and info.dtype == "uint16":
        import cv2
        arr = cv2.imread(info.path, cv2.IMREAD_UNCHANGED)
        if arr is None:
            raise ImageError(f"Cannot decode {os.path.basename(info.path)}: the PNG is damaged or incomplete")
        if arr.ndim == 2:
            arr = arr[:, :, None]
        elif arr.shape[2] == 3:
            arr = arr[:, :, ::-1]
        elif arr.shape[2] == 4:
            arr = arr[:, :, [2, 1, 0, 3]]
        if info.channels == 2 and arr.shape[2] == 4:
            arr = arr[:, :, [0, 3]]  # cv2 expands gray+alpha to BGRA
        return _add_trns_alpha(np.ascontiguousarray(arr), info)
    with Image.open(info.path) as im:
        im.load()
        if im.mode in ("P", "PA"):
            im = im.convert("RGBA" if info.channels == 4 else "RGB")
        elif im.mode == "1":
            im = im.convert("L")
        elif im.mode == "I;16" or im.mode.startswith("I;16"):
            a = np.array(im, dtype=np.uint16)
            return _add_trns_alpha(a[:, :, None], info)
        elif im.mode not in ("L", "LA", "RGB", "RGBA", "CMYK"):
            im = im.convert("RGB")
        a = np.array(im)
    if a.ndim == 2:
        a = a[:, :, None]
    return _add_trns_alpha(a, info)


def _add_trns_alpha(a: np.ndarray, info: ImageInfo) -> np.ndarray:
    """PNG tRNS on gray/RGB: pixels equal to the tRNS color become transparent.
    Color channels are left exactly as decoded."""
    if not info.trns or a.shape[2] == info.channels:
        return a
    k = len(info.trns)
    if a.shape[2] != k:
        return a
    t = np.array(info.trns, dtype=np.int64)
    if info.src_bits and info.src_bits < 8:
        t = t * (255 // ((1 << info.src_bits) - 1))  # Pillow expands low-bit gray to 0..255
    maxv = np.iinfo(a.dtype).max
    hit = np.all(a == t.astype(a.dtype)[None, None, :], axis=2)
    alpha = np.where(hit, 0, maxv).astype(a.dtype)
    return np.ascontiguousarray(np.dstack([a, alpha]))


_ORIENT_OPS = {
    1: lambda a: a,
    2: lambda a: a[:, ::-1],
    3: lambda a: a[::-1, ::-1],
    4: lambda a: a[::-1, :],
    5: lambda a: np.swapaxes(a, 0, 1),
    6: lambda a: np.swapaxes(a, 0, 1)[:, ::-1],
    7: lambda a: np.swapaxes(a, 0, 1)[::-1, ::-1],
    8: lambda a: np.swapaxes(a, 0, 1)[::-1, :],
}


def upright(arr: np.ndarray, orientation: int) -> np.ndarray:
    op = _ORIENT_OPS.get(int(orientation or 1), _ORIENT_OPS[1])
    return np.ascontiguousarray(op(arr))


def orient_box(box, orientation: int):
    """Transform a normalized top-left (x, y, w, h) box from stored to upright coordinates."""
    x, y, w, h = box
    o = int(orientation or 1)
    if o == 1:
        return (x, y, w, h)
    if o == 2:
        return (1 - x - w, y, w, h)
    if o == 3:
        return (1 - x - w, 1 - y - h, w, h)
    if o == 4:
        return (x, 1 - y - h, w, h)
    if o == 5:  # transpose
        return (y, x, h, w)
    if o == 6:  # rotate 90 CW: upright(x', y') = (1 - y - h, x)
        return (1 - y - h, x, h, w)
    if o == 7:  # transverse
        return (1 - y - h, 1 - x - w, h, w)
    if o == 8:  # rotate 90 CCW
        return (y, 1 - x - w, h, w)
    return (x, y, w, h)


def load_upright(path: str, info: Optional[ImageInfo] = None) -> Tuple[np.ndarray, ImageInfo]:
    info = info or probe(path)
    arr = read_pixels(info)
    return upright(arr, info.orientation), info


def pixel_hash(arr: np.ndarray) -> str:
    h = hashlib.sha256()
    h.update(f"{arr.shape}|{arr.dtype.str}|".encode())
    h.update(np.ascontiguousarray(arr).data)
    return h.hexdigest()[:32]


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

def _source_notes(info: ImageInfo) -> List[str]:
    """What the save changes about the source regardless of output format."""
    notes = []
    if info.pages > 1:
        n = info.pages - 1
        if not info.pages_exact:
            rest = f"the other {n}+ pages were dropped"
        else:
            rest = "the other page was dropped" if n == 1 else f"the other {n} pages were dropped"
        notes.append("Multi-page TIFF: only the first page was saved; " + rest)
    if info.dropped_samples:
        notes.append(f"{info.dropped_samples} extra sample channel(s) beyond alpha were dropped")
    if info.palette:
        notes.append("Palette (indexed) colors were written as " + ("RGBA" if info.channels == 4 else "RGB"))
    if info.src_bits and info.src_bits < 8:
        notes.append(f"{info.src_bits}-bit grayscale was written as 8-bit")
    if info.trns:
        notes.append("PNG transparency color (tRNS) was written as an alpha channel")
    return notes


def _check_icc(icc: Optional[bytes], out_channels: int, notes: List[str]) -> Optional[bytes]:
    """Drop a profile whose color space does not match the output pixels."""
    from .colors import icc_matches, icc_space
    if not icc:
        return None
    if icc_matches(icc, out_channels):
        return icc
    kind = "grayscale" if out_channels in (1, 2) else "RGB"
    notes.append(f"ICC profile ({icc_space(icc) or 'unknown'} color space) does not match the {kind} output "
                 "and was not embedded")
    return None


def _strip_tiff_tags(path: str, drop: Tuple[int, ...]) -> None:
    """Remove tags from the first IFD in place (entries shift down, count shrinks)."""
    with open(path, "r+b") as fh:
        hdr = fh.read(16)
        bo = "<" if hdr[:2] == b"II" else ">"
        big = struct.unpack(bo + "H", hdr[2:4])[0] == 43
        if big:
            off = struct.unpack(bo + "Q", hdr[8:16])[0]
            cfmt, esz, nfmt = "Q", 20, "Q"
        else:
            off = struct.unpack(bo + "I", hdr[4:8])[0]
            cfmt, esz, nfmt = "H", 12, "I"
        csz = struct.calcsize(cfmt)
        fh.seek(off)
        n = struct.unpack(bo + cfmt, fh.read(csz))[0]
        entries = [fh.read(esz) for _ in range(n)]
        nxt = fh.read(struct.calcsize(nfmt))
        keep = [e for e in entries if struct.unpack(bo + "H", e[:2])[0] not in drop]
        if len(keep) == n:
            return
        fh.seek(off)
        fh.write(struct.pack(bo + cfmt, len(keep)) + b"".join(keep) + nxt + b"\0" * (esz * (n - len(keep))))


def _png_chunk(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body) & 0xffffffff)


def _write_png(path: str, arr: np.ndarray, icc: Optional[bytes], dpi, extra: Dict[str, bytes]) -> None:
    """Small PNG encoder (any of gray, gray+alpha, RGB, RGBA at 8 or 16 bits)."""
    h, w, c = arr.shape
    depth = 16 if arr.dtype == np.uint16 else 8
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[c]
    out = [b"\x89PNG\r\n\x1a\n", _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, 0))]
    if icc:
        out.append(_png_chunk(b"iCCP", b"ICC Profile\0\0" + zlib.compress(icc)))
    for k in ("gAMA", "cHRM"):
        if k in extra:
            out.append(_png_chunk(k.encode(), extra[k]))
    if "sRGB" in extra and not icc:
        out.append(_png_chunk(b"sRGB", extra["sRGB"]))
    if dpi:
        out.append(_png_chunk(b"pHYs", struct.pack(">IIB", int(round(dpi[0] / 0.0254)),
                                                   int(round(dpi[1] / 0.0254)), 1)))
    bpp = c * depth // 8
    stride = w * bpp
    data = arr.astype(">u2") if depth == 16 else arr
    rows = np.ascontiguousarray(data).view(np.uint8).reshape(h, stride)
    # filter "Sub" on every row; RLE suits 8-bit photos, 16-bit needs LZ matches
    if depth == 8:
        comp = zlib.compressobj(6, zlib.DEFLATED, 15, 9, zlib.Z_RLE)
    else:
        comp = zlib.compressobj(3, zlib.DEFLATED, 15, 9, zlib.Z_FILTERED)
    idat = []
    step = max(1, (8 << 20) // max(1, stride))
    for y0 in range(0, h, step):
        blk = rows[y0:y0 + step]
        buf = np.empty((blk.shape[0], stride + 1), np.uint8)
        buf[:, 0] = 1
        buf[:, 1:] = blk
        buf[:, 1 + bpp:] -= blk[:, :-bpp]
        idat.append(comp.compress(buf.tobytes()))
    idat.append(comp.flush())
    z = b"".join(idat)
    with open(path, "wb") as fh:
        for part in out:
            fh.write(part)
        for i in range(0, max(1, len(z)), 8 << 20):
            fh.write(_png_chunk(b"IDAT", z[i:i + (8 << 20)]))
        fh.write(_png_chunk(b"IEND", b""))


def _to8(a: np.ndarray) -> np.ndarray:
    """16 -> 8 bit, round half up (same as the preview)."""
    return ((a.astype(np.uint32) + 128) // 257).astype(np.uint8)


def _flatten_alpha(a: np.ndarray, associated: bool) -> np.ndarray:
    """Composite a gray+alpha / RGBA array over white; returns the color channels."""
    nc = a.shape[2] - 1
    maxv = float(np.iinfo(a.dtype).max)
    out = np.empty(a.shape[:2] + (nc,), a.dtype)
    step = max(1, (16 << 20) // max(1, a.shape[1] * a.shape[2] * 4))
    for y0 in range(0, a.shape[0], step):
        blk = a[y0:y0 + step].astype(np.float32)
        al = blk[:, :, nc:] / maxv
        col = blk[:, :, :nc]
        if associated:
            res = col + (1 - al) * maxv
        else:
            res = col * al + maxv * (1 - al)
        out[y0:y0 + step] = np.clip(np.round(res), 0, maxv).astype(a.dtype)
    return out


def _jpeg_pixels(arr: np.ndarray, info: ImageInfo, notes: Optional[List[str]] = None) -> np.ndarray:
    """The 8-bit gray/RGB pixels a JPEG gets for ``arr``: alpha composited over white
    (un-premultiplied for associated alpha), unspecified extra channels dropped, 16-bit
    reduced with round-half-up."""
    notes = notes if notes is not None else []
    a = arr
    c = a.shape[2]
    if c in (2, 4):
        maxv = np.iinfo(a.dtype).max
        if info.extrasamples and info.extrasamples[0] == 0:
            a = a[:, :, :c - 1]
            notes.append("Extra (non-alpha) channel dropped for JPEG")
        elif (a[:, :, -1] == maxv).all():
            a = a[:, :, :c - 1]
            notes.append("Alpha channel dropped for JPEG (fully opaque)")
        else:
            assoc = bool(info.extrasamples) and info.extrasamples[0] == 1
            a = _flatten_alpha(a, assoc)
            notes.append("Transparent areas were composited over white for JPEG")
    if a.dtype == np.uint16:
        a = _to8(a)
        notes.append("16-bit pixels reduced to 8-bit for JPEG")
    return a


def expected_output_pixels(a: np.ndarray, info: ImageInfo, out_format: str) -> np.ndarray:
    """What :func:`write_image` stores for ``a`` (e.g. the photo region) in ``out_format``,
    before any lossy encoding. PNG and TIFF keep the pixels exactly."""
    if out_format.upper() == "JPEG":
        return _jpeg_pixels(a, info)
    return a


def write_image(path: str, arr: np.ndarray, info: ImageInfo, out_format: str,
                jpeg_quality: int = 95) -> List[str]:
    """Write ``arr`` (upright) in ``out_format`` preserving what ``info`` describes.
    Returns notes for the save log, including what the conversion changed about the source
    (pages or channels dropped, palette/tRNS/low-bit expansion, tiling, compression, ...)."""
    try:
        return _write_image(path, arr, info, out_format, jpeg_quality)
    except (ImageError, MemoryError):
        raise
    except Exception as e:
        raise ImageError(f"Could not write the {out_format.upper()} file: {e}")


def _write_image(path: str, arr: np.ndarray, info: ImageInfo, out_format: str,
                 jpeg_quality: int = 95) -> List[str]:
    notes: List[str] = _source_notes(info)
    out_format = out_format.upper()
    c = arr.shape[2]
    if out_format == "TIFF":
        import tifffile
        if info.format == "TIFF":
            code = int(info.compression_code or 1)
            if code in _TIFF_KEEP:
                comp = None if code == 1 else code
            else:
                comp = 8
                if info.compression == "jpeg":
                    notes.append("JPEG-compressed TIFF was written with lossless Deflate compression")
                else:
                    notes.append(f"Source compression {info.compression} was written as Deflate")
            if info.tiled:
                notes.append("Tiled TIFF was written as striped TIFF")
            if info.planar:
                notes.append("Planar (separate) samples were written interleaved")
        else:
            comp = 8
        data = arr
        if c in (1, 2):
            photometric = "minisblack"
            if info.miniswhite and info.format == "TIFF":
                data = arr.copy()
                data[:, :, 0] = np.iinfo(arr.dtype).max - arr[:, :, 0]  # gray only, never alpha
                photometric = "miniswhite"
        else:
            photometric = "rgb"
        extrasamples = None
        if c in (2, 4):
            es = info.extrasamples[0] if info.extrasamples else 2
            extrasamples = ["unspecified", "assocalpha", "unassalpha"][es if es in (0, 1, 2) else 2]
        if c == 1:
            data = data[:, :, 0]
        icc = _check_icc(info.icc, c, notes)
        res_kwargs = {}
        has_res = False
        # arr is upright: X/Y resolution follow the upright axes (swapped for orientation 5-8)
        if info.raw_resolution and info.format == "TIFF":
            res_kwargs["resolution"] = info.upright_raw_resolution
            res_kwargs["resolutionunit"] = {1: "none", 2: "inch", 3: "centimeter"}.get(info.resolution_unit, "inch")
            has_res = True
        elif info.dpi:
            res_kwargs["resolution"] = info.upright_dpi
            res_kwargs["resolutionunit"] = "inch"
            has_res = True
        big = data.nbytes > 3_900_000_000
        if info.bigtiff and not big and info.format == "TIFF":
            notes.append("BigTIFF was written as classic TIFF")

        def _write(compression):
            kw = {}
            if compression in (5, 8, 32946, 34925, 50000) and info.format == "TIFF" and info.predictor in (2, 3):
                kw["predictor"] = 2  # horizontal differencing, safe for integer data
            tifffile.imwrite(
                path, data, photometric=photometric, planarconfig="contig", compression=compression,
                extrasamples=extrasamples, iccprofile=icc, metadata=None, software=False, bigtiff=big,
                rowsperstrip=max(1, (256 * 1024) // max(1, data.shape[1] * data.itemsize * c)),
                maxworkers=_WORKERS, **res_kwargs, **kw,
            )

        try:
            _write(comp)
        except Exception as e:
            if comp in (None, 5, 8, 32773):
                raise ImageError(f"TIFF encode failed: {e}")
            notes.append(f"Source compression {info.compression} could not be written ({e}); used Deflate")
            _write(8)
        if not has_res:
            # tifffile always writes XResolution=1 / ResolutionUnit=none; the source had none
            _strip_tiff_tags(path, (282, 283, 296))
        return notes
    if out_format == "PNG":
        icc = _check_icc(info.icc, c, notes)
        if info.extrasamples and info.extrasamples[0] == 1 and c in (2, 4):
            notes.append("Associated (premultiplied) alpha was written as PNG alpha unchanged")
        _write_png(path, np.ascontiguousarray(arr), icc, info.upright_dpi, info.png_chunks)
        return notes
    if out_format == "JPEG":
        a = _jpeg_pixels(arr, info, notes)
        icc = _check_icc(info.icc, a.shape[2], notes)
        im = _to_pil(np.ascontiguousarray(a))
        kw = {"quality": int(jpeg_quality), "subsampling": 0 if jpeg_quality >= 90 else 2}
        if icc:
            kw["icc_profile"] = icc
        if info.dpi:
            kw["dpi"] = tuple(round(v) for v in info.upright_dpi)
        im.save(path, "JPEG", **kw)
        notes.append(f"JPEG re-encoded at quality {jpeg_quality}")
        return notes
    raise ImageError(f"Unknown output format {out_format}")


def _to_pil(arr: np.ndarray) -> Image.Image:
    c = arr.shape[2]
    if c == 1:
        return Image.fromarray(arr[:, :, 0], "L")
    if c == 2:
        return Image.fromarray(arr, "LA")
    if c == 3:
        return Image.fromarray(arr, "RGB")
    return Image.fromarray(arr, "RGBA")


def output_format_for(info: ImageInfo, setting: str) -> str:
    s = (setting or "same").lower()
    if s in ("same", "same as source"):
        return info.format
    return {"tiff": "TIFF", "tif": "TIFF", "jpeg": "JPEG", "jpg": "JPEG", "png": "PNG"}.get(s, info.format)


EXT_FOR = {"TIFF": ".tif", "JPEG": ".jpg", "PNG": ".png"}
