"""Preview proxies (8-bit sRGB WebP, 2,560 px long edge) and thumbnails, cached on disk."""
from __future__ import annotations

import hashlib
import io
import os
import threading
from typing import Dict, Optional, Tuple

import numpy as np
from PIL import Image

from . import decodegate, paths
from .colors import to_display_srgb8
from .imageio import ImageInfo, load_upright, probe
from .util import atomic_write_bytes

PROXY_LONG_EDGE = 2560
THUMB_LONG_EDGE = 320
# bump when the display conversion changes so cached proxies are rebuilt
# (2: gray ICC profiles are color-managed, sRGB is detected by profile content)
PROXY_VERSION = 2

_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def cache_key(path: str, size: int, mtime_ns: int, file_id: int = 0) -> str:
    """Key of a photo's cached proxy/analysis: shared by every Photoband process, so it names the
    file's version: size, modified time and file id (a replaced file with the same size and
    modified time is a new file id)."""
    fid = f"|{file_id}" if file_id else ""
    return hashlib.sha1(f"{os.path.abspath(path)}|{size}|{mtime_ns}{fid}|v{PROXY_VERSION}".encode("utf-8")).hexdigest()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(key)
        if lk is None:
            lk = _locks[key] = threading.Lock()
        return lk


def _resize_long_edge(arr: np.ndarray, long_edge: int) -> np.ndarray:
    import cv2
    h, w = arr.shape[:2]
    s = long_edge / max(h, w)
    if s >= 1:
        return arr
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    squeeze = arr.ndim == 3 and arr.shape[2] == 1
    out = cv2.resize(arr[:, :, 0] if squeeze else arr, (nw, nh), interpolation=cv2.INTER_AREA)
    if out.ndim == 2:
        out = out[:, :, None]
    return out


def make_display(arr: np.ndarray, info: ImageInfo, long_edge: int) -> np.ndarray:
    small = _resize_long_edge(arr, long_edge)
    return to_display_srgb8(small, info.icc, info.mode)


def _paths(key: str) -> Tuple[str, str]:
    d = paths.sub("cache")
    return os.path.join(d, key + ".webp"), os.path.join(d, key + ".thumb.webp")


def _cached(ppath: str, tpath: str) -> Optional[Tuple[str, str, Tuple[int, int]]]:
    if os.path.exists(ppath) and os.path.exists(tpath):
        try:
            os.utime(ppath, None)  # LRU touch
            with Image.open(ppath) as im:
                return ppath, tpath, im.size
        except Exception:
            pass
    return None


def cached_proxy(path: str, info: ImageInfo) -> Optional[Tuple[str, str, Tuple[int, int]]]:
    """The cached (proxy_path, thumb_path, size), or None when it has to be built."""
    return _cached(*_paths(cache_key(path, info.size_bytes, info.mtime_ns, info.file_id)))


def get_proxy(path: str, info: Optional[ImageInfo] = None, arr: Optional[np.ndarray] = None,
              prio: int = decodegate.CURRENT) -> Tuple[str, str, Tuple[int, int]]:
    """Return (proxy_path, thumb_path, (proxy_w, proxy_h)), building them if needed.
    A full decode (when ``arr`` is not given) waits for a decode slot at ``prio``."""
    info = info or probe(path)
    key = cache_key(path, info.size_bytes, info.mtime_ns, info.file_id)
    ppath, tpath = _paths(key)
    with _lock_for(key):
        hit = _cached(ppath, tpath)
        if hit:
            return hit
        if arr is None:
            with decodegate.slot(info, prio):
                arr, info = load_upright(path, info)
        disp = make_display(arr, info, PROXY_LONG_EDGE)
        im = Image.fromarray(disp, "RGB")
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=90, method=4)
        atomic_write_bytes(ppath, buf.getvalue())
        th = im.copy()
        th.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE), Image.LANCZOS)
        buf = io.BytesIO()
        th.save(buf, "WEBP", quality=80)
        atomic_write_bytes(tpath, buf.getvalue())
        size = im.size
    evict()
    return ppath, tpath, size


def evict(max_mb: Optional[int] = None) -> None:
    if max_mb is None:
        from .settings import load_settings
        max_mb = int(load_settings()["advanced"].get("cacheSizeMB", 2048))
    d = paths.sub("cache")
    entries = []
    total = 0
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        entries.append((st.st_mtime, st.st_size, p))
        total += st.st_size
    limit = max_mb * 1024 * 1024
    if total <= limit:
        return
    entries.sort()
    for _, size, p in entries:
        try:
            os.unlink(p)
            total -= size
        except OSError:
            pass
        if total <= limit * 0.9:
            break
