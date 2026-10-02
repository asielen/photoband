"""Text recognition for existing captions.

Engine order (see :func:`engines`):

1. ``vision``    - Apple Vision (macOS, pyobjc), ``VNRecognizeTextRequest`` at the
   accurate level; reads print and handwriting.
2. ``winocr``    - ``Windows.Media.Ocr`` (Windows, pywinrt ``winrt.windows.*``
   packages); print only, no per-word confidence.
3. ``tesseract`` - bundled Tesseract CLI fallback (TSV output).

The OS adapters are import-guarded: a missing package or a failing call makes
the engine unavailable and recognition falls through to the next engine; it
never raises.

All results are ``{"text", "confidence", "words": [{"text", "confidence",
"box"}], "engine"}`` with confidences in 0..1 and word boxes in the input
crop's pixel coordinates (x, y, w, h).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import List, Optional

import numpy as np

try:
    import cv2
except Exception as exc:  # pragma: no cover
    raise ImportError("photoband.ocr needs opencv-python") from exc

log = logging.getLogger(__name__)

TARGET_TEXT_HEIGHT = 64      # px: full line height (ascender..descender) -> x-height ~ 32-40
PAD = 16                     # white border added around crops for Tesseract
UNKNOWN_CONFIDENCE = 0.8     # engines without per-word confidence (Windows.Media.Ocr)

_EMPTY = {"text": "", "confidence": 0.0, "words": [], "engine": None}


# =============================================================================
# image preparation (shared)
# =============================================================================

def _to_rgb8(img: np.ndarray) -> np.ndarray:
    a = np.asarray(img)
    if a.dtype == np.uint16:
        a = (a.astype(np.float32) / 257.0 + 0.5).astype(np.uint8)
    elif a.dtype != np.uint8:
        a = np.clip(a, 0, 255).astype(np.uint8)
    if a.ndim == 2:
        a = a[:, :, None]
    if a.shape[2] in (1, 2):
        a = np.repeat(a[:, :, :1], 3, axis=2)
    return np.ascontiguousarray(a[:, :, :3])


def _ink_image(rgb: np.ndarray) -> np.ndarray:
    """Soft-binarised grey image with dark text on white, whatever the polarity and
    colours of the input.  Background = median of the crop border; ink strength =
    max per-channel distance from it, stretched to the 95th percentile of ink."""
    f = rgb.astype(np.float32)
    border = np.concatenate([f[:2].reshape(-1, 3), f[-2:].reshape(-1, 3),
                             f[:, :2].reshape(-1, 3), f[:, -2:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    d = np.abs(f - bg).max(axis=2)
    strong = d[d > max(12.0, 0.25 * float(d.max()))]
    top = float(np.percentile(strong, 90)) if strong.size else max(1.0, float(d.max()))
    top = max(top, 16.0)
    # soft: a gentle ramp instead of a hard threshold keeps antialiasing
    ink = np.clip((d - 0.08 * top) / (0.75 * top), 0.0, 1.0)
    return (255.0 * (1.0 - ink)).astype(np.uint8)


def _has_ink(img: np.ndarray, min_contrast: float = 12.0, min_pixels: int = 8) -> bool:
    rgb = _to_rgb8(img)
    return int((_ink_image(rgb) < 200).sum()) >= min_pixels and \
        float(np.ptp(rgb.reshape(-1, 3), axis=0).max()) >= min_contrast


def _text_rows(gray_inked: np.ndarray) -> List[tuple]:
    """Runs of rows that contain ink: [(start, end)]."""
    rows = (gray_inked < 128).sum(axis=1) > 0
    runs, start = [], None
    for i, r in enumerate(rows):
        if r and start is None:
            start = i
        elif not r and start is not None:
            runs.append((start, i)); start = None
    if start is not None:
        runs.append((start, len(rows)))
    return runs


def _fit_scale(scale: float, shape, maxdim: int, border: int) -> float:
    """``scale`` lowered so that the scaled image *plus* a ``border`` px border (both
    sides together) stays within ``maxdim`` px on its longer side."""
    longest = max(1, int(max(shape[:2])))
    return min(float(scale), max(1, maxdim - border) / float(longest))


def _fits(shape, maxdim: int, border: int) -> bool:
    """Whether the image, unscaled, plus the border is within the limit (so a scale
    close to 1 may skip the resize)."""
    return int(max(shape[:2])) + border <= maxdim


# Tesseract (Leptonica) refuses images with a side over 32767 px ("Image too large").
TESSERACT_MAX_DIM = 32767


def _prepare(img8: np.ndarray):
    """-> (prepared grey image, scale, pad, n_lines)"""
    rgb = _to_rgb8(img8)
    g = _ink_image(rgb)
    runs = [r for r in _text_rows(g) if r[1] - r[0] >= 2]
    if runs:
        hmax = max(b - a for a, b in runs)
        big = [r for r in runs if r[1] - r[0] >= 0.3 * hmax]
        # merge runs split by i-dots/accents: gap smaller than 25% of the line height
        merged = []
        for r in big:
            if merged and r[0] - merged[-1][1] < 0.25 * hmax:
                merged[-1] = (merged[-1][0], r[1])
            else:
                merged.append(r)
        n_lines = len(merged)
        line_h = float(np.median([b - a for a, b in merged]))
    else:
        n_lines, line_h = 1, float(g.shape[0])
    scale = float(np.clip(TARGET_TEXT_HEIGHT / max(line_h, 1.0), 0.35, 6.0))
    # the size limit applies to the image Tesseract gets, after the 2 * PAD border
    scale = _fit_scale(scale, g.shape, TESSERACT_MAX_DIM, 2 * PAD)
    if abs(scale - 1.0) > 0.05 or not _fits(g.shape, TESSERACT_MAX_DIM, 2 * PAD):
        interp = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
        g = cv2.resize(g, (max(1, round(g.shape[1] * scale)), max(1, round(g.shape[0] * scale))),
                       interpolation=interp)
    else:
        scale = 1.0
    g = cv2.copyMakeBorder(g, PAD, PAD, PAD, PAD, cv2.BORDER_CONSTANT, value=255)
    return g, scale, PAD, n_lines


# =============================================================================
# Tesseract
# =============================================================================

@lru_cache(maxsize=1)
def tesseract_path() -> Optional[str]:
    """Bundled ``<resource_dir>/vendor/tesseract/tesseract(.exe)``, then
    ``$PHOTOBAND_TESSERACT``, then PATH."""
    exe = "tesseract.exe" if sys.platform == "win32" else "tesseract"
    try:
        from .paths import resource_dir
        p = os.path.join(resource_dir(), "vendor", "tesseract", exe)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    except Exception:
        pass
    env = os.environ.get("PHOTOBAND_TESSERACT")
    if env and os.path.isfile(env):
        return env
    return shutil.which("tesseract")


def _tess_env(binary: str) -> dict:
    env = dict(os.environ)
    env.setdefault("OMP_THREAD_LIMIT", "1")
    tessdata = os.path.join(os.path.dirname(binary), "tessdata")
    if os.path.isdir(tessdata) and "TESSDATA_PREFIX" not in os.environ:
        env["TESSDATA_PREFIX"] = tessdata
    return env


def _run_tesseract(gray: np.ndarray, psm: int, whitelist: Optional[str] = None,
                   timeout: float = 10.0) -> List[dict]:
    """Run the CLI on a grey image; returns TSV word rows (level 5)."""
    binary = tesseract_path()
    if not binary:
        raise RuntimeError("tesseract not found")
    ok, png = cv2.imencode(".png", gray)
    if not ok:
        raise RuntimeError("png encode failed")
    cmd = [binary, "stdin", "stdout", "-l", "eng", "--psm", str(psm)]
    if whitelist:
        cmd += ["-c", "tessedit_char_whitelist=" + whitelist]
    cmd += ["-c", "preserve_interword_spaces=0", "tsv"]
    kw = {}
    if sys.platform == "win32":  # no console window flashing
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(cmd, input=png.tobytes(), capture_output=True, timeout=timeout,
                          env=_tess_env(binary), **kw)
    out = proc.stdout.decode("utf-8", "replace")
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            conf = float(parts[10])
            l, t, w, h = (int(parts[i]) for i in (6, 7, 8, 9))
            key = (int(parts[2]), int(parts[3]), int(parts[4]))
        except ValueError:
            continue
        rows.append({"text": text, "confidence": max(0.0, conf) / 100.0, "box": (l, t, w, h), "line": key})
    return rows


def tesseract_words(gray: np.ndarray, psm: int = 11, timeout: float = 10.0) -> List[dict]:
    """Words on an arbitrary grey image, boxes in that image's coordinates."""
    return [{k: v for k, v in r.items() if k != "line"} for r in _run_tesseract(gray, psm, timeout=timeout)]


def _assemble(rows: List[dict], scale: float, pad: int, engine: str) -> dict:
    words, lines, last = [], [], None
    for r in rows:
        l, t, w, h = r["box"]
        box = (int(round((l - pad) / scale)), int(round((t - pad) / scale)),
               max(1, int(round(w / scale))), max(1, int(round(h / scale))))
        words.append({"text": r["text"], "confidence": float(r["confidence"]), "box": box})
        if r.get("line") != last:
            lines.append([])
            last = r.get("line")
        lines[-1].append(r["text"])
    text = "\n".join(" ".join(ws) for ws in lines)
    if words:
        wts = np.array([max(1, len(w["text"])) for w in words], float)
        conf = float(np.average([w["confidence"] for w in words], weights=wts))
    else:
        conf = 0.0
    return {"text": text, "confidence": conf, "words": words, "engine": engine}


def tesseract_recognize(img8: np.ndarray, psm: Optional[int] = None, whitelist: Optional[str] = None) -> dict:
    """Tesseract on one line/block crop: soft binarisation, upscale to ~64 px line
    height, psm 7 (single line) or psm 6 (block) chosen from the row profile."""
    g, scale, pad, n_lines = _prepare(img8)
    if psm is None:
        psm = 7 if n_lines <= 1 else 6
    rows = _run_tesseract(g, psm, whitelist)
    return _assemble(rows, scale, pad, "tesseract")


# =============================================================================
# Apple Vision (macOS)
# =============================================================================

def _probe_once(fn):
    """Cache an availability probe's answer, but only a definitive one: True, False, or
    a missing package (ImportError). Any other exception (a COM hiccup, a busy
    service) answers False for this call only, so one transient error does not turn
    the engine off for the rest of the process."""
    box: list = []

    def probe() -> bool:
        if box:
            return box[0]
        try:
            ok = bool(fn())
        except ImportError:
            ok = False
        except Exception as exc:
            log.debug("OCR availability probe %s failed: %s", fn.__name__, exc)
            return False
        box.append(ok)
        return ok

    probe.cache_clear = box.clear  # type: ignore[attr-defined]
    probe.__name__ = fn.__name__
    return probe


@_probe_once
def _vision_available() -> bool:
    if sys.platform != "darwin":
        return False
    import Vision  # noqa: F401  (pyobjc-framework-Vision)
    import Quartz  # noqa: F401  (pyobjc-framework-Quartz)
    from Foundation import NSData  # noqa: F401
    return hasattr(Vision, "VNRecognizeTextRequest")


def _vision_recognize(img8: np.ndarray) -> dict:
    import Quartz
    import Vision
    from Foundation import NSData

    rgb = _to_rgb8(img8)
    h0, w0 = rgb.shape[:2]
    # Vision wants text >= ~3% of image height; small crops are upscaled a little
    scale = float(np.clip(48.0 / max(1, h0), 1.0, 4.0))
    if scale > 1.0:
        rgb = cv2.resize(rgb, (round(w0 * scale), round(h0 * scale)), interpolation=cv2.INTER_CUBIC)
    padv = 8
    rgb = cv2.copyMakeBorder(rgb, padv, padv, padv, padv, cv2.BORDER_REPLICATE)
    H, W = rgb.shape[:2]
    ok, png = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise RuntimeError("png encode failed")
    data = NSData.dataWithBytes_length_(png.tobytes(), len(png))
    src = Quartz.CGImageSourceCreateWithData(data, None)
    cg = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, {})
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    try:
        req.setRecognitionLanguages_(["en-US"])
        if hasattr(req, "setAutomaticallyDetectsLanguage_"):
            req.setAutomaticallyDetectsLanguage_(True)
    except Exception:
        pass
    res = handler.performRequests_error_([req], None)
    success = res[0] if isinstance(res, tuple) else bool(res)
    if not success:
        raise RuntimeError("Vision request failed")
    observations = list(req.results() or [])

    def to_box(bb):
        # normalised, origin bottom-left -> crop pixels, origin top-left
        (bx, by), (bw, bh) = bb.origin, bb.size
        x = bx * W - padv
        y = (1.0 - by - bh) * H - padv
        return (int(round(x / scale)), int(round(y / scale)),
                max(1, int(round(bw * W / scale))), max(1, int(round(bh * H / scale))))

    lines_txt, words, confs = [], [], []
    observations.sort(key=lambda o: (-(o.boundingBox().origin.y), o.boundingBox().origin.x))
    for obs in observations:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        s = str(cand.string())
        c = float(cand.confidence())
        lines_txt.append(s)
        confs.append((c, len(s)))
        # Vision has no per-word confidence; words get the line confidence and
        # their own boxes via boundingBoxForRange.
        pos = 0
        for tok in s.split():
            start = s.find(tok, pos)
            pos = start + len(tok)
            box = to_box(obs.boundingBox())
            try:
                r = cand.boundingBoxForRange_error_((start, len(tok)), None)
                rect_obs = r[0] if isinstance(r, tuple) else r
                if rect_obs is not None:
                    box = to_box(rect_obs.boundingBox())
            except Exception:
                pass
            words.append({"text": tok, "confidence": c, "box": box})
    conf = float(np.average([c for c, _ in confs], weights=[max(1, n) for _, n in confs])) if confs else 0.0
    return {"text": "\n".join(lines_txt), "confidence": conf, "words": words, "engine": "vision"}


# =============================================================================
# Windows.Media.Ocr
# =============================================================================

@_probe_once
def _winocr_available() -> bool:
    if sys.platform != "win32":
        return False
    from winrt.windows.media.ocr import OcrEngine  # noqa: F401
    from winrt.windows.graphics.imaging import SoftwareBitmap  # noqa: F401
    from winrt.windows.storage.streams import DataWriter  # noqa: F401
    eng = OcrEngine.try_create_from_user_profile_languages()
    return eng is not None


def _winocr_recognize(img8: np.ndarray) -> dict:
    import asyncio

    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter

    rgb = _to_rgb8(img8)
    h0, w0 = rgb.shape[:2]
    # Windows OCR works best with ~ 20-40 px cap height; it also caps the image size
    scale = float(np.clip(60.0 / max(1, h0), 1.0, 4.0))
    padv = 16
    # the limit is on the bitmap handed over, i.e. after the 2 * padv border is added
    maxdim = int(getattr(OcrEngine, "max_image_dimension", 10000) or 10000)
    scale = _fit_scale(scale, (h0, w0), maxdim, 2 * padv)
    if abs(scale - 1.0) > 0.01 or not _fits((h0, w0), maxdim, 2 * padv):
        rgb = cv2.resize(rgb, (max(1, round(w0 * scale)), max(1, round(h0 * scale))),
                         interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA)
    rgb = cv2.copyMakeBorder(rgb, padv, padv, padv, padv, cv2.BORDER_REPLICATE)
    H, W = rgb.shape[:2]
    bgra = np.ascontiguousarray(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGRA))

    writer = DataWriter()
    try:
        writer.write_bytes(bgra.tobytes())          # pywinrt >= 2 (buffer protocol)
    except TypeError:
        writer.write_bytes(list(bgra.tobytes()))    # older bindings want a list of ints
    buf = writer.detach_buffer()
    # pywinrt >= 3 names the overload with an alpha mode create_copy_with_alpha_from_buffer
    make = getattr(SoftwareBitmap, "create_copy_with_alpha_from_buffer", None) or SoftwareBitmap.create_copy_from_buffer
    bmp = make(buf, BitmapPixelFormat.BGRA8, W, H, BitmapAlphaMode.PREMULTIPLIED)
    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("no OCR language installed")

    async def run():
        return await engine.recognize_async(bmp)

    try:
        result = asyncio.run(run())
    except RuntimeError:  # already inside an event loop (e.g. called from FastAPI)
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(run())
        finally:
            loop.close()

    lines_txt, words = [], []
    for line in result.lines:
        lines_txt.append(str(line.text))
        for w in line.words:
            r = w.bounding_rect
            box = (int(round((r.x - padv) / scale)), int(round((r.y - padv) / scale)),
                   max(1, int(round(r.width / scale))), max(1, int(round(r.height / scale))))
            words.append({"text": str(w.text), "confidence": UNKNOWN_CONFIDENCE, "box": box})
    conf = UNKNOWN_CONFIDENCE if words else 0.0
    return {"text": "\n".join(lines_txt), "confidence": conf, "words": words, "engine": "winocr"}


# =============================================================================
# public API
# =============================================================================

_ADAPTERS = {
    "vision": (_vision_available, _vision_recognize),
    "winocr": (_winocr_available, _winocr_recognize),
    "tesseract": (lambda: tesseract_path() is not None,
                  lambda img: tesseract_recognize(img)),
}
# Engines turned off for the rest of the process, and engines that have read at least
# one crop. Only an engine that has never worked here is turned off: a binding problem
# fails on every crop, so an engine that has succeeded once and then raises had a
# problem with that crop, not with its binding.
_broken: set = set()
_ok: set = set()
_broken_lock = threading.Lock()


ENGINE_ORDER = ("vision", "winocr", "tesseract")   # preference order


def _rank(name: str) -> tuple:
    return (ENGINE_ORDER.index(name) if name in ENGINE_ORDER else len(ENGINE_ORDER), name)


def engines() -> List[str]:
    """Available engines in preference order, e.g. ``["tesseract"]``."""
    out = []
    for name in ENGINE_ORDER:
        try:
            if name not in _broken and _ADAPTERS[name][0]():
                out.append(name)
        except Exception:
            pass
    return out


def recognize(img8: np.ndarray) -> dict:
    """Recognise one line/block crop (8-bit RGB or grey).  Tries each engine in
    order; an engine that raises is skipped for this crop (and turned off for the
    process when it fails on a binding-level problem before ever having worked).
    Never raises: returns empty text if all fail."""
    img = np.asarray(img8)
    if img.size == 0 or min(img.shape[:2]) < 2:
        return dict(_EMPTY)
    if not _has_ink(img):  # engines hallucinate on blank crops
        return dict(_EMPTY)
    last_err = None
    for name in engines():
        try:
            r = _ADAPTERS[name][1](img)
            r["engine"] = name
            if name not in _ok:
                with _broken_lock:
                    _ok.add(name)
            return r
        except (ImportError, AttributeError, TypeError) as exc:
            # a missing or changed binding fails the same way on every crop: stop using the
            # engine - unless it has worked before, then only this crop failed
            with _broken_lock:
                if name not in _ok:
                    if name not in _broken:
                        log.warning("OCR engine %s does not work here (%s); using the next one", name, exc)
                    _broken.add(name)
            last_err = exc
        except Exception as exc:
            last_err = exc
    out = dict(_EMPTY)
    if last_err is not None:
        out["error"] = str(last_err)
    return out


def _crop8(arr: np.ndarray, box, pad: int) -> tuple:
    a = arr if arr.ndim == 3 else arr[:, :, None]
    H, W = a.shape[:2]
    x, y, w, h = (int(v) for v in box)
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(W, x + w + pad), min(H, y + h + pad)
    nc = 1 if a.shape[2] in (1, 2) else 3
    crop = a[y0:y1, x0:x1, :nc]
    return _to_rgb8(crop), (x0, y0)


def recognize_blocks(arr, blocks, crop_fn=None) -> List[str]:
    """Fill ``TextLine.text / confidence / words`` in place (word boxes in
    full-resolution image coordinates).  Lines are recognised in parallel.

    ``crop_fn(line) -> (rgb8 crop, (x0, y0)) | None`` supplies a cleaned crop
    (e.g. :func:`photoband.detect.line_ocr_crop`: only the line's own ink, clipped
    to its band); None falls back to a padded crop of the line box.

    Returns the engines that read the lines, most used first."""
    arr = np.asarray(arr)
    lines = [ln for b in blocks for ln in b.lines]
    if not lines:
        return []
    used: List[str] = []

    def work(ln):
        got = None
        if crop_fn is not None:
            try:
                got = crop_fn(ln)
            except Exception:
                got = None
        if got is not None:
            crop, (ox, oy) = got
        else:
            pad = max(6, int(0.35 * ln.box[3]))
            crop, (ox, oy) = _crop8(arr, ln.box, pad)
        r = recognize(crop)
        if r.get("engine"):
            used.append(r["engine"])
        ln.text = r.get("text", "").replace("\n", " ").strip()
        ln.confidence = float(r.get("confidence", 0.0))
        ln.words = [{"text": w["text"], "confidence": float(w["confidence"]),
                     "box": (w["box"][0] + ox, w["box"][1] + oy, w["box"][2], w["box"][3])}
                    for w in r.get("words", [])]

    workers = min(len(lines), max(1, min(4, (os.cpu_count() or 2))))
    if workers == 1:
        for ln in lines:
            work(ln)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, lines))
    # most used first; a tie goes to the preferred engine (set order depends on the hash seed)
    return sorted(set(used), key=lambda e: (-used.count(e), _rank(e)))
