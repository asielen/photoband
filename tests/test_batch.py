"""Batch queue: isolation of failures, changed files, journal resume without double writes, restore."""
import json
import os
import shutil
import time

import pytest

from conftest import make_band_layout
from photoband import batch as batchmod
from photoband.imageio import load_upright, probe
from photoband.settings import load_settings


def _stage(b, idx, path, mode="overwrite", expected=None, dest=None):
    arr, info = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=f"Photo {idx}")
    st = probe(path)
    job = {"path": path, "mode": mode, "layout": layout, "state": {"blocks": []},
           "expected_stat": expected or [st.size_bytes, str(st.mtime_ns)], "settings": load_settings()}
    if dest:
        job["mode"] = "copyAs"
        job["dest_path"] = dest
    b.stage(idx, job, [{"x": t.x, "y": t.y, "png": t.png} for t in tiles])


def _wait(b, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if b.data.get("state") in ("finished", "cancelled") and not (b._thread and b._thread.is_alive()):
            return
        time.sleep(0.2)
    raise TimeoutError("batch did not finish")


def test_batch_isolates_failures_and_restores(work):
    paths = [work(n) for n in ("02_gray8_uncompressed.tif", "05_person_in_image.png", "09_partial_date.jpg",
                               "07a_multipage.tif")]
    originals = {p: open(p, "rb").read() for p in paths}
    b = batchmod.new_batch({"count": len(paths), "files": paths})
    for i, p in enumerate(paths):
        _stage(b, i, p)
    # plant a "changed during batch" file
    os.utime(paths[2], ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    b.staging_complete()
    b.start(workers=2)
    _wait(b)
    s = b.summary()
    states = {e["path"]: e["state"] for e in s["entries"]}
    assert states[paths[0]] == "done" and states[paths[1]] == "done"
    assert states[paths[2]] == "changed"
    assert states[paths[3]] == "failed"  # multipage blocked, but the batch carried on
    assert "multi-page" in [e for e in s["entries"] if e["path"] == paths[3]][0]["error"].lower()
    assert s["canRestore"]
    csv_text = b.report_csv()
    assert "changed" in csv_text and "failed" in csv_text
    r = b.restore_originals()
    assert r["restored"] == 2 and not r["errors"]
    for p in paths[:2]:
        assert open(p, "rb").read() == originals[p]


def test_resume_does_not_write_twice(work, tmp_path):
    paths = [work(n) for n in ("02_gray8_uncompressed.tif", "05_person_in_image.png")]
    outs = [str(tmp_path / f"out{i}.tif") for i in range(2)]
    b = batchmod.new_batch({"count": 2, "files": paths})
    for i, p in enumerate(paths):
        _stage(b, i, p, mode="copy", dest=outs[i].replace(".tif", os.path.splitext(p)[1]))
    b.staging_complete()
    # simulate a crash: job 0 finished writing but the journal still says "running"; job 1 never started
    from photoband.batch import run_job
    r0 = run_job(b.id, 0)
    assert r0["ok"]
    mtime0 = os.stat(r0["out_path"]).st_mtime_ns
    b.mark(0, "running")
    b.mark(1, "running")
    b.data["state"] = "running"
    b._write()
    # new process view of the batch (as after relaunch)
    batchmod._batches.clear()
    assert any(x["id"] == b.id for x in batchmod.incomplete_batches())  # offered for resume on launch
    b2 = batchmod.get_batch(b.id)
    b2.start(workers=2)
    _wait(b2)
    s = b2.summary()
    assert [e["state"] for e in s["entries"]] == ["done", "done"]
    assert os.stat(r0["out_path"]).st_mtime_ns == mtime0  # not written again
    assert "Recovered after restart" in s["entries"][0]["notes"]


def test_kill_mid_save_leaves_original(work, monkeypatch):
    from photoband import save as savemod
    p = work("02_gray8_uncompressed.tif")
    before = open(p, "rb").read()
    arr, info = load_upright(p)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(savemod, "write_metadata", boom)
    r = savemod.save(savemod.SaveRequest(path=p, mode="overwrite", layout=layout, tiles=tiles, state={},
                                         settings=load_settings()))
    assert not r.ok
    assert open(p, "rb").read() == before
    assert not [f for f in os.listdir(os.path.dirname(p)) if f.startswith(".pbtmp-")]


# -- crash safety: workers die with the app, nothing is written twice, restore covers all -----

def _children(pid):
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as fh:
                    f = fh.read().rsplit(")", 1)[1].split()
                if int(f[1]) == pid and f[0] != "Z":
                    out.append(int(d))
            except (OSError, IndexError, ValueError):
                pass
    return out


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="needs /proc (Linux)")
def test_kill_mid_run_spawned_workers_die_and_resume_writes_once(work, tmp_path):
    import signal
    import subprocess
    import sys
    names = ("02_gray8_uncompressed.tif", "05_person_in_image.png", "09_partial_date.jpg")
    paths = [work(n) for n in names]
    originals = {p: open(p, "rb").read() for p in paths}
    b = batchmod.new_batch({"count": len(paths), "files": paths})
    for i, p in enumerate(paths):
        _stage(b, i, p)
    b.staging_complete()
    # the "app": runs the batch with workers that stall right before placing any file
    code = ("import sys, time; from photoband import batch as m; b = m.get_batch(sys.argv[1]); "
            "b.start(workers=2); time.sleep(600)")
    env = dict(os.environ, PHOTOBAND_TEST_WORKER_DELAY="4")
    app_proc = subprocess.Popen([sys.executable, "-c", code, b.id], env=env,
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        t0 = time.time()
        while time.time() - t0 < 60:
            j = json.load(open(b.jpath))
            if sum(e["state"] == "running" for e in j["entries"].values()) >= 2:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("workers never started")
        time.sleep(1.0)  # workers are inside their saves now
        workers = _children(app_proc.pid)
        assert workers, "no worker processes found"
        # spawned, not forked: a worker has no copy of the app's descriptors (sockets)
        for w in workers:
            with open(f"/proc/{w}/cmdline", "rb") as fh:
                assert b"multiprocessing" in fh.read()
        os.kill(app_proc.pid, signal.SIGKILL)
        app_proc.wait()
        t1 = time.time()
        while time.time() - t1 < 5 and any(_alive(w) for w in workers):
            time.sleep(0.1)
        assert not [w for w in workers if _alive(w)], "orphaned workers survived the app"
    finally:
        if app_proc.poll() is None:
            app_proc.kill()
    for p in paths:  # nothing was replaced by the killed run
        assert open(p, "rb").read() == originals[p]
    # relaunch: resume the batch
    batchmod._batches.clear()
    assert any(x["id"] == b.id for x in batchmod.incomplete_batches())
    b2 = batchmod.get_batch(b.id)
    b2.start(workers=2)
    _wait(b2)
    s = b2.summary()
    assert [e["state"] for e in s["entries"]] == ["done"] * 3
    from photoband import record
    from photoband.exiftool import get as et_get
    for i, p in enumerate(paths):
        assert record.from_metadata(et_get().read_json(p))["batchJob"] == f"{b.id}:{i}"
        bdir = os.path.join(os.path.dirname(p), "_originals")
        stem = os.path.splitext(os.path.basename(p))[0]
        # written once: exactly one backup of each original (a second write would back up our output)
        assert len([f for f in os.listdir(bdir) if f.startswith(stem)]) == 1
    r = b2.restore_originals()
    assert r["restored"] == 3 and not r["errors"]
    for p in paths:
        assert open(p, "rb").read() == originals[p]


def test_resume_waits_for_a_job_still_held_by_a_live_process(work):
    import subprocess
    import sys
    paths = [work(n) for n in ("02_gray8_uncompressed.tif", "05_person_in_image.png")]
    b = batchmod.new_batch({"count": 2, "files": paths})
    for i, p in enumerate(paths):
        _stage(b, i, p)
    b.staging_complete()
    b.mark(0, "running")
    b.data["state"] = "running"
    b._write()
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with open(os.path.join(b.dir, "00000", "worker.lock"), "w") as fh:
            json.dump({"pid": holder.pid, "time": time.time()}, fh)
        batchmod._batches.clear()
        b2 = batchmod.get_batch(b.id)
        b2.start(workers=1)
        t0 = time.time()
        while b2.data["entries"]["1"]["state"] != "done" and time.time() - t0 < 60:
            time.sleep(0.2)
        assert b2.data["entries"]["1"]["state"] == "done"
        time.sleep(1.0)
        assert b2.data["entries"]["0"]["state"] == "running"  # not re-queued while held
        assert b2._thread.is_alive()
    finally:
        holder.kill()
        holder.wait()
    _wait(b2)
    assert [e["state"] for e in b2.summary()["entries"]] == ["done", "done"]


def test_restore_uses_journaled_backup_when_entry_not_marked_done(work):
    from photoband.batch import run_job
    p = work("02_gray8_uncompressed.tif")
    before = open(p, "rb").read()
    b = batchmod.new_batch({"count": 1, "files": [p]})
    _stage(b, 0, p)
    r = run_job(b.id, 0)
    assert r["ok"] and r["backup_path"]
    b.mark(0, "failed", error="Changed during batch")  # e.g. a later attempt saw "our" file as changed
    assert b.summary()["canRestore"]
    res = b.restore_originals()
    assert res["restored"] == 1, res
    assert open(p, "rb").read() == before


def test_failed_rerun_of_our_own_output_is_adopted(work):
    from photoband.batch import run_job
    p = work("02_gray8_uncompressed.tif")
    b = batchmod.new_batch({"count": 1, "files": [p]})
    _stage(b, 0, p)
    assert run_job(b.id, 0)["ok"]  # written by an attempt the journal never recorded
    b.mark(0, "staged")
    b.staging_complete()
    b.start(workers=1)
    _wait(b)
    e = b.summary()["entries"][0]
    assert e["state"] == "done" and e["backup"] and "Saved by an earlier attempt" in e["notes"]


# -- journal: every photo of the plan, crash before staging, stopped photos ----------------

def test_unsaved_plan_items_are_journaled_and_reported(work):
    p = work("02_gray8_uncompressed.tif")
    plan = [{"index": 0, "path": p, "status": "ready", "action": "band", "reasons": []},
            {"index": None, "path": "/x/a.jpg", "status": "skipped", "action": "skip", "reasons": ["has an existing caption"]},
            {"index": None, "path": "/x/b.jpg", "status": "blocked", "action": "band", "reasons": ["Multi-page TIFF"]},
            {"index": None, "path": "/x/c.jpg", "status": "held", "action": "band", "reasons": ["held back for review", "no names found"]},
            {"index": None, "path": "/x/d.jpg", "status": "excluded", "action": "band", "reasons": ["excluded in review"]},
            {"index": None, "path": "/x/e.jpg", "status": "error", "action": "band", "reasons": ["Empty file"]}]
    b = batchmod.new_batch({"count": 1, "files": [p], "plan": plan, "folder": "/x"},
                           unsaved=[x for x in plan if x["index"] is None])
    s = b.summary()
    assert s["expected"] == 6
    assert s["counts"] == {"skipped": 1, "blocked": 2, "held": 1, "excluded": 1}
    csv_text = b.report_csv()
    for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"):
        assert name in csv_text
    assert "no names found" in csv_text and "Empty file" in csv_text
    # photo 0 was never staged: the batch is offered for resume
    batchmod._batches.pop(b.id)
    inc = [x for x in batchmod.incomplete_batches() if x["id"] == b.id]
    assert inc and inc[0]["missing"] == 1 and inc[0]["meta"]["plan"][0]["path"] == p


def test_crash_before_staging_is_offered_for_resume():
    b = batchmod.new_batch({"count": 3, "files": ["/x/a.jpg", "/x/b.jpg", "/x/c.jpg"]})
    batchmod._batches.pop(b.id)
    inc = [x for x in batchmod.incomplete_batches() if x["id"] == b.id]
    assert inc and inc[0]["pending"] == 3 and inc[0]["missing"] == 3


def test_stopped_and_unprepared_entries_are_not_retried_without_a_job(work):
    p = work("02_gray8_uncompressed.tif")
    b = batchmod.new_batch({"count": 3, "files": [p, "/x/b.jpg", "/x/c.jpg"]})
    _stage(b, 0, p)
    b.mark(1, "cancelled", path="/x/b.jpg", error="Stopped before it was prepared")
    b.mark(2, "failed", path="/x/c.jpg", error="Could not prepare: boom")
    s = b.summary()
    assert s["counts"] == {"staged": 1, "cancelled": 1, "failed": 1}
    ents = {e["index"]: e for e in s["entries"]}
    assert ents[1]["hasJob"] is False and ents[2]["hasJob"] is False
    assert "b.jpg" in b.report_csv()
    b.retry_failed()  # runs the staged photo; the two without a job wait for the UI to stage them
    _wait(b)
    ents = {e["index"]: e for e in b.summary()["entries"]}
    assert ents[0]["state"] == "done"
    assert ents[1]["state"] == "cancelled" and ents[2]["state"] == "failed"


# -- drafts ----------------------------------------------------------------------------------

def test_batch_save_clears_the_draft_it_used(work):
    from photoband import drafts
    p1, p2 = work("02_gray8_uncompressed.tif"), work("05_person_in_image.png")
    drafts.save_draft(p1, {"templateId": "t", "_hash": "h1"})
    drafts.save_draft(p2, {"templateId": "t", "_hash": "newer"})  # edited after pre-flight
    b = batchmod.new_batch({"count": 2, "files": [p1, p2]})
    for i, p in enumerate((p1, p2)):
        _stage(b, i, p)
        jp = os.path.join(b.dir, f"{i:05d}", "job.json")
        job = json.load(open(jp))
        job["draft_hash"] = "h1"
        json.dump(job, open(jp, "w"))
    b.staging_complete()
    b.start(workers=2)
    _wait(b)
    assert [e["state"] for e in b.summary()["entries"]] == ["done", "done"]
    assert drafts.load_draft_any(p1) is None
    assert drafts.load_draft_any(p2) == {"templateId": "t", "_hash": "newer"}


# -- memory-aware worker count -------------------------------------------------------------

def test_auto_workers_follow_memory():
    GB = 1024 ** 3
    assert batchmod.auto_workers(10 * 1024 ** 2, avail=16 * GB, cpu=8) == 4       # small photos: cpu cap
    assert batchmod.auto_workers(10 * 1024 ** 2, avail=16 * GB, cpu=2) == 2
    big = 600 * 1024 ** 2                                                         # a 600 MB decoded scan
    assert batchmod.auto_workers(big, avail=8 * GB, cpu=8) == int(8 * GB * 0.6 // (big * 6))
    assert batchmod.auto_workers(big, avail=1 * GB, cpu=8) == 1                   # never below one
    assert batchmod.available_memory() > 0


def test_decoded_bytes_from_probe(work):
    p = work("02_gray8_uncompressed.tif")
    info = probe(p)
    assert batchmod.decoded_bytes(p) == info.width * info.height * info.channels * (2 if "16" in info.dtype else 1)
    assert batchmod.decoded_bytes("/no/such/file.tif") == 0
