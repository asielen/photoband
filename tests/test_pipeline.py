"""End-to-end backend tests of the save pipeline against the spec's acceptance checklist."""
import os
import shutil
import subprocess

import numpy as np
import pytest
import tifffile
from PIL import Image

from conftest import make_band_layout
from photoband import photos
from photoband.exiftool import get as et_get
from photoband.existing import analyze_existing
from photoband.imageio import load_upright, pixel_hash, probe, read_pixels
from photoband.save import SaveRequest, save
from photoband.settings import load_settings


def _save(path, mode="copy", layout=None, tiles=None, settings_patch=None, **kw):
    s = load_settings()
    if settings_patch:
        for k, v in settings_patch.items():
            s["saving"][k] = v
    arr, info = load_upright(path)
    if layout is None:
        layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": "Ann, Bea and Carl", "custom": False}], "overrides": {}}
    req = SaveRequest(path=path, mode=mode, layout=layout, tiles=tiles, state=state, settings=s, **kw)
    return save(req), arr, info, layout


def _md(path):
    return et_get().read_json(path)


def test_prophoto16_roundtrip_bit_identical(work):
    p = work("01_prophoto16_lzw.tif")
    r, arr, info, layout = _save(p)
    assert r.ok, r.error
    assert r.out_path.endswith(os.path.join("captioned", "01_prophoto16_lzw-captioned.tif"))
    out = probe(r.out_path)
    assert out.dtype == "uint16" and out.channels == 3
    assert out.compression == "lzw"
    assert out.icc == info.icc
    assert out.dpi == info.dpi
    o = read_pixels(out)
    x, y, w, h = layout["photoRect"]
    assert np.array_equal(o[y:y + h, x:x + w], arr)
    # band is white in the file's (ProPhoto) space → 65535 stays 65535 for white
    assert tuple(o[5, 5]) == (65535, 65535, 65535)
    # text was composited (some dark pixels in the band)
    band = o[y + h:, :, :]
    assert band.min() < 20000
    md = _md(r.out_path)
    assert md.get("XMP-dc:Title") == "Picnic at Lake Merced"
    assert md.get("XMP-dc:Description") == "Sunday outing with the neighbors"  # never replaced
    assert "XMP-photoband:Record" in md
    # regions remapped onto the new canvas
    reg = md["XMP-mwg-rs:RegionInfo"]
    assert reg["AppliedToDimensions"]["W"] == out.width
    src_reg = _md(p)["XMP-mwg-rs:RegionInfo"]["RegionList"][0]["Area"]
    new = reg["RegionList"][0]["Area"]
    cx_px_src = float(src_reg["X"]) * arr.shape[1] + x
    assert abs(float(new["X"]) * out.width - cx_px_src) < 1.0
    cy_px_src = float(src_reg["Y"]) * arr.shape[0] + y
    assert abs(float(new["Y"]) * out.height - cy_px_src) < 1.0
    # source untouched
    assert np.array_equal(load_upright(p)[0], arr)


def test_gray8_depth_and_compression_kept(work):
    p = work("02_gray8_uncompressed.tif")
    r, arr, info, layout = _save(p)
    assert r.ok, r.error
    out = probe(r.out_path)
    assert (out.channels, out.dtype, out.compression) == (1, "uint8", "none")
    assert out.dpi == (300.0, 300.0)
    o = read_pixels(out)
    x, y, w, h = layout["photoRect"]
    assert np.array_equal(o[y:y + h, x:x + w], arr)


def test_rotated_jpeg_band_at_visual_bottom_and_regions(work):
    p = work("03_rotated6_mwg.jpg")
    info = probe(p)
    assert info.orientation == 6
    r, arr, info, layout = _save(p)
    assert r.ok, r.error
    out = probe(r.out_path)
    assert out.orientation == 1
    assert (out.width, out.height) == tuple(layout["canvas"])
    assert arr.shape[0] > arr.shape[1]  # upright portrait
    md = _md(r.out_path)
    assert md.get("IFD0:Orientation") in (1, None)
    lst = md["XMP-mwg-rs:RegionInfo"]["RegionList"]
    names = [e["Name"] for e in lst]
    assert names == ["Left Person", "Right Person"]
    # Left person's face is on the left of the upright output, in the top part
    a = lst[0]["Area"]
    x, y, w, h = layout["photoRect"]
    cx = float(a["X"]) * out.width
    assert x + 0.15 * w < cx < x + 0.35 * w
    o = read_pixels(out)
    # the face ellipse color sits under the remapped region center
    cy = float(a["Y"]) * out.height
    px = o[int(cy), int(cx)]
    assert abs(int(px[0]) - 224) < 25 and abs(int(px[2]) - 150) < 30
    assert "ThumbnailImage" not in " ".join(md.keys())


def test_mp_regions_remapped(work):
    p = work("04_mp_regions.tif")
    r, arr, info, layout = _save(p)
    assert r.ok, r.error
    md = _md(r.out_path)
    regs = md["XMP-MP:RegionInfoMP"]["Regions"]
    x, y, w, h = [float(v) for v in regs[0]["Rectangle"].split(",")]
    out = probe(r.out_path)
    px, py, pw, ph = layout["photoRect"]
    assert abs(x * out.width - (px + 0.2 * pw)) < 1.0
    assert abs(y * out.height - (py + 0.3 * ph)) < 1.0


def test_png_lossless_and_record(work):
    p = work("05_person_in_image.png")
    r, arr, info, layout = _save(p)
    assert r.ok, r.error
    o = read_pixels(probe(r.out_path))
    x, y, w, h = layout["photoRect"]
    assert np.array_equal(o[y:y + h, x:x + w], arr)


def test_multipage_and_cmyk_blocked(work):
    r, *_ = _save(work("07a_multipage.tif"))
    assert not r.ok and r.code == "multipage"
    with pytest.raises(Exception):
        # CMYK: probe says blocked; the layout helper can't even build (4 channels) → save refuses
        p = work("07b_cmyk.tif")
        s = load_settings()
        from photoband.composite import Tile
        res = save(SaveRequest(path=p, mode="copy", layout={"mode": "band"}, tiles=[], state={}, settings=s))
        assert not res.ok and res.code == "blocked"
        raise RuntimeError("blocked as expected")


def test_overwrite_with_backup_and_recaption(work):
    p = work("06_group_three_rows.tif")
    before = open(p, "rb").read()
    r, arr, info, layout = _save(p, mode="overwrite")
    assert r.ok, r.error
    assert r.backup_path and open(r.backup_path, "rb").read() == before
    # second overwrite keeps the first backup
    arr2, info2 = load_upright(p)
    md = _md(p)
    ana = analyze_existing(arr2, info2, md)
    assert ana["case"] == "A" and ana["source"] == "record"
    sx, sy, sw, sh = ana["sourceRect"]
    assert (sw, sh) == (arr.shape[1], arr.shape[0])
    lay2, tiles2 = make_band_layout(sw, sh, text="New caption", source_rect=ana["sourceRect"])
    r2, *_ = _save(p, mode="overwrite", layout=lay2, tiles=tiles2)
    assert r2.ok, r2.error
    assert open(r2.backup_path, "rb").read() == before
    out = read_pixels(probe(p))
    # no stacked band: same canvas size as a single caption, photo identical
    assert out.shape[:2] == (lay2["canvas"][1], lay2["canvas"][0])
    x, y, w, h = lay2["photoRect"]
    assert np.array_equal(out[y:y + h, x:x + w], arr)


def test_stripped_png_payload_restores_state(work, tmp_path):
    p = work("05_person_in_image.png")
    r, arr, info, layout = _save(p)
    assert r.ok and r.marker.get("robust") and r.marker.get("payload"), r.marker
    stripped = str(tmp_path / "stripped.png")
    shutil.copy2(r.out_path, stripped)
    subprocess.run(["exiftool", "-all=", "-overwrite_original", stripped], check=True, capture_output=True)
    assert "XMP-photoband:Record" not in _md(stripped)
    a, i = load_upright(stripped)
    ana = analyze_existing(a, i, _md(stripped))
    assert ana["case"] == "A" and ana["source"] == "marker+payload", ana.get("source")
    assert ana["sourceRect"] == layout["photoRect"]
    assert ana["state"]["blocks"][0]["text"] == "Ann, Bea and Carl"


def test_marker_after_jpeg_resave_and_resize(work, tmp_path):
    p = work("01_prophoto16_lzw.tif")
    r, arr, info, layout = _save(p)
    assert r.ok
    im = Image.fromarray((read_pixels(probe(r.out_path)) >> 8).astype(np.uint8))
    small = im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
    q = str(tmp_path / "resaved.jpg")
    small.save(q, quality=70)
    a, i = load_upright(q)
    ana = analyze_existing(a, i, {})
    assert ana.get("marker"), ana
    px, py, pw, ph = ana["marker"]["photoRect"]
    ex, ey, ew, eh = [v / 2 for v in layout["photoRect"]]
    assert abs(px - ex) <= 1 and abs(py - ey) <= 1 and abs(pw - ew) <= 1 and abs(ph - eh) <= 1


def test_case_b_other_tool_and_case_c_d(work):
    p = work("11_other_tool_colored_band.png")
    a, i = load_upright(p)
    ana = analyze_existing(a, i, _md(p))
    assert ana["case"] == "B"
    assert ana["band"]["photo_rect"] == [0, 0, 1800, 1200]
    assert "Rosa" in ana["text"]
    p = work("12_scanned_polaroid_handwriting.tif")
    a, i = load_upright(p)
    ana = analyze_existing(a, i, _md(p))
    assert ana["case"] == "C"
    x, y, w, h = ana["band"]["photo_rect"]
    assert abs(x - 80) <= 2 and abs(y - 110) <= 2 and abs(w - 1600) <= 2 and abs(h - 1560) <= 2
    p = work("13_date_stamp.jpg")
    a, i = load_upright(p)
    ana = analyze_existing(a, i, _md(p))
    assert ana["case"] == "D" and ana["textOverPhoto"]
    p = work("14_near_white_sky.tif")
    a, i = load_upright(p)
    ana = analyze_existing(a, i, _md(p))
    assert ana["case"] is None


def test_case_c_overwrite_disabled(work):
    p = work("12_scanned_polaroid_handwriting.tif")
    r, *_ = _save(p, mode="overwrite", case="C")
    assert not r.ok and r.code == "case_c_overwrite"
    assert not os.path.exists(os.path.join(os.path.dirname(p), "_originals"))


def test_changed_file_detected(work):
    p = work("09_partial_date.jpg")
    info = probe(p)
    r, *_ = _save(p, expected_stat=(info.size_bytes, info.mtime_ns - 1))
    assert not r.ok and r.code == "changed"


def test_conflict_policy_increment(work):
    p = work("09_partial_date.jpg")
    r1, *_ = _save(p)
    r2, *_ = _save(p)
    assert r1.ok and r2.ok and r1.out_path != r2.out_path and r2.out_path.endswith("-captioned-2.jpg")


def test_metadata_fields(fixtures_dir):
    m = photos.meta(os.path.join(fixtures_dir, "06_group_three_rows.tif"))
    assert m["fields"]["title"] == "Class of 1958"
    assert m["fields"]["date"] == "1958"
    assert len(m["fields"]["faces"]) == 11 and m["fields"]["faces_unnamed_count"] == 1
    m = photos.meta(os.path.join(fixtures_dir, "05_person_in_image.png"))
    assert [f["name"] for f in m["fields"]["faces"]] == ["Frank Miller", "Grace Lee"]
    assert "names without positions" in m["warnings"]
    m = photos.meta(os.path.join(fixtures_dir, "08_unicode_names.tif"))
    assert m["fields"]["faces"][2]["name"] == "Наталья Ивановна"
    from captiontokens import resolve
    assert resolve("{names}", m["fields"]).text == "Zoë Ångström, Łukasz Dvořák and Наталья Ивановна"
    m = photos.meta(os.path.join(fixtures_dir, "06_group_three_rows.tif"))
    rows = resolve("{names:rows}", m["fields"]).text
    assert rows.startswith("Front row, L–R: Hana, Ivo, Jun and Kai; Middle row")
    assert rows.endswith("Back row, L–R: Oli, Pia, Quin and Ray")
    m = photos.meta(os.path.join(fixtures_dir, "03_rotated6_mwg.jpg"))
    assert resolve("{names}", m["fields"]).text == "Left Person and Right Person"


def test_copy_as_refuses_to_replace_existing_file(work):
    p = work("09_partial_date.jpg")
    other = work("05_person_in_image.png")
    before = open(other, "rb").read()
    r, *_ = _save(p, mode="copyAs", dest_path=other)
    assert not r.ok and r.code == "exists"
    assert open(other, "rb").read() == before
    r, *_ = _save(p, mode="copyAs", dest_path=other, on_exists="overwrite")
    assert r.ok, r.error
    assert r.out_path == os.path.abspath(other) and open(other, "rb").read() != before
    assert not r.backup_path  # another file, not the source: no source backup involved
    # a new name is written without asking
    r, *_ = _save(p, mode="copyAs", dest_path=os.path.join(os.path.dirname(p), "fresh.png"))
    assert r.ok, r.error


def test_copy_as_onto_source_is_an_overwrite(work):
    p = work("06_group_three_rows.tif")
    before = open(p, "rb").read()
    # the same file under another name (a link here; a different letter case on Windows/macOS)
    alias = os.path.join(os.path.dirname(p), "alias.tif")
    os.symlink(p, alias)
    r, *_ = _save(p, mode="copyAs", dest_path=alias)
    assert not r.ok and r.code == "source"
    assert open(p, "rb").read() == before
    r, *_ = _save(p, mode="copyAs", dest_path=alias, on_exists="overwrite")
    assert r.ok, r.error
    assert r.out_path == os.path.abspath(p)
    assert r.backup_path and open(r.backup_path, "rb").read() == before
    assert os.path.islink(alias)  # the link itself was not replaced by a regular file


def test_copy_as_onto_source_honours_case_c_guard(work):
    p = work("12_scanned_polaroid_handwriting.tif")
    before = open(p, "rb").read()
    r, *_ = _save(p, mode="copyAs", dest_path=p, on_exists="overwrite", case="C")
    assert not r.ok and r.code == "case_c_overwrite"
    assert open(p, "rb").read() == before
    assert not os.path.exists(os.path.join(os.path.dirname(p), "_originals"))


def test_permissions_preserved(work):
    p = work("09_partial_date.jpg")
    os.chmod(p, 0o640)
    r, *_ = _save(p)
    assert r.ok, r.error
    assert (os.stat(r.out_path).st_mode & 0o777) == 0o640
    r, *_ = _save(p, mode="overwrite")
    assert r.ok, r.error
    assert (os.stat(p).st_mode & 0o777) == 0o640
