"""Final compositing. The backend never renders text: it fills borders, copies the
photo pixels byte for byte, and alpha-composites the text layer tiles the UI drew
with the same layout engine as the preview.

Layout object (produced by ui/src/lib/layout.ts, see docs/layout-format.md)::

    {
      "version": 1,
      "mode": "band" | "erase",
      "sourceRect": [x, y, w, h],      # part of the upright source that is the photo
      "canvas": [W, H],
      "photoRect": [x, y, w, h],       # where the photo sits in the canvas
      "fills": [{"rect": [x, y, w, h], "color": "#rrggbb"}],   # borders, band, divider, keyline
      "bandColor": "#rrggbb",
      "protect": [[x, y, w, h], ...],  # divider/keyline: the hidden marker keeps out
      "textAreas": [{"id": "a0", "rect": [x, y, w, h]}],
      "runs": [...],                   # positioned text (UI only)
      "textColors": ["#rrggbb", ...]
    }
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from .colors import parse_hex, srgb_to_file


class LayoutError(ValueError):
    pass


TILE_MAX = 4096          # px a side, as ui/src/lib/render.ts TILE_MAX


def upload_size(data: bytes) -> Tuple[int, int]:
    """(width, height) of an uploaded image from its header, without decoding the pixels."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception as e:
        raise ValueError(f"not a readable image ({e})")


@dataclass
class Tile:
    x: int
    y: int
    png: bytes


@dataclass
class CompositeResult:
    canvas: np.ndarray
    photo_rect: Tuple[int, int, int, int]
    band_color: np.ndarray            # file-space pixel value (color channels only)
    protect: Optional[np.ndarray]     # bool mask for the marker to avoid (None: nothing protected)
    notes: List[str] = field(default_factory=list)


def _rect(r, W=None, H=None) -> Tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(round(float(v))) for v in r)
    except Exception:
        raise LayoutError(f"Bad rectangle {r!r}")
    if w < 0 or h < 0:
        raise LayoutError(f"Negative rectangle {r!r}")
    return x, y, w, h


def _clip(r, W, H):
    x, y, w, h = r
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    return x0, y0, max(0, x1 - x0), max(0, y1 - y0)


def _outside(r, hole):
    """Up to four rectangles covering ``r`` minus ``hole`` (all as x, y, w, h)."""
    x, y, w, h = r
    hx, hy, hw, hh = hole
    x1, y1 = x + w, y + h
    ix0, iy0 = max(x, hx), max(y, hy)
    ix1, iy1 = min(x1, hx + hw), min(y1, hy + hh)
    if ix1 <= ix0 or iy1 <= iy0:
        return [r] if w and h else []
    out = [(x, y, w, iy0 - y), (x, iy1, w, y1 - iy1),          # above, below
           (x, iy0, ix0 - x, iy1 - iy0), (ix1, iy0, x1 - ix1, iy1 - iy0)]   # left, right
    return [q for q in out if q[2] > 0 and q[3] > 0]


MAX_CANVAS_PIXELS = 1_200_000_000


def composite(src: np.ndarray, icc: Optional[bytes], layout: Dict, tiles: Sequence[Tile],
              erase_mask: Optional[np.ndarray] = None, erase_band=None, erase_method: str = "auto") -> CompositeResult:
    """Build the output canvas from the upright source array ``src``."""
    notes: List[str] = []
    C = src.shape[2]
    dt = src.dtype
    ncolor = 1 if C in (1, 2) else 3
    mode = layout.get("mode", "band")
    band_hex = layout.get("bandColor") or "#ffffff"
    band_val = srgb_to_file(band_hex, C, dt, icc)

    if mode == "erase":
        W, H = src.shape[1], src.shape[0]
        canvas = src.copy()
        pr = _rect(layout.get("photoRect") or [0, 0, W, H])
        if erase_mask is not None and erase_mask.any():
            from .erase import erase_in_place
            canvas = erase_in_place(canvas, erase_mask, erase_band, method=erase_method)
            # erase_in_place never touches the photo rect; enforce anyway
            x, y, w, h = _clip(pr, W, H)
            canvas[y:y + h, x:x + w] = src[y:y + h, x:x + w]
        photo_rect = _clip(pr, W, H)
        if erase_band is not None and getattr(erase_band, "band_color", None):
            bc = np.array(erase_band.band_color, dtype=np.float64)[:ncolor]
            band_val = np.concatenate([np.round(bc).astype(dt),
                                       band_val[ncolor:]]).astype(dt) if C in (2, 4) else np.round(bc).astype(dt)
    else:
        sx, sy, sw, sh = _rect(layout.get("sourceRect") or [0, 0, src.shape[1], src.shape[0]])
        if sx < 0 or sy < 0 or sx + sw > src.shape[1] or sy + sh > src.shape[0] or sw == 0 or sh == 0:
            raise LayoutError("The photo rectangle lies outside the image")
        cw, ch = (int(v) for v in layout["canvas"])
        if cw <= 0 or ch <= 0 or cw * ch > MAX_CANVAS_PIXELS:
            raise LayoutError("Output size is out of range")
        px, py, pw, ph = _rect(layout["photoRect"])
        if (pw, ph) != (sw, sh):
            raise LayoutError("Photo size in the layout does not match the source")
        if px < 0 or py < 0 or px + pw > cw or py + ph > ch:
            raise LayoutError("Photo does not fit in the canvas")
        W, H = cw, ch
        canvas = np.empty((H, W, C), dtype=dt)
        # fills never touch the photo rectangle (the photo is copied there below), so only
        # the parts of each rectangle outside it are painted
        prect = (px, py, pw, ph)
        for r in _outside((0, 0, W, H), prect):
            canvas[r[1]:r[1] + r[3], r[0]:r[0] + r[2]] = band_val
        for f in layout.get("fills", []):
            r = _clip(_rect(f["rect"]), W, H)
            if r[2] and r[3]:
                val = srgb_to_file(f.get("color", band_hex), C, dt, icc)
                for q in _outside(r, prect):
                    canvas[q[1]:q[1] + q[3], q[0]:q[0] + q[2]] = val
        # photo pixels last, byte for byte
        canvas[py:py + ph, px:px + pw] = src[sy:sy + sh, sx:sx + sw]
        photo_rect = (px, py, pw, ph)

    # --- text layer -----------------------------------------------------------
    colors_hex = layout.get("textColors") or []
    palette_rgb = np.array([parse_hex(c) for c in colors_hex] or [(0, 0, 0)], dtype=np.float32)
    palette_file = np.stack([srgb_to_file(c, C, dt, icc)[:ncolor] for c in (colors_hex or ["#000000"])])
    px, py, pw, ph = photo_rect
    for t in tiles:
        im = Image.open(io.BytesIO(t.png))
        if im.mode != "RGBA":
            im = im.convert("RGBA")
        rgba = np.asarray(im)
        th, tw = rgba.shape[:2]
        x0, y0 = int(t.x), int(t.y)
        # clip to canvas
        cx0, cy0 = max(0, x0), max(0, y0)
        cx1, cy1 = min(W, x0 + tw), min(H, y0 + th)
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        sub = rgba[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
        a = sub[:, :, 3].astype(np.float32) / 255.0
        # never paint over photo pixels
        ix0, iy0 = max(cx0, px), max(cy0, py)
        ix1, iy1 = min(cx1, px + pw), min(cy1, py + ph)
        if ix1 > ix0 and iy1 > iy0:
            a[iy0 - cy0:iy1 - cy0, ix0 - cx0:ix1 - cx0] = 0
        nz = a > 0
        if not nz.any():
            continue
        # map each text pixel's color to the nearest layout text color and use its
        # file-space value (exact ICC conversion instead of per-pixel rounding)
        rgb = sub[:, :, :3].astype(np.float32)[nz]
        d = ((rgb[:, None, :] - palette_rgb[None, :, :]) ** 2).sum(-1)
        idx = d.argmin(1)
        col = palette_file[idx].astype(np.float32)
        region = canvas[cy0:cy1, cx0:cx1]
        dst = region[:, :, :ncolor][nz].astype(np.float32)
        av = a[nz][:, None]
        out = col * av + dst * (1 - av)
        maxv = np.iinfo(dt).max
        vals = np.clip(np.round(out), 0, maxv).astype(dt)
        rv = region[:, :, :ncolor]
        rv[nz] = vals
        region[:, :, :ncolor] = rv

    protect = None   # no protected pixels: the marker needs no mask
    for r in layout.get("protect", []) or []:
        x, y, w, h = _clip(_rect(r), W, H)
        if w and h:
            if protect is None:
                protect = np.zeros((H, W), dtype=bool)
            protect[y:y + h, x:x + w] = True
    return CompositeResult(canvas, photo_rect, band_val[:ncolor], protect, notes)
