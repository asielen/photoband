from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from typing import Iterable, List, Optional

log = logging.getLogger(__name__)


def atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj) -> None:
    atomic_write_bytes(path, json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"))


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


# --------------------------------------------------------------------------
# file identity
# --------------------------------------------------------------------------

_MB = 1024 * 1024


def quick_hash(path: str) -> str:
    """Cheap content fingerprint: size + first and last 1 MB. Catches content changes that
    keep size and mtime (tools that restore mtime) without reading a 600 MB scan."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        h.update(str(size).encode() + b"|")
        h.update(fh.read(_MB))
        if size > 2 * _MB:
            fh.seek(size - _MB)
            h.update(fh.read(_MB))
        elif size > _MB:
            h.update(fh.read())
    return h.hexdigest()[:32]


def file_identity(path: str) -> Optional[List]:
    """[size, mtime_ns, quick_hash] or None when the file cannot be read."""
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns, quick_hash(path)]
    except OSError:
        return None


def file_sha256(path: str, chunk: int = 4 * _MB) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def canonical_path(p: str) -> str:
    """Key for a file independent of links and letter case (case-insensitive systems)."""
    return os.path.normcase(os.path.realpath(p))


# --------------------------------------------------------------------------
# placing files
# --------------------------------------------------------------------------

def fsync_dir(d: str) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def place_exclusive(tmp: str, dst: str) -> None:
    """Move ``tmp`` to ``dst`` only if ``dst`` does not exist (raises FileExistsError).
    Uses a hard link (atomic, fails if present); on file systems without hard links
    (FAT/exFAT, some network shares) it reserves the name with O_EXCL, then replaces it."""
    try:
        os.link(tmp, dst)
    except FileExistsError:
        raise
    except (OSError, NotImplementedError, AttributeError) as e:
        if isinstance(e, OSError) and e.errno == errno.EEXIST:
            raise FileExistsError(errno.EEXIST, "File exists", dst)
        fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)  # FileExistsError if taken
        os.close(fd)
        try:
            replace_with_retry(tmp, dst)
        except BaseException:
            try:
                if os.path.getsize(dst) == 0:
                    os.unlink(dst)
            except OSError:
                pass
            raise
        return
    try:
        os.unlink(tmp)
    except OSError:
        pass


def replace_with_retry(src: str, dst: str, tries: int = 5, delay: float = 0.2) -> None:
    """os.replace, retrying Windows sharing/access violations (antivirus, indexers, previewers)."""
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            if os.name != "nt" or getattr(e, "winerror", None) not in (5, 32) or i == tries - 1:
                raise
            time.sleep(delay)


def is_readonly(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if os.name == "nt":
        return bool(getattr(st, "st_file_attributes", 0) & 0x1)  # FILE_ATTRIBUTE_READONLY
    # os.access is True for root even on 0444 files: honour the write bits too
    return (not os.access(path, os.W_OK)) or (st.st_mode & 0o222) == 0


def held_open_elsewhere(path: str) -> bool:
    """Windows: True when another program has ``path`` open without letting it be replaced
    (no FILE_SHARE_DELETE, as most editors and viewers open files). Replacing such a file fails
    with "access denied" (5), not a sharing violation, so this tells the two apart. Opens the
    file for DELETE access only to ask; nothing is changed. False elsewhere or when unsure."""
    if os.name != "nt" or not os.path.isfile(path):
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        DELETE, SHARE_ALL, OPEN_EXISTING = 0x00010000, 0x7, 3
        h = k32.CreateFileW(path, DELETE, SHARE_ALL, None, OPEN_EXISTING, 0, None)
        if h and h != wintypes.HANDLE(-1).value:
            k32.CloseHandle(h)
            return False
        return ctypes.get_last_error() == 32   # ERROR_SHARING_VIOLATION
    except Exception:
        return False


def dir_writable(d: str) -> bool:
    return os.access(d, os.W_OK | os.X_OK) if os.name != "nt" else True


# --------------------------------------------------------------------------
# file attributes kept across an overwrite
# --------------------------------------------------------------------------

def _darwin_xattr_lib():
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    libc.listxattr.restype = ctypes.c_ssize_t
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    libc.setxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
                              ctypes.c_uint32, ctypes.c_int]
    libc.setxattr.restype = ctypes.c_int
    return libc


def copy_xattrs(src: str, dst: str) -> List[str]:
    """Copy extended attributes (Finder tags, comments, xdg tags). Returns names that failed."""
    failed: List[str] = []
    if hasattr(os, "listxattr"):  # Linux
        try:
            names = os.listxattr(src)
        except OSError:
            return failed
        for n in names:
            try:
                os.setxattr(dst, n, os.getxattr(src, n))
            except OSError:
                if not n.startswith(("security.", "system.", "trusted.")):
                    failed.append(n)
        return failed
    if sys.platform == "darwin":
        try:
            import ctypes
            libc = _darwin_xattr_lib()
            s, d = os.fsencode(src), os.fsencode(dst)
            size = libc.listxattr(s, None, 0, 0)
            if size <= 0:
                return failed
            buf = ctypes.create_string_buffer(size)
            size = libc.listxattr(s, buf, size, 0)
            for raw in buf.raw[:max(0, size)].split(b"\0"):
                if not raw:
                    continue
                n = libc.getxattr(s, raw, None, 0, 0, 0)
                if n < 0:
                    failed.append(raw.decode("utf-8", "replace"))
                    continue
                vb = ctypes.create_string_buffer(max(1, n))
                n = libc.getxattr(s, raw, vb, n, 0, 0)
                if n < 0 or libc.setxattr(d, raw, vb, n, 0, 0) != 0:
                    failed.append(raw.decode("utf-8", "replace"))
        except Exception:
            pass
    return failed


def creation_time_ns(st: os.stat_result) -> int:
    ns = getattr(st, "st_birthtime_ns", None)
    if ns:
        return int(ns)
    b = getattr(st, "st_birthtime", None)
    if b:
        return int(b * 1e9)
    return int(st.st_ctime_ns) if os.name == "nt" else 0


def set_creation_time(dst: str, ns: int) -> bool:
    """Windows: set the creation time of ``dst`` (SetFileTime). False elsewhere or on failure."""
    if os.name != "nt" or not ns:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        ft = int(ns // 100) + 116444736000000000
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        FILE_WRITE_ATTRIBUTES, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS = 0x100, 3, 0x02000000
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        h = k32.CreateFileW(dst, FILE_WRITE_ATTRIBUTES, 0x7, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        if not h or h == wintypes.HANDLE(-1).value:
            return False
        try:
            c = wintypes.FILETIME(ft & 0xFFFFFFFF, ft >> 32)
            return bool(k32.SetFileTime(h, ctypes.byref(c), None, None))
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


# --------------------------------------------------------------------------
# locks
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            k32.CloseHandle(h)
            return code.value == 259  # STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # a killed process that hasn't been reaped yet (zombie) holds nothing
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            if fh.read().rsplit(b")", 1)[1].split()[0] == b"Z":
                return False
    except (OSError, IndexError):
        pass
    return True


class LockBusy(Exception):
    pass


class PidLock:
    """An O_EXCL lock file holding the owner's PID. A lock whose process is gone (or that is
    older than ``max_age``) is stale and taken over. Works across processes and app instances."""

    def __init__(self, lock_path: str, max_age: float = 6 * 3600):
        self.path = lock_path
        self.max_age = max_age
        self.held = False

    def acquire(self, timeout: float = 0.0) -> "PidLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        t0 = time.time()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                with os.fdopen(fd, "w") as fh:
                    fh.write(json.dumps({"pid": os.getpid(), "time": time.time()}))
                self.held = True
                return self
            except FileExistsError:
                if self._stale():
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    continue
                if time.time() - t0 >= timeout:
                    raise LockBusy(self.path)
                time.sleep(0.1)

    def _stale(self) -> bool:
        info = read_json(self.path)
        if not isinstance(info, dict):
            # half-written by a process that died; give a writer a moment
            try:
                return time.time() - os.path.getmtime(self.path) > 5
            except OSError:
                return True
        if time.time() - float(info.get("time", 0)) > self.max_age:
            return True
        return not _pid_alive(int(info.get("pid", 0)))

    def release(self) -> None:
        if self.held:
            self.held = False
            try:
                info = read_json(self.path)
                if isinstance(info, dict) and info.get("pid") == os.getpid():
                    os.unlink(self.path)
            except OSError:
                pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *a):
        self.release()


def file_lock(path: str) -> PidLock:
    """Per-file lock kept in app data, keyed by the file's canonical path."""
    from . import paths
    key = hashlib.sha1(canonical_path(path).encode("utf-8")).hexdigest()
    return PidLock(os.path.join(paths.sub("locks"), key + ".lock"))


# --------------------------------------------------------------------------
# leftover temp files
# --------------------------------------------------------------------------

def _is_litter(name: str) -> bool:
    return (name.startswith(".pbtmp-") or name.startswith(".pbbak-") or name.startswith(".pbrestore-")
            or name.endswith(".pbrestore") or name.endswith(".partial"))


# Source links for ExifTool (see metawrite._src_arg): a hard link to the PHOTO itself, so its mtime
# is the photo's, not the link's. The creation time is in the name and decides its age.
SRC_LINK_PREFIX = ".pbtmp-src-"
_SRC_LINK_RE = None


def src_link_name(ext: str) -> str:
    import uuid
    return f"{SRC_LINK_PREFIX}{int(time.time())}-{uuid.uuid4().hex[:12]}{ext}"


def _litter_age(p: str, name: str, now: float) -> float:
    """Seconds since a temp file was made. Source links carry their creation time in the name
    (their mtime is the linked photo's); anything else uses its mtime."""
    global _SRC_LINK_RE
    if name.startswith(SRC_LINK_PREFIX) or name.startswith("src-"):
        import re
        if _SRC_LINK_RE is None:
            # A prefix match on purpose: only the leading creation time is read; a random
            # suffix and the extension follow it.
            _SRC_LINK_RE = re.compile(r"^(?:\.pbtmp-)?src-(\d{9,11})-")
        m = _SRC_LINK_RE.match(name)
        if m:
            return now - int(m.group(1))
    return now - os.lstat(p).st_mtime


def _host_tag() -> str:
    import hashlib
    import socket
    return hashlib.sha1(socket.gethostname().lower().encode("utf-8", "replace")).hexdigest()[:4]


def temp_prefix(kind: str) -> str:
    """``.pbtmp-p<pid>h<host>-`` etc.: the process (and computer) that owns a temp file is in its
    name, so a sweep never removes the file of a save that is still running, here or on another
    computer writing to the same network folder (a multi-GB save to a slow share can take longer
    than the sweep's age limit)."""
    return f"{kind}p{os.getpid()}h{_host_tag()}-"


_OWNER_RE = None


def _owner_running(name: str, age: float) -> bool:
    """The temp file's owning process (from its name) is still running. Another computer's
    process can't be checked from here, so its files are kept as if running. Never trusted after
    a day, so a reused process id or a crashed computer can't keep litter forever."""
    global _OWNER_RE
    if age > 86400:
        return False
    if _OWNER_RE is None:
        import re
        # A prefix match on purpose: only the owner tag at the start is read; the file name follows.
        _OWNER_RE = re.compile(r"^\.pb(?:tmp|bak|restore)-p(\d{1,10})(?:h([0-9a-f]{4}))?-")
    m = _OWNER_RE.match(name)
    if not m:
        return False
    if m.group(2) and m.group(2) != _host_tag():
        return True
    pid = int(m.group(1))
    return pid == os.getpid() or _pid_alive(pid)


def sweep_temp(folders: Iterable[str], max_age: float = 600.0) -> List[str]:
    """Delete this app's temp files (.pbtmp-*, .pbbak-*, *.partial, *.pbrestore) older than
    ``max_age`` seconds in each folder and its backup subfolder. Returns what was removed."""
    removed: List[str] = []
    now = time.time()
    seen = set()
    for f in folders:
        if not f:
            continue
        for d in (f, os.path.join(f, "_originals")):
            try:
                key = canonical_path(d)
                if key in seen or not os.path.isdir(d):
                    continue
                seen.add(key)
                names = os.listdir(d)
            except OSError:
                continue
            in_backup = os.path.basename(d) == "_originals"
            for n in names:
                if not _is_litter(n):
                    continue
                if n.endswith(".partial") and not (in_backup or n.startswith(".pb")):
                    continue  # someone else's .partial download: not ours
                p = os.path.join(d, n)
                try:
                    if not os.path.isfile(p) or os.path.islink(p):
                        continue
                    age = _litter_age(p, n, now)
                    if age > max_age and not _owner_running(n, age):
                        os.unlink(p)
                        removed.append(p)
                except OSError:
                    log.debug("could not remove %s", p, exc_info=True)
    return removed


def sweep_app_tmp(tmp_dirs: Iterable[str], max_age: float = 600.0) -> List[str]:
    """Delete source links/copies (``src-*``) and metadata JSON files that a killed save left in
    the app's own temp folder. A link is removed with unlink, never followed: the photo it
    points to is untouched."""
    removed: List[str] = []
    now = time.time()
    for d in tmp_dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not (n.startswith("src-") or (n.startswith("tmp") and n.endswith(".json"))):
                continue
            p = os.path.join(d, n)
            try:
                if os.path.islink(p) or os.path.isfile(p):
                    if _litter_age(p, n, now) > max_age:
                        os.unlink(p)
                        removed.append(p)
            except OSError:
                log.debug("could not remove %s", p, exc_info=True)
    return removed
