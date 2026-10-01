"""Security: token/host checks, headers, the server-side allow-list (pick ids), settings that
could widen access or run programs, reveal-not-launch, decompression limits, Google Fonts
file names, template validation, dialog argv building, and the server socket."""
import base64
import io
import json
import os
import socket
import subprocess
import sys
import zlib

import pytest
from fastapi.testclient import TestClient

from photoband import dialogs, exiftool, fonts, record, security, server, templates

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOK = security.TOKEN


@pytest.fixture
def client():
    security.reset()
    c = TestClient(server.app, base_url="http://127.0.0.1")
    c.headers["X-Photoband-Token"] = TOK
    yield c
    security.reset()


@pytest.fixture
def anon():
    return TestClient(server.app, base_url="http://127.0.0.1")


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / "a.jpg").write_bytes(b"x")
    (tmp_path / "pics" / "b.png").write_bytes(b"x")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "secret.jpg").write_bytes(b"x")
    return tmp_path


def _listing(client, d):
    r = client.get("/api/fs/list", params={"dir": str(d)})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ token, host, headers

def test_token_required(anon):
    assert anon.get("/api/log").status_code == 403
    assert anon.get("/api/log", headers={"X-Photoband-Token": "wrong"}).status_code == 403
    assert anon.get("/api/log", headers={"X-Photoband-Token": TOK}).status_code == 200


def test_host_checked_before_token(anon):
    r = anon.get("/api/log", headers={"X-Photoband-Token": TOK, "Host": "evil.example:80"})
    assert r.status_code == 403 and "host" in r.json()["error"].lower()
    r = anon.get("/api/log", headers={"Host": "evil.example"})  # no token: still the host error
    assert "host" in r.json()["error"].lower()
    r = anon.get("/api/log", headers={"X-Photoband-Token": TOK, "Host": "127.0.0.1.evil.example"})
    assert r.status_code == 403
    assert anon.get("/api/log", headers={"X-Photoband-Token": TOK, "Host": "localhost:1234"}).status_code == 200


def test_query_token_only_on_media_get_routes(anon):
    # API calls need the header
    assert anon.get("/api/state", params={"t": TOK}).status_code == 403
    assert anon.post(f"/api/settings?t={TOK}", json={}).status_code == 403
    assert anon.post(f"/api/fs/allow?t={TOK}", json={}).status_code == 403
    # fonts / images / downloads may use ?t= (404/other errors, but not the token 403)
    r = anon.get("/fonts/file/nope", params={"t": TOK})
    assert r.status_code == 404
    r = anon.get("/api/photo/proxy", params={"t": TOK, "path": "/nonexistent.jpg"})
    assert r.status_code == 403 and r.json()["error"] != "Forbidden"  # the allow-list, not the token
    assert anon.get("/api/photo/proxy", params={"t": "bad", "path": "/x.jpg"}).json()["error"] == "Forbidden"


def test_security_headers_everywhere(anon, client):
    for r in (anon.get("/api/log"), client.get("/api/log"), anon.get("/"),
              anon.get("/api/log", headers={"Host": "evil"})):
        csp = r.headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "base-uri 'none'" in csp
        assert "img-src 'self' blob: data:" in csp and "connect-src 'self'" in csp
        assert r.headers.get("referrer-policy") == "no-referrer"
        assert r.headers.get("x-frame-options") == "DENY"
        assert r.headers.get("x-content-type-options") == "nosniff"


def test_token_compare_is_constant_time_helper():
    assert security.token_ok(TOK)
    assert not security.token_ok(TOK[:-1])
    assert not security.token_ok(None)
    assert not security.token_ok("")
    assert len(TOK) >= security.MIN_TOKEN_LEN


def test_short_env_token_refused(tmp_path):
    env = dict(os.environ, PHOTOBAND_TOKEN="short", PHOTOBAND_HOME=str(tmp_path))
    r = subprocess.run([sys.executable, "-m", "photoband", "serve", "--port", "0", "--no-browser"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "at least 16" in r.stderr
    assert "http://" not in r.stdout


# ------------------------------------------------------------------ allow-list via pick ids

def test_fs_allow_rejects_raw_paths(client, tree):
    f = str(tree / "other" / "secret.jpg")
    r = client.post("/api/fs/allow", json={"paths": [f]})
    assert r.status_code == 403
    r = client.post("/api/fs/allow", json={"paths": ["/"], "parentWritable": True})
    assert r.status_code == 403
    assert not security.is_allowed(f)
    assert client.get("/api/photo/meta", params={"path": f}).status_code == 403


def test_fs_allow_with_pick_ids(client, tree):
    lst = _listing(client, tree / "pics")
    assert lst["pick"] and all(e.get("pick") for e in lst["entries"])
    a = next(e for e in lst["entries"] if e["name"] == "a.jpg")
    r = client.post("/api/fs/allow", json={"picks": [a["pick"]]})
    assert r.status_code == 200, r.text
    assert r.json()["paths"] == [os.path.realpath(a["path"])]
    assert security.is_allowed(a["path"])
    assert not security.is_allowed(str(tree / "pics" / "b.png"))  # only what was picked
    # one-time ids
    assert client.post("/api/fs/allow", json={"picks": [a["pick"]]}).status_code == 403
    # made-up ids
    assert client.post("/api/fs/allow", json={"picks": ["nope"]}).status_code == 403
    # the listed folder itself ("Choose folder" with nothing selected)
    r = client.post("/api/fs/allow", json={"picks": [lst["pick"]]})
    assert r.status_code == 200
    assert security.is_allowed(str(tree / "pics" / "b.png"))
    assert not security.is_allowed(str(tree / "other" / "secret.jpg"))


def test_fs_allow_save_needs_folder_pick_and_plain_name(client, tree):
    lst = _listing(client, tree / "pics")
    for bad in ("../other/secret.jpg", "/etc/passwd", "a/b.jpg", "..", "", "a\\b.jpg", "con.jpg"):
        r = client.post("/api/fs/allow", json={"save": {"folder": lst["pick"], "name": bad}})
        assert r.status_code in (400, 403), bad
        lst = _listing(client, tree / "pics")  # the id may have been used up
    a = next(e for e in lst["entries"] if e["name"] == "a.jpg")
    r = client.post("/api/fs/allow", json={"save": {"folder": a["pick"], "name": "x.jpg"}})  # a file, not a folder
    assert r.status_code == 403
    lst = _listing(client, tree / "pics")
    r = client.post("/api/fs/allow", json={"save": {"folder": lst["pick"], "name": "out.jpg"}})
    assert r.status_code == 200, r.text
    assert r.json()["paths"] == [os.path.join(os.path.realpath(tree / "pics"), "out.jpg")]
    assert not security.is_allowed(str(tree / "other" / "secret.jpg"))


def test_pick_ids_expire(client, tree, monkeypatch):
    lst = _listing(client, tree / "pics")
    monkeypatch.setattr(security, "PICK_TTL", -1)
    lst2 = _listing(client, tree / "pics")
    assert client.post("/api/fs/allow", json={"picks": [lst2["pick"]]}).status_code == 403
    assert client.post("/api/fs/allow", json={"picks": [lst["pick"]]}).status_code == 200


def test_reopen_last_folder_only(client, tree):
    lst = _listing(client, tree / "pics")
    client.post("/api/fs/allow", json={"picks": [lst["pick"]]})
    assert client.post("/api/open", json={"paths": [str(tree / "pics")]}).status_code == 200
    security.reset()
    assert client.post("/api/fs/allow", json={"paths": [str(tree / "pics")]}).status_code == 200
    assert client.post("/api/fs/allow", json={"paths": [str(tree / "other")]}).status_code == 403


def test_check_returns_realpath(client, tree):
    link = tree / "link"
    os.symlink(tree / "pics", link)
    security.allow_root(str(link))
    assert security.check(str(link / "a.jpg")) == os.path.realpath(tree / "pics" / "a.jpg")
    # a symlink inside an allowed folder can't point outside it
    os.symlink(tree / "other" / "secret.jpg", tree / "pics" / "escape.jpg")
    with pytest.raises(PermissionError):
        security.check(str(tree / "pics" / "escape.jpg"))


def test_unc_paths(monkeypatch):
    for p in ("\\\\server\\share\\x.jpg", "//server/share/x.jpg", "\\\\?\\C:\\x.jpg", "\\\\.\\pipe\\x"):
        assert security.is_unc(p, "win32")
        assert not security.is_unc(p, "linux")
    assert not security.is_unc("C:\\Photos\\x.jpg", "win32")
    monkeypatch.setattr(security, "is_unc", lambda p, platform=None: str(p).replace("/", "\\").startswith("\\\\"))
    security.reset()
    security.allow(["\\\\server\\share\\x.jpg"])  # not from a dialog: ignored
    assert not security.is_allowed("\\\\server\\share\\x.jpg")
    security.allow(["\\\\server\\share\\x.jpg"], from_dialog=True)
    assert security.is_allowed("\\\\server\\share\\x.jpg")
    security.reset()


def test_fs_list_rejects_unc(client, monkeypatch):
    monkeypatch.setattr(security, "is_unc", lambda p, platform=None: str(p).startswith("//"))
    assert client.get("/api/fs/list", params={"dir": "//attacker/share"}).status_code == 403


# ------------------------------------------------------------------ settings

def test_settings_cannot_set_exiftool_path(client):
    r = client.post("/api/settings", json={"advanced": {"exiftoolPath": "/tmp/evil.pl", "cacheSizeMB": 1234}})
    assert r.status_code == 200
    assert r.json()["advanced"]["exiftoolPath"] == ""
    assert r.json()["advanced"]["cacheSizeMB"] == 1234


def test_settings_cannot_widen_allow_list(client, tree):
    for k in ("backupFolder", "fixedFolder"):
        r = client.post("/api/settings", json={"saving": {k: "/"}})
        assert r.json()["saving"][k] == ""
    r = client.post("/api/settings", json={"session": {"lastFolder": "/"}})
    assert r.json()["session"]["lastFolder"] != "/"
    assert not security.is_allowed("/etc/passwd")
    assert not security.is_allowed(str(tree / "other" / "secret.jpg"))
    # a folder the user chose is accepted (stored as its real path)
    lst = _listing(client, tree / "pics")
    client.post("/api/fs/allow", json={"picks": [lst["pick"]]})
    r = client.post("/api/settings", json={"saving": {"backupFolder": str(tree / "pics")}})
    assert r.json()["saving"]["backupFolder"] == os.path.realpath(tree / "pics")
    assert client.post("/api/settings", json={"saving": {"backupFolder": ""}}).json()["saving"]["backupFolder"] == ""
    # malformed sections are dropped rather than replacing the settings object
    r = client.post("/api/settings", json={"saving": "x", "general": {"theme": "dark"}})
    assert isinstance(r.json()["saving"], dict) and r.json()["general"]["theme"] == "dark"


def test_exiftool_only_via_dialog(client, tmp_path, monkeypatch):
    fake = tmp_path / "fake_et.pl"
    fake.write_text("open(F,'>','%s'); print F 'pwned';" % (tmp_path / "PWNED"))
    monkeypatch.setattr(dialogs, "ask", lambda kind, **kw: [str(fake)])
    r = client.post("/api/dialog", json={"kind": "exiftool"})
    assert r.status_code == 400
    assert client.get("/api/state").json()["settings"]["advanced"]["exiftoolPath"] == ""
    assert not (tmp_path / "PWNED").exists()
    # a script named exiftool that is not ExifTool: runs only as "-ver" and is rejected
    named = tmp_path / "exiftool"
    named.write_text("print 'hello\\n';")
    monkeypatch.setattr(dialogs, "ask", lambda kind, **kw: [str(named)])
    assert client.post("/api/dialog", json={"kind": "exiftool"}).status_code == 400
    real = exiftool.shutil.which("exiftool")
    if real:
        monkeypatch.setattr(dialogs, "ask", lambda kind, **kw: [real])
        r = client.post("/api/dialog", json={"kind": "exiftool"})
        assert r.status_code == 200, r.text
        assert r.json()["settings"]["advanced"]["exiftoolPath"] == os.path.abspath(real)
        r = client.post("/api/exiftool/use-bundled")
        assert r.json()["advanced"]["exiftoolPath"] == ""
    assert client.post("/api/dialog", json={"kind": "evil"}).status_code == 400


def test_find_exiftool_ignores_non_exiftool_override(tmp_path):
    fake = tmp_path / "fake_et.pl"
    fake.write_text("open(F,'>','%s');" % (tmp_path / "PWNED"))
    assert exiftool.exiftool_command(str(fake)) is None
    assert exiftool.exiftool_command("relative/exiftool") is None
    try:
        cmd = exiftool.find_exiftool(str(fake))
    except exiftool.ExifToolError:
        cmd = None
    assert cmd is None or str(fake) not in cmd
    assert not (tmp_path / "PWNED").exists()


# ------------------------------------------------------------------ open-folder reveals

def test_open_folder_reveals_never_launches(client, tree, monkeypatch):
    calls = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: calls.append(a[0]))
    f = str(tree / "other" / "secret.jpg")
    assert client.post("/api/open-folder", json={"which": "file", "path": f}).status_code == 403
    assert calls == []
    lst = _listing(client, tree / "pics")
    a = next(e for e in lst["entries"] if e["name"] == "a.jpg")
    client.post("/api/fs/allow", json={"picks": [a["pick"]]})
    assert client.post("/api/open-folder", json={"which": "file", "path": a["path"]}).status_code == 200
    assert calls and calls[-1] == ["xdg-open", os.path.realpath(tree / "pics")]
    assert server.reveal_argv("/x/Evil.app", "darwin") == ["open", "-R", "/x/Evil.app"]
    assert server.reveal_argv("C:\\x\\run.exe", "win32") == ["explorer", "/select,C:\\x\\run.exe"]
    app_dir = tree / "pics" / "Thing.app"
    app_dir.mkdir()
    assert server.reveal_argv(str(app_dir), "linux") == ["xdg-open", str(tree / "pics")]


# ------------------------------------------------------------------ zlib bombs

def test_record_zlib_bomb_rejected():
    bomb = zlib.compress(b"{" + b" " * (64 * 1024 * 1024) + b"}", 9)
    val = record.PREFIX + base64.b64encode(bomb).decode()
    assert len(val) < 200_000
    assert record.decode(val) is None
    with pytest.raises(record.DecompressionLimit):
        record.safe_decompress(bomb)
    with pytest.raises(zlib.error):
        record.safe_decompress(zlib.compress(b"hello")[:-3])
    assert record.decode(record.encode({"a": [1, 2]})) == {"a": [1, 2]}
    assert record.safe_decompress(zlib.compress(b"x" * 1000), 1000) == b"x" * 1000
    with pytest.raises(record.DecompressionLimit):
        record.safe_decompress(zlib.compress(b"x" * 1001), 1000)


# ------------------------------------------------------------------ Google Fonts

def test_gf_filenames_sanitized():
    meta = ('fonts { filename: "../../../../escaped.ttf" }\n fonts { filename: "Roboto[wdth,wght].ttf" }\n'
            'fonts { filename: "/etc/x.ttf" }\n fonts { filename: "a\\\\b.ttf" }\n fonts { filename: "ok-Bold.ttf" }\n'
            'fonts { filename: ".hidden.ttf" }\n fonts { filename: "x.otf" }\n')
    assert fonts._gf_filenames(meta) == ["Roboto[wdth,wght].ttf", "ok-Bold.ttf"]
    assert fonts._gf_family_dir("../../Evil Family") == "evilfamily"
    with pytest.raises(ValueError):
        fonts._gf_family_dir("../..")


def test_gf_catalog_family_names_plain():
    cat = fonts._clean_catalog([
        {"family": "Roboto Slab", "category": "Serif", "subsets": ["latin"]},
        {"family": "x', serif; background:url(http://127.0.0.1:9/leak)", "category": "", "subsets": []},
        {"family": "<img src=x onerror=alert(1)>"},
        {"family": 5}, "junk",
    ])
    assert [f["family"] for f in cat] == ["Roboto Slab"]


def test_gf_download_traversal_and_non_fonts(monkeypatch, tmp_path):
    home = os.environ["PHOTOBAND_HOME"]

    def fake(url, timeout=0):
        if url.endswith("METADATA.pb"):
            return io.BytesIO(b'fonts { filename: "../../../../escaped-x.ttf" }\nfonts { filename: "Good.ttf" }\n')
        return io.BytesIO(b"not a font")
    monkeypatch.setattr(fonts.urllib.request, "urlopen", fake)
    monkeypatch.setattr(fonts, "build_index", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        fonts.google_download("Roboto")
    assert not os.path.exists(os.path.join(os.path.dirname(home), "escaped-x.ttf"))
    assert not os.path.exists(os.path.join(home, "fonts", "google", "roboto", "Good.ttf"))

    def big(url, timeout=0):
        if url.endswith("METADATA.pb"):
            return io.BytesIO(b'fonts { filename: "Big.ttf" }\n')
        return io.BytesIO(b"\0" * (fonts.GF_MAX_BYTES + 10))
    monkeypatch.setattr(fonts.urllib.request, "urlopen", big)
    with pytest.raises(RuntimeError):
        fonts.google_download("Big")


# ------------------------------------------------------------------ templates

def _tpl():
    t = json.loads(json.dumps(templates.BUILTINS[1]))
    t.pop("id")
    t.pop("builtin")
    return t


@pytest.mark.parametrize("mutate", [
    lambda t: t["layout"].__setitem__("borderColor", "red;background:url(x)"),
    lambda t: t["layout"]["border"].__setitem__("top", float("inf")),
    lambda t: t["layout"]["border"].__setitem__("top", 1e12),
    lambda t: t["layout"]["border"].__setitem__("top", "5"),
    lambda t: t["layout"]["columns"].__setitem__("count", 7),
    lambda t: t["blocks"][0]["style"].__setitem__("font", {"x": 1}),
    lambda t: t["blocks"][0]["style"].__setitem__("color", "#12345"),
    lambda t: t["blocks"][0]["style"].__setitem__("size", float("nan")),
    lambda t: t["blocks"][0]["style"].__setitem__("align", "<script>"),
    lambda t: t["blocks"][0].__setitem__("format", "x" * 2001),
    lambda t: t["blocks"][0].__setitem__("id", ""),
    lambda t: t["blocks"].append(dict(t["blocks"][0])),  # duplicate id
    lambda t: t["blocks"].append("nope"),
    lambda t: t.__setitem__("blocks", {"a": 1}),
    lambda t: t.__setitem__("name", ["x"]),
    lambda t: t.__setitem__("layout", "x"),
])
def test_template_validation_rejects(mutate):
    t = _tpl()
    mutate(t)
    with pytest.raises(ValueError):
        templates.import_template(t)


def test_template_import_ok_and_api_rejects(client):
    t = templates.import_template(_tpl())
    assert t["id"].startswith("u-")
    templates.delete_template(t["id"])
    bad = _tpl()
    bad["blocks"][0]["style"]["color"] = "javascript:alert(1)"
    assert client.post("/api/templates/import", json=bad).status_code == 400


# ------------------------------------------------------------------ dialog argv (no interpolation)

EVIL_T = 'Save" & (do shell script "touch /tmp/PWNED") & "'
EVIL_DIR_NAME = 'dir" & (do shell script "id") & "'
EVIL_NAME = 'Grandma 1962" & (do shell script "open -a Calculator") & "-captioned.jpg'


def test_osascript_argv_has_no_interpolation(tmp_path):
    d = tmp_path / EVIL_DIR_NAME
    d.mkdir()
    for kind in dialogs.KINDS:
        argv = dialogs.osascript_argv(kind, EVIL_T, str(d), EVIL_NAME)
        assert argv[:3] == ["osascript", "-e", dialogs.OSASCRIPT]
        assert argv[3] == kind
        assert "do shell script" not in dialogs.OSASCRIPT
        assert "on run argv" in dialogs.OSASCRIPT
        assert argv[4] == EVIL_T and argv[5] == str(d) and argv[6] == EVIL_NAME
    # a save name can't carry folders
    assert dialogs.osascript_argv("save-file", "t", "", "../../x/y.jpg")[6] == "y.jpg"
    # unknown kinds don't reach the script as-is
    assert dialogs.osascript_argv('open-folder" & x', "t", "", "")[3] == "open-files"


def test_osascript_runs_fixed_script(monkeypatch, tmp_path):
    captured = {}

    class R:
        returncode = 0
        stdout = "/tmp/x.jpg\n"
        stderr = ""
    monkeypatch.setattr(dialogs, "_run", lambda args, timeout=600: captured.setdefault("args", args) and R())
    d = tmp_path / EVIL_DIR_NAME
    d.mkdir()
    assert dialogs._osascript("save-file", EVIL_T, str(d), EVIL_NAME) == ["/tmp/x.jpg"]
    assert captured["args"][2] == dialogs.OSASCRIPT


def test_linux_dialog_argv(tmp_path):
    for tool in ("zenity", "kdialog"):
        for kind in dialogs.KINDS:
            argv = dialogs.linux_argv(tool, kind, "-" + EVIL_T, str(tmp_path), "--help")
            assert argv[0] == tool
            # every value is its own argument or joined to its option; none looks like a bare option
            for a in argv[1:]:
                assert not a.startswith("-") or a.startswith("--")
            assert all(isinstance(a, str) for a in argv)
            assert not any(a == "--help" for a in argv)


def test_ask_rejects_unknown_kind():
    with pytest.raises(ValueError):
        dialogs.ask("rm -rf")


# ------------------------------------------------------------------ server socket

def test_port_squatting_aborts(tmp_path):
    squat = socket.socket()
    squat.bind(("127.0.0.1", 0))
    squat.listen(1)
    port = squat.getsockname()[1]
    try:
        env = dict(os.environ, PHOTOBAND_HOME=str(tmp_path), PHOTOBAND_NO_NATIVE_DIALOGS="1")
        env.pop("PHOTOBAND_TOKEN", None)
        r = subprocess.run([sys.executable, "-m", "photoband", "serve", "--port", str(port), "--no-browser"],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    finally:
        squat.close()
    assert r.returncode == 1
    assert "?t=" not in r.stdout + r.stderr


def test_bind_socket_is_ours():
    from photoband.__main__ import StartError, bind_socket
    s = bind_socket(0)
    try:
        assert s.getsockname()[0] == "127.0.0.1"
        with pytest.raises(StartError):
            bind_socket(s.getsockname()[1])
    finally:
        s.close()
