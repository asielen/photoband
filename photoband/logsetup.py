"""Logging for the app: a rotating ``photoband.log`` in the app's logs folder.

* ``setup()`` is called once, early, by ``photoband.__main__.main()`` and by the frozen
  launcher. INFO by default, DEBUG with ``PHOTOBAND_DEBUG=1``.
* Warnings and errors also go to the console when there is a real one.
* ``UVICORN_LOG_CONFIG`` makes uvicorn's loggers (including "Exception in ASGI application"
  tracebacks from request handlers) propagate into the same file. It needs no TTY, unlike
  uvicorn's default config, which crashes when ``sys.stdout`` is None (windowed apps).
* ``redirect_std_streams()`` gives a windowed (no console) build somewhere to put
  ``print()`` output and stray tracebacks.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

LOG_NAME = "photoband.log"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 3
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(message)s"

UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "uvicorn": {"handlers": [], "propagate": True},
        "uvicorn.error": {"handlers": [], "propagate": True},
        "uvicorn.access": {"handlers": [], "propagate": True},
    },
}

_configured = False
_streams_redirected = False


def logs_dir() -> str:
    from . import paths
    return paths.sub("logs")


def log_path() -> str:
    return os.path.join(logs_dir(), LOG_NAME)


def debug_enabled() -> bool:
    return (os.environ.get("PHOTOBAND_DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")


def setup(console: bool | None = None) -> str | None:
    """Attach the rotating file handler to the root logger (idempotent). Returns the log
    file path, or None when the logs folder can't be written (logging then stays console-only)."""
    global _configured
    root = logging.getLogger()
    level = logging.DEBUG if debug_enabled() else logging.INFO
    if _configured:
        return log_path()
    _configured = True
    root.setLevel(level)
    path: str | None
    try:
        path = log_path()
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                  encoding="utf-8", delay=True)
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(fh)
    except OSError:
        path = None
    if console is None:
        console = not _streams_redirected and sys.stderr is not None
    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.DEBUG if debug_enabled() else logging.WARNING)
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(sh)
    logging.captureWarnings(True)
    return path


def streams_redirected() -> bool:
    return _streams_redirected


def redirect_std_streams(force: bool = False) -> str | None:
    """When stdout/stderr are missing (PyInstaller ``console=False`` on Windows, or a closed
    descriptor), point them at ``logs/console.log`` so writes and ``isatty()`` work."""
    global _streams_redirected
    if not force and sys.stdout is not None and sys.stderr is not None:
        return None
    try:
        path = os.path.join(logs_dir(), "console.log")
        fh = open(path, "a", encoding="utf-8", buffering=1, errors="backslashreplace")
    except OSError:
        fh = open(os.devnull, "w", encoding="utf-8")
        path = None
    if force or sys.stdout is None:
        sys.stdout = fh
    if force or sys.stderr is None:
        sys.stderr = fh
    _streams_redirected = True
    return path
