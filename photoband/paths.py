"""Where things live: app data (settings, templates, drafts, cache, logs) and bundled resources."""
from __future__ import annotations

import os
import sys


def app_data() -> str:
    override = os.environ.get("PHOTOBAND_HOME")
    if override:
        base = override
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Photoband")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/Photoband")
    else:
        base = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Photoband")
    os.makedirs(base, exist_ok=True)
    return base


def sub(*parts: str) -> str:
    p = os.path.join(app_data(), *parts)
    os.makedirs(p, exist_ok=True)
    return p


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _package_data() -> str | None:
    """``photoband/_data`` inside an installed wheel (``pip install .``/pipx), populated by
    setup.py with ui/dist and fonts/. None for editable installs and source checkouts."""
    try:
        from importlib.resources import files
        d = files("photoband").joinpath("_data")
        if d.is_dir():
            return os.fspath(d)  # a plain directory for every normal (non-zip) install
    except (ImportError, TypeError, ValueError, OSError):
        pass
    return None


def resource_dir() -> str:
    """Root of the bundled resources (``ui/dist``, ``fonts``, ``vendor``):
    PyInstaller's bundle dir when frozen, ``photoband/_data`` in an installed wheel,
    else the repository root (editable install or running from a checkout)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS  # type: ignore[attr-defined]
    return _package_data() or _repo_root()


def ui_dist() -> str:
    return os.environ.get("PHOTOBAND_UI_DIST") or os.path.join(resource_dir(), "ui", "dist")


def fonts_dir() -> str:
    return os.path.join(resource_dir(), "fonts")
