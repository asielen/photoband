"""Regression tests for the safety review: batch destinations and restores, settings validation,
"overwrite" conflicts between different photos, case C protection worked out server-side,
drafts, the batch journal on a full disk, CSV injection, logs, file dates, the launch hand-off,
the in-app browser's scope, record templates and error mapping."""
import errno
import hashlib
import json
import os
import shutil
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from conftest import make_band_layout
from photoband import batch as batchmod
from photoband import dialogs, drafts, security, server
from photoband.imageio import load_upright, probe
from photoband.save import SaveError, SaveRequest, destination_for, save
from photoband.settings import load_settings, save_settings

TOK = security.TOKEN


@pytest.fixture
def client():
    security.reset()
    c = TestClient(server.app, base_url="http://127.0.0.1")
    c.headers["X-Photoband-Token"] = TOK
    yield c
    security.reset()


@pytest.fixture(autouse=True)
def _fresh_settings():
    from photoband.settings import reset_settings
    reset_settings()
    yield
    reset_settings()


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _copy(fixtures_dir, name, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(os.path.join(fixtures_dir, name), dst)
    return dst


def _save(path, mode="copy", saving=None, text="Ann, Bea and Carl", layout=None, **kw):
    s = load_settings()
    s["saving"]["location"] = "subfolder"   # these tests were written for copies in a "captioned" subfolder
    s["saving"].update(saving or {})
    arr, info = load_upright(path)
    tiles = []
    if layout is None:
        layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text=text)
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": text, "custom": False}], "overrides": {}}
    return save(SaveRequest(path=path, mode=mode, layout=layout, tiles=tiles, state=state, settings=s, **kw))


def _form(path, mode="copy", **extra):
    arr, _ = load_upright(path)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    job = {"path": path, "mode": mode, "layout": layout,
           "tiles": [{"name": "t0", "x": tiles[0].x, "y": tiles[0].y}],
           "state": {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"}, "blocks": [],
                     "overrides": {}}}
    job.update(extra)
    return {"job": json.dumps(job)}, {"t0": ("t0.png", tiles[0].png, "image/png")}


# ------------------------------------------------------------------ 1. batch destinations

def test_batch_stage_refuses_client_destinations(client, tmp_path, fixtures_dir):
    allowed = tmp_path / "allowed"
    victim_dir = tmp_path / "NOT_ALLOWED"
    victim_dir.mkdir()
    victim = victim_dir / "important.jpg"
    victim.write_bytes(b"precious")
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(allowed / "photo.jpg"))
    security.allow_root(str(allowed))
    bid = client.post("/api/batch/create", json={"files": [src], "plan": []}).json()["id"]
    data, files = _form(src, "copyAs", dest_path=str(victim), on_exists="overwrite", index=0)
    r = client.post(f"/api/batch/{bid}/stage", data=data, files=files)
    assert r.status_code == 400
    # a copy job's client-chosen dest_path / on_exists are ignored: the server derives them
    data, files = _form(src, "copy", dest_path=str(victim), on_exists="overwrite", index=0)
    r = client.post(f"/api/batch/{bid}/stage", data=data, files=files)
    assert r.status_code == 200, r.text
    dest = r.json()["dest"]
    assert os.path.dirname(dest) == str(allowed)   # copies go next to the original by default
    job = json.load(open(os.path.join(batchmod.get_batch(bid).dir, "00000", "job.json")))
    assert job["dest_path"] == dest and job.get("on_exists") is None
    assert not security.is_allowed(str(victim))
    # a photo that is not part of the batch can't be staged into it
    other = _copy(fixtures_dir, "13_date_stamp.jpg", str(allowed / "other.jpg"))
    data, files = _form(other, "overwrite", index=0)
    assert client.post(f"/api/batch/{bid}/stage", data=data, files=files).status_code == 403
    assert victim.read_bytes() == b"precious"


def test_batch_create_checks_plan_paths(client, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.jpg").write_bytes(b"x")
    (tmp_path / "b" / "secret.jpg").write_bytes(b"x")
    security.allow_root(str(tmp_path / "a"))
    r = client.post("/api/batch/create", json={"files": [], "plan": [{"path": str(tmp_path / "b" / "secret.jpg"),
                                                                       "status": "skipped"}]})
    assert r.status_code == 403


def test_batch_resume_grants_only_the_batch_files(client, tmp_path):
    d = tmp_path / "scans"
    d.mkdir()
    (d / "one.jpg").write_bytes(b"x")
    (d / "sibling.jpg").write_bytes(b"x")
    b = batchmod.new_batch({"count": 1, "files": [str(d / "one.jpg")]})
    security.reset()
    assert client.post(f"/api/batch/{b.id}/resume").status_code == 200
    assert security.is_allowed(str(d / "one.jpg"))
    assert not security.is_allowed(str(d / "sibling.jpg"))
    assert not security.is_allowed(str(d))


def test_unknown_batch_is_404(client):
    assert client.get("/api/batch/20990101-000000-abcdef").status_code == 404
    assert client.get("/api/batch/..%2f..%2fx").status_code in (404, 400)


# ------------------------------------------------------------------ 2. exclude / restore targets

def test_exclude_cannot_retarget_a_saved_entry_and_restore_stays_in_the_plan(client, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    victim_dir = tmp_path / "NOT_ALLOWED"
    victim_dir.mkdir()
    victim = victim_dir / "notes.txt"
    victim.write_bytes(b"never given to Photoband")
    src = allowed / "photo.jpg"
    src.write_bytes(b"captioned output")
    backup = allowed / "_originals" / "photo.jpg"
    backup.parent.mkdir()
    backup.write_bytes(b"the original")
    b = batchmod.new_batch({"count": 1, "files": [str(src)]})
    b.mark(0, "done", path=str(src), out=str(src), backup=str(backup), outIdentity=None)
    r = client.post(f"/api/batch/{b.id}/exclude", json={"index": 0, "state": "excluded", "path": str(victim)})
    assert r.status_code == 200 and r.json()["skipped"] == [0]
    e = b.data["entries"]["0"]
    assert e["path"] == str(src) and e["state"] == "done"
    # mark() never changes the path of an existing entry either
    b.mark(0, "done", path=str(victim))
    assert b.data["entries"]["0"]["path"] == str(src)
    # a journal edited behind our back still can't make restore write outside the plan
    b.data["entries"]["0"]["path"] = str(victim)
    res = b.restore_originals(force=True)
    assert res["restored"] == 0 and res["results"][0]["result"] == "error"
    assert victim.read_bytes() == b"never given to Photoband"


# ------------------------------------------------------------------ 3/4. settings

def test_subfolder_name_must_be_a_plain_folder_name(client, tmp_path, fixtures_dir):
    for bad in ("../../OUTSIDE/deep", "/etc/x", "..", "a/b", "a\\b", " captioned", ""):
        if bad == "":
            continue
        r = client.post("/api/settings", json={"saving": {"subfolderName": bad}})
        assert r.status_code == 400, bad
    assert load_settings()["saving"]["subfolderName"] == "captioned"
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(tmp_path / "a" / "p.jpg"))
    info = probe(src)
    for bad in ("../../OUTSIDE", str(tmp_path / "elsewhere")):
        with pytest.raises(SaveError):
            destination_for(src, info, {"location": "subfolder", "subfolderName": bad}, None, "")
    # and nothing was written outside the photo's folder by a save
    r = _save(src, saving={"subfolderName": "../../OUTSIDE"})
    assert not r.ok and r.code == "dest"
    assert not (tmp_path / "OUTSIDE").exists()


def test_settings_types_are_checked(client):
    for patch in ({"session": {"window": 5}}, {"saving": {"jpegQuality": "high"}},
                  {"advanced": {"cacheSizeMB": "big"}}, {"saving": {"subfolderName": 7}},
                  {"saving": {"onExists": "clobber"}}, {"batch": {"saveMode": "erase"}},
                  {"saving": {"keepFileDates": "yes"}}, {"saving": {"jpegQuality": 1000}}):
        r = client.post("/api/settings", json=patch)
        assert r.status_code == 400, patch
    s = load_settings()
    assert s["session"]["window"] == {"w": 1440, "h": 900}
    ok = client.post("/api/settings", json={"saving": {"jpegQuality": 90}, "session": {"window": {"w": 1000}}})
    assert ok.status_code == 200 and ok.json()["saving"]["jpegQuality"] == 90
    assert ok.json()["session"]["window"] == {"w": 1000, "h": 900}


def test_bad_settings_file_is_ignored_value_by_value():
    from photoband import paths
    from photoband.desktop import _window_size
    p = os.path.join(paths.app_data(), "settings.json")
    with open(p, "w") as fh:
        json.dump({"session": {"window": 5}, "saving": {"jpegQuality": "high", "subfolderName": "../x",
                                                         "embedMarker": False}}, fh)
    s = load_settings()
    assert s["session"]["window"] == {"w": 1440, "h": 900}
    assert s["saving"]["jpegQuality"] == 95 and s["saving"]["subfolderName"] == "captioned"
    assert s["saving"]["embedMarker"] is False   # valid values are kept
    assert _window_size(s) == (1440, 900)
    assert _window_size({"session": {"window": 5}}) == (1440, 900)
    assert _window_size({"session": {"window": {"w": "x", "h": 700}}}) == (1440, 700)
    with open(p, "w") as fh:
        fh.write("[]")
    assert load_settings()["saving"]["location"] == "same"
    # internal callers are validated too
    save_settings({"session": {"window": 5}})
    assert load_settings()["session"]["window"] == {"w": 1440, "h": 900}


# ------------------------------------------------------------------ 5. "overwrite" between photos

def test_overwrite_policy_never_replaces_another_photos_copy(tmp_path, fixtures_dir):
    pa = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "A" / "IMG_0001.tif"))
    pb = _copy(fixtures_dir, "06_group_three_rows.tif", str(tmp_path / "B" / "IMG_0001.tif"))
    sv = {"location": "fixed", "fixedFolder": str(tmp_path / "out"), "onExists": "overwrite"}
    ra = _save(pa, saving=sv, text="Grandma Rose, 1952")
    assert ra.ok, ra.error
    sha_a = _sha(ra.out_path)
    rb = _save(pb, saving=sv, text="Uncle Joe")
    assert rb.ok, rb.error
    assert rb.out_path != ra.out_path
    assert _sha(ra.out_path) == sha_a
    # the same photo saved again does replace its own earlier copy
    ra2 = _save(pa, saving=sv, text="Grandma Rose, 1953")
    assert ra2.ok and ra2.out_path == ra.out_path
    assert not any("kept" in n for n in ra2.notes)


def test_overwrite_policy_never_replaces_a_photo_captioned_in_place(tmp_path, fixtures_dir):
    scan = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "IMG_1.tif"))
    other = _copy(fixtures_dir, "09_partial_date.jpg", str(tmp_path / "IMG_1.jpg"))
    r0 = _save(other, mode="overwrite", text="Aunt May")
    assert r0.ok, r0.error
    sha = _sha(other)
    r = _save(scan, saving={"location": "same", "fileName": "{stem}", "outputFormat": "jpeg",
                            "onExists": "overwrite"})
    assert r.ok, r.error
    assert r.out_path != other and _sha(other) == sha


def test_confirmed_replace_of_another_photos_copy_keeps_it_aside(tmp_path, fixtures_dir):
    pa = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "A" / "IMG.tif"))
    pb = _copy(fixtures_dir, "06_group_three_rows.tif", str(tmp_path / "B" / "IMG.tif"))
    ra = _save(pa, saving={"location": "fixed", "fixedFolder": str(tmp_path / "out")})
    sha_a = _sha(ra.out_path)
    rb = _save(pb, mode="copyAs", dest_path=ra.out_path, on_exists="overwrite")
    assert rb.ok, rb.error
    kept = [n for n in rb.notes if "was kept at" in n]
    assert kept
    assert _sha(kept[0].split("was kept at ", 1)[1]) == sha_a


# ------------------------------------------------------------------ 6. case C worked out server-side

def test_case_c_overwrite_refused_without_client_case(tmp_path, fixtures_dir):
    p = _copy(fixtures_dir, "12_scanned_polaroid_handwriting.tif", str(tmp_path / "scan.tif"))
    before = _sha(p)
    r = _save(p, mode="overwrite")          # band mode: the editor sends case=None
    assert not r.ok and r.code == "case_c_overwrite"
    assert _sha(p) == before


def test_batch_erase_on_case_c_is_copies_only(tmp_path, fixtures_dir):
    p = _copy(fixtures_dir, "12_scanned_polaroid_handwriting.tif", str(tmp_path / "scan.tif"))
    before = _sha(p)
    arr, _ = load_upright(p)
    r = _save(p, mode="overwrite", saving={"allowOverwriteHandwritten": True},
              layout={"mode": "erase", "canvas": [arr.shape[1], arr.shape[0]]}, batch_job="b:0")
    assert not r.ok and r.code == "case_c_overwrite"
    assert _sha(p) == before


def test_batch_stage_turns_case_c_erase_overwrite_into_a_copy(client, tmp_path, fixtures_dir):
    p = _copy(fixtures_dir, "12_scanned_polaroid_handwriting.tif", str(tmp_path / "scan.tif"))
    security.allow_root(str(tmp_path))
    save_settings({"saving": {"allowOverwriteHandwritten": True}})
    bid = client.post("/api/batch/create", json={"files": [p], "plan": []}).json()["id"]
    data, files = _form(p, "overwrite", index=0, case="C")
    job = json.loads(data["job"])
    job["layout"]["mode"] = "erase"
    r = client.post(f"/api/batch/{bid}/stage", data={"job": json.dumps(job)}, files=files)
    assert r.status_code == 200, r.text
    assert r.json()["dest"] != p
    staged = json.load(open(os.path.join(batchmod.get_batch(bid).dir, "00000", "job.json")))
    assert staged["mode"] == "copyAs" and staged["dest_path"] != p


# ------------------------------------------------------------------ 7. batch restore of a case A file

def test_batch_restore_puts_back_the_file_as_it_was_before_the_batch(tmp_path, fixtures_dir):
    p = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "photo.tif"))
    x0 = _sha(p)
    r1 = _save(p, mode="overwrite", text="First caption")
    assert r1.ok, r1.error
    x1 = _sha(p)
    assert x1 != x0
    b = batchmod.new_batch({"count": 1, "files": [p]})
    arr, _ = load_upright(p)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0], text="Batch caption")
    st = probe(p)
    b.stage(0, {"path": p, "mode": "overwrite", "layout": layout, "state": {"blocks": []},
                "expected_stat": [st.size_bytes, str(st.mtime_ns)], "settings": load_settings()},
            [{"x": t.x, "y": t.y, "png": t.png} for t in tiles])
    r = batchmod.run_job(b.id, 0)
    assert r["ok"], r.get("error")
    assert _sha(r["backup_path"]) == x0            # the long-term original is still X0
    assert r["restore_path"] and _sha(r["restore_path"]) == x1
    b._record_result(0, r)
    assert b.data["entries"]["0"]["backup"] == r["restore_path"]
    res = b.restore_originals()
    assert res["restored"] == 1, res
    assert _sha(p) == x1


# ------------------------------------------------------------------ 8. drafts

def test_newer_draft_survives_an_overwrite(client, tmp_path, fixtures_dir):
    p = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "p.tif"))
    security.allow_root(str(tmp_path))
    client.post("/api/drafts", json={"path": p, "state": {"blocks": {"people": {"text": "typed"}}}, "hash": "h2"})
    data, files = _form(p, "overwrite", draft_hash="h1")
    r = client.post("/api/save", data=data, files=files)
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert (drafts.load_draft_any(p) or {}).get("_hash") == "h2"
    # the draft the save was made from is removed, even though the file's identity changed
    data, files = _form(p, "overwrite", draft_hash="h2")
    assert client.post("/api/save", data=data, files=files).json()["ok"]
    assert drafts.load_draft_any(p) is None


def test_drafts_are_keyed_canonically_and_old_keys_migrate(tmp_path, symlink):
    import hashlib as _h
    import unicodedata
    from photoband import paths
    from photoband.util import atomic_write_json
    d = tmp_path / "d"
    d.mkdir()
    name = unicodedata.normalize("NFD", "José.tif")
    f = d / name
    f.write_bytes(b"x" * 100)
    link = tmp_path / "link"
    symlink(d, link, target_is_directory=True)
    drafts.save_draft(str(link / name), {"v": 1})
    assert drafts.load_draft(str(f)) == {"v": 1}
    drafts.delete_draft(str(f))
    assert drafts.load_draft_any(str(link / name)) is None
    # a draft stored under the old abspath key is still found, and moved to the new key
    old = os.path.join(paths.sub("drafts"), _h.sha1(os.path.abspath(str(link / name)).encode()).hexdigest() + ".json")
    atomic_write_json(old, {"path": str(link / name), "stat": None, "ident": None, "state": {"v": 2},
                            "updated": time.time()})
    assert drafts.load_draft_any(str(link / name)) == {"v": 2}
    assert not os.path.exists(old)
    assert drafts.load_draft_any(str(f)) == {"v": 2}
    assert drafts.delete_draft_if(str(f), "nope") is False
    assert drafts.delete_draft_if(str(f), "nope", unhashed=True) is True


# ------------------------------------------------------------------ 9. journal on a full disk

def test_journal_write_failure_stops_the_batch_and_releases_the_lock(tmp_path, fixtures_dir, monkeypatch):
    p = _copy(fixtures_dir, "13_date_stamp.jpg", str(tmp_path / "p.jpg"))
    b = batchmod.new_batch({"count": 1, "files": [p]})
    arr, _ = load_upright(p)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    b.stage(0, {"path": p, "mode": "copy", "layout": layout, "state": {"blocks": []}, "settings": load_settings()},
            [{"x": t.x, "y": t.y, "png": t.png} for t in tiles])
    b.staging_complete()
    real = batchmod.atomic_write_json
    full = {"on": True, "n": 0}

    def write(path, obj):
        if full["on"] and path.endswith("journal.json"):
            full["n"] += 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(path, obj)
    monkeypatch.setattr(batchmod, "atomic_write_json", write)
    # start() itself can't journal: it fails and does not keep the run lock
    with pytest.raises(OSError):
        b.start(workers=1)
    assert b._run_lock is None
    # the disk fills up right after the run started
    full["on"] = False
    orig_mark = b.mark

    def mark(idx, state, **kw):
        full["on"] = True
        return orig_mark(idx, state, **kw)
    monkeypatch.setattr(b, "mark", mark)
    b.start(workers=1)
    t0 = time.time()
    while b._thread.is_alive() and time.time() - t0 < 60:
        time.sleep(0.1)
    assert not b._thread.is_alive()
    sm = b.summary()
    assert sm["state"] == "cancelled" and "No space left" in sm["error"]
    assert all(e["state"] != "running" for e in sm["entries"])
    assert b._run_lock is None


# ------------------------------------------------------------------ 10. CSV injection

def test_report_csv_neutralises_formulas(tmp_path):
    name = '=HYPERLINK("https://evil.example","Click").jpg'
    p = str(tmp_path / name)
    b = batchmod.new_batch({"count": 0, "files": [], "folder": str(tmp_path),
                            "plan": [{"path": p, "status": "skipped", "reasons": ["+SUM(1,1)"]}]},
                           unsaved=[{"path": p, "status": "skipped", "reasons": ["+SUM(1,1)", "x"]}])
    import csv
    import io
    rows = list(csv.reader(io.StringIO(b.report_csv())))
    for row in rows[1:]:
        for cell in row:
            assert not cell.startswith(("=", "+", "-", "@", "\t", "\r")), cell
    assert rows[1][1].startswith("'=HYPERLINK")
    assert batchmod.csv_safe("-1") == "'-1" and batchmod.csv_safe("ok") == "ok"


# ------------------------------------------------------------------ 11/12. logs and file dates

def test_error_log_rotates(monkeypatch):
    from photoband import paths, save as savemod
    monkeypatch.setattr(savemod, "LOG_ROTATE_BYTES", 100)
    p = os.path.join(paths.sub("logs"), "error.log")
    with open(p, "w") as fh:
        fh.write("x" * 200)
    savemod._append_rotating("error.log", "new entry\n")
    assert open(p).read() == "new entry\n"
    assert os.path.getsize(p + ".1") == 200


def test_unexpected_save_error_goes_to_the_rotating_error_log(tmp_path, fixtures_dir, monkeypatch):
    from photoband import paths, save as savemod
    p = _copy(fixtures_dir, "13_date_stamp.jpg", str(tmp_path / "p.jpg"))
    monkeypatch.setattr(savemod, "composite", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _save(p)
    assert not r.ok and "boom" in r.error
    assert "RuntimeError: boom" in open(os.path.join(paths.sub("logs"), "error.log")).read()


def test_creation_date_note_when_not_kept(tmp_path, fixtures_dir, monkeypatch):
    from photoband import save as savemod
    p = _copy(fixtures_dir, "13_date_stamp.jpg", str(tmp_path / "p.jpg"))
    monkeypatch.setattr(savemod, "keep_creation_time", lambda path, st: False)
    r = _save(p, saving={"keepFileDates": True})
    assert r.ok and any("creation date could not be kept" in n for n in r.notes)
    assert os.stat(r.out_path).st_mtime_ns == os.stat(p).st_mtime_ns
    monkeypatch.setattr(savemod, "keep_creation_time", lambda path, st: True)
    r = _save(p, saving={"keepFileDates": True})
    assert r.ok and not any("creation date" in n for n in r.notes)


def test_keep_creation_time_reports_honestly(tmp_path):
    from photoband import save as savemod
    f = tmp_path / "f"
    f.write_bytes(b"x")
    st = os.stat(f)
    got = savemod.keep_creation_time(str(f), st)
    # Linux can't set a birth time: never claims success there
    if not (os.name == "nt" or os.uname().sysname == "Darwin"):
        assert got is False


# ------------------------------------------------------------------ 13. launch hand-off

def test_launch_url_carries_a_one_time_nonce_not_the_token():
    anon = TestClient(server.app, base_url="http://127.0.0.1")
    url = security.launch_url(1234)
    assert TOK not in url and "?b=" in url
    nonce = url.split("?b=", 1)[1]
    r = anon.get("/", params={"b": nonce}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/#t=") and TOK in r.headers["location"]
    assert anon.get("/", params={"b": nonce}, follow_redirects=False).status_code == 410
    assert anon.get("/", params={"b": "made-up"}, follow_redirects=False).status_code == 410


# ------------------------------------------------------------------ 14. in-app browser scope

def test_fs_list_is_limited_to_home_drives_and_temp(client, tmp_path, monkeypatch):
    if os.name == "nt":
        # "/" is the current drive's root there, and drives are browsable by design; the
        # system folders on them are not
        assert client.get("/api/fs/list", params={"dir": os.environ.get("SystemRoot", "C:\\Windows")}
                          ).status_code == 403
    else:
        assert client.get("/api/fs/list", params={"dir": "/etc"}).status_code == 403
        assert client.get("/api/fs/list", params={"dir": "/"}).status_code == 403
    assert client.get("/api/fs/list", params={"dir": str(tmp_path)}).status_code == 200
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))   # what expanduser("~") reads on Windows
    j = client.get("/api/fs/list").json()
    assert j["dir"] == os.path.realpath(str(home))
    assert "/" not in j["roots"]
    assert client.get("/api/fs/list", params={"dir": str(home / ".ssh")}).status_code == 403
    # the desktop window has system dialogs: the in-app browser is off there
    monkeypatch.setitem(server.APP_MODE, "mode", "desktop")
    monkeypatch.setattr(dialogs, "_provider", lambda *a, **k: None)
    assert client.get("/api/fs/list", params={"dir": str(tmp_path)}).status_code == 403


# ------------------------------------------------------------------ 15/16. record templates, errors

def test_record_template_is_validated_before_the_ui_sees_it(client, tmp_path, monkeypatch):
    from photoband import photos
    f = tmp_path / "p.jpg"
    f.write_bytes(b"x")
    security.allow([str(f)])
    crafted = {"case": "A", "warnings": [], "state": {"template": {"name": 5, "blocks": "nope"},
                                                       "overrides": [], "blocks": "x"}}
    monkeypatch.setattr(photos, "existing", lambda p, run_ocr=True, prio=0: crafted)
    j = client.post("/api/photo/existing", json={"path": str(f)}).json()
    assert j["state"]["template"] is None and j["state"]["overrides"] is None and j["state"]["blocks"] is None
    assert any("not valid" in w for w in j["warnings"])
    assert crafted["state"]["template"] == {"name": 5, "blocks": "nope"}   # the cache is not changed


def test_template_export_unknown_is_404(client):
    assert client.get("/api/templates/no-such-template/export").status_code == 404


def test_user_errors_are_400_and_unexpected_value_errors_are_logged(client, caplog):
    r = client.post("/api/fs/allow", json={"picks": "x"})
    assert r.status_code == 400
    assert server._expected_value_error(security.UserError("x")) is False  # raised here, not in a module
    try:
        security.plain_file_name("a/b")
    except ValueError as e:
        assert isinstance(e, security.UserError)
        assert server._expected_value_error(e)


# ------------------------------------------------------------------ upload sizes

def _png(w, h):
    import io as _io
    from PIL import Image
    b = _io.BytesIO()
    Image.new("RGBA", (w, h)).save(b, "PNG")
    return b.getvalue()


def test_oversized_text_tile_is_refused_before_decoding(client, tmp_path, fixtures_dir):
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(tmp_path / "a" / "photo.jpg"))
    security.allow_root(str(tmp_path / "a"))
    data, _ = _form(src, "copy")
    r = client.post("/api/save", data=data, files={"t0": ("t0.png", _png(4097, 1), "image/png")})
    assert r.status_code == 400 and "4096" in r.text
    assert sorted(os.listdir(tmp_path / "a")) == ["photo.jpg"]   # nothing written
    r = client.post("/api/save", data=data, files={"t0": ("t0.png", b"not a png", "image/png")})
    assert r.status_code == 400


def test_oversized_brush_mask_is_refused():
    import base64
    from photoband.existing import MASK_MAX, decode_mask_png
    ok = decode_mask_png(base64.b64encode(_png(64, 32)).decode())
    assert ok is not None and ok.shape == (32, 64)
    with pytest.raises(ValueError):
        decode_mask_png(base64.b64encode(_png(MASK_MAX + 1, 1)).decode())


# ------------------------------------------------------------------ temp files of running saves

def test_sweep_keeps_temp_files_of_a_running_save(tmp_path):
    from photoband.util import sweep_temp, temp_prefix
    old = time.time() - 3600
    live = tmp_path / f"{temp_prefix('.pbtmp-')}abc.tif"          # this process: still saving
    dead = tmp_path / ".pbtmp-p999999-abc.tif"                    # no such process
    legacy = tmp_path / ".pbtmp-xyz.tif"                          # made before owners were named
    for f in (live, dead, legacy):
        f.write_bytes(b"x")
        os.utime(f, (old, old))
    removed = sweep_temp([str(tmp_path)])
    assert live.exists()
    assert not dead.exists() and not legacy.exists()
    assert sorted(removed) == sorted([str(dead), str(legacy)])
