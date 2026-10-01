"""App settings (settings.json) with defaults and deep-merge."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from . import paths
from .util import atomic_write_json

DEFAULTS: Dict[str, Any] = {
    "general": {
        "displayUnit": "%",          # % | px | pt | mm
        "theme": "system",           # system | light | dark
        "defaultTemplate": "classic-polaroid",
        "showTooltips": True,        # hover / keyboard-focus explanations on controls
    },
    "saving": {
        "location": "same",          # same | subfolder | fixed
        "subfolderName": "captioned",
        "fixedFolder": "",
        "fileName": "{stem}-captioned",
        "onExists": "increment",     # increment | overwrite | ask
        "outputFormat": "same",      # same | tiff | jpeg | png
        "jpegQuality": 95,
        "keepFileDates": False,
        "backupOriginals": True,
        "backupFolder": "",          # empty = "_originals" subfolder next to the file, as <stem>-original<ext>
        "embedMarker": True,
        "allowMultipageSave": False,
        "allowOverwriteHandwritten": False,
    },
    "advanced": {
        "cacheSizeMB": 2048,
        "exiftoolPath": "",
    },
    "batch": {
        "includeSubfolders": False,
        "saveMode": "copy",          # copy | overwrite
        "which": "all",              # all | selected | uncaptioned
        "caseA": "recaption",        # recaption | skip
        "caseB": "skip",             # rebuild | skip
        "caseC": "skip",             # skip | erase
        "useDrafts": True,
        "warnings": "hold",          # include | hold
    },
    "session": {
        "lastTemplate": "",
        "lastFolder": "",
        "window": {"w": 1440, "h": 900},
        "jpegWarned": False,
    },
}

log = logging.getLogger(__name__)

# allowed values of the choice settings
ENUMS: Dict[Tuple[str, str], Tuple[str, ...]] = {
    ("general", "displayUnit"): ("%", "px", "pt", "mm"),
    ("general", "theme"): ("system", "light", "dark"),
    ("saving", "location"): ("subfolder", "fixed", "same"),
    ("saving", "onExists"): ("increment", "overwrite", "ask"),
    ("saving", "outputFormat"): ("same", "tiff", "jpeg", "png"),
    ("batch", "saveMode"): ("copy", "overwrite"),
    ("batch", "which"): ("all", "selected", "uncaptioned"),
    ("batch", "caseA"): ("recaption", "skip"),
    ("batch", "caseB"): ("rebuild", "skip"),
    ("batch", "caseC"): ("skip", "erase"),
    ("batch", "warnings"): ("include", "hold"),
}
# inclusive ranges of the number settings
RANGES: Dict[Tuple[str, ...], Tuple[int, int]] = {
    ("saving", "jpegQuality"): (1, 100),
    ("advanced", "cacheSizeMB"): (64, 1024 * 1024),
    ("session", "window", "w"): (200, 100000),
    ("session", "window", "h"): (200, 100000),
}
MAX_TEXT = 4096


def _valid_subfolder(v: str) -> bool:
    from .security import is_plain_folder_name
    return is_plain_folder_name(v)


def _check_value(where: Tuple[str, ...], default: Any, v: Any) -> Tuple[bool, Any]:
    """(ok, value) for one leaf setting checked against the type of its default."""
    if isinstance(default, bool):
        return (isinstance(v, bool), v)
    if isinstance(default, int):
        if isinstance(v, bool):
            return (False, v)
        if isinstance(v, float) and v == v and abs(v) < 2 ** 31:
            v = int(round(v))   # a number typed with decimals
        if not isinstance(v, int):
            return (False, v)
        lo, hi = RANGES.get(where, (-(2 ** 31), 2 ** 31))
        return (lo <= v <= hi, v)
    if isinstance(default, str):
        if not isinstance(v, str) or len(v) > MAX_TEXT or "\x00" in v:
            return (False, v)
        if where[:2] in ENUMS and v not in ENUMS[where[:2]]:
            return (False, v)
        if where == ("saving", "subfolderName") and not _valid_subfolder(v):
            return (False, v)
        return (True, v)
    return (False, v)


def clean(data: Any, base: Optional[Dict[str, Any]] = None, where: Tuple[str, ...] = (),
          bad: Optional[List[str]] = None) -> Dict[str, Any]:
    """Only the known settings whose values have the right type (and allowed value): anything
    else is dropped and its dotted name appended to ``bad``."""
    base = DEFAULTS if base is None else base
    out: Dict[str, Any] = {}
    if not isinstance(data, dict):
        if bad is not None:
            bad.append(".".join(where) or "settings")
        return out
    for k, v in data.items():
        w = where + (str(k),)
        if k not in base:
            if bad is not None:
                bad.append(".".join(w))
            continue
        d = base[k]
        if isinstance(d, dict):
            if isinstance(v, dict):
                out[k] = clean(v, d, w, bad)
            elif bad is not None:
                bad.append(".".join(w))
            continue
        ok, val = _check_value(w, d, v)
        if ok:
            out[k] = val
        elif bad is not None:
            bad.append(".".join(w))
    return out


_lock = threading.Lock()


def _path() -> str:
    return os.path.join(paths.app_data(), "settings.json")


def _merge(base: Dict, over: Dict) -> Dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in out or not isinstance(base, dict):
            out[k] = v
        else:
            out[k] = v
    return out


def load_settings() -> Dict[str, Any]:
    try:
        with open(_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    bad: List[str] = []
    data = clean(data, bad=bad)
    if bad:
        log.warning("settings.json: ignored invalid values for %s", ", ".join(bad))
    return _merge(DEFAULTS, data)


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``patch`` (invalid or unknown values are dropped; see :func:`clean`)."""
    bad: List[str] = []
    patch = clean(patch, bad=bad)
    if bad:
        log.warning("settings: ignored invalid values for %s", ", ".join(bad))
    with _lock:
        cur = load_settings()
        new = _merge(cur, patch)
        atomic_write_json(_path(), new)
        return new


def reset_settings() -> Dict[str, Any]:
    with _lock:
        atomic_write_json(_path(), DEFAULTS)
        return copy.deepcopy(DEFAULTS)


# Legacy convenience used by exiftool.get()
def _flat_get(key: str):
    s = load_settings()
    return s.get("advanced", {}).get(key)
