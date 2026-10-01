"""Install/packaging plumbing: fetch_vendor (with fake archives), resource lookup,
logging setup and the frozen launcher's no-console handling."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fv = _load("fetch_vendor_under_test", "scripts/fetch_vendor.py")


def _fake_tar(path, ver="99.01"):
    with tarfile.open(path, "w:gz") as t:
        for name, data in [(f"Image-ExifTool-{ver}/exiftool", b"#!/usr/bin/perl\nprint 'x';\n" * 100),
                           (f"Image-ExifTool-{ver}/lib/Image/ExifTool.pm", b"package Image::ExifTool;\n"),
                           (f"Image-ExifTool-{ver}/README", b"readme")]:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))


def _fake_zip(path, nested=True):
    pre = "exiftool-99.01_64/" if nested else ""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(pre + "exiftool(-k).exe", b"MZ" + b"\0" * 4000)
        z.writestr(pre + "exiftool_files/perl.exe", b"MZ")
        z.writestr(pre + "exiftool_files/lib/Image/ExifTool.pm", b"pkg")


def _lock(tmp_path, kind, file, sha):
    p = tmp_path / "lock.json"
    entry = {"file": file, "sha256": sha}
    p.write_text(json.dumps({"exiftool": {"version": "99.01", "unix": entry, "win64": entry,
                                          "mirrors": ["http://127.0.0.1:9/{file}"]}}))
    return str(p)


def test_fetch_vendor_unix_layout_and_checksum(tmp_path):
    arc = tmp_path / "Image-ExifTool-99.01.tar.gz"
    _fake_tar(arc)
    dest = tmp_path / "vendor" / "exiftool"
    lock = _lock(tmp_path, "unix", arc.name, fv.sha256_file(str(arc)))
    assert fv.main(["--archive", str(arc), "--platform", "unix", "--dest", str(dest), "--lock", lock]) == 0
    assert sorted(os.listdir(dest)) == ["exiftool", "lib"]
    assert os.access(dest / "exiftool", os.X_OK)
    # leaves nothing else behind next to the destination
    assert sorted(os.listdir(dest.parent)) == ["exiftool"]


@pytest.mark.parametrize("nested", [True, False])
def test_fetch_vendor_windows_layout(tmp_path, nested):
    arc = tmp_path / "exiftool-99.01_64.zip"
    _fake_zip(arc, nested)
    dest = tmp_path / "vendor" / "exiftool"
    dest.mkdir(parents=True)
    (dest / "stale.txt").write_text("old copy")
    lock = _lock(tmp_path, "win64", arc.name, fv.sha256_file(str(arc)))
    assert fv.main(["--archive", str(arc), "--platform", "win64", "--dest", str(dest), "--lock", lock]) == 0
    assert sorted(os.listdir(dest)) == ["exiftool.exe", "exiftool_files"]
    assert (dest / "exiftool_files" / "perl.exe").exists()
    assert sorted(os.listdir(dest.parent)) == ["exiftool"]


def test_fetch_vendor_checksum_mismatch_keeps_old_copy(tmp_path, capsys):
    arc = tmp_path / "a.tar.gz"
    _fake_tar(arc)
    dest = tmp_path / "vendor" / "exiftool"
    dest.mkdir(parents=True)
    (dest / "exiftool").write_text("old")
    lock = _lock(tmp_path, "unix", arc.name, "0" * 64)
    assert fv.main(["--archive", str(arc), "--platform", "unix", "--dest", str(dest), "--lock", lock]) == 1
    assert (dest / "exiftool").read_text() == "old"
    assert "Checksum mismatch" in capsys.readouterr().err


def test_fetch_vendor_refuses_unverified_lock(tmp_path, capsys):
    lock = _lock(tmp_path, "unix", "x.tar.gz", "TBD-verify")
    assert fv.main(["--platform", "unix", "--dest", str(tmp_path / "d"), "--lock", lock]) == 2
    assert "REFUSING" in capsys.readouterr().err


def test_fetch_vendor_network_failure_is_friendly(tmp_path, capsys):
    lock = _lock(tmp_path, "unix", "x.tar.gz", "TBD-verify")
    rc = fv.main(["--no-verify", "--platform", "unix", "--dest", str(tmp_path / "d"), "--lock", lock])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Could not download ExifTool" in err and "brew install exiftool" in err and "winget" in err
    assert "Traceback" not in err
    assert not (tmp_path / "d").exists()


def test_fetch_vendor_rejects_path_traversal(tmp_path):
    arc = tmp_path / "evil.zip"
    with zipfile.ZipFile(arc, "w") as z:
        z.writestr("../../evil.exe", b"MZ")
    lock = _lock(tmp_path, "win64", arc.name, fv.sha256_file(str(arc)))
    dest = tmp_path / "v" / "exiftool"
    assert fv.main(["--archive", str(arc), "--platform", "win64", "--dest", str(dest), "--lock", lock]) == 1
    assert not (tmp_path / "evil.exe").exists()


def test_pinned_lock_is_well_formed():
    lock = fv.load_lock()
    assert lock["version"] in lock["unix"]["file"] and lock["version"] in lock["win64"]["file"]
    assert any("sourceforge" in m for m in lock["mirrors"])


def test_resource_lookup_prefers_installed_package_data(tmp_path, monkeypatch):
    from photoband import paths
    monkeypatch.delenv("PHOTOBAND_UI_DIST", raising=False)
    # source checkout / editable install: the repo layout
    monkeypatch.setattr(paths, "_package_data", lambda: None)
    assert paths.ui_dist() == os.path.join(ROOT, "ui", "dist")
    assert paths.fonts_dir() == os.path.join(ROOT, "fonts")
    # installed wheel: photoband/_data
    monkeypatch.setattr(paths, "_package_data", lambda: str(tmp_path))
    assert paths.ui_dist() == os.path.join(str(tmp_path), "ui", "dist")
    assert paths.fonts_dir() == os.path.join(str(tmp_path), "fonts")
    monkeypatch.setenv("PHOTOBAND_UI_DIST", "/x/dist")
    assert paths.ui_dist() == "/x/dist"


def test_version_single_source():
    import photoband
    with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    assert 'dynamic = ["version"]' in text and "photoband.__version__" in text
    assert photoband.__version__.count(".") == 2


def test_serve_mode_never_imports_pywebview():
    code = ("import sys, photoband.server, photoband.__main__ as m; "
            "assert 'webview' not in sys.modules, 'pywebview imported'; print('ok')")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("ok")


def test_launcher_survives_missing_std_streams(tmp_path):
    """A windowed build has sys.stdout/sys.stderr = None. The launcher redirects them to
    logs/console.log and logging goes to logs/photoband.log; --version must still work."""
    import socket
    env = dict(os.environ, PHOTOBAND_HOME=str(tmp_path), PYTHONPATH=ROOT)
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    code = ("import sys; sys.stdout = None; sys.stderr = None;"
            f"sys.argv = ['Photoband', 'serve', '--no-browser', '--port', '{port}'];"
            "sys.path.insert(0, 'packaging'); import launcher; raise SystemExit(launcher.run())")
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                           timeout=120)
    finally:
        busy.close()
    # the port is taken: main() returns 1 and reports it on the redirected stderr
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert r.stdout == "" and r.stderr == ""
    console = (tmp_path / "logs" / "console.log").read_text()
    assert "could not listen" in console.lower()
    assert "starting" in (tmp_path / "logs" / "photoband.log").read_text()


def test_uvicorn_config_needs_no_tty(monkeypatch):
    import uvicorn

    from photoband.logsetup import UVICORN_LOG_CONFIG
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    uvicorn.Config(lambda *a: None, log_config=UVICORN_LOG_CONFIG, log_level="warning")


def test_osascript_message_is_passed_as_argv(monkeypatch):
    launcher = _load("launcher_under_test", "packaging/launcher.py")
    calls = []
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    evil = 'x" & do shell script "touch /tmp/pwned" & "'
    launcher._show_error("Photoband could not start", evil)
    cmd = calls[0]
    assert cmd[0] == "/usr/bin/osascript" and evil not in cmd[2] and cmd[-1] == evil
