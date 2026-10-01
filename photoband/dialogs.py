"""Native file dialogs.

In the desktop app, pywebview's dialogs are used (see desktop.py, which
registers ``set_provider``). In browser mode the backend's helper tries the
OS dialog (osascript on macOS, a tkinter subprocess on Windows, zenity/kdialog
on Linux); if none works the UI falls back to its in-app file browser.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Callable, List, Optional

_provider: Optional[Callable[..., Optional[List[str]]]] = None


def set_provider(fn) -> None:
    global _provider
    _provider = fn


class Unavailable(Exception):
    pass


def ask(kind: str, title: str = "", initial: str = "", filetypes: Optional[List[str]] = None,
        save_name: str = "") -> Optional[List[str]]:
    """kind: open-files | open-folder | save-file | open-font | exiftool. Returns paths or None (cancelled).

    Every helper receives the title, initial folder and file name as separate arguments
    (never through a shell and never spliced into script source)."""
    if kind not in KINDS:
        raise ValueError(f"Unknown dialog kind {kind!r}")
    title = "".join(ch for ch in str(title or "") if ch >= " ")[:200]
    initial = _initial_dir(str(initial or ""))
    save_name = _plain_name(str(save_name or ""))
    if _provider is not None:
        return _provider(kind, title=title, initial=initial, filetypes=filetypes, save_name=save_name)
    if os.environ.get("PHOTOBAND_NO_NATIVE_DIALOGS"):
        raise Unavailable()
    if sys.platform == "darwin":
        return _osascript(kind, title, initial, save_name)
    if sys.platform == "win32":
        return _tk(kind, title, initial, filetypes, save_name)
    for tool in ("zenity", "kdialog"):
        if shutil.which(tool):
            return _linux(tool, kind, title, initial, save_name)
    return _tk(kind, title, initial, filetypes, save_name)


def _run(args, timeout=600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# The AppleScript is a constant: every user-controlled value (title, initial folder, file name)
# is passed as an argument and read with ``item N of argv``, never spliced into the source.
OSASCRIPT = """on run argv
set k to item 1 of argv
set t to item 2 of argv
set loc to item 3 of argv
set nm to item 4 of argv
if k is "open-folder" then
if loc is "" then
return POSIX path of (choose folder with prompt t)
end if
return POSIX path of (choose folder with prompt t default location (POSIX file loc as alias))
end if
if k is "save-file" then
if loc is "" then
return POSIX path of (choose file name with prompt t default name nm)
end if
return POSIX path of (choose file name with prompt t default location (POSIX file loc as alias) default name nm)
end if
if k is "open-files" then
set tys to {"public.tiff", "public.jpeg", "public.png"}
if loc is "" then
set fs to (choose file with prompt t of type tys with multiple selections allowed)
else
set fs to (choose file with prompt t of type tys default location (POSIX file loc as alias) with multiple selections allowed)
end if
else if k is "open-font" then
set fs to (choose file with prompt t of type {"public.font"})
else
set fs to (choose file with prompt t)
end if
set out to ""
repeat with f in (fs as list)
set out to out & POSIX path of f & linefeed
end repeat
return out
end run"""

KINDS = ("open-files", "open-folder", "save-file", "open-font", "exiftool")


def _plain_name(name: str) -> str:
    """A default file name for a save dialog: the last path component, no control characters."""
    n = os.path.basename((name or "").replace("\\", "/"))
    return "".join(ch for ch in n if ch >= " " and ch != "\x7f")[:255]


def _initial_dir(initial: str) -> str:
    return os.path.abspath(initial) if initial and os.path.isdir(initial) else ""


def osascript_argv(kind: str, title: str, initial: str, save_name: str) -> List[str]:
    """argv for osascript: a fixed script followed by plain string arguments."""
    if kind not in KINDS:
        kind = "open-files"
    t = "".join(ch for ch in (title or "Photoband") if ch >= " ")[:200] or "Photoband"
    return ["osascript", "-e", OSASCRIPT, kind, t, _initial_dir(initial), _plain_name(save_name)]


def _osascript(kind, title, initial, save_name):
    r = _run(osascript_argv(kind, title, initial, save_name))
    if r.returncode != 0:
        if "User canceled" in r.stderr or "-128" in r.stderr:
            return None
        raise Unavailable(r.stderr)
    return [ln for ln in r.stdout.splitlines() if ln.strip()] or None


_TK = r"""
import json, sys, tkinter as tk
from tkinter import filedialog
a = json.loads(sys.argv[1])
root = tk.Tk(); root.withdraw()
try: root.attributes('-topmost', True)
except Exception: pass
k = a['kind']
if k == 'open-folder':
    r = filedialog.askdirectory(title=a['title'], initialdir=a['initial'] or None); r = [r] if r else None
elif k == 'save-file':
    r = filedialog.asksaveasfilename(title=a['title'], initialdir=a['initial'] or None, initialfile=a['save_name'],
        filetypes=[('TIFF', '*.tif *.tiff'), ('JPEG', '*.jpg *.jpeg'), ('PNG', '*.png')]); r = [r] if r else None
elif k == 'open-font':
    r = filedialog.askopenfilename(title=a['title'], filetypes=[('Fonts', '*.ttf *.otf')]); r = [r] if r else None
elif k == 'exiftool':
    r = filedialog.askopenfilename(title=a['title']); r = [r] if r else None
else:
    r = filedialog.askopenfilenames(title=a['title'], initialdir=a['initial'] or None,
        filetypes=[('Images', '*.tif *.tiff *.jpg *.jpeg *.png')]); r = list(r) if r else None
print(json.dumps(r))
"""


def _tk(kind, title, initial, filetypes, save_name):
    if getattr(sys, "frozen", False):
        # a frozen app can't spawn "python -c"; run tkinter in-process on a thread instead
        raise Unavailable("tk subprocess unavailable in frozen app")
    try:
        r = _run([sys.executable, "-c", _TK, json.dumps({"kind": kind, "title": title, "initial": initial,
                                                        "save_name": save_name})])
    except Exception as e:
        raise Unavailable(str(e))
    if r.returncode != 0:
        raise Unavailable(r.stderr)
    return json.loads(r.stdout.strip() or "null")


def linux_argv(tool: str, kind: str, title: str, initial: str, save_name: str) -> List[str]:
    """argv for zenity/kdialog. Each value is its own argument; paths are absolute so none
    can be mistaken for an option."""
    title = title or "Photoband"
    start = _initial_dir(initial) or os.path.expanduser("~")
    name = _plain_name(save_name)
    if tool == "zenity":
        args = ["zenity", "--file-selection", "--title=" + title]
        if kind == "open-folder":
            args += ["--directory", "--filename=" + start.rstrip("/") + "/"]
        elif kind == "save-file":
            args += ["--save", "--confirm-overwrite", "--filename=" + os.path.join(start, name)]
        elif kind == "open-files":
            args += ["--multiple", "--separator=\n", "--filename=" + start.rstrip("/") + "/",
                     "--file-filter=Images | *.tif *.tiff *.jpg *.jpeg *.png"]
        elif kind == "open-font":
            args += ["--file-filter=Fonts | *.ttf *.otf"]
        return args
    if kind == "open-folder":
        return ["kdialog", "--title=" + title, "--getexistingdirectory", start]
    if kind == "save-file":
        return ["kdialog", "--title=" + title, "--getsavefilename", os.path.join(start, name)]
    if kind == "open-files":
        return ["kdialog", "--title=" + title, "--multiple", "--separate-output", "--getopenfilename", start,
                "*.tif *.tiff *.jpg *.jpeg *.png"]
    if kind == "open-font":
        return ["kdialog", "--title=" + title, "--getopenfilename", os.path.expanduser("~"), "*.ttf *.otf"]
    return ["kdialog", "--title=" + title, "--getopenfilename", os.path.expanduser("~")]


def _linux(tool, kind, title, initial, save_name):
    args = linux_argv(tool, kind, title, initial, save_name)
    try:
        r = _run(args)
    except Exception as e:
        raise Unavailable(str(e))
    if r.returncode == 1:
        return None
    if r.returncode != 0:
        raise Unavailable(r.stderr)
    return [ln for ln in r.stdout.splitlines() if ln.strip()] or None
