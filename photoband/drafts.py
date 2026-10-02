"""Autosaved per-photo drafts, keyed by file path and source identity.

Identity is size + a cheap content hash (first and last 1 MB), not mtime: backup tools,
metadata sync and antivirus touch mtimes without changing the photo, and a draft is hours of
typed names. A draft whose file really changed is kept (listed as not valid) so the UI can
offer it, and is pruned only after 60 days."""
from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import time
import unicodedata
from typing import Any, Dict, List, Optional

from . import paths
from .shared_state import interprocess_lock
from .shared_state import write_json as atomic_write_json
from .util import canonical_path, quick_hash, read_json

log = logging.getLogger(__name__)

PRUNE_AFTER_DAYS = 60
_lock = threading.RLock()   # an autosave and a compare-and-delete must not interleave
_held = threading.local()   # draft locks this thread holds (the lock file is not reentrant)


def _key(path: str) -> str:
    """Same file, same key: links resolved, letter case folded where the file system ignores it
    (as for locks and backups), and Unicode NFC (macOS hands out decomposed names)."""
    return unicodedata.normalize("NFC", canonical_path(path))


def _p(path: str) -> str:
    h = hashlib.sha1(_key(path).encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(paths.sub("drafts"), h + ".json")


def _legacy_p(path: str) -> str:
    """Where drafts were kept before canonical keys (os.path.abspath)."""
    h = hashlib.sha1(os.path.abspath(path).encode("utf-8", "surrogatepass")).hexdigest()
    return os.path.join(paths.sub("drafts"), h + ".json")


@contextlib.contextmanager
def _locked(path: str):
    """This photo's draft is read, compared and written by one process at a time: another
    Photoband window autosaves it, a batch in another process clears it after saving the photo.
    A check-then-write (compare-and-delete, the stat refresh) is atomic across those processes."""
    lp = os.path.join(paths.sub("drafts"), ".drafts.lock")   # one for all drafts: held for ms
    held = getattr(_held, "keys", None)
    if held is None:
        held = _held.keys = set()
    with _lock:
        if lp in held:
            yield
            return
        with interprocess_lock(lp):
            held.add(lp)
            try:
                yield
            finally:
                held.discard(lp)


def _read(path: str) -> Optional[Dict[str, Any]]:
    """The stored draft record for ``path``; a draft under the old key is moved to the new one."""
    p = _p(path)
    d = read_json(p)
    if d:
        return d
    old = _legacy_p(path)
    if old == p:
        return None
    d = read_json(old)
    if not d:
        return None
    with _locked(path):
        cur = read_json(p)
        if cur:
            return cur   # saved under the new key meanwhile (another window): that one is newer
        try:
            atomic_write_json(p, d)
            os.unlink(old)
        except OSError:
            log.debug("could not migrate draft %s", old, exc_info=True)
    return d


def _stat(path: str):
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def _ident(path: str) -> Optional[Dict[str, Any]]:
    try:
        return {"size": os.path.getsize(path), "hash": quick_hash(path)}
    except OSError:
        return None


def _check(d: Dict[str, Any]) -> bool:
    """True when the draft still belongs to the file on disk (and refreshes its stat)."""
    path = d.get("path")
    st = _stat(path) if path else None
    if st is None:
        return False
    if d.get("stat") == st:
        return True  # unchanged since the draft was saved: no need to read the file
    ident = d.get("ident")
    if not ident:
        return False  # draft from before content identities: stat is all we have
    cur = _ident(path)
    return bool(cur and cur == ident)


def save_draft(path: str, state: Dict[str, Any]) -> None:
    with _locked(path):
        _save_draft(path, state)


def _save_draft(path: str, state: Dict[str, Any]) -> None:
    atomic_write_json(_p(path), {"path": os.path.realpath(path), "stat": _stat(path), "ident": _ident(path),
                                 "state": state, "updated": time.time()})
    old = _legacy_p(path)
    if old != _p(path) and os.path.exists(old):
        try:
            os.unlink(old)
        except OSError:
            log.debug("could not remove old draft %s", old, exc_info=True)


def load_draft(path: str) -> Optional[Dict[str, Any]]:
    d = _read(path)
    if not d:
        return None
    if not _check(d):
        return None  # the file changed since the draft was made; kept for list_drafts
    if d.get("stat") != _stat(path):
        # same content, only the stat moved (mtime touch): remember the new stat. Written back
        # only over the very draft read here: a newer autosave (this or another process) wins.
        with _locked(path):
            cur = read_json(_p(path))
            if cur == d:
                cur["stat"] = _stat(path)
                try:
                    atomic_write_json(_p(path), cur)
                except OSError:
                    log.debug("could not refresh draft stat", exc_info=True)
    return d.get("state")


def load_draft_any(path: str) -> Optional[Dict[str, Any]]:
    """The stored draft whatever the file's state (for "use stale draft anyway")."""
    d = _read(path)
    return d.get("state") if d else None


def delete_draft(path: str) -> None:
    with _locked(path):
        for p in {_p(path), _legacy_p(path)}:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
            except OSError:
                log.debug("could not delete draft %s", p, exc_info=True)


def list_drafts() -> List[Dict[str, Any]]:
    out = []
    d = paths.sub("drafts")
    for fn in os.listdir(d):
        if fn.endswith(".json"):
            data = read_json(os.path.join(d, fn))
            if data and data.get("path"):
                exists = os.path.exists(data["path"])
                out.append({"path": data["path"], "updated": data.get("updated"), "exists": exists,
                            "valid": exists and _check(data)})
    return sorted(out, key=lambda x: -(x["updated"] or 0))


def prune_drafts(days: float = PRUNE_AFTER_DAYS) -> int:
    """Delete drafts not updated for ``days`` days. Returns how many were removed."""
    d = paths.sub("drafts")
    cutoff = time.time() - days * 86400
    n = 0
    for fn in os.listdir(d):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(d, fn)
        data = read_json(p)
        updated = (data or {}).get("updated")
        try:
            if updated is None:
                updated = os.path.getmtime(p)
            if float(updated) < cutoff:
                os.unlink(p)
                n += 1
        except (OSError, TypeError, ValueError):
            pass
    return n


def delete_draft_if(path: str, state_hash: str, unhashed: bool = False) -> bool:
    """Delete the draft only if it is still the one identified by ``state_hash`` (the editor
    state's hash, stored as state["_hash"]); a draft edited since is kept. ``unhashed``: a draft
    stored without a hash is deleted too. Compares whatever the file's state, so it works after
    an overwrite changed the file's identity. True if deleted."""
    with _locked(path):
        st = load_draft_any(path)
        if st is None or not state_hash:
            return False
        h = st.get("_hash") if isinstance(st, dict) else None
        if h != state_hash and not (unhashed and h is None):
            return False
        delete_draft(path)
        return True
