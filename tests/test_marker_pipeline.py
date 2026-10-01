"""Hidden band marker through the real save pipeline (save.save() outputs for the
five built-in template geometries): erase-in-place scrubbing, re-captioning,
geometry validation, payload capacity, the JPEG/resize acceptance transform and
invisibility."""
from __future__ import annotations

import io
import subprocess

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from conftest import _font
from photoband import marker
from photoband.composite import Tile
from photoband.existing import _erase_inputs_for_record, analyze_existing
from photoband.exiftool import get as et_get
from photoband.imageio import load_upright
from photoband.save import SaveRequest, decode_marker_payload, save
from photoband.settings import load_settings

# name: ((side, top, bottom as a fraction of the photo width), band, text colour, hairline divider)
TEMPLATES = {
    "classic": ((0.057, 0.076, 0.28), "#ffffff", "#23262e", False),
    "bottom": ((0.0, 0.0, 0.10), "#ffffff", "#222222", False),
    "archive": ((0.02, 0.02, 0.10), "#ffffff", "#333333", True),
    "museum": ((0.04, 0.04, 0.14), "#F4F1EA", "#1b1b1b", False),
    "dark": ((0.0, 0.0, 0.10), "#111111", "#ffffff", False),
}
LINES = ["Summer picnic at Lake Merced, July 1962",
         "Left to right: Anna, Ben, Carla and Uncle Dave",
         "Archive box 7 - print 12"]


def natural_photo(w, h, seed=0):
    rng = np.random.default_rng(seed)
    small = rng.random((max(2, h // 64), max(2, w // 64), 3)).astype(np.float32)
    img = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img += 0.3 * np.sin(xx[..., None] / (w / 7) + np.array([0, 1, 2])) * np.cos(yy[..., None] / (h / 5))
    for _ in range(12):
        cx, cy, r = rng.integers(0, w), rng.integers(0, h), rng.integers(w // 40, w // 8)
        cv2.circle(img, (int(cx), int(cy)), int(r), tuple(float(v) for v in rng.random(3)), -1, cv2.LINE_AA)
    img = (img - img.min()) / (img.max() - img.min())
    img += rng.normal(0, 0.015, img.shape).astype(np.float32)
    return np.round(np.clip(img, 0, 1) * 255).astype(np.uint8)


def text_tile(area, lines, color, size=None):
    fs = size or max(8, round(area[3] / (len(lines) * 1.35)))
    tile = Image.new("RGBA", (area[2], area[3]), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    for i, t in enumerate(lines):
        d.text((0, int(i * fs * 1.3)), t, font=_font(fs), fill=rgb + (255,))
    buf = io.BytesIO()
    tile.save(buf, "PNG")
    return Tile(area[0], area[1], buf.getvalue())


def make_layout(pw, ph, tpl, lines=LINES):
    (fs_, ft, fb), band, color, divider = TEMPLATES[tpl]
    side, top, bottom = round(pw * fs_), round(pw * ft), round(pw * fb)
    W, H = pw + 2 * side, ph + top + bottom
    fills = [{"rect": [0, 0, W, H], "color": band}]
    protect = []
    y_text = top + ph + round(bottom * 0.15)
    if divider:
        dh = max(1, round(pw * 0.0008))
        r = [0, top + ph + round(bottom * 0.06), W, dh]
        fills.append({"rect": r, "color": "#9a9a9a"})
        protect.append(r)
        y_text = r[1] + dh + round(bottom * 0.1)
    area = [side + round(pw * 0.03), y_text, round(pw * 0.94), top + ph + bottom - y_text - round(bottom * 0.1)]
    layout = {"version": 1, "mode": "band", "sourceRect": [0, 0, pw, ph], "canvas": [W, H],
              "photoRect": [side, top, pw, ph], "fills": fills, "bandColor": band, "protect": protect,
              "textAreas": [{"id": "a0", "rect": area}],
              "runs": [{"block": "people", "text": t} for t in lines], "textColors": [color]}
    return layout, [text_tile(area, lines, color)]


def state_for(text):
    return {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
            "blocks": [{"id": "people", "text": text, "custom": True}], "overrides": {}}


def do_save(src, dest, layout, tiles, text="\n".join(LINES), embed=None, erase=None, case=None):
    req = SaveRequest(path=src, mode="copyAs", layout=layout, tiles=tiles, state=state_for(text),
                      settings=load_settings(), dest_path=dest, on_exists="overwrite", embed_marker=embed,
                      erase=erase, case=case)
    return save(req)


def load(p):
    return load_upright(p)[0]


def strip_metadata(path):
    subprocess.run(["exiftool", "-all=", "-overwrite_original", path], check=True, capture_output=True)


def jpeg(a, q):
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, "JPEG", quality=q)
    return np.asarray(Image.open(io.BytesIO(buf.getvalue()))).copy()


def saved(tmp_path, tpl, pw, seed=0, lines=LINES, name=None):
    ph = pw * 3 // 4
    src = str(tmp_path / f"src_{tpl}_{pw}.png")
    Image.fromarray(natural_photo(pw, ph, seed)).save(src)
    lay, tiles = make_layout(pw, ph, tpl, lines)
    out = str(tmp_path / (name or f"{tpl}_{pw}.png"))
    r = do_save(src, out, lay, tiles, text="\n".join(lines))
    assert r.ok, r.error
    return r, lay, out


# --------------------------------------------------------------------------
# erase in place: an old marker never survives (#1, #2)
# --------------------------------------------------------------------------
OLD_TEXT = "Private note: Grandpa Robert, 1944"


def _erase_job(tmp_path, embed, new_text):
    """A captioned PNG with a marker and payload holding OLD_TEXT, stripped of
    metadata, then saved again in erase-in-place mode."""
    r, lay, out = saved(tmp_path, "classic", 1200, lines=[OLD_TEXT])
    assert r.marker["robust"] and r.marker["payload"], r.marker
    strip_metadata(out)
    a = load(out)
    rect = tuple(lay["photoRect"])
    old = marker.read(a, rect)
    assert old is not None and OLD_TEXT in decode_marker_payload(old.payload)["blocks"][0]["text"]
    ana = analyze_existing(a, load_upright(out)[1], et_get().read_json(out), run_ocr=False)
    assert ana["source"] == "marker+payload"
    ein = _erase_inputs_for_record(a, list(rect))
    H, W = a.shape[:2]
    area = lay["textAreas"][0]["rect"]
    elay = {"version": 1, "mode": "erase", "sourceRect": list(rect), "photoRect": list(rect), "canvas": [W, H],
            "fills": [], "bandColor": "#ffffff", "protect": [], "textAreas": [{"id": "a0", "rect": area}],
            "runs": [{"block": "people", "text": new_text}], "textColors": ["#23262e"]}
    dest = str(tmp_path / "erased.png")
    res = do_save(out, dest, elay, [text_tile(area, [new_text], "#23262e", size=40)], text=new_text, embed=embed,
                  erase={"band": ein["band"], "photoRect": list(rect), "blocks": ein["blocks"], "grow": 2}, case="A")
    return res, old, rect, dest


def _old_units_readable(img, old, rect):
    f = marker._unpack_data(old.raw, 2)
    return marker._read_units(marker._as3(img), 3, f, rect) is not None


def test_remove_marker_in_erase_mode_leaves_no_old_marker(tmp_path):
    res, old, rect, dest = _erase_job(tmp_path, embed=False, new_text=OLD_TEXT)
    assert res.ok, res.error
    assert res.marker["reason"] == "removed on request"
    out = load(dest)
    assert marker.read(out, rect) is None
    assert marker.read(out, rect, sweep=True) is None
    assert not _old_units_readable(out, old, rect), "old payload (old caption) still in the file"


def test_recaption_in_erase_mode_replaces_marker(tmp_path):
    res, old, rect, dest = _erase_job(tmp_path, embed=None, new_text="Zed and Yolanda, 1999")
    assert res.ok, res.error
    assert res.marker["robust"] and res.marker["payload"] and res.marker.get("verified"), res.marker
    out = load(dest)
    mk = marker.read(out, rect)
    assert mk is not None and mk.photo_rect == rect
    p = decode_marker_payload(mk.payload)
    assert p["blocks"][0]["text"] == "Zed and Yolanda, 1999"
    assert p["layout"]["mode"] == "erase"
    assert not _old_units_readable(out, old, rect)
    # a stripped erase-mode save restores with the erase inputs, like the record path
    strip_metadata(dest)
    ana = analyze_existing(out, load_upright(dest)[1], et_get().read_json(dest), run_ocr=False)
    assert ana["source"] == "marker+payload" and ana.get("band") and "blocks" in ana


def test_scrub_textured_band_removes_v1_and_v2():
    rng = np.random.default_rng(3)
    for writer in (marker._embed_v1, marker.embed):
        h, w, rect = 1500, 1300, (50, 60, 1200, 900)
        c = np.clip(rng.normal(236, 3.0, (h, w, 3)), 0, 255).astype(np.uint8)   # paper grain
        c[60:960, 50:1250] = natural_photo(1200, 900, 1)
        r = writer(c, rect, (236, 236, 236), "ab" * 16, b"secret caption" * 40)
        assert r.robust
        assert marker.read(c, rect) is not None
        rep = marker.scrub(c, rect, (236, 236, 236))
        assert rep["found"] and not rep["flat"] and rep["removed"], rep
        assert marker.read(c, rect) is None


# --------------------------------------------------------------------------
# geometry: the stored rect is only trusted when the image agrees (#3)
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def classic1200(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("c1200")
    r, lay, out = saved(tmp, "classic", 1200, seed=3)
    return load(out), tuple(lay["photoRect"])


@pytest.mark.parametrize("crop", [1, 2, 3, 5, 8])
def test_side_crop_rejects_marker_rect(classic1200, crop):
    o, (x, y, w, h) = classic1200
    for a, exp in ((o[:, crop:], (x - crop, y, w, h)), (o[:, crop:o.shape[1] - crop], (x - crop, y, w, h))):
        a = np.ascontiguousarray(a)
        mk = marker.read(a, exp)
        assert mk is None or not mk.rect_ok or mk.photo_rect == exp, (mk.photo_rect, exp)
        # opening the file uses detection for the edge, which is exact here
        ana = analyze_existing(a, _info(a), {}, run_ocr=False)
        assert tuple(ana["band"]["photo_rect"]) == exp


def _info(a):
    from photoband.imageio import ImageInfo
    return ImageInfo(path="", format="PNG", width=a.shape[1], height=a.shape[0], channels=a.shape[2],
                     dtype=str(a.dtype), mode="RGB")


@pytest.mark.parametrize("sx,sy", [(0.5, 0.52), (1.0, 0.97)])
def test_nonuniform_resize_rejects_marker_rect(classic1200, sx, sy):
    o, rect = classic1200
    H, W = o.shape[:2]
    a = cv2.resize(o, (round(W * sx), round(H * sy)), interpolation=cv2.INTER_AREA)
    mk = marker.read(a, tuple(round(v * s) for v, s in zip(rect, (sx, sy, sx, sy))))
    assert mk is None or not mk.rect_ok


@pytest.mark.parametrize("s", [0.75, 0.5])
def test_uniform_resize_keeps_marker_rect(classic1200, s):
    o, rect = classic1200
    H, W = o.shape[:2]
    a = cv2.resize(o, (round(W * s), round(H * s)), interpolation=cv2.INTER_AREA)
    mk = marker.read(a, tuple(round(v * s) for v in rect))
    assert mk is not None and mk.rect_ok, mk
    assert all(abs(p - v * s) <= 0.51 for p, v in zip(mk.photo_rect, rect))


# --------------------------------------------------------------------------
# payload capacity on text-heavy bands (#4)
# --------------------------------------------------------------------------
def test_payload_fits_classic_3000(tmp_path):
    r, lay, out = saved(tmp_path, "classic", 3000)
    assert r.marker["robust"] and r.marker["payload"] and r.marker.get("verified"), r.marker
    a = load(out)
    mk = marker.read(a, tuple(lay["photoRect"]))
    assert decode_marker_payload(mk.payload)["blocks"][0]["text"] == "\n".join(LINES)
    # and a 14 KB payload (the spec's size) fits the same text-heavy band
    c = a.copy()
    marker.scrub(c, tuple(lay["photoRect"]), (255, 255, 255))
    big = np.random.default_rng(0).bytes(14 * 1024)
    res = marker.embed(c, tuple(lay["photoRect"]), (255, 255, 255), "cd" * 16, big)
    assert res.robust and res.payload, res
    assert marker.read(c, tuple(lay["photoRect"])).payload == big


# --------------------------------------------------------------------------
# acceptance transform: 50 % resize then JPEG q70 at 1600 px (#5)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tpl", list(TEMPLATES))
def test_resize50_then_q70_at_1600(tmp_path, tpl):
    r, lay, out = saved(tmp_path, tpl, 1600)
    assert r.marker["robust"], r.marker
    a = load(out)
    H, W = a.shape[:2]
    small = np.asarray(Image.fromarray(a).resize((W // 2, H // 2), Image.LANCZOS))
    b = jpeg(small, 70)
    rect = tuple(lay["photoRect"])
    approx = tuple(round(v / 2) + j for v, j in zip(rect, (1, -1, -1, 2)))
    mk = marker.read(b, approx)
    assert mk is not None, f"{tpl}: marker lost ({r.marker})"
    assert mk.short_hash == marker.read(a, rect).short_hash
    assert all(abs(p - v / 2) <= 1 for p, v in zip(mk.photo_rect, rect)), (mk.photo_rect, rect)


# --------------------------------------------------------------------------
# invisibility: no spectral line at the block frequency after a +3 stop stretch (#6)
# --------------------------------------------------------------------------
def spectral_peak(img: np.ndarray, period: float) -> float:
    """Largest ratio of the power at 1/period and 2/period (rows and columns)
    to the median power of the neighbouring frequencies."""
    st = img.astype(np.float64) - img.mean()
    best = 0.0
    for ax in (1, 0):
        spec = (np.abs(np.fft.rfft(st, axis=ax)) ** 2).mean(axis=1 - ax)
        n = st.shape[ax]
        for mult in (1, 2):
            k = int(round(n / period * mult))
            if k < 14 or k + 14 >= spec.size:
                continue
            near = np.r_[spec[k - 13:k - 3], spec[k + 4:k + 14]]
            best = max(best, float(spec[k - 1:k + 2].max() / np.median(near)))
    return best


def _stretched_empty_band(writer, photo_w):
    from test_marker import HASH, Case, payload_bytes
    case = Case(photo_w, text=False, seed=14)
    res = writer(case.canvas, case.rect, case.band, HASH, payload_bytes(4096))
    assert res.robust
    x, y, w, h = case.rect
    band = case.canvas[y + h + 8: case.h - 8, 8: case.w - 8].astype(np.float64).mean(axis=2)
    return np.clip(255 - (255 - band) * 8, 0, 255), case.w / res.nb


@pytest.mark.parametrize("photo_w", [1200, 3000])
def test_no_spectral_line_after_levels_stretch(photo_w):
    st, period = _stretched_empty_band(marker.embed, photo_w)
    assert spectral_peak(st, period) < 2.5
    # the check does see the grid of the first format's pattern
    st1, period1 = _stretched_empty_band(marker._embed_v1, photo_w)
    assert spectral_peak(st1, period1) > 3.0
    # the white band only darkens, by about one level on average
    assert -2.0 < (st.mean() - 255) / 8 < 0


# --------------------------------------------------------------------------
# a confidently wrong detection (high-key photo) is rescued by the edge sweep (#8)
# --------------------------------------------------------------------------
def test_marker_sweep_rescues_high_key_edge(tmp_path):
    import time
    from photoband.detect import detect_band
    from photoband.existing import _read_marker
    pw, ph = 1600, 1200
    photo = natural_photo(pw, ph, 7).astype(np.int32)
    photo = 191 + photo // 4                                       # high key
    photo[-300:] = np.minimum(255, photo[-300:] + 60)              # near-white bottom meets the band
    src = str(tmp_path / "hk.png")
    Image.fromarray(photo.astype(np.uint8)).save(src)
    lay, tiles = make_layout(pw, ph, "classic")
    out = str(tmp_path / "hk_out.png")
    r = do_save(src, out, lay, tiles)
    assert r.ok and r.marker["robust"]
    a = load(out)
    band = detect_band(a)
    assert tuple(band.photo_rect) != tuple(lay["photoRect"])      # detection is wrong here
    t0 = time.perf_counter()
    mk = _read_marker(a, band, a.shape[1], a.shape[0])
    assert time.perf_counter() - t0 < 3
    assert mk is not None and list(mk.photo_rect) == lay["photoRect"]


# --------------------------------------------------------------------------
# the payload holds only what is printed plus layout (#12)
# --------------------------------------------------------------------------
def test_payload_holds_only_printed_text_and_layout():
    import json
    import zlib
    from photoband.save import payload_for_marker
    rec = {"blocks": [{"id": "people", "custom": True, "text": "Ann and Bea"},
                      {"id": "notes", "custom": True, "text": "not printed"}],
           "template": {"id": "t"}, "overrides": {}, "originalText": {"text": "secret"},
           "layout": {"version": 1, "mode": "band", "photoRect": [1, 2, 3, 4], "textAreas": [],
                      "runs": [{"block": "people", "text": "Ann and Bea"}],
                      "warnings": [{"message": "Text shrunk to 80%"}], "blockBoxes": {"people": [0, 0, 1, 1]},
                      "scale": {"dpi": 300}, "shrink": 0.8},
           "photoHash": "ab" * 16, "canvas": [10, 10]}
    p = json.loads(zlib.decompress(payload_for_marker(rec)))
    assert set(p["layout"]) == {"version", "mode", "photoRect", "textAreas"}
    assert [b["text"] for b in p["blocks"]] == ["Ann and Bea", ""]
    assert "originalText" not in p and "secret" not in json.dumps(p)
