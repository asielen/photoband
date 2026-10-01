"""Stop must win over a run/retry that was requested before it, whatever order the requests reach
the server in (a persisted stop epoch), also when another process holds the run; poll snapshots
are consistent; the batch preview uses the batch's own inputs and rules; request validation."""
import base64
import concurrent.futures as cf
import io
import json
import os
import shutil
import threading
import time
import zlib

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import make_band_layout
from photoband import batch as batchmod
from photoband import photos, security, server
from photoband.imageio import load_upright
from photoband.settings import reset_settings, save_settings

from test_batch import _wait


@pytest.fixture
def threads(monkeypatch):
    monkeypatch.setattr(batchmod, "_new_pool", lambda n: cf.ThreadPoolExecutor(max_workers=n))


@pytest.fixture(autouse=True)
def _fresh_settings():
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def client():
    security.reset()
    c = TestClient(server.app, base_url="http://127.0.0.1")
    c.headers["X-Photoband-Token"] = security.TOKEN
    yield c
    security.reset()


def _fake(monkeypatch):
    started = []

    def fake_run_job(bid, idx):
        started.append(idx)
        return {"ok": True, "out_path": f"/out/{idx}.jpg", "out_identity": [1, 1]}
    monkeypatch.setattr(batchmod, "run_job", fake_run_job)
    return started


def _staged(n, state=None, error=""):
    b = batchmod.new_batch({"count": n, "files": [f"/x/{i}.jpg" for i in range(n)]})
    for i in range(n):
        b.stage(i, {"path": f"/x/{i}.jpg"}, [])
        if state:
            b.mark(i, state, error=error)
    return b


# ------------------------------------------------------------------ stop vs. a later start


def test_retry_requested_before_stop_is_refused(threads, monkeypatch):
    started = _fake(monkeypatch)
    b = _staged(3, "failed", "boom")
    b.data["state"] = "finished"
    ep = b.summary()["epoch"]          # what the UI saw when Retry was pressed
    b.cancel()                          # Stop reaches the server first
    with pytest.raises(batchmod.BatchStopped):
        b.retry_failed(epoch=ep)        # the earlier Retry arrives late
    time.sleep(0.3)
    assert started == []
    assert [e["state"] for e in b.data["entries"].values()] == ["failed"] * 3
    assert not (b._thread and b._thread.is_alive())
    # a Retry pressed after the Stop (current epoch) runs
    b.retry_failed(epoch=b.summary()["epoch"])
    _wait(b)
    assert sorted(started) == [0, 1, 2] and b.data["state"] == "finished"


def test_stop_during_409_backoff_is_recorded_and_wins(threads, monkeypatch):
    started = _fake(monkeypatch)
    b = _staged(2, "cancelled", "Stopped before it was saved")
    b.data["state"] = "cancelled"
    ep = b.summary()["epoch"]
    lk = os.path.join(b.dir, "run.lock")
    with open(lk, "w") as fh:
        json.dump({"pid": os.getppid(), "time": time.time()}, fh)   # another live process runs it
    with pytest.raises(batchmod.BatchBusy):
        b.retry_failed(epoch=ep)        # 409: the UI backs off
    # nothing was re-queued by the refused retry
    assert [e["state"] for e in b.data["entries"].values()] == ["cancelled", "cancelled"]
    b.cancel()                          # Stop during the back-off: recorded although another process holds the run
    assert b.stop_epoch() == ep + 1
    os.unlink(lk)
    with pytest.raises(batchmod.BatchStopped):
        b.retry_failed(epoch=ep)        # the back-off's next attempt
    time.sleep(0.3)
    assert started == []


def test_stop_before_run_leaves_batch_cancelled(threads, monkeypatch):
    started = _fake(monkeypatch)
    b = batchmod.new_batch({"count": 2, "files": ["/x/0.jpg", "/x/1.jpg"]})
    ep = b.stop_epoch()
    b.cancel()
    with pytest.raises(batchmod.BatchStopped):
        b.start(epoch=ep)
    for i in range(2):
        b.mark(i, "cancelled", path=f"/x/{i}.jpg", error="Stopped before it was saved")
    b.staging_complete()
    time.sleep(0.3)
    assert started == [] and b.data["state"] == "cancelled"
    assert batchmod.read_json(b.jpath)["state"] == "cancelled"
    # the run lock of the refused run was released
    assert batchmod._lock_holder(os.path.join(b.dir, "run.lock")) is None


def test_stop_from_another_process_stops_the_run(threads, monkeypatch):
    gate = threading.Event()
    started = []

    def run_job(bid, idx):
        started.append(idx)
        assert gate.wait(30)
        return {"ok": True, "out_path": f"/out/{idx}.jpg", "out_identity": [1, 1]}
    monkeypatch.setattr(batchmod, "run_job", run_job)
    b = _staged(4)
    b.staging_complete()
    b.start(workers=1, epoch=0)
    t0 = time.time()
    while not started and time.time() - t0 < 10:
        time.sleep(0.02)
    # what cancel() in another Photoband process writes (its journal is not this process's)
    batchmod.atomic_write_json(os.path.join(b.dir, "stop.json"), {"epoch": 1, "time": time.time()})
    time.sleep(0.6)
    gate.set()
    _wait(b)
    assert started == [0]
    assert b.summary()["counts"] == {"done": 1, "cancelled": 3}
    assert b.data["state"] == "cancelled"


def test_photos_queued_after_the_run_found_nothing_left_still_run(threads, monkeypatch):
    """A retry re-queues while the run thread is ending: the run takes another pass."""
    started = _fake(monkeypatch)
    b = _staged(1, "retry")
    b.staging_complete()
    real = b._pending
    calls = {"n": 0}

    def pending():
        calls["n"] += 1
        return [] if calls["n"] <= 2 else real()   # the first pass sees an empty queue
    monkeypatch.setattr(b, "_pending", pending)
    b.start(workers=1)
    _wait(b)
    assert started == [0] and b.data["state"] == "finished"


def test_discard_refused_while_running(threads, monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(batchmod, "run_job", lambda bid, idx: gate.wait(30) and {"ok": True, "out_path": "/o"})
    b = _staged(1)
    b.staging_complete()
    b.start(workers=1)
    with pytest.raises(batchmod.BatchBusy):
        batchmod.discard_batch(b.id)
    gate.set()
    _wait(b)
    batchmod.discard_batch(b.id)
    assert b.data["state"] == "discarded"


def test_run_and_retry_endpoints_honour_the_stop_epoch(client, threads, monkeypatch):
    started = _fake(monkeypatch)
    r = client.post("/api/batch/create", json={"files": [], "plan": []})
    bid, ep = r.json()["id"], r.json()["epoch"]
    assert ep == 0
    assert client.post(f"/api/batch/{bid}/cancel").json()["epoch"] == 1
    assert client.post(f"/api/batch/{bid}/run", json={"epoch": ep}).json() == {"ok": False, "stopped": True}
    assert client.post(f"/api/batch/{bid}/retry", json={"epoch": ep}).json() == {"ok": False, "stopped": True}
    st = client.get(f"/api/batch/{bid}").json()
    assert st["state"] == "cancelled" and st["epoch"] == 1
    assert client.post(f"/api/batch/{bid}/run", json={"epoch": "x"}).status_code == 400
    client.post(f"/api/batch/{bid}/staging-complete")
    assert client.post(f"/api/batch/{bid}/run", json={"epoch": 1}).json() == {"ok": True}
    _wait(batchmod.get_batch(bid))
    assert started == []


def test_summary_counts_what_restore_would_put_back():
    b = _staged(3)
    b.mark(0, "done", backup="/b/0.jpg")
    b.mark(1, "restored", backup="/b/1.jpg")    # already put back: not counted again
    b.mark(2, "failed")
    s = b.summary()
    assert s["restorable"] == 1 and s["canRestore"]
    b.mark(0, "restored")
    assert b.summary()["restorable"] == 0 and not b.summary()["canRestore"]


# ------------------------------------------------------------------ pre-flight poll snapshot


def _pre_dict():
    for r in server.app.routes:
        if getattr(r, "path", "") == "/api/batch/preflight/{pid}" and "GET" in r.methods:
            fn = r.endpoint
            for name, cell in zip(fn.__code__.co_freevars, fn.__closure__):
                if name == "_pre":
                    return cell.cell_contents
    raise RuntimeError("no _pre")


def test_preflight_poll_never_reports_stopped_without_the_last_results(client, tmp_path, monkeypatch):
    files = []
    for i in range(6):
        f = tmp_path / f"p{i}.jpg"
        f.write_bytes(b"x")
        files.append(str(f))
    security.allow_root(str(tmp_path))
    gate = threading.Event()

    def meta(p):
        assert gate.wait(30)
        return {"info": {"save_blocked": "test"}, "fields": {}}
    monkeypatch.setattr(photos, "meta", meta)
    pid = client.post("/api/batch/preflight", json={"paths": files}).json()["id"]
    st = _pre_dict()[pid]
    t0 = time.time()
    while not st["active"] and time.time() - t0 < 10:
        time.sleep(0.01)
    client.post(f"/api/batch/preflight/{pid}/cancel")

    class Racy(dict):
        armed = True

        def values(self):
            snap = list(super().values())   # the poll's snapshot of the results
            if Racy.armed:
                Racy.armed = False
                gate.set()                   # the photos being checked finish right now
                t = time.time()
                while st["active"] and time.time() - t < 1:
                    time.sleep(0.01)
            return snap
    st["results"] = Racy(st["results"])
    seen = []
    while True:
        r = client.get(f"/api/batch/preflight/{pid}", params={"since": len(seen)}).json()
        seen += r["results"]
        if r["stopped"]:
            break
    # every photo that was checked is reported before (or with) "stopped"
    assert len(seen) == len(st["results"]) and len(seen) >= 1


def test_preflight_rejects_wrong_types(client):
    assert client.post("/api/batch/preflight", json={"paths": "C:/x"}).status_code == 400
    assert client.post("/api/batch/scan", json={}).status_code == 400


# ------------------------------------------------------------------ batch save preview


def _copy(fixtures_dir, name, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(os.path.join(fixtures_dir, name), dst)
    return dst


def _form(path, mode="copy", tile_png=None, **extra):
    arr, _ = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    job = {"path": path, "mode": mode, "layout": layout,
           "tiles": [{"name": "t0", "x": tiles[0].x, "y": tiles[0].y}],
           "state": {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"}, "blocks": [],
                     "overrides": {}}}
    job.update(extra)
    return {"job": json.dumps(job)}, {"t0": ("t0.png", tile_png or tiles[0].png, "image/png")}


def test_batch_preview_names_the_copy_exactly_as_staging_does(client, tmp_path, fixtures_dir, monkeypatch):
    folder = tmp_path / "photos"
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(folder / "scan.jpg"))
    security.allow_root(str(folder))
    # tokens that need the photo's metadata and the template name; a name already taken
    save_settings({"saving": {"location": "same", "fileName": "{title}-{template}-{stem}", "onExists": "ask"}})
    real_meta = photos.meta

    def meta(p):
        m = dict(real_meta(p))
        m["fields"] = {**m["fields"], "title": "Picnic"}
        return m
    monkeypatch.setattr(photos, "meta", meta)
    (folder / "Picnic-Classic-scan.jpg").write_bytes(b"taken")
    pv = client.post("/api/save/preview", json={"path": src, "templateName": "Classic", "batch": True}).json()
    # the single-photo preview (no batch rule) would ask about the taken name
    single = client.post("/api/save/preview", json={"path": src, "templateName": "Classic"}).json()
    assert single["copyExists"] is True
    bid = client.post("/api/batch/create", json={"files": [src], "plan": []}).json()["id"]
    data, files = _form(src, fields=photos.meta(src)["fields"], template_name="Classic", index=0)
    r = client.post(f"/api/batch/{bid}/stage", data=data, files=files)
    assert r.status_code == 200, r.text
    assert pv["copy"] == r.json()["dest"] == str(folder / "Picnic-Classic-scan-2.jpg")


def test_save_preview_validates_its_inputs(client, tmp_path, fixtures_dir):
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(tmp_path / "p" / "scan.jpg"))
    security.allow_root(str(tmp_path / "p"))
    for body in ({"path": src, "fields": [1, 2]}, {"path": src, "templateName": 5},
                 {"path": src, "onExists": "sometimes"}, {"path": 7}):
        r = client.post("/api/save/preview", json=body)
        assert r.status_code == 400, (body, r.status_code, r.text)
    assert client.post("/api/save/preview", json={"path": src, "onExists": "increment"}).status_code == 200


def test_batch_settings_are_the_ones_it_was_created_with(client, tmp_path, fixtures_dir):
    folder = tmp_path / "photos"
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(folder / "scan.jpg"))
    security.allow_root(str(folder))
    save_settings({"saving": {"location": "same", "fileName": "{stem}-first"}})
    bid = client.post("/api/batch/create", json={"files": [src], "plan": [],
                                                 "batchSettings": {"saveMode": "copy"}}).json()["id"]
    save_settings({"saving": {"fileName": "{stem}-changed-later"}})   # changed while the batch runs
    data, files = _form(src, index=0)
    r = client.post(f"/api/batch/{bid}/stage", data=data, files=files)
    assert r.status_code == 200, r.text
    assert os.path.basename(r.json()["dest"]) == "scan-first.jpg"
    # the batch's confirmed save mode can't be swapped by a later request
    data, files = _form(src, mode="overwrite", index=0)
    assert client.post(f"/api/batch/{bid}/stage", data=data, files=files).status_code == 400


# ------------------------------------------------------------------ upload validation on every entry point


def _png(w, h):
    buf = io.BytesIO()
    Image.new("L", (1, 1)).save(buf, "PNG")
    data = bytearray(buf.getvalue())
    # declare a huge image in the IHDR header (a few bytes; decoding it would need gigabytes)
    data[16:20] = w.to_bytes(4, "big")
    data[20:24] = h.to_bytes(4, "big")
    data[29:33] = zlib.crc32(bytes(data[12:29])).to_bytes(4, "big")
    return bytes(data)


def test_batch_stage_checks_tiles_and_masks_like_a_save(client, tmp_path, fixtures_dir):
    folder = tmp_path / "photos"
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(folder / "scan.jpg"))
    security.allow_root(str(folder))
    bid = client.post("/api/batch/create", json={"files": [src], "plan": []}).json()["id"]
    url = f"/api/batch/{bid}/stage"
    data, files = _form(src, tile_png=_png(20000, 20000), index=0)
    r = client.post(url, data=data, files=files)
    assert r.status_code == 400 and "at most" in r.text
    data, _ = _form(src, index=0)
    assert client.post(url, data=data, files={"other": ("x.png", _png(1, 1), "image/png")}).status_code == 400
    mask = "data:image/png;base64," + base64.b64encode(_png(9000, 9000)).decode()
    data, files = _form(src, index=0, erase={"brushAdd": mask})
    r = client.post(url, data=data, files=files)
    assert r.status_code == 400 and "brush mask" in r.text
    for bad in ({"fields": [1]}, {"erase": "x"}, {"layout": None}, {"template_name": 3}):
        data, files = _form(src, index=0, **bad)
        assert client.post(url, data=data, files=files).status_code == 400, bad
    assert client.post(url, data={"job": "{not json"}).status_code == 400
    assert not os.path.exists(os.path.join(batchmod.get_batch(bid).dir, "00000", "job.json"))
    # the single save and the erase preview refuse the same mask
    data, files = _form(src, index=0, erase={"brushAdd": mask})
    assert client.post("/api/save", data=data, files=files).status_code == 400
    r = client.post("/api/photo/erase-preview", json={"path": src, "erase": {"brushAdd": mask}})
    assert r.status_code == 400
    assert client.post("/api/photo/erase-preview", json={"path": src, "erase": [1]}).status_code == 400


def test_restore_rejects_wrong_index_types(client):
    bid = client.post("/api/batch/create", json={"files": [], "plan": []}).json()["id"]
    assert client.post(f"/api/batch/{bid}/restore", json={"indices": ["a"]}).status_code == 400
    assert client.post("/api/batch/create", json={"files": [], "plan": "x"}).status_code == 400
    assert client.post("/api/batch/create", json={"files": [], "batchSettings": {"saveMode": "x"}}).status_code == 400
