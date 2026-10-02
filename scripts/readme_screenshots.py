#!/usr/bin/env python3
"""Take the README screenshots (docs/images/editor-light.png and editor-dark.png).

    python scripts/readme_screenshots.py PHOTO [--name FILE] [--title TEXT] [--people TEXT] [--chromium PATH]

PHOTO is copied to a temporary folder (the original is never opened for writing), optionally
under another file name (--name: the real one may carry family names), the app is
started in serve mode with a fresh settings folder, the photo is opened, and the editor is
captured at 1600x956 in the light and the dark theme. --title / --people replace the caption's
Title and People lines (an empty --people clears it), so no real names need to appear. Needs the UI built (ui/dist) and
Playwright (pip install playwright; playwright install chromium, or --chromium for an installed
Chromium/Chrome).
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "images")
TOKEN = "readme-shots-0123456789abcdef"
SIZE = {"width": 1600, "height": 956}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("photo", help="the photo to show")
    ap.add_argument("--name", help="file name for the copy that is opened (default: the photo's own)")
    ap.add_argument("--title", help="text for the caption's Title line")
    ap.add_argument("--people", help="text for the caption's People line ('' clears it)")
    ap.add_argument("--chromium", help="path to a Chromium/Chrome executable for Playwright")
    args = ap.parse_args()
    from playwright.sync_api import expect, sync_playwright

    home = tempfile.mkdtemp(prefix="pb-readme-home-")
    work = os.path.join(tempfile.mkdtemp(prefix="pb-readme-"), "Photos")
    os.makedirs(work)
    name = args.name or os.path.basename(args.photo)
    # a file name only: a path (absolute, or with .. or separators) would copy outside the temp folder
    if name != os.path.basename(name) or name in ("", ".", "..") or "/" in name or "\\" in name:
        ap.error("--name must be a plain file name, not a path")
    if os.path.splitext(name)[1].lower() != os.path.splitext(args.photo)[1].lower():
        name += os.path.splitext(args.photo)[1]
    shutil.copy2(args.photo, os.path.join(work, name))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ, PHOTOBAND_HOME=home, PHOTOBAND_TOKEN=TOKEN, PHOTOBAND_NO_NATIVE_DIALOGS="1")
    exiftool = os.path.join(ROOT, "vendor", "exiftool")
    if os.path.isdir(exiftool):
        env["PATH"] = exiftool + os.pathsep + env["PATH"]
    proc = subprocess.Popen([sys.executable, "-m", "photoband", "serve", "--port", str(port), "--no-browser"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.chromium) if args.chromium else p.chromium.launch()
            for scheme in ("light", "dark"):
                pg = browser.new_page(viewport=SIZE, color_scheme=scheme)
                pg.goto(f"http://127.0.0.1:{port}/?t={TOKEN}")
                pg.wait_for_selector("text=Caption your photos")
                pg.click("text=Open folder…")
                expect(pg.locator('input[aria-label="Folder path"]')).not_to_have_value("")
                pg.fill('input[aria-label="Folder path"]', work)
                pg.keyboard.press("Enter")
                choose = pg.locator("div.dialog button:has-text('Choose folder')")
                for _ in range(100):
                    if choose.is_enabled():
                        break
                    pg.wait_for_timeout(100)
                choose.click()
                # the caption check has finished when Save turns on; then the preview settles
                pg.wait_for_selector("header.tb button.save:not([disabled])", timeout=120000)
                hint = pg.locator('[role="note"][aria-label="Getting started"] button:has-text("Got it")')
                if hint.count():
                    hint.click()
                for label, text in (("Title", args.title), ("People", args.people)):
                    if text is None:
                        continue
                    ed = pg.locator(f'[aria-label="{label}"][contenteditable]')
                    if not ed.count():
                        continue
                    ed.click()
                    pg.keyboard.press("ControlOrMeta+A")
                    pg.keyboard.press("Backspace")
                    if text:
                        pg.keyboard.type(text)
                pg.keyboard.press("Escape")
                pg.mouse.move(SIZE["width"] - 2, SIZE["height"] - 2)
                pg.wait_for_timeout(2500)
                out = os.path.join(OUT, f"editor-{scheme}.png")
                pg.screenshot(path=out)
                print("wrote", os.path.relpath(out, ROOT))
                pg.close()
            browser.close()
    finally:
        proc.terminate()
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(os.path.dirname(work), ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
