"""Editing a photo's details and faces in the Metadata tab and on the photo, end to end: the edits
reach the caption, "Save metadata only" writes them into the file (checked with ExifTool), undo works.

Run:  cd ui && npm run build && cd .. && python -m pytest tests/e2e/test_details_faces.py -q
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time

import numpy as np
import pytest
from PIL import Image

pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.environ.get("PHOTOBAND_SHOTS") or os.path.join(ROOT, "tests", "_artifacts", "details")
TOKEN = "e2e-details-0123456789abcdef"


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _photo(path):
    rng = np.random.default_rng(5)
    a = rng.integers(40, 200, (600, 900, 3), dtype=np.uint8)
    Image.fromarray(a).save(path, quality=90)
    j = path + ".json"
    with open(j, "w", encoding="utf-8") as fh:
        json.dump([{"SourceFile": path, "XMP-dc:Title": "Old title", "XMP-dc:Subject": ["Ann", "picnic"],
                    "XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": 900, "H": 600, "Unit": "pixel"},
                                              "RegionList": [{"Type": "Face", "Name": "Ann",
                                                              "Area": {"X": 0.3, "Y": 0.4, "W": 0.1, "H": 0.15, "Unit": "normalized"}}]}}], fh)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-n", "-struct", f"-json={j}", path], check=True)
    os.unlink(j)


def _md(path):
    out = subprocess.run(["exiftool", "-j", "-G1", "-struct", path], capture_output=True, text=True, check=True).stdout
    return json.loads(out)[0]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    if not os.path.exists(os.path.join(ROOT, "ui", "dist", "index.html")):
        pytest.skip("UI not built (cd ui && npm run build)")
    home = tmp_path_factory.mktemp("e2ehome")
    work = tmp_path_factory.mktemp("photos")
    _photo(str(work / "picnic.jpg"))
    port = _port()
    env = dict(os.environ, PHOTOBAND_HOME=str(home), PHOTOBAND_TOKEN=TOKEN, PHOTOBAND_NO_NATIVE_DIALOGS="1",
               PHOTOBAND_NO_SYSTEM_FONTS="1")
    proc = subprocess.Popen([sys.executable, "-m", "photoband", "serve", "--port", str(port), "--no-browser"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    os.makedirs(SHOTS, exist_ok=True)
    yield {"url": f"http://127.0.0.1:{port}/?t={TOKEN}", "work": str(work)}
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="module")
def page(server):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("PHOTOBAND_CHROMIUM") or None)
        pg = b.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(server["url"])
        pg.wait_for_selector("text=Caption your photos")
        pg.click("text=Open folder…")
        pw.expect(pg.locator('input[aria-label="Folder path"]')).not_to_have_value("")
        pg.fill('input[aria-label="Folder path"]', server["work"])
        pg.keyboard.press("Enter")
        choose = pg.locator("div.dialog button:has-text('Choose folder')")
        pw.expect(choose).to_be_enabled()
        choose.click()
        pg.wait_for_selector("header.tb button.save:not([disabled])", timeout=60000)
        hint = pg.locator('[role="note"][aria-label="Getting started"] button:has-text("Got it")')
        if hint.count():
            hint.click()
        pg.errors = errors
        yield pg
        b.close()


def test_edit_details_and_faces_then_save_to_original(page, server):
    pg = page
    pg.click('[role="tab"]:has-text("Metadata")')
    pw.expect(pg.locator("#d-title")).to_have_value("Old title")
    pg.fill("#d-title", "Picnic at the lake")
    pg.fill('input[aria-label="Year"]', "1952")
    pg.select_option('select[aria-label="Month"]', "6")
    pw.expect(pg.locator(".datef .pv")).to_have_text("June 1952")
    pw.expect(pg.locator(".details .bar")).to_contain_text("2 unsaved changes")

    # the faces button below the photo, then rename Ann on the photo itself
    pg.click('button[aria-label="Show faces"]')
    face = pg.locator(".faces-layer .face")
    pw.expect(face).to_have_count(1)
    face.click()
    pg.keyboard.press("Enter")
    pg.keyboard.type("Anne")
    pg.keyboard.press("Enter")
    pw.expect(pg.locator(".people input.nm").first).to_have_value("Anne")
    # N: a new face in the middle, named from the keyboard; then undo it
    pg.locator(".preview canvas").first.click(position={"x": 30, "y": 30})
    pg.keyboard.press("n")
    pg.keyboard.type("Bob")
    pg.keyboard.press("Enter")
    pw.expect(pg.locator(".faces-layer .face")).to_have_count(2)
    pg.locator(".preview canvas").first.click(position={"x": 30, "y": 30})
    pg.keyboard.press("Control+z")      # the name, then the box: one undo step each
    pw.expect(pg.locator(".faces-layer .tag.unnamed")).to_have_count(1)
    pg.keyboard.press("Control+z")
    pw.expect(pg.locator(".faces-layer .face")).to_have_count(1)
    pg.screenshot(path=os.path.join(SHOTS, "details-edited.png"))

    # a name still being typed (no Enter) is part of what the save writes
    pg.locator(".faces-layer .face").first.click()
    pg.keyboard.press("Enter")
    pg.keyboard.press("Control+a")
    pg.keyboard.type("Annie")
    save_only = pg.locator('button:has-text("Save metadata only")')
    save_only.click()
    pw.expect(pg.locator(".details .bar")).to_contain_text("No unsaved metadata changes", timeout=20000)
    pw.expect(save_only).to_be_disabled()
    # only the metadata: no captioned copy is made, and the photo shown is still this one
    assert not [f for f in os.listdir(server["work"]) if "caption" in f.lower()]
    pw.expect(pg.locator("#d-title")).to_be_visible()
    md = _md(os.path.join(server["work"], "picnic.jpg"))
    assert md["XMP-dc:Title"] == "Picnic at the lake"
    assert md["XMP-photoshop:DateCreated"] == "1952:06"
    subj = md["XMP-dc:Subject"] if isinstance(md["XMP-dc:Subject"], list) else [md["XMP-dc:Subject"]]
    assert "DATE: Y!M!" in subj and "Annie" in subj and "Ann" not in subj   # people keywords follow the rename
    names = [r.get("Name") for r in md["XMP-mwg-rs:RegionInfo"]["RegionList"]]
    assert names == ["Annie"]
    # the editor now shows the file's values, unedited
    pw.expect(pg.locator("#d-title")).to_have_value("Picnic at the lake")
    assert not pg.errors, pg.errors


def test_a_half_typed_date_blocks_saving_and_a_revert_does_not(page):
    pg = page
    pg.click('[role="tab"]:has-text("Metadata")')
    year = pg.locator('input[aria-label="Year"]')
    pw.expect(year).to_have_value("1952")
    year.fill("195")                                       # half typed: not a date yet
    pw.expect(pg.locator(".datef .warnline")).to_be_visible()
    pw.expect(pg.locator("header.tb button.save")).to_be_disabled()
    year.fill("1952")                                      # back to what the file has
    pw.expect(pg.locator(".datef .warnline")).to_have_count(0)
    pw.expect(pg.locator("header.tb button.save")).to_be_enabled()
    est = pg.locator('.datef input[type="checkbox"]')
    est.check()
    pw.expect(pg.locator(".datef .pv")).to_have_text("c. June 1952")
    est.uncheck()                                          # on and off again: no edit is left
    pw.expect(pg.locator(".datef .pv")).to_have_text("June 1952")
    pw.expect(pg.locator(".details .bar")).to_contain_text("No unsaved metadata changes")

def test_blank_parts_are_unknown_and_advanced_overrides_the_boxes(page):
    pg = page
    pg.click('[role="tab"]:has-text("Metadata")')
    year = pg.locator('input[aria-label="Year"]')
    pw.expect(year).to_have_value("1952")
    pv = pg.locator(".datef .pv")
    year.fill("")                                          # emptied: applied once the box is left
    pw.expect(pv).to_have_text("June 1952")
    year.press("Tab")                                      # the year unknown: June, then June 14
    pw.expect(pv).to_have_text("June")
    pg.fill('input[aria-label="Day"]', "14")
    pw.expect(pv).to_have_text("June 14")
    pg.click(".datef summary")
    stored, kw = pg.locator('input[aria-label="Stored date"]'), pg.locator('input[aria-label="Date keyword"]')
    pw.expect(stored).to_have_value("1952-06-14")          # the file's year, kept as the placeholder
    pw.expect(kw).to_have_value("DATE: Y?M!D!")
    kw.fill("DATE: Y!M~D!x")                               # not a keyword: marked, and saving waits
    pw.expect(pg.locator(".datef .kw.bad")).to_be_visible()
    pw.expect(pg.locator("header.tb button.save")).to_be_disabled()
    kw.fill("DATE: ")                                      # half typed: never applied, never rewritten
    kw.press("Backspace")
    pw.expect(kw).to_have_value("DATE:")
    kw.fill("DATE: Y~M!D!")                                # a known birthday, the year a guess
    kw.press("Enter")
    pw.expect(year).to_have_value("1952")
    pw.expect(pv).to_have_text("c. June 14, 1952")
    pw.expect(pg.locator(".datef input[type=checkbox]")).to_be_checked()
    pw.expect(pg.locator("header.tb button.save")).to_be_enabled()
    # the guessed year backspaced and typed again: still the year that is the guess
    year.fill("")
    year.press_sequentially("1953")
    pw.expect(pv).to_have_text("c. June 14, 1953")
    pw.expect(kw).to_have_value("DATE: Y~M!D!")
    pg.click('.details button:has-text("Clear")')
    pw.expect(year).to_have_value("")
    pw.expect(pv).to_have_count(0)
    pw.expect(pg.locator(".details .bar")).to_contain_text("1 unsaved change")
    pg.click('button[aria-label="Undo the date edit"]')
    pw.expect(year).to_have_value("1952")
    pw.expect(pg.locator(".details .bar")).to_contain_text("No unsaved metadata changes")
    assert not pg.errors, pg.errors


def test_faces_toggle_is_remembered(page):
    pg = page
    pw.expect(pg.locator('button[aria-label="Hide faces"]')).to_be_visible()
    pg.reload()
    pg.wait_for_selector("text=Caption your photos")
    assert pg.evaluate("localStorage.getItem('photoband.showFaces')") == "1"

