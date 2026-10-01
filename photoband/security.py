"""Per-launch token and the path allow-list.

The server binds to 127.0.0.1 only. Every request must carry the per-launch
token. File access is limited to paths the user picked in a dialog (or the
in-app browser) or dropped onto the window, plus the configured copy
destination and backup folder.

Paths are granted server-side only:

* native dialog results (``/api/dialog``),
* drop events from the desktop window (desktop.py),
* the in-app browser: ``/api/fs/list`` hands out a one-time *pick id* per
  listed entry; ``/api/fs/allow`` accepts only those ids (see :func:`new_pick`
  and :func:`redeem_pick`), never raw paths.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
import threading
import time
from typing import Dict, Iterable, Optional, Set, Tuple

MIN_TOKEN_LEN = 16
_env_token = os.environ.get("PHOTOBAND_TOKEN") or ""
#: Set when PHOTOBAND_TOKEN was given but is too short; the launcher refuses to start.
TOKEN_ERROR = (f"PHOTOBAND_TOKEN must be at least {MIN_TOKEN_LEN} characters."
               if _env_token and len(_env_token) < MIN_TOKEN_LEN else "")
TOKEN = _env_token if (_env_token and not TOKEN_ERROR) else secrets.token_urlsafe(24)


class UserError(ValueError):
    """A problem with what the user (or the UI) asked for; the API answers 400 with the message.
    Plain ValueErrors still map to 400 too, but are logged with a traceback (they may be bugs)."""


def token_ok(candidate: Optional[str]) -> bool:
    """Constant-time comparison with the per-launch token."""
    if not candidate or not isinstance(candidate, str):
        return False
    return secrets.compare_digest(candidate.encode("utf-8", "surrogatepass"), TOKEN.encode("utf-8"))


# ---------------------------------------------------------------------------- launch hand-off
# Opening the browser with ?t=TOKEN would put the token on the browser's command line, where
# any local user can read it (ps). The launcher opens /?b=<nonce> instead; the server trades the
# nonce for the token exactly once (a redirect to /#t=..., read and removed by the UI).

HANDOFF_TTL = 10 * 60        # seconds: the browser may take a while to start
_handoff_lock = threading.Lock()
_handoffs: Dict[str, float] = {}   # nonce -> expires (monotonic)


def new_handoff() -> str:
    n = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _handoff_lock:
        for k in [k for k, v in _handoffs.items() if v < now]:
            del _handoffs[k]
        _handoffs[n] = now + HANDOFF_TTL
    return n


def redeem_handoff(nonce: Optional[str]) -> bool:
    """True once for a nonce from :func:`new_handoff` that has not expired."""
    if not nonce or not isinstance(nonce, str):
        return False
    with _handoff_lock:
        exp = _handoffs.pop(nonce, None)
    return exp is not None and exp >= time.monotonic()


def launch_url(port: int) -> str:
    """The URL to open in a browser: carries a one-time nonce, never the token."""
    return f"http://127.0.0.1:{port}/?b={new_handoff()}"


_lock = threading.Lock()
_roots: Set[str] = set()     # folders: everything below is allowed
_files: Set[str] = set()     # single files


# ---------------------------------------------------------------------------- Windows device/UNC paths

def is_unc(p: str, platform: Optional[str] = None) -> bool:
    """Windows network (\\\\server\\share) and device (\\\\?\\, \\\\.\\) paths. Merely touching one
    can leak the user's NTLM credentials to a remote host, so only a native dialog may return them."""
    if (platform or sys.platform) != "win32" or not isinstance(p, str):
        return False
    q = p.replace("/", "\\")
    return q.startswith("\\\\")


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _norm_unc(p: str) -> str:
    # never resolve a UNC path (resolving touches the network); compare textually
    return os.path.normcase(os.path.normpath(p))


def allow(paths: Iterable[str], from_dialog: bool = False) -> None:
    """Grant files/folders. ``from_dialog`` marks native dialog/drop results, which may be UNC paths."""
    with _lock:
        for p in paths:
            if not p or not isinstance(p, str) or "\x00" in p:
                continue
            if is_unc(p):
                if not from_dialog:
                    continue
                n = _norm_unc(p)
                (_roots if os.path.isdir(p) else _files).add(n)
                continue
            n = _norm(p)
            if os.path.isdir(n):
                _roots.add(n)
            else:
                _files.add(n)


def allow_root(p: str, from_dialog: bool = False) -> None:
    if not p or not isinstance(p, str) or "\x00" in p:
        return
    if is_unc(p):
        if from_dialog:
            with _lock:
                _roots.add(_norm_unc(p))
        return
    with _lock:
        _roots.add(_norm(p))


def is_allowed(p: str) -> bool:
    if not p or not isinstance(p, str) or "\x00" in p:
        return False
    n = _norm_unc(p) if is_unc(p) else _norm(p)
    with _lock:
        if n in _files:
            return True
        for r in _roots:
            if n == r or n.startswith(r.rstrip(os.sep) + os.sep):
                return True
    return False


def is_allowed_root(p: str) -> bool:
    """True if ``p`` is a folder that is (inside) an allowed root."""
    return bool(p) and is_allowed(p) and os.path.isdir(p)


def allowed_roots():
    with _lock:
        return sorted(_roots)


def reset() -> None:
    with _lock:
        _roots.clear()
        _files.clear()
    with _picks_lock:
        _picks.clear()


def check(p: str) -> str:
    """The resolved real path of an allowed file or folder; PermissionError otherwise."""
    if not p or not isinstance(p, str) or not is_allowed(p):
        raise PermissionError("That file or folder was not opened in Photoband.")
    if is_unc(p):
        return os.path.normpath(p)
    return os.path.realpath(os.path.abspath(p))


def allow_settings_folders(settings) -> None:
    """The configured copy destination and backup folder (chosen in a dialog; see server.settings_patch)."""
    s = settings.get("saving", {})
    for k in ("fixedFolder", "backupFolder"):
        v = s.get(k)
        if v and isinstance(v, str) and not is_unc(v):
            allow_root(v)


# ---------------------------------------------------------------------------- in-app browser pick ids

PICK_TTL = 15 * 60          # seconds
PICK_MAX = 50000            # outstanding ids kept (oldest dropped)
_picks_lock = threading.Lock()
_picks: Dict[str, Tuple[str, bool, float]] = {}   # id -> (realpath, is_dir, expires)


def new_pick(path: str, is_dir: bool) -> str:
    """A one-time id standing for ``path`` as listed by the in-app browser."""
    pid = secrets.token_urlsafe(12)
    now = time.monotonic()
    with _picks_lock:
        if len(_picks) >= PICK_MAX:
            for k in [k for k, v in _picks.items() if v[2] < now]:
                del _picks[k]
            while len(_picks) >= PICK_MAX:
                del _picks[next(iter(_picks))]
        _picks[pid] = (os.path.realpath(path), bool(is_dir), now + PICK_TTL)
    return pid


def redeem_pick(pid: str) -> Tuple[str, bool]:
    """(path, is_dir) for a pick id; each id works once and expires after PICK_TTL."""
    if not isinstance(pid, str):
        raise PermissionError("Invalid pick.")
    with _picks_lock:
        v = _picks.pop(pid, None)
    if not v or v[2] < time.monotonic():
        raise PermissionError("That choice has expired. Please choose the file or folder again.")
    return v[0], v[1]


_BAD_NAME = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
# Matched with fullmatch and DOTALL: "$" alone also matches before a final newline, and "."
# would stop at one, so the whole name must be the device name (plus any extension).
_WIN_RESERVED = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", re.I | re.S)


def plain_file_name(name: str) -> str:
    """A bare file name (no folders, no '..', no device names); ValueError otherwise."""
    if not isinstance(name, str):
        raise UserError("Invalid file name")
    n = name.strip()
    if (not n or n in (".", "..") or len(n) > 255 or _BAD_NAME.search(n) or n.endswith((".", " "))
            or _WIN_RESERVED.fullmatch(n)):
        raise UserError("Please enter a plain file name (no folders or special characters).")
    return n


def is_plain_folder_name(name) -> bool:
    """A single folder name (no separators, no '.' or '..', no absolute path, no device name)."""
    if not isinstance(name, str) or name != name.strip():
        return False
    try:
        plain_file_name(name)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------- in-app browser scope

_WIN_SYSTEM = ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
               "system volume information", "recovery")


def mounted_volumes() -> list:
    """Drives and mounted volumes the in-app browser may show (not the system folders)."""
    out = []
    if sys.platform == "win32":
        import string
        out = [f"{c}:\\" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]
        return out
    bases = ["/Volumes"] if sys.platform == "darwin" else ["/media", "/mnt", "/run/media"]
    for b in bases:
        try:
            for n in sorted(os.listdir(b)):
                p = os.path.join(b, n)
                if n.startswith(".") or not os.path.isdir(p):
                    continue
                if b in ("/media", "/run/media"):
                    # /media/<user>/<volume> on most Linux desktops
                    try:
                        subs = [os.path.join(p, m) for m in sorted(os.listdir(p)) if not m.startswith(".")]
                    except OSError:
                        subs = []
                    out.extend(q for q in subs if os.path.isdir(q))
                    if not subs:
                        out.append(p)
                else:
                    out.append(p)
        except OSError:
            continue
    return out


def is_device_path(p: str) -> bool:
    """Windows device namespace paths (\\\\?\\..., \\\\.\\...): never browsed."""
    q = (p or "").replace("/", "\\")
    return q.startswith("\\\\?\\") or q.startswith("\\\\.\\")


def last_folder_roots(last: Optional[str]) -> list:
    """The folder used last time (``session.lastFolder``) and its parents up to its drive, share
    or mount root, so a NAS or external-drive folder stays reachable in the in-app browser after
    a restart. Never the system root or a top-level system folder (/, /var, /home ...)."""
    if not last or not isinstance(last, str) or "\x00" in last or is_device_path(last):
        return []
    unc = is_unc(last)
    try:
        p = _norm_unc(last) if unc else _norm(last)
    except (OSError, ValueError):
        return []
    if not unc and not os.path.isdir(p):
        return []
    out = []
    while True:
        if sys.platform == "win32" or unc:
            drive, rest = os.path.splitdrive(p)
            if not drive:
                break
            out.append(p)
            if not rest.strip("\\/"):
                break          # the drive or share root
        else:
            depth = len([x for x in p.split(os.sep) if x])
            if depth <= 1:
                break          # "/" and top-level folders (/var, /home, /mnt ...) are system places
            out.append(p)
            if os.path.ismount(p):
                break
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return out


def browse_roots(last: Optional[str] = None) -> list:
    """Folders the in-app browser (browser mode only) may list: the home folder, the temp folder
    (opened attachments land there), mounted drives, folders already granted and the folder
    used last time. System folders (/, /etc, C:\\Windows ...) are not listed; use the system file
    dialog for anything else."""
    import tempfile
    roots = [os.path.expanduser("~"), tempfile.gettempdir()] + mounted_volumes() + allowed_roots()
    out, seen = [], set()
    for r in roots:
        if not r or is_unc(r):
            continue
        n = _norm(r)
        if n in seen or (sys.platform != "win32" and n == os.sep):
            continue   # never the whole file system
        seen.add(n)
        out.append(n)
    for n in last_folder_roots(last):
        if n not in seen and not (sys.platform != "win32" and n == os.sep):
            seen.add(n)
            out.append(n)
    return out


def unc_browsable(p: str, last: Optional[str] = None) -> bool:
    """A network path (\\\\server\\share\\...) the in-app browser may open: inside a mapped
    network drive, a folder the user chose in a dialog, or the folder used last time. An
    arbitrary host typed into a request is never contacted (that can leak NTLM credentials)."""
    if not is_unc(p) or is_device_path(p):
        return False
    n = _norm_unc(p)
    known = [_norm_unc(r) for r in allowed_roots() if is_unc(r)]
    known += [r for r in last_folder_roots(last) if is_unc(r)]
    if sys.platform == "win32":
        for v in mounted_volumes():
            try:
                t = os.path.realpath(v)
            except (OSError, ValueError):
                continue
            if is_unc(t):
                known.append(_norm_unc(t))
    for r in known:
        rr = r.rstrip("\\/")
        if n == rr or n.startswith(rr + "\\") or n.startswith(rr + os.sep):
            return True
    return False


def may_browse(d: str, last: Optional[str] = None) -> bool:
    """True when the in-app browser may list folder ``d``. Call it on the path as given
    (normalised) AND on its resolved form: a mapped drive (Z:\\) resolves to its network share
    (\\\\server\\share), which is user data like any drive."""
    if sys.platform == "win32":
        if is_device_path(d):
            return False
        n = _norm_unc(d) if is_unc(d) else _norm(d)
        drive, rest = os.path.splitdrive(n)
        parts = [x for x in rest.split(os.sep) if x]
        if not drive.startswith("\\\\") and parts and parts[0].lower() in _WIN_SYSTEM:
            return False
        if parts and any(x.startswith(".") for x in parts):
            return False
        return bool(drive)
    n = _norm(d)
    for r in browse_roots(last):
        rr = r.rstrip(os.sep)
        if n == rr or n.startswith(rr + os.sep):
            rel = n[len(rr):].strip(os.sep)
            # hidden folders (~/.ssh, ~/.config ...) are never listed
            if any(x.startswith(".") for x in rel.split(os.sep) if x):
                return False
            return True
    return False
