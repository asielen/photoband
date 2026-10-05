"""Batch mode: pre-flight, staged jobs, a parallel save queue, a journal for resume,
and restore of originals.

Flow: the UI lays out every photo with the same engine as single-photo mode and
*stages* each job (layout + text-layer tiles + editor state) here. Workers pick
staged jobs and run the same verified save as a single photo. Every state
change is written to a journal so a killed run can resume without writing any
file twice.
"""
from __future__ import annotations

import concurrent.futures as cf
import contextlib
import copy
import csv
import io
import logging
import multiprocessing
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional

from . import paths
from .shared_state import interprocess_lock, stamp
from .shared_state import write_json as atomic_write_json
from .util import (LockBusy, PidLock, _pid_alive, atomic_write_bytes, canonical_path, file_identity, file_lock,
                   fsync_dir, read_json, replace_with_retry, sweep_temp, temp_prefix)

log = logging.getLogger(__name__)

# "held": flagged and held back for review; "blocked": pre-flight could not save it;
# "notPrepared": never staged and could not be prepared again on resume.
TERMINAL = {"done", "failed", "skipped", "changed", "excluded", "held", "blocked", "notPrepared"}
# pre-flight statuses of photos that are not saved -> journal state
_UNSAVED_STATE = {"skipped": "skipped", "blocked": "blocked", "error": "blocked", "excluded": "excluded",
                  "flagged": "held", "held": "held", "pending": "notPrepared",
                  "unchecked": "skipped"}


class BatchBusy(Exception):
    """The batch is being run by another Photoband window (or process)."""


class BatchNotFound(KeyError):
    """No batch with that id (or an invalid id)."""


class BatchStopped(Exception):
    """A run or retry was requested before the latest Stop (its stop epoch is out of date): the
    Stop wins, whatever order the requests reach the server in."""


def _bdir(bid: str) -> str:
    # ASCII only (str.isalnum would also take "é", "²", "５"); real ids: YYYYmmdd-HHMMSS-<6 hex>
    if not isinstance(bid, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", bid):
        raise BatchNotFound(bid)
    return paths.sub("batches", bid)


def _skip_dir_rules(saving: Optional[Dict[str, Any]]):
    """Folder names and absolute folders a scan must not descend into: backups and outputs."""
    if saving is None:
        try:
            from .settings import load_settings
            saving = load_settings().get("saving", {})
        except Exception:
            saving = {}
    names = {"_originals", "captioned"}
    if saving.get("subfolderName"):
        names.add(str(saving["subfolderName"]))
    names = {n.lower() for n in names}
    absolute = set()
    for k in ("backupFolder", "fixedFolder"):
        if saving.get(k):
            absolute.add(canonical_path(saving[k]))
    return names, absolute


def scan_folder(folder: str, include_sub: bool, saving: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Supported photos in ``folder``. Never lists backup folders (_originals, the configured backup
    folder) or copy destinations (the output subfolder, the fixed output folder)."""
    from .imageio import SUPPORTED_EXT
    out = []
    skip_names, skip_abs = _skip_dir_rules(saving)
    if include_sub:
        walker = os.walk(folder)
    else:
        walker = [(folder, [], os.listdir(folder))]
    for root, dirs, files in walker:
        dirs[:] = [d for d in dirs if d.lower() not in skip_names and not d.startswith(".")
                   and canonical_path(os.path.join(root, d)) not in skip_abs
                   and not os.path.isfile(os.path.join(root, d, ".photoband-backups"))]
        if root != folder and canonical_path(root) in skip_abs:
            continue
        for fn in sorted(files, key=str.lower):
            if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in SUPPORTED_EXT:
                continue
            p = os.path.join(root, fn)
            if os.path.isfile(p):
                st = os.stat(p)
                out.append({"path": p, "name": os.path.relpath(p, folder), "size": st.st_size,
                            "mtime": st.st_mtime_ns})
    out.sort(key=lambda e: e["name"].lower())
    return out


# -- worker processes ------------------------------------------------------------------
# Workers are *spawned* (never forked: a forked worker inherits the server's listening socket
# and every other open descriptor) and die with the app, so a killed run cannot leave a worker
# that replaces a file after the relaunched app has re-queued it:
#   Linux    PR_SET_PDEATHSIG(SIGKILL) set in the worker. It fires when the parent *thread* that
#            created the worker exits; the pool spawns its workers from Batch._run's thread, which
#            lives until the pool has shut down.
#   Windows  the parent puts each worker in a kill-on-close job object; the worker also watches
#            the parent's process handle.
#   all      a watchdog thread in the worker, and a parent check right before any file is placed
#            (save's replace / exclusive place), so an orphan aborts instead of writing.
_PARENT_PID = 0
_parent_handle = None


def _parent_alive() -> bool:
    if not _PARENT_PID:
        return True
    if os.name == "nt":  # pragma: no cover - Windows only
        if _parent_handle:
            try:
                import ctypes
                return ctypes.windll.kernel32.WaitForSingleObject(_parent_handle, 0) == 0x102  # WAIT_TIMEOUT
            except Exception:
                return True
        return _pid_alive(_PARENT_PID)
    return os.getppid() == _PARENT_PID


class ParentGone(Exception):
    pass


def _check_parent() -> None:
    if not _parent_alive():
        raise ParentGone("Photoband quit while this photo was being saved; nothing was written.")


def _watchdog() -> None:  # pragma: no cover - runs in the worker
    while True:
        if not _parent_alive():
            os._exit(1)
        time.sleep(0.5)


def _worker_init(parent_pid: int) -> None:  # pragma: no cover - runs in the worker
    global _PARENT_PID, _parent_handle
    _PARENT_PID = parent_pid
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGKILL))  # PR_SET_PDEATHSIG
        except Exception:
            pass
    elif os.name == "nt":
        try:
            import ctypes
            _parent_handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
        except Exception:
            _parent_handle = None
    if not _parent_alive():  # the app died before the death signal was armed
        os._exit(1)
    threading.Thread(target=_watchdog, daemon=True, name="parent-watchdog").start()
    # the last check before a file is placed: an orphan must never replace a file
    from . import save as savemod
    real_replace, real_place = savemod.replace_with_retry, savemod.place_exclusive
    try:  # tests: hold every worker right before it places a file
        delay = float(os.environ.get("PHOTOBAND_TEST_WORKER_DELAY") or 0)
    except ValueError:
        delay = 0.0

    def guarded_replace(src, dst, *a, **k):
        if delay:
            time.sleep(delay)
        _check_parent()
        return real_replace(src, dst, *a, **k)

    def guarded_place(src, dst, *a, **k):
        if delay:
            time.sleep(delay)
        _check_parent()
        return real_place(src, dst, *a, **k)
    savemod.replace_with_retry = guarded_replace
    savemod.place_exclusive = guarded_place


def _attach_workers(ex: cf.ProcessPoolExecutor, seen: set) -> None:
    """Windows: every worker joins the kill-on-close job object (killed when the app exits)."""
    if os.name != "nt":  # pragma: no cover - Windows only below
        return
    try:
        from .exiftool import _attach_kill_on_close_job
        for pid, proc in list((getattr(ex, "_processes", None) or {}).items()):
            if pid in seen:
                continue
            po = getattr(proc, "_popen", None)
            if po is not None and getattr(po, "_handle", None):
                _attach_kill_on_close_job(po)
                seen.add(pid)
    except Exception:
        pass


def _new_pool(n: int) -> cf.ProcessPoolExecutor:
    return cf.ProcessPoolExecutor(max_workers=n, mp_context=multiprocessing.get_context("spawn"),
                                  initializer=_worker_init, initargs=(os.getpid(),))


# -- memory ----------------------------------------------------------------------------
WORK_FACTOR = 6        # peak working memory of one save, in multiples of the decoded image
MEM_SHARE = 0.6        # share of the available memory the batch may use


def available_memory() -> int:
    """Bytes of memory available now (the app-wide probe in decodegate)."""
    from .decodegate import system_memory
    return system_memory()[1]


def decoded_bytes(path: str) -> int:
    """Size of the decoded image, from a header probe (0 when the file can't be probed)."""
    try:
        from .decodegate import decoded_bytes as _decoded
        from .imageio import probe
        return _decoded(probe(path))
    except Exception:
        log.debug("probe failed for %s", path, exc_info=True)
        return 0


def auto_workers(decoded: int, avail: Optional[int] = None, cpu: Optional[int] = None) -> int:
    """n = max(1, min(cpu, 4, floor(avail × 0.6 / (decoded × 6))))"""
    cpu = cpu or os.cpu_count() or 2
    avail = available_memory() if avail is None else avail
    if decoded <= 0:
        return max(1, min(cpu, 4))
    return max(1, min(cpu, 4, int(avail * MEM_SHARE // (decoded * WORK_FACTOR))))


def _zombie(pid: int) -> bool:
    """Linux: an exited process not reaped yet (after a hard kill, until init reaps it) still
    answers kill(pid, 0) but holds nothing."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] in ("Z", "X")
    except (OSError, IndexError):
        return False


def _lock_holder(lock_path: str) -> Optional[int]:
    """PID of a live process holding this PidLock file, else None."""
    info = read_json(lock_path)
    if not isinstance(info, dict):
        return None
    try:
        pid = int(info.get("pid", 0))
        if time.time() - float(info.get("time", 0)) > 6 * 3600:
            return None
    except (TypeError, ValueError):
        return None
    if _pid_alive(pid) and _zombie(pid):
        # a killed worker not reaped yet: its locks are stale, but PidLock (kill(pid, 0))
        # would treat them as held and the next save of this file would fail as "busy"
        try:
            os.unlink(lock_path)
        except OSError:
            pass
        return None
    return pid if _pid_alive(pid) else None


class Batch:
    """One batch run (persisted in app data/batches/<id>)."""

    def __init__(self, bid: str):
        self.id = bid
        self.dir = _bdir(bid)
        self.jpath = os.path.join(self.dir, "journal.json")
        self._lock = threading.RLock()
        # The journal is shared with every other Photoband process that opens this batch (another
        # window, a relaunched app). self.data is this process's copy of it: refresh() reads it
        # again when another process replaced the file since (_jstamp: the version self.data is),
        # and every change is a _txn(): under the journal lock, applied to the file's current
        # version, so no process writes back a stale copy over another's newer journal.
        self._jstamp = stamp(self.jpath)
        self.data: Dict[str, Any] = read_json(self.jpath) or {}
        self._txn_depth = 0
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._staging_done = threading.Event()
        self._run_lock: Optional[PidLock] = None
        # Stop is persisted as an epoch in stop.json (not in the journal, which the process running
        # the batch rewrites from memory). Every Stop raises it; a run/retry request carries the
        # epoch it was issued under and is refused when a Stop came since. A run started under
        # _run_epoch stops as soon as the file shows a newer one (a Stop from any process).
        self._stop_path = os.path.join(self.dir, "stop.json")
        self._run_epoch: Optional[int] = None
        self._start_lock = threading.Lock()
        self._stop_seen_t = 0.0

    # -- persistence ---------------------------------------------------------
    def refresh(self) -> bool:
        """Read the journal again if another process wrote it since this copy was read or
        written (a run, Stop, retry, restore or discard in another Photoband window). True when
        reloaded. Cheap (one stat) when nothing changed."""
        with self._lock:
            for _ in range(3):
                cur = stamp(self.jpath)
                if cur is None or cur == self._jstamp:
                    return False
                d = read_json(self.jpath)
                if stamp(self.jpath) != cur:
                    continue   # replaced while it was read: read the newer one
                if not isinstance(d, dict) or not d:
                    return False
                self.data = d
                self._jstamp = cur
                return True
            return False

    @contextlib.contextmanager
    def _txn(self) -> Iterator[Dict[str, Any]]:
        """A change to the journal: under this process's lock and the journal lock shared with
        other processes, starting from the journal's current version on disk. Nested: one lock."""
        with self._lock:
            if self._txn_depth:
                self._txn_depth += 1
                try:
                    yield self.data
                finally:
                    self._txn_depth -= 1
                return
            with interprocess_lock(os.path.join(self.dir, "journal.lock")):
                self.refresh()
                self._txn_depth = 1
                try:
                    yield self.data
                finally:
                    self._txn_depth = 0

    def _write(self):
        with self._txn():
            self.data["updated"] = time.time()
            self.data["pid"] = os.getpid()   # the last writer (see incomplete_batches)
            atomic_write_json(self.jpath, self.data)
            # still under the journal lock: no other process wrote in between
            self._jstamp = stamp(self.jpath)

    def stop_epoch(self) -> int:
        """How many times this batch was stopped (by any Photoband process)."""
        d = read_json(self._stop_path)
        try:
            return int(d.get("epoch", 0)) if isinstance(d, dict) else 0
        except (TypeError, ValueError):
            return 0

    def _stop_requested(self) -> bool:
        """Stop was pressed since the current run started (here or in another process)."""
        if self._cancel.is_set():
            return True
        if self._run_epoch is not None and self.stop_epoch() != self._run_epoch:
            self._cancel.set()
            return True
        return False

    def settings_snapshot(self) -> Optional[Dict[str, Any]]:
        """The app settings the batch was created with: each of its photos (also one staged later,
        retried or resumed) is saved with these, whatever is changed in Settings meanwhile."""
        s = read_json(os.path.join(self.dir, "settings.json"))
        return s if isinstance(s, dict) and s else None

    @classmethod
    def create(cls, meta: Dict[str, Any], unsaved: Optional[List[Dict[str, Any]]] = None,
               settings: Optional[Dict[str, Any]] = None) -> "Batch":
        """meta.count photos will be staged as indices 0..count-1. ``unsaved``: the rest of the
        pre-flight plan (skipped, blocked, excluded, held back), journaled now with their reasons
        so the summary and the report list every photo."""
        bid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        b = cls(bid)
        count = int(meta.get("count", 0))
        entries: Dict[str, Any] = {}
        for k, u in enumerate(unsaved or []):
            idx = count + k
            entries[str(idx)] = {"index": idx, "path": u.get("path", ""),
                                 "state": _UNSAVED_STATE.get(u.get("status", ""), "skipped"),
                                 "error": "; ".join(u.get("reasons") or []), "out": "", "backup": "", "notes": []}
        b.data = {"id": bid, "created": time.time(), "meta": meta, "entries": entries, "state": "staging",
                  "expected": count + len(entries)}
        if settings:
            atomic_write_json(os.path.join(b.dir, "settings.json"), settings)
        b._write()
        return b

    def stage(self, index: int, job: Dict[str, Any], tiles: List[Dict[str, Any]]) -> None:
        """job: SaveRequest-like dict; tiles: [{x, y, png(bytes)}]."""
        jd = os.path.join(self.dir, f"{index:05d}")
        os.makedirs(jd, exist_ok=True)
        tl = []
        for i, t in enumerate(tiles):
            fn = f"tile{i:03d}.png"
            atomic_write_bytes(os.path.join(jd, fn), t["png"])
            tl.append({"x": int(t["x"]), "y": int(t["y"]), "file": fn})
        job = dict(job)
        job["tiles"] = tl
        atomic_write_json(os.path.join(jd, "job.json"), job)
        with self._txn():
            # staged after Stop (it was being prepared then): kept, not run. Retry/resume runs it.
            stopped = self._stop_requested()
            self.data["entries"][str(index)] = {"index": index, "path": job["path"],
                                                "state": "cancelled" if stopped else "staged",
                                                "error": "Stopped before it was saved" if stopped else "",
                                                "out": "", "backup": "", "notes": [], "job": True}
            self._write()

    def mark(self, index: int, state: str, **kw) -> None:
        with self._txn():
            e = self.data["entries"].setdefault(str(index), {"index": index, "path": kw.get("path", "")})
            if e.get("path"):
                kw.pop("path", None)   # an entry's photo never changes (restore writes to it)
            e["state"] = state
            e.update(kw)
            self._write()

    def planned_paths(self) -> set:
        """Canonical paths of every photo this batch was created with (its files and the
        pre-flight plan): the only files it may ever write back to."""
        meta = self.data.get("meta", {}) or {}
        out = set()
        for p in list(meta.get("files") or []) + [x.get("path") for x in (meta.get("plan") or [])
                                                  if isinstance(x, dict)]:
            if isinstance(p, str) and p:
                out.add(canonical_path(p))
        return out

    def _jobdir(self, idx: int) -> str:
        return os.path.join(self.dir, f"{int(idx):05d}")

    def has_job(self, idx: int) -> bool:
        return os.path.isfile(os.path.join(self._jobdir(idx), "job.json"))

    def _backup_json(self, idx: int) -> str:
        b = (read_json(os.path.join(self._jobdir(idx), "backup.json")) or {}).get("backup") or ""
        return b if b and os.path.isfile(b) else ""

    def summary(self) -> Dict[str, Any]:
        self.refresh()
        with self._lock:
            ents = [dict(e) for e in self.data.get("entries", {}).values()]
            expected = self.data.get("expected", len(ents))
            meta = self.data.get("meta", {})
            state = self.data.get("state")
            staging_complete = bool(self.data.get("stagingComplete"))
        counts: Dict[str, int] = {}
        # photos "Restore originals" would put back: the UI shows this number, never its own guess
        restorable = 0
        for e in ents:
            counts[e["state"]] = counts.get(e["state"], 0) + 1
            if e["state"] != "restored":
                if e.get("backup"):
                    restorable += 1
                elif e["state"] in ("failed", "changed", "cancelled"):
                    # a replace that happened although the entry says otherwise (e.g. finished by an
                    # earlier attempt): its backup is journaled next to the job
                    if os.path.isfile(os.path.join(self._jobdir(e["index"]), "backup.json")):
                        restorable += 1
            if e["state"] in ("failed", "cancelled"):
                e["hasJob"] = self.has_job(e["index"])
        return {"id": self.id, "state": state, "counts": counts, "expected": expected, "meta": meta,
                "stagingComplete": staging_complete, "error": self.data.get("error", ""),
                "entries": sorted(ents, key=lambda e: e["index"]), "canRestore": restorable > 0,
                "restorable": restorable, "epoch": self.stop_epoch()}

    # -- running --------------------------------------------------------------
    def staging_complete(self):
        with self._txn():
            self.data["stagingComplete"] = True
            self._write()
        self._staging_done.set()

    def folders(self) -> List[str]:
        out = set()
        for e in self.data.get("entries", {}).values():
            for k in ("path", "dest", "out"):
                if e.get(k):
                    out.add(os.path.dirname(e[k]))
        for p in self.data.get("meta", {}).get("files", []) or []:
            out.add(os.path.dirname(p))
        return sorted(out)

    def _check_epoch(self, epoch: Optional[int]) -> int:
        """The current stop epoch. BatchStopped when ``epoch`` (the one a run/retry request was
        issued under) is out of date. None: not checked (callers inside this process)."""
        cur = self.stop_epoch()
        if epoch is not None and int(epoch) != cur:
            raise BatchStopped("The batch was stopped.")
        return cur

    def _requeue(self) -> None:
        """Failed and stopped photos with a staged job run again (call under self._lock)."""
        for e in self.data["entries"].values():
            if e["state"] in ("failed", "cancelled") and self.has_job(e["index"]):
                e["state"] = "retry"
                e["error"] = ""
        self.data["stagingComplete"] = True

    def start(self, workers: Optional[int] = None, epoch: Optional[int] = None, requeue: bool = False) -> None:
        """Run the queue. ``epoch``: the stop epoch the request was issued under; a Stop since then
        wins (BatchStopped) and nothing is changed. ``requeue``: failed and stopped photos run
        again, changed only once the run is sure to start (the run lock is ours, no Stop since)."""
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                if requeue:
                    with self._txn():
                        self._check_epoch(epoch)
                        self._requeue()
                        self._write()
                    self._staging_done.set()
                return
            lk = PidLock(os.path.join(self.dir, "run.lock"))
            try:
                lk.acquire()
            except LockBusy:
                # held by an app that was just killed (not reaped yet): take it over
                if not isinstance(read_json(lk.path), dict) or _lock_holder(lk.path) is not None:
                    raise BatchBusy("This batch is already running in another Photoband window.")
                try:
                    os.unlink(lk.path)
                except OSError:
                    pass
                try:
                    lk.acquire()
                except LockBusy:
                    raise BatchBusy("This batch is already running in another Photoband window.")
            self._run_lock = lk
            try:
                try:
                    sweep_temp(self.folders())  # temp files left by a killed run
                except Exception:
                    log.debug("temp sweep failed", exc_info=True)
                with self._txn():
                    # checked and cleared under the lock cancel() takes: a Stop is either before
                    # this (refused here) or after it (sets the flag the run loop reads)
                    cur = self._check_epoch(epoch)
                    before = copy.deepcopy(self.data)
                    if requeue:
                        self._requeue()
                    self.data["state"] = "running"
                    self.data.pop("error", None)
                    try:
                        self._write()
                    except BaseException:
                        self.data = before
                        raise
                    self._run_epoch = cur
                    self._cancel.clear()
                if self.data.get("stagingComplete"):
                    self._staging_done.set()
                self._thread = threading.Thread(target=self._run_locked, args=(workers,), daemon=True)
                self._thread.start()
            except BaseException:
                # never keep the run lock of a run that did not start
                self._release_run_lock()
                raise

    def _release_run_lock(self) -> None:
        lk, self._run_lock = self._run_lock, None
        if lk:
            try:
                lk.release()
            except Exception:
                log.debug("could not release the run lock", exc_info=True)

    def _run_locked(self, workers: Optional[int]):
        try:
            # again when photos were queued (a retry) after this pass found the queue empty
            while self._run(workers):
                pass
        except BaseException as e:
            # e.g. the disk filled up while the journal was written: stop, and say so (the UI
            # would otherwise poll a batch that stays "running" forever)
            log.error("batch %s stopped", self.id, exc_info=True)
            msg = f"The batch stopped: {e.strerror or e}" if isinstance(e, OSError) else f"The batch stopped: {e}"
            with self._txn():
                for ent in self.data.get("entries", {}).values():
                    if ent.get("state") in ("staged", "retry", "running"):
                        ent["state"] = "cancelled"
                        ent["error"] = "Stopped: " + msg[len("The batch stopped: "):]
                self.data["state"] = "cancelled"
                self.data["interrupted"] = True
                self.data["error"] = msg
                try:
                    self._write()
                except Exception:
                    log.debug("could not journal the stop", exc_info=True)
        finally:
            self._release_run_lock()

    def cancel(self):
        """Stop after the photos being saved now: no new photo starts, the ones in progress
        finish (each is written to a temp file and put in place atomically, so none is left half
        written) and queued ones become "cancelled" (Retry runs them). Photos staged after this
        are kept as "cancelled" too. Not running here (stopped while photos were still being
        prepared for a retry or resume): the queue is stopped right away.

        The Stop is persisted first (stop.json): a run or retry requested before it is refused
        even when it reaches the server later, and a run in another Photoband process stops too."""
        with self._txn():
            self._cancel.set()
            atomic_write_json(self._stop_path, {"epoch": self.stop_epoch() + 1, "time": time.time()})
            if self._thread and self._thread.is_alive():
                return
            if self.run_elsewhere():
                # running in another Photoband window: it reads stop.json, stops and journals the
                # outcome; this window's summary() reads that journal (refresh), never its own copy
                return
            n = 0
            for e in self.data.get("entries", {}).values():
                if e.get("state") in ("staged", "retry"):
                    e["state"] = "cancelled"
                    e["error"] = "Stopped before it was saved"
                    n += 1
            if n or self.data.get("state") in ("running", "staging"):
                self.data["state"] = "cancelled"
            self._write()

    def run_elsewhere(self) -> bool:
        """Another live Photoband process is running this batch (holds its run lock)."""
        return _lock_holder(os.path.join(self.dir, "run.lock")) not in (None, os.getpid())

    def _pending(self) -> List[int]:
        with self._lock:
            return sorted(int(k) for k, e in self.data["entries"].items() if e["state"] in ("staged", "retry"))

    def _run(self, workers: Optional[int]) -> bool:
        """One pass over the queue. True when photos were queued again after it ended (run again)."""
        waiting = set(self._recover_running())
        cpu = os.cpu_count() or 2
        n = max(1, int(workers)) if workers else max(1, min(cpu, 4))
        budget = available_memory() * MEM_SHARE
        budget_t = time.time()
        need: Dict[int, int] = {}     # idx -> working memory of that save
        running: Dict[cf.Future, int] = {}
        attached: set = set()
        last_wait = 0.0
        with _new_pool(n) as ex:
            while True:
                if self._cancel.is_set():
                    break
                if time.time() - self._stop_seen_t > 0.25:
                    # a Stop pressed in another Photoband process (stop.json)
                    self._stop_seen_t = time.time()
                    if self._stop_requested():
                        break
                if waiting and time.time() - last_wait > 0.5:
                    # entries another process (a worker of the killed run) is still saving
                    last_wait = time.time()
                    for idx in sorted(waiting):
                        if not self._job_busy(idx):
                            waiting.discard(idx)
                            self._recover_one(idx)
                if not workers and time.time() - budget_t > 5:
                    budget, budget_t = available_memory() * MEM_SHARE, time.time()
                for idx in self._pending():
                    if len(running) >= n:
                        break
                    if idx in running.values():
                        continue
                    if idx not in need:
                        path = self.data["entries"][str(idx)].get("path", "")
                        need[idx] = decoded_bytes(path) * WORK_FACTOR
                    if not workers and running:
                        # memory: n = floor(avail × 0.6 / (decoded × 6)) for photos of this size
                        if sum(need.get(i, 0) for i in running.values()) + need[idx] > budget:
                            break
                    self.mark(idx, "running")
                    fut = ex.submit(run_job, self.id, idx)
                    running[fut] = idx
                _attach_workers(ex, attached)
                if not running:
                    if self._staging_done.is_set() and not self._pending() and not waiting:
                        break
                    time.sleep(0.1)
                    continue
                done, _ = cf.wait(list(running), timeout=0.25, return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    idx = running.pop(fut)
                    try:
                        r = fut.result()
                    except Exception as e:  # worker crashed
                        r = {"ok": False, "error": f"Worker failed: {e}", "code": "crash"}
                    self._record_result(idx, r)
            # cancel: wait for the files in progress
            for fut in cf.as_completed(list(running)):
                idx = running.pop(fut)
                try:
                    self._record_result(idx, fut.result())
                except Exception as e:
                    self._record_result(idx, {"ok": False, "error": str(e), "code": "crash"})
        with self._txn():
            if self._cancel.is_set():
                for e in self.data["entries"].values():
                    if e["state"] in ("staged", "retry"):
                        e["state"] = "cancelled"
                        e["error"] = "Stopped before it was saved"
                self.data["state"] = "cancelled"
            elif any(e["state"] in ("staged", "retry") for e in self.data["entries"].values()):
                return True  # queued (a retry) after the loop found nothing left: never left behind
            else:
                self.data["state"] = "finished"
            self._write()
        return False

    def _record_result(self, idx: int, r: Dict[str, Any]):
        if r.get("ok"):
            out = r.get("out_path", "")
            # identity of what we wrote: restore refuses to undo edits made after the batch.
            # The backup restore puts back is the file as it was right before THIS save.
            self.mark(idx, "done", out=out, backup=r.get("restore_path") or r.get("backup_path", ""),
                      notes=r.get("notes", []), error="",
                      outIdentity=r.get("out_identity") or file_identity(out), batchJob=f"{self.id}:{idx}")
            self._clear_draft(idx)
            return
        # the file may already be this job's output (written by an earlier attempt of this batch
        # that the journal did not record): adopt it rather than report a failure
        if r.get("code") in ("changed", "exists", "busy", "crash") and self._adopt(idx, "Saved by an earlier attempt"):
            return
        if r.get("code") == "changed":
            self.mark(idx, "changed", error="Changed during batch")
        elif r.get("code") == "exists":
            # save() reports the conflicting path as the error (for the editor's "Replace?" dialog)
            name = os.path.basename(str(r.get("error") or "")) or "The output file"
            self.mark(idx, "failed", code="exists",
                      error=f"{name} appeared after the batch was prepared and was not replaced. Rename or move "
                            f"it, then use Retry.")
        else:
            self.mark(idx, "failed", error=r.get("error", "Unknown error"), code=r.get("code", ""))

    def _job_out(self, idx: int, job: Optional[Dict[str, Any]] = None) -> Optional[str]:
        job = job if job is not None else (read_json(os.path.join(self._jobdir(idx), "job.json")) or {})
        if job.get("dest_path"):
            return job["dest_path"]
        if job.get("mode") == "overwrite" and job.get("path"):
            return os.path.realpath(job["path"])
        return None

    def _adopt(self, idx: int, note: str) -> bool:
        """Mark ``idx`` done when its output carries this batch job id. True if adopted."""
        job = read_json(os.path.join(self._jobdir(idx), "job.json")) or {}
        out = self._job_out(idx, job)
        if not out or not os.path.exists(out) or not self._is_our_output(out, idx):
            return False
        # the backup path is journaled by the worker before the file is replaced
        b = self._backup_json(idx)
        notes = [note]
        if job.get("mode") == "overwrite" and not b and (job.get("settings") or {}).get(
                "saving", {}).get("backupOriginals", True):
            notes.append("Backup not found; this file cannot be restored from the batch")
        self.mark(idx, "done", out=out, backup=b, notes=notes, error="", outIdentity=file_identity(out),
                  batchJob=f"{self.id}:{idx}")
        self._clear_draft(idx)
        return True

    def _clear_draft(self, idx: int) -> None:
        """The photo's single-photo draft is superseded by this save, unless it was edited since
        it was staged (then its hash differs and it is kept)."""
        try:
            job = read_json(os.path.join(self._jobdir(idx), "job.json")) or {}
            if job.get("draft_hash") and job.get("path"):
                from .drafts import delete_draft_if, keep_details_if
                if job.get("mode") == "copy" and job.get("meta_edits"):
                    # the copy has the edited details, the original doesn't: they stay as its draft
                    keep_details_if(job["path"], str(job["draft_hash"]))
                else:
                    delete_draft_if(job["path"], str(job["draft_hash"]))
        except Exception:
            pass

    def _job_busy(self, idx: int) -> bool:
        """A live process (an orphaned worker of a killed run, another window) holds this job's
        worker lock or the per-file lock of its target."""
        d = self._jobdir(idx)
        if _lock_holder(os.path.join(d, "worker.lock")):
            return True
        job = read_json(os.path.join(d, "job.json")) or {}
        out = self._job_out(idx, job)
        targets = [out] if out else []
        if job.get("path"):
            targets.append(os.path.realpath(job["path"]))
        for t in targets:
            if t and _lock_holder(file_lock(t).path):
                return True
        return False

    def _recover_one(self, idx: int) -> None:
        if not self._adopt(idx, "Recovered after restart"):
            self.mark(idx, "staged")

    def _recover_running(self) -> List[int]:
        """After a crash: jobs left 'running' either completed (their output carries this
        batch job id) or must run again. Jobs still held by a live process (a worker of the
        killed run finishing its file) stay 'running'; their indices are returned and are
        re-checked once that process lets go."""
        with self._lock:
            ents = [e for e in self.data["entries"].values() if e["state"] == "running"]
        waiting = []
        for e in ents:
            idx = e["index"]
            if self._job_busy(idx):
                waiting.append(idx)
            else:
                self._recover_one(idx)
        return waiting

    def retry_failed(self, epoch: Optional[int] = None):
        """Failed and stopped photos run again. Entries that were never staged (no job) can't
        run here: the UI stages them again from the kept pre-flight plan first. Nothing changes
        when the run can't start (BatchBusy) or a Stop came after the request (BatchStopped)."""
        self.start(epoch=epoch, requeue=True)

    def restore_originals(self, force: bool = False, indices: Optional[List[int]] = None) -> Dict[str, Any]:
        """Put backed-up originals back. A file edited since the batch wrote it is skipped
        ("edited since batch") unless ``force``; then the current file is first kept as
        <backups>/<name>-before-restore<ext>. Returns per-file results."""
        from .save import store_copy
        results: List[Dict[str, Any]] = []
        self.refresh()
        with self._lock:
            ents = sorted(self.data.get("entries", {}).values(), key=lambda e: e["index"])
            planned = self.planned_paths()
        for e in ents:
            if indices is not None and e["index"] not in indices:
                continue
            b = e.get("backup")
            ours = False
            if not b and e["state"] not in ("restored", "done"):
                # not recorded as saved (failed, changed, stopped...), but the file may still be
                # this job's output with its backup journaled before the replace
                bj = self._backup_json(e["index"])
                if bj and os.path.exists(e["path"]) and self._is_our_output(os.path.realpath(e["path"]), e["index"]):
                    b, ours = bj, True
            if not b:
                continue
            r: Dict[str, Any] = {"index": e["index"], "path": e["path"], "backup": b, "result": "", "message": ""}
            results.append(r)
            tmp = None
            lk = None
            try:
                if not e.get("path") or canonical_path(e["path"]) not in planned:
                    # only ever write back to a photo this batch was created with
                    r.update(result="error", message="Not a photo of this batch; not restored")
                    continue
                target = os.path.realpath(e["path"])
                bid = file_identity(b)
                if not bid or bid[0] == 0:
                    r.update(result="missing-backup", message="The backup file is missing or empty")
                    continue
                # held from before the "edited since the batch" check until the file is put back:
                # a save that finishes in between would otherwise be restored over unseen
                try:
                    lk = file_lock(target).acquire()
                except LockBusy:
                    r.update(result="error", message="This photo is being saved right now; try again when it finishes")
                    continue
                cur = file_identity(target) if os.path.exists(target) else None
                if cur and cur[0] == bid[0] and cur[2] == bid[2]:
                    r.update(result="unchanged", message="Already the original")
                    self.mark(e["index"], "restored", restoreResult="unchanged")
                    continue
                edited = False
                if cur and not ours:
                    want = e.get("outIdentity")
                    if want:
                        edited = [int(cur[0]), int(cur[1]), cur[2]] != [int(want[0]), int(want[1]), want[2]]
                    else:  # journal from before identities were kept: our output carries this job id
                        edited = not self._is_our_output(target, e["index"])
                if edited and not force:
                    r.update(result="skipped-edited", message="Edited since the batch; not restored")
                    self.mark(e["index"], e["state"], restoreResult="skipped-edited")
                    continue
                if edited:
                    stem, ext = os.path.splitext(os.path.basename(target))
                    r["aside"] = store_copy(target, os.path.join(os.path.dirname(b), f"{stem}-before-restore{ext}"))
                d = os.path.dirname(target)
                fd, tmp = tempfile.mkstemp(prefix=temp_prefix(".pbrestore-"), suffix=os.path.splitext(target)[1], dir=d)
                with os.fdopen(fd, "wb") as out, open(b, "rb") as inp:
                    shutil.copyfileobj(inp, out, 4 * 1024 * 1024)
                    out.flush()
                    os.fsync(out.fileno())
                try:
                    shutil.copystat(b, tmp)
                except OSError:
                    pass
                got = file_identity(tmp)
                if not got or got[0] != bid[0] or got[2] != bid[2]:
                    raise OSError("The restored copy could not be verified")
                replace_with_retry(tmp, target)
                tmp = None
                fsync_dir(d)
                r.update(result="restored", message="Restored" + (" (the edited file was kept)" if edited else ""))
                self.mark(e["index"], "restored", restoreResult="restored", restoredAside=r.get("aside", ""))
            except Exception as ex:
                r.update(result="error", message=str(ex))
            finally:
                if tmp and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if lk:
                    lk.release()
        return {"restored": sum(1 for r in results if r["result"] == "restored"),
                "unchanged": sum(1 for r in results if r["result"] == "unchanged"),
                "skipped": sum(1 for r in results if r["result"] == "skipped-edited"),
                "errors": [f"{r['path']}: {r['message']}" for r in results if r["result"] in ("error", "missing-backup")],
                "results": results}

    def _is_our_output(self, path: str, idx: int) -> bool:
        try:
            from .exiftool import get as et_get
            from . import record
            rec = record.from_metadata(et_get().read_json(path))
            return bool(rec and rec.get("batchJob") == f"{self.id}:{idx}")
        except Exception:
            return False

    def report_csv(self) -> str:
        """Every photo of the batch: saved, failed, skipped, blocked, excluded, held back, stopped.
        Cells that a spreadsheet would run as a formula (file names, reasons) are prefixed with '."""
        buf = io.StringIO()
        w = _SafeCsvWriter(csv.writer(buf))
        w.writerow(["file", "relative path", "state", "action", "output", "backup", "error or reason", "notes"])
        sm = self.summary()
        folder = sm["meta"].get("folder") or ""
        actions = {}
        for it in sm["meta"].get("plan") or []:
            if isinstance(it, dict) and it.get("path"):
                actions[it["path"]] = it.get("action", "")
        for e in sm["entries"]:
            p = e.get("path") or ""
            rel = p
            if folder and p:
                try:
                    rel = os.path.relpath(p, folder)
                except ValueError:  # another drive (Windows)
                    rel = p
            w.writerow([p, rel, e.get("state"), actions.get(p, ""), e.get("out", ""), e.get("backup", ""),
                        e.get("error", ""), " | ".join(e.get("notes") or [])])
        return buf.getvalue()


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(v: Any) -> Any:
    """A CSV cell that spreadsheets show as text, never evaluate (CSV/formula injection)."""
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


class _SafeCsvWriter:
    def __init__(self, w):
        self._w = w

    def writerow(self, row):
        self._w.writerow([csv_safe(v) for v in row])


def run_job(bid: str, idx: int) -> Dict[str, Any]:
    """Executed in a worker process."""
    d = os.path.join(_bdir(bid), f"{idx:05d}")
    job = read_json(os.path.join(d, "job.json"))
    if not job:
        return {"ok": False, "error": "Staged job is missing", "code": "missing"}
    # held while this job runs: a relaunched app waits for it instead of running the job again
    wl = PidLock(os.path.join(d, "worker.lock"))
    try:
        wl.acquire()
    except LockBusy:
        return {"ok": False, "error": "This photo is still being saved by another process.", "code": "busy"}
    try:
        return _run_job(bid, idx, d, job)
    finally:
        wl.release()


def _run_job(bid: str, idx: int, d: str, job: Dict[str, Any]) -> Dict[str, Any]:
    from .composite import Tile
    from .save import SaveRequest, save
    from .settings import load_settings
    tiles = []
    for t in job.get("tiles", []):
        with open(os.path.join(d, t["file"]), "rb") as fh:
            tiles.append(Tile(t["x"], t["y"], fh.read()))
    settings = job.get("settings") or load_settings()

    def on_backup(path: str) -> None:
        # journaled before the replace, so a killed run can still restore this file
        _check_parent()
        atomic_write_json(os.path.join(d, "backup.json"), {"backup": path, "time": time.time()})

    req = SaveRequest(path=job["path"], mode=job["mode"], layout=job["layout"], tiles=tiles,
                      state=job.get("state") or {}, settings=settings, dest_path=job.get("dest_path"),
                      erase=job.get("erase"), original_text=job.get("original_text"), case=job.get("case"),
                      expected_stat=tuple(job["expected_stat"]) if job.get("expected_stat") else None,
                      batch_job=f"{bid}:{idx}", fields=job.get("fields"), template_name=job.get("template_name", ""),
                      on_exists=job.get("on_exists"), expected_hash=job.get("expected_hash"), on_backup=on_backup,
                      meta_edits=job.get("meta_edits"))
    r = save(req)
    out = r.to_json()
    if r.ok:
        out["out_identity"] = file_identity(r.out_path)
    return out


# -- registry ----------------------------------------------------------------------
# a batch in "staging" last journaled by another live process less than this long ago is being
# prepared there (photos are journaled as each one is staged)
STAGING_IDLE_S = 600.0
_batches: Dict[str, Batch] = {}
_reg_lock = threading.Lock()


def get_batch(bid: str) -> Batch:
    """This process's Batch for ``bid``, its journal copy revalidated against the file (another
    Photoband process may have run, stopped, retried, restored or discarded it since)."""
    with _reg_lock:
        b = _batches.get(bid)
        if b is None:
            b = Batch(bid)
            if not b.data:
                raise BatchNotFound(bid)
            _batches[bid] = b
            return b
    b.refresh()
    return b


def new_batch(meta: Dict[str, Any], unsaved: Optional[List[Dict[str, Any]]] = None,
              settings: Optional[Dict[str, Any]] = None) -> Batch:
    b = Batch.create(meta, unsaved, settings)
    with _reg_lock:
        _batches[b.id] = b
    return b


def incomplete_batches() -> List[Dict[str, Any]]:
    out = []
    root = paths.sub("batches")
    for bid in sorted(os.listdir(root)):
        data = read_json(os.path.join(root, bid, "journal.json"))
        if not data:
            continue
        if data.get("state") in ("running", "staging") and bid not in _batches and not _open_elsewhere(bid, data):
            ents = list(data.get("entries", {}).values())
            expected = int(data.get("expected", len(ents)) or 0)
            pending = sum(1 for e in ents if e["state"] not in TERMINAL and e["state"] != "restored")
            # photos never staged (the app died before or while preparing them)
            missing = max(0, expected - len(ents))
            if pending or missing:
                out.append({"id": bid, "created": data.get("created"), "pending": pending + missing,
                            "missing": missing, "total": expected,
                            "meta": data.get("meta", {}), "stagingComplete": data.get("stagingComplete", False)})
    return out


def _open_elsewhere(bid: str, data: Dict[str, Any]) -> bool:
    """The batch is being run, or prepared, by another live Photoband process right now: not
    one to offer for resume (its journal says "running"/"staging" because it is)."""
    if _lock_holder(os.path.join(paths.sub("batches"), bid, "run.lock")) not in (None, os.getpid()):
        return True
    try:
        pid = int(data.get("pid") or 0)
        recent = time.time() - float(data.get("updated") or 0) < STAGING_IDLE_S
    except (TypeError, ValueError):
        return False
    # being staged in another window: its photos are prepared and journaled one by one
    return data.get("state") == "staging" and recent and pid not in (0, os.getpid()) and _pid_alive(pid)


def discard_batch(bid: str) -> None:
    b = get_batch(bid)
    with b._txn():
        if (b._thread and b._thread.is_alive()) or b.run_elsewhere():
            # the run's end would write its own state over "discarded"
            raise BatchBusy("This batch is still saving. Stop it first.")
        b.data["state"] = "discarded"
        b._write()
