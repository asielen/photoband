"""Acceptance tests for band detection, text finding, OCR, style estimate, case D
and erase-in-place (photoband.detect / photoband.ocr / photoband.erase).

All fixtures are synthetic and built here; a few are written to
tests/_artifacts/ as PNGs so humans can look at them.
"""
from __future__ import annotations

import os
import re
import sys
import time

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from conftest import best_time
from photoband import detect, erase, ocr
from photoband.existing import analyze_existing


def analyze(img, run_ocr=True):
    """The production analysis of an opened photo (no metadata record)."""
    return analyze_existing(np.asarray(img), None, {}, run_ocr=run_ocr)

ART = os.path.join(os.path.dirname(__file__), "_artifacts")


# =============================================================================
# fixture helpers
# =============================================================================

# The repo's bundled fonts, never the machine's: system-font lookups picked
# DejaVu on Linux, Arial on Windows and whatever fontconfig listed first on macOS
# (a Courier), so the same test drew different text on each OS and CI caught
# failures local runs could not.  A spec is a path or (path, weight) for a
# variable font.
FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
SANS = os.path.join(FONTS, "noto-sans", "NotoSans[wdth,wght].ttf")
SANS_BOLD = (SANS, 700)
ITALIC = os.path.join(FONTS, "noto-serif", "NotoSerif-Italic[wdth,wght].ttf")
MONO = os.path.join(FONTS, "courier-prime", "CourierPrime-Regular.ttf")
MONO_BOLD = os.path.join(FONTS, "courier-prime", "CourierPrime-Bold.ttf")
HAS_TESS = ocr.tesseract_path() is not None


def _font(spec, size):
    path, weight = spec if isinstance(spec, tuple) else (spec, None)
    font = ImageFont.truetype(path, size)
    if weight is not None:
        font.set_variation_by_axes([weight if ax["name"] in (b"Weight", "Weight") else ax["default"]
                                    for ax in font.get_variation_axes()])
    return font


def save_artifact(name, arr):
    os.makedirs(ART, exist_ok=True)
    a = arr
    if a.dtype == np.uint16:
        a = (a // 257).astype(np.uint8)
    Image.fromarray(a[:, :, :3] if a.shape[2] >= 3 else a[:, :, 0]).save(os.path.join(ART, name))


def make_photo(w, h, seed=0, lo=25, hi=225, noise=3.0, grid=7):
    """Natural-ish photo: smooth random colour blobs plus sensor noise."""
    rng = np.random.default_rng(seed)
    small = rng.uniform(lo, hi, (grid, grid, 3)).astype(np.float32)
    img = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    # some mid-frequency detail too
    mid = cv2.resize(rng.normal(0, 18, (grid * 6, grid * 6, 3)).astype(np.float32), (w, h),
                     interpolation=cv2.INTER_CUBIC)
    img = img + mid + rng.normal(0, noise, (h, w, 3)).astype(np.float32)
    return np.clip(img, lo - 10, hi + 10).astype(np.uint8)


def draw_lines(img, box, lines, font_px, color, align="center", font_path=None, spacing=1.3):
    """Draw text lines into img (uint8 RGB, in place) inside box (x, y, w, h)."""
    font_path = font_path or SANS
    x, y, w, h = box
    pil = Image.fromarray(img)
    d = ImageDraw.Draw(pil)
    font = _font(font_path, font_px)
    total = font_px * spacing * (len(lines) - 1) + font_px
    ty = y + (h - total) / 2
    for i, s in enumerate(lines):
        l, t, r, b = d.textbbox((0, 0), s, font=font)
        tw = r - l
        if align == "center":
            tx = x + (w - tw) / 2
        elif align == "left":
            tx = x + 0.06 * w
        else:
            tx = x + w - 0.06 * w - tw
        d.text((int(tx), int(ty + i * font_px * spacing)), s, font=font, fill=tuple(color))
    img[...] = np.asarray(pil)
    return img


def framed(photo, color, sides, lines=(), font_px=None, text_color=(15, 15, 15), align="center",
           font_path=None, texture=0.0, gradient=0.0, seed=1):
    """Put ``photo`` on a border of ``color``.  sides = (top, bottom, left, right) px.
    Text goes in the bottom band.  Returns (image, truth photo rect)."""
    t, b, l, r = sides
    ph, pw = photo.shape[:2]
    H, W = ph + t + b, pw + l + r
    img = np.empty((H, W, 3), np.uint8)
    img[:] = np.array(color, np.uint8)
    if lines:
        font_px = font_px or max(12, int(b * 0.2))
        draw_lines(img, (0, t + ph, W, b), list(lines), font_px, text_color, align, font_path)
    if texture or gradient:
        rng = np.random.default_rng(seed)
        f = img.astype(np.float32)
        gx = np.linspace(-gradient, gradient, W, dtype=np.float32)[None, :, None]
        gy = np.linspace(-gradient / 2, gradient / 2, H, dtype=np.float32)[:, None, None]
        grain = rng.normal(0, texture, (H, W, 1)).astype(np.float32)
        grain = grain + rng.normal(0, texture * 0.3, (H, W, 3)).astype(np.float32)
        img = np.clip(f + gx + gy + grain, 0, 255).astype(np.uint8)
    img[t:t + ph, l:l + pw] = photo
    return img, (l, t, pw, ph)


def polaroid_sides(pw):
    return (round(0.076 * pw), round(0.28 * pw), round(0.057 * pw), round(0.057 * pw))


def sky_photo(w, h, seed=3):
    """Top 30 % is a smooth 250 -> 200 gradient with sigma 2 noise, blobs below."""
    rng = np.random.default_rng(seed)
    base = make_photo(w, h, seed).astype(np.float32)
    sky_h = int(0.3 * h)
    ramp = np.linspace(250, 200, sky_h, dtype=np.float32)[:, None, None]
    sky = np.repeat(np.repeat(ramp, w, axis=1), 3, axis=2)
    sky[:, :, 2] += 2  # a touch of blue
    sky += rng.normal(0, 2.0, sky.shape).astype(np.float32)
    # blend into the blobs over 40 rows
    base[:sky_h] = sky
    blend = 40
    for i in range(blend):
        a = 1 - i / blend
        base[sky_h + i] = a * sky[-1] + (1 - a) * base[sky_h + i]
    return np.clip(base, 0, 255).astype(np.uint8)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def assert_words_read(expected_lines, got_text):
    got = [_norm(w) for w in got_text.split()]
    got = [g for g in got if g]
    for line in expected_lines:
        for w in line.split():
            n = _norm(w)
            if not n:
                continue
            assert any(_lev(n, g) <= 1 for g in got), f"word {w!r} not read in {got_text!r}"


def lines_of(blocks):
    return [ln for b in blocks for ln in b.lines]


# =============================================================================
# 1-2. Polaroid, white, 2 lines of text
# =============================================================================

POLA_TEXT = ["Summer at Lake Tahoe", "June 1998 with Grandma Rose"]


@pytest.fixture(scope="module")
def polaroid():
    photo = make_photo(1000, 980, seed=11)
    img, truth = framed(photo, (255, 255, 255), polaroid_sides(1000), POLA_TEXT, font_px=46)
    save_artifact("polaroid_white.png", img)
    return img, truth


def test_polaroid_white_rect_lines_and_ocr(polaroid):
    img, truth = polaroid
    band = detect.detect_band(img)
    assert band.found
    assert band.photo_rect == truth
    assert {b["side"] for b in band.bands} == {"top", "bottom", "left", "right"}
    assert band.band_color == (255, 255, 255) and band.band_color_hex == "#ffffff"
    assert not band.textured
    assert band.confidence > 0.5
    blocks = detect.find_text(img, band)
    lines = lines_of(blocks)
    assert len(lines) == 2
    assert all(ln.box[1] >= truth[1] + truth[3] for ln in lines)
    if HAS_TESS:
        ocr.recognize_blocks(img, blocks)
        assert_words_read(POLA_TEXT, " ".join(ln.text for ln in lines))
        for ln in lines:
            assert 0 < ln.confidence <= 1 and ln.words
            for w in ln.words:  # word boxes are full-res and inside the line (with slack)
                x, y, ww, hh = w["box"]
                assert ln.box[1] - 20 <= y <= ln.box[1] + ln.box[3] + 20
    js = band.to_json()
    assert js["photo_rect"] == list(truth)


def test_polaroid_uint16(polaroid):
    img, truth = polaroid
    img16 = img.astype(np.uint16) * 257
    band = detect.detect_band(img16)
    assert band.found and band.photo_rect == truth
    assert band.band_color == (65535, 65535, 65535)
    assert band.band_color_hex == "#ffffff"


def test_rgba_and_gray_inputs(polaroid):
    img, truth = polaroid
    rgba = np.concatenate([img, np.full(img.shape[:2] + (1,), 255, np.uint8)], axis=2)
    assert detect.detect_band(rgba).photo_rect == truth
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)[:, :, None]
    b = detect.detect_band(gray)
    assert b.photo_rect == truth and len(b.band_color) == 1 and b.band_color_hex == "#ffffff"


# =============================================================================
# 3. coloured band, bottom only, white text
# =============================================================================

def test_colored_band_bottom_only():
    photo = make_photo(1200, 800, seed=21)
    lines = ["Family reunion 1987"]
    img, truth = framed(photo, (0x2a, 0x4d, 0x8f), (0, 170, 0, 0), lines, font_px=56,
                        text_color=(255, 255, 255))
    save_artifact("colored_band.png", img)
    band = detect.detect_band(img)
    assert band.found and band.photo_rect == truth
    assert [b["side"] for b in band.bands] == ["bottom"]
    assert band.band_color_hex == "#2a4d8f"
    blocks = detect.find_text(img, band)
    assert len(lines_of(blocks)) == 1
    if HAS_TESS:
        ocr.recognize_blocks(img, blocks)
        assert_words_read(lines, " ".join(ln.text for ln in lines_of(blocks)))


def test_dark_band():
    photo = make_photo(900, 700, seed=22)
    img, truth = framed(photo, (0x11, 0x11, 0x11), (40, 140, 40, 40), ["Paris 1964"], font_px=40,
                        text_color=(235, 235, 235))
    band = detect.detect_band(img)
    assert band.found and band.photo_rect == truth
    assert band.band_color_hex == "#111111"


# =============================================================================
# 4. off-white museum card
# =============================================================================

def test_offwhite_museum_card():
    pw, ph = 1100, 850
    photo = make_photo(pw, ph, seed=31)
    s = round(0.04 * pw)
    img, truth = framed(photo, (0xF4, 0xF1, 0xEA), (s, round(0.12 * pw), s, s),
                        ["Harbour at dawn, c. 1920"], font_px=38, text_color=(60, 55, 50), align="left")
    save_artifact("museum_card.png", img)
    band = detect.detect_band(img)
    assert band.found and band.photo_rect == truth
    assert band.band_color_hex == "#f4f1ea"


def test_one_side_and_uneven_widths():
    photo = make_photo(800, 900, seed=32)
    img, truth = framed(photo, (250, 250, 250), (0, 0, 70, 0))
    assert detect.detect_band(img).photo_rect == truth
    img, truth = framed(photo, (240, 240, 240), (13, 95, 27, 51), ["x 1999"], font_px=30)
    assert detect.detect_band(img).photo_rect == truth


# =============================================================================
# 5. near-white sky
# =============================================================================

def test_sky_without_border_is_not_a_band():
    photo = sky_photo(1200, 900)
    save_artifact("sky_noborder.png", photo)
    band = detect.detect_band(photo)
    assert (not band.found) or band.photo_rect == (0, 0, 1200, 900)


def test_sky_with_polaroid_border():
    photo = sky_photo(1000, 900)
    img, truth = framed(photo, (255, 255, 255), polaroid_sides(1000), ["Big Sur"], font_px=50)
    save_artifact("sky_polaroid.png", img)
    band = detect.detect_band(img)
    assert band.found and band.photo_rect == truth


def test_sky_without_border_large_proxy_downscale():
    photo = sky_photo(1200, 900)
    big = cv2.resize(photo, (3600, 2700), interpolation=cv2.INTER_LINEAR)
    band = detect.detect_band(big)
    assert (not band.found) or band.photo_rect == (0, 0, 3600, 2700)


# =============================================================================
# 6. textured paper with handwriting-like text
# =============================================================================

@pytest.fixture(scope="module")
def paper():
    photo = make_photo(1000, 1000, seed=41)
    img, truth = framed(photo, (236, 228, 208), polaroid_sides(1000), ["Aunt Mary, Easter"],
                        font_px=58, text_color=(40, 45, 90), font_path=ITALIC, texture=4.0, gradient=5.0)
    save_artifact("paper_handwriting.png", img)
    return img, truth


def test_textured_paper(paper):
    img, truth = paper
    band = detect.detect_band(img)
    assert band.found and band.textured
    assert 2.5 < band.noise < 7
    for a, b in zip(band.photo_rect, truth):
        assert abs(a - b) <= 2
    lines = lines_of(detect.find_text(img, band))
    assert len(lines) >= 1
    res = analyze(img, run_ocr=HAS_TESS)
    assert res["case"] == "C"


# =============================================================================
# 7. no border
# =============================================================================

def test_no_border_natural_photo():
    for seed in range(5):
        photo = make_photo(1500, 1000, seed=100 + seed)
        band, dt = best_time(lambda: detect.detect_band(photo))
        assert not band.found, seed
        assert band.photo_rect == (0, 0, 1500, 1000)
        assert dt < 0.5, dt


# =============================================================================
# 8. erase in place
# =============================================================================

def test_erase_flat(polaroid):
    img, truth = polaroid
    band = detect.detect_band(img)
    blocks = detect.find_text(img, band)
    mask = erase.build_mask(img, band, blocks)
    x, y, w, h = truth
    assert not mask[y:y + h, x:x + w].any()
    out = erase.erase_in_place(img, mask, band)
    assert out.dtype == img.dtype and out is not img
    assert np.array_equal(out[y:y + h, x:x + w], img[y:y + h, x:x + w])
    assert (out[mask] == 255).all()
    bottom = out[y + h:]
    assert (bottom == 255).all(), "text left behind"


def test_erase_flat_uint16_and_brush(polaroid):
    img, truth = polaroid
    img16 = img.astype(np.uint16) * 257
    band = detect.detect_band(img16)
    blocks = detect.find_text(img16, band)
    x, y, w, h = truth
    add = np.zeros(img.shape[:2], bool)
    add[5:15, 5:15] = True                 # brush on the border
    add[y + 10:y + 20, x + 10:x + 20] = True  # brush on the photo: must be ignored
    rem = np.zeros(img.shape[:2], bool)
    first = lines_of(blocks)[0].box
    rem[first[1] - 5:first[1] + first[3] + 5, :] = True  # keep line 1
    mask = erase.build_mask(img16, band, blocks, add_mask=add, remove_mask=rem)
    assert mask[5:15, 5:15].all() and not mask[y:y + h, x:x + w].any()
    out = erase.erase_in_place(img16, mask, band, method="flat")
    assert out.dtype == np.uint16
    assert np.array_equal(out[y:y + h, x:x + w], img16[y:y + h, x:x + w])
    assert (out[mask] == 65535).all()
    # line 1 kept, line 2 erased
    fx, fy, fw, fh = first
    assert (out[fy:fy + fh, fx:fx + fw] < 30000).any()
    second = lines_of(blocks)[1].box
    sx, sy, sw, sh = second
    assert (out[sy:sy + sh, sx:sx + sw] == 65535).all()


def _check_inpaint(img, out, mask, truth, unit=1.0):
    x, y, w, h = truth
    assert np.array_equal(out[y:y + h, x:x + w], img[y:y + h, x:x + w])
    band_region = np.zeros(mask.shape, bool)
    band_region[y + h + 5:, :] = True
    ring = cv2.dilate(mask.astype(np.uint8), np.ones((25, 25), np.uint8)).astype(bool) & ~mask & band_region
    paper = np.median(out[ring].astype(np.float64) / unit, axis=0)
    erased = out[mask].astype(np.float64) / unit
    mad = np.abs(erased - paper).mean()
    assert mad < 6, mad
    assert abs(erased.mean() - paper.mean()) < 6
    # no ink left: nothing much darker than the paper in the erased area
    assert np.percentile(erased.mean(axis=1), 0.5) > paper.mean() - 30


def test_erase_textured_inpaint(paper):
    img, truth = paper
    band = detect.detect_band(img)
    blocks = detect.find_text(img, band)
    mask = erase.build_mask(img, band, blocks)
    assert mask.sum() > 500
    out = erase.erase_in_place(img, mask, band)  # auto -> inpaint
    save_artifact("paper_erased.png", out)
    _check_inpaint(img, out, mask, truth)


def test_erase_textured_uint16(paper):
    img, truth = paper
    img16 = img.astype(np.uint16) * 257 + 100
    band = detect.detect_band(img16)
    assert band.textured
    blocks = detect.find_text(img16, band)
    mask = erase.build_mask(img16, band, blocks)
    out = erase.erase_in_place(img16, mask, band, method="inpaint")
    assert out.dtype == np.uint16
    _check_inpaint(img16, out, mask, truth, unit=257.0)


# =============================================================================
# 9. case D: date stamp over the photo
# =============================================================================

def _stamp(img, text="'98 6 14", size=44, glow=True):
    h, w = img.shape[:2]
    font = _font(MONO_BOLD, size)
    layer = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(layer)
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    x, y = w - (r - l) - int(0.05 * w), h - (b - t) - int(0.06 * h)
    d.text((x - l, y - t), text, font=font, fill=255)
    core = np.asarray(layer, np.float32)[:, :, None] / 255
    out = img.astype(np.float32)
    orange = np.array([255, 140, 25], np.float32)
    if glow:
        halo = np.asarray(layer.filter(ImageFilter.GaussianBlur(4)), np.float32)[:, :, None] / 255
        out = out * (1 - 0.5 * halo) + orange * 0.5 * halo
    out = out * (1 - core) + orange * core
    return np.clip(out, 0, 255).astype(np.uint8), (x, y, r - l, b - t)


def _overlap(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


@pytest.mark.skipif(not HAS_TESS, reason="tesseract not installed")
def test_case_d_date_stamp():
    photo = make_photo(1200, 900, seed=51, lo=20, hi=150)
    img, sbox = _stamp(photo)
    save_artifact("date_stamp.png", img)
    boxes, dt = best_time(lambda: detect.detect_text_over_photo(img, (0, 0, 1200, 900)), repeats=2)
    assert dt < 2.0, dt
    assert any(_overlap(b, sbox) for b in boxes), boxes


@pytest.mark.skipif(not HAS_TESS, reason="tesseract not installed")
def test_case_d_plain_photos_have_no_boxes():
    for seed in range(4):
        photo = make_photo(1200, 900, seed=60 + seed)
        boxes, dt = best_time(lambda: detect.detect_text_over_photo(photo, (0, 0, 1200, 900)), repeats=2)
        assert dt < 2.0, dt
        assert boxes == [], (seed, boxes)


# =============================================================================
# 10. style estimate
# =============================================================================

@pytest.mark.parametrize("align", ["center", "left", "right"])
def test_style_estimate(align):
    photo = make_photo(1000, 900, seed=71)
    size = 48
    img, truth = framed(photo, (255, 255, 255), polaroid_sides(1000),
                        ["Grandpa Joe at the farm", "Iowa 1952"], font_px=size, text_color=(20, 20, 20),
                        align=align)
    band = detect.detect_band(img)
    blocks = detect.find_text(img, band)
    st = detect.estimate_style(img, band, blocks)
    assert abs(st.font_size_px - size) <= 0.2 * size, st
    assert st.align == align, st
    r, g, b = (int(st.color_hex[i:i + 2], 16) for i in (1, 3, 5))
    assert max(r, g, b) < 60, st
    assert 1.0 < st.line_height < 1.6, st
    assert st.to_json()["align"] == align


# =============================================================================
# 11. timing, analyze
# =============================================================================

def test_timing_big_uint16():
    W, H = 6000, 8000
    pw = round(W / 1.114)
    t, b, l = round(0.076 * pw), round(0.28 * pw), round(0.057 * pw)
    ph = H - t - b
    small = make_photo(pw // 8, ph // 8, seed=81)
    photo = cv2.resize(small, (pw, ph), interpolation=cv2.INTER_LINEAR)
    img = np.full((H, W, 3), 65535, np.uint16)
    img[t:t + ph, l:l + pw] = photo.astype(np.uint16) * 257
    band, dt = best_time(lambda: detect.detect_band(img), repeats=2)
    assert band.found and band.photo_rect == (l, t, pw, ph)
    assert dt < 3.0, dt


@pytest.mark.skipif(not HAS_TESS, reason="tesseract not installed")
def test_analyze_3000px():
    pw = 2600
    photo = cv2.resize(make_photo(650, 500, seed=91), (pw, 2000), interpolation=cv2.INTER_LINEAR)
    img, truth = framed(photo, (255, 255, 255), polaroid_sides(pw), POLA_TEXT, font_px=110)
    t0 = time.perf_counter()
    res = analyze(img)
    dt = time.perf_counter() - t0
    # the production analysis also searches for the hidden marker (about 1.5x the
    # detection + OCR time on an unmarked photo), which the old test-only copy skipped
    assert dt < 9.0, dt
    assert res["band"]["found"] and res["band"]["photo_rect"] == list(truth)
    # the engine that read it is the first available one: tesseract only where no OS engine
    # (Apple Vision, Windows.Media.Ocr) is present
    assert res["case"] == "B" and res["engine"] == (ocr.engines() or [None])[0]
    assert_words_read(POLA_TEXT, res["text"].replace("\n", " "))
    assert res["style"]["align"] == "center"
    assert res["textOverPhoto"] == []
    import json
    json.dumps(res)


def test_engines_and_empty_recognize():
    e = ocr.engines()
    assert isinstance(e, list)
    if HAS_TESS:
        assert e[-1] == "tesseract"
    r = ocr.recognize(np.full((40, 200, 3), 255, np.uint8))
    assert set(r) >= {"text", "confidence", "words", "engine"}
    assert r["text"] == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OCR is Windows only")
def test_windows_ocr_reads_a_line():
    # the binding changed between pywinrt versions (create_copy_with_alpha_from_buffer):
    # call the engine directly, so a system Tesseract can't hide a broken Windows OCR
    if not ocr._winocr_available():
        pytest.skip("Windows OCR is not available here")
    font = ImageFont.truetype(os.path.join(FONTS, "inter", "Inter[opsz,wght].ttf"), 40)
    im = Image.new("RGB", (520, 80), "white")
    ImageDraw.Draw(im).text((12, 14), "Lake Merced 1962", font=font, fill="black")
    r = ocr._winocr_recognize(np.asarray(im))
    assert "Merced" in r["text"] and "1962" in r["text"], r["text"]
