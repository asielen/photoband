"""Erase existing caption text in place (cases B and C).

* Flat band (``auto`` picks it only for a flat digital fill: noise below
  ``FLAT_NOISE``, sides within ``FLAT_DRIFT`` levels, no lighting trend):
  masked pixels are set to the band colour (exact, per band side).
* Anything else (scanned paper, lighting drift, JPEG'd scans): OpenCV Telea
  inpainting, then grain matching the paper noise measured around the mask.

The photo is never touched: it (and its tilted outline) is removed from the
mask.  Photo pixels and the blurred photo-edge strip are never inpainting
sources either: they are inpainted along with the mask (then discarded), so
photo colours cannot bleed into the fill.
"""
from __future__ import annotations

from typing import Optional

import numpy as np

try:
    import cv2
except Exception as exc:  # pragma: no cover
    raise ImportError("photoband.erase needs opencv-python") from exc

from .detect import (FLAT_DRIFT, FLAT_NOISE, BandResult, _as3d, _clear_photo, _ncolor, _photo_masks, _unit,
                     text_mask)

INPAINT_RADIUS = 5


def _fit_mask(m, shape) -> Optional[np.ndarray]:
    """Bool mask at image size (user brush masks may come at proxy size)."""
    if m is None:
        return None
    m = np.asarray(m)
    if m.ndim == 3:
        m = m[:, :, 0]
    m = m.astype(bool)
    H, W = shape
    if m.shape != (H, W):
        m = cv2.resize(m.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST).astype(bool)
    return m


def build_mask(arr, band, blocks, grow=2, add_mask=None, remove_mask=None) -> np.ndarray:
    """Text mask (actual text pixels in the line boxes, grown by ``grow`` px), plus
    the user's brush ``add_mask``, minus ``remove_mask``; never inside the photo."""
    arr = _as3d(np.asarray(arr))
    H, W = arr.shape[:2]
    m = text_mask(arr, band, blocks or [], grow=grow)
    add = _fit_mask(add_mask, (H, W))
    rem = _fit_mask(remove_mask, (H, W))
    if add is not None:
        m |= add
    if rem is not None:
        m &= ~rem
    _clear_photo(m, band)
    return m


def _band_fill_color(arr, band: BandResult, side_rect=None) -> np.ndarray:
    nc = _ncolor(arr)
    col = None
    if side_rect is not None:
        col = side_rect.get("color")
    if col is None:
        col = band.band_color
    c = np.asarray(col, np.float64)
    if c.size == 0:
        raise ValueError("band has no colour")
    if c.size != nc:
        c = np.resize(c, nc)
    return c


def _cast(values: np.ndarray, dtype) -> np.ndarray:
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.clip(np.round(values), info.min, info.max).astype(dtype)
    return values.astype(dtype)


def _inpaint_roi(roi: np.ndarray, m8: np.ndarray, radius: int) -> np.ndarray:
    """Telea inpaint an H x W x nc ROI of any supported dtype."""
    nc = roi.shape[2]
    if roi.dtype == np.uint8 and nc == 3:
        return cv2.inpaint(np.ascontiguousarray(roi), m8, radius, cv2.INPAINT_TELEA)
    out = np.empty_like(roi)
    for ch in range(nc):
        plane = np.ascontiguousarray(roi[:, :, ch])
        try:
            # Telea supports 8U, 16U and 32F single-channel images
            if plane.dtype not in (np.uint8, np.uint16, np.float32):
                raise cv2.error("unsupported")
            out[:, :, ch] = cv2.inpaint(plane, m8, radius, cv2.INPAINT_TELEA)
        except cv2.error:
            res = cv2.inpaint(plane.astype(np.float32), m8, radius, cv2.INPAINT_TELEA)
            out[:, :, ch] = _cast(res, roi.dtype)
    return out


def _flat_ok(a: np.ndarray, band) -> bool:
    """A flat fill is invisible only on a flat digital band."""
    if band.textured or float(band.noise) >= FLAT_NOISE or float(getattr(band, "color_drift", 0.0)) > FLAT_DRIFT:
        return False
    cols = [np.asarray(b["color"], float) for b in (band.bands or []) if b.get("color") is not None]
    if len(cols) > 1:
        unit = _unit(a)
        c = np.stack(cols) / unit
        if float((c.max(axis=0) - c.min(axis=0)).max()) > FLAT_DRIFT:
            return False
    return True


def shift_band(band, x0: int, y0: int, w: int, h: int):
    """``band`` in the coordinates of the ROI (x0, y0, w, h): rects clipped to it."""
    from dataclasses import replace

    def clip(r):
        x, y, rw, rh = (int(v) for v in r)
        a0, b0 = max(x - x0, 0), max(y - y0, 0)
        a1, b1 = min(x + rw - x0, w), min(y + rh - y0, h)
        return (a0, b0, max(0, a1 - a0), max(0, b1 - b0))

    px, py, pw, ph = (int(v) for v in band.photo_rect)
    bands = [dict(b, rect=clip(b["rect"])) for b in (band.bands or [])]
    bands = [b for b in bands if b["rect"][2] > 0 and b["rect"][3] > 0]
    quad = tuple((float(qx) - x0, float(qy) - y0) for qx, qy in (band.photo_quad or ()))
    return replace(band, photo_rect=(px - x0, py - y0, pw, ph), bands=bands, photo_quad=quad)


def _grain_sigma(roi8: np.ndarray, valid: np.ndarray) -> float:
    """Per-pixel paper grain (8-bit std) from the unmasked paper around the mask."""
    if valid.sum() < 50:
        return 0.0
    vals = []
    for ch in range(roi8.shape[2]):
        x = np.ascontiguousarray(roi8[:, :, ch])
        r = x - cv2.blur(x, (5, 5))
        ok = cv2.erode(valid.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        if ok.sum() < 50:
            ok = valid
        vals.append(1.4826 * float(np.median(np.abs(r[ok]))) / 0.98)
    return float(np.mean(vals))


def erase_in_place(arr: np.ndarray, mask: np.ndarray, band, method: str = "auto") -> np.ndarray:
    """Return a new array with the masked caption pixels erased.

    ``method``: ``auto`` (flat only on a flat digital band, else inpaint),
    ``flat`` (band colour), ``inpaint`` (Telea + grain).  Works for uint8 and
    uint16, 1-4 channels; alpha is left unchanged.  Photo pixels (``photo_rect``
    and the tilted ``photo_quad``) are never modified."""
    src = np.asarray(arr)
    squeeze = src.ndim == 2
    a = _as3d(src)
    H, W = a.shape[:2]
    out = a.copy()
    m = _fit_mask(mask, (H, W))
    if m is None:
        return out[:, :, 0] if squeeze else out
    m = m.copy()
    _clear_photo(m, band)
    if not m.any():
        return out[:, :, 0] if squeeze else out
    if method == "auto":
        method = "flat" if _flat_ok(a, band) else "inpaint"
    nc = _ncolor(a)

    if method == "flat":
        # fill each band side with its own median colour
        bands = band.bands or [{"rect": (0, 0, W, H)}]
        done = np.zeros_like(m)
        for bd in bands:
            x, y, w, h = (int(v) for v in bd["rect"])
            x, y = max(0, x), max(0, y)
            sub = m[y:y + h, x:x + w] & ~done[y:y + h, x:x + w]
            if not sub.any():
                continue
            c = _cast(_band_fill_color(a, band, bd), a.dtype)
            view = out[y:y + h, x:x + w, :nc]
            view[sub] = c
            done[y:y + h, x:x + w] |= sub
        rest = m & ~done  # user brush outside every band rect
        if rest.any():
            out[:, :, :nc][rest] = _cast(_band_fill_color(a, band), a.dtype)
        return out[:, :, 0] if squeeze else out

    if method != "inpaint":
        raise ValueError(f"unknown method {method!r}")

    ys, xs = np.nonzero(m)
    margin = 3 * INPAINT_RADIUS + 4
    y0, y1 = max(0, ys.min() - margin), min(H, ys.max() + margin + 1)
    x0, x1 = max(0, xs.min() - margin), min(W, xs.max() + margin + 1)
    roi = out[y0:y1, x0:x1, :nc].copy()
    mroi = m[y0:y1, x0:x1]
    src_m = mroi.copy()
    if band.found:
        hard, strip = _photo_masks(band, x0, y0, x1 - x0, y1 - y0)
        prot = (hard | strip) & ~mroi
        if prot.any():
            # photo / blurred-edge pixels near the mask are inpainted too (never
            # used as sources); farther ones get the band colour
            near = cv2.dilate(mroi.astype(np.uint8), np.ones((2 * margin + 1, 2 * margin + 1), np.uint8)).astype(bool)
            src_m |= prot & near
            far = prot & ~near
            if far.any():
                roi[far] = _cast(_band_fill_color(a, band), a.dtype)
    filled = _inpaint_roi(roi, src_m.astype(np.uint8) * 255, INPAINT_RADIUS)

    # grain: match the paper's noise around the mask so the fill does not look smooth
    unit = _unit(a)
    valid = ~src_m
    sigma = _grain_sigma(roi.astype(np.float32) / np.float32(unit), valid)
    if sigma > 0.3:
        rng = np.random.default_rng(int(mroi.sum()) & 0xFFFFFFFF)
        grain = rng.normal(0.0, sigma * unit, size=(int(mroi.sum()), 1))
        vals = filled[mroi].astype(np.float64) + grain  # same grain on all channels (luma-like)
        filled[mroi] = _cast(vals, a.dtype)

    view = out[y0:y1, x0:x1, :nc]
    view[mroi] = filled[mroi]
    return out[:, :, 0] if squeeze else out
