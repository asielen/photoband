"""Persistent ExifTool process (-stay_open), one per process/thread user."""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Sequence

from . import paths

log = logging.getLogger(__name__)

CONFIG = r"""
%Image::ExifTool::UserDefined = (
    'Image::ExifTool::XMP::Main' => {
        photoband => {
            SubDirectory => { TagTable => 'Image::ExifTool::UserDefined::photoband' },
        },
    },
);
%Image::ExifTool::UserDefined::photoband = (
    GROUPS => { 0 => 'XMP', 1 => 'XMP-photoband', 2 => 'Image' },
    NAMESPACE => { 'photoband' => 'http://ns.photoband.app/1.0/' },
    WRITABLE => 'string',
    Version => { },
    Record => { },
);
1;
"""


class ExifToolError(Exception):
    pass


class ExifData(dict):
    """ExifTool JSON for one file (numbers as numbers, as before). ``text`` is
    the same data with every number kept as the exact text ExifTool printed
    ("1.50", "007"), for reading text tags."""

    def __init__(self, *args, text: Optional[Dict] = None, **kw):
        super().__init__(*args, **kw)
        self.text = text if text is not None else dict(self)

    def __reduce__(self):
        return (self.__class__, (dict(self),), {"text": self.text})


_VER_RE = re.compile(r"^\s*(\d+\.\d+)\s*$")
_verified: Dict[tuple, str] = {}
_verified_lock = threading.Lock()


def exiftool_command(path: str) -> Optional[List[str]]:
    """How to launch the ExifTool at ``path``, or None if it can't be one: an executable
    (or .exe) file, or a Perl script literally named exiftool / exiftool.pl."""
    if not path or not isinstance(path, str) or "\x00" in path or not os.path.isabs(path):
        return None
    if not os.path.isfile(path):
        return None
    if path.lower().endswith(".exe") or (sys.platform != "win32" and os.access(path, os.X_OK)):
        return [path]
    if os.path.basename(path).lower() in ("exiftool", "exiftool.pl"):
        return ["perl", path]
    return None


def exiftool_version(cmd: Sequence[str], timeout: float = 15.0) -> Optional[str]:
    """Run ``<cmd> -ver``; the version string if it prints one (cached per file state)."""
    try:
        st = os.stat(cmd[-1])
        key = (tuple(cmd), st.st_size, st.st_mtime_ns)
    except OSError:
        key = (tuple(cmd),)
    with _verified_lock:
        if key in _verified:
            return _verified[key]
    try:
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(list(cmd) + ["-ver"], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, **kw)
    except Exception:
        return None
    m = _VER_RE.match(r.stdout or "") if r.returncode == 0 else None
    if not m:
        return None
    with _verified_lock:
        _verified[key] = m.group(1)
    return m.group(1)


def validate_exiftool(path: str) -> List[str]:
    """The launch command for a user-chosen ExifTool; ExifToolError if it isn't one."""
    cmd = exiftool_command(path)
    if not cmd:
        raise ExifToolError("That file is not ExifTool (choose the exiftool program or exiftool.exe).")
    if not exiftool_version(cmd):
        raise ExifToolError("That file did not run as ExifTool (it did not report a version).")
    return cmd


def find_exiftool(override: Optional[str] = None) -> List[str]:
    """Command prefix to launch ExifTool: override (chosen in a native dialog), $PHOTOBAND_EXIFTOOL,
    bundled copy, then PATH. Each candidate must look like ExifTool and answer ``-ver``."""
    cands = []
    if override:
        cands.append(override)
    env = os.environ.get("PHOTOBAND_EXIFTOOL")
    if env:
        cands.append(env)
    base = paths.resource_dir()
    if sys.platform == "win32":
        cands += [os.path.join(base, "vendor", "exiftool", "exiftool.exe")]
    else:
        cands += [os.path.join(base, "vendor", "exiftool", "exiftool")]
    w = shutil.which("exiftool")
    if w:
        cands.append(w)
    for c in cands:
        cmd = exiftool_command(os.path.abspath(c)) if c else None
        if cmd and exiftool_version(cmd):
            return cmd
    raise ExifToolError("ExifTool was not found. Install it or choose it in Settings › Advanced.")


def config_path() -> str:
    """The ExifTool config (our XMP-photoband namespace). Written atomically, so a
    process starting ExifTool never reads a half-written file."""
    p = os.path.join(paths.app_data(), "exiftool_photoband.config")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            if fh.read() == CONFIG:
                return p
    except OSError:
        pass
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(CONFIG)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return p


def _die_with_parent(parent_pid: int):
    """Linux: a preexec_fn that ends ExifTool when the app dies, even on a hard kill."""
    def fn():  # pragma: no cover - runs in the child
        try:
            import ctypes
            import signal
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
        except Exception:
            pass
        if os.getppid() != parent_pid:  # the app died before prctl took effect
            os._exit(1)
    return fn


class _Spawner:
    """One long-lived thread that starts every ExifTool process (Linux).

    PR_SET_PDEATHSIG fires when the *thread* that forked the child exits, not
    the process. ExifTool started from a short-lived worker thread would be
    killed as soon as that thread finished ("ExifTool exited unexpectedly").
    Forking from this daemon thread, which lives as long as the process, keeps
    the parent-death kill tied to the app's lifetime."""

    def __init__(self):
        self.pid = os.getpid()
        self._q: "queue.Queue" = queue.Queue()
        self._t = threading.Thread(target=self._run, name="exiftool-spawner", daemon=True)
        self._t.start()

    def _run(self):  # pragma: no cover - exercised through call()
        while True:
            fn, box, ev = self._q.get()
            try:
                box["v"] = fn()
            except BaseException as e:  # handed back to the caller
                box["e"] = e
            ev.set()

    def call(self, fn):
        if threading.current_thread() is self._t:
            return fn()
        box: Dict = {}
        ev = threading.Event()
        self._q.put((fn, box, ev))
        ev.wait()
        if "e" in box:
            raise box["e"]
        return box["v"]


_spawner: Optional[_Spawner] = None
_spawner_lock = threading.Lock()


def _spawn(fn):
    """Run ``fn`` (which starts a process) on the spawner thread on Linux."""
    global _spawner
    if not sys.platform.startswith("linux"):
        return fn()
    with _spawner_lock:
        if _spawner is None or _spawner.pid != os.getpid():  # rebuilt after a fork
            _spawner = _Spawner()
        sp = _spawner
    return sp.call(fn)


_SUMMARY_UPDATED = re.compile(r"(\d+) image files? updated")
_SUMMARY_UNCHANGED = re.compile(r"(\d+) image files? unchanged")


def _strip_file(msg: str) -> str:
    """'Error: Something - /path/file.jpg' -> 'Something'."""
    msg = re.sub(r"^(Error|Warning):\s*", "", msg.strip())
    return re.sub(r"\s+-\s+\S.*$", "", msg) if " - " in msg else msg


def parse_warnings(text: str) -> List[str]:
    """ExifTool 'Warning: ...' lines, without the prefix and file name, de-duplicated."""
    out: List[str] = []
    for line in (text or "").splitlines():
        if line.strip().startswith("Warning:"):
            w = _strip_file(line)
            if w and w not in out:
                out.append(w)
    return out


def check_write(out: str, err: str, require_change: bool = True) -> None:
    """Raise ExifToolError unless ExifTool's summary says the file was updated.

    The summary goes to stdout for normal writes and to stderr in -json import
    mode, so both are searched. "0 image files updated" is a failure (unless
    ``require_change`` is False and the file was merely unchanged), as are
    "Error:" lines, a JSON import that matched no SourceFile and "Nothing to do"."""
    text = f"{out}\n{err}"
    errors = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("Error")]
    for bad in ("No SourceFile", "Nothing to do"):
        if bad in text:
            errors.append(next(ln.strip() for ln in text.splitlines() if bad in ln))
    m = _SUMMARY_UPDATED.search(text)
    updated = int(m.group(1)) if m else 0
    m = _SUMMARY_UNCHANGED.search(text)
    unchanged = int(m.group(1)) if m else 0
    if errors:
        raise ExifToolError(_strip_file(errors[0]) or "ExifTool could not write the file")
    if updated == 0 and not (unchanged and not require_change):
        raise ExifToolError(text.strip() or "ExifTool did not update the file")


def _file_arg(path: str) -> str:
    """A file name as an ExifTool argument: no line breaks (the -@ argument
    stream is line based) and never mistaken for an option."""
    if "\n" in path or "\r" in path:
        raise ExifToolError("File names with line breaks aren't supported")
    if path.startswith("-") or path.startswith("@"):
        path = os.path.abspath(path)
    return path


_job = None


def _attach_kill_on_close_job(proc) -> None:  # pragma: no cover - Windows only
    """Windows: put ExifTool in a job object that is killed when the app exits."""
    global _job
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        if _job is None:
            _job = k32.CreateJobObjectW(None, None)

            class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))
        k32.AssignProcessToJobObject(_job, int(proc._handle))
    except Exception:
        pass


READ_TIMEOUT = 300.0     # s: reading a big file on a slow network share can take minutes
WRITE_TIMEOUT = 900.0    # s: a write rewrites the whole file (a 2 GB TIFF on a share)
ERR_GRACE = 30.0         # s: stderr marker after stdout's (normally immediate)
CLOSE_LOCK_TIMEOUT = 5.0

_ERR_MARK = re.compile(r"^\{ready_err(\d+)\}$")


def _pump(stream, q: "queue.Queue") -> None:
    """Move lines from a pipe into a queue; None marks the end of the stream."""
    try:
        for line in iter(stream.readline, b""):
            q.put(line)
    except (OSError, ValueError):
        log.debug("ExifTool pipe closed", exc_info=True)
    finally:
        q.put(None)


class ExifTool:
    def __init__(self, override: Optional[str] = None):
        self.cmd = find_exiftool(override)
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._n = 0
        self._out_q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._err_q: "queue.Queue[Optional[bytes]]" = queue.Queue()

    # -- process management ------------------------------------------------
    def _start(self):
        args = self.cmd + ["-config", config_path(), "-stay_open", "True", "-@", "-"]
        flags = 0
        if sys.platform == "win32":
            flags = 0x08000000  # CREATE_NO_WINDOW
        linux = sys.platform.startswith("linux")
        pre = _die_with_parent(os.getpid()) if linux else None
        self._proc = _spawn(lambda: subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                     stderr=subprocess.PIPE, creationflags=flags,
                                                     preexec_fn=pre))
        if sys.platform == "win32":
            _attach_kill_on_close_job(self._proc)
        # fresh queues per process: nothing a killed process left behind reaches the next command
        self._out_q, self._err_q = queue.Queue(), queue.Queue()
        for stream, q, nm in ((self._proc.stdout, self._out_q, "out"), (self._proc.stderr, self._err_q, "err")):
            threading.Thread(target=_pump, args=(stream, q), daemon=True,
                             name=f"exiftool-{nm}-{self._proc.pid}").start()

    def _kill(self) -> None:
        """End the process now (the next command starts a new one)."""
        p, self._proc = self._proc, None
        if p is None:
            return
        try:
            p.kill()
        except OSError:
            log.debug("ExifTool kill failed", exc_info=True)
        try:
            p.wait(timeout=5)
        except Exception:
            log.debug("ExifTool did not end after kill", exc_info=True)

    def close(self):
        """Ask ExifTool to quit; kill it if it doesn't, or if a stuck command holds
        the instance (so the app can always exit)."""
        got = self._lock.acquire(timeout=CLOSE_LOCK_TIMEOUT)
        try:
            p = self._proc
            if p is None:
                return
            if not got:
                self._kill()
                return
            if p.poll() is None:
                try:
                    p.stdin.write(b"-stay_open\nFalse\n")
                    p.stdin.flush()
                    p.wait(timeout=5)
                except Exception:
                    log.debug("ExifTool did not quit; killing it", exc_info=True)
                    p.kill()
            self._proc = None
        finally:
            if got:
                self._lock.release()

    def __del__(self):  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass

    # -- commands ----------------------------------------------------------
    def execute(self, *args: str, files: Sequence[str] = (), timeout: Optional[float] = None) -> (str, str):
        """Run one command. ``files`` go last, checked and made safe (see _file_arg).

        A command that does not finish within ``timeout`` seconds kills (and on the next
        command restarts) this ExifTool process and raises ExifToolError."""
        files = [_file_arg(f) for f in files]
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._start()
            self._n += 1
            n = self._n
            payload = ["-charset", "filename=utf8", "-charset", "utf8", *args, *files,
                       "-echo4", f"{{ready_err{n}}}", f"-execute{n}"]
            if any("\n" in a or "\r" in a for a in payload):
                raise ExifToolError("ExifTool arguments cannot contain line breaks; use a JSON import file")
            data = "\n".join(payload) + "\n"
            try:
                self._proc.stdin.write(data.encode("utf-8"))
                self._proc.stdin.flush()
            except OSError as e:
                self._kill()
                raise ExifToolError(f"ExifTool exited unexpectedly ({e})")
            timeout = READ_TIMEOUT if timeout is None else float(timeout)
            deadline = time.monotonic() + timeout
            out_lines = []
            marker = f"{{ready{n}}}".encode()
            while True:
                try:
                    line = self._out_q.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    self._kill()
                    raise ExifToolError(f"ExifTool did not finish within {timeout:.0f} s and was stopped "
                                        f"(the file may be damaged or the drive not responding)")
                if line is None:
                    self._kill()
                    raise ExifToolError("ExifTool exited unexpectedly")
                if line.strip() == marker:
                    break
                out_lines.append(line)
            err_lines: List[str] = []
            err_deadline = time.monotonic() + ERR_GRACE
            while True:
                try:
                    raw = self._err_q.get(timeout=max(0.0, err_deadline - time.monotonic()))
                except queue.Empty:
                    # the stderr marker never came: restart rather than let late lines
                    # be read as the next command's
                    log.debug("ExifTool stderr marker %d missing; restarting", n)
                    self._kill()
                    break
                if raw is None:
                    break  # the process ended; the next command starts a new one
                line = raw.decode("utf-8", "replace")
                m = _ERR_MARK.match(line.strip())
                if m:
                    if int(m.group(1)) == n:
                        break
                    err_lines = []  # an earlier command's leftovers: not ours
                    continue
                err_lines.append(line)
            return b"".join(out_lines).decode("utf-8", "replace"), "".join(err_lines)

    def read_json(self, path: str, extra: Sequence[str] = ()) -> Dict:
        out, err = self.execute("-j", "-struct", "-G1", "-n", *extra, files=[path])
        try:
            data = json.loads(out)
            # ExifTool prints number-looking text unquoted, so a title "1.50"
            # would read as 1.5. The same data with number text kept verbatim
            # goes along as .text for text fields (see metadata.normalize).
            text = json.loads(out, parse_float=str, parse_int=str)
        except json.JSONDecodeError:
            raise ExifToolError(err.strip() or "ExifTool returned no data")
        if not data:
            return ExifData()
        return ExifData(data[0], text=text[0] if isinstance(text[0], dict) else None)

    def write(self, path: str, args: Sequence[str], require_change: bool = True) -> str:
        """Write tags; raise ExifToolError unless the file was updated (see
        check_write). Returns ExifTool's stderr (warnings: parse_warnings)."""
        out, err = self.execute("-overwrite_original", "-m", *args, files=[path], timeout=max(WRITE_TIMEOUT, READ_TIMEOUT))
        check_write(out, err, require_change)
        return err


_POOL_SIZE = 3
_pool: List["ExifTool"] = []
_pool_pid = 0
_pool_lock = threading.Lock()
_rr = 0


def get() -> ExifTool:
    """A shared ExifTool process from a small pool (per OS process).

    ExifTool processes are expensive (one Perl interpreter each), so threads share
    a pool of three instead of one per thread; each instance serializes its own
    commands. The pool is rebuilt after a fork so a child never shares pipes
    with its parent."""
    global _pool, _pool_pid, _rr
    with _pool_lock:
        if _pool_pid != os.getpid():
            _pool = []
            _pool_pid = os.getpid()
        if len(_pool) < _POOL_SIZE:
            from .settings import load_settings
            et = ExifTool(load_settings().get("advanced", {}).get("exiftoolPath") or None)
            _pool.append(et)
            return et
        # prefer an idle instance; otherwise round-robin
        for et in _pool:
            if not et._lock.locked():
                return et
        _rr = (_rr + 1) % len(_pool)
        return _pool[_rr]


_warm = {"started": False}


def warm_async() -> None:
    """Start the pool's ExifTool processes in the background (once per OS process), so the
    first photo opened doesn't pay for the Perl start-ups."""
    with _pool_lock:
        if _warm["started"] and _pool_pid == os.getpid():
            return
        _warm["started"] = True

    def run():
        try:
            seen = []
            for _ in range(_POOL_SIZE):
                et = get()
                if et in seen:
                    break
                seen.append(et)
                if not et.cmd:
                    break
                with et._lock:
                    if et._proc is None or et._proc.poll() is not None:
                        et._start()
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="exiftool-warm").start()


def close_all() -> None:
    _warm["started"] = False
    with _pool_lock:
        if _pool_pid == os.getpid():
            for et in _pool:
                try:
                    et.close()
                except Exception:
                    pass
            _pool.clear()


import atexit  # noqa: E402

atexit.register(close_all)
