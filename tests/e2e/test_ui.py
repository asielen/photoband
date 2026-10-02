"""End-to-end UI tests: the real backend + the built UI in Chromium (Playwright).

Run:  cd ui && npm run build && cd .. && python -m pytest tests/e2e -q
Screenshots land in tests/_artifacts/ui/.
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

pw = pytest.importorskip("playwright.sync_api")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.path.join(ROOT, "tests", "_artifacts", "ui")
TOKEN = "e2e-token-0123456789abcdef"  # PHOTOBAND_TOKEN must be at least 16 characters


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server(tmp_path_factory, fixtures_dir):
    if not os.path.exists(os.path.join(ROOT, "ui", "dist", "index.html")):
        pytest.skip("UI not built (cd ui && npm run build)")
    home = tmp_path_factory.mktemp("e2ehome")
    work = tmp_path_factory.mktemp("photos")
    for n in os.listdir(fixtures_dir):
        if n.endswith((".tif", ".jpg", ".png")):
            shutil.copy2(os.path.join(fixtures_dir, n), work / n)
    port = _port()
    env = dict(os.environ, PHOTOBAND_HOME=str(home), PHOTOBAND_TOKEN=TOKEN, PHOTOBAND_NO_NATIVE_DIALOGS="1",
               PHOTOBAND_NO_SYSTEM_FONTS="1")
    proc = subprocess.Popen([sys.executable, "-m", "photoband", "serve", "--port", str(port), "--no-browser"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    os.makedirs(SHOTS, exist_ok=True)
    yield {"url": f"http://127.0.0.1:{port}/?t={TOKEN}", "work": str(work), "home": str(home), "port": port}
    proc.terminate()
    proc.wait(5)


@pytest.fixture(scope="module")
def page(server):
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1500, "height": 920})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.goto(server["url"])
        pg.wait_for_selector("text=Caption your photos")
        pg.errors = errors
        yield pg
        b.close()


def shot(pg, name):
    pg.screenshot(path=os.path.join(SHOTS, name + ".png"))


def open_folder(pg, folder):
    pg.click("text=Open folder…")
    pw.expect(pg.locator('input[aria-label="Folder path"]')).not_to_have_value('')  # the first listing has filled in the path
    pg.fill('input[aria-label="Folder path"]', folder)
    pg.keyboard.press("Enter")
    choose = pg.locator("div.dialog button:has-text('Choose folder')")
    for _ in range(100):  # the listing loads; Choose stays disabled until it has
        if choose.is_enabled():
            break
        pg.wait_for_timeout(100)
    err = pg.locator("div.dialog .err, div.dialog [role=alert]")
    assert choose.is_enabled(), f"folder did not open: {err.all_inner_texts() if err.count() else '(no message)'}"
    choose.click()
    pg.wait_for_selector("text=Before", timeout=20000)


def select_photo(pg, name):
    pg.get_by_role("option", name=name).click()
    pg.wait_for_timeout(1200)


def test_open_edit_save(page, server):
    pg = page
    open_folder(pg, server["work"])
    pg.wait_for_timeout(2500)
    shot(pg, "01_editor")
    # prefilled caption from metadata
    assert "Picnic at Lake Merced" in pg.inner_text(".insp")
    assert "Ann Church, Bea Ortiz and Carl Church" in pg.inner_text(".insp")
    # edit the People block → Custom
    ed = pg.locator('[aria-label="People"][contenteditable]')
    ed.click()
    pg.keyboard.press("End")
    pg.keyboard.type(" (1952)")
    pg.wait_for_timeout(400)
    assert pg.locator(".chip.custom").count() == 1
    shot(pg, "02_custom_edit")
    # save copy
    pg.keyboard.press("Control+s")
    pg.wait_for_selector("text=Saved 01_prophoto16_lzw-captioned.tif", timeout=60000)
    out = os.path.join(server["work"], "01_prophoto16_lzw-captioned.tif")   # next to the original
    assert os.path.exists(out)
    from photoband.imageio import probe
    info = probe(out)
    assert info.dtype == "uint16" and info.compression == "lzw"
    shot(pg, "03_saved")
    # saving moves on to the next photo: the next tests work on the first one
    select_photo(pg, "01_prophoto16_lzw.tif")
    assert not [e for e in pg.errors if "favicon" not in e], pg.errors


def test_template_switch_keeps_custom(page):
    pg = page
    # the saved edit went into the copy: edit the original's People line again
    ed = pg.locator('[aria-label="People"][contenteditable]')
    ed.click()
    pg.keyboard.press("End")
    pg.keyboard.type(" (1952)")
    pg.wait_for_timeout(400)
    pg.select_option('select[aria-label="Template"]', "museum-card")
    pg.wait_for_timeout(1200)
    shot(pg, "04_museum_card")
    txt = pg.inner_text(".insp")
    assert "(1952)" in txt  # custom block kept
    pg.select_option('select[aria-label="Template"]', "classic-polaroid")
    pg.wait_for_timeout(800)


def test_style_and_layout_tabs(page):
    pg = page
    pg.click("role=tab[name='Style']")
    pg.wait_for_timeout(300)
    shot(pg, "05_style_tab")
    pg.click("role=tab[name='Layout']")
    pg.wait_for_timeout(300)
    shot(pg, "06_layout_tab")
    pg.click("role=tab[name='Metadata']")
    pg.wait_for_timeout(300)
    shot(pg, "07_metadata_tab")
    pg.click("role=tab[name='Text']")


def test_faces_overlay_and_undo(page):
    pg = page
    pg.keyboard.press("Escape")
    pg.locator(".preview").click(position={"x": 200, "y": 200})
    pg.keyboard.press("f")
    pg.wait_for_timeout(400)
    shot(pg, "08_faces")
    pg.keyboard.press("f")


def test_group_rows_and_unicode(page):
    pg = page
    select_photo(pg, "06_group_three_rows.tif")
    pg.select_option('select[aria-label="Template"]', "bottom-band")
    pg.wait_for_timeout(1500)
    assert "Front row" in pg.inner_text(".insp")
    shot(pg, "09_bottom_band_rows")
    select_photo(pg, "08_unicode_names.tif")
    pg.wait_for_timeout(1500)
    assert "Наталья" in pg.inner_text(".insp")
    shot(pg, "10_unicode")


def test_case_b_recognized(page):
    pg = page
    select_photo(pg, "11_other_tool_colored_band.png")
    pg.wait_for_selector("text=Existing caption found", timeout=30000)
    shot(pg, "11_case_b_banner")
    pg.click("button:has-text('Use recognized text')")
    pg.wait_for_timeout(1500)
    shot(pg, "12_case_b_rebuild")
    assert "Rosa" in pg.inner_text(".insp")


def test_case_c_erase(page):
    pg = page
    select_photo(pg, "12_scanned_polaroid_handwriting.tif")
    pg.wait_for_selector("text=Physical caption on a scan", timeout=30000)
    pg.click("button:has-text('Replace with template')")
    pg.wait_for_timeout(3000)
    shot(pg, "13_case_c_erase")
    # overwrite is disabled for case C by default
    assert pg.locator("header.tb button.overwrite").is_disabled()


def test_case_d_flag(page):
    pg = page
    select_photo(pg, "13_date_stamp.jpg")
    pg.wait_for_selector("text=Text printed over the photo", timeout=30000)
    shot(pg, "14_case_d")


def test_blocked_formats(page):
    pg = page
    select_photo(pg, "07b_cmyk.tif")
    pg.wait_for_timeout(800)
    assert pg.locator("button:has-text('Save copy')").first.is_disabled()
    shot(pg, "15_cmyk_blocked")


def test_settings_formats(page):
    pg = page
    pg.click("button[aria-label='Settings']")
    pg.click("nav >> text=Caption formats")
    pg.wait_for_timeout(800)
    shot(pg, "16_settings_formats")
    ta = pg.locator("textarea").first
    ta.click()
    pg.keyboard.press("End")
    pg.keyboard.type(" {bogus} {da")
    pg.wait_for_timeout(500)
    shot(pg, "17_autocomplete")
    pg.keyboard.press("Escape")
    pg.click("nav >> text=Saving")
    pg.wait_for_timeout(300)
    shot(pg, "18_settings_saving")
    pg.click("nav >> text=Fonts")
    pg.wait_for_timeout(300)
    shot(pg, "19_settings_fonts")
    pg.click("button:has-text('Done')")
    # the edited caption format is unsaved: the dirty guard asks before closing
    pg.click("div.dialog button:has-text('Discard changes')")
    pg.wait_for_selector("nav >> text=Caption formats", state="detached")


def test_overwrite_with_backup(page, server):
    pg = page
    select_photo(pg, "04_mp_regions.tif")
    pg.wait_for_timeout(800)
    pg.click("header.tb button.overwrite")
    pg.wait_for_selector("text=Overwrite the original?")
    shot(pg, "20_overwrite_confirm")
    pg.click("div.dialog button:has-text('Overwrite')")
    pg.wait_for_selector("text=Overwrote the original", timeout=60000)
    assert os.path.exists(os.path.join(server["work"], "_originals", "04_mp_regions-original.tif"))
    # overwrite moves on to the next photo: go back to the overwritten one
    select_photo(pg, "04_mp_regions.tif")
    pg.wait_for_selector("text=Captioned by Photoband", timeout=30000)
    shot(pg, "21_case_a_after_overwrite")


def test_batch(page, server):
    pg = page
    pg.click("button:has-text('Batch')")
    pg.click("button:has-text('Choose folder…')")
    pw.expect(pg.locator('input[aria-label="Folder path"]')).not_to_have_value('')  # the first listing has filled in the path
    pg.fill('input[aria-label="Folder path"]', server["work"])
    pg.keyboard.press("Enter")
    choose = pg.locator("div.dialog button:has-text('Choose folder')")
    for _ in range(100):  # the listing loads; Choose stays disabled until it has
        if choose.is_enabled():
            break
        pg.wait_for_timeout(100)
    err = pg.locator("div.dialog .err, div.dialog [role=alert]")
    assert choose.is_enabled(), f"folder did not open: {err.all_inner_texts() if err.count() else '(no message)'}"
    choose.click()
    pg.wait_for_timeout(800)
    shot(pg, "22_batch_setup")
    pg.click("button:has-text('Check photos')")
    pg.wait_for_selector("text=ready", timeout=30000)
    pg.wait_for_function("() => !document.body.innerText.includes('Checking…')", timeout=180000)
    pg.wait_for_timeout(500)
    shot(pg, "23_batch_preflight")
    btn = pg.locator("button:has-text('Save ')").last
    btn.click()
    pg.click("div.dialog button.primary")
    pg.wait_for_selector(".batch[data-step=summary]", timeout=300000)
    pg.wait_for_timeout(500)
    shot(pg, "24_batch_summary")
    txt = pg.inner_text(".batch")
    import re
    saved = int(re.search(r"(\d+)\s+saved", txt).group(1))
    failed = int(re.search(r"(\d+)\s+failed", txt).group(1))
    assert saved >= 5 and failed == 0, txt
    assert not [e for e in pg.errors if "favicon" not in e], pg.errors
    # a second batch over the same folder lists the copies the first one made (they sit next to
    # the originals): it leaves every one of them alone instead of captioning a copy of a copy
    pg.click("button:has-text('New batch')")
    pg.wait_for_timeout(800)
    pg.click("button:has-text('Check photos')")
    pg.wait_for_selector("text=ready", timeout=30000)
    pg.wait_for_function("() => !document.body.innerText.includes('Checking…')", timeout=180000)
    pg.wait_for_timeout(500)
    shot(pg, "25_batch_second_run_skips_copies")
    rows = pg.locator(".prow", has_text="a captioned copy Photoband made")
    # this batch's copies and the one an earlier test saved in the same folder
    copies = [n for n in os.listdir(server["work"]) if "-captioned" in n]
    assert rows.count() == len(copies) >= saved, pg.inner_text(".batch")
    for i in range(rows.count()):
        assert "-captioned" in rows.nth(i).inner_text()
    pg.click("button:has-text('Back')")
    assert not [e for e in pg.errors if "favicon" not in e], pg.errors
