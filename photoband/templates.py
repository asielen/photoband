"""Templates: built-in (read-only) and user templates stored as JSON files.

Units: in ``relative`` scale mode every length and font size is a percentage of
the photo width. In ``physical`` mode lengths are millimetres and font sizes
points, converted with the file's DPI.
"""
from __future__ import annotations

import copy
import os
import re
import time
import uuid
from typing import Dict, List, Optional

from . import paths
from .util import atomic_write_json, read_json

SCHEMA = 1


def _style(font="source-serif-4", size=2.0, weight=400, italic=False, color="#222222", align="left",
           line_height=1.25, letter=0.0, before=0.0, after=0.0, case="none") -> Dict:
    return {"font": font, "weight": weight, "italic": italic, "size": size, "color": color,
            "align": align, "lineHeight": line_height, "letterSpacing": letter,
            "spaceBefore": before, "spaceAfter": after, "case": case}


def _layout(top, right, bottom, left, color="#ffffff", band_mode="auto", band_min=8.0, overflow="warn",
            pad=(2.0, 4.0, 2.0, 4.0), columns=1, split=60.0, valign="middle", divider=False) -> Dict:
    return {
        "border": {"top": top, "right": right, "bottom": bottom, "left": left},
        "lockSides": left == right,
        "borderColor": color, "bandColor": color, "linkColors": True,
        "bandHeight": {"mode": band_mode, "min": band_min, "overflow": overflow},
        "padding": {"top": pad[0], "right": pad[1], "bottom": pad[2], "left": pad[3]},
        "textMaxWidth": 100.0,
        "vAlign": valign,
        "columns": {"count": columns, "split": split, "gutter": 4.0},
        "divider": {"enabled": divider, "width": 0.08, "color": "#9a9a9a", "inset": 0.0},
        "keyline": {"enabled": False, "width": 0.05, "color": "#000000"},
    }


def _block(bid, name, fmt, style, column=0) -> Dict:
    return {"id": bid, "name": name, "format": fmt, "column": column, "style": style}


# Multi-row group photos: one line per row (the spec's default row labels are kept).
ROWS_ON_LINES = r"{names:rows|row_sep=\n}"

BUILTINS: List[Dict] = [
    {
        "id": "classic-polaroid", "name": "Classic Polaroid", "builtin": True, "schema": SCHEMA,
        "scaleMode": "relative",
        "description": "5.7 / 7.6 / 28 white frame from the Polaroid 600; handwriting, centered.",
        "layout": _layout(7.6, 5.7, 28.0, 5.7, band_mode="fixed", overflow="shrink", pad=(2.5, 1.0, 2.5, 1.0)),
        "blocks": [
            _block("title", "Title", "{title}", _style("caveat", 7.0, 500, color="#23262e", align="center",
                                                          line_height=1.05, after=0.6)),
            _block("people", "People", "{names}", _style("caveat", 4.8, 400, color="#23262e", align="center",
                                                          line_height=1.1)),
        ],
    },
    {
        "id": "bottom-band", "name": "Bottom band", "builtin": True, "schema": SCHEMA, "scaleMode": "relative",
        "description": "White band under the photo; left-aligned serif.",
        "layout": _layout(0.0, 0.0, 6.0, 0.0, band_mode="auto", band_min=6.0, pad=(2.6, 3.2, 2.8, 3.2),
                          valign="top"),
        "blocks": [
            _block("title", "Title", "{title}", _style("source-serif-4", 3.0, 600, after=0.5)),
            _block("people", "People", ROWS_ON_LINES, _style("source-serif-4", 2.2, 400, after=0.5)),
            _block("notes", "Notes", "[{caption}][\n{date:mmmm d, yyyy}][, {location}]",
                   _style("source-serif-4", 2.0, 400, italic=True, color="#444444")),
        ],
    },
    {
        "id": "archive-label", "name": "Archive label", "builtin": True, "schema": SCHEMA, "scaleMode": "relative",
        "description": "Thin white frame, hairline divider, small serif with a catalog footer.",
        "layout": _layout(2.0, 2.0, 5.0, 2.0, band_mode="auto", band_min=5.0, pad=(1.8, 0.4, 1.6, 0.4),
                          valign="top", divider=True),
        "blocks": [
            _block("people", "People", ROWS_ON_LINES, _style("eb-garamond", 2.2, 400, after=0.4)),
            _block("notes", "Notes", "{caption}", _style("eb-garamond", 2.0, 400, italic=True, after=0.9,
                                                           color="#333333")),
            _block("footer", "Footer", "{stem}[ · {date:yyyy-mm-dd}]",
                   _style("eb-garamond", 1.7, 400, color="#666666", letter=6.0, case="smallcaps")),
        ],
    },
    {
        "id": "museum-card", "name": "Museum card", "builtin": True, "schema": SCHEMA, "scaleMode": "relative",
        "description": "Off-white mat with a two-column label: title and people, date and place.",
        "layout": {**_layout(4.0, 4.0, 10.0, 4.0, color="#F4F1EA", band_mode="auto", band_min=12.0,
                             pad=(3.2, 0.0, 3.4, 0.0), columns=2, split=64.0, valign="top")},
        "blocks": [
            _block("title", "Title", "{title}", _style("playfair-display", 3.4, 600, color="#1b1b1b", after=0.5), 0),
            _block("people", "People", "{names}", _style("source-serif-4", 2.3, 400, color="#2b2b2b"), 0),
            _block("date", "Date", "{date}", _style("source-serif-4", 2.3, 600, color="#2b2b2b", align="right",
                                                      after=0.3), 1),
            _block("place", "Place", "{location}", _style("source-serif-4", 2.1, 400, italic=True,
                                                            color="#4a4a4a", align="right"), 1),
        ],
    },
    {
        "id": "dark-band", "name": "Dark band", "builtin": True, "schema": SCHEMA, "scaleMode": "relative",
        "description": "Near-black band with white text for screens and TV slideshows.",
        "layout": _layout(0.0, 0.0, 7.0, 0.0, color="#111111", band_mode="auto", band_min=9.0,
                          pad=(2.2, 3.6, 2.4, 3.6), valign="middle"),
        "blocks": [
            _block("title", "Title", "{title}", _style("inter", 3.2, 600, color="#ffffff", after=0.6)),
            _block("people", "People", "{names}", _style("inter", 2.4, 400, color="#d6d6d6")),
        ],
    },
]
BUILTIN_IDS = {t["id"] for t in BUILTINS}


def _dir() -> str:
    return paths.sub("templates")


def _safe_id(tid: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", tid or ""):
        raise ValueError("Invalid template id")
    return tid


def list_templates() -> List[Dict]:
    out = [copy.deepcopy(t) for t in BUILTINS]
    user = []
    for fn in sorted(os.listdir(_dir())):
        if fn.endswith(".json"):
            t = read_json(os.path.join(_dir(), fn))
            if isinstance(t, dict) and t.get("id") and t.get("layout") and t.get("blocks") is not None:
                t["builtin"] = False
                user.append(t)
    user.sort(key=lambda t: t.get("name", "").lower())
    return out + user


def get_template(tid: str) -> Optional[Dict]:
    for t in list_templates():
        if t["id"] == tid:
            return t
    return None


def _unique_name(name: str) -> str:
    names = {t["name"].lower() for t in list_templates()}
    if name.lower() not in names:
        return name
    i = 2
    while f"{name} {i}".lower() in names:
        i += 1
    return f"{name} {i}"


def save_template(t: Dict, new: bool = False) -> Dict:
    validate_template(t)
    if t.get("id") is not None and not isinstance(t.get("id"), str):
        raise ValueError("Invalid template id")
    t = copy.deepcopy(t)
    if new or not t.get("id") or t["id"] in BUILTIN_IDS:
        t["id"] = "u-" + uuid.uuid4().hex[:12]
        t["name"] = _unique_name(t.get("name") or "Untitled template")
    _safe_id(t["id"])
    t["builtin"] = False
    t["schema"] = SCHEMA
    t["updated"] = int(time.time())
    validate_template(t)
    atomic_write_json(os.path.join(_dir(), t["id"] + ".json"), t)
    return t


def delete_template(tid: str) -> None:
    if tid in BUILTIN_IDS:
        raise ValueError("Built-in templates cannot be deleted")
    p = os.path.join(_dir(), _safe_id(tid) + ".json")
    if os.path.exists(p):
        os.unlink(p)


def import_template(data: Dict) -> Dict:
    if not isinstance(data, dict) or "layout" not in data or "blocks" not in data:
        raise ValueError("That file is not a Photoband template")
    data = copy.deepcopy(data)
    data.pop("id", None)
    validate_template(data)
    return save_template(data, new=True)


def export_template(tid: str) -> Dict:
    t = get_template(tid)
    if not t:
        raise KeyError(tid)
    t = copy.deepcopy(t)
    t.pop("builtin", None)
    return t


_COLOR = re.compile(r"#[0-9A-Fa-f]{6}")
_ENUMS = {
    "scaleMode": ("relative", "physical"),
    "align": ("left", "center", "right", "justify"),
    "case": ("none", "upper", "lower", "smallcaps"),
    "vAlign": ("top", "middle", "bottom"),
    "bandMode": ("auto", "fixed"),
    "overflow": ("warn", "shrink"),
}
MAX_BLOCKS = 50
MAX_FORMAT = 2000


def _num(v, where: str, lo: float = -10000.0, hi: float = 10000.0) -> None:
    import math
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not (lo <= v <= hi):
        raise ValueError(f"Template: {where} must be a number between {lo:g} and {hi:g}")


def _str(v, where: str, maxlen: int = 200, allow_empty: bool = True) -> None:
    if not isinstance(v, str) or len(v) > maxlen or (not allow_empty and not v):
        raise ValueError(f"Template: {where} must be text of at most {maxlen} characters")


def _color(v, where: str) -> None:
    if not isinstance(v, str) or not _COLOR.fullmatch(v):
        raise ValueError(f"Template: {where} must be a color like #rrggbb")


def _bool(v, where: str) -> None:
    if not isinstance(v, bool):
        raise ValueError(f"Template: {where} must be true or false")


def _enum(v, key: str, where: str) -> None:
    if v not in _ENUMS[key]:
        raise ValueError(f"Template: {where} must be one of {', '.join(_ENUMS[key])}")


def _dict(v, where: str) -> Dict:
    if not isinstance(v, dict):
        raise ValueError(f"Template: {where} must be an object")
    return v


def _validate_style(st: Dict, where: str) -> None:
    _dict(st, where)
    checks = {
        "font": lambda v, w: _str(v, w, 200),
        "weight": lambda v, w: _num(v, w, 1, 1000),
        "italic": _bool,
        "size": lambda v, w: _num(v, w, 0, 1000),
        "color": _color,
        "align": lambda v, w: _enum(v, "align", w),
        "lineHeight": lambda v, w: _num(v, w, 0, 20),
        "letterSpacing": lambda v, w: _num(v, w, -100, 1000),
        "spaceBefore": lambda v, w: _num(v, w, -1000, 1000),
        "spaceAfter": lambda v, w: _num(v, w, -1000, 1000),
        "case": lambda v, w: _enum(v, "case", w),
    }
    for k, fn in checks.items():
        if k in st:
            fn(st[k], f"{where}.{k}")


def _validate_layout(lay: Dict) -> None:
    _dict(lay, "layout")
    for k in ("border", "padding", "bandHeight", "columns"):
        if k not in lay:
            raise ValueError(f"Template layout is missing '{k}'")
    for k in ("border", "padding"):
        box = _dict(lay[k], f"layout.{k}")
        for side in ("top", "right", "bottom", "left"):
            if side in box:
                _num(box[side], f"layout.{k}.{side}", 0, 10000)
    bh = _dict(lay["bandHeight"], "layout.bandHeight")
    if "mode" in bh:
        _enum(bh["mode"], "bandMode", "layout.bandHeight.mode")
    if "min" in bh:
        _num(bh["min"], "layout.bandHeight.min", 0, 10000)
    if "overflow" in bh:
        _enum(bh["overflow"], "overflow", "layout.bandHeight.overflow")
    cols = _dict(lay["columns"], "layout.columns")
    if "count" in cols and cols["count"] not in (1, 2):
        raise ValueError("Template: layout.columns.count must be 1 or 2")
    if "split" in cols:
        _num(cols["split"], "layout.columns.split", 0, 100)
    if "gutter" in cols:
        _num(cols["gutter"], "layout.columns.gutter", 0, 1000)
    for k in ("borderColor", "bandColor"):
        if k in lay:
            _color(lay[k], f"layout.{k}")
    for k in ("lockSides", "linkColors"):
        if k in lay:
            _bool(lay[k], f"layout.{k}")
    if "textMaxWidth" in lay:
        _num(lay["textMaxWidth"], "layout.textMaxWidth", 0, 100)
    if "vAlign" in lay:
        _enum(lay["vAlign"], "vAlign", "layout.vAlign")
    for k in ("divider", "keyline"):
        if k in lay:
            d = _dict(lay[k], f"layout.{k}")
            if "enabled" in d:
                _bool(d["enabled"], f"layout.{k}.enabled")
            if "width" in d:
                _num(d["width"], f"layout.{k}.width", 0, 1000)
            if "color" in d:
                _color(d["color"], f"layout.{k}.color")
            if "inset" in d:
                _num(d["inset"], f"layout.{k}.inset", 0, 1000)


def validate_template(t: Dict) -> None:
    """Types and ranges of everything the UI renders, so a shared template can't break it."""
    _dict(t, "template")
    if "name" in t:
        _str(t["name"], "name", 200)
    if "description" in t:
        _str(t["description"], "description", MAX_FORMAT)
    if "scaleMode" in t:
        _enum(t["scaleMode"], "scaleMode", "scaleMode")
    _validate_layout(t.get("layout"))
    if not isinstance(t.get("blocks"), list):
        raise ValueError("Template has no blocks list")
    if len(t["blocks"]) > MAX_BLOCKS:
        raise ValueError(f"Template has more than {MAX_BLOCKS} blocks")
    ids = set()
    for i, b in enumerate(t["blocks"]):
        _dict(b, f"blocks[{i}]")
        bid = b.get("id")
        if not isinstance(bid, str) or not bid or len(bid) > 100 or bid in ids:
            raise ValueError("Each block needs a unique id")
        ids.add(bid)
        if "style" not in b or "format" not in b:
            raise ValueError(f"Block {bid} needs a format and a style")
        _str(b["format"], f"block {bid} format", MAX_FORMAT)
        if "name" in b:
            _str(b["name"], f"block {bid} name", 200)
        if "column" in b and b["column"] not in (0, 1):
            raise ValueError(f"Template: block {bid} column must be 0 or 1")
        _validate_style(b["style"], f"block {bid} style")
