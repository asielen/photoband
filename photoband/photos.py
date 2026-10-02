"""Per-photo service: probe, metadata, proxies, existing-text analysis, with an
in-memory cache keyed by path + size + mtime."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

import numpy as np
from collections import OrderedDict, deque
from typing import Any, Dict, List, Optional

from . import decodegate, drafts, paths, proxy
from .util import atomic_write_bytes, quick_hash
from .exiftool import get as get_exiftool
from .imageio import SUPPORTED_EXT, ImageInfo, load_upright, probe
from .metadata import normalize, raw_listing
from .record import from_metadata

log = logging.getLogger(__name__)

_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_lock = threading.Lock()
CACHE_MAX = 64


def _key(path: str, info: ImageInfo) -> str:
    # the file id too (as probe's own cache): a file replaced by another process (a save in
    # another window, a batch worker) with the same size and modified time is another file
    return f"{os.path.abspath(path)}|{info.size_bytes}|{info.mtime_ns}|{info.file_id}"


def list_photos(paths_or_folder: List[str], include_sub: bool = False) -> List[Dict[str, Any]]:
    from .batch import scan_folder
    out = []
    seen = set()
    for p in paths_or_folder:
        if os.path.isdir(p):
            for e in scan_folder(p, include_sub):
                if e["path"] not in seen:
                    seen.add(e["path"])
                    out.append({"path": e["path"], "name": e["name"]})
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_EXT:
            if p not in seen:
                seen.add(p)
                out.append({"path": p, "name": os.path.basename(p)})
    out.sort(key=lambda e: e["name"].lower())
    for e in out:
        e["draft"] = drafts.load_draft(e["path"]) is not None
    return out


def meta(path: str) -> Dict[str, Any]:
    info = probe(path)
    k = _key(path, info)
    with _lock:
        hit = _cache.get(k)
        if hit and "meta" in hit:
            _cache.move_to_end(k)
            return hit["meta"]
    md = get_exiftool().read_json(path)
    norm = normalize(md, info)
    rec = from_metadata(md)
    res = {
        "path": os.path.abspath(path),
        "name": os.path.basename(path),
        "info": info.to_json(),
        "fields": norm["fields"],
        "fieldSources": norm["sources"],
        "faces": norm["faces"],
        "warnings": norm["warnings"],
        "raw": raw_listing(md),
        "hasRecord": rec is not None,
        # size, mtime (string: JS numbers lose ns precision), quick content hash for change detection
        "stat": [info.size_bytes, str(info.mtime_ns), quick_hash(path)],
    }
    with _lock:
        _cache[k] = {"meta": res, "md": md, "info": info}
        _cache.move_to_end(k)
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
    # the whole-file hash a save compares against if the modified time changes meanwhile
    from .save import hash_in_background
    hash_in_background(path)
    return res


_current = {"path": None}


def set_current(path: str) -> None:
    """The photo the editor shows (its decodes are shared and go first)."""
    _current["path"] = os.path.abspath(path)


def raw_md(path: str) -> Dict[str, Any]:
    info = probe(path)
    k = _key(path, info)
    with _lock:
        hit = _cache.get(k)
        if hit and "md" in hit:
            return hit["md"]
    meta(path)
    with _lock:
        return _cache[k]["md"]


def proxy_paths(path: str, thumb: bool = False, prio: Optional[int] = None):
    """(proxy_path, thumb_path, size). A cache miss for the current photo (the default)
    decodes through :func:`full_array`, so the existing-text analysis and crops reuse that
    decode; thumbnails and prefetch decode on their own at a lower priority."""
    info = probe(path)
    hit = proxy.cached_proxy(path, info)
    if hit:
        return hit
    if prio is None:
        # the filmstrip thumbnail of the photo on screen is as urgent as its proxy (and
        # its decode is then shared with the analysis)
        prio = decodegate.THUMB if thumb and os.path.abspath(path) != _current["path"] else decodegate.CURRENT
    if prio == decodegate.CURRENT:
        arr, info = full_array(path, info=info)   # builds the proxy from the same decode
        return proxy.get_proxy(path, info, arr)
    arr, _ = _peek_full(path, info)
    return proxy.get_proxy(path, info, arr, prio=prio)


PREFETCH_MAX = 8        # queued prefetch requests; older ones are dropped first
PREFETCH_WORKERS = 2
_pf_queue: "deque[str]" = deque()
_pf_cv = threading.Condition()
_pf_threads: List[threading.Thread] = []


def prefetch(paths: List[str]) -> None:
    """Queue proxies for neighbours of the photo on screen. The queue is bounded: when
    the user flips through photos quickly, the oldest requests (photos already left
    behind) are dropped instead of piling up."""
    with _pf_cv:
        for p in paths:
            try:
                _pf_queue.remove(p)   # re-requested: move to the newest end
            except ValueError:
                pass
            _pf_queue.append(p)
            while len(_pf_queue) > PREFETCH_MAX:
                _pf_queue.popleft()
        while len(_pf_threads) < PREFETCH_WORKERS:
            t = threading.Thread(target=_pf_worker, name=f"prefetch-{len(_pf_threads)}", daemon=True)
            _pf_threads.append(t)
            t.start()
        _pf_cv.notify_all()


def prefetch_pending() -> List[str]:
    with _pf_cv:
        return list(_pf_queue)


def _pf_worker() -> None:
    while True:
        with _pf_cv:
            while not _pf_queue:
                _pf_cv.wait()
            p = _pf_queue.popleft()
        _safe_proxy(p)


def _safe_proxy(p):
    try:
        proxy_paths(p, prio=decodegate.PREFETCH)
    except Exception:
        log.debug("prefetch of %s failed", p, exc_info=True)


# existing-text analysis results are also cached on disk next to the proxy (same key), so
# re-opens and batch pre-flight don't analyze again. Bump when analyze_existing changes.
ANALYSIS_VERSION = 7   # 2: provenance (isCopy/copyUnknown) on every path; record from a marker payload
#                       3: B/C need writing (a plain border is no case); edgeConfidence; hasText = words read
#                       4: OCR reads count only as plausible text on writing-sized marks
#                       5: writing-shaped marks count with or without OCR (single strips too)
#                       6: unread marks count only as several glyph-sized pieces (not a paper edge)
#                       7: ...or as a looping line (connected cursive)


def _existing_cache_file(path: str, info: ImageInfo, ocr: bool) -> str:
    key = proxy.cache_key(path, info.size_bytes, info.mtime_ns, info.file_id)
    tag = ""
    if ocr:
        try:
            from .ocr import engines
            tag = "o" + hashlib.sha1(json.dumps(engines(), sort_keys=True, default=str).encode()).hexdigest()[:8]
        except Exception:
            tag = "o"
    return os.path.join(paths.sub("cache"), f"{key}.existing{ANALYSIS_VERSION}{tag}.json")


def _load_existing(path: str, info: ImageInfo, run_ocr: bool):
    """(result, with_ocr) from the disk cache; an OCR result also serves a no-OCR request."""
    for ocr in ((True,) if run_ocr else (False, True)):
        try:
            with open(_existing_cache_file(path, info, ocr), "rb") as f:
                return json.loads(f.read().decode("utf-8")), ocr
        except (OSError, ValueError):
            continue
    return None, False


def _store_existing(path: str, info: ImageInfo, run_ocr: bool, res: Dict[str, Any]) -> None:
    try:
        data = json.dumps(res, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return  # not plain JSON: keep it in memory only
    try:
        atomic_write_bytes(_existing_cache_file(path, info, run_ocr), data)
    except OSError:
        pass


def existing(path: str, run_ocr: bool = True, prio: int = decodegate.CURRENT) -> Dict[str, Any]:
    from .existing import analyze_existing
    info = probe(path)
    k = _key(path, info)
    with _lock:
        hit = _cache.get(k)
        if hit and "existing" in hit and (hit.get("existing_ocr") or not run_ocr):
            return hit["existing"]
    res, ocr_done = _load_existing(path, info, run_ocr)
    if res is None:
        ocr_done = run_ocr
        md = raw_md(path)
        if prio == decodegate.CURRENT:
            arr, info = full_array(path, info=info)
        else:
            # batch pre-flight: one decode for the proxy and the analysis, not kept
            arr, _ = _peek_full(path, info)
            if arr is None:
                with decodegate.slot(info, prio):
                    arr, info = load_upright(path, info)
            if proxy.cached_proxy(path, info) is None:
                proxy.get_proxy(path, info, arr)
        if prio == decodegate.CURRENT:
            with decodegate.foreground():
                res = analyze_existing(arr, info, md, run_ocr=run_ocr)
        else:
            res = analyze_existing(arr, info, md, run_ocr=run_ocr)
        del arr
        _store_existing(path, info, run_ocr, res)
    with _lock:
        ent = _cache.setdefault(k, {})
        ent["existing"] = res
        ent["existing_ocr"] = ocr_done
    return res


# ---------------------------------------------------------------------------
# full-resolution access for the current photo: proxy, existing-text analysis, detail
# views (zoom beyond the proxy, the edge loupe) and the erase-in-place preview share one
# decode. Only one full-res image is kept, and only while it is used: it is dropped after
# FULL_IDLE_S without use, when another photo is decoded, or when the decode gate needs
# the memory.

FULL_IDLE_S = 60.0
_full: Dict[str, Any] = {"key": None, "arr": None, "info": None, "used": 0.0}
_full_lock = threading.Lock()
_sweeper: Optional[threading.Thread] = None


def _drop_full() -> None:
    """Forget the cached array (no lock: called from the decode gate and the idle sweeper)."""
    _full.update(key=None, arr=None, info=None, used=0.0)
    decodegate.set_pinned(0)


def _peek_full(path: str, info: ImageInfo):
    """The cached full array for ``path`` if it is the one held, else (None, None)."""
    k = _key(path, info)
    ent = dict(_full)
    if ent["key"] == k and ent["arr"] is not None:
        _full["used"] = time.monotonic()
        return ent["arr"], ent["info"]
    return None, None


def _sweep() -> None:
    while True:
        time.sleep(min(10.0, FULL_IDLE_S / 2))
        if _full["key"] is not None and time.monotonic() - _full["used"] > FULL_IDLE_S:
            if _full_lock.acquire(blocking=False):
                try:
                    if time.monotonic() - _full["used"] > FULL_IDLE_S:
                        _drop_full()
                finally:
                    _full_lock.release()


def _start_sweeper() -> None:
    global _sweeper
    if _sweeper is None or not _sweeper.is_alive():
        _sweeper = threading.Thread(target=_sweep, name="full-array-idle", daemon=True)
        _sweeper.start()


def full_array(path: str, info: Optional[ImageInfo] = None):
    with decodegate.foreground():
        return _full_array(path, info)


def _full_array(path: str, info: Optional[ImageInfo] = None):
    info = info or probe(path)
    k = _key(path, info)
    with _full_lock:
        arr, cinfo = _peek_full(path, info)
        if arr is not None:
            return arr, cinfo
        _drop_full()   # never hold the old photo while the new one decodes
        with decodegate.slot(info, decodegate.CURRENT):
            arr, info = load_upright(path, info)
        _full.update(key=k, arr=arr, info=info, used=time.monotonic())
        decodegate.set_pinned(arr.nbytes, _drop_full)
        _start_sweeper()
    if proxy.cached_proxy(path, info) is None:
        # the first decode of a photo also builds its proxy and thumbnail
        try:
            proxy.get_proxy(path, info, arr)
        except Exception:
            pass
    return arr, info


def crop_webp(path: str, x: int, y: int, w: int, h: int, out_w: int) -> bytes:
    import io
    import cv2
    from PIL import Image
    from .colors import to_display_srgb8
    arr, info = full_array(path)
    H, W = arr.shape[:2]
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(W, int(x + w)), min(H, int(y + h))
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Empty crop")
    sub = arr[y0:y1, x0:x1]
    out_w = max(1, min(int(out_w), 4096))
    if sub.shape[1] > out_w:
        s = out_w / sub.shape[1]
        squeeze = sub.shape[2] == 1
        sub = cv2.resize(sub[:, :, 0] if squeeze else sub, (out_w, max(1, round(sub.shape[0] * s))),
                         interpolation=cv2.INTER_AREA)
        if sub.ndim == 2:
            sub = sub[:, :, None]
    disp = to_display_srgb8(sub, info.icc, info.mode)
    buf = io.BytesIO()
    Image.fromarray(disp, "RGB").save(buf, "WEBP", quality=92)
    return buf.getvalue()


def erase_preview_webp(path: str, erase: Dict[str, Any], long_edge: int = 2560) -> bytes:
    """The erase-in-place result at proxy size (for the After pane)."""
    import io
    import cv2
    from PIL import Image
    from .colors import to_display_srgb8
    from .save import _erase_inputs
    from .erase import INPAINT_RADIUS, erase_in_place, shift_band
    arr, info = full_array(path)
    from .save import SaveError
    try:
        mask, band = _erase_inputs(arr, erase)
    except SaveError as e:   # e.g. an oversized brush mask: the endpoint answers 400
        raise ValueError(str(e))
    ys, xs = np.nonzero(mask)
    out = arr
    if len(ys):
        # erase only an ROI around the mask (inpainting context included), with
        # the band geometry shifted into ROI coordinates
        pad = 3 * INPAINT_RADIUS + 4 + 16
        y0, y1 = max(0, ys.min() - pad), min(arr.shape[0], ys.max() + pad + 1)
        x0, x1 = max(0, xs.min() - pad), min(arr.shape[1], xs.max() + pad + 1)
        sub_band = shift_band(band, x0, y0, x1 - x0, y1 - y0)
        er = erase_in_place(arr[y0:y1, x0:x1], mask[y0:y1, x0:x1], sub_band, method=erase.get("method", "auto"))
        out = arr.copy()
        out[y0:y1, x0:x1] = er
    H, W = out.shape[:2]
    s = min(1.0, long_edge / max(H, W))
    small = cv2.resize(out, (max(1, round(W * s)), max(1, round(H * s))), interpolation=cv2.INTER_AREA) if s < 1 else out
    if small.ndim == 2:
        small = small[:, :, None]
    disp = to_display_srgb8(small, info.icc, info.mode)
    buf = io.BytesIO()
    Image.fromarray(disp, "RGB").save(buf, "WEBP", quality=90)
    return buf.getvalue()
