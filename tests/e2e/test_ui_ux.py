"""UX regressions: tooltips (shortcut chips, reasons on disabled controls), the command palette,
progressive disclosure in the Inspector and the first-run hint.

Run:  cd ui && npm run build && cd .. && python -m pytest tests/e2e/test_ui_ux.py -q
Screenshots land in tests/_artifacts/ui_ux/ (or $PHOTOBAND_SHOTS).
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
SHOTS = os.environ.get("PHOTOBAND_SHOTS") or os.path.join(ROOT, "tests", "_artifacts", "ui_ux")
TOKEN = "e2e-token-0123456789abcdef"  # PHOTOBAND_TOKEN must be at least 16 characters
W, H = 1440, 900


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
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def page(server, browser):
    ctx = browser.new_context(viewport={"width": W, "height": H})
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(server["url"])
    pg.wait_for_selector("text=Caption your photos")
    pg.errors = errors
    yield pg
    ctx.close()


def shot(pg, name):
    pg.screenshot(path=os.path.join(SHOTS, name + ".png"))


def open_folder(pg, folder):
    pg.click("text=Open folder…")
    pw.expect(pg.locator('input[aria-label="Folder path"]')).not_to_have_value('')  # the first listing has filled in the path
    pg.fill('input[aria-label="Folder path"]', folder)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(300)
    pg.click("div.dialog button:has-text('Choose folder')")
    pg.wait_for_selector("text=Before", timeout=20000)
    # the existing-caption check has finished when Save copy turns on
    pg.wait_for_selector("header.tb button.save:not([disabled])", timeout=30000)


def reopen(pg):
    """After a reload: the welcome screen's Reopen button opens the last folder again."""
    pg.wait_for_selector("text=Caption your photos")
    pg.click(".reopen button")
    pg.wait_for_selector("text=Before", timeout=20000)
    pg.wait_for_selector("header.tb button.save:not([disabled])", timeout=30000)


def select_photo(pg, name):
    pg.get_by_role("option", name=name).click()
    pg.wait_for_timeout(1200)


def tip_for(pg, selector):
    """Hover a control (enabled or not) and return the visible tooltip's text and key chip."""
    pg.mouse.move(1, 1)
    pg.wait_for_timeout(700)  # past the "warm" window, so the next tip waits for its delay
    box = pg.locator(selector).first.bounding_box()
    pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    pg.wait_for_selector(".pb-tip[data-show]", timeout=3000)
    text = pg.inner_text(".pb-tip .pb-tip-text")
    key = pg.locator(".pb-tip kbd")
    return text, (key.inner_text() if key.count() else "")


def test_first_run_hint_dismisses_and_stays_dismissed(page, server):
    pg = page
    open_folder(pg, server["work"])
    coach = pg.locator('[role="note"][aria-label="Getting started"]')
    assert coach.is_visible()
    assert "Save copy" in coach.inner_text()
    shot(pg, "01_coach")
    coach.locator("button:has-text('Got it')").click()
    assert coach.count() == 0
    # after a restart (reload) and opening photos again, it stays away
    pg.reload()
    reopen(pg)
    pg.wait_for_timeout(500)
    assert pg.locator('[role="note"][aria-label="Getting started"]').count() == 0


def test_save_copy_tooltip_shows_shortcut(page):
    pg = page
    text, key = tip_for(pg, "header.tb button.save")
    assert "Save a captioned copy" in text
    assert key in ("Ctrl+S", "⌘S"), key
    shot(pg, "02_tip_save")


def test_disabled_control_explains_why(page, server):
    pg = page
    undo = pg.locator('header.tb button[aria-label="Undo"]')
    assert undo.is_disabled()
    text, key = tip_for(pg, 'header.tb button[aria-label="Undo"]')
    assert "Nothing to undo" in text
    assert key in ("Ctrl+Z", "⌘Z"), key
    # a scan with a handwritten caption: Overwrite is off, and its tooltip says why
    select_photo(pg, "12_scanned_polaroid_handwriting.tif")
    pg.wait_for_selector("text=Handwriting or printing on the photo", timeout=30000)
    over = pg.locator('header.tb button.overwrite')
    assert over.is_disabled()
    text, _ = tip_for(pg, 'header.tb button.overwrite')
    assert "overwriting it is turned off" in text
    shot(pg, "03_tip_disabled_overwrite")
    select_photo(pg, "01_prophoto16_lzw.tif")


def test_command_palette(page):
    pg = page
    zoom = pg.locator(".status .zoom")
    fit_text = zoom.inner_text()
    pg.click('.status button[aria-label="Zoom in"]')
    pg.click('.status button[aria-label="Zoom in"]')
    assert zoom.inner_text() != fit_text
    # focus somewhere known, so we can check it comes back
    pg.locator(".strip button.cur").focus()
    pg.keyboard.press("ControlOrMeta+k")
    dlg = pg.get_by_role("dialog", name="Commands")
    dlg.wait_for()
    total = dlg.get_by_role("option").count()
    assert total > 20
    # disabled commands say why, with the toolbar's reasons
    pg.keyboard.type("undo")
    assert "Nothing to undo" in dlg.get_by_role("option").first.inner_text()
    pg.keyboard.press("ControlOrMeta+a")
    pg.keyboard.type("zoom fit")
    opts = dlg.get_by_role("option")
    assert 0 < opts.count() < total
    assert "Zoom to fit" in opts.first.inner_text()
    shot(pg, "04_palette_zoom")
    pg.keyboard.press("Enter")
    dlg.wait_for(state="detached")
    pg.wait_for_timeout(300)
    assert zoom.inner_text() == fit_text
    assert pg.evaluate("() => document.activeElement && document.activeElement.getAttribute('role')") == "option"
    # Esc closes it; arrows move the selection
    pg.keyboard.press("ControlOrMeta+k")
    dlg.wait_for()
    pg.keyboard.press("ArrowDown")
    assert dlg.locator("[role=option][aria-selected=true]").count() == 1
    pg.keyboard.press("Escape")
    dlg.wait_for(state="detached")


def test_style_more_options_hidden_until_opened_and_remembered(page, server):
    pg = page
    pg.click("role=tab[name='Style']")
    pg.wait_for_timeout(300)
    insp = pg.locator(".insp")
    assert insp.locator('select[aria-label="Font"]').is_visible()
    assert insp.locator('input[aria-label="Letter spacing"]').count() == 0
    more = insp.locator("button.disclosure", has_text="More options")
    assert more.get_attribute("aria-expanded") == "false"
    more.click()
    assert more.get_attribute("aria-expanded") == "true"
    assert insp.locator('input[aria-label="Letter spacing"]').is_visible()
    shot(pg, "05_style_more")
    # remembered across a restart
    pg.reload()
    reopen(pg)
    pg.click("role=tab[name='Style']")
    pg.wait_for_timeout(300)
    assert pg.locator('.insp input[aria-label="Letter spacing"]').is_visible()
    pg.locator(".insp button.disclosure", has_text="More options").click()
    assert pg.locator('.insp input[aria-label="Letter spacing"]').count() == 0
    pg.click("role=tab[name='Text']")
    assert not pg.errors, pg.errors


def test_warning_about_a_hidden_control_opens_its_disclosure(page):
    """Text that overflows the fixed Polaroid band warns; the Layout tab's "More options" (where
    the overflow behaviour lives) opens by itself, and the warning links straight to it."""
    pg = page
    assert pg.locator("select[aria-label='Template']").input_value() == "classic-polaroid"
    pg.click("role=tab[name='Layout']")
    more = pg.locator(".insp button.disclosure", has_text="More options")
    assert more.get_attribute("aria-expanded") == "false"
    pg.click("role=tab[name='Text']")
    ed = pg.locator('[aria-label="People"][contenteditable]')
    ed.click()
    pg.keyboard.press("End")
    for _ in range(14):
        pg.keyboard.press("Enter")
        pg.keyboard.type("A very long line of names that keeps going")
    pg.wait_for_selector("text=overflows", timeout=10000)
    link = pg.locator("[role=status] button", has_text="Band settings")
    assert link.count() == 1
    link.click()
    pg.wait_for_timeout(300)
    assert pg.locator(".insp [role=tab][aria-selected=true]").inner_text() == "Layout"
    assert more.get_attribute("aria-expanded") == "true"
    assert pg.locator(".insp [role=radiogroup][aria-label='Overflow']").is_visible()
    shot(pg, "06_warning_reveals")
    # put the photo back
    pg.click("role=tab[name='Text']")
    pg.click(".insp button:has-text('Revert to template')")
    pg.click("div.dialog button:has-text('Revert')")
    pg.wait_for_timeout(500)
    assert pg.locator("text=overflows").count() == 0
    assert not pg.errors, pg.errors
