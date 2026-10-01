"""Process-wide gate for full-resolution decodes.

A 50 MP / 600 MB scan needs several hundred MB to a few GB while it is decoded, so the
server must never decode many of them at once (filmstrip thumbnails, prefetch, the
existing-text analysis and crops all need a full decode). Every full decode outside
saving runs inside :func:`slot`:

* at most ``MAX_SLOTS`` decodes run at once, and a second one only when the estimated
  peak memory of everything in flight (plus the array the photo cache pins) fits the
  budget — so two small photos decode in parallel, two huge ones never do;
* waiters are served by priority (the current photo first, then prefetch / pre-flight,
  thumbnails last), in arrival order within a priority.

:func:`check_budget` refuses images whose decoded size is too large for this machine
(which also covers decompression bombs, since Pillow's pixel limit is turned off).
"""
from __future__ import annotations

import heapq
import itertools
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, List, Optional, Tuple

# priorities: lower is served first
CURRENT = 0      # the photo on screen: proxy, existing-text analysis, crops, erase preview
PREFETCH = 1     # neighbours, batch pre-flight
THUMB = 2        # filmstrip thumbnails

MAX_SLOTS = 2
PEAK_FACTOR = 3.0          # decode peak ~ 3x the decoded array (codec buffers, upright copy)
BUDGET_SHARE = 0.3         # share of the available RAM the concurrent decodes may use
MAX_DECODED_BYTES = 4 * 1024 ** 3
MAX_DECODED_SHARE = 0.25   # of physical RAM


log = logging.getLogger(__name__)

_DEFAULT_TOTAL = 8 * 1024 ** 3
_DEFAULT_AVAIL = 4 * 1024 ** 3


def _mem_windows() -> Optional[Tuple[int, int]]:
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
        return None
    return int(st.ullTotalPhys), int(st.ullAvailPhys)


def _mem_macos() -> Optional[Tuple[int, int]]:
    """hw.memsize for the total; free + inactive + speculative + purgeable pages from
    vm_stat for what is available (what the OS hands out without swapping)."""
    import re
    import subprocess
    total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                               timeout=5, check=True).stdout.strip())
    avail = None
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5, check=True).stdout
        m = re.search(r"page size of (\d+) bytes", out)
        page = int(m.group(1)) if m else 4096
        pages = 0
        for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
            m = re.search(rf"^{key}:\s+(\d+)", out, re.M)
            if m:
                pages += int(m.group(1))
        if pages:
            avail = pages * page
    except Exception:
        log.debug("vm_stat failed", exc_info=True)
    if avail is None:
        avail = total // 2
    return total, min(avail, total)


def _mem_sysconf() -> Optional[Tuple[int, int]]:
    page = os.sysconf("SC_PAGE_SIZE")
    total = page * os.sysconf("SC_PHYS_PAGES")
    try:
        avail = page * os.sysconf("SC_AVPHYS_PAGES")
    except (ValueError, OSError):
        avail = total // 2
    return int(total), int(avail)


def system_memory() -> Tuple[int, int]:
    """(physical, available) RAM in bytes. psutil when installed; otherwise
    GlobalMemoryStatusEx on Windows, sysctl/vm_stat on macOS, sysconf elsewhere;
    8 GB / 4 GB when nothing answers. The one memory probe for the whole app."""
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return int(vm.total), int(vm.available)
    except Exception:
        pass
    now = time.monotonic()
    hit = _fallback_cache.get("v")
    if hit is not None and now - hit[0] < _FALLBACK_TTL:
        return hit[1]
    r = _system_memory_fallback()
    _fallback_cache["v"] = (now, r)
    return r


_FALLBACK_TTL = 2.0   # the gate asks often; vm_stat is a process start on macOS
_fallback_cache: dict = {}


def _system_memory_fallback() -> Tuple[int, int]:
    probes = []
    if sys.platform == "win32":
        probes.append(_mem_windows)
    elif sys.platform == "darwin":
        probes.append(_mem_macos)
    probes.append(_mem_sysconf)
    for fn in probes:
        try:
            r = fn()
        except Exception:
            log.debug("memory probe %s failed", fn.__name__, exc_info=True)
            continue
        if r and r[0] > 0 and r[1] >= 0:
            return r
    return _DEFAULT_TOTAL, _DEFAULT_AVAIL


_mem = system_memory   # old name


def decoded_bytes(info) -> int:
    """Size of the decoded array for an :class:`~photoband.imageio.ImageInfo`: what
    the codec really allocates when the probe knows it (TIFF: every sample at its
    item size), at least the size of the array read_pixels returns."""
    bps = 2 if info.dtype == "uint16" else 1
    ch = max(1, int(info.channels or 1))
    est = int(info.width) * int(info.height) * ch * bps
    return max(est, int(getattr(info, "decode_bytes", 0) or 0))


def max_decoded_bytes() -> int:
    total, _ = _mem()
    return int(min(MAX_DECODED_BYTES, total * MAX_DECODED_SHARE))


def check_budget(info) -> None:
    """Raise ImageError when decoding ``info`` would take too much memory."""
    from .imageio import ImageError
    n = decoded_bytes(info)
    if n > max_decoded_bytes():
        raise ImageError(
            f"This image is too large to open ({n / 2 ** 20:,.0f} MB when decoded; this computer allows up to "
            f"{max_decoded_bytes() / 2 ** 20:,.0f} MB). {os.path.basename(info.path)} is {info.width} x "
            f"{info.height} pixels.")


class _Gate:
    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._active: List[int] = []            # estimated peak bytes of running decodes
        self._waiting: List[Tuple[int, int, int]] = []   # heap of (prio, seq, est)
        self._seq = itertools.count()
        self._pinned = 0                        # bytes held by the full-array cache
        self._release_pinned: Optional[Callable[[], None]] = None
        self._local = threading.local()
        self._fg = 0                            # current-photo work in progress (see foreground)

    # the photo cache tells the gate what it holds, and how to drop it under pressure
    def set_pinned(self, nbytes: int, release: Optional[Callable[[], None]] = None) -> None:
        with self._cv:
            self._pinned = max(0, int(nbytes))
            self._release_pinned = release if nbytes else None
            self._cv.notify_all()

    def _budget(self) -> int:
        _, avail = _mem()
        # available RAM already excludes what is in flight / pinned; add it back
        return int((avail + sum(self._active) + self._pinned) * BUDGET_SHARE)

    def _fits(self, est: int, prio: int) -> bool:
        if prio > CURRENT and self._fg:
            return False      # background decodes wait while the photo on screen is being prepared
        if not self._active:
            return True       # one decode always runs (the size check keeps it sane)
        if len(self._active) >= MAX_SLOTS:
            return False
        return sum(self._active) + est + self._pinned <= self._budget()

    @contextmanager
    def slot(self, est: int, prio: int = CURRENT) -> Iterator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            # nested (a decode inside a gated section of the same thread): already counted
            yield
            return
        me = (int(prio), next(self._seq), int(est))
        release = None
        with self._cv:
            heapq.heappush(self._waiting, me)
            while not (self._waiting[0] is me and self._fits(me[2], me[0])):
                self._cv.wait(timeout=1.0)
            heapq.heappop(self._waiting)
            # nothing else running but the cache pins a big array that together with this
            # decode would not fit: let the cache drop it (it is only a speed-up)
            if (self._pinned and self._release_pinned is not None
                    and self._pinned + me[2] > self._budget()):
                release = self._release_pinned
            self._active.append(me[2])
            self._cv.notify_all()
        if release is not None:
            try:
                release()
            except Exception:
                pass
        self._local.depth = 1
        try:
            yield
        finally:
            self._local.depth = 0
            with self._cv:
                self._active.remove(me[2])
                self._cv.notify_all()

    @contextmanager
    def foreground(self) -> Iterator[None]:
        with self._cv:
            self._fg += 1
        try:
            yield
        finally:
            with self._cv:
                self._fg -= 1
                self._cv.notify_all()

    def stats(self) -> dict:
        with self._cv:
            return {"active": len(self._active), "waiting": len(self._waiting), "pinned": self._pinned}


_gate = _Gate()


@contextmanager
def slot(info, prio: int = CURRENT) -> Iterator[None]:
    """Hold a decode slot for ``info`` (checks the size budget first)."""
    check_budget(info)
    with _gate.slot(int(decoded_bytes(info) * PEAK_FACTOR), prio):
        yield


def foreground() -> "contextmanager":
    """Mark work for the photo on screen (decode, proxy, analysis): no background decode
    starts meanwhile, so it gets the CPU and the memory."""
    return _gate.foreground()


def set_pinned(nbytes: int, release: Optional[Callable[[], None]] = None) -> None:
    _gate.set_pinned(nbytes, release)


def stats() -> dict:
    return _gate.stats()
