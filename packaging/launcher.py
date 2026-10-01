"""PyInstaller entry point for the frozen Photoband app.

photoband/__main__.py uses relative imports, so it can't be the frozen script itself
("attempted relative import with no known parent package"). This launcher imports it as
part of the ``photoband`` package and adds what a windowed app needs:

* With ``console=False`` (Windows) sys.stdout/sys.stderr are None; they are pointed at
  ``<app data>/logs/console.log`` before anything (uvicorn included) touches them.
* Logging goes to ``<app data>/logs/photoband.log`` (photoband.logsetup).
* An exception that escapes startup is written to the log and, on Windows and macOS,
  shown in a native message box, since there is no console to read it from.
"""
from __future__ import annotations

import multiprocessing
import os
import subprocess
import sys
import traceback


def _show_error(title: str, text: str) -> None:
    """A native message box. Never raises."""
    try:
        if sys.platform == "win32":
            import ctypes
            MB_OK, MB_ICONERROR, MB_SETFOREGROUND = 0x0, 0x10, 0x10000
            ctypes.windll.user32.MessageBoxW(None, text, title, MB_OK | MB_ICONERROR | MB_SETFOREGROUND)
        elif sys.platform == "darwin":
            # The message travels as argv, never inside the AppleScript source, so quotes or
            # backslashes in it can't change the script.
            script = ('on run argv\n'
                      '  display alert (item 1 of argv) message (item 2 of argv) as critical buttons {"OK"}\n'
                      'end run')
            subprocess.run(["/usr/bin/osascript", "-e", script, title, text],
                           timeout=600, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _report_crash(exc: BaseException) -> None:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log_file = None
    try:
        import logging

        from photoband import logsetup
        log_file = logsetup.setup()
        logging.getLogger("photoband.launcher").critical("Photoband failed to start:\n%s", tb)
    except Exception:
        pass
    if not log_file:
        try:
            from photoband import logsetup
            log_file = os.path.join(logsetup.logs_dir(), "crash.log")
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(tb + "\n")
        except Exception:
            log_file = None
    try:
        print(tb, file=sys.stderr, flush=True)
    except Exception:
        pass
    where = f"\n\nDetails were written to:\n{log_file}" if log_file else ""
    _show_error("Photoband could not start", f"{type(exc).__name__}: {exc}"[:1500] + where)


def run() -> int:
    multiprocessing.freeze_support()  # batch workers are spawned from this same executable
    try:
        from photoband import logsetup
        logsetup.redirect_std_streams()
        no_console = logsetup.streams_redirected()
        log_file = logsetup.setup()
        from photoband.__main__ import main
        rc = int(main() or 0)
        if rc and no_console:
            # main() reports start failures on stderr, which nobody sees in a windowed app
            where = os.path.join(logsetup.logs_dir(), "console.log")
            _show_error("Photoband could not start",
                        f"Photoband stopped with an error (code {rc}).\n\nDetails were written to:\n{where}"
                        + (f"\nand {log_file}" if log_file else ""))
        return rc
    except SystemExit as e:  # argparse --help/--version, explicit exits
        code = e.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except KeyboardInterrupt:
        return 130
    except BaseException as e:  # noqa: BLE001 - last-resort crash report
        _report_crash(e)
        return 1


if __name__ == "__main__":
    sys.exit(run())
