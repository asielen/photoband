"""State on disk that several Photoband processes share (two windows, a relaunched app, batch
workers): batch journals, settings, drafts.

Two rules keep a process from acting on, or writing back, an out-of-date copy:

* A read-modify-write of a shared file runs under :func:`interprocess_lock` and starts from the
  file as it is on disk *now*, never from a copy kept in memory since an earlier read.
* A copy kept in memory is revalidated against the file's :func:`stamp` before it is used: when
  another process replaced the file since, the copy is read again.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from typing import Iterator, Optional, Tuple

from .util import replace_with_retry

log = logging.getLogger(__name__)

Stamp = Tuple[int, int, int]


def stamp(path: str) -> Optional[Stamp]:
    """Identity of the file's current version: (file id, modified ns, size), None when missing.
    Shared files are replaced atomically (a new file each time), so a write by any process
    changes the file id even when the size and the modified time (coarse on some file systems)
    stay the same."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _try_lock(fd: int) -> bool:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        log.debug("could not unlock", exc_info=True)


@contextlib.contextmanager
def interprocess_lock(lock_path: str, timeout: float = 15.0) -> Iterator[bool]:
    """Serialize a read-modify-write of a shared file across processes and threads: an OS lock
    (LockFileEx / flock) on ``lock_path``, which is kept (never deleted, so every process locks
    the same file). Released by the OS when its process dies, so it is never left stale. Held for
    milliseconds. Not reentrant. Yields True when held; if it is still held by another process
    after ``timeout`` (it should never be) the work goes ahead unlocked rather than failing a
    save or a run."""
    fd = None
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        log.warning("could not open %s; going ahead without it", lock_path, exc_info=True)
    held = False
    if fd is not None:
        t0 = time.monotonic()
        delay = 0.002
        while not (held := _try_lock(fd)):
            if time.monotonic() - t0 >= timeout:
                log.warning("%s is held by another process; going ahead without it", lock_path)
                break
            time.sleep(delay)
            delay = min(delay * 2, 0.05)
    try:
        yield held
    finally:
        if fd is not None:
            if held:
                _unlock(fd)
            os.close(fd)


def write_json(path: str, obj) -> None:
    """Atomic JSON write of a file other processes read: on Windows a reader holding the old
    file open makes the replace fail for a moment (a sharing violation), so it is retried."""
    data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
