"""Photo details edited in Photoband: validated, applied to the caption fields, and written into
a file's metadata (a saved copy, an overwrite, or the original alone via ``save_details``).

An edit set (``edits``) is the draft's ``meta``::

    {"title": "Picnic", "caption": "", ...}       a text field present = edited ("" = clear)
    {"keywords": ["family", "picnic"]}           the whole visible list (no "DATE:" markers)
    {"date": {"iso": "1952-06", "level": "month"}}   or None to clear the date
    {"faces": {"<face key>": {"name": .., "box": [x, y, w, h] | None, "deleted": True,
                              "was": {"name": .., "box": ..}},
               "new:<id>": {"name": .., "box": [..]}}}

Face keys are the reader's (metadata.parse_regions): the ids of the regions a face was read from.

What is written, per field (the sync rule): the field's primary tag; its mirrors (the same value in
another standard: IPTC, EXIF, Windows XP tags) when the file already has them; and when a field is
cleared, also the other tags the reader falls back to, so an old value can't come back. IPTC is
never created, and never re-flagged: a value IPTC can't hold (beyond cp1252 in a file not flagged
UTF-8, or over the IIM length limit) is removed from IPTC instead, and XMP holds it (the reader
prefers XMP). In a PNG no EXIF block is created: XMP-exif tags take the EXIF values.
"""
from __future__ import annotations

import copy
import datetime as _dt
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from captiontokens.dates import PartialDate, apply_certainty, parse_date

from .imageio import ImageInfo, is_tifffile_shape_description, orient_box
from .metadata import (EXIF_GROUPS, _fix_iptc_utf8, _iptc_is_utf8, _lightroom_region, _text_view,
                       region_frame_orientation)


class EditError(ValueError):
    """An edit set that can't be written as given (shown to the user)."""


TEXT_FIELDS = ("title", "caption", "notes", "creator", "sublocation", "city", "state", "country")
MULTILINE = ("caption", "notes")
LEVELS = {"day": "Y!M!D!", "month": "Y!M!", "year": "Y!", "circa": "Y~"}
# photokin's date-certainty keyword, and only that: a keyword like "Date: ask Ann" is the user's own
DATE_MARKER = re.compile(r"\s*DATE:\s*Y[!?~@](?:M[!?~@])?(?:D[!?~@])?\s*$", re.IGNORECASE)

MAX_TEXT = 64 * 1024
MAX_LINE = 2000
MAX_KEYWORDS = 500
MAX_KEYWORD = 200
MAX_FACES = 500

# IPTC IIM 4.2 maximum lengths in bytes
IPTC_MAX = {"IPTC:ObjectName": 64, "IPTC:Caption-Abstract": 2000, "IPTC:By-line": 32, "IPTC:Sub-location": 32,
            "IPTC:City": 32, "IPTC:Province-State": 32, "IPTC:Country-PrimaryLocationName": 64,
            "IPTC:SpecialInstructions": 256, "IPTC:Keywords": 64}

# field -> (primary, mirrors, fallbacks). "EXIF:" means whichever IFD the file has it in.
SPEC: Dict[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    "title": ("XMP-dc:Title", ("IPTC:ObjectName", "EXIF:XPTitle"), ("XMP-photoshop:Headline",)),
    "caption": ("XMP-dc:Description", ("IPTC:Caption-Abstract", "EXIF:ImageDescription"), ("EXIF:XPComment",)),
    "notes": ("ExifIFD:UserComment", ("XMP-exif:UserComment",),
              ("XMP-photoshop:Instructions", "IPTC:SpecialInstructions")),
    "creator": ("XMP-dc:Creator", ("IPTC:By-line", "EXIF:Artist", "EXIF:XPAuthor"), ()),
    "sublocation": ("XMP-iptcCore:Location", ("IPTC:Sub-location",), ()),
    "city": ("XMP-photoshop:City", ("IPTC:City",), ()),
    "state": ("XMP-photoshop:State", ("IPTC:Province-State",), ()),
    "country": ("XMP-photoshop:Country", ("IPTC:Country-PrimaryLocationName",), ()),
}
LIST_TAGS = {"XMP-dc:Creator", "IPTC:By-line"}
# EXIF text in a JPEG (one 64 KB APP1 segment for everything): longer values stay in XMP only
EXIF_JPEG_MAX = 16000
DATE_TAGS_ORIGINAL = ("ExifIFD:DateTimeOriginal", "XMP-exif:DateTimeOriginal")
DATE_CLEAR = ("ExifIFD:DateTimeOriginal", "XMP-exif:DateTimeOriginal", "XMP-photoshop:DateCreated",
              "IPTC:DateCreated", "IPTC:TimeCreated", "ExifIFD:OffsetTimeOriginal", "ExifIFD:SubSecTimeOriginal")


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BREAKS = re.compile("[\n\u0085\u2028\u2029]")


def _check_text(field: str, v: Any, multiline: bool, limit: int) -> str:
    if not isinstance(v, str):
        raise EditError(f"{field} must be text")
    v = v.replace("\r\n", "\n").replace("\r", "\n")
    if _CTRL.search(v):
        raise EditError(f"{field} contains control characters")
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        raise EditError(f"{field} contains characters that can't be stored")
    if not multiline and _BREAKS.search(v):
        raise EditError(f"{field} can't contain line breaks")
    if len(v) > limit:
        raise EditError(f"{field} is longer than {limit} characters")
    if v.lstrip().lower().startswith("base64:"):
        # ExifTool's JSON import would decode such a value into binary data
        raise EditError(f"{field} can't start with “base64:”")
    return v


def _check_box(b: Any, where: str) -> Optional[List[float]]:
    if b is None:
        return None
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        raise EditError(f"{where}: a face box is four numbers")
    try:
        x, y, w, h = (float(v) for v in b)
    except (TypeError, ValueError):
        raise EditError(f"{where}: a face box is four numbers")
    if any(v != v for v in (x, y, w, h)) or w <= 0 or h <= 0 or x < -1e-6 or y < -1e-6 \
            or x + w > 1 + 1e-6 or y + h > 1 + 1e-6:
        raise EditError(f"{where}: the face box is outside the photo")
    return [min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0), min(w, 1.0), min(h, 1.0)]


def _check_was_box(b: Any) -> Optional[List[float]]:
    """The box a face had in the file (only to recognise the face): four numbers, as the file has
    them (a box a little outside the photo, as some tools write, is fine here)."""
    if b is None:
        return None
    try:
        vals = [float(v) for v in b] if isinstance(b, (list, tuple)) and len(b) == 4 else None
    except (TypeError, ValueError):
        vals = None
    if vals is None or any(v != v or v in (float("inf"), float("-inf")) for v in vals):
        raise EditError("A face: a face box is four numbers")
    return vals


def validate(edits: Any) -> Dict[str, Any]:
    """The edit set, checked and normalized (line endings, box clamping). Raises EditError."""
    if edits is None:
        return {}
    if not isinstance(edits, dict):
        raise EditError("details must be an object")
    out: Dict[str, Any] = {}
    for k, v in edits.items():
        if k in TEXT_FIELDS:
            out[k] = _check_text(k, v, k in MULTILINE, MAX_TEXT if k in MULTILINE else MAX_LINE).strip()
        elif k == "keywords":
            if not isinstance(v, list) or len(v) > MAX_KEYWORDS:
                raise EditError(f"keywords must be a list of at most {MAX_KEYWORDS}")
            seen, kws = set(), []
            for kw in v:
                kw = _check_text("A keyword", kw, False, MAX_KEYWORD).strip()
                if kw and kw.lower() not in seen and not DATE_MARKER.match(kw):
                    seen.add(kw.lower())
                    kws.append(kw)
            out[k] = kws
        elif k == "date":
            out[k] = _check_date(v)
        elif k == "faces":
            out[k] = _check_faces(v)
        else:
            raise EditError(f"unknown detail “{k}”")
    return out


def _check_date(v: Any) -> Optional[Dict[str, str]]:
    if v is None:
        return None
    if not isinstance(v, dict) or v.get("level") not in LEVELS or not isinstance(v.get("iso"), str):
        raise EditError("date must be {iso, level}")
    iso, level = v["iso"].strip(), v["level"]
    m = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", iso)
    if not m:
        raise EditError("date must be written as YYYY, YYYY-MM or YYYY-MM-DD")
    want = {"day": 3, "month": 2, "year": 1, "circa": 1}[level]
    if sum(1 for g in m.groups() if g) != want:
        raise EditError(f"a {level} date is written as " + ("YYYY-MM-DD", "YYYY-MM", "YYYY")[3 - want])
    y = int(m.group(1))
    if not 1000 <= y <= _dt.date.today().year + 1:
        raise EditError(f"{y} is not a year a photo was taken")
    try:
        PartialDate(y, int(m.group(2) or 0), int(m.group(3) or 0))
    except ValueError:
        raise EditError(f"{iso} is not a date")
    return {"iso": iso, "level": level}


def _check_faces(v: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(v, dict) or len(v) > MAX_FACES:
        raise EditError(f"faces must be an object with at most {MAX_FACES} entries")
    out = {}
    for key, e in v.items():
        if not isinstance(key, str) or not key or len(key) > 400 or not isinstance(e, dict):
            raise EditError("invalid face edit")
        f: Dict[str, Any] = {}
        if "name" in e:
            f["name"] = _check_text("A name", e["name"], False, MAX_KEYWORD).strip()
        if "box" in e:
            f["box"] = _check_box(e["box"], "A face")
        if e.get("deleted"):
            f["deleted"] = True
        was = e.get("was")
        if isinstance(was, dict):
            f["was"] = {"name": str(was.get("name") or ""), "box": _check_was_box(was.get("box"))}
        if key.startswith("new:"):
            if f.get("deleted"):
                continue
            if f.get("box") is None and not f.get("name"):
                continue
        out[key] = f
    return out


# --------------------------------------------------------------------------
# effective fields
# --------------------------------------------------------------------------

def fill_date(iso: str) -> Tuple[int, int, int]:
    """A partial date filled in for tags that need a whole date (DateTimeOriginal): the middle
    of what is known, as photokin does (a year: June 15; a month: the 15th)."""
    parts = [int(p) for p in iso.split("-")]
    y = parts[0]
    m = parts[1] if len(parts) > 1 else 6
    d = parts[2] if len(parts) > 2 else 15
    return y, m, d


def is_date_marker(kw: str) -> bool:
    return bool(DATE_MARKER.match(kw or ""))


def _faces_after(fields: Dict[str, Any], faces_edit: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict], List[Dict]]:
    base = [dict(f) for f in (fields.get("faces") or []) if isinstance(f, dict)] + \
           [dict(f) for f in (fields.get("faces_unnamed") or []) if isinstance(f, dict)]
    out = []
    for f in base:
        e = faces_edit.get(f.get("key") or "")
        if e is None:
            out.append(f)
            continue
        if e.get("deleted"):
            continue
        g = dict(f)
        if "name" in e:
            g["name"] = e["name"]
        if "box" in e:
            g["box"] = e["box"]
        out.append(g)
    for k, e in faces_edit.items():
        if k.startswith("new:"):
            out.append({"name": e.get("name", ""), "box": e.get("box"), "source": "edit", "ids": [], "key": k})
    # a face with neither a name nor a place on the photo (a PersonInImage name cleared) is nothing
    # any standard can hold: it is gone
    out = [f for f in out if (f.get("name") or "").strip() or f.get("box") is not None]
    named = [f for f in out if (f.get("name") or "").strip()]
    unnamed = [f for f in out if not (f.get("name") or "").strip()]
    return named, unnamed


def keywords_after(fields: Dict[str, Any], edits: Dict[str, Any]) -> List[str]:
    """The keyword list a file has once ``edits`` are written: the edited list (or the file's
    own, without date markers) plus the date marker the date edit calls for, or the file's own
    markers when the date is not edited."""
    cur = [k for k in (fields.get("keywords") or []) if isinstance(k, str)]
    kws = list(edits["keywords"]) if "keywords" in edits else [k for k in cur if not is_date_marker(k)]
    if "date" in edits:
        if edits["date"] is not None:
            kws.append(f"DATE: {LEVELS[edits['date']['level']]}")
    else:
        kws += [k for k in cur if is_date_marker(k)]
    return kws


def apply_to_fields(fields: Dict[str, Any], edits: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The caption fields as they will be once ``edits`` are written (the same values the reader
    will find in the saved file)."""
    if not edits:
        return fields
    f = dict(fields)
    for k in TEXT_FIELDS:
        if k in edits:
            f[k] = edits[k] or None
    if "keywords" in edits or "date" in edits:
        f["keywords"] = keywords_after(fields, edits)
    if "date" in edits:
        d = edits["date"]
        f["date"] = d["iso"] if d else None
        f["date_certainty"] = LEVELS[d["level"]] if d else None
    if edits.get("faces"):
        named, unnamed = _faces_after(fields, edits["faces"])
        f["faces"], f["faces_unnamed"], f["faces_unnamed_count"] = named, unnamed, len(unnamed)
    return f


# --------------------------------------------------------------------------
# tag updates
# --------------------------------------------------------------------------

def _exif_key(t: Dict[str, Any], tag: str) -> Optional[str]:
    """The key "EXIF:Tag" has in this file (IFD0:Tag, ExifIFD:Tag ...), or None."""
    name = tag.split(":", 1)[1]
    return next((f"{g}:{name}" for g in EXIF_GROUPS if f"{g}:{name}" in t), None)


def _present_keys(t: Dict[str, Any], tag: str) -> List[str]:
    """Keys of ``tag`` the file has: all its lang-alt variants for XMP, the IFD that holds it for EXIF."""
    if tag.startswith("EXIF:"):
        k = _exif_key(t, tag)
        if k == "IFD0:ImageDescription" and is_tifffile_shape_description(t.get(k)):
            return []   # tifffile's shape note, not a description
        return [k] if k else []
    if tag.startswith("XMP"):
        return [k for k in t if k == tag or k.startswith(tag + "-")]
    return [tag] if tag in t else []


def _has_group(t: Dict[str, Any], *prefixes: str) -> bool:
    return any(k.startswith(prefixes) for k in t)


class _Writer:
    """Collects JSON-import updates and ExifTool delete arguments."""

    def __init__(self, t: Dict[str, Any], info: ImageInfo, out_fmt: Optional[str] = None):
        self.t = t
        self.info = info
        self.fmt = out_fmt or info.format
        self.upd: Dict[str, Any] = {}
        self.dels: List[str] = []
        self.notes: List[str] = []
        self.iptc_utf8 = _iptc_is_utf8(t)
        # a PNG gets no EXIF block it doesn't have (a JPEG or TIFF always may)
        self.has_exif = self.fmt != "PNG" or _has_group(t, "ExifIFD:", "IFD0:")

    def exif_fits(self, key: str, value: str) -> bool:
        """A JPEG keeps its EXIF in one 64 KB segment: a long value in EXIF would split it (other
        apps then can't read it). Windows XP tags and a non-ASCII UserComment are stored as UTF-16."""
        if self.fmt != "JPEG":
            return True
        wide = key.split(":", 1)[1].startswith("XP") or (key.endswith("UserComment") and not value.isascii())
        return len(value.encode("utf-16-le" if wide else "utf-8")) <= EXIF_JPEG_MAX

    def set(self, key: str, value: Any) -> None:
        if key.startswith("IPTC:"):
            self._set_iptc(key, value)
            return
        if key.split(":", 1)[0] in EXIF_GROUPS and isinstance(value, str) and not self.exif_fits(key, value):
            if key in self.t:
                self.delete(key)
            self.notes.append(f"{key.split(':')[1]} is too long for the JPEG's EXIF; XMP keeps it")
            return
        self.upd[key] = value

    def delete(self, key: str) -> None:
        if key in self.upd:
            del self.upd[key]
        arg = f"-{key}="
        if arg not in self.dels:
            self.dels.append(arg)

    def _iptc_ok(self, s: str, key: str) -> bool:
        limit = IPTC_MAX.get(key)
        if self.iptc_utf8:
            b = s.encode("utf-8")
        else:
            try:
                b = s.encode("cp1252")
            except UnicodeEncodeError:
                return False
            if _fix_iptc_utf8(s) != s:
                return False   # the reader would take these bytes for UTF-8 and change them
        return limit is None or len(b) <= limit

    def _set_iptc(self, key: str, value: Any) -> None:
        if isinstance(value, list):
            vals = [v for v in value if self._iptc_ok(v, key)]
            if len(vals) < len(value):
                self.notes.append(f"{key.split(':')[1]}: {len(value) - len(vals)} value(s) too long for IPTC kept in XMP only")
            if vals:
                self.upd[key] = vals
            else:
                self.delete(key)
            return
        if self._iptc_ok(value, key):
            self.upd[key] = value
        else:
            self.delete(key)
            self.notes.append(f"{key.split(':')[1]} removed from IPTC (it can't hold this text); XMP keeps it")


def _creators(t: Dict[str, Any], value: str) -> List[str]:
    """Several photographers: a file that lists them separately keeps them separate. The reader
    shows them joined with ", ", or with "; " when a name holds a comma ("Smith, John"), and the
    value is split the same way."""
    if "; " in value:
        return [x.strip() for x in value.split("; ") if x.strip()]
    cur = t.get("XMP-dc:Creator", t.get("IPTC:By-line"))
    if isinstance(cur, list) and len(cur) > 1:
        return [x.strip() for x in value.split(", ") if x.strip()]
    return [value]


def _text_updates(w: _Writer, field: str, value: str, old: Optional[str]) -> None:
    primary, mirrors, fallbacks = SPEC[field]
    t = w.t
    if field == "notes" and (not w.has_exif or not w.exif_fits(primary, value or "")):
        if "ExifIFD:UserComment" in t and value:
            w.delete("ExifIFD:UserComment")   # too long for the JPEG's EXIF: XMP holds the notes
        primary, mirrors = "XMP-exif:UserComment", ()
    if value:
        many = _creators(t, value) if field == "creator" else [value]
        one = "; ".join(many)
        w.set(primary, many if primary in LIST_TAGS else value)
        for m in mirrors:
            for key in _present_keys(t, m):
                if key.startswith("XMP") and key != m:
                    continue   # other languages of a lang-alt mirror stay as they are
                w.set(key, many if m in LIST_TAGS else one if field == "creator" else value)
        # a fallback that held the very same text (Windows' copy of the caption) follows it
        for fb in fallbacks:
            for key in _present_keys(t, fb):
                if old and _same_text(t.get(key), old):
                    w.set(key, value)
        return
    for tag in (primary,) + mirrors + fallbacks:
        for key in _present_keys(t, tag):
            w.delete(key)


def _same_text(a: Any, b: Any) -> bool:
    def norm(v):
        if isinstance(v, dict):
            v = v.get("x-default") or next(iter(v.values()), "")
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        return " ".join(str(v or "").split())
    return norm(a) == norm(b)


def face_renames(fields: Dict[str, Any], faces_edit: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Old name -> new name (lower-case keys) of the faces these edits rename."""
    base = {f.get("key"): f for f in list(fields.get("faces") or []) + list(fields.get("faces_unnamed") or [])
            if isinstance(f, dict)}
    out = {}
    for k, e in faces_edit.items():
        f = base.get(k)
        old = ((f or {}).get("name") or "").strip()
        if f and "name" in e and not e.get("deleted") and old and e["name"] and e["name"].lower() != old.lower():
            out[old.lower()] = e["name"]
    return out


def _keyword_updates(w: _Writer, kws: List[str], fields: Dict[str, Any], renames: Optional[Dict[str, str]] = None) -> None:
    t = w.t
    w.set("XMP-dc:Subject", kws)
    if "IPTC:Keywords" in t:
        w.set("IPTC:Keywords", kws)
    xp = _exif_key(t, "EXIF:XPKeywords")
    if xp:
        # Windows splits its keyword list on ";": a keyword holding one would come apart there
        xs = [k for k in kws if ";" not in k]
        if xs:
            w.set(xp, ";".join(xs))
        else:
            w.delete(xp)
    # Lightroom's keyword hierarchy: a keyword removed here goes from its hierarchy too, or
    # Lightroom would put it back when it reads the file
    hs = t.get("XMP-lr:HierarchicalSubject")
    if hs not in (None, "", []):
        keep = {k.lower() for k in kws}
        old = {k.lower() for k in (fields.get("keywords") or []) if isinstance(k, str)}
        gone = old - keep
        items = hs if isinstance(hs, list) else [hs]
        new = []
        for h in items:
            path = str(h).split("|")
            leaf = path[-1].strip().lower()
            if leaf in gone and renames and leaf in renames and renames[leaf].lower() in keep:
                # a renamed person: same place in the hierarchy, new name
                new.append("|".join(path[:-1] + [renames[leaf]]))
            elif leaf not in gone:
                new.append(h)
        if new != items:
            if new:
                w.set("XMP-lr:HierarchicalSubject", new)
            else:
                w.delete("XMP-lr:HierarchicalSubject")


def _date_updates(w: _Writer, d: Optional[Dict[str, str]]) -> None:
    t = w.t
    if d is None:
        for k in DATE_CLEAR:
            if k in t:
                w.delete(k)
        return
    iso, level = d["iso"], d["level"]
    y, m, dd = fill_date(iso)
    whole = f"{y:04d}:{m:02d}:{dd:02d} 00:00:00"
    partial = iso.replace("-", ":")
    keep_time = False
    cur = None
    if level == "day":
        # the same day as the file says (a camera's own date): its time of day stays
        cur = next((t[k] for k in DATE_TAGS_ORIGINAL if t.get(k) not in (None, "")), None)
        pd = parse_date(str(cur)) if cur is not None else None
        keep_time = pd is not None and (pd.year, pd.month, pd.day) == (y, m, dd)
    if keep_time:
        # the camera's own date and time stay; the copies of the date in other standards that
        # disagree are brought to that same moment
        same = str(cur)
        if "XMP-exif:DateTimeOriginal" in t and not _same_day(t["XMP-exif:DateTimeOriginal"], (y, m, dd)):
            w.set("XMP-exif:DateTimeOriginal", same)
        if "IPTC:DateCreated" in t and not _same_day(t["IPTC:DateCreated"], (y, m, dd)):
            w.set("IPTC:DateCreated", f"{y:04d}:{m:02d}:{dd:02d}")
    else:
        if w.has_exif:
            w.set("ExifIFD:DateTimeOriginal", whole)
            for k in ("ExifIFD:OffsetTimeOriginal", "ExifIFD:SubSecTimeOriginal"):
                if k in t:
                    w.delete(k)
        if "XMP-exif:DateTimeOriginal" in t or not w.has_exif:
            w.set("XMP-exif:DateTimeOriginal", whole)
        if "IPTC:DateCreated" in t:
            w.set("IPTC:DateCreated", f"{y:04d}:{m:02d}:{dd:02d}")
        if "IPTC:TimeCreated" in t:
            w.delete("IPTC:TimeCreated")
    w.set("XMP-photoshop:DateCreated", partial)


def _same_day(v: Any, ymd: Tuple[int, int, int]) -> bool:
    d = parse_date(str(v)) if v not in (None, "") else None
    return d is not None and (d.year, d.month, d.day) == ymd


def tag_updates(md: Dict[str, Any], info: ImageInfo, fields: Dict[str, Any], edits: Dict[str, Any],
                out_fmt: Optional[str] = None) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """(JSON-import updates, ExifTool delete arguments, notes) that write ``edits`` into a file
    whose metadata is ``md`` and whose caption fields (as read) are ``fields``. ``out_fmt``: the
    format of the file written (a copy may be another format than the source)."""
    t = _text_view(md)
    w = _Writer(t, info, out_fmt)
    for k in TEXT_FIELDS:
        if k in edits:
            _text_updates(w, k, edits[k], fields.get(k))
    if "keywords" in edits or "date" in edits:
        _keyword_updates(w, keywords_after(fields, edits), fields, face_renames(fields, edits.get("faces") or {}))
    if "date" in edits:
        _date_updates(w, edits["date"])
    if "Photoshop:IPTCDigest" in t and any(k.startswith("IPTC:") for k in touched_tags(w.upd, w.dels)):
        w.upd["Photoshop:IPTCDigest"] = "new"   # IPTC and XMP agree again (MWG)
    return w.upd, w.dels, w.notes


def touched_tags(upd: Dict[str, Any], dels: Sequence[str]) -> set:
    """Tags an edit sets or deletes (metawrite's check that source XMP-dc tags reached the output
    must not count these as lost)."""
    return set(upd) | {d[1:].split("=", 1)[0] for d in dels}


# --------------------------------------------------------------------------
# face regions
# --------------------------------------------------------------------------

def unorient_box(box, orientation: int):
    """Upright normalized box -> the frame ``orientation`` describes (inverse of orient_box)."""
    o = int(orientation or 1)
    return orient_box(box, {6: 8, 8: 6}.get(o, o))


def _area(box, frame: int) -> Dict[str, Any]:
    x, y, w, h = unorient_box(box, frame)
    r = lambda v: round(float(v), 6)  # noqa: E731
    return {"X": r(x + w / 2), "Y": r(y + h / 2), "W": r(w), "H": r(h), "Unit": "normalized"}


def _mp_rect(box, frame: int) -> str:
    return ", ".join(f"{float(v):.6f}" for v in unorient_box(box, frame))


def region_updates(md: Dict[str, Any], info: ImageInfo, fields: Dict[str, Any],
                   faces_edit: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """New values for XMP-mwg-rs:RegionInfo, XMP-MP:RegionInfoMP and XMP-iptcExt:PersonInImage
    (only those that change) with ``faces_edit`` applied. Regions no edit refers to are kept
    exactly as they are. An edited face changes every region it was read from; an unnamed
    region that only duplicated a named face is removed when that face is edited."""
    t = _text_view(md)
    if not faces_edit:
        return {}
    mwg = copy.deepcopy(t.get("XMP-mwg-rs:RegionInfo")) if isinstance(t.get("XMP-mwg-rs:RegionInfo"), dict) else None
    mp = copy.deepcopy(t.get("XMP-MP:RegionInfoMP")) if isinstance(t.get("XMP-MP:RegionInfoMP"), dict) else None
    pii_raw = t.get("XMP-iptcExt:PersonInImage")
    pii = None if pii_raw in (None, "", []) else [str(x) for x in (pii_raw if isinstance(pii_raw, list) else [pii_raw])]
    mwg_list = list(mwg.get("RegionList") or []) if mwg else []
    mp_list = list(mp.get("Regions") or []) if mp else []
    base = {f.get("key"): f for f in list(fields.get("faces") or []) + list(fields.get("faces_unnamed") or [])
            if isinstance(f, dict) and f.get("key")}
    drop_mwg, drop_mp, drop_pii = set(), set(), set()
    changed = {"mwg": False, "mp": False, "pii": False}
    o = int(info.orientation or 1)
    pii_renames: List[Tuple[str, Optional[str]]] = []
    def own_name(rid: str) -> str:
        kind, _, idx = rid.partition(":")
        i = int(idx) if idx.isdigit() else -1
        if kind == "mwg" and 0 <= i < len(mwg_list) and isinstance(mwg_list[i], dict):
            return str(mwg_list[i].get("Name") or "").strip()
        if kind == "mp" and 0 <= i < len(mp_list) and isinstance(mp_list[i], dict):
            return str(mp_list[i].get("PersonDisplayName") or "").strip()
        return ""

    for key, e in faces_edit.items():
        if key.startswith("new:"):
            continue
        f = base.get(key)
        if f is None:
            continue   # the face is gone from the file (checked by the caller)
        was_name = (f.get("name") or "").strip()
        name = e["name"] if "name" in e else was_name
        box = e["box"] if "box" in e else f.get("box")
        ids = list(f.get("ids") or [])
        # standards that name this face in a region of their own: an unnamed region of the same
        # standard on the face is a duplicate (dropped once the face is edited); in another
        # standard it is that standard's only region for the face, and follows the edit
        named_in = {rid.partition(":")[0] for rid in ids if own_name(rid)}
        for rid in ids:
            kind, _, idx = rid.partition(":")
            i = int(idx) if idx.isdigit() else -1
            if kind == "mwg" and 0 <= i < len(mwg_list) and isinstance(mwg_list[i], dict):
                r = mwg_list[i] = dict(mwg_list[i])
                changed["mwg"] = True
                own = str(r.get("Name") or "").strip()
                if e.get("deleted") or (was_name and not own and "mwg" in named_in):
                    drop_mwg.add(i)   # removed, or an unnamed duplicate of this named face
                    continue
                if "name" in e:
                    if name:
                        r["Name"] = name
                    else:
                        r.pop("Name", None)
                if "box" in e and box is not None:
                    r["Area"] = {**(r.get("Area") if isinstance(r.get("Area"), dict) else {}),
                                 **_area(box, region_frame_orientation(t, info, r))}
            elif kind == "mp" and 0 <= i < len(mp_list) and isinstance(mp_list[i], dict):
                r = mp_list[i] = dict(mp_list[i])
                changed["mp"] = True
                own = str(r.get("PersonDisplayName") or "").strip()
                if e.get("deleted") or (was_name and not own and "mp" in named_in):
                    drop_mp.add(i)
                    continue
                if "name" in e:
                    if name:
                        r["PersonDisplayName"] = name
                    else:
                        r.pop("PersonDisplayName", None)
                if "box" in e and box is not None:
                    r["Rectangle"] = _mp_rect(box, o)
            elif kind == "pii" and pii is not None and 0 <= i < len(pii):
                changed["pii"] = True
                if e.get("deleted") or not name:
                    drop_pii.add(i)
                else:
                    pii[i] = name
                if box is not None and not e.get("deleted"):
                    # a name placed on the photo: it becomes a face region too
                    _new_region(t, info, mwg_list, mp_list if mp else None, name, box)
                    changed["mwg"] = True
                    if mp:
                        changed["mp"] = True
        if pii is not None and not key.startswith("pii") and ("name" in e or e.get("deleted")):
            pii_renames.append((was_name, None if e.get("deleted") else name))
    for key, e in faces_edit.items():
        if key.startswith("new:") and e.get("box") is not None:
            _new_region(t, info, mwg_list, mp_list if mp else None, e.get("name") or "", e["box"])
            changed["mwg"] = True
            if mp:
                changed["mp"] = True
            if pii is not None and e.get("name"):
                pii_renames.append(("", e["name"]))
    out: Dict[str, Any] = {}
    if changed["mwg"]:
        mwg = mwg or {}
        if "AppliedToDimensions" not in mwg:
            # no declared frame: the boxes are in the stored frame (MWG), and the reader takes
            # them so when there is no AppliedToDimensions
            mwg["AppliedToDimensions"] = {"W": info.width, "H": info.height, "Unit": "pixel"}
        mwg["RegionList"] = [r for i, r in enumerate(mwg_list) if i not in drop_mwg]
        out["XMP-mwg-rs:RegionInfo"] = mwg
    if changed["mp"] and mp is not None:
        mp["Regions"] = [r for i, r in enumerate(mp_list) if i not in drop_mp]
        out["XMP-MP:RegionInfoMP"] = mp
    if pii is not None and (changed["pii"] or pii_renames):
        names = [n for i, n in enumerate(pii) if i not in drop_pii]
        final = {(x or "").lower() for x in _final_names(base, faces_edit)}
        for old, new in pii_renames:
            if old and old.lower() not in final:
                names = [n for n in names if n.lower() != old.lower()]
            if new and new.lower() not in {n.lower() for n in names}:
                names.append(new)
        if names != pii:
            out["XMP-iptcExt:PersonInImage"] = names
    return out


def _final_names(base: Dict[str, Dict], faces_edit: Dict[str, Dict]) -> List[str]:
    out = []
    for k, f in base.items():
        e = faces_edit.get(k) or {}
        if e.get("deleted"):
            continue
        out.append(e["name"] if "name" in e else (f.get("name") or ""))
    out += [e.get("name") or "" for k, e in faces_edit.items() if k.startswith("new:")]
    return [n for n in out if n]


def _new_region(t: Dict[str, Any], info: ImageInfo, mwg_list: List, mp_list: Optional[List], name: str, box) -> None:
    """A new face region in the frame this file's regions use: Lightroom's (stored, with a
    Rotation) when Lightroom wrote the others, else the frame AppliedToDimensions declares
    (stored when there is none)."""
    lightroom = any(_lightroom_region(r) for r in mwg_list)
    r: Dict[str, Any] = {"Type": "Face"}
    if name:
        r["Name"] = name
    if lightroom:
        r["Rotation"] = 0
    frame = region_frame_orientation(t, info, r)
    r["Area"] = _area(box, frame)
    mwg_list.append(r)
    if mp_list is not None:
        m: Dict[str, Any] = {"Rectangle": _mp_rect(box, int(info.orientation or 1))}
        if name:
            m["PersonDisplayName"] = name
        mp_list.append(m)


def missing_faces(fields: Dict[str, Any], faces_edit: Dict[str, Dict[str, Any]]) -> List[str]:
    """Edited faces this version of the file no longer has (their keys): the key is gone, or (keys
    are region positions) it now names another face than the one the edit was made on."""
    from .metadata import _iou
    faces = {f.get("key"): f for f in list(fields.get("faces") or []) + list(fields.get("faces_unnamed") or [])
             if isinstance(f, dict)}
    out = []
    for k, e in faces_edit.items():
        if k.startswith("new:"):
            continue
        f = faces.get(k)
        was = e.get("was")
        if f is None:
            out.append(k)
        elif was and ((f.get("name") or "") != was.get("name", "") or
                      (f.get("box") is not None and was.get("box") is not None and _iou(f["box"], was["box"]) < 0.5)):
            out.append(k)
    return out


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def _norm_text(v) -> str:
    return " ".join(str(v or "").split())


def _rendered_date(fields: Dict[str, Any]):
    d = parse_date(fields.get("date"))
    if d is None:
        return _norm_text(fields.get("date")) or None
    d2, approx = apply_certainty(d, fields.get("date_certainty"))
    return (d2.iso() if d2 else None, approx)


def check_written(expected: Dict[str, Any], got: Dict[str, Any], edits: Dict[str, Any],
                  boxes: bool = True, dropped: Sequence[str] = ()) -> List[str]:
    """What of ``edits`` did not reach the file: ``expected`` = apply_to_fields(before, edits),
    ``got`` = the caption fields read back. ``boxes``: also compare face positions (in place;
    a saved copy moves them onto the new canvas). ``dropped``: names of faces the copy left out
    because they were cropped away."""
    bad = []
    for k in TEXT_FIELDS:
        if k in edits and _norm_text(expected.get(k)) != _norm_text(got.get(k)):
            bad.append(k)
    if "keywords" in edits or "date" in edits:
        if {x.lower() for x in expected.get("keywords") or []} != {x.lower() for x in got.get("keywords") or []}:
            bad.append("keywords")
    if "date" in edits and _rendered_date(expected) != _rendered_date(got):
        bad.append("date")
    if edits.get("faces"):
        from collections import Counter
        want = Counter(f["name"].lower() for f in expected.get("faces") or [])
        have = Counter((f.get("name") or "").lower() for f in got.get("faces") or [])
        if boxes:
            ok = want == have and len(expected.get("faces_unnamed") or []) == int(got.get("faces_unnamed_count") or 0)
        else:
            # a captioned copy: faces cropped away are left out (and say so); nothing else may be
            drop = {n.lower() for n in dropped}
            ok = not (have - want) and all(n in drop for n in want - have)
        if not ok:
            bad.append("faces")
        elif boxes:
            from .metadata import _iou
            for f in expected.get("faces") or []:
                if f.get("box") is None:
                    continue
                if not any(g.get("name") == f["name"] and g.get("box") is not None and _iou(g["box"], f["box"]) > 0.95
                           for g in got.get("faces") or []):
                    bad.append("faces")
                    break
    return bad


def previous_values(md: Dict[str, Any], tags) -> str:
    """The file's values of ``tags`` before an edit, as JSON (for the save log: every value an edit
    replaced or removed, region structures included, can be put back from it)."""
    import json
    t = _text_view(md)
    old = {k: t[k] for k in sorted(set(tags)) if k in t}
    return json.dumps(old, ensure_ascii=False, default=str)


def describe(fields: Dict[str, Any], edits: Dict[str, Any]) -> List[str]:
    """Old -> new values, for the save log (so a details edit can be undone by hand)."""
    out = []
    for k in TEXT_FIELDS:
        if k in edits:
            out.append(f"{k}: {fields.get(k)!r} -> {edits[k]!r}")
    if "keywords" in edits:
        out.append(f"keywords: {fields.get('keywords')!r} -> {edits['keywords']!r}")
    if "date" in edits:
        out.append(f"date: {fields.get('date')!r} ({fields.get('date_certainty')}) -> {edits['date']!r}")
    for k, e in (edits.get("faces") or {}).items():
        out.append(f"face {k}: {e.get('was') or ''} -> " + ("deleted" if e.get("deleted") else
                                                             repr({x: e[x] for x in ('name', 'box') if x in e})))
    return out


