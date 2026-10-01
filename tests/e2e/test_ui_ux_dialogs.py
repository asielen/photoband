"""End-to-end checks for the secondary surfaces' UX: Settings tooltips and the "Show tooltips"
switch, the guided batch steps, and Help › Getting started.

Run:  cd ui && npm run build && cd .. && python -m pytest tests/e2e/test_ui_ux_dialogs.py -q
Screenshots land in tests/_artifacts/ui_ux_dialogs/.
"""
import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOTS = os.path.join(ROOT, "tests", "_artifacts", "ui_ux_dialogs")
TOKEN = "e2e-token-uxdialogs-0123456789"
W, H = 960, 640
TIP = ".pb-tip[data-show]"


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def server(tmp_path_factory, fixtures_dir):
    dist = os.environ.get("PHOTOBAND_UI_DIST") or os.path.join(ROOT, "ui", "dist")
    if not os.path.exists(os.path.join(dist, "index.html")):
        pytest.skip("UI not built (cd ui && npm run build)")
    home = tmp_path_factory.mktemp("uxhome")
    work = tmp_path_factory.mktemp("uxphotos")
    # a few quick photos are enough for the batch steps
    for n in ("01_prophoto16_lzw.tif", "03_rotated6_mwg.jpg", "09_partial_date.jpg"):
        shutil.copy2(os.path.join(fixtures_dir, n), work / n)
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
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": W, "height": H})
        pg = ctx.new_page()
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


def park_pointer(pg):
    """Move the pointer to an empty corner and let any tooltip go away."""
    pg.mouse.move(2, H - 2)
    pg.wait_for_timeout(700)


def open_settings(pg, tab="General"):
    pg.click("button[aria-label='Settings']")
    pg.wait_for_selector("div.dialog[aria-label='Settings']")
    pg.click(f"div.dialog nav button:has-text('{tab}')")
    pg.wait_for_timeout(200)


def close_dialog(pg):
    pg.keyboard.press("Escape")
    pg.wait_for_selector("div.dialog", state="detached")


def test_settings_tooltip_appears(page):
    pg = page
    open_settings(pg, "General")
    park_pointer(pg)
    sel = pg.locator("div.dialog select[aria-label='Default template']")
    sel.hover()
    pg.wait_for_selector(TIP, timeout=3000)
    text = pg.inner_text(TIP)
    assert "New photos" in text or "new photos" in text, text
    # the control is linked to the tooltip for screen readers
    assert "pb-tip" in (sel.get_attribute("aria-describedby") or "")
    shot(pg, "01_settings_tooltip")
    # every setting on the tab says what it changes
    assert pg.locator("div.dialog .content .desc").count() >= 3
    close_dialog(pg)


def test_show_tooltips_switch_turns_them_off(page):
    pg = page
    open_settings(pg, "General")
    switch = pg.locator("div.dialog input[role=switch]")
    assert switch.is_checked()
    switch.click(force=True)  # the input is visually hidden behind the styled track
    pg.wait_for_function("() => !document.querySelector('div.dialog input[role=switch]').checked")
    pg.wait_for_timeout(300)
    park_pointer(pg)
    pg.locator("div.dialog select[aria-label='Default template']").hover()
    pg.wait_for_timeout(1200)
    assert pg.locator(TIP).count() == 0
    shot(pg, "02_tooltips_off")
    # the choice is saved
    close_dialog(pg)
    open_settings(pg, "General")
    assert not pg.locator("div.dialog input[role=switch]").is_checked()
    # and back on
    pg.locator("div.dialog input[role=switch]").click(force=True)
    pg.wait_for_function("() => document.querySelector('div.dialog input[role=switch]').checked")
    pg.wait_for_timeout(300)
    park_pointer(pg)
    pg.locator("div.dialog select[aria-label='Default template']").hover()
    pg.wait_for_selector(TIP, timeout=3000)
    close_dialog(pg)


def test_advanced_settings_are_one_step_away(page):
    pg = page
    open_settings(pg, "Saving")
    # the rare options are behind "Advanced", not gone
    assert pg.locator("div.dialog select[aria-label='Output format']").count() == 0
    adv = pg.locator("div.dialog button.disclosure:has-text('Advanced')")
    assert adv.get_attribute("aria-expanded") == "false"
    adv.click()
    assert pg.locator("div.dialog select[aria-label='Output format']").count() == 1
    close_dialog(pg)
    # the open state is remembered
    open_settings(pg, "Saving")
    assert pg.locator("div.dialog button.disclosure:has-text('Advanced')").get_attribute("aria-expanded") == "true"
    close_dialog(pg)


def _current_step(pg):
    return pg.inner_text(".batch [aria-current=step]").strip()


def test_batch_step_indicator_advances(page, server):
    pg = page
    pg.click("header.tb button:has-text('Batch')")
    pg.wait_for_selector(".batch")
    assert "Choose photos" in _current_step(pg)
    # Check photos explains why it is unavailable before a folder is chosen
    check = pg.locator(".batch button:has-text('Check photos')")
    assert check.is_disabled()
    assert "folder" in (check.get_attribute("data-tip") or "").lower()
    pg.click(".batch button:has-text('Choose folder…')")
    pw.expect(pg.locator('input[aria-label="Folder path"]')).not_to_have_value('')  # the first listing has filled in the path
    pg.fill("div.dialog input[aria-label='Folder path']", server["work"])
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)
    pg.click("div.dialog button:has-text('Choose folder')")
    pg.wait_for_selector(".batch :text('3 photos')")
    shot(pg, "03_batch_choose")
    check.click()
    pg.wait_for_selector(".batch[data-step=check]")
    assert "Check" in _current_step(pg)
    pg.wait_for_function("() => /photos? checked/.test(document.querySelector('.batch h3')?.innerText || '')", timeout=180000)
    shot(pg, "04_batch_check")
    pg.locator(".batch .actions button.primary").click()
    pg.wait_for_selector("div.dialog")
    pg.locator("div.dialog footer button.primary").click()
    pg.wait_for_selector(".batch[data-step=save], .batch[data-step=summary]")
    assert "Save" in _current_step(pg)
    pg.wait_for_selector(".batch[data-step=summary]", timeout=300000)
    # finished: every step shows as done, and the summary says what happened in words
    assert pg.locator(".batch .stepper .step.past").count() == 3
    txt = pg.inner_text(".batch")
    assert "Your originals were not changed" in txt, txt
    shot(pg, "05_batch_summary")
    pg.click(".batch .actions button:has-text('Done')")
    pg.wait_for_selector(".batch", state="hidden")


def test_help_getting_started(page):
    pg = page
    pg.evaluate("() => document.activeElement && document.activeElement.blur()")
    pg.keyboard.press("F1")
    pg.wait_for_selector("div.dialog[aria-label='Keyboard shortcuts']")
    assert "Command palette" in pg.inner_text("div.dialog")  # Mod+K is listed
    pg.click("div.dialog [role=tab]:has-text('Getting started')")
    pg.wait_for_selector("div.dialog[aria-label='Getting started']")
    assert pg.locator("div.dialog [role=tab][aria-selected=true]").inner_text().strip() == "Getting started"
    steps = pg.locator("div.dialog ol.start > li")
    assert 3 <= steps.count() <= 5
    assert "Save a copy" in pg.inner_text("div.dialog")
    # arrow keys move between help pages
    pg.locator("div.dialog [role=tab][aria-selected=true]").focus()
    pg.keyboard.press("ArrowRight")
    pg.wait_for_selector("div.dialog[aria-label='Keyboard shortcuts']")
    pg.keyboard.press("ArrowLeft")
    pg.wait_for_selector("div.dialog[aria-label='Getting started']")
    shot(pg, "06_help_getting_started")
    close_dialog(pg)
    assert not [e for e in pg.errors if "favicon" not in e], pg.errors
