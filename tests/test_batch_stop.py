"""Stopping a batch save and a pre-flight check: no new photo starts, the ones in progress finish
(never half written), the rest is reported as stopped and can be run again; an overwrite batch
stopped part way can still restore the originals it replaced."""
import concurrent.futures as cf
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from photoband import batch as batchmod
from photoband import photos, security, server

from test_batch import _stage, _wait


def _until(cond, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.02)
    raise TimeoutError("condition not reached")


@pytest.fixture
def threads(monkeypatch):
    """Workers as threads (the stop logic is the same; timing can be controlled)."""
    monkeypatch.setattr(batchmod, "_new_pool", lambda n: cf.ThreadPoolExecutor(max_workers=n))


def _fake_jobs(monkeypatch, gate):
    started = []
    lock = threading.Lock()

    def fake_run_job(bid, idx):
        with lock:
            started.append(idx)
        assert gate.wait(30)
        return {"ok": True, "out_path": f"/out/{idx}.jpg", "out_identity": [1, 1]}

    monkeypatch.setattr(batchmod, "run_job", fake_run_job)
    return started


def test_stop_finishes_photos_in_progress_and_starts_no_new_ones(threads, monkeypatch):
    gate = threading.Event()
    started = _fake_jobs(monkeypatch, gate)
    b = batchmod.new_batch({"count": 5, "files": [f"/x/{i}.jpg" for i in range(5)]})
    for i in range(5):
        b.stage(i, {"path": f"/x/{i}.jpg"}, [])
    b.staging_complete()
    b.start(workers=2)
    _until(lambda: len(started) == 2)
    b.cancel()
    time.sleep(0.4)
    assert b.data["state"] == "running"   # still finishing the two in progress
    gate.set()
    _wait(b)
    s = b.summary()
    assert sorted(started) == [0, 1]       # nothing started after Stop
    assert s["state"] == "cancelled"
    assert s["counts"] == {"done": 2, "cancelled": 3}
    for e in s["entries"]:
        if e["state"] == "cancelled":
            assert e["error"] == "Stopped before it was saved" and e["hasJob"]


def test_photo_staged_after_stop_is_kept_and_retry_runs_it(threads, monkeypatch):
    gate = threading.Event()
    gate.set()
    started = _fake_jobs(monkeypatch, gate)
    b = batchmod.new_batch({"count": 3, "files": [f"/x/{i}.jpg" for i in range(3)]})
    b.start(workers=1)
    b.stage(0, {"path": "/x/0.jpg"}, [])
    _until(lambda: b.data["entries"]["0"]["state"] == "done")
    b.cancel()
    _wait(b)
    # the photo being prepared when Stop was pressed arrives afterwards: kept, not run
    b.stage(1, {"path": "/x/1.jpg"}, [])
    assert b.data["entries"]["1"]["state"] == "cancelled"
    time.sleep(0.3)
    assert started == [0]
    b.retry_failed()
    _wait(b)
    assert b.summary()["counts"] == {"done": 2}
    assert started == [0, 1]


def test_stop_while_not_running_stops_the_queue_at_once():
    """Stop pressed while photos are prepared again for a retry (no run yet)."""
    b = batchmod.new_batch({"count": 2, "files": ["/x/0.jpg", "/x/1.jpg"]})
    b.stage(0, {"path": "/x/0.jpg"}, [])
    b.data["state"] = "running"   # e.g. resumed after a crash, run not started yet
    b.cancel()
    assert b.data["state"] == "cancelled"
    assert b.data["entries"]["0"]["state"] == "cancelled"
    b.stage(1, {"path": "/x/1.jpg"}, [])
    assert b.data["entries"]["1"]["state"] == "cancelled"
    assert batchmod.read_json(b.jpath)["state"] == "cancelled"


def test_stopped_overwrite_batch_can_restore_what_it_saved(threads, work, monkeypatch):
    paths = [work(n) for n in ("02_gray8_uncompressed.tif", "05_person_in_image.png", "09_partial_date.jpg")]
    originals = {p: open(p, "rb").read() for p in paths}
    real = batchmod.run_job
    in_second = threading.Event()
    go = threading.Event()

    def run_job(bid, idx):
        if idx == 1:
            in_second.set()
            assert go.wait(60)
        return real(bid, idx)

    monkeypatch.setattr(batchmod, "run_job", run_job)
    b = batchmod.new_batch({"count": 3, "files": paths})
    for i, p in enumerate(paths):
        _stage(b, i, p)
    b.staging_complete()
    b.start(workers=1)
    assert in_second.wait(120)
    b.cancel()          # while photo 2 of 3 is being saved
    go.set()
    _wait(b)
    s = b.summary()
    states = [e["state"] for e in s["entries"]]
    assert states == ["done", "done", "cancelled"]
    assert open(paths[2], "rb").read() == originals[paths[2]]   # never started: untouched
    for p in paths[:2]:
        assert open(p, "rb").read() != originals[p]
    # no temp files left next to the photos
    folder = os.path.dirname(paths[0])
    assert not [f for f in os.listdir(folder) if f.startswith((".pbtmp-", ".pbbak-"))]
    assert s["canRestore"]
    r = b.restore_originals()
    assert r["restored"] == 2 and not r["errors"]
    for p in paths:
        assert open(p, "rb").read() == originals[p]


# ---------------------------------------------------------------- pre-flight

@pytest.fixture
def client():
    security.reset()
    c = TestClient(server.app, base_url="http://127.0.0.1")
    c.headers["X-Photoband-Token"] = security.TOKEN
    yield c
    security.reset()


def test_preflight_stop_lets_photos_in_progress_finish(client, tmp_path, monkeypatch):
    files = []
    for i in range(12):
        f = tmp_path / f"p{i:02d}.jpg"
        f.write_bytes(b"x")
        files.append(str(f))
    security.allow_root(str(tmp_path))
    gate = threading.Event()
    begun = []

    def meta(p):
        begun.append(p)
        assert gate.wait(30)
        return {"info": {"save_blocked": "test"}, "fields": {}}

    monkeypatch.setattr(photos, "meta", meta)
    pid = client.post("/api/batch/preflight", json={"paths": files}).json()["id"]
    _until(lambda: client.get(f"/api/batch/preflight/{pid}").json()["active"] >= 1)
    time.sleep(0.2)
    assert client.post(f"/api/batch/preflight/{pid}/cancel").json()["ok"]
    st = client.get(f"/api/batch/preflight/{pid}").json()
    assert not st["stopped"] and st["active"] >= 1   # still checking the ones in progress
    n = len(begun)
    gate.set()
    _until(lambda: client.get(f"/api/batch/preflight/{pid}").json()["stopped"])
    time.sleep(0.3)
    st = client.get(f"/api/batch/preflight/{pid}").json()
    assert st["stopped"] and st["active"] == 0
    assert len(begun) == n < len(files)              # nothing started after Stop
    assert st["done"] == n == len(st["results"])     # the ones in progress were reported
    assert all(r["blocked"] == "test" for r in st["results"])
