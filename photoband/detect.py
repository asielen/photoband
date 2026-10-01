"""Existing-caption detection: find a border/caption band, the text inside it,
estimate its style, and flag text printed over the photo itself.

Cases (see the spec, "Existing text: detect, read and replace"):

* **B** - a flat band without a photoband record (stripped metadata, other tool).
* **C** - a physical border on a scan (paper texture, handwriting).
* **D** - text printed over the photo (date stamp): flagged only.

Everything here works on H x W x C numpy arrays (C = 1..4, alpha last), dtype
uint8 or uint16, already upright.  Internally, "8-bit units" means values on a
0..255 scale regardless of the array's dtype (uint16 values are divided by 257).

Band detection outline
----------------------
1. Build a proxy (8-bit scale, INTER_AREA, long edge <= ``proxy_long_edge``).
2. For each side, look at the lines (rows for top/bottom, columns for left/right)
   going inward.  The band colour ``c`` is the median of the 4 outermost lines;
   they must be near-constant (robust spread <= 12).  The tolerance is
   ``clip(3.5 * noise, 3, 20)``.  ``f[i]`` is the fraction of pixels of line
   ``i`` within tolerance of ``c``:
   * ``f >= 0.9``: clean band line; ``0.5 <= f < 0.9``: partial (text, blend,
     JPEG ringing); ``f < 0.5``: not band.  A run of non-band lines followed
     again by two clean lines is a text line inside the band and is skipped.
   The proxy edge is the first non-band line after the band.
3. The edge must be a *step*, not a ramp (this is what rejects near-white sky):
   the transition may be at most ``2 + 8*scale`` proxy lines wide (sharp edge +
   one JPEG block of ringing); the mean distance to ``c`` must jump by
   ``>= 0.8 * tol`` from the clean band to just past the transition, by more
   than 2x what the photo side keeps changing over the same distance, and by
   more than 2x the colour drift inside the band; that drift itself must stay
   below ``max(4, tol/2)``.
4. A second pass repeats the scan with each side restricted to the photo span
   found in pass 1 (so side bands / bottom text don't pollute the profiles).
5. Each edge is refined at full resolution in a window around the proxy edge
   (+-3 proxy lines, +8 px for ringing), against the colour of full-res lines
   under the last clean proxy lines: the edge is the first line whose jump in
   mean distance is >= half the window's largest jump, moved to the half-level
   crossing for blurred (scanned) edges.  Exact for flat digital bands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

try:  # OpenCV is a hard dependency of the app, but keep import errors readable.
    import cv2
except Exception as exc:  # pragma: no cover
    raise ImportError("photoband.detect needs opencv-python") from exc

SIDES = ("top", "bottom", "left", "right")

# --- band detection thresholds -------------------------------------------------
MAX_BAND_FRAC = 0.6        # a band may be at most 60% of the image along its axis
MIN_PHOTO_FRAC = 0.2       # the photo must keep at least 20% of each dimension
MIN_BAND_PROXY = 3         # at least 3 proxy lines deep
CLEAN_F = 0.9              # line is "clean band" if >= 90% pixels within tol
BAND_F = 0.5               # below this a line is not band at all
OUTER_LINES = 4            # lines used to estimate the band colour
MAX_OUTER_SPREAD = 12.0    # 8-bit units; outer lines must be near-constant
TOL_MIN, TOL_MAX = 3.0, 20.0
MAX_DRIFT = 4.0            # 8-bit units: band colour may not drift more than this inward
TEXTURE_NOISE = 1.5        # band noise (8-bit std) above which paper grain is certain (case C cue)
FLAT_NOISE = 0.3           # below this (and FLAT_DRIFT) a band is a flat digital fill
FLAT_DRIFT = 2.0           # 8-bit: max colour difference between sides / trend along a band
MAX_TILT_DEG = 2.2         # tilted scans up to this angle are measured
TAN_MAX = float(np.tan(np.radians(MAX_TILT_DEG)))
N_SEG = 16                 # segments per side for the tilt-aware scan
REFINE_SEG_PX = 64         # full-res span per segment for edge refinement
MAX_LEVELS = 3             # nested levels (scanner lid -> paper -> photo)
RIM_PX = 10                # a jagged / deckled rim up to this many px is skipped
DRIFT_PER_DIM = 50.0       # 8-bit: band colour may drift this much over the image dimension

# --- text finding --------------------------------------------------------------
TEXT_WORK_LONG_EDGE = 4000  # text finding runs on at most this long edge per band
CAP_HEIGHT_EM = 0.72        # typical cap height / em for common sans/serif fonts


# =============================================================================
# small helpers
# =============================================================================

def _ncolor(arr: np.ndarray) -> int:
    """Number of colour channels (alpha dropped)."""
    if arr.ndim == 2:
        return 1
    c = arr.shape[2]
    return {1: 1, 2: 1, 3: 3, 4: 3}.get(c, min(c, 3))


def _as3d(arr: np.ndarray) -> np.ndarray:
    return arr[:, :, None] if arr.ndim == 2 else arr


def _unit(arr: np.ndarray) -> float:
    """Divisor that maps array units to 8-bit units."""
    if arr.dtype == np.uint16:
        return 257.0
    if arr.dtype == np.uint8:
        return 1.0
    if np.issubdtype(arr.dtype, np.floating):
        return 1.0 / 255.0
    return float(np.iinfo(arr.dtype).max) / 255.0 if np.issubdtype(arr.dtype, np.integer) else 1.0


def _as8f(block: np.ndarray, unit: float) -> np.ndarray:
    """Float32 copy on an 8-bit scale."""
    out = block.astype(np.float32)
    if unit != 1.0:
        out *= np.float32(1.0 / unit)
    return out


def _colors(arr: np.ndarray) -> np.ndarray:
    """View of the colour channels only (H x W x nc)."""
    a = _as3d(arr)
    return a[:, :, :_ncolor(arr)]


def _hex8(color8: Sequence[float]) -> str:
    vals = [int(max(0, min(255, round(float(v))))) for v in color8]
    if len(vals) == 1:
        vals = vals * 3
    return "#%02x%02x%02x" % tuple(vals[:3])


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, np.ndarray):
        return v.tolist()
    return v


def _resize_area(arr: np.ndarray, scale: float) -> np.ndarray:
    h, w = arr.shape[:2]
    nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
    a = arr
    if a.ndim == 3 and a.shape[2] == 1:
        a = a[:, :, 0]
    if a.ndim == 3 and a.shape[2] == 2:  # cv2 handles 1,3,4 channels best
        a = np.ascontiguousarray(a[:, :, :1])[:, :, 0]
    out = cv2.resize(np.ascontiguousarray(a), (nw, nh), interpolation=cv2.INTER_AREA)
    return out[:, :, None] if out.ndim == 2 else out


def _oriented(img: np.ndarray, side: str) -> np.ndarray:
    """View of ``img`` (H x W x C) where axis 0 runs inward from ``side``."""
    if side == "top":
        return img
    if side == "bottom":
        return img[::-1]
    if side == "left":
        return img.transpose(1, 0, 2)
    return img.transpose(1, 0, 2)[::-1]


def _side_lines(col: np.ndarray, side: str, a: int, b: int, lo: int, hi: int) -> np.ndarray:
    """Full-res lines ``a..b`` (distance from ``side``), span ``lo..hi``.  Shape (n, L, C)."""
    H, W = col.shape[:2]
    if side == "top":
        return col[a:b, lo:hi]
    if side == "bottom":
        return col[H - b:H - a, lo:hi][::-1]
    if side == "left":
        return col[lo:hi, a:b].transpose(1, 0, 2)
    return col[lo:hi, W - b:W - a][:, ::-1].transpose(1, 0, 2)


def _rect_of_band(side: str, depth: Dict[str, int], W: int, H: int) -> Tuple[int, int, int, int]:
    t, b, l, r = depth["top"], depth["bottom"], depth["left"], depth["right"]
    if side == "top":
        return (0, 0, W, t)
    if side == "bottom":
        return (0, H - b, W, b)
    if side == "left":
        return (0, t, l, H - t - b)
    return (W - r, t, r, H - t - b)


# =============================================================================
# band detection
# =============================================================================

@dataclass
class BandResult:
    found: bool
    photo_rect: tuple          # (x, y, w, h) exact full-res pixels; whole image if not found.
                               # On a tilted scan: the largest axis-aligned rect of photo pixels only.
    bands: list                # [{"side", "rect", "color"}]
    band_color: tuple          # per-channel, array units, no alpha
    band_color_hex: str        # 8-bit approximation "#rrggbb"
    textured: bool             # not a flat digital fill (grain, lighting drift) -> inpainting
    noise: float               # band noise in 8-bit units (robust to JPEG smoothing)
    confidence: float          # 0..1
    angle: float = 0.0         # detected tilt in degrees, + = counter-clockwise as displayed
    photo_quad: tuple = ()     # ((x, y) TL, TR, BR, BL) floats: the (possibly tilted) photo outline
    edge_blur: float = 0.0     # median 10-90 % width of the photo edge, px (tilt removed)
    color_drift: float = 0.0   # 8-bit: colour disagreement between sides / linear trend along a band
    nested: bool = False       # an outer scanner margin / lid level was skipped

    def to_json(self) -> dict:
        return _jsonable({
            "found": bool(self.found),
            "photo_rect": [int(v) for v in self.photo_rect],
            "bands": [{"side": b["side"], "rect": [int(v) for v in b["rect"]],
                       **({"color": [float(c) for c in b["color"]]} if "color" in b else {})}
                      for b in self.bands],
            "band_color": [float(v) for v in self.band_color],
            "band_color_hex": self.band_color_hex,
            "textured": bool(self.textured),
            "noise": round(float(self.noise), 3),
            "confidence": round(float(self.confidence), 3),
            "angle": round(float(self.angle), 3),
            "photo_quad": [[round(float(x), 2), round(float(y), 2)] for x, y in self.photo_quad],
            "edge_blur": round(float(self.edge_blur), 2),
            "color_drift": round(float(self.color_drift), 2),
            "nested": bool(self.nested),
        })


def _chan_dist(S: np.ndarray, c) -> np.ndarray:
    """max over channels of |S - c| (faster than ``np.abs(S - c).max(axis=2)``)."""
    d = np.abs(S[..., 0] - np.float32(c[0]))
    for ch in range(1, S.shape[-1]):
        np.maximum(d, np.abs(S[..., ch] - np.float32(c[ch])), out=d)
    return d


def _scan_side(V: np.ndarray, lo: int, hi: int, scale: float = 1.0, start: int = 0,
               extra_ramp: int = 0, rim: int = 0) -> Optional[dict]:
    """Scan one oriented proxy view (axis 0 = inward) over span ``lo..hi``,
    starting at depth ``start``.  Up to ``rim`` more starting lines are tried
    (a jagged / deckled rim, a thin scanner margin, a blended transition).
    Returns depth etc. or None."""
    n_along = V.shape[0]
    D = min(n_along, max(8, int(n_along * MAX_BAND_FRAC)))
    S = V[:D, lo:hi]
    L = S.shape[1]
    if L < 8 or D < 8 or start + OUTER_LINES + 6 >= D:
        return None
    C = S.shape[2]
    for o in range(start, min(start + rim, D - OUTER_LINES - 6) + 1):
        outer = S[o:o + OUTER_LINES]
        c = np.median(outer.reshape(-1, C), axis=0)
        dev = np.abs(outer - c).max(axis=2)
        spread = 1.4826 * float(np.median(dev))
        local = float(np.median(np.abs(np.diff(outer, axis=1)).max(axis=2))) * 1.4826 / np.sqrt(2)
        if spread > MAX_OUTER_SPREAD or local > MAX_OUTER_SPREAD:
            continue
        tol = float(np.clip(3.5 * max(spread, local), TOL_MIN, TOL_MAX))
        if o > start and int(((dev <= tol).mean(axis=1) >= CLEAN_F).sum()) < 3:
            continue
        r = _scan_from(S, o, c, tol, D, scale, extra_ramp)
        if r != "short":
            return r
    return None


def _chan_dist_rows(S: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """max over channels of |S[i, j] - ref[i]| for S (n, L, C), ref (n, C)."""
    d = np.abs(S[..., 0] - ref[:, None, 0])
    for ch in range(1, S.shape[-1]):
        np.maximum(d, np.abs(S[..., ch] - ref[:, None, ch]), out=d)
    return d


def _rate_track(ms: np.ndarray, c: np.ndarray, rate: float) -> np.ndarray:
    """y[0] = c; y[i] = y[i-1] + clip(ms[i] - y[i-1], -rate, rate), per channel."""
    n, C = ms.shape
    out = np.empty((n, C), np.float32)
    for ch in range(C):
        y = float(c[ch])
        col = ms[:, ch].tolist()
        o = out[:, ch]
        res = [0.0] * n
        for i, x in enumerate(col):
            dlt = x - y
            if dlt > rate:
                y += rate
            elif dlt < -rate:
                y -= rate
            else:
                y = x
            res[i] = y
        o[:] = res
    return out


def _scan_from(S, o, c, tol, D, scale, extra_ramp) -> Optional[dict]:
    Sx = S[o:]
    n = Sx.shape[0]
    # Reference colour per line: follows a slow drift of the band (scanner
    # light fall-off, vignetting) through a causal running median of the line
    # medians, limited to DRIFT_PER_DIM levels over the image dimension.  A
    # sky ramp changes much faster and still fails the step test below.
    sub = Sx[:, ::max(1, Sx.shape[1] // 16)]
    ms = np.median(sub, axis=1)
    rate = DRIFT_PER_DIM / float(S.shape[0] / MAX_BAND_FRAC)
    c = np.asarray(c, np.float32)
    ref = _rate_track(ms, c, rate)
    dist = _chan_dist_rows(Sx, ref)                      # (D - o, L), rows relative to o
    f = (dist <= tol).mean(axis=1)
    clean = f >= CLEAN_F
    bandish = f >= BAND_F

    i, last_clean = 0, -1
    while i < n:
        if clean[i]:
            last_clean = i
            i += 1
            continue
        if bandish[i]:
            i += 1
            continue
        if last_clean < 0:
            break
        # band resumes (same colour as before the gap, two clean lines): that
        # was a text line.  The colour is frozen at the last clean line so a
        # slowly tracked reference cannot walk into a similar-coloured photo.
        fr = (_chan_dist_rows(Sx[i + 1:], np.repeat(ref[last_clean:last_clean + 1], n - i - 1, axis=0))
              <= tol).mean(axis=1) >= CLEAN_F
        c2 = np.flatnonzero(fr[:-1] & fr[1:])
        if c2.size:
            j = i + 1 + int(c2[0])
            clean[j:j + 2] = True
            i = j
            continue
        break
    # e: first line that is not band at all (relative to o)
    e = i
    lc = last_clean + 1
    if lc < MIN_BAND_PROXY:
        return "short"          # the band never established itself: a later start may work
    if e + o >= D - 5:
        return None

    # --- the boundary must be a step, not a ramp ------------------------------
    # Transition allowance: 2 proxy lines + one JPEG block (8 full-res px) for
    # ringing + what a tilted edge (<= MAX_TILT_DEG) spans over this segment.
    Wr = 2 + int(np.ceil(8 * scale)) + int(extra_ramp)
    if e - lc > Wr:
        return None
    if lc < n:
        refz = ref.copy()
        refz[lc:] = ref[lc - 1]
        dist[lc:] = _chan_dist_rows(Sx[lc:], refz[lc:])
    g = np.minimum(dist, 4 * tol).mean(axis=1)
    band_near = g[max(0, lc - 3):lc].mean()
    p1 = min(max(e + 1, lc + Wr), n - 1)
    p1 = min(p1 + int(np.argmax(g[p1:p1 + 3])), n - 1)   # first photo lines can still ring
    q1 = min(p1 + Wr, n - 1)
    step = float(g[p1] - band_near)
    cont = abs(float(g[q1] - g[p1])) * 1.25
    drift = float(np.median(dist[max(0, lc - 3):lc]) - np.median(dist[:OUTER_LINES]))
    drift = max(0.0, drift)
    need = max(0.8 * tol, 2.0 * cont, 2.0 * drift)
    if step < need or drift > max(MAX_DRIFT, 0.5 * tol):
        return None
    conf = 0.5 * float(np.clip((step / tol - 0.8) / 4.0, 0, 1)) + 0.5 * float(clean[:e].mean())
    return {"depth": e + o, "last_clean": lc + o, "start": o, "color8": np.asarray(c, np.float32),
            "tol": tol, "conf": conf, "step": step}


def _segments(lo: int, hi: int, n: int) -> List[Tuple[int, int]]:
    L = hi - lo
    n = max(1, min(n, L // 8))
    return [(lo + L * k // n, lo + L * (k + 1) // n) for k in range(n)]


def _side_points(V: np.ndarray, lo: int, hi: int, scale: float) -> Tuple[List[dict], int]:
    """Per-segment scans (with nested levels) of one side.  -> (points, n segments)."""
    rim = int(np.ceil(RIM_PX * scale)) + 1
    segs = _segments(lo, hi, N_SEG)
    pts: List[dict] = []
    for k, (a, b) in enumerate(segs):
        if b - a < 8:
            continue
        extra = int(np.ceil((b - a) * TAN_MAX))
        start, r_rim = 0, rim
        for lev in range(MAX_LEVELS):
            r = _scan_side(V, a, b, scale, start=start, extra_ramp=extra, rim=r_rim)
            if r is None:
                break
            r.update(u=(a + b) / 2.0, seg=k, lev=lev)
            pts.append(r)
            start = r["depth"]
            r_rim = rim + extra + 2
    return pts, len(segs)


def _ransac_line(u: np.ndarray, d: np.ndarray, seg: np.ndarray, tol: float, max_slope: float):
    """Robust line d = a + b*u.  -> (a, b, inlier bool mask) or None."""
    n = len(u)
    if n == 0:
        return None
    i, j = np.triu_indices(n, 1)
    ok = (seg[i] != seg[j]) & (u[j] != u[i])
    i, j = i[ok], j[ok]
    b = (d[j] - d[i]) / (u[j] - u[i])
    keep = np.abs(b) <= max_slope
    b = np.concatenate([b[keep], np.zeros(n)])
    a = np.concatenate([d[i[keep]] - b[:keep.sum()] * u[i[keep]], d])
    res = np.abs(d[None, :] - (a[:, None] + b[:, None] * u[None, :]))
    inl = res <= tol
    score = inl.sum(axis=1) - 0.01 * np.where(inl, res, 0).sum(axis=1) / tol
    best = int(np.argmax(score))
    A, B = float(a[best]), float(b[best])
    m = inl[best]
    for _ in range(2):  # least-squares refit on the inliers
        if m.sum() >= 2 and np.ptp(u[m]) > 0:
            B2, A2 = np.polyfit(u[m], d[m], 1)
            B2 = float(np.clip(B2, -max_slope, max_slope))
            A2 = float(np.mean(d[m] - B2 * u[m]))
        else:
            A2, B2 = float(np.mean(d[m])), 0.0
        m2 = np.abs(d - (A2 + B2 * u)) <= tol
        if m2.sum() < m.sum():
            break
        A, B, m = A2, B2, m2
    # one point per segment: keep the closest
    res = np.abs(d - (A + B * u))
    for sg in np.unique(seg[m]):
        idx = np.flatnonzero(m & (seg == sg))
        if idx.size > 1:
            m[idx] = False
            m[idx[np.argmin(res[idx])]] = True
    return A, B, m


def _fit_side(pts: List[dict], nseg: int, span: Tuple[int, int]) -> List[dict]:
    """Straight edges supported by the per-segment scans, outer to inner."""
    lo, hi = span
    if len(pts) < 3 or hi <= lo:
        return []
    u = np.array([p["u"] for p in pts], float)
    d = np.array([p["depth"] for p in pts], float)
    seg = np.array([p["seg"] for p in pts], int)
    segw = (hi - lo) / max(1, nseg)
    weak = max(3, int(np.ceil(0.35 * nseg)))
    remaining = np.ones(len(pts), bool)
    lines = []
    for _ in range(MAX_LEVELS + 1):
        idx = np.flatnonzero(remaining)
        if idx.size < weak:
            break
        fit = _ransac_line(u[idx], d[idx], seg[idx], 2.0, TAN_MAX * 1.15)
        if fit is None:
            break
        a, b, m = fit
        inl = idx[m]
        support = len(np.unique(seg[inl]))
        if support < weak:
            break
        remaining[inl] = False
        cover = float((u[inl].max() - u[inl].min() + segw) / (hi - lo))
        sel = [pts[q] for q in inl]
        lines.append({"a": a, "b": b, "support": support, "cover": cover,
                      "strong": support >= max(3, int(np.ceil(0.6 * nseg))) and cover >= 0.6,
                      "color8": np.median(np.stack([p["color8"] for p in sel]), axis=0),
                      "tol": float(np.median([p["tol"] for p in sel])),
                      "conf": float(np.mean([p["conf"] for p in sel])) * min(1.0, support / (0.8 * nseg)),
                      "pts": sel, "span": (lo, hi), "nseg": nseg})
    mid = (lo + hi) / 2.0
    lines.sort(key=lambda ln: ln["a"] + ln["b"] * mid)
    return lines


def _line_max(ln: Optional[dict]) -> float:
    if not ln:
        return 0.0
    lo, hi = ln["span"]
    return max(ln["a"] + ln["b"] * lo, ln["a"] + ln["b"] * hi)


def _side_phi(k: str, b: float) -> float:
    """Tilt (deg, + = counter-clockwise as displayed) implied by a side's slope."""
    t = float(np.degrees(np.arctan(b)))
    return -t if k in ("top", "right") else t


def _phi_slope(k: str, phi: float) -> float:
    t = float(np.tan(np.radians(phi)))
    return -t if k in ("top", "right") else t


def _choose_lines(cands: Dict[str, List[dict]], dim_d: Dict[str, int]) -> Dict[str, Optional[dict]]:
    """Pick each side's photo edge among its candidate lines.

    Default: the outermost strong line.  An inner line (paper inside a scanner
    lid margin, a deckled rim, dark scan-background corners) replaces it when
    the outer level is thin and the inner level's colour matches a band level
    found on another side (a frame), which a photo's own content almost never
    does."""
    chosen: Dict[str, Optional[dict]] = {}
    nested = False
    for k in SIDES:
        strong = [ln for ln in cands[k] if ln["strong"]]
        if not strong:
            chosen[k] = None
            continue
        pick = strong[0]
        others = [ln for q in SIDES if q != k for ln in cands[q] if ln["strong"]]
        for inner in reversed(strong[1:]):
            outer = strong[0]
            if _line_max(outer) > 0.12 * dim_d[k]:
                break
            ci = inner["color8"]
            thr = max(8.0, 2.0 * inner["tol"])
            if any(float(np.abs(ci - o["color8"]).max()) <= thr for o in others):
                pick = inner
                nested = True
                break
        pick = dict(pick)
        pick["nested"] = pick is not strong[0] and nested
        chosen[k] = pick
    # weak sides: accepted when they agree with the tilt of >= 2 strong sides
    phis = [_side_phi(k, chosen[k]["b"]) for k in SIDES if chosen[k]]
    if len(phis) >= 2:
        phi = float(np.median(phis))
        for k in SIDES:
            if chosen[k] is None and cands[k]:
                for ln in cands[k]:
                    if abs(_side_phi(k, ln["b"]) - phi) <= 0.3 and ln["cover"] >= 0.4:
                        chosen[k] = dict(ln)
                        chosen[k]["nested"] = False
                        break
    return chosen


def _refine_seg(col: np.ndarray, unit: float, side: str, e: float, lc: float, s: float,
                span: Tuple[int, int], full_len: int, ramp: int = 0) -> Optional[Tuple[float, float]]:
    """Continuous full-res edge position (first photo line = round(p)) and its
    10-90 % width over a short span.  ``e``: proxy edge estimate, ``lc``: one
    past the last clean proxy line, ``s``: proxy scale along depth.

    Reference colour: full-res lines under the last clean proxy lines.  Within a
    window around the proxy edge (widened by one JPEG block for ringing), k0 is
    the first line whose jump in mean distance is >= half the window's largest
    jump; the edge is the half-level crossing of the (unclipped) mean distance
    after it, interpolated between lines (exact for flat digital bands)."""
    lo, hi = span
    if hi - lo < 4:
        return None
    ref_a = max(0, int(np.floor((lc - 3) / s)))
    ref_b = max(ref_a + 1, int(np.floor(lc / s)))
    win_a = max(1, min(ref_b, int(np.floor((e - 3) / s))))
    win_b = min(full_len, int(np.ceil((e + 3) / s)) + 9 + ramp)
    if win_b - win_a < 3:
        return None
    ref = _as8f(_side_lines(col, side, ref_a, ref_b, lo, hi), unit)
    C = ref.shape[2]
    c = np.median(ref.reshape(-1, C), axis=0)
    spread = 1.4826 * float(np.median(np.abs(ref - c).max(axis=2)))
    tol = float(np.clip(3.5 * spread, TOL_MIN, 40.0))
    win = _as8f(_side_lines(col, side, win_a - 1, win_b, lo, hi), unit)
    dist = _chan_dist(np.ascontiguousarray(win), c)
    g = np.minimum(dist, 64.0).mean(axis=1)
    jump = np.diff(g)                       # jump[k]: line win_a-1+k -> win_a+k
    if jump.size == 0:
        return None
    f = (dist[1:] <= tol).mean(axis=1)
    jmax = float(jump.max())
    if jmax < 1.0:
        return None
    cand = np.flatnonzero((jump >= 0.5 * jmax) & (f < CLEAN_F))
    if not cand.size:
        return None
    k0 = int(cand[0])                      # gr index k0+1 is the first photo-ish line
    gr = dist.mean(axis=1)
    g_lo = float(np.median(gr[max(0, k0 - 3):k0 + 1]))
    look = gr[k0 + 1:k0 + 6 + ramp]
    g_hi = float(look.max())
    if g_hi - g_lo < 1.0:
        return None
    half = 0.5 * (g_lo + g_hi)
    j = k0 + 1 + int(np.flatnonzero(look >= half)[0])
    prev, cur = float(gr[j - 1]), float(gr[j])
    t = float(np.clip((half - prev) / (cur - prev), 0.0, 1.0)) if cur > prev else 1.0
    p = (win_a - 1) + (j - 1) + 0.5 + t
    # 10-90 % width
    seq = (gr[max(0, k0 - 2):k0 + 6 + ramp] - g_lo) / (g_hi - g_lo)

    def cross(level, start=0):
        for q in range(max(1, start), len(seq)):
            if seq[q] >= level:
                a0, a1 = seq[q - 1], seq[q]
                return q - 1 + (float(np.clip((level - a0) / (a1 - a0), 0, 1)) if a1 > a0 else 1.0)
        return None

    x10 = cross(0.1)
    x90 = cross(0.9, int(x10) if x10 is not None else 0)
    width = (x90 - x10) if (x10 is not None and x90 is not None) else float("nan")
    return p, width


def _refine_side(col, unit, k, ln, s_d, s_u, full_len, full_along):
    """Full-res edge line of side ``k`` from its proxy line ``ln``.
    -> (a, b, widths, n points) in full-res units (depth = a + b * along)."""
    lo, hi = ln["span"]
    by_seg = {p["seg"]: p for p in ln["pts"]}
    segs = _segments(lo, hi, N_SEG)
    b_full = ln["b"] * s_u / s_d
    U, P, Wd = [], [], []
    for q, (a0, a1) in enumerate(segs):
        uc = (a0 + a1) / 2.0
        e = ln["a"] + ln["b"] * uc
        lc = by_seg[q]["last_clean"] if q in by_seg else e - 1.0
        if q in by_seg and abs(by_seg[q]["depth"] - e) > 2.5:
            lc = e - 1.0
        Uc = uc / s_u
        Lr = min(REFINE_SEG_PX, max(8.0, (a1 - a0) / s_u))
        sp = (max(0, int(round(Uc - Lr / 2))), min(full_along, int(round(Uc + Lr / 2))))
        ramp = int(np.ceil(Lr * abs(b_full))) + 1
        r = _refine_seg(col, unit, k, e, lc, s_d, sp, full_len, ramp)
        if r is None:
            continue
        U.append((sp[0] + sp[1]) / 2.0)
        P.append(r[0])
        Wd.append(r[1])
    fallback = (ln["a"] / s_d, b_full, [], 0)
    if len(U) < 3:
        return fallback
    U, P = np.array(U), np.array(P)
    fit = _ransac_line(U, P, np.arange(len(U)), 1.5, TAN_MAX * 1.15)
    if fit is None or fit[2].sum() < max(3, 0.4 * len(U)):
        return fallback
    a, b, m = fit
    span_len = float(np.ptp(U[m])) if m.sum() > 1 else 0.0
    if abs(b) * max(span_len, 1.0) < 0.75:   # effectively straight: exact integer edges stay exact
        b = 0.0
        a = float(np.median(P[m]))
    widths = [w for w, mm in zip(Wd, m) if mm and np.isfinite(w)]
    return a, b, widths, int(m.sum())


def _corners(lines: Dict[str, Tuple[float, float]], W: int, H: int):
    """Photo quad (TL, TR, BR, BL) from side lines in full-res units.
    top: y = a + b x; bottom: y = H - (a + b x); left: x = a + b y; right: x = W - (a + b y)."""
    at, bt = lines.get("top", (0.0, 0.0))
    ab, bb = lines.get("bottom", (0.0, 0.0))
    al, bl = lines.get("left", (0.0, 0.0))
    ar, br = lines.get("right", (0.0, 0.0))

    def hv(ya, yb, xa, xb):
        # y = ya + yb*x ; x = xa + xb*y
        x = (xa + xb * ya) / (1.0 - xb * yb)
        return x, ya + yb * x

    tl = hv(at, bt, al, bl)
    tr = hv(at, bt, W - ar, -br)
    br_ = hv(H - ab, -bb, W - ar, -br)
    bl_ = hv(H - ab, -bb, al, bl)
    return (tl, tr, br_, bl_)


def _quad_mask(quad, x0: int, y0: int, w: int, h: int, grow: float = 0.0) -> np.ndarray:
    """Bool (h, w) mask of the convex quad (pixel-edge coordinates), grown outward
    by ``grow`` px, for the crop at (x0, y0): a pixel is in when its centre is
    within ``grow`` of the inside of every edge."""
    w, h = max(0, int(w)), max(0, int(h))
    if w == 0 or h == 0:
        return np.zeros((h, w), bool)
    q = np.asarray(quad, np.float64)
    c = q.mean(axis=0)
    X = (np.arange(w, dtype=np.float32) + np.float32(x0 + 0.5))[None, :]
    Y = (np.arange(h, dtype=np.float32) + np.float32(y0 + 0.5))[:, None]
    m = np.ones((h, w), bool)
    for i in range(4):
        p1, p2 = q[i], q[(i + 1) % 4]
        dx, dy = p2 - p1
        L = float(np.hypot(dx, dy))
        if L < 1e-9:
            continue
        nx, ny = dy / L, -dx / L                       # unit normal
        if nx * (c[0] - p1[0]) + ny * (c[1] - p1[1]) < 0:
            nx, ny = -nx, -ny                          # pointing inward
        off = np.float32(-(nx * p1[0] + ny * p1[1]) + grow)
        m &= (np.float32(nx) * X + np.float32(ny) * Y + off) >= 0
    return m


def _robust_noise(block8: np.ndarray) -> float:
    """Robust grain std (8-bit units) of a band crop; text edges are outliers.

    The larger of a pixel-scale high-pass estimate and a 4x4-block estimate
    (scaled back to per-pixel white noise): JPEG removes the fine grain of a
    scanned paper but keeps its block-scale variation, which a flat digital
    fill does not have."""
    if block8.shape[0] < 6 or block8.shape[1] < 6:
        return 0.0
    hp, lp = [], []
    for ch in range(block8.shape[2]):
        x = np.ascontiguousarray(block8[:, :, ch])
        r = x - cv2.blur(x, (5, 5))
        hp.append(1.4826 * float(np.median(np.abs(r[2:-2, 2:-2]))) / 0.98)
        h4, w4 = x.shape[0] // 4, x.shape[1] // 4
        if h4 >= 6 and w4 >= 6:
            sm = cv2.resize(x[:h4 * 4, :w4 * 4], (w4, h4), interpolation=cv2.INTER_AREA)
            r4 = sm - cv2.blur(sm, (5, 5))
            lp.append(4.0 * 1.4826 * float(np.median(np.abs(r4[2:-2, 2:-2]))) / 0.98)
    n_hp = float(np.mean(hp))
    n_lp = float(np.mean(lp)) if lp else 0.0
    return max(n_hp, n_lp)


def _trend8(blk8: np.ndarray, along_axis: int) -> float:
    """Linear colour trend (8-bit, max over channels) along a band: 8 chunk medians."""
    n = blk8.shape[along_axis]
    if n < 32:
        return 0.0
    k = 8
    meds, pos = [], []
    for i in range(k):
        a, b = n * i // k, n * (i + 1) // k
        chunk = blk8[:, a:b] if along_axis == 1 else blk8[a:b]
        meds.append(np.median(chunk.reshape(-1, chunk.shape[2]), axis=0))
        pos.append((a + b) / 2.0)
    meds = np.array(meds)
    pos = np.array(pos)
    tr = 0.0
    for ch in range(meds.shape[1]):
        slope = np.polyfit(pos, meds[:, ch], 1)[0]
        tr = max(tr, abs(float(slope)) * n)
    return tr


def _band_stats(arr: np.ndarray, depth: Dict[str, int], phi: float = 0.0, nested=()):
    """Per-side band rects, colours, grain and colour drift for given band depths.
    -> (bands, colour (array units), noise, drift, trends) or None."""
    H, W = arr.shape[:2]
    unit = _unit(arr)
    nc = _ncolor(arr)
    col = arr[:, :, :nc]
    sliver = int(np.ceil(max(W, H) * abs(np.tan(np.radians(phi))))) + 2 if phi else 0
    bands, samples, noises, weights, trends = [], [], [], [], []
    for k in SIDES:
        if not depth[k]:
            continue
        rect = _rect_of_band(k, depth, W, H)
        bx, by, bw, bh = rect
        if bw <= 0 or bh <= 0:
            continue
        # statistics away from the photo edge (tilt sliver, blur) and the outer rim
        dk = depth[k]
        cut_in = min(dk // 3, sliver + 3)
        cut_out = min(dk // 3, int(RIM_PX + 2) if (k in nested or phi) else 0)
        if k == "top":
            iy0, iy1, ix0, ix1 = by + cut_out, by + bh - cut_in, bx, bx + bw
        elif k == "bottom":
            iy0, iy1, ix0, ix1 = by + cut_in, by + bh - cut_out, bx, bx + bw
        elif k == "left":
            iy0, iy1, ix0, ix1 = by, by + bh, bx + cut_out, bx + bw - cut_in
        else:
            iy0, iy1, ix0, ix1 = by, by + bh, bx + cut_in, bx + bw - cut_out
        blk = col[iy0:iy1, ix0:ix1]
        if blk.size == 0:
            blk = col[by:by + bh, bx:bx + bw]
            iy0, iy1, ix0, ix1 = by, by + bh, bx, bx + bw
        stride = max(1, int(np.sqrt(blk.shape[0] * blk.shape[1] / 250_000.0)))
        smp = blk[::stride, ::stride].reshape(-1, nc)
        colr = np.median(smp, axis=0)
        if np.issubdtype(arr.dtype, np.integer):
            colr = np.round(colr)
        samples.append(smp)
        # noise on a central crop (<= 768 px each way) at full resolution
        cy, cx = (iy0 + iy1) // 2, (ix0 + ix1) // 2
        hh, hw = min(iy1 - iy0, 768) // 2, min(ix1 - ix0, 768) // 2
        crop = _as8f(col[cy - hh:cy + hh, cx - hw:cx + hw], unit)
        noises.append(_robust_noise(crop))
        weights.append(bw * bh)
        # lighting / colour trend along the band (strided for speed)
        st2 = max(1, int(np.sqrt(blk.shape[0] * blk.shape[1] / 400_000.0)))
        trends.append(_trend8(_as8f(blk[::st2, ::st2], unit), 1 if k in ("top", "bottom") else 0))
        bands.append({"side": k, "rect": tuple(int(v) for v in rect),
                      "color": tuple(float(v) for v in colr)})
    if not bands:
        return None
    allsmp = np.concatenate(samples, axis=0)
    bc = np.median(allsmp, axis=0)
    if np.issubdtype(arr.dtype, np.integer):
        bc = np.round(bc)
    noise = float(np.average(noises, weights=weights))
    side_cols = np.array([np.asarray(b["color"], float) / unit for b in bands])
    disagree = float((side_cols.max(axis=0) - side_cols.min(axis=0)).max()) if len(bands) > 1 else 0.0
    drift = max(disagree, max(trends) if trends else 0.0)
    return bands, bc, noise, drift, trends


def band_from_rect(arr: np.ndarray, rect, like: Optional[BandResult] = None) -> BandResult:
    """A BandResult for a known photo rect (e.g. from the hidden marker): the
    bands around it with their measured colours, grain and drift."""
    arr = _as3d(np.asarray(arr))
    H, W = arr.shape[:2]
    x, y, w, h = (int(v) for v in rect)
    x, y = max(0, x), max(0, y)
    w, h = min(w, W - x), min(h, H - y)
    depth = {"top": y, "bottom": H - y - h, "left": x, "right": W - x - w}
    st = _band_stats(arr, depth)
    if st is None:
        return BandResult(False, (0, 0, W, H), [], (), "", False, 0.0, 0.0)
    bands, bc, noise, drift, _tr = st
    unit = _unit(arr)
    band_color = tuple(int(v) if np.issubdtype(arr.dtype, np.integer) else float(v) for v in bc)
    return BandResult(True, (x, y, w, h), bands, band_color, _hex8(np.asarray(bc, float) / unit),
                      bool(noise >= FLAT_NOISE or drift > FLAT_DRIFT), noise,
                      float(like.confidence) if like is not None and like.found else 0.0,
                      edge_blur=float(like.edge_blur) if like is not None else 0.0, color_drift=round(drift, 2))


def detect_band(arr: np.ndarray, proxy_long_edge: int = 1600) -> BandResult:
    """Detect caption/border bands on up to four sides.

    ``photo_rect`` is exact to the pixel for flat digital bands and typically
    within 1-2 px for scanned paper.  Never mistakes a smooth light gradient
    (sky) at the photo's own edge for a band: the band/photo boundary must be a
    step in colour, not a ramp.

    Each side is scanned in ``N_SEG`` segments (so a tilted edge is a short
    step in every segment), with up to ``MAX_LEVELS`` nested levels per
    segment (scanner lid margin -> paper -> photo) and a skipped rim of up to
    ``RIM_PX`` px (deckled edges, dark scan-background corners).  A robust line
    through the segment edges gives each side's edge; tilts up to
    ``MAX_TILT_DEG`` are accepted.  The reported ``photo_rect`` is the largest
    axis-aligned rectangle of photo pixels inside the (tilted) photo outline.
    """
    arr = _as3d(np.asarray(arr))
    H, W = arr.shape[:2]
    unit = _unit(arr)
    nc = _ncolor(arr)
    col = arr[:, :, :nc]
    whole = (0, 0, W, H)
    none = BandResult(False, whole, [], (), "", False, 0.0, 0.0)
    if H < 16 or W < 16:
        return none

    s = min(1.0, float(proxy_long_edge) / max(H, W))
    small = _resize_area(arr, s) if s < 1.0 else arr
    P = _as8f(small[:, :, :nc], unit)
    ph, pw = P.shape[:2]
    sy, sx = ph / H, pw / W

    views = {k: np.ascontiguousarray(_oriented(P, k)) for k in SIDES}
    s_d = {"top": sy, "bottom": sy, "left": sx, "right": sx}      # proxy scale along depth
    s_u = {"top": sx, "bottom": sx, "left": sy, "right": sy}      # ... and along the side
    alen = {"top": pw, "bottom": pw, "left": ph, "right": ph}
    dim_d = {"top": ph, "bottom": ph, "left": pw, "right": pw}
    perp = {"top": ("left", "right"), "bottom": ("left", "right"),
            "left": ("top", "bottom"), "right": ("top", "bottom")}

    # pass 1: full spans; pass 2: spans restricted to the photo found in pass 1
    chosen: Dict[str, Optional[dict]] = {k: None for k in SIDES}
    for _pass in range(2):
        spans = {}
        for k in SIDES:
            a_s, b_s = perp[k]
            m = 2 if _pass else 0
            lo = int(np.ceil(_line_max(chosen[a_s]))) + m if chosen[a_s] else 0
            hi = alen[k] - (int(np.ceil(_line_max(chosen[b_s]))) + m if chosen[b_s] else 0)
            spans[k] = (lo, hi)
        cands = {}
        for k in SIDES:
            pts, nseg = _side_points(views[k], *spans[k], s_d[k])
            cands[k] = _fit_side(pts, nseg, spans[k])
        chosen = _choose_lines(cands, dim_d)

    if not any(chosen.values()):
        return none
    dp = {k: _line_max(chosen[k]) for k in SIDES}
    if pw - dp["left"] - dp["right"] < MIN_PHOTO_FRAC * pw or ph - dp["top"] - dp["bottom"] < MIN_PHOTO_FRAC * ph:
        return none

    # --- full resolution edges ---------------------------------------------------
    full_len = {"top": H, "bottom": H, "left": W, "right": W}
    full_along = {"top": W, "bottom": W, "left": H, "right": H}
    fl: Dict[str, tuple] = {}
    for k in SIDES:
        if chosen[k]:
            fl[k] = _refine_side(col, unit, k, chosen[k], s_d[k], s_u[k], full_len[k], full_along[k])
    # consensus tilt: a scanned print is a rigid body
    phis = [(_side_phi(k, fl[k][1]), chosen[k]["support"]) for k in fl]
    phi = float(np.median([p for p, _ in phis])) if phis else 0.0
    if len(fl) >= 2:
        for k in list(fl):
            if abs(_side_phi(k, fl[k][1]) - phi) > 0.3:
                bfix = _phi_slope(k, phi)
                a, b, wds, n = fl[k]
                # re-anchor the intercept at the span centre so the fixed slope pivots there
                lo, hi = chosen[k]["span"]
                uc = (lo + hi) / 2.0 / s_u[k]
                fl[k] = (a + b * uc - bfix * uc, bfix, wds, n)
    if all(fl[k][1] == 0.0 for k in fl):
        phi = 0.0
    lines = {k: (fl[k][0], fl[k][1]) for k in fl}
    quad = _corners(lines, W, H)
    (tlx, tly), (trx, try_), (brx, bry), (blx, bly) = quad
    fx0, fx1 = max(tlx, blx, 0.0), min(trx, brx, float(W))
    fy0, fy1 = max(tly, try_, 0.0), min(bly, bry, float(H))
    x0 = int(np.ceil(fx0 - 0.25)); y0 = int(np.ceil(fy0 - 0.25))
    x1 = int(np.floor(fx1 + 0.25)); y1 = int(np.floor(fy1 + 0.25))
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 - x0 < MIN_PHOTO_FRAC * W or y1 - y0 < MIN_PHOTO_FRAC * H:
        return none
    photo = (x0, y0, x1 - x0, y1 - y0)
    depth = {"top": y0 if "top" in fl else 0, "bottom": H - y1 if "bottom" in fl else 0,
             "left": x0 if "left" in fl else 0, "right": W - x1 if "right" in fl else 0}
    if any(depth[k] <= 0 for k in fl):
        depth = {k: max(0, v) for k, v in depth.items()}
    widths = [w for k in fl for w in fl[k][2]]
    blur = 0.0
    if widths:
        wmed = float(np.median(widths))
        tilt_w = 0.8 * REFINE_SEG_PX * abs(np.tan(np.radians(phi)))
        blur = float(np.sqrt(max(0.0, wmed ** 2 - tilt_w ** 2)))
    st = _band_stats(arr, depth, phi, {k for k in fl if chosen[k].get("nested")})
    if st is None:
        return none
    bands, bc, noise, drift, trends = st
    conf = float(np.mean([chosen[b["side"]]["conf"] for b in bands]))
    if len(bands) == 1:  # one plain strip with a colour trend looks like a wall, not a band
        conf *= float(np.clip(1.0 - max(trends) / 20.0, 0.5, 1.0))
    textured = noise >= FLAT_NOISE or drift > FLAT_DRIFT
    band_color = tuple(int(v) if np.issubdtype(arr.dtype, np.integer) else float(v) for v in bc)
    return BandResult(True, tuple(int(v) for v in photo), bands, band_color,
                      _hex8(np.asarray(bc, float) / unit), bool(textured), noise,
                      float(np.clip(conf, 0, 1)), angle=round(phi, 3),
                      photo_quad=tuple((float(x), float(y)) for x, y in quad),
                      edge_blur=round(blur, 2), color_drift=round(drift, 2),
                      nested=any(chosen[k].get("nested") for k in fl))


# =============================================================================
# text finding
# =============================================================================

@dataclass
class TextLine:
    box: tuple                 # (x, y, w, h) full-res
    text: str = ""
    confidence: float = 0.0    # 0..1
    words: list = field(default_factory=list)   # [{"text", "confidence", "box"}]
    # detection internals (not serialised): the line's own ink (x0, y0, bool mask,
    # full res), its median contrast to the paper and its ink colour (8-bit)
    ink: Optional[tuple] = field(default=None, repr=False, compare=False)
    contrast: float = field(default=0.0, repr=False, compare=False)
    ink8: tuple = field(default=(), repr=False, compare=False)

    def to_json(self) -> dict:
        return _jsonable({"box": [int(v) for v in self.box], "text": self.text,
                          "confidence": round(float(self.confidence), 3),
                          "words": [{"text": w.get("text", ""),
                                     "confidence": round(float(w.get("confidence", 0.0)), 3),
                                     "box": [int(v) for v in w.get("box", (0, 0, 0, 0))]}
                                    for w in self.words]})


@dataclass
class TextBlock:
    box: tuple
    lines: list                # [TextLine]
    role: str = "caption"      # "caption" | "other" (paper backprint, logo: never used as caption text)

    def to_json(self) -> dict:
        return {"box": [int(v) for v in self.box], "lines": [ln.to_json() for ln in self.lines],
                "role": self.role}


@dataclass
class StyleEstimate:
    font_size_px: float
    cap_height_px: float
    align: str                 # left|center|right
    color_hex: str
    line_height: float         # multiplier estimate

    def to_json(self) -> dict:
        return {"font_size_px": round(float(self.font_size_px), 2),
                "cap_height_px": round(float(self.cap_height_px), 2),
                "align": self.align, "color_hex": self.color_hex,
                "line_height": round(float(self.line_height), 3)}


def _band_color8(arr: np.ndarray, band: BandResult, bd: dict) -> np.ndarray:
    unit = _unit(arr)
    colr = bd.get("color") or band.band_color
    c = np.asarray(colr, np.float32) / np.float32(unit)
    nc = _ncolor(arr)
    if c.size != nc:
        c = np.resize(c, nc) if c.size else np.zeros(nc, np.float32)
    return c


def _bg_map(crop8: np.ndarray) -> np.ndarray:
    """Smooth local paper colour: heavy downscale, median, upscale.  Removes ink strokes."""
    h, w = crop8.shape[:2]
    f = max(1, int(round(min(h, w) / 24.0)))  # ~24 cells across the short side
    f = min(f, 8)
    small = cv2.resize(crop8, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA)
    small = np.ascontiguousarray(small)
    if small.ndim == 2:
        small = small[:, :, None]
    k = 5 if min(small.shape[:2]) >= 5 else 3 if min(small.shape[:2]) >= 3 else 1
    if k > 1:
        med = np.stack([cv2.medianBlur(np.ascontiguousarray(small[:, :, ch]), k)
                        for ch in range(small.shape[2])], axis=2).astype(np.float32)
        # a second pass removes large ink blobs (bold handwriting)
        med = np.stack([cv2.medianBlur(np.ascontiguousarray(med[:, :, ch]), k)
                        for ch in range(med.shape[2])], axis=2)
    else:
        med = small.astype(np.float32)
    up = cv2.resize(med, (w, h), interpolation=cv2.INTER_LINEAR)
    return up[:, :, None] if up.ndim == 2 else up


def _text_thresholds(noise: float) -> Tuple[float, float]:
    """(high, low) contrast thresholds in 8-bit units."""
    high = max(20.0, 6.0 * noise + 10.0)
    low = max(3.0, 3.5 * noise + 3.0)
    return high, low


def _dist_map(crop8: np.ndarray, textured: bool, color8: np.ndarray) -> np.ndarray:
    bg = _bg_map(crop8) if textured else color8.reshape(1, 1, -1)
    return np.abs(crop8 - bg).max(axis=2)


# --- photo outline / exclusion strips --------------------------------------------

def _photo_quad(band: BandResult):
    """The photo outline to protect: the detected (tilted) quad when it still
    matches ``photo_rect`` (the user may have moved the edge), else the rect."""
    x, y, w, h = (float(v) for v in band.photo_rect)
    rect_q = ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    q = band.photo_quad
    if not q or len(q) != 4:
        return rect_q
    tol = max(4.0, 0.045 * max(w, h))
    if all(abs(qa - ra) <= tol and abs(qb - rb) <= tol for (qa, qb), (ra, rb) in zip(q, rect_q)):
        return tuple((float(a), float(b)) for a, b in q)
    return rect_q


def _edge_strip(band: BandResult) -> int:
    """Width of the strip along the photo edge kept out of text finding: the
    blurred (scanned) photo edge produces ghost 'text' there."""
    return int(np.ceil(max(3.0, 2.0 * float(band.edge_blur or 0.0))))


def _photo_masks(band: BandResult, x0: int, y0: int, w: int, h: int):
    """(hard, strip) bool masks for the crop: photo pixels (never touched) and
    the photo-edge strip around them."""
    q = _photo_quad(band)
    hard = _quad_mask(q, x0, y0, w, h, grow=0.25)
    strip = _quad_mask(q, x0, y0, w, h, grow=float(_edge_strip(band))) & ~hard
    return hard, strip


def _group_lines(boxes: np.ndarray) -> List[List[int]]:
    """Group component boxes (N x 4: x, y, w, h) into lines.

    1. Primary components (>= 35 % of the typical height) linked by union-find:
       vertical overlap, gap <= 1.5 x height, similar heights.
    2. Groups on the same row merge across a gap <= 3 x-heights (a caption split
       around punctuation: "SUMMER 1978 - LAKE MERCED").  A wider gap (up to 6
       x-heights) still merges when small marks inside it (a hyphen, dash, dot)
       bridge it into pieces of <= 3 x-heights each: in a monospaced / typewriter
       face " - " is three full cells wide.
    3. A group nested in / mostly overlapping another merges into it
       (apostrophes, i-dots, accents grouped on their own).
    3b. A tiny group just below / above a line, within its x-extent, joins it
       (a comma tail under a line without descenders).
    4. Small components (dots, commas, hyphens, accents) attach only within the
       line's x-extent (+ a little for trailing punctuation) and close to it
       vertically, so dust specks do not stretch line boxes."""
    n = len(boxes)
    if n == 0:
        return []
    x, y, w, h = (boxes[:, i].astype(np.float64) for i in range(4))
    href = float(np.percentile(h, 75))
    primary = h >= 0.35 * href
    pidx = np.flatnonzero(primary)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    if pidx.size:
        px, py, pw_, ph_ = x[pidx], y[pidx], w[pidx], h[pidx]
        ov = np.minimum(py[:, None] + ph_[:, None], py[None] + ph_[None]) - np.maximum(py[:, None], py[None])
        minh = np.minimum(ph_[:, None], ph_[None])
        maxh = np.maximum(ph_[:, None], ph_[None])
        gap = np.maximum(px[:, None], px[None]) - np.minimum(px[:, None] + pw_[:, None], px[None] + pw_[None])
        link = (ov >= 0.5 * minh) & (gap <= 1.5 * maxh) & (maxh <= 2.5 * minh + 2)
        ii, jj = np.nonzero(np.triu(link, 1))
        for a, b in zip(ii, jj):
            ra, rb = find(int(pidx[a])), find(int(pidx[b]))
            if ra != rb:
                parent[rb] = ra
    groups: Dict[int, List[int]] = {}
    for i in pidx:
        groups.setdefault(find(int(i)), []).append(int(i))
    lines = list(groups.values())

    def gb(g):
        return (min(x[m] for m in g), min(y[m] for m in g),
                max(x[m] + w[m] for m in g), max(y[m] + h[m] for m in g),
                float(np.median([h[m] for m in g])))

    sidx = np.flatnonzero(~primary)

    def bridged_gap(lo, hi, top, bot):
        """The widest piece of the gap lo..hi left once small marks lying in it,
        vertically inside top..bot, are counted as ink."""
        if sidx.size == 0:
            return hi - lo
        sx0, sx1 = x[sidx], x[sidx] + w[sidx]
        scy = y[sidx] + h[sidx] / 2
        inside = (sx0 >= lo) & (sx1 <= hi) & (scy >= top) & (scy <= bot)
        widest, edge = 0.0, lo
        for k in np.argsort(sx0[inside]):
            a0, a1 = sx0[inside][k], sx1[inside][k]
            widest = max(widest, a0 - edge)
            edge = max(edge, a1)
        return max(widest, hi - edge)

    merged = True
    while merged and len(lines) > 1:
        merged = False
        boxes_g = [gb(g) for g in lines]
        for a in range(len(lines)):
            for b in range(a + 1, len(lines)):
                ax0, ay0, ax1, ay1, ah = boxes_g[a]
                bx0, by0, bx1, by1, bh = boxes_g[b]
                ha, hb = ay1 - ay0, by1 - by0
                ov = min(ay1, by1) - max(ay0, by0)
                gap = max(ax0, bx0) - min(ax1, bx1)
                xh = 0.75 * min(ah, bh)
                row_like = ov >= 0.5 * min(ha, hb) and max(ah, bh) <= 2.0 * min(ah, bh) + 2
                same_row = row_like and (gap <= 3.0 * xh or (
                    gap <= 6.0 * xh
                    and bridged_gap(min(ax1, bx1), max(ax0, bx0), max(ay0, by0), min(ay1, by1)) <= 3.0 * xh))
                iw = min(ax1, bx1) - max(ax0, bx0)
                inter = max(0.0, iw) * max(0.0, ov)
                small_area = min((ax1 - ax0) * ha, (bx1 - bx0) * hb)
                if (ax1 - ax0) * ha <= (bx1 - bx0) * hb:
                    cx, cy, big = (ax0 + ax1) / 2, (ay0 + ay1) / 2, (bx0, by0, bx1, by1)
                else:
                    cx, cy, big = (bx0 + bx1) / 2, (by0 + by1) / 2, (ax0, ay0, ax1, ay1)
                nested = (small_area > 0 and inter >= 0.5 * small_area) or \
                    (big[0] <= cx <= big[2] and big[1] <= cy <= big[3])
                if same_row or nested:
                    lines[a] = lines[a] + lines[b]
                    del lines[b]
                    merged = True
                    break
            if merged:
                break

    # a fragment split off a line: a comma tail hanging below a descender-free
    # line, an accent / apostrophe just above.  Tiny relative to the line, within
    # its x-extent, touching or nearly touching it; joins the nearest such line.
    merged = True
    while merged and len(lines) > 1:
        merged = False
        bb = np.array([gb(g)[:4] for g in lines])
        gh, gw = bb[:, 3] - bb[:, 1], bb[:, 2] - bb[:, 0]
        cx = (bb[:, 0] + bb[:, 2]) / 2
        vgap = np.maximum(bb[:, None, 1], bb[None, :, 1]) - np.minimum(bb[:, None, 3], bb[None, :, 3])
        frag = ((gh[:, None] <= 0.5 * gh[None]) & (gw[:, None] <= 0.8 * gh[None])
                & (cx[:, None] >= bb[None, :, 0]) & (cx[:, None] <= bb[None, :, 2])
                & (vgap <= 0.2 * gh[None]))
        np.fill_diagonal(frag, False)
        for s in np.flatnonzero(frag.any(axis=1)):
            tgt = np.flatnonzero(frag[s])
            b = int(tgt[np.argmin(vgap[s, tgt])])
            lines[b] = lines[b] + lines[s]
            del lines[s]
            merged = True
            break

    # attach small components (dots, commas, hyphens, accents); line extents come
    # from the primary members only, so attached specks cannot chain-grow a line
    ext = [gb(members) for members in lines]
    for i in np.flatnonzero(~primary):
        cx, cy = x[i] + w[i] / 2, y[i] + h[i] / 2
        best, bestd = None, None
        for li, (lx0, ly0, lx1, ly1, lmed) in enumerate(ext):
            lh = ly1 - ly0
            if lx0 - 0.15 * lh <= cx <= lx1 + 0.6 * lh and ly0 - 0.3 * lh <= cy <= ly1 + 0.3 * lh:
                d = abs(cy - (ly0 + ly1) / 2)
                if bestd is None or d < bestd:
                    best, bestd = li, d
        if best is not None:
            lines[best].append(int(i))
    return lines


def _union_box(boxes: Sequence[Sequence[float]]) -> Tuple[int, int, int, int]:
    x0 = min(b[0] for b in boxes); y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes); y1 = max(b[1] + b[3] for b in boxes)
    return (int(x0), int(y0), int(x1 - x0), int(y1 - y0))


def _group_blocks(lines: List[TextLine]) -> List[TextBlock]:
    if not lines:
        return []
    lines = sorted(lines, key=lambda ln: (ln.box[1], ln.box[0]))
    n = len(lines)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        xi, yi, wi, hi = lines[i].box
        for j in range(i + 1, n):
            xj, yj, wj, hj = lines[j].box
            vgap = max(yi, yj) - min(yi + hi, yj + hj)
            hgap = max(xi, xj) - min(xi + wi, xj + wj)
            mh = max(hi, hj)
            stacked = vgap <= 1.0 * mh and hgap <= 2.0 * mh and vgap > -0.5 * min(hi, hj)
            # same baseline, a little apart: one caption ("Name    1978")
            same_base = (-vgap >= 0.5 * min(hi, hj) and abs((yi + hi) - (yj + hj)) <= 0.2 * mh
                         and hgap <= 5.0 * 0.75 * min(hi, hj))
            if stacked or same_base:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups: Dict[int, List[TextLine]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(lines[i])
    blocks = [TextBlock(_union_box([ln.box for ln in g]), sorted(g, key=lambda ln: (ln.box[1], ln.box[0])))
              for g in groups.values()]
    blocks.sort(key=lambda b: (b.box[1], b.box[0]))
    return blocks


def _band_components(arr: np.ndarray, band: BandResult, bd: dict):
    """Strong-contrast components of one band, with the photo, the photo-edge
    strip and the image border excluded.

    -> dict(lab, stats (kept rows, label ids), crop8, dist, t, origin, keep ids) or None."""
    H, W = arr.shape[:2]
    unit = _unit(arr)
    col = _colors(arr)
    bx, by, bw, bh = (int(v) for v in bd["rect"])
    if bw < 4 or bh < 4:
        return None
    hard, strip = _photo_masks(band, bx, by, bw, bh)
    crop = col[by:by + bh, bx:bx + bw]
    t = min(1.0, TEXT_WORK_LONG_EDGE / float(max(bw, bh)))
    if t < 1.0:
        crop = _resize_area(crop, t)
        sz = (crop.shape[1], crop.shape[0])
        hard = cv2.resize(hard.astype(np.uint8), sz, interpolation=cv2.INTER_NEAREST).astype(bool)
        strip = cv2.resize(strip.astype(np.uint8), sz, interpolation=cv2.INTER_NEAREST).astype(bool)
    crop8 = _as8f(crop, unit)
    color8 = _band_color8(arr, band, bd)
    if hard.any():  # photo pixels must not colour the paper estimate
        crop8 = crop8.copy()
        crop8[hard] = color8
    dist = _dist_map(crop8, band.textured, color8)
    high, _ = _text_thresholds(band.noise)
    excl = hard | strip
    mask = ((dist > high) & ~excl).astype(np.uint8)
    n, lab, stats, _cent = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 1:
        return None
    ids = np.arange(1, n)
    st = stats[1:]
    ch, cw = mask.shape
    ww, hh, area = st[:, 2], st[:, 3], st[:, 4]
    min_side = np.minimum(ww, hh)
    thin_line = ((ww > 15 * hh) | (hh > 15 * ww)) & (min_side <= max(4, 0.004 * max(bw, bh) * t))
    keep = (area >= 3) & ~thin_line & (hh < 0.95 * ch) & (ww < 0.95 * cw)
    # components touching the image border: scanner lid / background, never caption text
    x0s, y0s, x1s, y1s = st[:, 0], st[:, 1], st[:, 0] + ww, st[:, 1] + hh
    touch = np.zeros(len(st), bool)
    if bx == 0:
        touch |= x0s == 0
    if by == 0:
        touch |= y0s == 0
    if bx + bw >= W:
        touch |= x1s >= cw
    if by + bh >= H:
        touch |= y1s >= ch
    keep &= ~touch
    # tiny slivers right at the excluded photo-edge strip: ghosts of the blurred edge
    if excl.any():
        near = cv2.dilate(excl.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool) & ~excl
        near_ids = np.unique(lab[near])
        near_ids = near_ids[near_ids > 0]
        ghost = np.zeros(len(st), bool)
        ghost[near_ids - 1] = True
        keep &= ~(ghost & (min_side <= 2))
    return {"lab": lab, "stats": st, "ids": ids, "keep": keep, "crop8": crop8, "dist": dist,
            "t": t, "origin": (bx, by), "size": (bw, bh), "color8": color8, "strip": strip, "hard": hard}


def _find_in_band(arr: np.ndarray, band: BandResult, bd: dict) -> List[TextLine]:
    bc = _band_components(arr, band, bd)
    if bc is None:
        return []
    st_all, keep, lab = bc["stats"], bc["keep"], bc["lab"]
    st = st_all[keep]
    kid = bc["ids"][keep]
    if len(st) == 0:
        return []
    bx, by = bc["origin"]
    t = bc["t"]
    groups = _group_lines(st[:, :4])
    # drop lines hugging the band boundary (the photo edge strip / band border)
    strip_w = _edge_strip(band) * t
    out: List[TextLine] = []
    inv = 1.0 / t
    lut = np.zeros(lab.max() + 1, np.int32)
    good = []
    for g in groups:
        bb = _union_box([st[i, :4] for i in g])
        if bb[3] < 4 or max(st[i, 3] for i in g) < 4:
            continue  # specks
        if band.textured and len(g) == 1 and st[g[0], 4] < 30:
            continue  # isolated dust on paper
        good.append((g, bb))
    hs = [bb[3] for _, bb in good]
    hmed = float(np.median(hs)) if hs else 0.0
    for li, (g, bb) in enumerate(good):
        # a long thin line right at the excluded strip: residue of the photo edge
        if bb[3] <= max(6.0, 0.35 * hmed) and bb[2] >= 6 * bb[3]:
            sx0, sy0 = bb[0], bb[1]
            sub = bc["strip"][max(0, sy0 - int(3 + strip_w)):sy0 + bb[3] + int(3 + strip_w),
                              max(0, sx0 - int(3 + strip_w)):sx0 + bb[2] + int(3 + strip_w)]
            if sub.any():
                continue
        lut[kid[g]] = li + 1
    line_map = lut[lab]
    dist, crop8 = bc["dist"], bc["crop8"]
    for li, (g, bb) in enumerate(good):
        if not (lut[kid[g]] == li + 1).any():
            continue
        x0 = bx + int(np.floor(bb[0] * inv)); y0 = by + int(np.floor(bb[1] * inv))
        x1 = bx + int(np.ceil((bb[0] + bb[2]) * inv)); y1 = by + int(np.ceil((bb[1] + bb[3]) * inv))
        m = line_map[bb[1]:bb[1] + bb[3], bb[0]:bb[0] + bb[2]] == li + 1
        dv = dist[bb[1]:bb[1] + bb[3], bb[0]:bb[0] + bb[2]][m]
        cv = crop8[bb[1]:bb[1] + bb[3], bb[0]:bb[0] + bb[2]][m]
        if t < 1.0:
            m = cv2.resize(m.astype(np.uint8), (x1 - x0, y1 - y0), interpolation=cv2.INTER_NEAREST).astype(bool)
        ink8 = tuple(float(v) for v in np.median(cv, axis=0)) if cv.size else ()
        out.append(TextLine((x0, y0, x1 - x0, y1 - y0), ink=(x0, y0, m),
                            contrast=float(np.median(dv)) if dv.size else 0.0, ink8=ink8))
    return out


def _classify_blocks(blocks: List[TextBlock], band: BandResult) -> None:
    """Mark paper backprint / lab logos as role "other": a block much smaller and
    fainter than the main caption, grey, near a corner of its band."""
    if len(blocks) < 2:
        return
    stats = []
    for b in blocks:
        hs = [ln.box[3] for ln in b.lines]
        cs = [ln.contrast for ln in b.lines if ln.contrast]
        inks = [ln.ink8 for ln in b.lines if ln.ink8]
        chroma = float(np.median([max(c) - min(c) for c in inks])) if inks and len(inks[0]) >= 3 else 0.0
        stats.append((max(hs) if hs else 0, float(np.median(cs)) if cs else 0.0, chroma))
    main = max(range(len(blocks)), key=lambda i: (stats[i][0] * max(1, len(blocks[i].lines)), stats[i][1]))
    mh, mc, _ = stats[main]
    for i, b in enumerate(blocks):
        if i == main:
            continue
        h, c, chroma = stats[i]
        bd = _band_for_box(band, b.box)
        corner = False
        if bd is not None:
            x, y, w, hh = bd["rect"]
            cx, cy = b.box[0] + b.box[2] / 2.0, b.box[1] + b.box[3] / 2.0
            if bd["side"] in ("top", "bottom"):
                corner = cx < x + 0.25 * w or cx > x + 0.75 * w
            else:
                corner = cy < y + 0.25 * hh or cy > y + 0.75 * hh
        if corner and h <= 0.6 * mh and c <= 0.7 * mc and chroma <= 25:
            b.role = "other"


def find_text(arr: np.ndarray, band: BandResult) -> List[TextBlock]:
    """Find text lines inside the band rectangles and group them into blocks."""
    arr = _as3d(np.asarray(arr))
    if not band.found:
        return []
    blocks: List[TextBlock] = []
    for bd in band.bands:
        blocks.extend(_group_blocks(_find_in_band(arr, band, bd)))
    _classify_blocks(blocks, band)
    return blocks


def _alnum(s: str) -> int:
    return sum(ch.isalnum() for ch in s)


def filter_ocr_lines(blocks: List[TextBlock]) -> List[TextBlock]:
    """After OCR: drop lines that are punctuation or specks read as text ("|",
    "'", "i" at low confidence) and blocks left empty."""
    out = []
    for b in blocks:
        keep = [ln for ln in b.lines
                if _alnum(ln.text) >= 2 or (_alnum(ln.text) >= 1 and ln.confidence >= 0.6)]
        if not keep:
            continue
        if len(keep) != len(b.lines):
            b = TextBlock(_union_box([ln.box for ln in keep]), keep, b.role)
        out.append(b)
    return out


def line_ocr_crop(arr: np.ndarray, band: BandResult, line: TextLine):
    """8-bit RGB crop for OCR built from the line's own ink: other components
    (dust, neighbouring lines) are blanked to the paper colour, and the crop is
    clipped to the line's band (no photo pixels).  -> (crop, (x0, y0)) or None."""
    if line.ink is None:
        return None
    bd = _band_for_box(band, line.box)
    if bd is None:
        return None
    a = _as3d(np.asarray(arr))
    unit = _unit(a)
    x, y, w, h = (int(v) for v in line.box)
    pad = max(6, int(0.35 * h))
    x0, y0, x1, y1 = _clip_rect(x - pad, y - pad, x + w + pad, y + h + pad, bd["rect"])
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    crop8 = _as8f(_colors(a)[y0:y1, x0:x1], unit)
    ix, iy, m = line.ink
    keep = np.zeros(crop8.shape[:2], np.uint8)
    ax0, ay0 = max(ix, x0), max(iy, y0)
    ax1, ay1 = min(ix + m.shape[1], x1), min(iy + m.shape[0], y1)
    if ax1 <= ax0 or ay1 <= ay0:
        return None
    keep[ay0 - y0:ay1 - y0, ax0 - x0:ax1 - x0] = m[ay0 - iy:ay1 - iy, ax0 - ix:ax1 - ix]
    r = max(2, int(round(0.08 * h)))
    keep = cv2.dilate(keep, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))).astype(bool)
    hard, _ = _photo_masks(band, x0, y0, x1 - x0, y1 - y0)
    keep &= ~hard
    if (~keep).any():
        bg = np.median(crop8[~keep], axis=0)
    else:
        bg = _band_color8(a, band, bd)
    crop8[~keep] = bg
    rgb = np.clip(crop8 + 0.5, 0, 255).astype(np.uint8)
    if rgb.shape[2] == 1:
        rgb = np.repeat(rgb, 3, axis=2)
    return np.ascontiguousarray(rgb[:, :, :3]), (x0, y0)


def _band_for_box(band: BandResult, box) -> Optional[dict]:
    cx, cy = box[0] + box[2] / 2.0, box[1] + box[3] / 2.0
    best, bestd = None, None
    for bd in band.bands:
        x, y, w, h = bd["rect"]
        dx = max(x - cx, 0, cx - (x + w)); dy = max(y - cy, 0, cy - (y + h))
        d = dx + dy
        if bestd is None or d < bestd:
            best, bestd = bd, d
    return best


def _clip_rect(x0, y0, x1, y1, rect):
    rx, ry, rw, rh = rect
    return max(x0, rx), max(y0, ry), min(x1, rx + rw), min(y1, ry + rh)


def _line_pixels(arr: np.ndarray, band: BandResult, line: TextLine):
    """Hysteresis text mask of one line at full res.

    Distance is measured against the local paper colour (``_bg_map``) unless the
    band is a flat digital fill.  Photo pixels are excluded; inside the photo-edge
    strip only strong pixels in the line's own extent count (text touching the
    photo is erased, the blurred photo edge is not).  Weak pixels may extend the
    strong ones only a little beyond their bounding box (no flooding into paper
    shading).

    Returns (mask bool, (x0, y0), dist, crop8) for the padded, band-clipped box,
    or None if the line lies outside every band."""
    bd = _band_for_box(band, line.box)
    if bd is None:
        return None
    unit = _unit(arr)
    col = _colors(arr)
    x, y, w, h = (int(v) for v in line.box)
    pad = max(4, int(0.5 * h))
    x0, y0, x1, y1 = _clip_rect(x - pad, y - pad, x + w + pad, y + h + pad, bd["rect"])
    if x1 <= x0 or y1 <= y0:
        return None
    crop8 = _as8f(col[y0:y1, x0:x1], unit)
    hard, strip = _photo_masks(band, x0, y0, x1 - x0, y1 - y0)
    color8 = _band_color8(arr, band, bd)
    if hard.any():
        crop8 = crop8.copy()
        crop8[hard] = color8
    if band.textured:
        if min(crop8.shape[:2]) >= 24:
            dist = np.abs(crop8 - _bg_map(crop8)).max(axis=2)
        else:
            bg = np.median(crop8.reshape(-1, crop8.shape[2]), axis=0)
            dist = np.abs(crop8 - bg).max(axis=2)
    else:
        dist = np.abs(crop8 - color8.reshape(1, 1, -1)).max(axis=2)
    high, low = _text_thresholds(band.noise)
    allowed = ~hard & ~strip
    if strip.any():
        # text touching the photo: strip pixels within reach of real ink outside it
        sw = _edge_strip(band) + 2
        seed = ((dist > high) & allowed).astype(np.uint8)
        if seed.any():
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * sw + 1, 2 * sw + 1))
            allowed |= strip & cv2.dilate(seed, k).astype(bool)
    strong_px = (dist > high) & allowed
    lowm = ((dist > low) & allowed).astype(np.uint8)
    n, lab = cv2.connectedComponents(lowm, connectivity=8)
    strong = np.unique(lab[strong_px])
    strong = strong[strong > 0]
    keep = np.zeros(n, bool)
    keep[strong] = True
    mask = keep[lab]
    if strong_px.any():
        ys, xs = np.nonzero(strong_px)
        mg = max(3, int(round(0.15 * h)))
        cap = np.zeros_like(mask)
        cap[max(0, ys.min() - mg):ys.max() + mg + 1, max(0, xs.min() - mg):xs.max() + mg + 1] = True
        mask &= cap
    return mask, (x0, y0), dist, crop8


def _clear_photo(mask: np.ndarray, band: BandResult) -> None:
    """Remove photo pixels (the rect, and the tilted outline around it) from ``mask``."""
    if not band.found:
        return
    x, y, w, h = (int(v) for v in band.photo_rect)
    H, W = mask.shape[:2]
    ya, yb = min(H, max(0, y)), min(H, max(0, y + h))
    xa, xb = min(W, max(0, x)), min(W, max(0, x + w))
    mask[ya:yb, xa:xb] = False     # clamped: the rect may lie partly outside an ROI
    q = _photo_quad(band)
    xs = [p[0] for p in q]; ys = [p[1] for p in q]
    qx0, qy0 = max(0, int(np.floor(min(xs))) - 1), max(0, int(np.floor(min(ys))) - 1)
    qx1, qy1 = min(W, int(np.ceil(max(xs))) + 2), min(H, int(np.ceil(max(ys))) + 2)
    # only the thin frames between the quad's bounding box and the rect can differ
    for (a0, b0, a1, b1) in ((qx0, qy0, qx1, y), (qx0, y + h, qx1, qy1), (qx0, y, x, y + h), (x + w, y, qx1, y + h)):
        a0, b0, a1, b1 = max(0, a0), max(0, b0), min(W, a1), min(H, b1)
        if a1 > a0 and b1 > b0:
            sub = mask[b0:b1, a0:a1]
            if sub.any():
                sub &= ~_quad_mask(q, a0, b0, a1 - a0, b1 - b0, grow=0.25)


def text_mask(arr: np.ndarray, band: BandResult, blocks: List[TextBlock], grow: int = 2) -> np.ndarray:
    """Boolean H x W mask of the actual text pixels (hysteresis threshold against
    the band / local paper colour) inside the line boxes, grown by ``grow`` px.

    Also covers every other strong component in a band that holds caption text
    (stray punctuation, a hyphen between words, dust), except those touching the
    image border (scanner margins), in the photo-edge strip, too big to be
    text, or inside a block marked "other" (paper backprint).
    Never includes pixels inside the photo."""
    arr = _as3d(np.asarray(arr))
    H, W = arr.shape[:2]
    out = np.zeros((H, W), bool)
    if not band.found:
        return out
    caption_lines = []
    for blk in blocks:
        if getattr(blk, "role", "caption") == "other":
            continue
        for ln in blk.lines:
            caption_lines.append(ln)
            r = _line_pixels(arr, band, ln)
            if r is None:
                continue
            m, (x0, y0), _, _ = r
            out[y0:y0 + m.shape[0], x0:x0 + m.shape[1]] |= m
    # every other strong component in bands that carry caption text
    others = [b.box for b in blocks if getattr(b, "role", "caption") == "other"]
    if caption_lines:
        hmax = max(ln.box[3] for ln in caption_lines)
        used = {id(_band_for_box(band, ln.box)) for ln in caption_lines}
        for bd in band.bands:
            if id(bd) not in used:
                continue
            bc = _band_components(arr, band, bd)
            if bc is None:
                continue
            st, keep, t = bc["stats"], bc["keep"].copy(), bc["t"]
            keep &= (st[:, 3] <= 1.5 * hmax * t) & (st[:, 2] <= 6 * hmax * t)
            bx, by = bc["origin"]
            for (ox, oy, ow, oh) in others:
                cx = bx + (st[:, 0] + st[:, 2] / 2.0) / t
                cy = by + (st[:, 1] + st[:, 3] / 2.0) / t
                keep &= ~((cx >= ox - 4) & (cx <= ox + ow + 4) & (cy >= oy - 4) & (cy <= oy + oh + 4))
            if not keep.any():
                continue
            lut = np.zeros(bc["lab"].max() + 1, bool)
            lut[bc["ids"][keep]] = True
            cm = lut[bc["lab"]]
            bw, bh = bc["size"]
            if t < 1.0:
                cm = cv2.resize(cm.astype(np.uint8), (bw, bh), interpolation=cv2.INTER_NEAREST).astype(bool)
            out[by:by + bh, bx:bx + bw] |= cm
    if grow > 0 and out.any():
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
        ys, xs = np.nonzero(out)
        ya, yb = max(0, ys.min() - grow), min(H, ys.max() + grow + 1)
        xa, xb = max(0, xs.min() - grow), min(W, xs.max() + grow + 1)
        sub = cv2.dilate(out[ya:yb, xa:xb].astype(np.uint8), k)
        out[ya:yb, xa:xb] = sub.astype(bool)
    _clear_photo(out, band)
    return out


# =============================================================================
# style estimate
# =============================================================================

def estimate_styles(arr, band, blocks) -> List[Optional[dict]]:
    """Per-block style estimates (mixed-size captions), JSON-able, in block order."""
    out = []
    for b in blocks:
        try:
            out.append(estimate_style(arr, band, [b]).to_json())
        except Exception:
            out.append(None)
    return out


def estimate_style(arr, band, blocks) -> StyleEstimate:
    """Font size (from cap/ascender height), alignment, text colour, line height.
    Blocks marked ``role == "other"`` (backprint, logos) are ignored."""
    arr = _as3d(np.asarray(arr))
    if any(getattr(b, "role", "caption") != "other" for b in blocks):
        blocks = [b for b in blocks if getattr(b, "role", "caption") != "other"]
    caps, colors, weights = [], [], []
    baselines: Dict[int, float] = {}
    lines_all: List[TextLine] = [ln for b in blocks for ln in b.lines]
    for ln in lines_all:
        r = _line_pixels(arr, band, ln)
        if r is None:
            continue
        m, (x0, y0), dist, crop8 = r
        # restrict to the line box itself (padding may reach neighbouring lines)
        lx, ly, lw, lh = (int(v) for v in ln.box)
        sub = np.zeros_like(m)
        sub[max(0, ly - y0):ly - y0 + lh, max(0, lx - x0):lx - x0 + lw] = True
        high, _ = _text_thresholds(band.noise)
        core = (dist > high) & sub
        n, _lab, stats, _ = cv2.connectedComponentsWithStats(core.astype(np.uint8), connectivity=8)
        if n <= 1:
            continue
        st = stats[1:][stats[1:, 4] >= 3]
        if len(st) == 0:
            continue
        bottoms = (st[:, 1] + st[:, 3]).astype(float) + y0
        hs = st[:, 3].astype(float)
        # baseline: where most glyphs end.  Descenders (g, p, y, J) and
        # punctuation end elsewhere and are excluded from the cap estimate.
        big = hs >= 0.4 * hs.max()
        baseline = float(np.median(bottoms[big]))
        on_base = big & (np.abs(bottoms - baseline) <= max(2.0, 0.06 * hs.max()))
        hb = hs[on_base] if on_base.any() else hs[big]
        cap_l = float(np.percentile(hb, 90))
        caps.append(cap_l)
        baselines[id(ln)] = baseline
        weights.append(float(lw))
        # colour: the most contrasting ~15 % of the core pixels (thin strokes are
        # mostly antialiased edge pixels, which bias a plain median light)
        dv = dist[core]
        sel = core & (dist >= np.percentile(dv, 85))
        colors.append(np.median(crop8[sel], axis=0))
    if not caps:
        return StyleEstimate(0.0, 0.0, "center", "#000000", 1.2)
    cap = float(np.average(caps, weights=weights))
    font = cap / CAP_HEIGHT_EM
    color_hex = _hex8(np.median(np.stack(colors), axis=0))

    # alignment relative to the photo's span (top/bottom bands) or the band (side bands)
    aligns = []
    for blk in blocks:
        ls = blk.lines
        if not ls:
            continue
        bd = _band_for_box(band, ls[0].box)
        if bd is None:
            continue
        if bd["side"] in ("top", "bottom"):
            s0, s1 = band.photo_rect[0], band.photo_rect[0] + band.photo_rect[2]
        else:
            s0, s1 = bd["rect"][0], bd["rect"][0] + bd["rect"][2]
        span = max(1.0, float(s1 - s0))
        lefts = np.array([ln.box[0] - s0 for ln in ls], float)
        rights = np.array([s1 - (ln.box[0] + ln.box[2]) for ln in ls], float)
        centers = (lefts - rights) / 2.0
        if len(ls) >= 2:
            spreads = {"left": np.ptp(lefts), "center": np.ptp(centers), "right": np.ptp(rights)}
            best = min(spreads, key=spreads.get)
            if spreads[best] < 0.02 * span + 2 and sorted(spreads.values())[1] > spreads[best] + 0.01 * span:
                aligns.append(best)
                continue
        L, R = float(lefts.mean()), float(rights.mean())
        if abs(L - R) <= 0.08 * span:
            aligns.append("center")
        else:
            aligns.append("left" if L < R else "right")
    align = max(set(aligns), key=aligns.count) if aligns else "center"

    pitches = []
    for blk in blocks:
        bl = [baselines[id(ln)] for ln in blk.lines if id(ln) in baselines]
        pitches.extend(b - a for a, b in zip(bl, bl[1:]) if b > a)
    line_height = float(np.median(pitches)) / font if pitches and font > 0 else 1.2
    return StyleEstimate(round(font, 2), round(cap, 2), align, color_hex, round(line_height, 3))


# =============================================================================
# case D: text over the photo
# =============================================================================

def _stamp_groups(mask: np.ndarray, ph: int) -> List[Tuple[int, int, int, int]]:
    n, _lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    if n <= 1:
        return []
    st = stats[1:]
    w, h, a = st[:, 2].astype(float), st[:, 3].astype(float), st[:, 4].astype(float)
    fill = a / np.maximum(1, w * h)
    ok = (h >= max(6, 0.012 * ph)) & (h <= 0.15 * ph) & (a >= 12) & (fill >= 0.12) & (fill <= 0.95) \
        & (h / np.maximum(w, 1) >= 0.8) & (h / np.maximum(w, 1) <= 6)
    st = st[ok]
    if len(st) < 3:
        return []
    x, y, w, h = (st[:, i].astype(float) for i in range(4))
    cy = y + h / 2
    k = len(st)
    parent = list(range(k))

    def find(q):
        while parent[q] != q:
            parent[q] = parent[parent[q]]
            q = parent[q]
        return q

    for i in range(k):
        for j in range(i + 1, k):
            mh, nh = max(h[i], h[j]), min(h[i], h[j])
            if mh > 1.5 * nh or abs(cy[i] - cy[j]) > 0.35 * mh:
                continue
            gap = max(x[i], x[j]) - min(x[i] + w[i], x[j] + w[j])
            if gap <= 1.6 * mh:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
    groups: Dict[int, List[int]] = {}
    for i in range(k):
        groups.setdefault(find(i), []).append(i)
    # second stage: groups on the same baseline merge across a wider gap (a
    # character lost to a merged background blob splits "'98 6 14" in two)
    gl = [g for g in groups.values() if len(g) >= 2]
    merged = True
    while merged and len(gl) > 1:
        merged = False
        for a in range(len(gl)):
            for b in range(a + 1, len(gl)):
                ba, bb = _union_box([st[i, :4] for i in gl[a]]), _union_box([st[i, :4] for i in gl[b]])
                mh = max(ba[3], bb[3])
                if max(ba[3], bb[3]) > 1.5 * min(ba[3], bb[3]):
                    continue
                if abs((ba[1] + ba[3] / 2) - (bb[1] + bb[3] / 2)) > 0.35 * mh:
                    continue
                gap = max(ba[0], bb[0]) - min(ba[0] + ba[2], bb[0] + bb[2])
                if gap <= 3.0 * mh:
                    gl[a] = gl[a] + gl[b]
                    del gl[b]
                    merged = True
                    break
            if merged:
                break
    return [_union_box([st[i, :4] for i in g]) for g in gl if len(g) >= 3]


_DATE_RE = __import__("re").compile(r"\d{4}|\d{1,2}[ './-]\d{1,2}|'\d{2}")
GENERAL_TEXT_TIMEOUT = 4.0   # s; retried once with 3x on timeout, then reported in ``status``


def _uniform_stroke(gray: np.ndarray) -> bool:
    """Printed text has a near-constant stroke width; textures do not."""
    if gray.size == 0 or min(gray.shape) < 6:
        return False
    g = np.ascontiguousarray(gray.astype(np.uint8))
    _t, bw = cv2.threshold(g, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = bw if bw.mean() < 0.5 else 1 - bw           # minority class is ink
    if ink.sum() < 12:
        return False
    dt = cv2.distanceTransform(ink.astype(np.uint8), cv2.DIST_L2, 3)
    ridge = (dt > 0) & (dt >= cv2.dilate(dt, np.ones((3, 3), np.uint8)) - 1e-6)
    v = dt[ridge]
    if v.size < 6:
        return False
    return float(np.std(v) / max(1e-6, np.mean(v))) <= 0.6


def detect_text_over_photo(proxy8: np.ndarray, photo_rect: tuple, status: Optional[dict] = None) -> List[tuple]:
    """Boxes (x, y, w, h, proxy coords) of text printed over the photo (case D).

    1. Date stamps: bright, saturated orange/yellow/red masks; >= 3 digit-like
       components of similar height on one baseline, in an outer region of the
       photo; confirmed by Tesseract reading >= 2 digits (when available).
    2. General printed text: Tesseract (psm 11) on the grey photo, keeping words
       with confidence > 85 and a uniform stroke width that either sit on one
       baseline with another such word or match a date / digit pattern.  A
       timeout is retried once with a longer limit and reported in ``status``
       ({"general_text": "timeout"}) instead of being silently skipped.
    Designed to stay silent on natural textures; flag-only, never removed.
    """
    from . import ocr as _ocr

    img = _as3d(np.asarray(proxy8))
    if img.dtype != np.uint8:
        img = np.clip(img.astype(np.float32) / _unit(img), 0, 255).astype(np.uint8)
    x, y, w, h = (int(v) for v in photo_rect)
    img = img[y:y + h, x:x + w]
    if img.shape[2] == 1 or img.shape[2] == 2:
        rgb = np.repeat(img[:, :, :1], 3, axis=2)
    else:
        rgb = np.ascontiguousarray(img[:, :, :3])
    ph, pw = rgb.shape[:2]
    if ph < 32 or pw < 32:
        return []
    boxes: List[tuple] = []

    # --- 1. coloured date stamps ----------------------------------------------
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    warm = (hue <= 38) | (hue >= 165)
    # stroke-scale white top-hat: LCD digits are thin and brighter than what is
    # around them, even on an orange-ish background
    k = max(9, int(round(0.03 * ph)) | 1)
    tophat = cv2.morphologyEx(val, cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    masks = (lambda: warm & (sat >= 110) & (val >= 180),
             lambda: warm & (sat >= 140) & (val >= 225),
             lambda: warm & (sat >= 90) & (val >= 150) & (tophat >= 30))
    for make_mask in masks:
        mask = make_mask()
        # stamp must be brighter than its surroundings (not a big orange object)
        if mask.mean() > 0.25:
            continue
        cands = _stamp_groups(mask, ph)
        found_any = False
        for gx, gy, gw, gh in cands:
            cx, cy = gx + gw / 2, gy + gh / 2
            outer = (cx < 0.4 * pw or cx > 0.6 * pw) or (cy < 0.3 * ph or cy > 0.7 * ph)
            if not outer:
                continue
            pad = max(4, gh // 2)
            a0, b0 = max(0, gy - pad), max(0, gx - pad)
            a1, b1 = min(ph, gy + gh + pad), min(pw, gx + gw + pad)
            ok = True
            if _ocr.tesseract_path():
                crop = np.where(mask[a0:a1, b0:b1], 0, 255).astype(np.uint8)
                r = _ocr.tesseract_recognize(crop, psm=7, whitelist="0123456789'-./ ")
                digits = sum(ch.isdigit() for ch in r.get("text", ""))
                best = max([float(w.get("confidence", 0.0)) for w in r.get("words", [])] or [0.0])
                ok = digits >= 3 and best >= 0.6   # stained cells read as "3 4" at 40 %
            if ok:
                boxes.append((x + b0, y + a0, b1 - b0, a1 - a0))
                found_any = True
        if found_any:
            break

    # --- 2. general printed text ------------------------------------------------
    # Textures (grass, cells, brick) produce isolated confident "words"; real text
    # is >= 2 words on one baseline, or a date / digit pattern, with a uniform
    # stroke width and a high confidence.
    if _ocr.tesseract_path():
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        s = min(1.0, 1400.0 / max(ph, pw))
        if s < 1.0:
            gray = cv2.resize(gray, (round(pw * s), round(ph * s)), interpolation=cv2.INTER_AREA)
        words = []
        for tmo in (GENERAL_TEXT_TIMEOUT, 3 * GENERAL_TEXT_TIMEOUT):   # slow machine: one longer retry
            try:
                words = _ocr.tesseract_words(gray, psm=11, timeout=tmo)
                break
            except Exception as exc:  # subprocess.TimeoutExpired or engine failure
                if status is not None:
                    status["general_text"] = "timeout" if "Timeout" in type(exc).__name__ else "failed"
                words = []
                if "Timeout" not in type(exc).__name__:
                    break
        if words and status is not None:
            status.pop("general_text", None)
        cand = []
        for wd in words:
            raw = wd["text"].strip()
            txt = "".join(ch for ch in raw if ch.isalnum())
            bx, by, bw, bh = wd["box"]
            if wd["confidence"] <= 0.85 or len(txt) < 2 or len(txt) < 0.8 * len(raw) or bh < 8:
                continue
            if not _uniform_stroke(gray[by:by + bh, bx:bx + bw]):
                continue
            cand.append((wd, txt))
        used = [False] * len(cand)
        for i, (wd, txt) in enumerate(cand):
            bx, by, bw, bh = wd["box"]
            ok = bool(_DATE_RE.search(wd["text"]))
            for j, (wd2, _t2) in enumerate(cand):
                if j == i:
                    continue
                cx, cy, cw, chh = wd2["box"]
                if max(bh, chh) > 1.5 * min(bh, chh) or abs((by + bh) - (cy + chh)) > 0.3 * max(bh, chh):
                    continue
                if max(bx, cx) - min(bx + bw, cx + cw) <= 3 * max(bh, chh):
                    ok = True
                    break
            if ok:
                used[i] = True
        for (wd, _t), u in zip(cand, used):
            if u:
                bx, by, bw, bh = wd["box"]
                boxes.append((x + int(bx / s), y + int(by / s), int(np.ceil(bw / s)), int(np.ceil(bh / s))))

    # merge overlapping boxes
    merged: List[list] = []
    for b in boxes:
        b = list(b)
        for m in merged:
            if b[0] < m[0] + m[2] and m[0] < b[0] + b[2] and b[1] < m[1] + m[3] and m[1] < b[1] + b[3]:
                x0, y0 = min(b[0], m[0]), min(b[1], m[1])
                x1, y1 = max(b[0] + b[2], m[0] + m[2]), max(b[1] + b[3], m[1] + m[3])
                m[:] = [x0, y0, x1 - x0, y1 - y0]
                break
        else:
            merged.append(b)
    return [tuple(int(v) for v in m) for m in merged]


# =============================================================================
# convenience
# =============================================================================

_SCANNER_WORDS = ("scan", "perfection", "canoscan", "plustek", "opticfilm", "fujitsu", "coolscan", "imacon",
                  "flextight", "microtek", "reflecta", "visioneer", "avision", "doxie", "expression 1", "v600",
                  "v850", "v700", "v550", "v370", "v39")
_CAMERA_KEYS = ("exposuretime", "fnumber", "iso", "focallength", "shutterspeedvalue", "aperturevalue")


def _md_get(md, name: str):
    """Value of an ExifTool tag by bare name (group prefixes like 'IFD0:' ignored)."""
    if not md:
        return None
    try:
        items = md.items()
    except Exception:
        return None
    name = name.lower()
    for k, v in items:
        if str(k).split(":")[-1].lower() == name:
            return v
    return None


def scan_evidence(band: BandResult, md=None) -> Tuple[float, List[str]]:
    """How strongly the band looks like a *physical* border on a scan (case C)
    rather than a flat digital band (case B).  -> (score, cue names).

    Cues: a tilted photo edge (digital bands are never tilted), a soft full-res
    edge, lighting drift / colour disagreement between the sides, paper grain
    (measured robustly to JPEG smoothing), scanner EXIF.
    A score >= 2 means case C; when unsure this leans to C, which only adds
    protections (erase on a copy, overwrite off)."""
    score, cues = 0.0, []
    if not band.found:
        return 0.0, cues
    if abs(float(band.angle)) >= 0.12:
        score += 2.0; cues.append("tilted")
    if band.edge_blur >= 1.5:
        score += 1.0; cues.append("soft edge")
    if band.color_drift >= FLAT_DRIFT:
        score += 1.0; cues.append("lighting drift")
    if band.noise >= TEXTURE_NOISE:
        # not +2: a digital band carrying Photoband's hidden marker (or another
        # tool's texture) also has block-scale "grain"; one more cue is needed
        score += 1.5; cues.append("paper grain")
    elif band.noise >= FLAT_NOISE:
        score += 1.0; cues.append("grain")
    if md:
        dev = " ".join(str(_md_get(md, k) or "") for k in ("Make", "Model", "Software")).lower()
        if any(wd in dev for wd in _SCANNER_WORDS):
            score += 2.0; cues.append("scanner")
        else:
            try:
                dpi = float(_md_get(md, "XResolution") or 0)
            except (TypeError, ValueError):
                dpi = 0.0
            if dpi >= 300 and not any(_md_get(md, k) is not None for k in _CAMERA_KEYS):
                score += 0.5; cues.append("high dpi, no camera data")
    # (stroke-width variance was evaluated as a cue and dropped: monoline pens
    # and pencils vary less than typeset serif/sans text, so it misleads)
    return score, cues


def has_real_text(blocks, min_conf: float = 0.5) -> bool:
    """>= 2 alphanumerics read with confidence >= ``min_conf`` in caption blocks."""
    n = 0
    for b in blocks:
        if getattr(b, "role", "caption") == "other":
            continue
        for ln in b.lines:
            if ln.words:
                n += sum(_alnum(w.get("text", "")) for w in ln.words if float(w.get("confidence", 0)) >= min_conf)
            elif ln.confidence >= min_conf:
                n += _alnum(ln.text)
    return n >= 2


def decide_case(band: BandResult, blocks, score: float, ocr_ran: bool, marker: bool = False):
    """-> (case, quiet hint or None) for a detected band.

    A single plain strip with no real text (a white wall at the bottom of a
    borderless photo, a sky strip) is not a caption band: no case, a quiet hint."""
    if not band.found:
        return None, None
    captions = [b for b in blocks if getattr(b, "role", "caption") != "other"]
    real = has_real_text(captions) if ocr_ran else bool(captions)
    if len(band.bands) == 1 and not real and not marker:
        side = band.bands[0]["side"]
        return None, (f"A plain strip along the {side} edge looks like part of the photo (no readable text "
                      f"in it), so it is left alone. Use Edge to mark a border if it is one.")
    return ("C" if score >= 2.0 else "B"), None


def edge_candidates(arr: np.ndarray, per_side: int = 2, proxy_long_edge: int = 1600) -> List[Tuple[str, float]]:
    """Approximate photo-edge depths (full-res px from each side), strongest first,
    including weak ones that band detection rejected (a high-key photo whose edge
    is invisible along part of the side).  For the hidden-marker fallback."""
    arr = _as3d(np.asarray(arr))
    H, W = arr.shape[:2]
    if H < 16 or W < 16:
        return []
    unit = _unit(arr)
    nc = _ncolor(arr)
    s = min(1.0, float(proxy_long_edge) / max(H, W))
    small = _resize_area(arr, s) if s < 1.0 else arr
    P = _as8f(small[:, :, :nc], unit)
    ph, pw = P.shape[:2]
    sy, sx = ph / H, pw / W
    col = arr[:, :, :nc]
    out = []
    for k in SIDES:
        V = np.ascontiguousarray(_oriented(P, k))
        alen = pw if k in ("top", "bottom") else ph
        sd, su = (sy, sx) if k in ("top", "bottom") else (sx, sy)
        pts, nseg = _side_points(V, 0, alen, sd)
        if len(pts) < 2:
            continue
        d = np.array([p["depth"] for p in pts], float)
        lines = _fit_side(pts, nseg, (0, alen))
        if not lines:  # fewer than the weak support: use the most common depths
            vals, cnt = np.unique(np.round(d).astype(int), return_counts=True)
            lines = [{"a": float(v), "b": 0.0, "support": int(c), "pts": [p for p in pts if round(p["depth"]) == v],
                      "span": (0, alen), "nseg": nseg} for v, c in zip(vals, cnt) if c >= 2]
        lines.sort(key=lambda ln: -ln["support"])
        for ln in lines[:per_side]:
            fl = _refine_side(col, unit, k, ln, sd, su, H if k in ("top", "bottom") else W,
                              W if k in ("top", "bottom") else H)
            full_along = W if k in ("top", "bottom") else H
            out.append((k, float(fl[0] + fl[1] * full_along / 2.0), int(ln["support"])))
    out.sort(key=lambda c: -c[2])
    return [(k, dpt) for k, dpt, _ in out]
