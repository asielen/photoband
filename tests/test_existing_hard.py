"""Hard existing-text cases (review #8): tilted scans, scanner lid margins and
deckled rims, a borderless photo with a white wall, apostrophes / i-dots,
captions split around punctuation, dust specks, and the erase ROI.

All fixtures are synthetic with exact ground truth.
"""
from __future__ import annotations

import os

import cv2
import numpy as np
import pytest

from photoband import detect, erase
from photoband.existing import analyze_existing

from test_detect import HAS_TESS, SANS_BOLD, analyze, draw_lines, framed, lines_of, make_photo


def _paper(h, w, color, grain, seed):
    rng = np.random.default_rng(seed)
    p = np.ones((h, w, 3), np.float32) * np.array(color, np.float32)
    lo = cv2.resize(rng.normal(0, 1, (h // 16 + 1, w // 16 + 1)).astype(np.float32), (w, h),
                    interpolation=cv2.INTER_CUBIC)
    return p + (rng.normal(0, grain, (h, w)).astype(np.float32) + 0.8 * grain * lo)[:, :, None]


def _scan(img, angle, bg, blur=0.7, noise=1.2, light=0.06, seed=5):
    """Rotate like a crooked scan (cv2 convention: + = counter-clockwise), with
    light fall-off, blur and sensor noise.  -> (uint8 image, 2x3 matrix)."""
    h, w = img.shape[:2]
    out = img.astype(np.float32)
    gx = np.linspace(1 + light / 2, 1 - light / 2, w, dtype=np.float32)
    out = out * gx[None, :, None]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    if angle:
        out = cv2.warpAffine(out, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=bg)
    out = cv2.GaussianBlur(out, (0, 0), blur)
    out += np.random.default_rng(seed).normal(0, noise, out.shape).astype(np.float32)
    return np.clip(np.round(out), 0, 255).astype(np.uint8), M


def _true_quad(M, rect):
    x, y, w, h = rect
    pts = np.array([[x, y, 1], [x + w, y, 1], [x + w, y + h, 1], [x, y + h, 1]], np.float64)
    return pts @ M.T


def _inscribed(q):
    tl, tr, br, bl = q
    return (max(tl[0], bl[0]), max(tl[1], tr[1]), min(tr[0], br[0]), min(bl[1], br[1]))


def _polaroid_scan(angle, seed=3):
    W, H = 1400, 1700
    side, top, pw = 84, 110, 1232
    ph = 1232
    base = _paper(H, W, (238, 235, 225), 2.2, seed)
    base[top:top + ph, side:side + pw] = make_photo(pw, ph, seed=seed + 10).astype(np.float32)
    img = np.clip(base, 0, 255).astype(np.uint8)
    draw_lines(img, (0, top + ph, W, H - top - ph), ["Aunt Rose at Tahoe", "July 1987"], 70, (30, 35, 90))
    return _scan(img, angle, bg=(238, 235, 225)), (side, top, pw, ph)


# =============================================================================
# 1. tilted scan
# =============================================================================

@pytest.mark.parametrize("angle", [1.2, -0.7])
def test_tilted_scan_edges_angle_and_inscribed_rect(angle):
    (img, M), rect = _polaroid_scan(angle)
    band = detect.detect_band(img)
    assert band.found
    assert {b["side"] for b in band.bands} == {"top", "bottom", "left", "right"}
    assert abs(band.angle - angle) < 0.1, band.angle
    q = _true_quad(M, rect)
    for (dx, dy), (tx, ty) in zip(band.photo_quad, q):
        assert abs(dx - tx) <= 1.5 and abs(dy - ty) <= 1.5, (band.photo_quad, q.tolist())
    L, T, R, B = _inscribed(q)
    x, y, w, h = band.photo_rect
    for got, want in zip((x, y, x + w, y + h), (L, T, R, B)):
        assert abs(got - want) <= 1.5, (band.photo_rect, (L, T, R, B))
    js = band.to_json()
    assert "angle" in js and len(js["photo_quad"]) == 4


def test_tilted_scan_is_case_c_and_text_is_clean():
    (img, M), rect = _polaroid_scan(1.2)
    res = analyze(img, run_ocr=HAS_TESS)
    assert res["case"] == "C"
    assert "tilted" in res["scanCues"]
    band = detect.detect_band(img)
    blocks = detect.find_text(img, band)
    lines = lines_of(blocks)
    assert len(lines) == 2, [ln.box for ln in lines]      # no ghost lines at the blurred, tilted photo edge
    if HAS_TESS:
        assert "Tahoe" in res["text"] and "1987" in res["text"]


def test_erase_never_touches_tilted_photo():
    (img, M), rect = _polaroid_scan(1.2)
    band = detect.detect_band(img)
    blocks = detect.find_text(img, band)
    mask = erase.build_mask(img, band, blocks)
    # a brush stroke right across the photo edge must stay out of the photo
    add = np.zeros(img.shape[:2], bool)
    add[1300:1360, 200:1200] = True
    mask = erase.build_mask(img, band, blocks, add_mask=add)
    out = erase.erase_in_place(img, mask, band)
    inside = detect._quad_mask(_true_quad(M, rect), 0, 0, img.shape[1], img.shape[0], grow=-1.0)
    assert np.array_equal(out[inside], img[inside])
    assert (out != img).any()


# =============================================================================
# 2. scanner lid margin, deckled rim
# =============================================================================

def _print_on(margin_color, margin=15):
    photo = make_photo(900, 880, seed=5)
    img, (x, y, w, h) = framed(photo, (240, 237, 228), (70, 250, 52, 52), ["Lake Merced 1962"], font_px=48,
                               texture=2.0, seed=9)
    H, W = img.shape[:2]
    big = np.empty((H + 2 * margin, W + 2 * margin, 3), np.uint8)
    big[:] = margin_color
    big[margin:margin + H, margin:margin + W] = img
    return big, (x + margin, y + margin, w, h)


@pytest.mark.parametrize("lid", [(252, 252, 252), (40, 40, 42)])
def test_scanner_lid_margin_nested(lid):
    img, truth = _print_on(lid)
    band = detect.detect_band(img)
    assert band.found
    for a, b in zip(band.photo_rect, truth):
        assert abs(a - b) <= 1, (band.photo_rect, truth)
    # the band colour is the paper's, not the lid's
    c = np.array(band.band_color, float)
    assert np.abs(c - np.array([240, 237, 228])).max() < 8, band.band_color
    lines = lines_of(detect.find_text(img, band))
    assert len(lines) == 1, [ln.box for ln in lines]   # the lid is not text


def test_deckled_rim_is_skipped():
    photo = make_photo(900, 880, seed=6)
    img, truth = framed(photo, (240, 237, 228), (70, 250, 52, 52), texture=2.0, seed=4)
    H, W = img.shape[:2]
    rng = np.random.default_rng(5)
    for side in range(4):
        n = W if side < 2 else H
        prof = np.clip(np.cumsum(rng.normal(0, 1.2, n)) % 7 + rng.integers(0, 3, n), 0, 9).astype(int)
        for i, d in enumerate(prof):
            if side == 0:
                img[:d, i] = 70
            elif side == 1:
                img[H - d:, i] = 70
            elif side == 2:
                img[i, :d] = 70
            else:
                img[i, W - d:] = 70
    band = detect.detect_band(img)
    assert band.found
    for a, b in zip(band.photo_rect, truth):
        assert abs(a - b) <= 1, (band.photo_rect, truth)


# =============================================================================
# 3. borderless photo with a white wall at the bottom
# =============================================================================

def test_white_wall_is_not_a_caption_band():
    W, H = 1500, 1100
    img = (make_photo(W, H, seed=12).astype(np.float32) * 0.7)
    top = int(H * 0.78)
    xx = np.linspace(0, 1, W, dtype=np.float32)
    wall = 236 - 10 * xx[None, :, None] + np.zeros((H - top, 1, 3), np.float32)
    wall += np.random.default_rng(1).normal(0, 1.2, wall.shape).astype(np.float32)
    img[top:] = wall
    # a light switch in the wall
    cv2.rectangle(img, (1100, top + 70), (1150, top + 160), (225, 225, 222), -1)
    cv2.rectangle(img, (1118, top + 100), (1132, top + 135), (200, 200, 198), -1)
    img = np.clip(img, 0, 255).astype(np.uint8)
    ex = analyze_existing(img, None, {}, run_ocr=HAS_TESS)   # info is only read for a metadata record
    assert ex["case"] is None, (ex["case"], ex.get("text"))
    assert ex.get("hints") and any("left alone" in w for w in ex["warnings"])
    assert not ex["blocks"]
    assert ex["band"]["photo_rect"] == [0, 0, W, H]


# =============================================================================
# 4. apostrophes / i-dots / commas; dust specks
# =============================================================================

def test_apostrophes_and_dots_stay_in_their_line():
    photo = make_photo(1000, 900, seed=31)
    text = ["Grandma's kitchen, Jim's 'n' Sue's", "iris & lilies, i.e. July"]
    img, truth = framed(photo, (250, 250, 250), (40, 260, 40, 40), text, font_px=52,
                        text_color=(20, 20, 20))
    band = detect.detect_band(img)
    lines = lines_of(detect.find_text(img, band))
    assert len(lines) == 2, [ln.box for ln in lines]
    if HAS_TESS:
        res = analyze(img)
        got = res["text"].split("\n")
        assert len(got) == 2 and "|" not in res["text"], got


FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
BUNDLED = {"inter": os.path.join(FONTS, "inter", "Inter[opsz,wght].ttf"),
           "source-sans-3": os.path.join(FONTS, "source-sans-3", "SourceSans3[wght].ttf"),
           "noto-sans": os.path.join(FONTS, "noto-sans", "NotoSans[wdth,wght].ttf")}


@pytest.mark.parametrize("px", [36, 52])
@pytest.mark.parametrize("font", sorted(BUNDLED))
def test_comma_tail_stays_in_line_without_descenders(font, px):
    # the first line has no g/j/p/q/y, so the comma tail is its lowest ink; in
    # Inter / Source Sans (and Arial, Calibri ...) it used to split off as a line
    photo = make_photo(1000, 900, seed=31)
    text = ["Grandma's kitchen, Jim's 'n' Sue's", "iris & lilies, i.e. July"]
    img, _ = framed(photo, (250, 250, 250), (40, 260, 40, 40), text, font_px=px,
                    text_color=(20, 20, 20), font_path=BUNDLED[font])
    band = detect.detect_band(img)
    lines = lines_of(detect.find_text(img, band))
    assert len(lines) == 2, [ln.box for ln in lines]
    ex = analyze(img, run_ocr=False)
    assert sum(len(b["lines"]) for b in ex["blocks"]) == 2, ex["blocks"]


@pytest.mark.parametrize("second,px2", [("1952", 52), ("1952", 24), ("photo by Ed", 22)])
def test_short_second_line_stays_separate(second, px2):
    photo = make_photo(1000, 900, seed=33)
    img, _ = framed(photo, (250, 250, 250), (40, 260, 40, 40), [], font_px=52)
    W = img.shape[1]
    draw_lines(img, (0, 940, W, 150), ["Grandma's kitchen"], 52, (20, 20, 20), font_path=BUNDLED["inter"])
    draw_lines(img, (0, 1080, W, 80), [second], px2, (20, 20, 20), font_path=BUNDLED["inter"])
    band = detect.detect_band(img)
    lines = sorted(lines_of(detect.find_text(img, band)), key=lambda ln: ln.box[1])
    assert len(lines) == 2, [ln.box for ln in lines]
    assert lines[0].box[3] <= 1.2 * 52 and lines[1].box[3] <= 1.2 * px2, [ln.box for ln in lines]


def test_dust_does_not_stretch_lines():
    photo = make_photo(1000, 900, seed=32)
    img, truth = framed(photo, (248, 248, 248), (40, 260, 40, 40), ["Summer at the lake"], font_px=56,
                        text_color=(25, 25, 25))
    x, y, w, h = truth
    rng = np.random.default_rng(3)
    for _ in range(40):  # specks above / below the caption line
        cx, cy = int(rng.uniform(60, 940)), int(rng.choice([rng.uniform(y + h + 15, y + h + 70),
                                                              rng.uniform(y + h + 190, y + h + 245)]))
        cv2.circle(img, (cx, cy), int(rng.uniform(1, 3)), (60, 60, 60), -1, lineType=cv2.LINE_AA)
    band = detect.detect_band(img)
    lines = lines_of(detect.find_text(img, band))
    assert len(lines) == 1
    assert lines[0].box[3] <= 1.35 * 56, lines[0].box
    if HAS_TESS:
        res = analyze(img)
        assert res["text"].split() == ["Summer", "at", "the", "lake"], res["text"]


# =============================================================================
# 5. caption split around punctuation
# =============================================================================

def test_punctuation_split_is_one_line_and_hyphen_is_erased():
    photo = make_photo(1100, 900, seed=41)
    img, truth = framed(photo, (255, 255, 255), (150, 30, 30, 30), [], font_px=56)
    draw_lines(img, (0, 0, img.shape[1], 150), ["SUMMER 1978 - LAKE MERCED"], 56, (20, 20, 20),
               font_path=SANS_BOLD)
    band = detect.detect_band(img)
    assert band.photo_rect == truth
    blocks = detect.find_text(img, band)
    assert len(blocks) == 1 and len(blocks[0].lines) == 1, [[ln.box for ln in b.lines] for b in blocks]
    mask = erase.build_mask(img, band, blocks)
    out = erase.erase_in_place(img, mask, band)
    assert (out[:truth[1]] == 255).all(), "text (the hyphen?) left behind"
    if HAS_TESS:
        res = analyze(img)
        assert res["text"].replace(" ", "") == "SUMMER1978-LAKEMERCED", res["text"]


# =============================================================================
# 6. erase ROI (preview) matches the full erase
# =============================================================================

@pytest.mark.parametrize("method", ["flat", "inpaint"])
def test_erase_roi_matches_full(method):
    (img, M), rect = _polaroid_scan(0.0)
    band = detect.detect_band(img)
    mask = erase.build_mask(img, band, detect.find_text(img, band))
    full = erase.erase_in_place(img, mask, band, method=method)
    ys, xs = np.nonzero(mask)
    pad = 3 * erase.INPAINT_RADIUS + 4 + 16
    y0, y1 = max(0, ys.min() - pad), min(img.shape[0], ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(img.shape[1], xs.max() + pad + 1)
    sub = erase.erase_in_place(img[y0:y1, x0:x1], mask[y0:y1, x0:x1],
                               erase.shift_band(band, x0, y0, x1 - x0, y1 - y0), method=method)
    diff = np.abs(sub.astype(int) - full[y0:y1, x0:x1].astype(int))
    assert diff.max() <= (0 if method == "flat" else 3), diff.max()
