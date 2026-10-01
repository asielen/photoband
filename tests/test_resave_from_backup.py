"""Re-saving an overwritten photo takes the photo pixels from its verified original backup, so a
JPEG is always one generation from the original however often it is re-captioned."""
import os
import shutil

import numpy as np
from PIL import Image

from conftest import make_band_layout
from photoband import save as savemod
from photoband.exiftool import get as et_get
from photoband.existing import analyze_existing
from photoband.imageio import load_upright
from photoband.save import SaveRequest, backup_path_for, save, save_preview
from photoband.settings import load_settings

TEXT = "Ann, Bea and Carl"


def _jpg(path, seed=1, size=(360, 480), orientation=None):
    rng = np.random.default_rng(seed)
    h, w = size
    y, x = np.mgrid[0:h, 0:w]
    a = np.dstack([(x * 255 // w), (y * 255 // h), ((x + y) * 255 // (w + h))]).astype(np.uint8)
    a = np.clip(a.astype(int) + rng.integers(0, 60, a.shape), 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.fromarray(a)
    if orientation:
        ex = Image.Exif()
        ex[0x0112] = orientation
        im.save(path, quality=90, exif=ex.tobytes())
    else:
        im.save(path, quality=90)
    return path


def _twin(p, d):
    os.makedirs(d, exist_ok=True)
    return shutil.copy2(p, os.path.join(d, os.path.basename(p)))


def _settings(**saving):
    s = load_settings()
    s["saving"]["location"] = "subfolder"
    s["saving"].update(saving)
    return s


def _save(path, mode="overwrite", text=TEXT, **saving):
    """Caption ``path`` like the editor: an earlier Photoband output is re-captioned (case A)."""
    arr, info = load_upright(path)
    ana = analyze_existing(arr, info, et_get().read_json(path), run_ocr=False)
    if ana.get("case") == "A":
        sr = ana["sourceRect"]
        lay, tiles = make_band_layout(sr[2], sr[3], text=text, source_rect=sr)
        lay["mode"] = "band"
    else:
        lay, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=text)
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": text, "custom": False}], "overrides": {}}
    return save(SaveRequest(path=path, mode=mode, layout=lay, tiles=tiles, state=state, settings=_settings(**saving)))


def _photo(path, res):
    """The photo region of a saved file (from its record geometry)."""
    a, _ = load_upright(path)
    from photoband import record
    rec = record.from_metadata(et_get().read_json(path))
    x, y = rec["photoOffset"]
    w, h = rec["originalSize"]
    return a[y:y + h, x:x + w].astype(int)


def _from_backup(res):
    return any(n.startswith("Photo taken from the original backup") for n in res.notes)


def test_recaption_three_times_stays_one_generation_from_original(tmp_path):
    p = _jpg(str(tmp_path / "a" / "scan.jpg"))
    once = _twin(p, tmp_path / "b")   # the same original, captioned once
    r = _save(once)
    assert r.ok, r.error
    ref = _photo(once, r)
    r = _save(p)
    assert r.ok and not _from_backup(r), r.notes
    for i in range(3):
        r = _save(p)
        assert r.ok, r.error
        assert _from_backup(r), r.notes
        assert any(os.path.join("_originals", "scan-original.jpg") in n for n in r.notes), r.notes
        assert np.array_equal(_photo(p, r), ref), f"generation loss on re-caption {i + 1}"
    # the backup is still the untouched original, and only one
    assert sorted(f for f in os.listdir(tmp_path / "a" / "_originals") if not f.startswith(".")) == ["scan-original.jpg"]


def test_without_backup_source_recaptions_lose_quality(tmp_path, monkeypatch):
    """Control for the test above: re-encoding the current JPEG differs from captioning once."""
    p = _jpg(str(tmp_path / "a" / "scan.jpg"))
    once = _twin(p, tmp_path / "b")
    assert _save(once).ok
    ref = _photo(once, None)
    assert _save(p).ok
    monkeypatch.setattr(savemod, "original_photo", lambda *a, **k: (None, "disabled"))
    for _ in range(3):
        assert _save(p).ok
    assert not np.array_equal(_photo(p, None), ref)


def test_recaption_with_rotated_original(tmp_path):
    p = _jpg(str(tmp_path / "a" / "scan.jpg"), orientation=6)
    once = _twin(p, tmp_path / "b")
    assert _save(once).ok
    ref = _photo(once, None)
    assert ref.shape[:2] == (480, 360)    # upright
    assert _save(p).ok
    for _ in range(2):
        r = _save(p)
        assert r.ok and _from_backup(r), r.notes
        assert np.array_equal(_photo(p, r), ref)


def test_modified_backup_falls_back_to_the_file(tmp_path):
    p = _jpg(str(tmp_path / "scan.jpg"))
    assert _save(p).ok
    bk = backup_path_for(p, {})
    a = np.asarray(Image.open(bk)).copy()
    a[5:30, 5:30] = 0
    Image.fromarray(a).save(bk, quality=90)
    r = _save(p)
    assert r.ok, r.error
    assert not _from_backup(r)
    assert any("original backup was not used" in n for n in r.notes), r.notes
    # the changed file is not this photo's original: a new backup was kept, the old one untouched
    assert r.backup_path != bk


def test_deleted_backup_falls_back_to_the_file(tmp_path):
    p = _jpg(str(tmp_path / "scan.jpg"))
    assert _save(p).ok
    os.remove(backup_path_for(p, {}))
    r = _save(p)
    assert r.ok, r.error
    assert not _from_backup(r)
    assert any("original backup was not used" in n for n in r.notes), r.notes


def test_photo_edited_elsewhere_is_not_replaced_by_the_backup(tmp_path):
    p = _jpg(str(tmp_path / "scan.jpg"))
    assert _save(p).ok
    a = np.asarray(Image.open(p)).copy()
    a[100:120, 100:120] = 255             # retouched in another app (inside the photo)
    Image.fromarray(a).save(p, quality=95)
    lay, tiles = make_band_layout(a.shape[1], a.shape[0], text=TEXT)
    r = save(SaveRequest(path=p, mode="copy", layout=lay, tiles=tiles, state={}, settings=_settings()))
    assert r.ok, r.error
    assert not _from_backup(r)


def test_copy_of_overwritten_jpeg_uses_the_backup(tmp_path):
    p = _jpg(str(tmp_path / "a" / "scan.jpg"))
    once = _twin(p, tmp_path / "b")
    assert _save(once).ok
    ref = _photo(once, None)
    assert _save(p).ok
    r = _save(p, mode="copy")
    assert r.ok, r.error
    assert _from_backup(r), r.notes
    assert os.path.dirname(r.out_path).endswith("captioned")
    assert np.array_equal(_photo(r.out_path, r), ref)


def test_tiff_recaption_uses_backup_too(tmp_path):
    import tifffile
    p = str(tmp_path / "scan.tif")
    a = np.random.default_rng(3).integers(0, 255, (300, 400, 3), dtype=np.uint8)
    tifffile.imwrite(p, a, photometric="rgb")
    assert _save(p).ok
    r = _save(p)
    assert r.ok and _from_backup(r), r.notes
    assert np.array_equal(_photo(p, r), a.astype(int))


def test_save_preview_reports_pixel_source(tmp_path):
    p = _jpg(str(tmp_path / "scan.jpg"))
    s = _settings()["saving"]
    pv = save_preview(p, s, None, "")
    assert pv["pixelSource"] == "file" and pv["originalBackup"] is None
    assert _save(p).ok
    pv = save_preview(p, s, None, "")
    assert pv["pixelSource"] == "backup" and pv["originalBackup"] == backup_path_for(p, {})
    # backups off: an existing verified original is still used
    pv = save_preview(p, dict(s, backupOriginals=False), None, "")
    assert pv["pixelSource"] == "backup"
    os.remove(backup_path_for(p, {}))
    pv = save_preview(p, s, None, "")
    assert pv["pixelSource"] == "file" and pv["originalBackup"] is None


def test_original_rect_candidates_for_erase_records():
    rec = {"mode": "erase", "photoOffset": [10, 20], "originalSize": [100, 80], "canvas": [140, 150],
           "layout": {"mode": "erase", "sourceRect": [10, 20, 100, 80]}}
    assert savemod._original_rects(rec, 140, 150) == [(10, 20, 100, 80)]
    assert savemod._original_rects(rec, 100, 80) == []          # backup is not the same canvas
    band = {"mode": "band", "photoOffset": [30, 40], "originalSize": [100, 80], "canvas": [160, 200],
            "layout": {"sourceRect": [0, 0, 100, 80]}}
    assert savemod._original_rects(band, 100, 80) == [(0, 0, 100, 80)]
    band["originalRect"] = [5, 5, 100, 80]
    assert savemod._original_rects(band, 120, 90) == [(5, 5, 100, 80), (0, 0, 100, 80)]


# ---- the preview names what the save will really do (Codex review: preview vs actual)

def test_preview_backup_is_the_file_the_save_keeps(tmp_path):
    # a new photo: the backup will be <stem>-original, and it holds the untouched original
    p = _jpg(str(tmp_path / "scan.jpg"))
    s = _settings()["saving"]
    pv = save_preview(p, s, None, "")
    assert pv["backup"] == backup_path_for(p, {}) and pv["backupKind"] == "original" and not pv["backupExists"]
    r = _save(p)
    assert r.ok and r.backup_path == pv["backup"]
    # re-captioning: the verified original is reused, and the preview says so
    pv = save_preview(p, s, None, "")
    assert pv["backup"] == r.backup_path and pv["backupKind"] == "original" and pv["backupExists"]
    r2 = _save(p, text="Again")
    assert r2.ok and r2.backup_path == pv["backup"]


def test_preview_follows_a_legacy_backup_and_a_taken_name(tmp_path):
    # an older backup under the photo's own name is reused: the preview shows that file
    p = _jpg(str(tmp_path / "a.jpg"))
    os.makedirs(tmp_path / "_originals")
    legacy = tmp_path / "_originals" / "a.jpg"
    shutil.copy2(p, legacy)
    s = _settings()["saving"]
    pv = save_preview(p, s, None, "")
    assert pv["backup"] == str(legacy) and pv["backupKind"] == "original"
    r = _save(p)
    assert r.ok and r.backup_path == pv["backup"]
    # the new name is taken by other content: the new backup gets -2, and the preview says -2
    q = _jpg(str(tmp_path / "b.jpg"), seed=2)
    (tmp_path / "_originals" / "b-original.jpg").write_bytes(b"something else")
    pv = save_preview(q, s, None, "")
    assert pv["backup"].endswith("b-original-2.jpg")
    r = _save(q)
    assert r.ok and r.backup_path == pv["backup"]


def test_preview_never_promises_a_tampered_backup(tmp_path):
    # same size, other content: the save will not use it, so the preview must not promise it
    p = _jpg(str(tmp_path / "scan.jpg"))
    assert _save(p).ok
    bk = backup_path_for(p, {})
    data = bytearray(open(bk, "rb").read())
    data[-3] ^= 0xFF
    open(bk, "wb").write(bytes(data))
    pv = save_preview(p, _settings()["saving"], None, "")
    assert pv["pixelSource"] == "file" and pv["originalBackup"] is None
    # the file was captioned and its original is gone: the backup will hold this version
    assert pv["captioned"] and pv["backupKind"] == "current" and not pv["backupExists"]
