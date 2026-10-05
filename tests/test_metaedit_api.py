"""Edited details through the API: batch copy names, drafts kept for (and removed from) the original."""
import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_band_layout
from photoband import batch as batchmod
from photoband import drafts, security, server
from photoband.imageio import load_upright
from photoband.settings import save_settings

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


@pytest.fixture
def client():
    security.reset()
    c = TestClient(server.app, base_url="http://127.0.0.1")
    c.headers["X-Photoband-Token"] = security.TOKEN
    yield c
    security.reset()


@pytest.fixture(autouse=True)
def _fresh_settings():
    from photoband.settings import reset_settings
    reset_settings()
    yield
    reset_settings()


def _photo(fixtures_dir, tmp_path, name="photo.jpg"):
    d = tmp_path / "photos"
    d.mkdir(exist_ok=True)
    dst = str(d / name)
    shutil.copy2(os.path.join(fixtures_dir, "13_date_stamp.jpg"), dst)
    security.allow_root(str(d))
    return dst


def _job(path, mode, **extra):
    arr, _ = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    job = {"path": path, "mode": mode, "layout": layout, "tiles": [{"name": "t0", "x": tiles[0].x, "y": tiles[0].y}],
           "state": {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"}, "blocks": [], "overrides": {}},
           "fields": {"title": None, "stem": os.path.splitext(os.path.basename(path))[0]}}
    job.update(extra)
    return {"job": json.dumps(job)}, {"t0": ("t0.png", tiles[0].png, "image/png")}


def _run(client, bid):
    assert client.post(f"/api/batch/{bid}/run", json={}).status_code == 200
    b = batchmod.get_batch(bid)
    t0 = time.time()
    while time.time() - t0 < 120:
        if b.data.get("state") in ("finished", "cancelled") and not (b._thread and b._thread.is_alive()):
            return b
        time.sleep(0.2)
    raise TimeoutError


def test_a_batch_copy_is_named_from_the_edited_details_and_keeps_them_for_the_original(client, fixtures_dir, tmp_path):
    src = _photo(fixtures_dir, tmp_path)
    save_settings({"saving": {"fileName": "{title}", "location": "same"}})
    drafts.save_draft(src, {"templateId": "t", "blocks": {}, "meta": {"title": "Picnic"}, "_hash": "h1"})
    bid = client.post("/api/batch/create", json={"files": [src], "plan": [], "settings": {"saveMode": "copy"}}).json()["id"]
    data, files = _job(src, "copy", index=0, meta_edits={"title": "Picnic"}, draft_hash="h1")
    r = client.post(f"/api/batch/{bid}/stage", data=data, files=files)
    assert r.status_code == 200, r.text
    assert os.path.basename(r.json()["dest"]).startswith("Picnic")      # the edited title names the copy
    client.post(f"/api/batch/{bid}/staging-complete")
    b = _run(client, bid)
    assert [e["state"] for e in b.summary()["entries"]] == ["done"], b.summary()
    # the original does not have the title yet: it stays as the original's draft
    assert drafts.load_draft_any(src)["meta"] == {"title": "Picnic"}


def test_overwriting_with_the_kept_details_removes_their_draft(client, fixtures_dir, tmp_path):
    src = _photo(fixtures_dir, tmp_path)
    drafts.save_draft(src, {"templateId": "t", "blocks": {}, "overrides": {}, "meta": {"title": "Picnic"}, "_hash": "other"})
    data, files = _job(src, "overwrite", meta_edits={"title": "Picnic"}, draft_hash="not-this-one")
    r = client.post("/api/save", data=data, files=files)
    assert r.json()["ok"], r.json()
    assert drafts.load_draft_any(src) is None
    # a draft with other edits too is kept
    drafts.save_draft(src, {"templateId": "t", "blocks": {}, "meta": {"title": "Picnic", "city": "Rome"}, "_hash": "x"})
    st = os.stat(src)
    r = client.post("/api/photo/details", json={"path": src, "edits": {"title": "Picnic"},
                                                "expected_stat": [st.st_size, str(st.st_mtime_ns)]})
    assert r.json()["ok"], r.json()
    assert drafts.load_draft_any(src)["meta"]["city"] == "Rome"


def test_details_need_the_version_they_were_made_for(client, fixtures_dir, tmp_path):
    src = _photo(fixtures_dir, tmp_path)
    r = client.post("/api/photo/details", json={"path": src, "edits": {"title": "x"}})
    assert r.status_code == 400
