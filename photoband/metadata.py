"""Read metadata with ExifTool and normalize it into the fixed token field set."""
from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from captiontokens.dates import parse_date

from .imageio import ImageInfo, orient_box

EXIF_GROUPS = ("IFD0", "ExifIFD", "IFD1", "EXIF")


def _num(v, default=0.0) -> float:
    """ExifTool numbers arrive as strings (read_json keeps number text as-is)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f and f not in (float("inf"), float("-inf")) else default


def _opt_num(v) -> Optional[float]:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    f = _num(v, None)
    return f


def _nl(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")


def _cp1252_byte(ch: str) -> int:
    try:
        b = ch.encode("cp1252")
    except UnicodeEncodeError:
        if ord(ch) < 256:
            return ord(ch)
        raise
    return b[0]


def _fix_iptc_utf8(s: str) -> str:
    """IPTC written as UTF-8 without CodedCharacterSet is decoded by ExifTool
    as Latin-1/cp1252 ("CafÃ©"). Re-decode when the bytes are valid UTF-8."""
    if s.isascii():
        return s
    try:
        return bytes(_cp1252_byte(c) for c in s).decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError):
        return s


def _iptc_is_utf8(md: Dict[str, Any]) -> bool:
    cs = str(md.get("IPTC:CodedCharacterSet") or "")
    return "%G" in cs or "utf" in cs.lower()


def _fix_value(v, key: str, md: Dict[str, Any]):
    if not key.startswith("IPTC:") or _iptc_is_utf8(md):
        return v
    if isinstance(v, str):
        return _fix_iptc_utf8(v)
    if isinstance(v, list):
        return [_fix_iptc_utf8(x) if isinstance(x, str) else x for x in v]
    return v


def _present(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if isinstance(v, (list, dict)) and not v:
        return False
    return True


def _candidates(md: Dict[str, Any], k: str) -> List[str]:
    """Keys to try for k: any EXIF IFD group for "EXIF:Tag"; for XMP tags the
    key itself, then its language variants ("XMP-dc:Title-de")."""
    grp, _, tag = k.partition(":")
    if grp == "EXIF":
        return [f"{g}:{tag}" for g in EXIF_GROUPS]
    cands = [k]
    if grp.startswith("XMP"):
        pre = k + "-"
        cands += [x for x in md if x.startswith(pre) and x != k]
    return cands


def _iter_sources(md: Dict[str, Any], keys):
    for k in keys:
        for c in _candidates(md, k):
            v = md.get(c)
            if _present(v):
                yield _fix_value(v, c, md), c


def _get(md: Dict[str, Any], *keys: str):
    """First non-empty value among keys. A key without a group ("EXIF:Tag")
    matches any EXIF IFD group; an XMP lang-alt tag falls back to any language."""
    for v, c in _iter_sources(md, keys):
        return v, c
    return None, None


def _text(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, dict):  # lang-alt struct
        v = v.get("x-default") or next((x for x in v.values() if x), "")
    if isinstance(v, list):
        v = ", ".join(str(x).strip() for x in v if str(x).strip())
    s = _nl(str(v)).strip()
    return s or None


def _list(v) -> List[str]:
    """List-type tag values. ExifTool -j gives a list for several items and a
    plain string for one; a single string is never split on commas."""
    if v is None:
        return []
    if not isinstance(v, list):
        v = [v]
    return [_nl(str(x)).strip() for x in v if str(x).strip()]


FIELD_SOURCES = {
    "title": ["XMP-dc:Title", "IPTC:ObjectName", "XMP-photoshop:Headline", "EXIF:XPTitle"],
    "caption": ["XMP-dc:Description", "IPTC:Caption-Abstract", "EXIF:ImageDescription", "EXIF:XPComment"],
    # when the photo was taken; never the scan / file date, which has its own field below
    "date": ["XMP-photoshop:DateCreated", "EXIF:DateTimeOriginal", "IPTC:DateCreated", "XMP-exif:DateTimeOriginal"],
    # when it was scanned or the file was made: on a scan this is the scan date
    "digitized": ["EXIF:CreateDate", "XMP-xmp:CreateDate", "XMP-exif:DateTimeDigitized", "IPTC:DigitalCreationDate"],
    "creator": ["XMP-dc:Creator", "IPTC:By-line", "EXIF:Artist"],
    "sublocation": ["XMP-iptcCore:Location", "IPTC:Sub-location"],
    "city": ["XMP-photoshop:City", "IPTC:City"],
    "state": ["XMP-photoshop:State", "IPTC:Province-State"],
    "country": ["XMP-photoshop:Country", "IPTC:Country-PrimaryLocationName"],
}


def _iou(a, b) -> float:
    if a is None or b is None:
        return 0.0
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _name(v) -> str:
    """Region names may arrive as numbers (``"Name": 1952``) or lang-alt dicts."""
    return _text(v) or ""


def _atd(md: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    mwg = md.get("XMP-mwg-rs:RegionInfo")
    atd = mwg.get("AppliedToDimensions") if isinstance(mwg, dict) else None
    if not isinstance(atd, dict):
        return None
    aw, ah = _num(atd.get("W")), _num(atd.get("H"))
    return (aw, ah) if aw > 0 and ah > 0 else None


def _lightroom_regions(md: Dict[str, Any]) -> bool:
    """True when the MWG regions were written by Lightroom.

    Lightroom (LR6 to Classic 14 at least) normalizes region boxes against the
    STORED pixels, like the MWG spec, but writes AppliedToDimensions with the
    UPRIGHT (displayed) size, so for orientation 5-8 its AppliedToDimensions
    looks like an upright-frame declaration while the boxes are not. Its
    regions carry a per-region mwg-rs:Rotation, which is not part of MWG 2.0
    (ExifTool: "observed in LR6 XMP"); the XMP CreatorTool names it too."""
    mwg = md.get("XMP-mwg-rs:RegionInfo")
    regions = mwg.get("RegionList") if isinstance(mwg, dict) else None
    if isinstance(regions, list) and any(isinstance(r, dict) and "Rotation" in r for r in regions):
        return True
    tool = _text(md.get("XMP-xmp:CreatorTool")) or ""
    return "lightroom" in tool.lower()


def region_frame_orientation(md: Dict[str, Any], info: ImageInfo) -> int:
    """Orientation to apply to stored region boxes (MWG and MP).

    Regions are normally written against the stored pixels, so the EXIF
    orientation applies. For orientation 5-8, a tool may have written them
    against the upright image instead; AppliedToDimensions tells which: its
    aspect ratio is compared with the stored and the upright frame (it may be
    a scaled copy), and 1 is returned when it matches the upright frame.
    Lightroom is the exception: its AppliedToDimensions is always the upright
    size while its boxes are in the stored frame (see _lightroom_regions)."""
    o = int(info.orientation or 1)
    md = _text_view(md)
    atd = _atd(md)
    if o not in (5, 6, 7, 8) or atd is None or not info.width or not info.height:
        return o
    if _lightroom_regions(md):
        return o
    aw, ah = atd
    r = math.log(aw / ah)
    stored = abs(r - math.log(info.width / info.height))
    upright = abs(r - math.log(info.height / info.width))
    return 1 if upright < stored else o


def _dims_mismatch(md: Dict[str, Any], info: ImageInfo) -> bool:
    """AppliedToDimensions is neither the stored nor the upright pixel size (a
    scaled or edited copy). Which of the two it names says nothing here: Lightroom
    writes the upright size for stored-frame boxes."""
    atd = _atd(md)
    if atd is None or not info.width or not info.height:
        return False
    aw, ah = atd

    def off(fw, fh):
        return abs(aw - fw) / fw > 0.01 or abs(ah - fh) / fh > 0.01
    return off(info.width, info.height) and off(info.height, info.width)


def _mwg_box(area, rotate: int):
    """Upright top-left box from an MWG Area, or None when it is missing,
    incomplete or not normalized."""
    if not isinstance(area, dict):
        return None
    if str(area.get("Unit", "normalized")).strip().lower() != "normalized":
        return None
    vals = [_opt_num(area.get(k)) for k in ("X", "Y", "W", "H")]
    if any(v is None for v in vals):
        return None
    cx, cy, w, h = vals
    if w < 0 or h < 0:
        return None
    return list(orient_box((cx - w / 2, cy - h / 2, w, h), rotate))


def _mp_box(rect, rotate: int):
    if rect is None:
        return None
    try:
        parts = [float(p) for p in str(rect).split(",")]
    except (TypeError, ValueError):
        return None
    if len(parts) != 4 or parts[2] < 0 or parts[3] < 0:
        return None
    return list(orient_box(tuple(parts), rotate))


def _text_view(md: Dict[str, Any]) -> Dict[str, Any]:
    """photoband.exiftool.ExifData keeps number text verbatim in ``.text``
    ("1.50" stays "1.50"); plain dicts are used as they are. Everything here
    accepts numbers either as numbers or as strings."""
    t = getattr(md, "text", None)
    return t if isinstance(t, dict) else md


def parse_regions(md: Dict[str, Any], info: ImageInfo) -> Dict[str, Any]:
    """Face regions from MWG and MP, converted to upright normalized top-left boxes.
    A region without usable coordinates keeps its name with box None."""
    md = _text_view(md)
    warnings: List[str] = []
    rotate = region_frame_orientation(md, info)
    if _dims_mismatch(md, info):
        warnings.append("region dimensions mismatch")
    mwg_named: List[Dict[str, Any]] = []
    mwg_unnamed: List[Dict[str, Any]] = []
    mwg = md.get("XMP-mwg-rs:RegionInfo")
    regions = mwg.get("RegionList") if isinstance(mwg, dict) else None
    for r in regions if isinstance(regions, list) else []:
        if not isinstance(r, dict):
            continue
        if str(r.get("Type", "Face")).strip().lower() != "face":
            continue
        name = _name(r.get("Name"))
        entry = {"name": name, "box": _mwg_box(r.get("Area"), rotate), "source": "MWG"}
        (mwg_named if name else mwg_unnamed).append(entry)
    named, unnamed = list(mwg_named), list(mwg_unnamed)
    mp = md.get("XMP-MP:RegionInfoMP")
    mp_regions = mp.get("Regions") if isinstance(mp, dict) else None
    for r in mp_regions if isinstance(mp_regions, list) else []:
        if not isinstance(r, dict):
            continue
        name = _name(r.get("PersonDisplayName"))
        entry = {"name": name, "box": _mp_box(r.get("Rectangle"), int(info.orientation or 1)), "source": "MP"}  # MP has no ATD: file orientation
        if name:
            # the same person named by both sources: keep the MWG one
            dup = any(e["name"].lower() == name.lower() and
                      (_iou(e["box"], entry["box"]) > 0.5 or e["box"] is None or entry["box"] is None)
                      for e in mwg_named)
            if not dup:
                named.append(entry)
        else:
            if entry["box"] is None or not any(_iou(e["box"], entry["box"]) > 0.5 for e in mwg_unnamed):
                unnamed.append(entry)
    # an unnamed region on the same face as a named one (either source) is not extra
    unnamed = [u for u in unnamed if not any(_iou(u["box"], n["box"]) > 0.5 for n in named)]
    if not named:
        pii = _list(_get(md, "XMP-iptcExt:PersonInImage")[0])
        if pii:
            named = [{"name": n, "box": None, "source": "PersonInImage"} for n in pii]
            warnings.append("names without positions")
    elif any(n["box"] is None for n in named):
        warnings.append("names without positions")
    if not named and not unnamed:
        warnings.append("no face regions")
    return {"named": named, "unnamed_count": len(unnamed), "unnamed": unnamed,
            "has_positions": any(n["box"] is not None for n in named), "warnings": warnings}


def normalize(md: Dict[str, Any], info: ImageInfo) -> Dict[str, Any]:
    md = _text_view(md)
    fields: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for key, srcs in FIELD_SOURCES.items():
        if key in ("date", "digitized"):
            # first source whose value parses; "0000:00:00" must not block IPTC/XMP, but
            # a real value that is no exact date ("1950s", "circa 1950") stops the search:
            # it prints as written, and a lower source must not stand in for it
            v = src = None
            first = None
            for cand, c in _iter_sources(md, srcs):
                if first is None:
                    first = (cand, c)
                if parse_date(_text(cand)) is not None or re.search(r"[1-9]", _text(cand) or ""):
                    v, src = cand, c
                    break
            if v is None and first is not None:
                v, src = first
            fields[key] = _text(v)
        else:
            v, src = _get(md, *srcs)
            fields[key] = _text(v)
        if src:
            sources[key] = src
    kws: List[str] = []
    seen = set()
    for src in ("XMP-dc:Subject", "IPTC:Keywords"):
        for k in _list(_fix_value(md.get(src), src, md)):
            if k.lower() not in seen:
                seen.add(k.lower())
                kws.append(k)
    fields["keywords"] = kws
    fields["keyword_paths"] = _list(md.get("XMP-lr:HierarchicalSubject"))
    base = os.path.basename(info.path)
    fields["filename"] = base
    fields["stem"] = os.path.splitext(base)[0]
    fields["folder"] = os.path.basename(os.path.dirname(os.path.abspath(info.path)))
    faces = parse_regions(md, info)
    fields["faces"] = faces["named"]
    fields["faces_unnamed_count"] = faces["unnamed_count"]
    return {"fields": fields, "sources": sources, "faces": faces, "warnings": list(faces["warnings"])}


def raw_listing(md: Dict[str, Any], limit: int = 400) -> List[Tuple[str, str]]:
    """Flat (tag, value) list for the Metadata tab / Try-on panel."""
    md = _text_view(md)
    out = []
    skip_groups = ("System:", "File:", "ExifTool:")
    for k, v in md.items():
        if k == "SourceFile" or k.startswith(skip_groups):
            continue
        if isinstance(v, (dict, list)):
            import json
            s = json.dumps(v, ensure_ascii=False)
        else:
            s = str(v)
        if len(s) > 500:
            s = s[:500] + "…"
        out.append((k, s))
        if len(out) >= limit:
            break
    return out
