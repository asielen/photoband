"""Native window via pywebview (WebView2 on Windows, WKWebView on macOS).

If the native window cannot start (for example Windows without the WebView2
runtime), the app switches to browser mode automatically with a one-line notice.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
import webbrowser

from . import dialogs, security
from .server import APP_MODE
from .settings import load_settings, save_settings

log = logging.getLogger(__name__)

FLUSH_TIMEOUT = 3.0      # seconds the window waits for the editor's unsaved edits on close
# resolves once the editor has sent its pending edits (or right away if the page has no hook)
FLUSH_JS = ("Promise.resolve(window.__photobandFlush ? window.__photobandFlush() : null)"
            ".then(function () { return true; }, function () { return false; })")


class _QuietBackendProbe(logging.Filter):
    """pywebview logs a full traceback for every GUI backend it fails to import (GTK, Qt ...)
    before giving up; the app then opens in the browser with a one-line notice. Keep those
    tracebacks out of the console (they stay in the debug log)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "cannot be loaded" in msg:
            log.debug("pywebview: %s", msg, exc_info=record.exc_info)
            return False
        return True


def _quiet_pywebview_logger() -> None:
    lg = logging.getLogger("pywebview")
    if not any(isinstance(f, _QuietBackendProbe) for f in lg.filters):
        lg.addFilter(_QuietBackendProbe())


def flush_editor(window, timeout: float = FLUSH_TIMEOUT) -> bool:
    """Ask the page to send its pending edits and wait (at most ``timeout`` s) until it has.

    Must not run on the GUI thread: evaluate_js waits for the GUI thread to run the script.
    Returns True when the page reported that the flush finished."""
    done = threading.Event()
    result = {"ok": False}

    def cb(value):
        result["ok"] = value is True or value == "true"
        done.set()

    def run():
        try:
            window.evaluate_js(FLUSH_JS, cb)
        except Exception:
            log.debug("flushing the editor before closing failed", exc_info=True)
            done.set()

    threading.Thread(target=run, name="photoband-flush", daemon=True).start()
    if not done.wait(timeout):
        log.warning("The editor did not finish saving its last edits within %.0f s; closing anyway.", timeout)
        return False
    return result["ok"]


class CloseGuard:
    """The window's ``closing`` handler. pywebview runs it on the GUI thread and closes the
    window unless it returns False, so it cannot wait for the page itself: the first close is
    cancelled, the edits are flushed on a worker thread (bounded by FLUSH_TIMEOUT), and then the
    window is destroyed for real. The local server keeps running until webview.start() returns,
    i.e. after the window is gone, so the flush's requests reach it."""

    def __init__(self, window, flush=flush_editor, on_close=None):
        self.window = window
        self._flush = flush
        self._on_close = on_close
        self._state = "open"            # open -> flushing -> closing
        self._lock = threading.Lock()

    def __call__(self) -> bool:
        with self._lock:
            if self._state == "closing":
                return True
            if self._state == "flushing":
                return False             # a second click while the edits are being sent
            self._state = "flushing"
        if self._on_close:
            try:
                self._on_close()
            except Exception:
                log.debug("close handler failed", exc_info=True)
        threading.Thread(target=self._finish, name="photoband-close", daemon=True).start()
        return False

    def _finish(self) -> None:
        try:
            self._flush(self.window)
        except Exception:
            log.debug("flushing the editor before closing failed", exc_info=True)
        with self._lock:
            self._state = "closing"
        try:
            self.window.destroy()
        except Exception:
            log.debug("closing the window failed", exc_info=True)


def _webview2_available() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ]
        for hive, k in keys:
            try:
                with winreg.OpenKey(hive, k) as h:
                    v, _ = winreg.QueryValueEx(h, "pv")
                    if v and v != "0.0.0.0":
                        return True
            except OSError:
                continue
    except Exception:
        return True
    return False


def _browser_fallback(port: int, notice: str) -> int:
    APP_MODE["mode"] = "browser"
    APP_MODE["notice"] = notice
    url = f"http://127.0.0.1:{port}/?t={security.TOKEN}"
    # the browser's command line (visible to other local users) only carries a one-time nonce
    webbrowser.open(security.launch_url(port))
    print(notice, url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def _window_size(s) -> tuple:
    """The saved window size, or the default for anything unusable."""
    win = (s.get("session") or {}).get("window") if isinstance(s, dict) else None
    win = win if isinstance(win, dict) else {}
    out = []
    for k, d in (("w", 1440), ("h", 900)):
        v = win.get(k, d)
        out.append(int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and 200 <= v <= 100000 else d)
    return tuple(out)


def run_desktop() -> int:
    from .__main__ import StartError, start_server
    try:
        server, port = start_server()
    except StartError as e:
        # never show or open a URL carrying the token unless our own server is serving it
        print(e, file=sys.stderr, flush=True)
        return 1
    url = f"http://127.0.0.1:{port}/?t={security.TOKEN}"
    if not _webview2_available():
        return _browser_fallback(port, "Microsoft Edge WebView2 is not installed, so Photoband opened in your browser.")
    try:
        import webview  # pywebview
    except Exception:
        return _browser_fallback(port, "The native window is unavailable, so Photoband opened in your browser.")
    APP_MODE["mode"] = "desktop"
    s = load_settings()
    w, h = _window_size(s)
    window = webview.create_window("Photoband", url, width=w, height=h, min_size=(960, 640),
                                   background_color="#1e1e1e", text_select=False)

    def provider(kind, title="", initial="", filetypes=None, save_name=""):
        FOLDER = getattr(webview, "FOLDER_DIALOG", 20)
        OPEN = getattr(webview, "OPEN_DIALOG", 10)
        SAVE = getattr(webview, "SAVE_DIALOG", 30)
        if kind == "open-folder":
            r = window.create_file_dialog(FOLDER, directory=initial or "")
        elif kind == "save-file":
            r = window.create_file_dialog(SAVE, directory=initial or "", save_filename=save_name or "")
        elif kind == "open-font":
            r = window.create_file_dialog(OPEN, file_types=("Fonts (*.ttf;*.otf)",))
        elif kind == "exiftool":
            r = window.create_file_dialog(OPEN, allow_multiple=False)
        else:
            r = window.create_file_dialog(OPEN, allow_multiple=True, directory=initial or "",
                                          file_types=("Images (*.tif;*.tiff;*.jpg;*.jpeg;*.png)", "All files (*.*)"))
        if not r:
            return None
        return [r] if isinstance(r, str) else list(r)

    dialogs.set_provider(provider)

    def on_loaded():
        # Drag and drop: pywebview exposes full paths of dropped files.
        try:
            from webview.dom import DOMEventHandler

            def on_drop(e):
                files = e.get("dataTransfer", {}).get("files", [])
                ps = [f.get("pywebviewFullPath") for f in files if f.get("pywebviewFullPath")]
                if ps:
                    # a drop is a user gesture in the native window: grant like a dialog result
                    security.allow(ps, from_dialog=True)
                    for p in ps:
                        if not os.path.isdir(p):
                            security.allow_root(os.path.dirname(p), from_dialog=True)
                    import json
                    window.evaluate_js(f"window.__photobandDrop && window.__photobandDrop({json.dumps(ps)})")

            window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)
        except Exception:
            log.debug("drag and drop is unavailable", exc_info=True)

    def remember_size():
        save_settings({"session": {"window": {"w": window.width, "h": window.height}}})

    window.events.loaded += on_loaded
    window.events.closing += CloseGuard(window, on_close=remember_size)
    _quiet_pywebview_logger()
    try:
        webview.start(private_mode=False)
    except Exception as e:
        return _browser_fallback(port, f"The native window could not start ({e}), so Photoband opened in your browser.")
    return 0
