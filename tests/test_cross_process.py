"""State shared on disk by several Photoband processes (two windows, a relaunched app): a process
never acts on, or writes back, a copy of it that another process has changed since.

Class of issue: in-memory state cached from a file that another process can change, read or
written back without revalidation. Batch journals (cached by get_batch), settings, drafts, the
font index, and the photo caches keyed by the file's stat."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest
from PIL import Image

from photoband import batch as batchmod
from photoband import drafts, fonts, paths, photos, proxy, settings
from photoband.imageio import probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _until(cond, timeout=60.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.05)
    raise TimeoutError(what)


def _child(code: str) -> subprocess.Popen:
    """Another Photoband process, on the same app data."""
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code)], env=env, cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _new(n=3):
    b = batchmod.new_batch({"count": n, "files": [f"/x/{i}.jpg" for i in range(n)]})
    for i in range(n):
        b.stage(i, {"path": f"/x/{i}.jpg", "mode": "overwrite"}, [])
    b.staging_complete()
    return b


@pytest.fixture
def elsewhere(monkeypatch):
    """The batch's run lock is held by another live Photoband process."""
    other = os.getpid() + 1
    real = batchmod._lock_holder
    monkeypatch.setattr(batchmod, "_lock_holder",
                        lambda p: other if p.endswith("run.lock") else real(p))
    return other


# ------------------------------------------------------------------ the review finding

def test_stop_forwarded_to_the_process_running_the_batch_shows_its_outcome():
    """Window B presses Stop on a batch window A (another process) is running: B's cached Batch
    reads the journal A writes, so B sees the run end and the photos it saved."""
    b = _new(3)
    gate = os.path.join(b.dir, "gate")
    child = _child(f"""
        import concurrent.futures as cf, os, sys, time
        from photoband import batch as batchmod
        batchmod._new_pool = lambda n: cf.ThreadPoolExecutor(max_workers=n)
        def run_job(bid, idx):
            while not os.path.exists({gate!r}):
                time.sleep(0.02)
            return {{"ok": True, "out_path": "/out/%d.jpg" % idx, "out_identity": [1, 1]}}
        batchmod.run_job = run_job
        b = batchmod.get_batch({b.id!r})
        b.start(workers=1)
        b._thread.join(60)
        sys.exit(0 if b.data["state"] == "cancelled" else 3)
    """)
    try:
        cached = batchmod.get_batch(b.id)
        assert cached is b   # the window that created it keeps it cached
        # running in the other process: seen through the cached Batch
        _until(lambda: b.summary()["state"] == "running" and b.run_elsewhere(), what="run in the child")
        _until(lambda: b.summary()["counts"].get("running") == 1, what="first photo running")
        b.cancel()           # forwarded: the child stops after the photo in progress
        time.sleep(1.0)      # the child checks stop.json every 0.25 s
        with open(gate, "w"):
            pass
        out, _ = child.communicate(timeout=60)
        assert child.returncode == 0, out.decode(errors="replace")
    finally:
        if child.poll() is None:
            child.kill()
    sm = batchmod.get_batch(b.id).summary()
    assert sm["state"] == "cancelled"
    assert sm["counts"] == {"done": 1, "cancelled": 2}
    assert sm["entries"][0]["out"] == "/out/0.jpg"
    # and this window never wrote its stale copy over the child's journal
    with open(b.jpath, encoding="utf-8") as fh:
        assert json.load(fh)["state"] == "cancelled"


def test_stop_forwarded_in_process_simulation(elsewhere):
    """Same as above with two Batch objects on one batch folder (deterministic)."""
    a = _new(2)
    b = batchmod.Batch(a.id)          # the other window's copy
    a.mark(0, "running")
    assert b.summary()["counts"] == {"running": 1, "staged": 1}
    b.cancel()                        # A holds the run lock: B only raises the stop epoch
    assert b.stop_epoch() == 1
    a.mark(0, "done")
    with a._txn():
        a.data["entries"]["1"]["state"] = "cancelled"
        a.data["state"] = "cancelled"
        a._write()
    sm = b.summary()
    assert sm["state"] == "cancelled" and sm["counts"] == {"done": 1, "cancelled": 1}


# ------------------------------------------------------------------ batch journal siblings

def test_writes_from_two_processes_are_merged_never_overwritten():
    a = _new(3)
    b = batchmod.Batch(a.id)          # read before A's changes
    a.mark(0, "done", out="/out/0.jpg")
    b.mark(1, "failed", error="disk")  # its copy is stale: the change applies to A's journal
    a.mark(2, "excluded")
    c = batchmod.Batch(a.id)
    states = {k: e["state"] for k, e in c.data["entries"].items()}
    assert states == {"0": "done", "1": "failed", "2": "excluded"}
    assert c.data["entries"]["0"]["out"] == "/out/0.jpg"


def test_get_batch_revalidates_the_cached_batch():
    a = _new(2)
    other = batchmod.Batch(a.id)
    other.mark(1, "failed", error="x")
    with other._txn():
        other.data["state"] = "finished"
        other._write()
    got = batchmod.get_batch(a.id)
    assert got is a and got.data["state"] == "finished"
    assert got.data["entries"]["1"]["state"] == "failed"


def test_cancel_here_applies_to_the_journal_as_it_is_now():
    """Stop with no run anywhere: queued photos become cancelled, photos another process saved
    in the meantime stay saved (no stale "staged" written back)."""
    a = _new(3)
    other = batchmod.Batch(a.id)
    other.mark(0, "done", out="/out/0.jpg")
    a.cancel()
    c = batchmod.Batch(a.id)
    assert {k: e["state"] for k, e in c.data["entries"].items()} == {"0": "done", "1": "cancelled", "2": "cancelled"}


def test_retry_requeues_from_the_journal_as_it_is_now(monkeypatch):
    a = _new(2)
    other = batchmod.Batch(a.id)
    other.mark(0, "failed", error="x")
    other.mark(1, "done", out="/out/1.jpg")
    seen = {}

    def fake_run(self, workers):
        seen.update({k: e["state"] for k, e in self.data["entries"].items()})
        with self._txn():
            self.data["state"] = "finished"
            self._write()
        return False
    monkeypatch.setattr(batchmod.Batch, "_run", fake_run)
    a.retry_failed()
    a._thread.join(10)
    assert seen == {"0": "retry", "1": "done"}


def test_discard_refused_while_another_process_runs_it(elsewhere):
    a = _new(1)
    with pytest.raises(batchmod.BatchBusy):
        batchmod.discard_batch(a.id)
    assert batchmod.Batch(a.id).data["state"] != "discarded"


def test_restore_sees_photos_saved_by_another_process(tmp_path):
    a = _new(1)
    other = batchmod.Batch(a.id)
    other.mark(0, "done", out="/x/0.jpg", backup=str(tmp_path / "gone.jpg"))
    r = a.restore_originals()   # a stale copy would find nothing to restore
    assert [x["result"] for x in r["results"]] == ["missing-backup"]


def test_incomplete_batches_skips_one_running_or_staging_in_another_process():
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        running = _new(2)
        with running._txn():
            running.data["state"] = "running"
            running._write()
        with open(os.path.join(running.dir, "run.lock"), "w") as fh:
            json.dump({"pid": sleeper.pid, "time": time.time()}, fh)
        staging = batchmod.new_batch({"count": 2, "files": ["/x/a.jpg", "/x/b.jpg"]})
        staging.stage(0, {"path": "/x/a.jpg", "mode": "overwrite"}, [])
        data = json.load(open(staging.jpath, encoding="utf-8"))
        data["pid"] = sleeper.pid      # being prepared in the other window
        with open(staging.jpath, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        crashed = batchmod.new_batch({"count": 1, "files": ["/x/c.jpg"]})
        crashed.stage(0, {"path": "/x/c.jpg", "mode": "overwrite"}, [])
        for b in (running, staging, crashed):
            batchmod._batches.pop(b.id, None)
        ids = {x["id"] for x in batchmod.incomplete_batches()}
        assert running.id not in ids and staging.id not in ids
        assert crashed.id in ids       # not running anywhere: offered for resume
    finally:
        sleeper.kill()
        sleeper.wait()
    # once the other process is gone both are offered for resume
    ids = {x["id"] for x in batchmod.incomplete_batches()}
    assert running.id in ids and staging.id in ids


def test_batch_ids_are_ascii():
    for bad in ("2026é", "20²6", "２０２６", "a/b", "", "a" * 65, "x\n"):
        with pytest.raises(batchmod.BatchNotFound):
            batchmod._bdir(bad)
    assert batchmod._bdir("20260101-120000-abc123")


# ------------------------------------------------------------------ settings

def test_settings_saved_by_two_processes_keep_both_changes():
    code = """
        import sys
        from photoband.settings import save_settings
        key, tag = sys.argv[1], sys.argv[2]
        for i in range(25):
            save_settings({"session": {key: "%s-%d" % (tag, i)}})
    """

    def run(key, tag):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen([sys.executable, "-c", textwrap.dedent(code), key, tag], env=env, cwd=ROOT)
    p1, p2 = run("lastTemplate", "one"), run("lastFolder", "two")
    assert p1.wait(120) == 0 and p2.wait(120) == 0
    s = settings.load_settings()["session"]
    assert s["lastTemplate"] == "one-24" and s["lastFolder"] == "two-24"


# ------------------------------------------------------------------ drafts

def test_draft_stat_refresh_never_overwrites_a_newer_draft(tmp_path, monkeypatch):
    p = str(tmp_path / "p.jpg")
    with open(p, "wb") as fh:
        fh.write(b"x" * 100)
    drafts.save_draft(p, {"v": 1})
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # touched: same content
    real = drafts._check

    def check_then_other_window_autosaves(d):
        ok = real(d)
        drafts._save_draft(p, {"v": 2})   # another process, between the read and the refresh
        return ok
    monkeypatch.setattr(drafts, "_check", check_then_other_window_autosaves)
    assert drafts.load_draft(p) == {"v": 1}
    monkeypatch.setattr(drafts, "_check", real)
    assert drafts.load_draft_any(p) == {"v": 2}


def test_draft_compare_and_delete_waits_for_another_process(tmp_path):
    """The draft lock is shared across processes: held by another live process, a
    compare-and-delete waits for it instead of interleaving with its autosave."""
    p = str(tmp_path / "q.jpg")
    with open(p, "wb") as fh:
        fh.write(b"y" * 10)
    drafts.save_draft(p, {"_hash": "h1"})
    child = _child(f"""
        import time
        from photoband import drafts
        with drafts._locked({p!r}):
            print("held", flush=True)
            time.sleep(0.8)
            drafts._save_draft({p!r}, {{"_hash": "h2"}})   # its autosave, under the draft lock
    """)
    try:
        assert child.stdout.readline().strip() == b"held"
        assert drafts.delete_draft_if(p, "h1") is False   # waited: the draft is now h2
        assert drafts.load_draft_any(p) == {"_hash": "h2"}
        assert child.wait(60) == 0
    finally:
        if child.poll() is None:
            child.kill()


# ------------------------------------------------------------------ font index

def test_font_added_by_another_process_is_found(monkeypatch):
    monkeypatch.setattr(fonts, "SIG_CHECK_S", 0.0)
    fonts.build_index(include_system=False)
    src = os.path.join(ROOT, "fonts", "caveat", "Caveat[wght].ttf")
    dst = os.path.join(paths.sub("fonts", "user"), "CrossProcessCaveat.ttf")
    try:
        before = fonts.index()
        assert dst not in before["files"].values()
        shutil.copy2(src, dst)       # what add_font_file does in the other window
        # a folder's modified time can be coarse: make sure it moved
        st = os.stat(os.path.dirname(dst))
        os.utime(os.path.dirname(dst), ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
        assert dst in fonts.index()["files"].values()
    finally:
        os.unlink(dst)
        fonts.build_index(include_system=False)


# ------------------------------------------------------------------ photo caches keyed by stat

def test_photo_caches_tell_a_replaced_file_with_the_same_size_and_time(tmp_path):
    """A save in another process can replace a photo keeping its size and modified time (a copy
    re-saved with the source's dates, an uncompressed TIFF): the file id tells them apart."""
    p = str(tmp_path / "a.tif")
    Image.new("RGB", (16, 16), (10, 20, 30)).save(p, compression="raw")
    st = os.stat(p)
    i1 = probe(p)
    k1, c1 = photos._key(p, i1), proxy.cache_key(p, i1.size_bytes, i1.mtime_ns, i1.file_id)
    tmp = str(tmp_path / "b.tif")
    Image.new("RGB", (16, 16), (200, 100, 50)).save(tmp, compression="raw")
    assert os.path.getsize(tmp) == st.st_size
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    keep = str(tmp_path / "keep.tif")
    os.replace(p, keep)                # the old file still exists: its id is not reused
    os.replace(tmp, p)
    i2 = probe(p)
    assert (i2.size_bytes, i2.mtime_ns) == (i1.size_bytes, i1.mtime_ns)
    assert photos._key(p, i2) != k1
    assert proxy.cache_key(p, i2.size_bytes, i2.mtime_ns, i2.file_id) != c1


# ------------------------------------------------------------------ anchored validation (server)

def test_token_and_host_patterns_match_whole_strings_only():
    from photoband import server
    assert server._QUERY_TOKEN_RE.fullmatch("/api/photo/proxy")
    assert server._QUERY_TOKEN_RE.fullmatch("/api/batch/20260101-120000-abc123/report.csv")
    for bad in ("/api/photo/proxy\n", "/api/photo/proxyX", "/api/photo/proxy/../x", "x/api/photo/proxy"):
        assert not server._QUERY_TOKEN_RE.fullmatch(bad), bad
    assert server._HOST_RE.fullmatch("127.0.0.1:8765") and server._HOST_RE.fullmatch("localhost")
    for bad in ("localhost\n", "localhost:8765\n", "localhost.evil.com", "127.0.0.1:123456"):
        assert not server._HOST_RE.fullmatch(bad), bad
