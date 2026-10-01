"""Font registry: bundled Google Fonts, user-added files, downloaded Google Fonts and
installed system fonts. Every font file is served to the UI so preview and output
use the same file."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import threading
import urllib.request
from typing import Dict, List, Optional

from . import paths
from .util import atomic_write_json, read_json

FALLBACK_IDS = ["noto-serif", "noto-sans"]
DEFAULT_FAMILY = "source-serif-4"
_lock = threading.Lock()
_index: Optional[Dict] = None
FONT_EXT = (".ttf", ".otf")


def _fid(path: str) -> str:
    return hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _ranges(codepoints) -> List[List[int]]:
    cps = sorted(codepoints)
    out: List[List[int]] = []
    for c in cps:
        if out and c == out[-1][1] + 1:
            out[-1][1] = c
        else:
            out.append([c, c])
    return out


def inspect_font(path: str) -> Optional[Dict]:
    try:
        from fontTools.ttLib import TTFont
        f = TTFont(path, lazy=True, fontNumber=0)
    except Exception:
        return None
    try:
        name = f["name"]
        fam = (name.getDebugName(16) or name.getDebugName(1) or os.path.basename(path)).strip()
        sub = (name.getDebugName(17) or name.getDebugName(2) or "Regular").strip()
        os2 = f["OS/2"] if "OS/2" in f else None
        weight = int(os2.usWeightClass) if os2 else 400
        italic = bool(os2 and (os2.fsSelection & 1)) or "italic" in sub.lower() or "oblique" in sub.lower()
        wrange = None
        if "fvar" in f:
            for ax in f["fvar"].axes:
                if ax.axisTag == "wght":
                    wrange = [int(ax.minValue), int(ax.maxValue)]
        cmap = f.getBestCmap() or {}
        smcp = False
        if "GSUB" in f and f["GSUB"].table.FeatureList:
            smcp = any(fr.FeatureTag == "smcp" for fr in f["GSUB"].table.FeatureList.FeatureRecord)
        return {"family": fam, "subfamily": sub, "weight": weight, "italic": italic,
                "weightRange": wrange, "coverage": _ranges(cmap.keys()), "smallCaps": smcp}
    except Exception:
        return None
    finally:
        try:
            f.close()
        except Exception:
            pass


def _system_dirs() -> List[str]:
    if sys.platform == "win32":
        dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
        la = os.environ.get("LOCALAPPDATA")
        if la:
            dirs.append(os.path.join(la, "Microsoft", "Windows", "Fonts"))
        return dirs
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts")]


def _scan(dirs: List[str], source: str, cache: Dict, maxfiles: int = 4000) -> List[Dict]:
    out = []
    n = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith(FONT_EXT):
                    continue
                p = os.path.join(root, fn)
                n += 1
                if n > maxfiles:
                    return out
                try:
                    mt = os.stat(p).st_mtime_ns
                except OSError:
                    continue
                key = f"{p}|{mt}"
                info = cache.get(key)
                if info is None:
                    info = inspect_font(p)
                    cache[key] = info or {}
                if info:
                    out.append({**info, "path": p, "source": source})
    return out


def _group(files: List[Dict], manifest: Dict[str, Dict]) -> List[Dict]:
    fams: Dict[str, Dict] = {}
    for fi in files:
        src = fi["source"]
        if src == "bundled":
            fid = fi["bundleId"]
        else:
            fid = f"{src}-{_slug(fi['family'])}"
        fam = fams.get(fid)
        if fam is None:
            m = manifest.get(fid, {})
            fam = fams[fid] = {
                "id": fid, "family": fi["family"], "source": src, "role": m.get("role", ""),
                "license": m.get("license", ""), "fallback": bool(m.get("fallback")),
                "faces": [], "coverage": [], "smallCaps": False,
            }
        fam["faces"].append({"file": _fid(fi["path"]), "weight": fi["weight"], "weightRange": fi["weightRange"],
                             "italic": fi["italic"], "subfamily": fi["subfamily"],
                             "smallCaps": bool(fi.get("smallCaps"))})
        fam["coverage"] = _merge_ranges(fam["coverage"], fi["coverage"])
    for fam in fams.values():
        fam["smallCaps"] = _family_small_caps(fam["faces"])
    return sorted(fams.values(), key=lambda f: (f["source"] != "bundled", f["fallback"], f["family"].lower()))


def _face_distance(face: Dict, weight: int = 400) -> int:
    lo, hi = face.get("weightRange") or (face["weight"], face["weight"])
    return lo - weight if weight < lo else weight - hi if weight > hi else 0


def _family_small_caps(faces: List[Dict]) -> bool:
    """Family-level flag, kept for compatibility: true only if the upright regular face
    (the upright face nearest weight 400) has real small caps. Per-face flags live on
    each face; the UI uses the flag of the face that actually draws the text."""
    upright = [f for f in faces if not f["italic"]]
    if not upright:
        return False
    return bool(min(upright, key=_face_distance).get("smallCaps"))


def _merge_ranges(a, b):
    allr = sorted([list(x) for x in a] + [list(x) for x in b])
    out = []
    for r in allr:
        if out and r[0] <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], r[1])
        else:
            out.append(r)
    return out


def build_index(include_system: bool = True) -> Dict:
    global _index
    with _lock:
        cache_path = os.path.join(paths.app_data(), "font_cache.json")
        cache = read_json(cache_path, {}) or {}
        files: List[Dict] = []
        manifest = {}
        mpath = os.path.join(paths.fonts_dir(), "manifest.json")
        for m in read_json(mpath, []) or []:
            manifest[m["id"]] = m
            d = os.path.join(paths.fonts_dir(), m["id"])
            for fn in m["files"]:
                p = os.path.join(d, fn)
                key = f"{p}|{os.stat(p).st_mtime_ns}" if os.path.exists(p) else None
                if not key:
                    continue
                info = cache.get(key) or inspect_font(p)
                cache[key] = info or {}
                if info:
                    files.append({**info, "path": p, "source": "bundled", "bundleId": m["id"]})
        files += _scan([paths.sub("fonts", "user")], "user", cache)
        files += _scan([paths.sub("fonts", "google")], "google", cache)
        if include_system and not os.environ.get("PHOTOBAND_NO_SYSTEM_FONTS"):
            files += _scan(_system_dirs(), "system", cache)
        atomic_write_json(cache_path, cache)
        families = _group(files, manifest)
        by_file = {_fid(f["path"]): f["path"] for f in files}
        _index = {"families": families, "files": by_file}
        return _index


def index() -> Dict:
    return _index or build_index()


def file_path(file_id: str) -> Optional[str]:
    return index()["files"].get(file_id)


def add_font_file(src: str) -> Dict:
    if not src.lower().endswith(FONT_EXT):
        raise ValueError("Only .ttf and .otf files can be added")
    info = inspect_font(src)
    if not info:
        raise ValueError("That file is not a readable font")
    dst_dir = paths.sub("fonts", "user")
    dst = os.path.join(dst_dir, os.path.basename(src))
    shutil.copy2(src, dst)
    build_index()
    return info


# -- Google Fonts browsing (the only online feature; used only when opened) ----------

GF_META = "https://fonts.google.com/metadata/fonts"
GF_RAW = "https://raw.githubusercontent.com/google/fonts/main"
GF_API = "https://api.github.com/repos/google/fonts/contents"


def google_catalog() -> List[Dict]:
    cache = os.path.join(paths.sub("fonts"), "google_catalog.json")
    data = read_json(cache)
    if data and isinstance(data, list):
        return _clean_catalog(data)
    fams: List[Dict] = []
    try:
        raw = urllib.request.urlopen(GF_META, timeout=20).read().decode("utf-8")
        if raw.startswith(")]}'"):
            raw = raw[4:]
        meta = json.loads(raw)
        for f in meta.get("familyMetadataList", []):
            fams.append({"family": f["family"], "category": f.get("category", ""),
                         "subsets": f.get("subsets", [])})
    except Exception:
        for lic in ("ofl", "apache", "ufl"):
            try:
                req = urllib.request.Request(f"{GF_API}/{lic}", headers={"User-Agent": "Photoband"})
                lst = json.loads(urllib.request.urlopen(req, timeout=20).read())
                fams += [{"family": e["name"], "dir": f"{lic}/{e['name']}", "category": "", "subsets": []}
                         for e in lst if e.get("type") == "dir"]
            except Exception:
                continue
    fams = _clean_catalog(fams)
    if not fams:
        raise RuntimeError("Could not reach Google Fonts. Check your internet connection.")
    atomic_write_json(cache, fams)
    return fams


_GF_FAMILY_RE = re.compile(r"[A-Za-z0-9 ]{1,100}")


def _clean_catalog(fams) -> List[Dict]:
    """Catalog entries whose family names are plain (letters, digits, spaces): the UI puts them
    into CSS and URLs."""
    out = []
    for f in fams:
        if isinstance(f, dict) and isinstance(f.get("family"), str) and _GF_FAMILY_RE.fullmatch(f["family"]):
            out.append({"family": f["family"],
                        "category": f.get("category") if isinstance(f.get("category"), str) else "",
                        "subsets": [x for x in (f.get("subsets") or []) if isinstance(x, str)][:50]})
    return out


GF_FILE_RE = re.compile(r"[A-Za-z0-9._\[\],-]+\.ttf")
GF_MAX_BYTES = 20 * 1024 * 1024
GF_MAX_FILES = 64


def _gf_family_dir(family: str) -> str:
    gdir = re.sub(r"[^a-z0-9]", "", (family or "").lower())[:100]
    if not gdir:
        raise ValueError("Invalid font family name")
    return gdir


def _gf_filenames(meta: str) -> List[str]:
    """Font file names from METADATA.pb, kept only when they are plain basenames."""
    out: List[str] = []
    for fn in re.findall(r'filename:\s*"([^"]+)"', meta):
        if (GF_FILE_RE.fullmatch(fn) and os.path.basename(fn) == fn and not fn.startswith(".")
                and fn not in out):
            out.append(fn)
    return out[:GF_MAX_FILES]


def _read_capped(resp, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("The font file is too large")
    return data


def google_download(family: str) -> Dict:
    if not isinstance(family, str):
        raise ValueError("Invalid font family name")
    gdir = _gf_family_dir(family)
    meta = None
    base = None
    for lic in ("ofl", "apache", "ufl"):
        try:
            meta = _read_capped(urllib.request.urlopen(f"{GF_RAW}/{lic}/{gdir}/METADATA.pb", timeout=20),
                                1024 * 1024).decode("utf-8", "replace")
            base = f"{GF_RAW}/{lic}/{gdir}"
            break
        except Exception:
            continue
    if not meta:
        raise RuntimeError(f"{family} was not found in the Google Fonts repository")
    files = _gf_filenames(meta)
    if not files:
        raise RuntimeError(f"{family} has no downloadable font files")
    root = os.path.realpath(paths.sub("fonts", "google"))
    d = os.path.join(root, gdir)
    if os.path.dirname(os.path.realpath(d)) != root:
        raise ValueError("Invalid font family name")
    os.makedirs(d, exist_ok=True)
    got = []
    for fn in files:
        p = os.path.join(d, fn)
        if os.path.dirname(os.path.realpath(p)) != os.path.realpath(d):
            continue
        if not os.path.exists(p):
            data = _read_capped(urllib.request.urlopen(f"{base}/{urllib.request.quote(fn)}", timeout=60),
                                GF_MAX_BYTES)
            tmp = p + ".download"
            with open(tmp, "wb") as fh:
                fh.write(data)
            if not inspect_font(tmp):
                os.unlink(tmp)
                continue
            os.replace(tmp, p)
        got.append(fn)
    if not got:
        raise RuntimeError(f"{family} could not be downloaded as a valid font")
    build_index()
    return {"family": family, "files": got}
