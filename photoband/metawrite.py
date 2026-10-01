"""Metadata on output: copy the source's metadata with ExifTool, update dimensions and
orientation, remap face regions onto the new canvas, and write the photoband record.

Two ExifTool passes on the temp file:

1. copy from the source: the XMP packet as one block (``-xmp``), so custom
   namespaces survive, plus every other group tag by tag (``-all:all --xmp:all``)
   and the ICC profile; orientation, thumbnails and previews are left out.
2. our own values: record, orientation, dimensions, remapped regions and
   subject areas (a JSON import, ``-n``), xmpMM instance/history, and removal
   of what describes the old pixels (XMP-tiff orientation and structure tags,
   XMP thumbnails, Lightroom crop and local corrections, tifffile leftovers).

Then the file is read back and checked (record, dimensions); a failure raises
MetadataError so the save stops before anything is replaced.

Decisions worth knowing:

* Lightroom/Camera Raw crop (crs:Crop*, HasCrop) is turned off rather than
  remapped: a remapped crop would still cut the band off in Lightroom, and the
  band is the point of the output. Local corrections (masks, gradients, brush,
  spot removal, red eye) reference the old geometry and are removed; global
  develop settings stay. Both are reported in the notes.
* Frames: MWG regions use metadata.region_frame_orientation (AppliedToDimensions
  decides stored vs upright for orientation 5-8). MP regions have no
  AppliedToDimensions and use the file's orientation. IPTC ImageRegion and the
  EXIF/XMP subject area are handled the same way as MP (the stored pixels,
  with the EXIF orientation applied), because that is how they were
  written by the camera/tool against the stored image.
* Nothing Photoband renders (the caption) is ever written into a metadata field.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import tempfile
import uuid
import datetime as _dt
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, paths, record
from .exiftool import ExifToolError, get as get_exiftool, parse_warnings
from .imageio import ImageInfo, is_tifffile_shape_description, orient_box

log = logging.getLogger(__name__)


class MetadataError(Exception):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _opt(v) -> Optional[float]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _text(md: Dict) -> Dict:
    """ExifData.text (numbers kept as the exact text ExifTool printed) when present."""
    t = getattr(md, "text", None)
    return t if isinstance(t, dict) else md


def _r(v: float, nd: int = 6):
    v = round(float(v), nd)
    return int(v) if v == int(v) and nd == 0 else v


def _name(v) -> str:
    """Region name from a plain string or a lang-alt dict."""
    if isinstance(v, dict):
        v = v.get("x-default") or next(iter(v.values()), "")
    return str(v).strip() if v not in (None, "") else ""


def mwg_orientation(md: Dict, info: ImageInfo) -> int:
    """Orientation to apply to stored MWG region boxes (shared decision with metadata.py)."""
    from .metadata import region_frame_orientation
    return region_frame_orientation(md, info)


def mp_orientation(md: Dict, info: ImageInfo) -> int:
    """MP regions carry no AppliedToDimensions, so the MWG frame decision says
    nothing about them: they follow the file's orientation."""
    return int(info.orientation or 1)


def _frame_px(orientation: int, src_up: Tuple[int, int]) -> Tuple[int, int]:
    """Pixel size of the frame region coordinates are normalized against."""
    Wu, Hu = src_up
    return (Hu, Wu) if int(orientation or 1) in (5, 6, 7, 8) else (Wu, Hu)


def _mirrored(orientation: int) -> bool:
    return int(orientation or 1) in (2, 4, 5, 7)


class _Geo:
    """Maps upright source pixels (after orientation) to output canvas pixels."""

    def __init__(self, src_up, source_rect, photo_rect, canvas):
        self.Ws, self.Hs = src_up
        self.sx, self.sy, self.sw, self.sh = source_rect
        self.px, self.py = photo_rect[0], photo_rect[1]
        self.Wc, self.Hc = canvas

    def point(self, xn: float, yn: float, orientation: int) -> Optional[Tuple[float, float]]:
        """Normalized point in the stored frame → canvas pixels, None when cropped away."""
        x, y, _, _ = orient_box((xn, yn, 0.0, 0.0), orientation)
        X, Y = x * self.Ws - self.sx, y * self.Hs - self.sy
        if X < 0 or Y < 0 or X > self.sw or Y > self.sh:
            return None
        return self.px + X, self.py + Y

    def box(self, box, orientation: int) -> Optional[Tuple[float, float, float, float]]:
        """Normalized top-left box in the stored frame → canvas pixel box; None when
        it is entirely or mostly (more than half) cropped away."""
        x, y, w, h = orient_box(box, orientation)
        X0, Y0 = x * self.Ws - self.sx, y * self.Hs - self.sy
        X1, Y1 = X0 + w * self.Ws, Y0 + h * self.Hs
        X0c, Y0c = max(0.0, X0), max(0.0, Y0)
        X1c, Y1c = min(float(self.sw), X1), min(float(self.sh), Y1)
        if X1c - X0c <= 0 or Y1c - Y0c <= 0:
            return None
        if (X1c - X0c) * (Y1c - Y0c) < 0.5 * (X1 - X0) * (Y1 - Y0):
            return None  # mostly cropped away
        return (self.px + X0c, self.py + Y0c, X1c - X0c, Y1c - Y0c)

    def clamp(self, X: float, Y: float) -> Tuple[float, float]:
        """Upright source pixel → canvas pixel, clamped to the kept photo area."""
        X = min(max(X - self.sx, 0.0), float(self.sw))
        Y = min(max(Y - self.sy, 0.0), float(self.sh))
        return self.px + X, self.py + Y


def remap_box(box, orientation: int, src_size: Tuple[int, int], source_rect, photo_rect, canvas) -> Optional[Tuple[float, float, float, float]]:
    """Normalized top-left box in stored coords → normalized box in the output canvas."""
    g = _Geo(src_size, source_rect, photo_rect, canvas)
    b = g.box(box, orientation)
    if b is None:
        return None
    X, Y, W, H = b
    Wc, Hc = canvas
    return (X / Wc, Y / Hc, W / Wc, H / Hc)


# --------------------------------------------------------------------------
# regions
# --------------------------------------------------------------------------

def _dropped(notes: Optional[List[str]], kind: str, name: str) -> None:
    if notes is not None and name:
        msg = f"{kind} region “{name}” was left out: it is outside the kept photo area"
        if msg not in notes:
            notes.append(msg)


def _mwg_updates(mwg: Dict, md: Dict, info: ImageInfo, g: _Geo, notes) -> Dict:
    ori = mwg_orientation(md, info)
    fw, _ = _frame_px(ori, info.upright_size)
    new_list = []
    for r in mwg.get("RegionList") or []:
        if not isinstance(r, dict):
            continue
        area = dict(r.get("Area") or {}) if isinstance(r.get("Area"), dict) else {}
        cx, cy = _opt(area.get("X")), _opt(area.get("Y"))
        if str(area.get("Unit", "normalized")).strip().lower() != "normalized" or cx is None or cy is None:
            new_list.append(r)  # no usable position: keep it as it is
            continue
        w, h, d = _opt(area.get("W")), _opt(area.get("H")), _opt(area.get("D"))
        nr = dict(r)
        name = _name(r.get("Name"))
        if w is not None and h is not None and w > 0 and h > 0:
            b = g.box((cx - w / 2, cy - h / 2, w, h), ori)
            if b is None:
                _dropped(notes, "Face" if str(r.get("Type", "")).lower() == "face" else "Named", name)
                continue
            X, Y, W, H = b
            area.update({"X": _r((X + W / 2) / g.Wc), "Y": _r((Y + H / 2) / g.Hc),
                         "W": _r(W / g.Wc), "H": _r(H / g.Hc)})
            if _mirrored(ori) and _opt(r.get("Rotation")):
                # orient_box already swaps w/h for 90° turns, so a turn keeps the
                # angle; a mirror reverses its direction
                nr["Rotation"] = _r(-_opt(r.get("Rotation")))
        else:
            # a point, a zero-size box (kept as a point) or a circle
            p = g.point(cx, cy, ori)
            if p is None:
                _dropped(notes, "Face" if str(r.get("Type", "")).lower() == "face" else "Named", name)
                continue
            area.update({"X": _r(p[0] / g.Wc), "Y": _r(p[1] / g.Hc)})
            if w is not None or h is not None:
                area.update({"W": 0, "H": 0})
            if d is not None:
                area["D"] = _r(d * fw / g.Wc)  # MWG circles: diameter relative to the width
        area["Unit"] = "normalized"
        nr["Area"] = area
        new_list.append(nr)
    out = dict(mwg)
    out["AppliedToDimensions"] = {"W": g.Wc, "H": g.Hc, "Unit": "pixel"}
    out["RegionList"] = new_list
    return out


def _mp_updates(mp: Dict, md: Dict, info: ImageInfo, g: _Geo, notes) -> Dict:
    ori = mp_orientation(md, info)
    regs = []
    for r in mp.get("Regions") or []:
        if not isinstance(r, dict):
            continue
        try:
            x, y, w, h = (float(p) for p in str(r.get("Rectangle")).split(","))
        except (ValueError, TypeError, AttributeError):
            regs.append(r)  # no usable position: keep it as it is
            continue
        b = g.box((x, y, w, h), ori)
        if b is None:
            _dropped(notes, "Face", _name(r.get("PersonDisplayName")))
            continue
        X, Y, W, H = b
        nr = dict(r)
        nr["Rectangle"] = ", ".join(f"{v:.6f}" for v in (X / g.Wc, Y / g.Hc, W / g.Wc, H / g.Hc))
        regs.append(nr)
    new_mp = dict(mp)
    new_mp["Regions"] = regs
    return new_mp


def _iptc_updates(regions: List, info: ImageInfo, g: _Geo, notes) -> List:
    """IPTC 2019 ImageRegion: rectangles, circles and polygons, relative or pixel."""
    ori = int(info.orientation or 1)
    Fw, Fh = _frame_px(ori, info.upright_size)
    out = []
    for r in regions:
        if not isinstance(r, dict) or not isinstance(r.get("RegionBoundary"), dict):
            out.append(r)
            continue
        rb = dict(r["RegionBoundary"])
        unit = str(rb.get("RbUnit", "")).strip().lower()
        shape = str(rb.get("RbShape", "")).strip().lower()
        if unit not in ("relative", "pixel"):
            out.append(r)
            continue
        sx, sy = (1.0, 1.0) if unit == "relative" else (float(Fw), float(Fh))  # to normalized
        ox, oy = (float(g.Wc), float(g.Hc)) if unit == "relative" else (1.0, 1.0)  # canvas px to unit

        def out_x(v):
            return _r(v / ox, 6 if unit == "relative" else 2)

        def out_y(v):
            return _r(v / oy, 6 if unit == "relative" else 2)

        name = _name(r.get("Name"))
        nr = dict(r)
        if shape == "rectangle":
            x, y, w, h = (_opt(rb.get(k)) for k in ("RbX", "RbY", "RbW", "RbH"))
            if None in (x, y, w, h):
                out.append(r)
                continue
            b = g.box((x / sx, y / sy, w / sx, h / sy), ori) if w > 0 and h > 0 else None
            if b is None:
                p = g.point(x / sx, y / sy, ori) if not (w > 0 and h > 0) else None
                if p is None:
                    _dropped(notes, "Image", name)
                    continue
                b = (p[0], p[1], 0.0, 0.0)
            X, Y, W, H = b
            rb.update({"RbX": out_x(X), "RbY": out_y(Y), "RbW": out_x(W), "RbH": out_y(H)})
        elif shape == "circle":
            x, y, rx = (_opt(rb.get(k)) for k in ("RbX", "RbY", "RbRx"))
            if None in (x, y):
                out.append(r)
                continue
            p = g.point(x / sx, y / sy, ori)
            if p is None:
                _dropped(notes, "Image", name)
                continue
            rb.update({"RbX": out_x(p[0]), "RbY": out_y(p[1])})
            if rx is not None:
                # radius: relative to the width (relative unit), or pixels (unchanged)
                rb["RbRx"] = _r(rx * Fw / g.Wc) if unit == "relative" else _r(rx, 2)
        elif shape == "polygon":
            verts = rb.get("RbVertices")
            pts = []
            for v in verts or []:
                vx, vy = (_opt(v.get(k)) for k in ("RbX", "RbY")) if isinstance(v, dict) else (None, None)
                if vx is None or vy is None:
                    pts = None
                    break
                ux, uy, _, _ = orient_box((vx / sx, vy / sy, 0.0, 0.0), ori)
                pts.append((ux * g.Ws, uy * g.Hs))
            if not pts:
                out.append(r)
                continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            bx, by, bw, bh = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            ok = g.box((bx / g.Ws, by / g.Hs, bw / g.Ws, bh / g.Hs), 1) if bw > 0 and bh > 0 else None
            if ok is None:
                _dropped(notes, "Image", name)
                continue
            new_v = []
            for (X, Y), v in zip(pts, verts):
                cX, cY = g.clamp(X, Y)
                nv = dict(v)
                nv.update({"RbX": out_x(cX), "RbY": out_y(cY)})
                new_v.append(nv)
            rb["RbVertices"] = new_v
        else:
            out.append(r)
            continue
        nr["RegionBoundary"] = rb
        out.append(nr)
    return out


def build_region_updates(md: Dict, info: ImageInfo, source_rect, photo_rect, canvas,
                         notes: Optional[List[str]] = None) -> Dict[str, Any]:
    """JSON-import updates (values as ExifTool -n text) for MWG, MP and IPTC regions.

    Built from the text view of the metadata, so region names and other text
    that looks like a number ("1.50", "007") are written back verbatim; only the
    coordinates are converted. Regions cropped away (a box more than half
    outside, a point or circle centre outside) are left out and, when named,
    reported in ``notes``."""
    t = _text(md)
    upd: Dict[str, Any] = {}
    g = _Geo(info.upright_size, source_rect, photo_rect, canvas)
    mwg = t.get("XMP-mwg-rs:RegionInfo")
    if isinstance(mwg, dict) and mwg.get("RegionList"):
        upd["XMP-mwg-rs:RegionInfo"] = _mwg_updates(mwg, t, info, g, notes)
    mp = t.get("XMP-MP:RegionInfoMP")
    if isinstance(mp, dict) and mp.get("Regions"):
        upd["XMP-MP:RegionInfoMP"] = _mp_updates(mp, t, info, g, notes)
    ir = t.get("XMP-iptcExt:ImageRegion")
    if isinstance(ir, list) and ir:
        upd["XMP-iptcExt:ImageRegion"] = _iptc_updates(ir, info, g, notes)
    return upd


def _subject(vals: Sequence[float], info: ImageInfo, g: _Geo) -> Optional[List[int]]:
    """EXIF SubjectArea/SubjectLocation (stored-frame pixels: point, circle or
    centre rectangle) → canvas pixels, None when cropped away."""
    ori = int(info.orientation or 1)
    W, H = info.width, info.height
    if not W or not H or len(vals) not in (2, 3, 4):
        return None
    p = g.point(vals[0] / W, vals[1] / H, ori)
    if p is None:
        return None
    if len(vals) == 2:
        return [round(p[0]), round(p[1])]
    if len(vals) == 3:
        return [round(p[0]), round(p[1]), round(vals[2])]
    w, h = vals[2], vals[3]
    b = g.box(((vals[0] - w / 2) / W, (vals[1] - h / 2) / H, w / W, h / H), ori)
    if b is None:
        return None
    X, Y, BW, BH = b
    return [round(X + BW / 2), round(Y + BH / 2), round(BW), round(BH)]


def build_geometry_updates(md: Dict, info: ImageInfo, source_rect, photo_rect, canvas,
                           notes: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Other tags holding old-frame geometry: (JSON updates, ExifTool delete args).

    Subject area/location are remapped or deleted; Lightroom crop is turned off
    and local corrections are removed (see the module docstring)."""
    t = _text(md)
    upd: Dict[str, Any] = {}
    dels: List[str] = []
    g = _Geo(info.upright_size, source_rect, photo_rect, canvas)
    for key in ("ExifIFD:SubjectArea", "ExifIFD:SubjectLocation", "XMP-exif:SubjectArea",
                "XMP-exif:SubjectLocation"):
        v = t.get(key)
        if v in (None, ""):
            continue
        parts = v if isinstance(v, list) else str(v).replace(",", " ").split()
        try:
            vals = [float(p) for p in parts]
        except (TypeError, ValueError):
            dels.append(f"-{key}=")
            continue
        nv = _subject(vals, info, g)
        if nv is None:
            dels.append(f"-{key}=")
        else:
            upd[key] = nv if key.startswith("XMP") else " ".join(str(int(x)) for x in nv)
    crs = {k: v for k, v in t.items() if k.startswith("XMP-crs:")}
    if crs:
        crop = [k for k in crs if k.startswith("XMP-crs:Crop")]
        if str(crs.get("XMP-crs:HasCrop", "")).lower() in ("true", "1") or crop:
            dels += [f"-{k}=" for k in crop]
            if "XMP-crs:HasCrop" in crs:
                dels.append("-XMP-crs:HasCrop=False")
            if notes is not None and str(crs.get("XMP-crs:HasCrop", "")).lower() in ("true", "1"):
                notes.append("Lightroom crop turned off: it would cut off the band")
        local = [k for k in ("XMP-crs:MaskGroupBasedCorrections", "XMP-crs:CircularGradientBasedCorrections",
                             "XMP-crs:GradientBasedCorrections", "XMP-crs:PaintBasedCorrections",
                             "XMP-crs:RetouchInfo", "XMP-crs:RetouchAreas", "XMP-crs:RedEyeInfo")
                 if k in crs]
        if local:
            dels += [f"-{k}=" for k in local]
            if notes is not None:
                notes.append("Lightroom local adjustments removed: they refer to the old image area")
    return upd, dels


# --------------------------------------------------------------------------
# cross-format text fields
# --------------------------------------------------------------------------

def _text_field_updates(t: Dict, info: ImageInfo, out_fmt: str) -> Dict[str, Any]:
    """PNG text fields and the JPEG comment have no home in another format:
    carry them into the equivalent XMP (or native) field when that is empty."""
    if info.format == out_fmt:
        return {}
    upd: Dict[str, Any] = {}

    def has(*keys):
        return any(t.get(k) not in (None, "") for k in keys)

    def val(k):
        v = t.get(k)
        if v in (None, ""):
            return None
        v = str(v)
        # ExifTool's JSON import would decode "base64:..." into binary: never pass such values on
        return None if v.startswith("base64:") else v

    if info.format == "PNG":
        for src, dst in (("PNG:Title", "XMP-dc:Title"), ("PNG:Description", "XMP-dc:Description"),
                         ("PNG:Author", "XMP-dc:Creator"), ("PNG:Copyright", "XMP-dc:Rights")):
            if val(src) is not None and not has(dst):
                upd[dst] = val(src)
        comment = val("PNG:Comment")
    else:
        comment = val("File:Comment")
    if comment is not None:
        if out_fmt == "JPEG":
            upd["File:Comment"] = comment
        elif out_fmt == "PNG":
            upd["PNG:Comment"] = comment
        elif not has("ExifIFD:UserComment", "XMP-exif:UserComment"):
            upd["XMP-exif:UserComment"] = comment
    return upd


_PNG_SKIP_KEYS = ("XML:com.adobe.xmp", "Raw profile type", "icc", "ICC Profile")


def _png_text_chunks(path: str) -> List[Tuple[bytes, bytes]]:
    """(keyword, raw chunk bytes incl. length and CRC) for tEXt/zTXt/iTXt chunks."""
    out = []
    with open(path, "rb") as fh:
        if fh.read(8) != b"\x89PNG\r\n\x1a\n":
            return out
        while True:
            head = fh.read(8)
            if len(head) < 8:
                break
            n, typ = struct.unpack(">I4s", head)
            if typ in (b"tEXt", b"zTXt", b"iTXt"):
                data = fh.read(n)
                crc = fh.read(4)
                out.append((data.split(b"\0", 1)[0], head + data + crc))
            else:
                fh.seek(n + 4, 1)
            if typ == b"IEND":
                break
    return out


def _copy_png_custom_text(src: str, dst: str) -> int:
    """Copy PNG text chunks ExifTool can't write (custom keywords) verbatim,
    inserted before IEND. Returns the number of chunks copied."""
    have = {k for k, _ in _png_text_chunks(dst)}
    add = [raw for k, raw in _png_text_chunks(src)
           if k not in have and not any(k.decode("latin-1").startswith(s) for s in _PNG_SKIP_KEYS)]
    if not add:
        return 0
    with open(dst, "r+b") as fh:
        fh.seek(-12, 2)
        iend = fh.read(12)
        if iend[4:8] != b"IEND":
            return 0
        fh.seek(-12, 2)
        fh.write(b"".join(add) + iend)
        fh.truncate()
    return len(add)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

_XMP_TIFF_STRUCTURAL = ("BitsPerSample", "Compression", "PhotometricInterpretation", "SamplesPerPixel",
                        "PlanarConfiguration", "YCbCrSubSampling", "YCbCrPositioning", "YCbCrCoefficients")
_NOISE = ("Creating non-standard", "is not defined", "Nothing to write")


def _is_bigtiff(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            h = fh.read(4)
    except OSError:
        return False
    return h in (b"II+\x00", b"MM\x00+")


def _mime(fmt: str) -> str:
    return {"TIFF": "image/tiff", "JPEG": "image/jpeg", "PNG": "image/png"}.get(fmt, "")


_XMP_TOO_LARGE = "too large for JPEG segment"
_EXIF_DATE_TAGS = ("DateTimeOriginal", "CreateDate", "ModifyDate")
_OPEN_FAILED = ("Error opening file", "does not exist for -tagsFromFile")


def _src_arg(src: str) -> Tuple[str, Optional[str]]:
    """``src`` as a -tagsFromFile argument: (argument, temp file to remove afterwards).

    ExifTool expands %d %f %e %c (with widths, e.g. %2f) in a -tagsFromFile file
    name, and doubling the % is not reliable, so a name with a % is replaced by a
    %-free stand-in, in order of preference:

    1. a hidden hard link next to the photo (same volume, costs nothing; the temp sweep
       recognises its ``.pbtmp-src-`` name if a killed process leaves it behind);
    2. on POSIX, a symlink in the app's temp folder (ExifTool opens it like the file);
    3. a hard link, and only then a full copy, in the app's temp folder.

    The stand-in's name carries its creation time, so the startup sweep can tell a leftover
    from a link another save is still using."""
    if "%" not in src:
        return src, None
    from .util import src_link_name
    ext = os.path.splitext(src)[1].replace("%", "")
    tried: List[str] = []

    def attempt(kind: str, dst: str) -> bool:
        try:
            if kind == "link":
                os.link(src, dst)
            elif kind == "symlink":
                os.symlink(os.path.abspath(src), dst)
            else:
                shutil.copyfile(src, dst)
            return True
        except (OSError, NotImplementedError) as e:
            tried.append(f"{kind}: {e}")
            log.debug("%s of %s at %s failed", kind, src, dst, exc_info=True)
            try:
                if os.path.lexists(dst):
                    os.unlink(dst)
            except OSError:
                log.debug("could not remove %s", dst, exc_info=True)
            return False

    here = os.path.dirname(os.path.abspath(src))
    if "%" not in here:
        dst = os.path.join(here, src_link_name(ext))
        if attempt("link", dst):
            return dst, dst
    tmp_dirs = []
    for d in (paths.sub("tmp"), tempfile.gettempdir()):
        if "%" not in d and d not in tmp_dirs:
            tmp_dirs.append(d)
    kinds = (["symlink"] if os.name != "nt" else []) + ["link", "copy"]
    for kind in kinds:
        for d in tmp_dirs:
            dst = os.path.join(d, src_link_name(ext)[len(".pbtmp-"):])
            if attempt(kind, dst):
                return dst, dst
    if not tmp_dirs and "%" in here:
        raise MetadataError("The temp folder path contains a % sign, so metadata can't be copied.")
    raise MetadataError("Could not prepare the source for metadata copying: " + "; ".join(tried[-2:]))


def _check_open(err: str) -> None:
    """With -m, a source ExifTool could not open is only a warning: make it fatal."""
    for ln in (err or "").splitlines():
        if any(m in ln for m in _OPEN_FAILED):
            raise MetadataError("Copying metadata failed: ExifTool could not open the source file.")


def _check_too_large(err: str) -> None:
    if _XMP_TOO_LARGE in (err or ""):
        raise MetadataError("Writing metadata failed: the XMP metadata is too large for the JPEG and could "
                            "not be written as extended XMP.")


def _dc_missing(src_t: Dict, out_md: Dict) -> List[str]:
    """XMP-dc tags the source has that the output lacks."""
    return sorted(k for k, v in src_t.items()
                  if k.startswith("XMP-dc:") and v not in (None, "", [], {}) and k not in out_md)


def _verify(et, tmp: str, out_fmt: str, canvas, rec: Dict, src_t: Optional[Dict] = None) -> Dict:
    try:
        md2 = et.read_json(tmp)
    except ExifToolError as e:
        raise MetadataError(f"Metadata check failed: {e}")
    got = record.from_metadata(md2)
    if not got or got.get("photoHash") != rec.get("photoHash") or list(got.get("canvas") or []) != list(rec.get("canvas") or []):
        raise MetadataError("Metadata check failed: the Photoband record was not written.")
    Wc, Hc = canvas
    dim = {"TIFF": ("IFD0:ImageWidth", "IFD0:ImageHeight"), "JPEG": ("File:ImageWidth", "File:ImageHeight"),
           "PNG": ("PNG:ImageWidth", "PNG:ImageHeight")}[out_fmt]
    checks = [dim, ("ExifIFD:ExifImageWidth", "ExifIFD:ExifImageHeight"),
              ("XMP-exif:ExifImageWidth", "XMP-exif:ExifImageHeight"), ("XMP-tiff:ImageWidth", "XMP-tiff:ImageHeight")]
    for i, (kw, kh) in enumerate(checks):
        if i and kw not in md2 and kh not in md2:
            continue
        if (_num(md2.get(kw), -1), _num(md2.get(kh), -1)) != (Wc, Hc):
            raise MetadataError(f"Metadata check failed: {kw.split(':')[0]} reports "
                                f"{md2.get(kw)}×{md2.get(kh)} instead of {Wc}×{Hc}.")
    for k in md2:
        if k.endswith((":ThumbnailImage", ":PreviewImage", ":PhotoshopThumbnail")):
            raise MetadataError("Metadata check failed: an old embedded thumbnail is still present.")
    if src_t:
        missing = _dc_missing(src_t, md2)
        if missing:
            raise MetadataError("Metadata check failed: " + ", ".join(k.split(":", 1)[1] for k in missing)
                                + " from the source did not reach the output.")
    return md2


def write_metadata(tmp: str, src: str, md: Dict, info: ImageInfo, out_fmt: str, canvas_size, photo_rect,
                   source_rect, rec: Dict) -> List[str]:
    """Copy and update metadata on ``tmp``. Returns notes for the save log (dropped
    regions, Lightroom settings changed, ExifTool warnings). Raises MetadataError."""
    et = get_exiftool()
    notes: List[str] = []
    warn: List[str] = []
    Wc, Hc = canvas_size
    t = _text(md)
    src = os.path.abspath(src)
    if out_fmt == "TIFF" and _is_bigtiff(tmp):
        raise MetadataError("Files over 4 GB can't carry metadata (ExifTool can't write BigTIFF). "
                            "Save a smaller image or a JPEG instead.")

    desc = t.get("IFD0:ImageDescription")
    stale_desc = is_tifffile_shape_description(desc)
    stale_sw = str(t.get("IFD0:Software", "")).strip() == "tifffile.py"

    # pass 1: copy
    # The ICC profile is written by imageio.write_image, which drops a profile that doesn't match
    # the output's color space; copying it here again would undo that.
    p1 = ["-tagsFromFile", src, "-all:all", "--xmp:all", "-xmp", "--icc_profile:all",
          "--Orientation", "--ThumbnailImage", "--PreviewImage", "--PhotoshopThumbnail", "--IFD1:all"]
    if stale_desc:
        p1.append("--IFD0:ImageDescription")
    if stale_sw:
        p1.append("--IFD0:Software")
    # orientations 5-8: the output is turned a quarter, so its X and Y resolution are
    # the source's swapped (imageio writes them so); never copy the source's over them
    turned = int(info.orientation or 1) in (5, 6, 7, 8)
    if turned:
        p1 += ["--XResolution", "--YResolution"]
    # EXIF dates are copied raw: the print-converted copy silently drops partial dates
    # ("1952:06:00 00:00:00", a common way to store a month or year only) as invalid
    p1 += [f"-{k}#<{k}#" for k in t if k.partition(":")[0] in ("IFD0", "ExifIFD")
           and k.partition(":")[2] in _EXIF_DATE_TAGS]

    # pass 2: our values (-n: raw values, as read)
    p2 = ["-n", "-XMP-photoband:all=", f"-XMP-photoband:Version={__version__}",
          f"-XMP-photoband:Record={record.encode(rec)}",
          "-XMP-tiff:Orientation=", "-XMP-xmp:Thumbnails=",
          "-IFD1:all=", "-ThumbnailImage=", "-PreviewImage=", "-Photoshop:PhotoshopThumbnail="]
    if any(k.endswith(":Orientation") and not k.startswith("XMP") for k in t):
        p2.append("-IFD0:Orientation=1")
    p2 += [f"-XMP-tiff:{k}=" for k in _XMP_TIFF_STRUCTURAL if f"XMP-tiff:{k}" in t]
    # dimension tags: only those the source has (never create an ExifIFD just for them)
    if "ExifIFD:ExifImageWidth" in t or "ExifIFD:ExifImageHeight" in t:
        p2 += [f"-ExifIFD:ExifImageWidth={Wc}", f"-ExifIFD:ExifImageHeight={Hc}"]
    if "XMP-exif:ExifImageWidth" in t or "XMP-exif:ExifImageHeight" in t:
        p2 += [f"-XMP-exif:ExifImageWidth={Wc}", f"-XMP-exif:ExifImageHeight={Hc}"]
    if "XMP-tiff:ImageWidth" in t or "XMP-tiff:ImageHeight" in t:
        p2 += [f"-XMP-tiff:ImageWidth={Wc}", f"-XMP-tiff:ImageHeight={Hc}"]
    if out_fmt == "TIFF" and (stale_desc or desc in (None, "")):
        p2.append("-IFD0:ImageDescription=")
    if out_fmt == "TIFF" and (stale_sw or t.get("IFD0:Software") in (None, "")):
        p2.append("-IFD0:Software=")
    dpi = info.upright_dpi
    if out_fmt == "PNG" and dpi:
        p2 += [f"-PNG:PixelsPerUnitX={round(dpi[0] / 0.0254)}",
               f"-PNG:PixelsPerUnitY={round(dpi[1] / 0.0254)}", "-PNG:PixelUnits=1"]
    if turned:
        for grp in ("IFD0", "XMP-tiff"):
            if out_fmt == "TIFF" and grp == "IFD0":
                continue  # imageio wrote them, already swapped
            xr, yr = t.get(f"{grp}:XResolution"), t.get(f"{grp}:YResolution")
            if xr in (None, "") and yr in (None, ""):
                continue
            p2 += [f"-{grp}:XResolution={'' if yr is None else yr}",
                   f"-{grp}:YResolution={'' if xr is None else xr}"]
    if "Photoshop:IPTCDigest" in t and out_fmt in ("JPEG", "TIFF"):
        p2.append("-Photoshop:IPTCDigest=new")
    # xmpMM: a new instance of the same document
    iid = f"xmp.iid:{uuid.uuid4()}"
    when = _dt.datetime.now().astimezone().strftime("%Y:%m:%d %H:%M:%S%z")
    when = when[:-2] + ":" + when[-2:]
    p2 += [f"-XMP-xmpMM:InstanceID={iid}",
           f"-XMP-xmpMM:History+={{Action=saved,InstanceID={iid},When={when},"
           f"SoftwareAgent=Photoband {__version__},Changed=/}}"]
    if info.format != out_fmt:
        p2[-1:-1] = [f"-XMP-xmpMM:History+={{Action=converted,Parameters=from {_mime(info.format)} "
                     f"to {_mime(out_fmt)}}}"]

    updates = build_region_updates(md, info, source_rect, photo_rect, (Wc, Hc), notes)
    gupd, gdel = build_geometry_updates(md, info, source_rect, photo_rect, (Wc, Hc), notes)
    updates.update(gupd)
    updates.update(_text_field_updates(t, info, out_fmt))
    p2 += gdel

    jpath = None
    if updates:
        fd, jpath = tempfile.mkstemp(suffix=".json", dir=paths.sub("tmp"))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([{"SourceFile": "*", **updates}], fh, ensure_ascii=False)
        p2 += ["-struct", f"-json={jpath}"]
    src_link = None
    try:
        src_arg, src_link = _src_arg(src)
        p1[1] = src_arg
        err = et.write(tmp, p1, require_change=False)
        _check_open(err)
        w1 = parse_warnings(err)
        big_xmp = out_fmt == "JPEG" and _XMP_TOO_LARGE in err
        if big_xmp:
            # the XMP packet does not fit one JPEG segment: copy it tag by tag below,
            # so ExifTool splits it into standard + extended XMP
            w1 = [w for w in w1 if _XMP_TOO_LARGE not in w]
            notes.append("XMP over 64 KB was written as JPEG extended XMP (tags in namespaces "
                         "ExifTool does not know are not carried)")
        warn += w1
        if big_xmp or any(k.endswith("xmpNote:HasExtendedXMP") for k in t):
            # JPEG extended XMP (packets over 64 KB) is not part of the XMP
            # block: copy the known tags on top (the block kept custom ones)
            err = et.write(tmp, ["-tagsFromFile", src_arg, "-xmp:all"], require_change=False)
            _check_open(err)
            if out_fmt == "JPEG":
                _check_too_large(err)
            warn += parse_warnings(err)
        err = et.write(tmp, p2)
        if out_fmt == "JPEG":
            _check_too_large(err)
        warn += parse_warnings(err)
    except ExifToolError as e:
        raise MetadataError(f"Writing metadata failed: {e}")
    finally:
        for f in (jpath, src_link):
            if f:
                try:
                    os.unlink(f)
                except OSError:
                    log.debug("could not remove %s", f, exc_info=True)
    if out_fmt == "PNG" and info.format == "PNG":
        try:
            _copy_png_custom_text(info.path or src, tmp)
        except OSError:
            log.debug("copying PNG text chunks failed", exc_info=True)
    _verify(et, tmp, out_fmt, (Wc, Hc), rec, t)

    if out_fmt == "PNG" and info.icc and out_fmt != info.format:
        notes.append("ICC profile carried into the PNG")
    for w in warn:
        if not any(n in w for n in _NOISE) and f"ExifTool: {w}" not in notes:
            notes.append(f"ExifTool: {w}")
    return notes
