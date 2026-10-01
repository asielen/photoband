"""Decisions made from the Photoband record must not change when a file's metadata is stripped.

The record (XMP) disappears with ``exiftool -all=``; the hidden marker (and, on PNG/TIFF, its
payload) stays. Every decision that reads a record field must read the same value from the
payload, or be conservative when it can't be known (see photoband/record.py for the table):

- saveMode -> isCopy: a batch never captions a Photoband copy again (payload carries it; a JPEG
  copy keeps only the marker, so how it was saved is unknown and the batch leaves it alone);
- mode (erase in place): the editor's re-edit reads ``record.mode`` on both paths;
- originalFile: a backup of a file that was already captioned is never called "the untouched
  original" (save preview, the next save's record, the pixel source).
"""
import os
import shutil
import subprocess
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import make_band_layout
from photoband import photos, record, security, server
from photoband.existing import analyze_existing, provenance
from photoband.exiftool import get as et_get
from photoband.imageio import load_upright
from photoband.save import (SaveRequest, decode_marker_payload, find_original_backup, payload_for_marker, save,
                            save_preview)
from photoband.settings import load_settings

needs_et = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


@pytest.fixture(autouse=True)
def _fresh_settings():
    from photoband.settings import reset_settings
    reset_settings()
    yield
    reset_settings()


def _photo(path, w=1600, h=1200, seed=3):
    rng = np.random.default_rng(seed)
    small = rng.random((h // 64, w // 64, 3)).astype(np.float32)
    img = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    img = (img * 200 + 25 + rng.normal(0, 4, (h, w, 3))).clip(0, 255).astype(np.uint8)
    Image.fromarray(img).save(path, quality=95) if path.endswith(".jpg") else Image.fromarray(img).save(path)
    return path


def _save(path, mode="copy", saving=None, text="Ann, Bea and Carl"):
    s = load_settings()
    s["saving"].update(saving or {})
    arr, info = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=text)
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": text, "custom": False}], "overrides": {}}
    r = save(SaveRequest(path=path, mode=mode, layout=layout, tiles=tiles, state=state, settings=s))
    assert r.ok, r.error
    return r


def _strip(path):
    subprocess.run(["exiftool", "-all=", "-overwrite_original", path], check=True, capture_output=True)
    assert record.from_metadata(et_get().read_json(path)) is None


def _analyze(path):
    a, i = load_upright(path)
    return analyze_existing(a, i, et_get().read_json(path), run_ocr=False)


# ------------------------------------------------------------------ the payload carries saveMode

def test_payload_carries_save_mode_and_old_payloads_read_as_unknown():
    rec = {"blocks": [], "layout": {"mode": "band"}, "saveMode": "copy", "photoHash": "ab" * 16}
    assert decode_marker_payload(payload_for_marker(rec))["saveMode"] == "copy"
    old = decode_marker_payload(payload_for_marker({k: v for k, v in rec.items() if k != "saveMode"}))
    assert "saveMode" not in old
    assert provenance(None, old, True) == {"isCopy": False, "copyUnknown": True}
    # payload values are untrusted: only the exact strings count
    assert provenance(None, {"saveMode": "COPY"}, True)["copyUnknown"] is True
    assert provenance(None, {"saveMode": "copy"}, True) == {"isCopy": True}
    assert provenance(None, {"saveMode": "overwrite"}, True) == {"isCopy": False}
    # records from before saveMode: an in-place save records the original it replaced
    assert provenance({"originalFile": {"sha256": "x"}}, None, False) == {"isCopy": False}
    assert provenance(None, None, False) == {}


# ------------------------------------------------------------------ 1. isCopy through marker recovery

@needs_et
def test_stripped_lossless_copy_is_still_a_copy_for_the_batch(tmp_path):
    src = _photo(str(tmp_path / "IMG.png"))
    r = _save(src)
    assert r.marker["robust"] and r.marker["payload"], r.marker
    assert _analyze(r.out_path)["isCopy"] is True              # with its record
    _strip(r.out_path)
    ana = _analyze(r.out_path)
    assert ana["source"] == "marker+payload" and ana["case"] == "A"
    assert ana["isCopy"] is True and not ana.get("copyUnknown")

    # and the batch pre-flight hands the batch plan exactly that (BatchView skips ex.isCopy)
    security.reset()
    security.allow_root(str(tmp_path))
    c = TestClient(server.app, base_url="http://127.0.0.1")
    c.headers["X-Photoband-Token"] = security.TOKEN
    try:
        pid = c.post("/api/batch/preflight", json={"paths": [r.out_path, src]}).json()["id"]
        t0 = time.time()
        while True:
            st = c.get(f"/api/batch/preflight/{pid}").json()
            if st["done"] >= 2 or time.time() - t0 > 60:
                break
            time.sleep(0.05)
        res = {os.path.normcase(x["path"]): x for x in st["results"]}
        copy = res[os.path.normcase(os.path.abspath(r.out_path))]["existing"]
        orig = res[os.path.normcase(os.path.abspath(src))]["existing"]
    finally:
        security.reset()
    assert copy["isCopy"] is True
    assert not orig.get("isCopy") and not orig.get("copyUnknown")


@needs_et
def test_stripped_in_place_save_is_not_a_copy(tmp_path):
    src = _photo(str(tmp_path / "IMG.png"))
    _save(src, mode="overwrite")
    _strip(src)
    ana = _analyze(src)
    assert ana["source"] == "marker+payload"
    assert ana["isCopy"] is False and not ana.get("copyUnknown")


@needs_et
def test_stripped_jpeg_copy_has_unknown_provenance(tmp_path):
    # JPEG: the marker survives, its payload is never written (lossy): it may be a copy
    src = _photo(str(tmp_path / "IMG.jpg"))
    r = _save(src)
    assert r.marker["robust"], r.marker
    _strip(r.out_path)
    ana = _analyze(r.out_path)
    assert ana["source"] == "marker", ana.get("source")
    assert ana["isCopy"] is False and ana["copyUnknown"] is True


@needs_et
def test_copy_changed_elsewhere_is_still_a_copy(tmp_path):
    # the record no longer matches the pixels (edited in another app), but it still says how the
    # FILE was made: the detection fallback keeps isCopy
    src = _photo(str(tmp_path / "IMG.png"))
    r = _save(src)
    a, i = load_upright(r.out_path)
    a = a.copy()
    rec = record.from_metadata(et_get().read_json(r.out_path))
    x, y = rec["photoOffset"]
    a[y + 10:y + 40, x + 10:x + 40] = 0
    ana = analyze_existing(a, i, et_get().read_json(r.out_path), run_ocr=False)
    assert ana["source"] != "record"
    assert ana["isCopy"] is True


# ------------------------------------------------------------------ 2. erase-in-place mode survives

@needs_et
def test_stripped_erase_in_place_output_reports_its_mode(tmp_path):
    src = _photo(str(tmp_path / "IMG.png"))
    r = _save(src)
    _strip(r.out_path)
    a = load_upright(r.out_path)[0]
    ana = _analyze(r.out_path)
    rect = ana["sourceRect"]
    H, W = a.shape[:2]
    area = [rect[0], rect[1] + rect[3] + 20, rect[2], 60]
    from photoband.existing import _erase_inputs_for_record
    ein = _erase_inputs_for_record(a, list(rect))
    elay = {"version": 1, "mode": "erase", "sourceRect": list(rect), "photoRect": list(rect), "canvas": [W, H],
            "fills": [], "bandColor": "#ffffff", "protect": [], "textAreas": [{"id": "a0", "rect": area}],
            "runs": [{"block": "people", "text": "Zed"}], "textColors": ["#23262e"]}
    dest = str(tmp_path / "erased.png")
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": "Zed", "custom": True}], "overrides": {}}
    res = save(SaveRequest(path=r.out_path, mode="copyAs", layout=elay, tiles=[], state=state,
                           settings=load_settings(), dest_path=dest, on_exists="overwrite", case="A",
                           erase={"band": ein["band"], "photoRect": list(rect), "blocks": ein["blocks"], "grow": 2}))
    assert res.ok, res.error
    assert res.marker["robust"] and res.marker["payload"], res.marker
    assert _analyze(dest)["record"]["mode"] == "erase"          # with its record
    _strip(dest)
    ana = _analyze(dest)
    assert ana["source"] == "marker+payload"
    # the editor re-edits in place (ex.record.mode === 'erase' && ex.band), as with the record
    assert ana["record"]["mode"] == "erase" and ana.get("band")
    assert ana["isCopy"] is True


# ------------------------------------------------------------------ 3. no "untouched original" for a captioned file

@needs_et
def test_preview_and_resave_of_a_stripped_output_never_claim_the_original(tmp_path):
    src = _photo(str(tmp_path / "IMG.jpg"))
    saving = load_settings()["saving"]
    _save(src, mode="overwrite")
    # the untouched original was backed up; now the captioned file loses its metadata
    _strip(src)
    photos.existing(src, run_ocr=False)              # what opening the photo does
    pv = save_preview(src, saving, {}, "Classic")
    assert pv["captioned"] is True
    assert pv["backupKind"] == "current"            # the backup made now holds the captioned file
    # re-captioned in place: the new record must not call that backup the original
    _save(src, mode="overwrite", text="Dee and Eve")
    rec = record.from_metadata(et_get().read_json(src))
    assert rec["originalFile"].get("captioned") is True
    assert find_original_backup(os.path.realpath(src), saving, rec) is None
    photos.existing(src, run_ocr=False)
    pv = save_preview(src, saving, {}, "Classic")
    assert pv["backupKind"] == "current" and pv["pixelSource"] == "file"


@needs_et
def test_first_caption_still_backs_up_the_original(tmp_path):
    # unchanged for an uncaptioned photo: its backup IS the untouched original
    src = _photo(str(tmp_path / "IMG.jpg"))
    saving = load_settings()["saving"]
    photos.existing(src, run_ocr=False)
    assert save_preview(src, saving, {}, "Classic")["backupKind"] == "original"
    _save(src, mode="overwrite")
    rec = record.from_metadata(et_get().read_json(src))
    assert "captioned" not in rec["originalFile"]
    assert find_original_backup(os.path.realpath(src), saving, rec)

