"""Regression tests for the save/file safety review: copies of read-only originals, files held
open by another program on Windows, and a batch restore racing a save of the same photo."""
import hashlib
import os
import stat
import sys
import time

import numpy as np
import pytest
import tifffile

from conftest import make_band_layout
from photoband import batch as batchmod
from photoband.imageio import load_upright, probe
from photoband.save import SaveRequest, save
from photoband.settings import load_settings
from photoband.util import file_lock


@pytest.fixture(autouse=True)
def _fresh_settings():
    from photoband.settings import reset_settings
    reset_settings()
    yield
    reset_settings()


def _sha(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _tif(path, seed=1, size=(240, 320)):
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 255, size + (3,), dtype=np.uint8)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tifffile.imwrite(path, a, photometric="rgb")
    return path


def _save(path, mode="copy", text="Ann, Bea and Carl", **kw):
    arr, _info = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=text)
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": text, "custom": False}], "overrides": {}}
    return save(SaveRequest(path=path, mode=mode, layout=layout, tiles=tiles, state=state,
                            settings=load_settings(), **kw))


def _litter(folder):
    return [os.path.join(dp, f) for dp, _, fs in os.walk(folder) for f in fs if f.startswith(".pb")]


# ---------------------------------------------------------------- read-only originals
def test_copy_of_read_only_original_is_saved_and_writable(tmp_path):
    """Archivists mark originals read-only to protect them; "Save copy" is the safe path for
    exactly those files. The temp file used to get the read-only bit before its fsync, so the
    save failed ("The file is read-only") and left an undeletable .pbtmp file behind."""
    p = _tif(str(tmp_path / "scan.tif"))
    before = _sha(p)
    os.chmod(p, stat.S_IREAD)
    try:
        r = _save(p, "copy")
        assert r.ok, r.error
        assert os.path.isfile(r.out_path)
        assert os.stat(r.out_path).st_mode & stat.S_IWRITE      # the copy is not read-only
        assert not _litter(str(tmp_path))
        # the original is untouched and still protected
        assert _sha(p) == before and not os.stat(p).st_mode & stat.S_IWRITE
        # a second copy over the first ("overwrite" policy replaces this app's own copy)
        r2 = _save(p, "copy", text="Ann", on_exists="overwrite")
        assert r2.ok, r2.error
    finally:
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)


# ---------------------------------------------------------------- Windows: file open elsewhere
@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes")
def test_overwrite_of_file_open_in_another_app_says_so(tmp_path):
    """An editor or viewer that has the photo open (sharing read, not delete) makes the replace
    fail with "access denied", which used to be reported as a read-only/permission problem."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    p = _tif(str(tmp_path / "scan.tif"))
    before = _sha(p)
    h = k32.CreateFileW(p, 0x80000000, 0x1, None, 3, 0, None)   # GENERIC_READ, FILE_SHARE_READ
    assert h and h != wintypes.HANDLE(-1).value
    try:
        r = _save(p, "overwrite")
    finally:
        k32.CloseHandle(h)
    assert not r.ok and r.code == "locked", (r.code, r.error)
    assert "open in another app" in r.error
    assert _sha(p) == before
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".pb")]
    # closed in the other app: the overwrite works and reuses the backup made the first time
    r = _save(p, "overwrite")
    assert r.ok, r.error
    assert _sha(r.backup_path) == before
    assert os.listdir(tmp_path / "_originals") == [".photoband-backups", "scan-original.tif"]


# ---------------------------------------------------------------- batch restore vs. a save
def _stage(b, idx, path, **extra):
    arr, _info = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=f"Photo {idx}")
    st = probe(path)
    job = {"path": path, "mode": "overwrite", "layout": layout, "state": {"blocks": []},
           "expected_stat": [st.size_bytes, str(st.mtime_ns)], "settings": load_settings(), **extra}
    b.stage(idx, job, [{"x": t.x, "y": t.y, "png": t.png} for t in tiles])


def _wait(b, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if b.data.get("state") in ("finished", "cancelled") and not (b._thread and b._thread.is_alive()):
            return
        time.sleep(0.1)
    raise TimeoutError


def _batch_overwrite(tmp_path):
    p = _tif(str(tmp_path / "a.tif"))
    orig = _sha(p)
    b = batchmod.new_batch({"count": 1, "files": [p]})
    _stage(b, 0, p)
    b.staging_complete()
    b.start(workers=1)
    _wait(b)
    assert b.summary()["entries"][0]["state"] == "done"
    return p, orig, b


def test_restore_never_puts_back_over_a_save_that_finished_meanwhile(tmp_path, monkeypatch):
    """A save of the photo that completes between restore's "edited since the batch?" check and
    the restore itself must not be overwritten unseen: the check runs under the file lock."""
    p, _orig, b = _batch_overwrite(tmp_path)
    real_lock = batchmod.file_lock
    edited = {}

    class _SaveFinishesFirst:
        def __init__(self, path):
            self._lk = real_lock(path)
            self._path = path

        def acquire(self, *a, **k):
            # another window's overwrite releases the photo just before restore gets the lock
            a_ = tifffile.imread(self._path)
            a_[5:25, 5:25] = 3
            tifffile.imwrite(self._path, a_, photometric="rgb")
            edited["sha"] = _sha(self._path)
            self._lk.acquire(*a, **k)
            return self._lk

    monkeypatch.setattr(batchmod, "file_lock", _SaveFinishesFirst)
    res = b.restore_originals()
    x = res["results"][0]
    assert x["result"] == "skipped-edited", x
    assert _sha(p) == edited["sha"]


def test_restore_of_a_photo_being_saved_reports_it(tmp_path):
    p, _orig, b = _batch_overwrite(tmp_path)
    after = _sha(p)
    lk = file_lock(os.path.realpath(p)).acquire()   # a save of this photo is running
    try:
        res = b.restore_originals()
    finally:
        lk.release()
    x = res["results"][0]
    assert x["result"] == "error" and "being saved" in x["message"], x
    assert _sha(p) == after


def test_batch_copy_whose_name_was_taken_meanwhile_is_kept_and_explained(tmp_path):
    """A file that appears at a batch copy's reserved name (another app, another window) after
    staging is never replaced; the batch says so in words, not with a bare path."""
    p = _tif(str(tmp_path / "a.tif"))
    dest = str(tmp_path / "captioned" / "a-captioned.tif")
    b = batchmod.new_batch({"count": 1, "files": [p]})
    _stage(b, 0, p, mode="copyAs", dest_path=dest)
    b.staging_complete()
    _tif(dest, seed=9)            # someone else's file takes the name before the batch runs
    theirs = _sha(dest)
    b.start(workers=1)
    _wait(b)
    e = b.summary()["entries"][0]
    assert e["state"] == "failed" and e.get("code") == "exists"
    assert "a-captioned.tif appeared after the batch was prepared" in e["error"]
    assert _sha(dest) == theirs
