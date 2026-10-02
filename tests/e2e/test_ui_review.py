"""UI review regressions at the 960 x 640 minimum window (photoband/desktop.py).

Covers: no horizontal overflow in the Style/Layout inspector tabs, toolbar labels that stay visible,
Tab never hiding the side panels (Ctrl+\\ and a status-bar button do), focus on the filmstrip after a
folder opens, the Help shortcut list, no After-only preview mode, toasts never covering dialogs,
the file browser after a bad path, plain batch wording and shaped filmstrip status badges.

Run:  cd ui && npm run build && cd .. && python -m pytest tests/e2e/test_ui_review.py -q
Screenshots land in tests/_artifacts/ui_review/ (or $PHOTOBAND_SHOTS).
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
SHOTS = os.environ.get("PHOTOBAND_SHOTS") or os.path.join(ROOT, "tests", "_artifacts", "ui_review")
TOKEN = "e2e-token-0123456789abcdef"  # PHOTOBAND_TOKEN must be at least 16 characters
W, H = 960, 640


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
    pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
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
    pg.wait_for_timeout(400)
    pg.click("div.dialog button:has-text('Choose folder')")
    pg.wait_for_selector("text=Before", timeout=20000)


def overflow_report(pg, root):
    """Controls inside `root` that stick out past its right edge, plus the scroll overflow of its body."""
    return pg.evaluate(
        """(sel) => {
          const root = document.querySelector(sel)
          const r = root.getBoundingClientRect()
          const body = root.querySelector('.body')
          const bad = []
          for (const el of root.querySelectorAll('input, select, button, [role=radio], .seg')) {
            const b = el.getBoundingClientRect()
            if (!b.width) continue
            if (b.right > r.right + 1 || b.left < r.left - 1) bad.push((el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 40) + ' @' + Math.round(b.right))
          }
          return { bad, scroll: body ? body.scrollWidth - body.clientWidth : 0, right: r.right }
        }""",
        root,
    )


def test_welcome_explains_the_app(page):
    txt = page.inner_text(".welcome")
    assert "caption band" in txt
    shot(page, "00_welcome_960")


def test_open_focuses_filmstrip(page, server):
    pg = page
    open_folder(pg, server["work"])
    pg.wait_for_timeout(1500)
    role = pg.evaluate("() => document.activeElement && document.activeElement.getAttribute('role')")
    assert role == "option", "after a folder opens, the filmstrip's current photo has focus"
    shot(pg, "01_editor_960")


def test_toolbar_labels_at_960(page):
    pg = page
    for label in ("Save copy", "Overwrite", "Batch"):
        loc = pg.locator(f"header.tb button:visible:has-text('{label}')")
        assert loc.count() >= 1, label
        assert loc.first.bounding_box()["x"] + loc.first.bounding_box()["width"] <= W, label
    # the visible Overwrite text is a label, not a lone (download-looking) icon
    assert pg.locator("header.tb .lbl.short:visible", has_text="Overwrite").count() == 1
    # nothing in the toolbar is cut off at the minimum width
    clipped = pg.evaluate(
        """() => [...document.querySelectorAll('header.tb button, header.tb select')]
               .filter((b) => b.offsetParent && b.getBoundingClientRect().right > window.innerWidth)
               .map((b) => b.getAttribute('aria-label') || b.textContent.trim())"""
    )
    assert not clipped, clipped


@pytest.mark.parametrize("tab", ["Style", "Layout"])
def test_inspector_tabs_fit_at_960(page, tab):
    pg = page
    pg.click(f"role=tab[name='{tab}']")
    pg.wait_for_timeout(400)
    rep = overflow_report(pg, ".insp")
    assert rep["scroll"] <= 0, rep
    assert not rep["bad"], rep
    assert rep["right"] <= W
    shot(pg, f"02_{tab}_960")
    pg.click("role=tab[name='Text']")


def test_tab_never_hides_panels(page):
    pg = page
    # nothing focused: Tab moves focus into the window instead of hiding the panels
    pg.evaluate("() => document.activeElement && document.activeElement.blur()")
    pg.keyboard.press("Tab")
    pg.wait_for_timeout(200)
    assert pg.locator(".strip").count() == 1 and pg.locator(".insp").count() == 1
    assert pg.evaluate("() => document.activeElement !== document.body")
    # from the filmstrip, Tab moves on (focus leaves the strip, panels stay)
    pg.locator(".strip button.cur").focus()
    pg.keyboard.press("Tab")
    pg.wait_for_timeout(200)
    assert pg.locator(".strip").count() == 1 and pg.locator(".insp").count() == 1


def test_panels_toggle_shortcut_and_button(page):
    pg = page
    pg.locator(".strip button.cur").focus()
    pg.keyboard.press("ControlOrMeta+Backslash")
    pg.wait_for_timeout(300)
    assert pg.locator(".strip").count() == 0 and pg.locator(".insp").count() == 0
    btn = pg.locator("button[aria-label='Show side panels']")
    assert btn.is_visible()
    shot(pg, "03_panels_hidden_960")
    btn.click()
    pg.wait_for_timeout(300)
    assert pg.locator(".strip").count() == 1 and pg.locator(".insp").count() == 1
    assert pg.locator("button[aria-label='Hide side panels']").is_visible()


def test_e_from_filmstrip_focuses_first_block(page):
    pg = page
    pg.locator(".strip button.cur").focus()
    pg.keyboard.press("e")
    pg.wait_for_timeout(400)
    assert pg.evaluate("() => !!document.activeElement && document.activeElement.isContentEditable")
    pg.keyboard.press("Escape")


def test_no_after_only_mode_and_size_readout(page):
    pg = page
    assert pg.locator("text=After only").count() == 0
    assert pg.locator(".status button[aria-label='Side by side']").count() == 1
    assert pg.locator(".status button[aria-label='Stacked']").count() == 1
    # the size readout is fully visible (px and print size) at 960
    size = pg.locator(".status .size")
    assert "px" in size.inner_text()
    b = size.bounding_box()
    sb = pg.locator(".status").bounding_box()
    assert b["x"] + b["width"] <= sb["x"] + sb["width"] + 1
    # Y switches between the two arrangements only
    pg.locator(".strip button.cur").focus()
    seen = set()
    for _ in range(4):
        pg.keyboard.press("y")
        pg.wait_for_timeout(150)
        seen.add(pg.locator(".status .seg button[aria-pressed='true']").get_attribute("aria-label"))
        assert pg.locator(".pane").count() == 2 and all(pg.locator(".pane").nth(i).is_visible() for i in range(2))
    assert seen == {"Side by side", "Stacked"}


def test_old_after_only_setting_falls_back(server, browser):
    ctx = browser.new_context(viewport={"width": W, "height": H})
    pg = ctx.new_page()
    pg.goto(server["url"])
    pg.wait_for_selector("text=Caption your photos")
    pg.evaluate("() => localStorage.setItem('photoband.previewLayout', 'after')")
    pg.reload()
    pg.wait_for_selector("text=Caption your photos")
    open_folder(pg, server["work"])
    pg.wait_for_timeout(1200)
    assert pg.locator(".pane").count() == 2
    assert pg.locator(".pane").nth(0).is_visible() and pg.locator(".pane").nth(1).is_visible()
    assert pg.locator(".status button[aria-label='Side by side']").get_attribute("aria-pressed") == "true"
    ctx.close()


def test_help_lists_all_shortcuts(page):
    pg = page
    pg.evaluate("() => document.activeElement && document.activeElement.blur()")
    pg.keyboard.press("F1")
    pg.wait_for_selector("div.dialog[aria-label='Keyboard shortcuts']")
    txt = pg.inner_text("div.dialog")
    for want in ("Switch Before / After", "First / last photo", "Five photos back", "first caption block",
                 "10 px", "Hide or show the photo list", "Home", "End", "PageUp", "PageDown", "Y", "E"):
        assert want in txt, want
    assert "\\" in txt  # Ctrl+\ for the panels
    shot(pg, "04_help_960")
    pg.keyboard.press("Escape")


def test_toasts_do_not_cover_dialogs(page):
    pg = page
    # in browser mode, dropping shows an info toast
    pg.dispatch_event(".shell", "drop")
    pg.wait_for_selector(".toast")
    pg.click("button[aria-label='Settings']")
    pg.wait_for_selector("div.dialog")
    pg.wait_for_timeout(300)
    t = pg.locator(".toast").first.bounding_box()
    d = pg.locator("div.dialog").first.bounding_box()
    assert t["y"] + t["height"] <= d["y"] + 1, (t, d)  # above the dialog, not over its footer
    shot(pg, "05_toast_with_dialog_960")
    pg.keyboard.press("Escape")
    pg.wait_for_selector("div.dialog", state="detached")
    for b in pg.locator(".toast button[aria-label='Dismiss']").all():
        b.click()


def test_file_browser_bad_path(page, server):
    pg = page
    pg.click("header.tb button:has-text('Open')")
    pg.click("text=Open folder…")
    pg.wait_for_selector("div.dialog input[aria-label='Folder path']")
    pg.wait_for_timeout(400)
    assert pg.locator("div.dialog [aria-label='Places'] button:has-text('Home')").count() == 1
    pw.expect(pg.locator('input[aria-label="Folder path"]')).not_to_have_value('')  # the first listing has filled in the path
    pg.fill('input[aria-label="Folder path"]', os.path.join(server["work"], "no-such-folder"))
    pg.keyboard.press("Enter")
    pg.wait_for_selector("div.dialog .err")
    pg.wait_for_timeout(200)
    assert pg.locator("div.dialog .list .item").count() == 0  # the old listing is not left under the error
    assert pg.locator("div.dialog button:has-text('Choose folder')").is_disabled()
    shot(pg, "06_filebrowser_badpath_960")
    pg.click("div.dialog .err button:has-text('Back to')")
    pg.wait_for_timeout(500)
    assert pg.locator("div.dialog .err").count() == 0
    assert not pg.locator("div.dialog button:has-text('Choose folder')").is_disabled()
    pg.keyboard.press("Escape")
    pg.wait_for_selector("div.dialog", state="detached")


def test_filmstrip_badge_is_a_shape(page):
    pg = page
    ed = pg.locator('[aria-label="People"][contenteditable]')
    ed.click()
    pg.keyboard.press("End")
    pg.keyboard.type(" x")
    pg.wait_for_timeout(500)
    badge = pg.locator(".strip button.cur .badge")
    assert badge.get_attribute("aria-label") == "Unsaved edits"
    assert badge.locator("svg").count() == 1
    shot(pg, "08_badge_draft_960")
    pg.keyboard.press("ControlOrMeta+z")
    pg.keyboard.press("Escape")


def test_batch_wording(page):
    pg = page
    pg.click("header.tb button:has-text('Batch')")
    pg.wait_for_selector(".batch [aria-current=step]:has-text('Choose photos')")
    # the existing-caption choices live under "More options"
    more = pg.locator(".batch button.disclosure:has-text('More options')")
    if more.get_attribute("aria-expanded") != "true":
        more.click()
    txt = pg.inner_text(".batch")
    assert "Check photos" in txt and "pre-flight" not in txt.lower()
    for helper in ("Captions this app made earlier", "Caption bands added by another app", "Handwriting or printing on a print’s border"):
        assert helper in txt, helper
    shot(pg, "07_batch_setup_960")
    pg.click("button:has-text('Back to editor')")
    # the bad-path test logs one expected 404 from /api/fs/list
    assert not [e for e in pg.errors if "favicon" not in e and "404" not in e], pg.errors


def test_case_c_ignore_keeps_overwrite_off(page):
    """A scan with a handwritten caption can't be overwritten after "Ignore" (a new band below the
    handwriting) either: the server refuses it in every mode, so the button is off with the reason."""
    pg = page
    pg.get_by_role("option", name="12_scanned_polaroid_handwriting.tif").click()
    pg.wait_for_selector("text=Handwriting or printing on the photo", timeout=30000)
    pg.click("button:has-text('Keep the writing')")
    pg.wait_for_timeout(500)
    btn = pg.locator('header.tb button.overwrite')
    assert btn.is_disabled()
    assert "overwriting it is turned off" in (btn.get_attribute("data-tip") or "")
