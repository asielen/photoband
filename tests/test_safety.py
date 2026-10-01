"""Data-safety guarantees of saving, backups, batch restore and housekeeping (review #7).

The photos are irreplaceable archive scans: every test here is about never losing a file."""
import errno
import json
import os
import shutil
import time

import numpy as np
import pytest
import tifffile

from conftest import make_band_layout
from photoband import batch as batchmod
from photoband import drafts
from photoband import imageio as io_
from photoband import save as savemod
from photoband.imageio import load_upright, probe
from photoband.save import SaveRequest, backup_path_for, save
from photoband.settings import load_settings
from photoband.util import file_lock, place_exclusive, quick_hash, sweep_temp


def _sha(p):
    import hashlib
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _tif(path, seed=1, size=(360, 480)):
    """A small synthetic 8-bit RGB scan (fast to save)."""
    rng = np.random.default_rng(seed)
    h, w = size
    y, x = np.mgrid[0:h, 0:w]
    a = np.dstack([(x * 255 // w), (y * 255 // h), ((x + y) * 255 // (w + h))]).astype(np.uint8)
    a = np.clip(a.astype(int) + rng.integers(0, 40, a.shape), 0, 255).astype(np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tifffile.imwrite(path, a, photometric="rgb")
    return path


def _retouch(path, value=0):
    a = tifffile.imread(path)
    a[10:40, 10:40] = value
    tifffile.imwrite(path, a, photometric="rgb")


def _save(path, mode="copy", patch=None, text="Ann, Bea and Carl", **kw):
    s = load_settings()
    for k, v in (patch or {}).items():
        s["saving"][k] = v
    arr, info = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=text)
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": text, "custom": False}], "overrides": {}}
    return save(SaveRequest(path=path, mode=mode, layout=layout, tiles=tiles, state=state, settings=s, **kw))


def _all_hashes(folder):
    return {_sha(os.path.join(dp, f)) for dp, _, fs in os.walk(folder) for f in fs}


# ---------------------------------------------------------------- 1: stale backups
def test_backup_not_reused_for_restored_and_retouched_original(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    orig = _sha(p)
    r = _save(p, "overwrite")
    assert r.ok, r.error
    base = backup_path_for(p, {})
    assert r.backup_path == base and _sha(base) == orig
    # the user restores the original by hand and retouches it in another app
    shutil.copy2(base, p)
    _retouch(p)
    retouched = _sha(p)
    r2 = _save(p, "overwrite")
    assert r2.ok, r2.error
    assert r2.backup_path != base and r2.backup_path.endswith("scan-2.tif")
    assert _sha(r2.backup_path) == retouched and _sha(base) == orig


def test_backup_not_reused_for_new_scan_with_same_name(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"), seed=1)
    assert _save(p, "overwrite").ok
    _tif(p, seed=2, size=(300, 420))  # a different photo scanned to the same file name
    newscan = _sha(p)
    r = _save(p, "overwrite")
    assert r.ok, r.error
    assert _sha(r.backup_path) == newscan and newscan in _all_hashes(str(tmp_path))


def test_recaption_reuses_backup_and_identical_original_is_not_duplicated(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    orig = _sha(p)
    r1 = _save(p, "overwrite")
    # re-caption of our own output: same backup, no new version
    from photoband.existing import analyze_existing
    from photoband.exiftool import get as et_get
    a, i = load_upright(p)
    ana = analyze_existing(a, i, et_get().read_json(p))
    assert ana["case"] == "A"
    lay, tiles = make_band_layout(ana["sourceRect"][2], ana["sourceRect"][3], text="New",
                                  source_rect=ana["sourceRect"])
    r2 = save(SaveRequest(path=p, mode="overwrite", layout=lay, tiles=tiles, state={}, settings=load_settings()))
    assert r2.ok and r2.backup_path == r1.backup_path
    # restored unchanged and captioned again: byte-identical backup is reused
    shutil.copy2(r1.backup_path, p)
    r3 = _save(p, "overwrite")
    assert r3.ok and r3.backup_path == r1.backup_path
    assert sorted(f for f in os.listdir(os.path.dirname(r1.backup_path)) if not f.startswith(".")) == ["scan.tif"]
    assert _sha(r1.backup_path) == orig


def test_copied_record_does_not_make_a_new_scan_reuse_the_backup(tmp_path):
    """A new scan carrying a record copied from a captioned file is not 'our output'."""
    p = _tif(str(tmp_path / "scan.tif"), seed=1)
    assert _save(p, "overwrite").ok
    captioned = str(tmp_path / "captioned_copy.tif")
    shutil.copy2(p, captioned)
    _tif(p, seed=5)
    import subprocess
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-tagsFromFile", captioned, "-XMP-photoband:all", p],
                   check=False, capture_output=True)
    newscan = _sha(p)
    r = _save(p, "overwrite")
    assert r.ok, r.error
    assert _sha(r.backup_path) == newscan


def test_empty_backup_is_never_reused_or_replaced(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    orig = _sha(p)
    os.makedirs(tmp_path / "_originals")
    (tmp_path / "_originals" / "scan.tif").write_bytes(b"")
    r = _save(p, "overwrite")
    assert r.ok, r.error
    assert r.backup_path.endswith("scan-2.tif") and _sha(r.backup_path) == orig
    assert os.path.getsize(tmp_path / "_originals" / "scan.tif") == 0


# ---------------------------------------------------------------- 2: copy over another original
def test_copy_overwrite_policy_never_replaces_a_foreign_file(tmp_path):
    d = tmp_path
    master = _tif(str(d / "scan001.tif"), seed=3)
    before = _sha(master)
    access = str(d / "scan001.jpg")
    from PIL import Image
    Image.fromarray(tifffile.imread(master)).save(access, quality=92)
    patch = {"location": "same", "fileName": "{stem}", "outputFormat": "TIFF", "onExists": "overwrite"}
    r = _save(access, "copy", patch=patch)
    assert r.ok, r.error
    assert r.out_path.endswith("scan001-2.tif") and _sha(master) == before
    assert any("was not made by Photoband" in n for n in r.notes)
    # an earlier output (carries a record) is replaced as the policy says
    r2 = _save(access, "copy", patch=patch, text="Second")
    assert r2.ok and r2.out_path == r.out_path


def test_copy_never_targets_backup_folder(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    r = _save(p, "copy", patch={"subfolderName": "_originals"})
    assert not r.ok and r.code == "dest"
    r = _save(p, "copyAs", dest_path=str(tmp_path / "_originals" / "x.tif"))
    assert not r.ok and r.code == "dest"
    bf = str(tmp_path / "Backups")
    r = _save(p, "copy", patch={"location": "fixed", "fixedFolder": bf, "backupFolder": bf})
    assert not r.ok and r.code == "dest"


def test_overwrite_of_a_backup_is_refused(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    r = _save(p, "overwrite")
    before = _sha(r.backup_path)
    r2 = _save(r.backup_path, "overwrite")
    assert not r2.ok and r2.code == "backup_folder" and _sha(r.backup_path) == before
    # a custom backup folder stays protected after the setting changes (marker file)
    bf = str(tmp_path / "Backups")
    q = _tif(str(tmp_path / "other.tif"), seed=4)
    r3 = _save(q, "overwrite", patch={"backupFolder": bf})
    assert r3.ok and r3.backup_path.startswith(bf)
    r4 = _save(r3.backup_path, "overwrite")  # backupFolder no longer set
    assert not r4.ok and r4.code == "backup_folder"


def test_confirmed_copy_as_over_a_foreign_file_keeps_it(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"), seed=1)
    other = _tif(str(tmp_path / "other.tif"), seed=2)
    before = _sha(other)
    r = _save(p, "copyAs", dest_path=other, on_exists="overwrite")
    assert r.ok, r.error
    kept = [n for n in r.notes if n.startswith("The replaced file was kept at ")]
    assert kept and _sha(kept[0].split(" at ", 1)[1]) == before


# ---------------------------------------------------------------- 3 + 4: batch restore
def _stage(b, idx, path, mode="overwrite"):
    arr, info = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=f"Photo {idx}")
    st = probe(path)
    job = {"path": path, "mode": mode, "layout": layout, "state": {"blocks": []},
           "expected_stat": [st.size_bytes, str(st.mtime_ns)], "settings": load_settings()}
    b.stage(idx, job, [{"x": t.x, "y": t.y, "png": t.png} for t in tiles])


def _wait(b, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if b.data.get("state") in ("finished", "cancelled") and not (b._thread and b._thread.is_alive()):
            return
        time.sleep(0.1)
    raise TimeoutError


def test_restore_skips_files_edited_after_the_batch(tmp_path):
    p = _tif(str(tmp_path / "a.tif"), seed=1)
    q = _tif(str(tmp_path / "b.tif"), seed=2)
    origs = {p: _sha(p), q: _sha(q)}
    b = batchmod.new_batch({"count": 2, "files": [p, q]})
    _stage(b, 0, p)
    _stage(b, 1, q)
    b.staging_complete()
    b.start(workers=1)
    _wait(b)
    ents = b.summary()["entries"]
    assert [e["state"] for e in ents] == ["done", "done"]
    assert all(e.get("outIdentity") and e.get("batchJob") for e in ents)
    _retouch(p, 7)  # edited later in another app
    edited = _sha(p)
    r = b.restore_originals()
    by = {x["path"]: x for x in r["results"]}
    assert by[p]["result"] == "skipped-edited" and by[q]["result"] == "restored"
    assert r["restored"] == 1 and r["skipped"] == 1
    assert _sha(p) == edited and _sha(q) == origs[q]
    # forced: the edited file is kept aside first
    r = b.restore_originals(force=True, indices=[0])
    x = r["results"][0]
    assert x["result"] == "restored" and _sha(p) == origs[p]
    assert x["aside"].endswith("a-before-restore.tif") and _sha(x["aside"]) == edited
    assert not [f for f in os.listdir(tmp_path) if "pbrestore" in f]


def test_killed_batch_keeps_restore_coverage(tmp_path):
    p = _tif(str(tmp_path / "a.tif"))
    orig = _sha(p)
    b = batchmod.new_batch({"count": 1, "files": [p]})
    _stage(b, 0, p)
    b.staging_complete()
    r0 = batchmod.run_job(b.id, 0)  # the worker finished; the app died before journaling "done"
    assert r0["ok"]
    assert json.load(open(os.path.join(b.dir, "00000", "backup.json")))["backup"] == r0["backup_path"]
    b.mark(0, "running")
    b.data["state"] = "running"
    b._write()
    batchmod._batches.clear()
    b2 = batchmod.get_batch(b.id)
    b2.start(workers=1)
    _wait(b2)
    e = b2.summary()["entries"][0]
    assert e["state"] == "done" and e["backup"] == r0["backup_path"] and b2.summary()["canRestore"]
    assert b2.restore_originals()["restored"] == 1 and _sha(p) == orig


def test_batch_run_lock(tmp_path):
    p = _tif(str(tmp_path / "a.tif"))
    b = batchmod.new_batch({"count": 1, "files": [p]})
    _stage(b, 0, p)
    b.staging_complete()
    from photoband.util import PidLock
    other = PidLock(os.path.join(b.dir, "run.lock")).acquire()  # "another window" (alive pid)
    try:
        with pytest.raises(batchmod.BatchBusy):
            b.start(workers=1)
    finally:
        other.release()
    # a lock left by a dead process is taken over
    with open(os.path.join(b.dir, "run.lock"), "w") as fh:
        json.dump({"pid": 999999999, "time": time.time()}, fh)
    b.start(workers=1)
    _wait(b)
    assert b.summary()["entries"][0]["state"] == "done"
    assert not os.path.exists(os.path.join(b.dir, "run.lock"))


# ---------------------------------------------------------------- 5: verify
@pytest.mark.parametrize("name,corrupt", [
    ("01_prophoto16_lzw.tif", "to8"), ("01_prophoto16_lzw.tif", "flip"), ("01_prophoto16_lzw.tif", "dropch"),
    ("02_gray8_uncompressed.tif", "lsb"), ("09_partial_date.jpg", "flip"),
])
def test_verify_catches_corrupt_writes(work, monkeypatch, name, corrupt):
    p = work(name)
    before = _sha(p)
    real = io_.write_image

    def bad(path, arr, info, fmt, q=95):
        a = arr
        if corrupt == "to8" and a.dtype == np.uint16:
            a = (a >> 8).astype(np.uint8)
        elif corrupt == "flip":
            a = a[::-1].copy()
        elif corrupt == "dropch":
            a = a[:, :, :1].copy()
        elif corrupt == "lsb":
            a = a.copy()
            a[a.shape[0] // 3, a.shape[1] // 2] ^= 1
        return real(path, a, info, fmt, q)
    monkeypatch.setattr(io_, "write_image", bad)
    r = _save(p, "overwrite")
    assert not r.ok and r.code == "verify", (r.code, r.error)
    assert _sha(p) == before
    assert not [f for f in os.listdir(os.path.dirname(p)) if f.startswith(".pbtmp-")]


def test_jpeg_verify_passes_with_tolerance(work):
    r = _save(work("09_partial_date.jpg"), "copy")
    assert r.ok, r.error
    assert any("within JPEG tolerance" in n for n in r.notes)
    r = _save(work("01_prophoto16_lzw.tif"), "copy", patch={"outputFormat": "JPEG", "jpegQuality": 80})
    assert r.ok, r.error


# ---------------------------------------------------------------- 6: scan
def test_scan_skips_backup_and_output_folders(tmp_path):
    root = tmp_path / "album"
    _tif(str(root / "a.tif"))
    for sub in ("_originals", "captioned", "With captions", "Backups", "Out", "Other backups", "keep"):
        _tif(str(root / sub / "x.tif"))
    (root / "Other backups" / ".photoband-backups").write_text("")
    saving = {"subfolderName": "With captions", "backupFolder": str(root / "Backups"),
              "fixedFolder": str(root / "Out")}
    names = [e["name"] for e in batchmod.scan_folder(str(root), True, saving)]
    assert names == ["a.tif", os.path.join("keep", "x.tif")]


# ---------------------------------------------------------------- 7 + 8: temp files
def test_sweep_removes_only_old_app_temp_files(tmp_path):
    d = tmp_path
    (d / "_originals").mkdir()
    old = time.time() - 3600
    files = {"old": [d / ".pbtmp-abc.tif", d / ".pbtmp-abc.tif_exiftool_tmp", d / "_originals" / ".pbbak-x.partial",
                     d / "_originals" / "scan.tif.partial", d / "scan.tif.pbrestore", d / ".pbrestore-q.tif"],
             "keep": [d / ".pbtmp-new.tif", d / "photo.tif", d / "download.partial"]}
    for f in files["old"] + files["keep"]:
        f.write_bytes(b"x")
    for f in files["old"] + [d / "download.partial"]:
        os.utime(f, (old, old))
    removed = sweep_temp([str(d)])
    assert sorted(removed) == sorted(str(f) for f in files["old"])
    assert all(f.exists() for f in files["keep"])


def test_failed_backup_leaves_nothing_behind(tmp_path, monkeypatch):
    p = _tif(str(tmp_path / "scan.tif"))
    before = _sha(p)

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(savemod, "place_exclusive", full)
    r = _save(p, "overwrite")
    assert not r.ok and r.code == "io"
    assert _sha(p) == before
    litter = [f for dp, _, fs in os.walk(tmp_path) for f in fs if f.startswith(".pb") or f.endswith(".partial")]
    assert litter == []


def test_failed_restore_leaves_nothing_behind(tmp_path, monkeypatch):
    p = _tif(str(tmp_path / "a.tif"))
    b = batchmod.new_batch({"count": 1, "files": [p]})
    _stage(b, 0, p)
    b.staging_complete()
    b.start(workers=1)
    _wait(b)
    captioned = _sha(p)

    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(batchmod, "replace_with_retry", full)
    r = b.restore_originals()
    assert r["results"][0]["result"] == "error" and r["errors"]
    assert _sha(p) == captioned
    assert not [f for f in os.listdir(tmp_path) if "pbrestore" in f]


# ---------------------------------------------------------------- 9: per-file lock
def test_concurrent_overwrite_is_refused_and_stale_lock_taken_over(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    before = _sha(p)
    lk = file_lock(p).acquire()
    try:
        r = _save(p, "overwrite")
        assert not r.ok and r.code == "busy" and _sha(p) == before
    finally:
        lk.release()
    with open(file_lock(p).path, "w") as fh:
        json.dump({"pid": 999999999, "time": time.time()}, fh)
    assert _save(p, "overwrite").ok


# ---------------------------------------------------------------- 10: drafts
def test_drafts_survive_mtime_touch_and_stale_drafts_are_listed(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    drafts.save_draft(p, {"blocks": {"people": {"text": "Grandpa Robert, 1944"}}})
    os.utime(p, None)
    assert drafts.load_draft(p) is not None
    _retouch(p)
    assert drafts.load_draft(p) is None
    ent = [x for x in drafts.list_drafts() if x["path"] == os.path.abspath(p)][0]
    assert ent["exists"] and not ent["valid"]
    assert drafts.load_draft_any(p)["blocks"]["people"]["text"].startswith("Grandpa")
    # pruned after 60 days
    fn = drafts._p(p)
    data = json.load(open(fn))
    data["updated"] = time.time() - 61 * 86400
    json.dump(data, open(fn, "w"))
    assert drafts.prune_drafts() >= 1 and not os.path.exists(fn)


# ---------------------------------------------------------------- 12, 13, 14: file system
def test_read_only_file_is_not_overwritten(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    before = _sha(p)
    os.chmod(p, 0o444)
    r = _save(p, "overwrite", patch={"backupOriginals": False})
    assert not r.ok and r.code == "readonly" and "read-only" in r.error
    assert _sha(p) == before and (os.stat(p).st_mode & 0o777) == 0o444


def test_overwrite_through_symlink_replaces_the_target(tmp_path):
    p = _tif(str(tmp_path / "real" / "scan.tif"))
    before = _sha(p)
    os.makedirs(tmp_path / "links")
    ln = str(tmp_path / "links" / "photo.tif")
    os.symlink(p, ln)
    r = _save(ln, "overwrite")
    assert r.ok, r.error
    assert os.path.islink(ln) and os.path.realpath(ln) == os.path.realpath(p)
    assert _sha(p) != before
    assert r.backup_path == os.path.join(os.path.dirname(os.path.realpath(p)), "_originals", "scan.tif")
    assert _sha(r.backup_path) == before
    assert not os.path.exists(tmp_path / "links" / "_originals")


def test_overwrite_keeps_xattrs_and_notes_hardlinks(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    try:
        os.setxattr(p, "user.xdg.tags", b"Grandpa,Archive")
    except (OSError, AttributeError):
        pytest.skip("no user xattrs here")
    os.link(p, str(tmp_path / "other_name.tif"))
    r = _save(p, "overwrite")
    assert r.ok, r.error
    assert os.getxattr(p, "user.xdg.tags") == b"Grandpa,Archive"
    assert any("hard-linked" in n for n in r.notes)


def test_fixed_backup_folder_key_follows_links(tmp_path):
    p = _tif(str(tmp_path / "real" / "scan.tif"))
    ln = str(tmp_path / "link.tif")
    os.symlink(p, ln)
    s = {"backupFolder": str(tmp_path / "B")}
    assert backup_path_for(ln, s) == backup_path_for(p, s)


# ---------------------------------------------------------------- 17, 20
def test_exclusive_placement_never_replaces(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"new")
    b.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        place_exclusive(str(a), str(b))
    assert b.read_bytes() == b"old" and a.exists()


def test_content_change_with_same_size_and_mtime_is_detected(tmp_path):
    p = _tif(str(tmp_path / "scan.tif"))
    st = os.stat(p)
    qh = quick_hash(p)
    _retouch(p, 3)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.path.getsize(p) == st.st_size
    r = _save(p, "overwrite", expected_stat=(st.st_size, st.st_mtime_ns, qh))
    assert not r.ok and r.code == "changed"
    r = _save(p, "overwrite", expected_stat=(st.st_size, st.st_mtime_ns), expected_hash=qh)
    assert not r.ok and r.code == "changed"
