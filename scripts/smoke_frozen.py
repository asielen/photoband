#!/usr/bin/env python3
"""Smoke-test a built Photoband (the PyInstaller app, or any `photoband` command).

    python scripts/smoke_frozen.py dist/Photoband/Photoband            # Linux
    python scripts/smoke_frozen.py dist\\Photoband\\Photoband.exe       # Windows
    python scripts/smoke_frozen.py dist/arm64/Photoband.app/Contents/MacOS/Photoband
    python scripts/smoke_frozen.py -- photoband                        # an installed command

Starts ``<app> serve --no-browser`` on a free port with a known PHOTOBAND_TOKEN (the
windowed Windows build has no stdout to read the URL from) and a throwaway PHOTOBAND_HOME,
then checks:
  1. GET /                 serves the built UI (not the "UI is not built" page), and its assets load
  2. GET /api/fonts        bundled fonts are found
  3. an LZW-compressed TIFF opens: allow it the way the in-app browser does
     (/api/fs/list pick id -> /api/fs/allow), then GET /api/photo/proxy returns an image.
     This is what catches missing imagecodecs modules in a frozen build.
  4. (--expect-exiftool) GET /api/photo/meta reads metadata through ExifTool
Pass --tiff to use an existing LZW TIFF; otherwise one is written with tifffile.
Only the stdlib is needed (plus numpy/tifffile when no --tiff is given).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_lzw_tiff(path: str) -> None:
    import numpy as np
    import tifffile
    y, x = np.mgrid[0:240, 0:320]
    img = np.stack([(x * 200) % 65536, (y * 270) % 65536, ((x + y) * 100) % 65536], -1).astype("uint16")
    tifffile.imwrite(path, img, photometric="rgb", compression="lzw")


class Client:
    def __init__(self, port: int, token: str):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def get(self, path: str, auth: bool = True, timeout: float = 60):
        req = urllib.request.Request(self.base + path)
        if auth:
            req.add_header("X-Photoband-Token", self.token)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def post(self, path: str, body: dict, timeout: float = 60):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Photoband-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("app", nargs="+", help="the executable (and any leading arguments)")
    ap.add_argument("--tiff", help="an existing LZW TIFF to open")
    ap.add_argument("--expect-exiftool", action="store_true", help="also require metadata to load via ExifTool")
    ap.add_argument("--startup-timeout", type=float, default=90)
    args = ap.parse_args(argv)

    work = tempfile.mkdtemp(prefix="photoband-smoke-")
    home = os.path.join(work, "home")
    photos = os.path.join(work, "photos")
    os.makedirs(photos)
    tiff = os.path.join(photos, "smoke_lzw.tif")
    if args.tiff:
        shutil.copy2(args.tiff, tiff)
    else:
        make_lzw_tiff(tiff)

    port, token = free_port(), secrets.token_urlsafe(24)
    env = dict(os.environ, PHOTOBAND_HOME=home, PHOTOBAND_TOKEN=token, PHOTOBAND_NO_SYSTEM_FONTS="1",
               PHOTOBAND_NO_NATIVE_DIALOGS="1")
    out_path = os.path.join(work, "app-output.txt")
    out = open(out_path, "wb")
    cmd = list(args.app) + ["serve", "--no-browser", "--port", str(port)]
    print("starting:", " ".join(cmd), flush=True)
    proc = subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    c = Client(port, token)
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(': ' + detail) if detail else ''}", flush=True)
        if not ok:
            failures.append(name)

    try:
        deadline = time.time() + args.startup_timeout
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"the app exited with code {proc.returncode} before serving")
            try:
                status, ctype, body = c.get("/", auth=False, timeout=5)
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if time.time() > deadline:
                    raise RuntimeError("the app did not start serving in time")
                time.sleep(0.5)
        html = body.decode("utf-8", "replace")
        check("GET / serves the built UI", status == 200 and "not built" not in html and "<script" in html,
              f"{status} {ctype}")
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
        ok_assets = True
        for a in assets:
            try:
                ok_assets &= c.get(a, auth=False)[0] == 200
            except urllib.error.HTTPError:
                ok_assets = False
        check("UI assets load", bool(assets) and ok_assets, f"{len(assets)} assets")

        _, _, body = c.get("/api/fonts")
        fams = json.loads(body).get("families", [])
        bundled = [f for f in fams if f.get("source") == "bundled"]
        check("bundled fonts found", len(bundled) >= 10, f"{len(bundled)} bundled families")
        if bundled and bundled[0].get("faces"):
            st, ct, data = c.get(f"/fonts/file/{bundled[0]['faces'][0]['file']}")
            check("a bundled font file is served", st == 200 and len(data) > 1000, f"{len(data)} bytes")

        _, _, body = c.get("/api/fs/list?dir=" + urllib.parse.quote(photos))
        entry = next((e for e in json.loads(body)["entries"] if e["name"] == os.path.basename(tiff)), None)
        check("in-app browser lists the TIFF", entry is not None)
        if entry:
            c.post("/api/fs/allow", {"picks": [entry["pick"]]})
            q = urllib.parse.quote(entry["path"])
            try:
                st, ct, data = c.get(f"/api/photo/proxy?path={q}", timeout=120)
                check("LZW TIFF opens (proxy image)", st == 200 and ct.startswith("image/") and len(data) > 100,
                      f"{st} {ct} {len(data)} bytes")
            except urllib.error.HTTPError as e:
                check("LZW TIFF opens (proxy image)", False, f"{e.code} {e.read()[:300]!r}")
            try:
                st, ct, data = c.get(f"/api/photo/meta?path={q}", timeout=120)
                meta = json.loads(data)
                check("photo metadata loads", st == 200, f"{meta.get('info', {}).get('compression', '?')}")
            except urllib.error.HTTPError as e:
                msg = e.read()[:300]
                if args.expect_exiftool:
                    check("photo metadata loads", False, f"{e.code} {msg!r}")
                else:
                    print(f"[SKIP] photo metadata (needs ExifTool; pass --expect-exiftool to require it): "
                          f"{e.code} {msg!r}")
    except Exception as e:  # noqa: BLE001 - report and fail
        check("smoke run", False, f"{type(e).__name__}: {e}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        out.close()
        log = os.path.join(home, "logs", "photoband.log")
        if failures:
            for p in (out_path, log, os.path.join(home, "logs", "console.log")):
                if os.path.exists(p):
                    print(f"\n----- {p} -----")
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        print(fh.read()[-6000:])
        shutil.rmtree(work, ignore_errors=True)
    print("SMOKE TEST", "FAILED: " + ", ".join(failures) if failures else "PASSED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
