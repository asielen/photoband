"""Acceptance tests for photoband.marker (hidden band marker)."""
from __future__ import annotations

import io
import os
import time
from typing import Tuple

import cv2
import numpy as np
import pytest
from PIL import Image

from photoband import marker

WHITE = (255, 255, 255)
OFF_WHITE = (0xF4, 0xF1, 0xEA)
DARK = (0x11, 0x11, 0x11)
HASH = "3fa9c07d1be24e58a1c9d0f7e6b5a4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7"
ART_DIR = os.path.join(os.path.dirname(__file__), "_artifacts")
LINES = ("Summer picnic at the lake, July 1962",
         "Left to right: Anna, Ben, Carla and Uncle Dave",
         "Scanned 2026 - archive box 7, print 12")


class Case:
    """A synthetic Polaroid-like canvas plus the ground truth."""

    def __init__(self, photo_w: int, dtype=np.uint8, band=WHITE, gray: bool = False,
                 text: bool = True, seed: int = 0, divider: bool = False):
        rng = np.random.default_rng(seed)
        photo_h = photo_w * 3 // 4
        side, top, bottom = (round(photo_w * f) for f in (0.057, 0.076, 0.28))
        self.h, self.w = top + photo_h + bottom, 2 * side + photo_w
        self.rect = (side, top, photo_w, photo_h)
        self.scale = 257 if dtype == np.uint16 else 1
        ch = 1 if gray else 3
        band_arr = np.array(band[:ch], np.float64)
        img = np.empty((self.h, self.w, ch), np.float64)
        img[:] = band_arr
        photo = rng.integers(0, 256, (photo_h, photo_w, ch)).astype(np.float64)
        photo = cv2.GaussianBlur(photo.astype(np.float32), (0, 0), 1.0).reshape(photo_h, photo_w, ch)
        img[top:top + photo_h, side:side + photo_w] = photo
        # anti-aliased text in the bottom band
        alpha = np.zeros((self.h, self.w), np.uint8)
        if text:
            fs = photo_w / 1200 * 1.3
            th = max(1, round(fs * 2))
            y = top + photo_h + round(bottom * 0.22)
            for line in LINES:
                cv2.putText(alpha, line, (side + round(photo_w * 0.03), y),
                            cv2.FONT_HERSHEY_SIMPLEX, fs, 255, th, cv2.LINE_AA)
                y += round(bottom * 0.2)
        a = (alpha.astype(np.float64) / 255)[:, :, None]
        ink = np.full(ch, 225.0 if max(band) < 128 else 20.0)
        img = img * (1 - a) + ink * a
        self.exclude = np.zeros((self.h, self.w), bool)
        if divider:  # hairline divider between photo and caption
            yd = top + photo_h + round(bottom * 0.06)
            img[yd, side:side + photo_w] = np.full(ch, 200.0 if max(band) > 128 else 60.0)
            self.exclude[yd, side:side + photo_w] = True
        self.text = alpha > 1  # OpenCV 5 LINE_AA leaves an alpha-1 fringe the marker rightly treats as background
        if dtype == np.uint16:
            img = img * 257
            band_units = tuple(int(v) * 257 for v in band[:ch])
        else:
            band_units = tuple(int(v) for v in band[:ch])
        self.canvas = np.clip(np.round(img), 0, 255 * self.scale).astype(dtype)
        if gray:
            self.canvas = self.canvas[:, :, 0].copy()
        self.band = band_units
        self.band8 = tuple(band) if not gray else (band[0],) * 3

    def approx(self, s: float = 1.0, jitter: Tuple[int, int, int, int] = (2, -2, -1, 2)):
        return tuple(round(v * s) + j for v, j in zip(self.rect, jitter))

    def scaled_rect(self, s: float):
        return tuple(v * s for v in self.rect)


def payload_bytes(n: int, seed: int = 1) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, n, dtype=np.uint8).tobytes()


def jpeg(arr: np.ndarray, q: int) -> np.ndarray:
    if arr.dtype == np.uint16:
        arr = np.round(arr / 257).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "JPEG", quality=q)
    return np.asarray(Image.open(io.BytesIO(buf.getvalue()))).copy()


def png(arr: np.ndarray) -> np.ndarray:
    ok, enc = cv2.imencode(".png", arr)
    assert ok
    return cv2.imdecode(enc, cv2.IMREAD_UNCHANGED)


def resize(arr: np.ndarray, s: float) -> np.ndarray:
    h, w = arr.shape[:2]
    return cv2.resize(arr, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)


def assert_rect_close(found, truth, tol: float = 1.0):
    assert found is not None, "marker not found"
    assert all(abs(a - b) <= tol for a, b in zip(found.photo_rect, truth)), (found.photo_rect, truth)


# --------------------------------------------------------------------------
# fixtures (embedding is the expensive part, share it)
# --------------------------------------------------------------------------
VARIANTS = {
    "rgb8_white": dict(photo_w=1200),
    "rgb8_offwhite": dict(photo_w=1200, band=OFF_WHITE, seed=2),
    "rgb8_dark": dict(photo_w=1200, band=DARK, seed=3),
    "rgb16_white": dict(photo_w=1200, dtype=np.uint16, seed=4),
    "gray8_white": dict(photo_w=1200, gray=True, seed=5),
    "rgb8_white_divider": dict(photo_w=1200, divider=True, seed=6),
}
_CACHE: dict = {}


def embedded(name: str, pay_len: int = 4096):
    key = (name, pay_len)
    if key not in _CACHE:
        case = Case(**VARIANTS[name]) if name != "large" else Case(3000, seed=9)
        before = case.canvas.copy()
        pay = payload_bytes(pay_len)
        res = marker.embed(case.canvas, case.rect, case.band, HASH, pay,
                           exclude=case.exclude if case.exclude.any() else None)
        _CACHE[key] = (case, before, pay, res)
    return _CACHE[key]


ALL = list(VARIANTS)


# --------------------------------------------------------------------------
# 1. round trip / 3. lossless copies
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL)
def test_round_trip(name):
    case, _, pay, res = embedded(name)
    assert res.robust and res.payload, res
    assert res.tiles >= 1.5          # copies of the 780-bit code
    info = marker.read(case.canvas, case.approx())
    assert info is not None
    assert info.photo_rect == case.rect
    assert info.band_color == case.band8
    assert info.short_hash == HASH[:16] == marker.short_hash(HASH)
    assert info.payload_len == len(pay)
    assert info.payload == pay


def test_round_trip_large_12k():
    case, _, pay, res = embedded("large", 12 * 1024)
    assert res.robust and res.payload and res.tiles >= 3, res
    info = marker.read(case.canvas, case.approx())
    assert info is not None and info.photo_rect == case.rect and info.rect_ok
    assert info.payload == pay


@pytest.mark.parametrize("name", ["rgb8_white", "rgb16_white", "gray8_white", "rgb8_dark"])
def test_payload_survives_png(name):
    case, _, pay, _ = embedded(name)
    loaded = png(case.canvas)
    assert loaded.dtype == case.canvas.dtype
    info = marker.read(loaded, case.approx())
    assert info is not None and info.photo_rect == case.rect
    assert info.payload == pay


# --------------------------------------------------------------------------
# 2. untouched pixels and amplitude
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL)
def test_only_background_changes(name):
    case, before, _, _ = embedded(name)
    after = case.canvas
    x, y, w, h = case.rect
    assert np.array_equal(after[y:y + h, x:x + w], before[y:y + h, x:x + w])
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))  # 4 px growth
    grown = cv2.dilate(case.text.astype(np.uint8), k).astype(bool)
    assert np.array_equal(after[grown], before[grown])
    assert np.array_equal(after[case.exclude], before[case.exclude])
    d = after.astype(np.int64) - before.astype(np.int64)
    assert np.abs(d).max() <= 2 * case.scale
    assert np.abs(d).max() > 0
    if case.band8 == WHITE:
        assert d.max() <= 0, "white band pixels must only move down"


def test_alpha_untouched():
    case = Case(1200, seed=11)
    rgba = np.dstack([case.canvas, np.full(case.canvas.shape[:2], 255, np.uint8)])
    res = marker.embed(rgba, case.rect, (255, 255, 255, 255), HASH, payload_bytes(1000))
    assert res.robust and res.payload
    assert (rgba[:, :, 3] == 255).all()
    info = marker.read(rgba, case.approx())
    assert info is not None and info.payload == payload_bytes(1000)


@pytest.mark.parametrize("turn", ["top", "right", "left"])
def test_other_anchor_sides(turn):
    """The largest band may be on any side (e.g. a rotated layout)."""
    case = Case(1200, seed=17)
    x, y, w, h = case.rect
    H, W = case.h, case.w
    if turn == "top":
        img, rect = case.canvas[::-1], (x, H - y - h, w, h)
    elif turn == "right":   # transpose: bottom band becomes the right band
        img, rect = case.canvas.transpose(1, 0, 2), (y, x, h, w)
    else:                   # transpose + flip: bottom band becomes the left band
        img, rect = case.canvas.transpose(1, 0, 2)[:, ::-1], (W - y - h, x, h, w)
    img = np.ascontiguousarray(img)
    pay = payload_bytes(3000, seed=3)
    res = marker.embed(img, rect, case.band, HASH, pay)
    assert res.robust and res.payload, res
    approx = tuple(v + j for v, j in zip(rect, (1, -2, 2, 1)))
    info = marker.read(img, approx)
    assert info is not None and info.side == turn
    assert info.photo_rect == rect and info.payload == pay
    info = marker.read(jpeg(resize(img, 0.5), 85), tuple(round(v * 0.5) for v in approx))
    assert_rect_close(info, tuple(v * 0.5 for v in rect))


def test_faint_keyline_in_exclude():
    """An exclude mask the reader cannot see (a keyline almost the band colour)
    is never touched; payload units simply avoid it, so the payload still reads."""
    case = Case(1200, seed=18)
    x, y, w, h = case.rect
    ex = np.zeros(case.canvas.shape[:2], bool)
    ex[y + h + 30, x:x + w] = True
    case.canvas[ex] = 253          # 2 levels off white: below every threshold
    before = case.canvas.copy()
    pay = payload_bytes(2000)
    res = marker.embed(case.canvas, case.rect, case.band, HASH, pay, exclude=ex)
    assert res.robust and res.payload, res
    assert np.array_equal(case.canvas[ex], before[ex])
    info = marker.read(case.canvas, case.approx())
    assert info is not None and info.photo_rect == case.rect and info.payload == pay


def test_no_payload():
    case = Case(1200, seed=19)
    res = marker.embed(case.canvas, case.rect, case.band, HASH)
    assert res.robust and not res.payload and "payload" not in res.reason
    info = marker.read(case.canvas, case.approx())
    assert info.payload is None and info.payload_len == 0


# --------------------------------------------------------------------------
# 4-6. robustness of the robust layer
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ALL)
def test_jpeg_q70(name):
    case, _, pay, _ = embedded(name)
    info = marker.read(jpeg(case.canvas, 70), case.approx())
    assert_rect_close(info, case.rect)
    assert info.short_hash == HASH[:16]
    assert info.payload is None or info.payload == pay


@pytest.mark.parametrize("name", ALL)
def test_resize_50_jpeg85(name):
    case, _, _, _ = embedded(name)
    small = jpeg(resize(case.canvas, 0.5), 85)
    info = marker.read(small, case.approx(0.5))
    assert_rect_close(info, case.scaled_rect(0.5))
    assert info.payload is None


@pytest.mark.parametrize("s", [0.5, 0.25])
def test_large_resize(s):
    case, _, _, _ = embedded("large", 12 * 1024)
    small = jpeg(resize(case.canvas, s), 85)
    info = marker.read(small, case.approx(s))
    assert_rect_close(info, case.scaled_rect(s))


@pytest.mark.parametrize("name", ALL)
def test_crop_far_edge(name):
    case, _, _, _ = embedded(name)
    x, y, w, h = case.rect
    band_h = case.h - (y + h)
    cropped = case.canvas[: case.h - int(band_h * 0.3)].copy()
    info = marker.read(cropped, case.approx())
    assert_rect_close(info, case.rect, 0)
    assert info.payload is None
    # and after a JPEG re-save too
    info = marker.read(jpeg(cropped, 80), case.approx())
    assert_rect_close(info, case.rect)


# --------------------------------------------------------------------------
# 7-8. skip and false positives
# --------------------------------------------------------------------------
def test_tiny_canvas_skips():
    case = Case(200, seed=12)
    before = case.canvas.copy()
    res = marker.embed(case.canvas, case.rect, case.band, HASH, payload_bytes(100))
    assert not res.robust and not res.payload
    assert res.reason
    assert np.array_equal(case.canvas, before)
    assert marker.read(case.canvas, case.approx()) is None


def test_no_false_positives():
    rng = np.random.default_rng(13)
    photo = cv2.GaussianBlur(rng.integers(0, 256, (1600, 1200, 3), dtype=np.uint8), (0, 0), 2)
    t0 = time.perf_counter()
    assert marker.read(photo, (60, 80, 1080, 1200)) is None
    assert marker.read(photo, None) is None
    flat = np.full((1600, 1200, 3), 255, np.uint8)
    assert marker.read(flat, (60, 80, 1080, 1100)) is None
    for name in ("rgb8_white", "rgb8_dark"):
        clean = Case(**VARIANTS[name])
        assert marker.read(clean.canvas, clean.approx()) is None
        assert marker.read(jpeg(clean.canvas, 75), clean.approx()) is None
    assert time.perf_counter() - t0 < 10


# --------------------------------------------------------------------------
# 9. invisibility
# --------------------------------------------------------------------------
def test_invisible_after_levels_stretch():
    case = Case(1200, text=False, seed=14)
    res = marker.embed(case.canvas, case.rect, case.band, HASH, payload_bytes(4096))
    assert res.robust and res.payload
    x, y, w, h = case.rect
    band = case.canvas[y + h + 8: case.h - 8, 8: case.w - 8].astype(np.float64).mean(axis=2)
    stretched = np.clip(255 - (255 - band) * 8, 0, 255)
    os.makedirs(ART_DIR, exist_ok=True)
    Image.fromarray(stretched.astype(np.uint8)).save(os.path.join(ART_DIR, "marker_stretch.png"))
    bh, bw = (d // 8 * 8 for d in stretched.shape)
    means8 = stretched[:bh, :bw].reshape(bh // 8, 8, bw // 8, 8).mean(axis=(1, 3))
    assert means8.std() < 6, means8.std()
    # white only darkens: about one level on average, never two anywhere (x8 after the stretch)
    win = cv2.blur(stretched, (32, 32))[16:-16, 16:-16]
    assert (255 - win).max() < 16, (255 - win).max()
    assert 4 < 255 - stretched.mean() < 12


# --------------------------------------------------------------------------
# performance
# --------------------------------------------------------------------------
def test_performance():
    small = Case(1200, seed=15)
    t0 = time.perf_counter()
    marker.embed(small.canvas, small.rect, small.band, HASH, payload_bytes(4096))
    t_embed_small = time.perf_counter() - t0
    t0 = time.perf_counter()
    assert marker.read(small.canvas, small.approx()) is not None
    t_read_small = time.perf_counter() - t0
    large = Case(3000, seed=16)
    t0 = time.perf_counter()
    marker.embed(large.canvas, large.rect, large.band, HASH, payload_bytes(12 * 1024))
    t_embed_large = time.perf_counter() - t0
    t0 = time.perf_counter()
    assert marker.read(large.canvas, large.approx()) is not None
    t_read_large = time.perf_counter() - t0
    print(f"embed {t_embed_small:.2f}s/{t_embed_large:.2f}s read {t_read_small:.2f}s/{t_read_large:.2f}s")
    assert t_embed_small < 1 and t_read_small < 1
    assert t_embed_large < 3 and t_read_large < 3


# --------------------------------------------------------------------------
# format 1 files (written by the first release) still read
# --------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["rgb8_white", "rgb8_dark", "rgb16_white", "gray8_white"])
def test_version1_files_still_read(name):
    case = Case(**VARIANTS[name])
    pay = payload_bytes(3000, seed=4)
    res = marker._embed_v1(case.canvas, case.rect, case.band, HASH, pay)
    assert res.robust and res.payload, res
    info = marker.read(case.canvas, case.approx())
    assert info is not None and info.version == 1
    assert info.photo_rect == case.rect and info.short_hash == HASH[:16]
    assert info.payload == pay
    info = marker.read(jpeg(case.canvas, 80), case.approx())
    assert_rect_close(info, case.rect)
    assert info.version == 1


# --------------------------------------------------------------------------
# payload units are local: a scribble or dust costs at most one copy
# --------------------------------------------------------------------------
def test_payload_survives_local_damage():
    case, _, pay, res = embedded("rgb8_white")
    assert res.payload
    h, w = case.canvas.shape[:2]
    for name, fn in (
        ("dot", lambda a: cv2.circle(a, (w - 30, h - 20), 1, (80, 80, 80), -1)),
        ("scribble", lambda a: cv2.line(a, (w - 200, h - 40), (w - 160, h - 30), (30, 30, 200), 2)),
        ("dust", lambda a: cv2.circle(a, (40, h - 20), 2, (245, 245, 245), -1)),
        ("lsb flips", None),
    ):
        a = case.canvas.copy()
        if fn is None:
            rng = np.random.default_rng(1)
            ys, xs = rng.integers(h - 60, h - 5, 20), rng.integers(w - 300, w - 10, 20)
            a[ys, xs, 0] ^= 1
        else:
            fn(a)
        info = marker.read(a, case.approx())
        assert info is not None and info.payload == pay, name


def test_scrub_flat_band():
    case, _, pay, _ = embedded("rgb8_white")
    c = case.canvas.copy()
    rep = marker.scrub(c, case.rect, case.band)
    assert rep == {"found": True, "flat": True, "removed": True}
    assert marker.read(c, case.approx()) is None
    x, y, w, h = case.rect
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    grown = cv2.dilate(case.text.astype(np.uint8), k).astype(bool)
    assert np.array_equal(c[y:y + h, x:x + w], case.canvas[y:y + h, x:x + w])     # photo untouched
    diff = np.abs(c.astype(int) - case.canvas.astype(int)).max(axis=2)
    assert diff[grown & (np.abs(case.canvas.astype(int) - 255).max(axis=2) > 3)].max() == 0   # text untouched


def test_large_canvas_speed():
    """12k-wide canvas: embed and read well under the save budget."""
    case, _, pay, _ = embedded("large", 12 * 1024)
    big = cv2.resize(case.canvas, (case.w * 4, case.h * 4), interpolation=cv2.INTER_NEAREST)
    rect = tuple(v * 4 for v in case.rect)
    marker.scrub(big, rect, case.band)
    t0 = time.perf_counter()
    res = marker.embed(big, rect, case.band, HASH, pay)
    t_embed = time.perf_counter() - t0
    t0 = time.perf_counter()
    info = marker.read(big, tuple(v + 2 for v in rect))
    t_read = time.perf_counter() - t0
    print(f"12k: embed {t_embed:.2f}s read {t_read:.2f}s")
    assert res.robust and res.payload and info is not None and info.payload == pay
    assert t_embed < 3 and t_read < 3
