"""Regression tests for the post-merge verification: the "overwrite" policy after a folder move
and for records without sourceKey, flushing the editor when the desktop window closes, % in a
source name (stand-in links and their sweep), the in-app browser's roots, and the case C
overwrite message."""
import hashlib
import os
import shutil
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import make_band_layout
from photoband import record, security, server
from photoband import metawrite as MW
from photoband.exiftool import get as get_exiftool
from photoband.imageio import load_upright
from photoband.save import (CASE_C_OVERWRITE_MSG, SaveRequest, is_output_of, save, source_ids_for_file)
from photoband.settings import load_settings, save_settings
from photoband.util import sweep_app_tmp, sweep_temp

TOK = security.TOKEN
needs_et = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


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


def _save(path, mode="copy", saving=None, text="Ann, Bea and Carl", source_rect=None, **kw):
    s = load_settings()
    s["saving"]["location"] = "subfolder"   # these tests were written for copies in a "captioned" subfolder
    s["saving"].update(saving or {})
    arr, info = load_upright(path)
    pw, ph = (source_rect[2], source_rect[3]) if source_rect else (arr.shape[1], arr.shape[0])
    layout, tiles = make_band_layout(pw, ph, text=text, source_rect=source_rect)
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": text, "custom": False}], "overrides": {}}
    return save(SaveRequest(path=path, mode=mode, layout=layout, tiles=tiles, state=state, settings=s, **kw))


def _strip_source_key(path):
    """Make ``path`` look like a copy written before sourceKey existed."""
    rec = record.from_metadata(get_exiftool().read_json(path))
    rec.pop("sourceKey", None)
    rec.pop("saveMode", None)
    get_exiftool().write(path, [f"-XMP-photoband:Record={record.encode(rec)}"])
    assert "sourceKey" not in record.from_metadata(get_exiftool().read_json(path))


OVERWRITE = {"onExists": "overwrite"}


# ------------------------------------------------------------------ 1. "overwrite" after a path change

@needs_et
def test_overwrite_policy_survives_a_folder_move(tmp_path, fixtures_dir):
    src = _copy(fixtures_dir, "13_date_stamp.jpg", str(tmp_path / "album" / "p.jpg"))
    r1 = _save(src, saving=OVERWRITE)
    assert r1.ok, r1.error
    moved = str(tmp_path / "album-renamed")
    os.rename(str(tmp_path / "album"), moved)
    src2 = os.path.join(moved, "p.jpg")
    r2 = _save(src2, saving=OVERWRITE, text="Second caption")
    assert r2.ok, r2.error
    assert os.path.basename(r2.out_path) == os.path.basename(r1.out_path)
    assert not any("was not made by Photoband" in n for n in r2.notes)
    assert not os.path.exists(os.path.join(moved, "captioned", "p-captioned-2.jpg"))


@needs_et
def test_overwrite_policy_recognises_legacy_records(tmp_path, fixtures_dir):
    src = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "IMG.tif"))
    r1 = _save(src, saving=OVERWRITE)
    assert r1.ok, r1.error
    _strip_source_key(r1.out_path)
    r2 = _save(src, saving=OVERWRITE, text="Again")
    assert r2.ok, r2.error
    assert r2.out_path == r1.out_path
    assert not any("kept" in n for n in r2.notes)


@needs_et
def test_confirmed_replace_of_own_moved_copy_is_not_kept_aside(tmp_path, fixtures_dir):
    """The editor's Ask -> Replace: an earlier copy of this photo is replaced, not moved aside."""
    src = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "a" / "IMG.tif"))
    r1 = _save(src)
    assert r1.ok, r1.error
    os.rename(str(tmp_path / "a"), str(tmp_path / "b"))
    src2 = str(tmp_path / "b" / "IMG.tif")
    out = os.path.join(str(tmp_path / "b"), "captioned", os.path.basename(r1.out_path))
    r2 = _save(src2, saving={"onExists": "ask"}, on_exists="overwrite")
    assert r2.ok, r2.error
    assert r2.out_path == os.path.realpath(out)
    assert not any("kept at" in n for n in r2.notes)
    assert not [n for n in os.listdir(os.path.dirname(out)) if "replaced" in n]


@needs_et
def test_pixel_identity_never_matches_a_different_photo(tmp_path, fixtures_dir):
    pa = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "A" / "IMG_0001.tif"))
    pb = _copy(fixtures_dir, "06_group_three_rows.tif", str(tmp_path / "B" / "IMG_0001.tif"))
    sv = {"location": "fixed", "fixedFolder": str(tmp_path / "out"), **OVERWRITE}
    ra = _save(pa, saving=sv)
    assert ra.ok, ra.error
    _strip_source_key(ra.out_path)       # only the pixel identity is left to decide
    sha_a = _sha(ra.out_path)
    rb = _save(pb, saving=sv, text="Uncle Joe")
    assert rb.ok, rb.error
    assert rb.out_path != ra.out_path and _sha(ra.out_path) == sha_a
    assert not is_output_of(ra.out_path, pb, source_ids_for_file(pb))
    assert is_output_of(ra.out_path, pa, source_ids_for_file(pa))


@needs_et
def test_pixel_identity_never_replaces_a_photo_captioned_in_place(tmp_path, fixtures_dir):
    """Same pixels as a photo that was captioned in place: that file is a photo, not a copy."""
    p = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "P.tif"))
    twin = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "other" / "Q.tif"))
    r0 = _save(p, mode="overwrite", saving={"backupOriginals": False})
    assert r0.ok, r0.error
    sha = _sha(p)
    assert not is_output_of(p, twin, source_ids_for_file(twin))
    r = _save(twin, saving={"location": "fixed", "fixedFolder": str(tmp_path), "fileName": "P", **OVERWRITE})
    assert r.ok, r.error
    assert r.out_path != os.path.realpath(p) and _sha(p) == sha


@needs_et
def test_case_a_source_is_identified_by_its_inner_photo(tmp_path, fixtures_dir):
    """A copy made from one of the app's own outputs (case A) carries the inner photo's hash."""
    p = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "a" / "P.tif"))
    r0 = _save(p, mode="overwrite")
    assert r0.ok, r0.error
    rec = record.from_metadata(get_exiftool().read_json(p))
    (x, y), (w, h) = rec["photoOffset"], rec["originalSize"]
    r1 = _save(p, saving=OVERWRITE, source_rect=[x, y, w, h])
    assert r1.ok, r1.error
    os.rename(str(tmp_path / "a"), str(tmp_path / "b"))
    p2 = str(tmp_path / "b" / "P.tif")
    out = os.path.join(str(tmp_path / "b"), "captioned", os.path.basename(r1.out_path))
    assert is_output_of(out, p2, source_ids_for_file(p2))
    r2 = _save(p2, saving=OVERWRITE, source_rect=[x, y, w, h], text="New")
    assert r2.ok, r2.error
    assert r2.out_path == os.path.realpath(out)


@needs_et
def test_pixel_hash_is_skipped_when_the_source_key_matches(tmp_path, fixtures_dir, monkeypatch):
    from photoband import save as savemod
    src = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "IMG.tif"))
    assert _save(src, saving=OVERWRITE).ok
    calls = []
    real = savemod.source_photo_hashes
    monkeypatch.setattr(savemod, "source_photo_hashes", lambda *a, **k: calls.append(1) or real(*a, **k))
    r = _save(src, saving=OVERWRITE)
    assert r.ok, r.error
    assert calls == []
    # no candidate file at all: no hash either
    r = _save(src, saving={"location": "fixed", "fixedFolder": str(tmp_path / "new"), **OVERWRITE})
    assert r.ok and calls == []


@needs_et
def test_saved_records_say_how_they_were_saved(tmp_path, fixtures_dir):
    src = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(tmp_path / "IMG.tif"))
    r = _save(src)
    assert record.from_metadata(get_exiftool().read_json(r.out_path))["saveMode"] == "copy"
    r = _save(src, mode="overwrite")
    assert record.from_metadata(get_exiftool().read_json(src))["saveMode"] == "overwrite"


# ------------------------------------------------------------------ 2. desktop: flush on close

class _FakeWindow:
    def __init__(self, resolve=True, delay=0.0):
        self.resolve, self.delay = resolve, delay
        self.scripts, self.destroyed = [], threading.Event()

    def evaluate_js(self, script, callback=None):
        self.scripts.append(script)
        if self.resolve and callback:
            def later():
                time.sleep(self.delay)
                callback(True)
            threading.Thread(target=later, daemon=True).start()
        return "true"

    def destroy(self):
        self.destroyed.set()


def test_close_flushes_the_editor_before_destroying_the_window():
    from photoband.desktop import CloseGuard
    w = _FakeWindow(delay=0.2)
    order = []
    guard = CloseGuard(w, on_close=lambda: order.append("size"))
    assert guard() is False                      # the first close is held back
    assert guard() is False                      # a second click while flushing too
    assert w.destroyed.wait(5)
    assert any("__photobandFlush" in s for s in w.scripts)
    assert order == ["size"]
    assert guard() is True                       # destroy's own closing event goes through


def test_flush_gives_up_after_the_timeout():
    from photoband.desktop import flush_editor
    w = _FakeWindow(resolve=False)
    t0 = time.monotonic()
    assert flush_editor(w, timeout=0.3) is False
    assert time.monotonic() - t0 < 2

    class Broken(_FakeWindow):
        def evaluate_js(self, script, callback=None):
            raise RuntimeError("page gone")
    assert flush_editor(Broken(), timeout=2) is False


def test_pywebview_backend_tracebacks_are_quiet(caplog):
    import logging

    from photoband.desktop import _quiet_pywebview_logger
    _quiet_pywebview_logger()
    lg = logging.getLogger("pywebview")
    passed = []

    class H(logging.Handler):
        def emit(self, record):
            passed.append(record.getMessage())
    h = H()
    lg.addHandler(h)
    try:
        try:
            raise ImportError("no gi")
        except ImportError:
            lg.exception("GTK cannot be loaded")
        lg.warning("something else")
    finally:
        lg.removeHandler(h)
    assert passed == ["something else"]


# ------------------------------------------------------------------ 3. % in the source name

def test_percent_source_links_next_to_the_photo(tmp_path):
    p = tmp_path / "scan 50%.tif"
    p.write_bytes(b"abc")
    arg, tmp = MW._src_arg(str(p))
    try:
        assert "%" not in arg and arg == tmp
        assert os.path.dirname(arg) == str(tmp_path)
        assert os.path.basename(arg).startswith(".pbtmp-src-") and arg.endswith(".tif")
        assert os.path.samefile(arg, str(p))
    finally:
        os.unlink(tmp)
    assert p.read_bytes() == b"abc"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
def test_percent_in_folder_uses_a_symlink_not_a_copy(tmp_path, monkeypatch):
    d = tmp_path / "100% scans"
    d.mkdir()
    p = d / "a%d.tif"
    p.write_bytes(b"x" * 1000)
    copies = []
    monkeypatch.setattr(MW.shutil, "copyfile", lambda *a, **k: copies.append(a))
    arg, tmp = MW._src_arg(str(p))
    try:
        assert "%" not in arg and os.path.islink(arg) and os.path.samefile(arg, str(p))
        assert copies == []
    finally:
        os.unlink(tmp)
    assert p.read_bytes() == b"x" * 1000


def test_percent_falls_back_when_links_fail(tmp_path, monkeypatch):
    p = tmp_path / "50%.jpg"
    p.write_bytes(b"abc")

    def nope(*a, **k):
        raise OSError("not supported")
    monkeypatch.setattr(MW.os, "link", nope)
    monkeypatch.setattr(MW.os, "symlink", nope)
    arg, tmp = MW._src_arg(str(p))
    try:
        assert "%" not in arg and not os.path.islink(arg) and open(arg, "rb").read() == b"abc"
        assert os.path.basename(arg).startswith("src-")
    finally:
        os.unlink(tmp)


@needs_et
@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
def test_exiftool_follows_the_symlink(tmp_path, fixtures_dir):
    d = tmp_path / "scans 100%"
    src = _copy(fixtures_dir, "02_gray8_uncompressed.tif", str(d / "a%f.tif"))
    get_exiftool().write(src, ["-XMP-dc:Title=Lake", "-IFD0:Artist=Robert"])
    r = _save(src)
    assert r.ok, r.error
    md = get_exiftool().read_json(r.out_path)
    assert md.get("XMP-dc:Title") == "Lake" and md.get("IFD0:Artist") == "Robert"
    assert not [n for n in os.listdir(str(d)) if n.startswith(".pbtmp")]


def test_stale_source_links_are_swept_but_fresh_ones_are_kept(tmp_path, symlink):
    photo = tmp_path / "old.tif"
    photo.write_bytes(b"photo")
    old = time.time() - 86400
    os.utime(str(photo), (old, old))
    fresh = tmp_path / f".pbtmp-src-{int(time.time())}-abc.tif"
    stale = tmp_path / f".pbtmp-src-{int(time.time()) - 7200}-def.tif"
    os.link(str(photo), str(fresh))           # mtime is the photo's (a day old) ...
    os.link(str(photo), str(stale))
    removed = sweep_temp([str(tmp_path)])
    assert str(stale) in removed and fresh.exists()   # ... but the name's time decides
    assert photo.read_bytes() == b"photo"

    app_tmp = tmp_path / "apptmp"
    app_tmp.mkdir()
    s1 = app_tmp / f"src-{int(time.time()) - 7200}-aaa.tif"
    s2 = app_tmp / f"src-{int(time.time())}-bbb.tif"
    symlink(str(photo), str(s1))
    os.link(str(photo), str(s2))
    legacy = app_tmp / "src-0123456789abcdef.tif"
    legacy.write_bytes(b"copy")
    os.utime(str(legacy), (old, old))
    removed = sweep_app_tmp([str(app_tmp)])
    assert str(s1) in removed and str(legacy) in removed and s2.exists()
    assert photo.read_bytes() == b"photo"


# ------------------------------------------------------------------ 4/5. in-app browser roots

def test_fs_list_offers_only_roots_it_opens(client, tmp_path, monkeypatch, symlink):
    vols = tmp_path / "Volumes"
    vols.mkdir()
    symlink("/", str(vols / "Macintosh HD"), target_is_directory=True)
    (vols / "Photos SSD").mkdir()
    monkeypatch.setattr(security, "mounted_volumes",
                        lambda: [str(vols / "Macintosh HD"), str(vols / "Photos SSD")])
    roots = client.get("/api/fs/list").json()["roots"]
    assert str(vols / "Photos SSD") in roots
    assert str(vols / "Macintosh HD") not in roots
    for r in roots:
        assert client.get("/api/fs/list", params={"dir": r}).status_code == 200, r


def test_fs_list_reaches_the_last_folder_after_a_restart(client, monkeypatch):
    import tempfile
    base = tempfile.mkdtemp(prefix="pbnas-", dir="/var/tmp" if os.path.isdir("/var/tmp") else None)
    try:
        nas = os.path.join(base, "photos", "1950s")
        os.makedirs(nas)
        monkeypatch.setattr(security, "mounted_volumes", lambda: [])
        if security.may_browse(base):
            pytest.skip("the test folder is already browsable here")
        assert client.get("/api/fs/list", params={"dir": nas}).status_code == 403
        save_settings({"session": {"lastFolder": nas}})
        assert client.get("/api/fs/list", params={"dir": nas}).status_code == 200
        j = client.get("/api/fs/list", params={"dir": os.path.join(base, "photos")})
        assert j.status_code == 200
        # never the system root
        assert client.get("/api/fs/list", params={"dir": "/"}).status_code == 403
        assert client.get("/api/fs/list", params={"dir": "/etc"}).status_code == 403
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_last_folder_roots_stop_at_top_level_folders(tmp_path):
    assert security.last_folder_roots(None) == []
    if sys.platform == "win32":
        # "/" is the current drive's root on Windows, and drive roots are browsable there by
        # design (system folders are refused by may_browse): the chain stops at the drive root
        roots = security.last_folder_roots(str(tmp_path))
        drive = os.path.splitdrive(roots[0])[0]
        assert drive and roots[-1] == os.path.normcase(drive + os.sep)
        assert all(os.path.dirname(a) == b for a, b in zip(roots, roots[1:])), roots
        return
    roots = security.last_folder_roots("/etc/ssl")
    assert "/" not in roots and "/etc" not in roots
    assert security.last_folder_roots("/") == []


def test_unc_input_still_refused_unless_known(client, monkeypatch):
    monkeypatch.setattr(security, "is_unc", lambda p, platform=None: str(p).startswith("//"))
    assert client.get("/api/fs/list", params={"dir": "//attacker/share"}).status_code == 403
    assert not security.unc_browsable("//attacker/share")


# ------------------------------------------------------------------ 6. case C message

def test_case_c_overwrite_message_is_plain(tmp_path, fixtures_dir):
    p = _copy(fixtures_dir, "12_scanned_polaroid_handwriting.tif", str(tmp_path / "scan.tif"))
    before = _sha(p)
    r = _save(p, mode="overwrite")
    assert not r.ok and r.code == "case_c_overwrite"
    assert r.error == CASE_C_OVERWRITE_MSG
    assert "handwritten or printed caption" in r.error and "Settings › Saving" in r.error
    assert _sha(p) == before
