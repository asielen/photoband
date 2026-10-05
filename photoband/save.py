"""The save procedure: back up, composite into a temp file, apply metadata, verify,
fsync and atomically replace. A failed save never damages the source.

Data-safety rules (the photos are irreplaceable archive scans):
* A backup is reused only when the file being overwritten is provably this app's output of
  that backup; anything else gets a new, versioned backup (name-2.tif, name-3.tif ...).
* Backups are written to a unique temp name, verified (size + SHA-256), fsynced and placed
  exclusively; an existing file is never replaced by a backup.
* Nothing is ever written into a backup folder, and a copy never replaces a file this app
  did not make unless the user confirmed that specific file (and then it is kept aside first).
* Overwrites hold a per-file lock, follow symlinks to the real file, refuse read-only files,
  and keep extended attributes.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import threading
from collections import OrderedDict
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from captiontokens import resolve_filename

from . import __version__, paths, record
from .composite import CompositeResult, LayoutError, Tile, composite
from .exiftool import ExifToolError, get as get_exiftool
from .imageio import (EXT_FOR, ImageError, ImageInfo, load_upright, output_format_for,
                      pixel_hash, probe, read_pixels, upright)
from .util import (LockBusy, canonical_path, copy_xattrs, creation_time_ns, dir_writable,
                   file_lock, file_sha256, fsync_dir, held_open_elsewhere, is_readonly, place_exclusive, quick_hash,
                   replace_with_retry, set_creation_time, temp_prefix)

log = logging.getLogger(__name__)

BACKUP_DIRNAME = "_originals"
BACKUP_SUFFIX = "-original"           # _originals/scan.tif -> _originals/scan-original.tif
BACKUP_MARKER = ".photoband-backups"   # dropped into every folder that holds backups


def same_file(a: str, b: str) -> bool:
    """True when two paths name the same file (case-insensitive file systems, links)."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _copy_mode(src: str, dst: str, writable: bool = False) -> None:
    """Give the temp file (mkstemp creates it 0600) the source's permission bits. ``writable``
    keeps the owner's write bit: a copy of a read-only (protected) original is a new file, and a
    read-only copy could not be fsynced, cleaned up or replaced by a later save (Windows)."""
    try:
        mode = os.stat(src).st_mode & 0o7777
        if writable:
            mode |= 0o200
        os.chmod(dst, mode)
    except OSError:
        pass


class SaveError(Exception):
    def __init__(self, message: str, code: str = "save_failed"):
        super().__init__(message)
        self.code = code


@dataclass
class SaveRequest:
    path: str
    mode: str                         # copy | overwrite | copyAs
    layout: Dict[str, Any]
    tiles: Sequence[Tile]
    state: Dict[str, Any]             # editor state for the record (template, blocks, overrides)
    settings: Dict[str, Any]
    dest_path: Optional[str] = None   # copyAs
    erase: Optional[Dict[str, Any]] = None
    original_text: Optional[Dict[str, Any]] = None
    case: Optional[str] = None        # A | B | C | None
    # (size, mtime_ns) or (size, mtime_ns, quick_hash) from pre-flight / open
    expected_stat: Optional[Tuple] = None
    batch_job: Optional[str] = None
    embed_marker: Optional[bool] = None
    fields: Optional[Dict[str, Any]] = None       # token fields, for the file-name pattern
    template_name: str = ""
    on_exists: Optional[str] = None   # override for "ask" answered by the UI
    expected_hash: Optional[str] = None           # util.quick_hash of the source when it was opened
    on_backup: Optional[Callable[[str], None]] = None   # told the backup path before the replace
    meta_edits: Optional[Dict[str, Any]] = None   # photo details the user edited (metaedit), written into the output


@dataclass
class SaveResult:
    ok: bool
    path: str
    out_path: str = ""
    backup_path: str = ""
    notes: List[str] = field(default_factory=list)
    marker: Dict[str, Any] = field(default_factory=dict)
    size: Tuple[int, int] = (0, 0)
    format: str = ""
    elapsed_ms: int = 0
    error: str = ""
    code: str = ""
    # batch overwrite of a file that already was this app's output: the copy of the file as it
    # was right before this save (what "Restore originals" of that batch puts back)
    restore_path: str = ""

    def to_json(self):
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


# --------------------------------------------------------------------------
# backup folders
# --------------------------------------------------------------------------

def is_backup_location(path: str, saving: Dict) -> bool:
    """True for anything inside an _originals folder or the configured backup folder."""
    rp = canonical_path(path)
    parts = [p for p in re.split(r"[\\/]+", rp) if p]
    if os.path.normcase(BACKUP_DIRNAME) in parts:
        return True
    # a folder this app wrote backups into, even if the setting has changed since
    parent = os.path.dirname(rp)
    if os.path.isfile(os.path.join(parent, BACKUP_MARKER)) or os.path.isfile(os.path.join(rp, BACKUP_MARKER)):
        return True
    bf = (saving or {}).get("backupFolder")
    if bf:
        b = canonical_path(bf).rstrip("\\/")
        if rp == b or rp.startswith(b + os.sep):
            return True
    return False


def carries_record(path: str) -> bool:
    """True when the file holds a photoband record, i.e. it is an earlier output of this app
    (of SOME photo: see :func:`is_output_of` before replacing it)."""
    try:
        return record.from_metadata(get_exiftool().read_json(path)) is not None
    except Exception:
        log.debug("could not read the record of %s", path, exc_info=True)
        return False


def source_key(src: str) -> str:
    """Identifies the source photo a copy was made from (stored in the record as ``sourceKey``):
    a hash of its canonical path, so no folder names end up in the output's metadata."""
    k = unicodedata.normalize("NFC", canonical_path(src))
    return hashlib.sha256(k.encode("utf-8", "surrogatepass")).hexdigest()[:24]


class SourceIds:
    """The path-independent identity of a source photo: the pixel hashes (computed exactly as the
    record's ``photoHash``) that a copy made from it can carry. Computed lazily, once, and only
    when a candidate file's ``sourceKey`` did not match (the hash costs a pass over the pixels).

    ``fn`` returns the hashes; see :func:`source_photo_hashes`."""

    def __init__(self, fn: Callable[[], Sequence[str]]):
        self._fn = fn
        self._v: Optional[frozenset] = None

    @property
    def computed(self) -> bool:
        return self._v is not None

    def __call__(self) -> frozenset:
        if self._v is None:
            try:
                self._v = frozenset(h for h in self._fn() if h)
            except Exception:
                log.debug("could not hash the source photo", exc_info=True)
                self._v = frozenset()
        return self._v


def _layout_region(arr: np.ndarray, layout: Optional[Dict]) -> Optional[np.ndarray]:
    """The part of the upright source that save() records as ``photoHash`` for ``layout``."""
    if not layout:
        return None
    W, H = arr.shape[1], arr.shape[0]
    try:
        if layout.get("mode") == "erase":
            x, y, w, h = (int(v) for v in layout.get("photoRect") or (0, 0, W, H))
            x, y = max(0, x), max(0, y)
            w, h = min(w, W - x), min(h, H - y)
        else:
            x, y, w, h = (int(v) for v in layout.get("sourceRect") or (0, 0, W, H))
    except (TypeError, ValueError):
        return None
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > W or y + h > H:
        return None
    return arr[y:y + h, x:x + w]


def source_photo_hashes(arr: np.ndarray, md: Optional[Dict] = None, layout: Optional[Dict] = None) -> List[str]:
    """Pixel hashes that identify the photo in ``arr`` (upright source pixels), the way the
    record's ``photoHash`` is computed: the whole image, the region the current layout uses, and,
    when the source is itself this app's output (case A), its inner photo region."""
    out = [pixel_hash(arr)]
    reg = _layout_region(arr, layout)
    if reg is not None and reg.shape != arr.shape:
        out.append(pixel_hash(reg))
    rec = record.from_metadata(md) if md else None
    if rec and is_app_output(rec, arr):
        # is_app_output matched the inner region against one of these
        out += [h for h in (rec.get("photoHash"), rec.get("outputPhotoHash")) if isinstance(h, str)]
    return out


def source_ids_for_file(src: str, layout: Optional[Dict] = None) -> SourceIds:
    """A lazy :class:`SourceIds` that decodes ``src`` from disk only if it is needed."""
    def fn():
        a, inf = load_upright(src)
        try:
            md = get_exiftool().read_json(src)
        except Exception:
            md = None
        return source_photo_hashes(a, md, layout)
    return SourceIds(fn)


def _record_of(path: str) -> Optional[Dict]:
    try:
        return record.from_metadata(get_exiftool().read_json(path))
    except Exception:
        log.debug("could not read the record of %s", path, exc_info=True)
        return None


def is_output_of(path: str, src: str, src_ids: Optional[SourceIds] = None) -> bool:
    """True only when ``path`` is an earlier copy THIS app made of the same source photo, so the
    "overwrite" policy may replace it. Another photo's captioned copy (same output name from
    another folder) or a photo captioned in place is never replaced silently.

    A copy is "of this source" when its record's ``sourceKey`` (a hash of the source's path)
    matches, or, when the path changed since (folder moved or renamed, other drive letter or
    mount) or the record predates ``sourceKey``, when its ``photoHash`` equals the source's own
    photo pixel hash (``src_ids``). Different photos never share a pixel hash."""
    if same_file(path, src):
        return False
    rec = _record_of(path)
    if not rec:
        return False
    own_key = source_key(path)
    if rec.get("sourceKey") and rec.get("sourceKey") == own_key:
        # a source captioned in place carries its own key: that file is a photo, not a copy
        return False
    if rec.get("sourceKey") and rec.get("sourceKey") == source_key(src):
        return True
    if src_ids is None:
        return False
    # pixel identity: never for a file captioned in place (a photo, not a copy), which records
    # how it was saved (saveMode) or which original it replaced (originalFile)
    if rec.get("saveMode") == "overwrite" or rec.get("originalFile"):
        return False
    ph = rec.get("photoHash")
    return isinstance(ph, str) and bool(ph) and ph in src_ids()


def _next_free(out: str) -> str:
    base, ext = os.path.splitext(out)
    i = 2
    while os.path.exists(f"{base}-{i}{ext}"):
        i += 1
    return f"{base}-{i}{ext}"


# --------------------------------------------------------------------------
# destinations
# --------------------------------------------------------------------------

def destination_for(src: str, info: ImageInfo, saving: Dict, fields: Optional[Dict], template_name: str,
                    on_exists: Optional[str] = None, notes: Optional[List[str]] = None,
                    src_ids: Optional[SourceIds] = None) -> Tuple[str, str]:
    """(path, format) for Save copy, honouring location, file-name pattern and conflict policy.

    The "overwrite" policy only replaces earlier outputs of this app (files with a photoband
    record); any other file of that name (possibly another photo's original) is kept and the
    copy gets the next free -2, -3 name. ``on_exists="overwrite"`` is the user's answer for a
    specific file; save() then keeps a copy of a replaced file that is not an earlier output."""
    fmt = output_format_for(info, saving.get("outputFormat", "same"))
    loc = saving.get("location", "subfolder")
    src_dir = os.path.dirname(os.path.abspath(src))
    if loc == "fixed" and saving.get("fixedFolder"):
        folder = saving["fixedFolder"]
        if not isinstance(folder, str) or not os.path.isabs(folder):
            raise SaveError("The fixed output folder in Settings › Saving is not valid. Choose it again.", code="dest")
    elif loc == "same":
        folder = src_dir
    else:
        from .security import is_plain_folder_name
        sub = saving.get("subfolderName") or "captioned"
        if not is_plain_folder_name(sub):
            # never a path: "../x" or "/etc" would write outside the photo's folder
            raise SaveError("The output subfolder name in Settings › Saving must be a plain folder name.",
                            code="dest")
        folder = os.path.join(src_dir, sub)
    folder = os.path.abspath(folder)
    # the copy goes into the photo's folder, a direct subfolder of it, or the chosen fixed folder
    allowed_parent = folder == src_dir or os.path.dirname(folder) == src_dir or (
        loc == "fixed" and folder == os.path.abspath(saving.get("fixedFolder") or ""))
    if not allowed_parent:
        raise SaveError("The copy destination is outside the photo's folder.", code="dest")
    if is_backup_location(os.path.join(folder, "x"), saving):
        raise SaveError("The copy destination is inside a backups folder (_originals). Choose another location "
                        "in Settings › Saving.", code="dest")
    f = dict(fields or {})
    stem = os.path.splitext(os.path.basename(src))[0]
    f.setdefault("stem", stem)
    f.setdefault("filename", os.path.basename(src))
    f.setdefault("folder", os.path.basename(src_dir))
    f["template"] = template_name
    name = resolve_filename(saving.get("fileName") or "{stem}-captioned", f) or stem + "-captioned"
    ext = EXT_FOR[fmt]
    if fmt == info.format and os.path.splitext(src)[1].lower() in {".tif", ".tiff", ".jpg", ".jpeg", ".png"}:
        ext = os.path.splitext(src)[1]  # keep .tiff vs .tif, .jpeg vs .jpg
    out = os.path.join(folder, name + ext)
    if os.path.dirname(out) != folder:
        raise SaveError("The file name pattern made an invalid file name.", code="dest")
    policy = on_exists or saving.get("onExists", "increment")
    if same_file(out, src):
        # never let a copy silently replace its own source
        out = os.path.join(folder, name + "-captioned" + ext)
    if os.path.exists(out):
        if policy == "overwrite":
            if src_ids is None:
                src_ids = source_ids_for_file(src)
            if on_exists != "overwrite" and not is_output_of(out, src, src_ids):
                # next name that is free or is itself an earlier copy of this photo (replaced per the policy)
                b, e = os.path.splitext(out)
                i = 2
                while os.path.exists(f"{b}-{i}{e}") and not is_output_of(f"{b}-{i}{e}", src, src_ids):
                    i += 1
                new = f"{b}-{i}{e}"
                if notes is not None:
                    notes.append(f"{os.path.basename(out)} already exists and was not made by Photoband from this "
                                 f"photo, so it was kept; the copy was saved as {os.path.basename(new)}")
                out = new
        elif policy == "ask":
            raise SaveError(out, code="exists")
        else:
            out = _next_free(out)
    return out, fmt


def backup_path_for(src: str, saving: Dict) -> str:
    """The first-choice backup name; later versions are name-2.ext, name-3.ext ..."""
    folder = saving.get("backupFolder")
    if folder:
        h = hashlib.sha1(canonical_path(src).encode("utf-8")).hexdigest()[:8]
        stem, ext = os.path.splitext(os.path.basename(os.path.realpath(src)))
        return os.path.join(folder, f"{stem}__{h}{ext}")
    real = os.path.realpath(src)
    stem, ext = os.path.splitext(os.path.basename(real))
    return os.path.join(os.path.dirname(real), BACKUP_DIRNAME, f"{stem}{BACKUP_SUFFIX}{ext}")


def save_preview(src: str, saving: Dict, fields: Optional[Dict], template_name: str) -> Dict[str, Any]:
    """Where each kind of save would write, for the UI. Nothing is written; hashes are cached.

    copy: the copy's path (as Save copy would name it now); copyExists: the name is taken and the
    user will be asked. backup: the exact file Overwrite keeps (an existing backup it reuses, or
    the new one's name; None with backups off); backupKind: "original" when that file is the
    untouched original, "current" when it is this already-captioned file as it is now;
    backupExists: the original is already backed up there. pixelSource: "backup" when saving takes
    the photo from the verified original backup (originalBackup). captioned: the file is this app's
    output: it has a Photoband record, or (metadata stripped) the analysis made when it was opened
    found the hidden marker. Without the record nothing about its original is known, so a backup
    made now holds the captioned file as it is ("current"), never "the untouched original"."""
    real = os.path.realpath(src)
    out: Dict[str, Any] = {"overwrite": src, "copyExists": False, "copyError": ""}
    try:
        out["copy"], _ = destination_for(src, probe(real), saving, fields, template_name)
    except SaveError as e:
        if e.code == "exists":
            out["copy"], out["copyExists"] = str(e), True
        else:
            out["copy"], out["copyError"] = "", str(e)
    # the backup and the pixel source are decided by the same code the save runs (plan_backup,
    # find_original_backup), with hashes cached per file version so a preview stays cheap
    rec = _record_of(real)
    captioned = bool(rec) or photoband_output_without_record(real)
    out["captioned"] = captioned
    if saving.get("backupOriginals", True):
        try:
            plan = plan_backup(real, saving, rec, sha_fn=_cached_sha256)
        except OSError:
            plan = None
        if plan is None:
            out["backup"], out["backupKind"] = None, ""
        elif plan.reuse:
            # an existing backup is kept: the untouched original, or a copy identical to the file now
            out["backup"] = plan.reuse
            out["backupKind"] = "original" if plan.original or not captioned else "current"
        else:
            out["backup"] = _next_versioned(plan.base)
            # a new backup holds the file as it is now: for a photo Photoband already captioned in
            # place that is not the untouched original
            out["backupKind"] = "current" if captioned else "original"
    else:
        out["backup"], out["backupKind"] = None, ""
    out["backupExists"] = out["backupKind"] == "original" and bool(out["backup"]) and os.path.exists(out["backup"])
    # the save also requires the file to still be this app's output (its photo region unchanged):
    # check that on the pixels the editor already holds; without them it is only "probably"
    bk = find_original_backup(real, saving, rec, sha_fn=_cached_sha256) if rec else None
    source = "file"
    if bk:
        from .photos import _peek_full
        arr, _ = _peek_full(real, probe(real))
        source = "unverified" if arr is None else "backup" if is_app_output(rec, arr) else "file"
    out["pixelSource"], out["originalBackup"] = source, (bk if source != "file" else None)
    return out


_SHA_CACHE: "OrderedDict[Tuple[str, int, int, int], str]" = OrderedDict()
_SHA_LOCK = threading.Lock()


def _cached_sha256(path: str) -> str:
    """SHA-256 of a file, remembered per (file, size, mtime, file id) for previews. The file id
    tells a same-size replacement whose modified time was kept from the file it replaced."""
    st = os.stat(path)
    key = (canonical_path(path), st.st_size, st.st_mtime_ns, st.st_ino)
    with _SHA_LOCK:
        if key in _SHA_CACHE:
            _SHA_CACHE.move_to_end(key)
            return _SHA_CACHE[key]
    sha = file_sha256(path)
    with _SHA_LOCK:
        _SHA_CACHE[key] = sha
        while len(_SHA_CACHE) > 256:
            _SHA_CACHE.popitem(last=False)
    return sha


def opened_full_hash(path: str, size: int, mtime_ns: int, file_id: Optional[int] = None) -> Optional[str]:
    """The full SHA-256 of ``path`` as it was at (size, mtime_ns[, file id]), if it was hashed then.
    Without a file id (an older client), any version with that size and time."""
    cp = canonical_path(path)
    with _SHA_LOCK:
        if file_id:
            return _SHA_CACHE.get((cp, size, mtime_ns, file_id))
        return next((v for k, v in reversed(_SHA_CACHE.items()) if k[:3] == (cp, size, mtime_ns)), None)


_OPEN_HASHER = None


def hash_in_background(path: str) -> None:
    """Hash an opened photo once, off the request thread, so a later save can tell a
    modified-time touch from a real edit by its whole content. One worker: flipping through a
    folder queues the photos instead of reading them all at once."""
    global _OPEN_HASHER
    if _OPEN_HASHER is None:
        from concurrent.futures import ThreadPoolExecutor
        _OPEN_HASHER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pb-open-hash")

    def run():
        try:
            _cached_sha256(path)
        except OSError:
            pass
    _OPEN_HASHER.submit(run)


def _next_versioned(base: str) -> str:
    """The name store_copy gives a new backup: base, or base-2, base-3 ... when taken."""
    i = 1
    while os.path.exists(_versioned(base, i)):
        i += 1
    return _versioned(base, i)


def _legacy_backup_base(src: str, saving: Dict) -> Optional[str]:
    """Backups made before they were named <stem>-original<ext>: _originals/<name>."""
    if saving.get("backupFolder"):
        return None
    real = os.path.realpath(src)
    return os.path.join(os.path.dirname(real), BACKUP_DIRNAME, os.path.basename(real))


def _versioned(base: str, i: int) -> str:
    if i <= 1:
        return base
    b, ext = os.path.splitext(base)
    return f"{b}-{i}{ext}"


def backup_versions(base: str) -> List[str]:
    """Existing backups for ``base``: base, base-2, base-3 ... (newest last)."""
    d = os.path.dirname(base)
    stem, ext = os.path.splitext(os.path.basename(base))
    pat = re.compile(re.escape(stem) + r"(?:-(\d+))?" + re.escape(ext) + r"$", re.IGNORECASE)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    found = []
    for n in names:
        m = pat.fullmatch(n)
        if m and os.path.isfile(os.path.join(d, n)):
            found.append((int(m.group(1) or 1), os.path.join(d, n)))
    return [p for _, p in sorted(found)]


def is_app_output(rec: Optional[Dict], arr: np.ndarray) -> bool:
    """True when ``arr`` (upright source pixels) really is the output a record describes: the
    canvas size matches and the photo region still hashes to the recorded photo hash."""
    if not rec:
        return False
    try:
        W, H = (int(v) for v in rec.get("canvas") or (0, 0))
        x, y = (int(v) for v in rec.get("photoOffset") or (0, 0))
        w, h = (int(v) for v in rec.get("originalSize") or (0, 0))
    except (TypeError, ValueError):
        return False
    if (W, H) != (arr.shape[1], arr.shape[0]) or w <= 0 or h <= 0 or x + w > W or y + h > H:
        return False
    ph = pixel_hash(arr[y:y + h, x:x + w])
    return ph in {rec.get("outputPhotoHash"), rec.get("photoHash")} - {None}


def _legacy_backup_matches(rec: Dict, backup: str) -> bool:
    """Records written before ``originalFile`` existed: the backup is this output's original when
    the backup's photo region hashes to the record's photoHash (first caption of an
    uncaptioned file)."""
    try:
        a, _ = load_upright(backup)
        if rec.get("mode") == "erase":
            x, y = (int(v) for v in rec.get("photoOffset"))
            w, h = (int(v) for v in rec.get("originalSize"))
        else:
            x, y, w, h = (int(v) for v in (rec.get("layout") or {}).get("sourceRect")
                          or (0, 0, a.shape[1], a.shape[0]))
        if (w, h) != tuple(int(v) for v in rec.get("originalSize") or (0, 0)):
            return False
        return pixel_hash(a[y:y + h, x:x + w]) == rec.get("photoHash")
    except Exception:
        return False


# --------------------------------------------------------------------------
# re-saving from the original backup (no JPEG generation loss on re-captions)
# --------------------------------------------------------------------------

def _rect(v) -> Optional[Tuple[int, int, int, int]]:
    try:
        x, y, w, h = (int(t) for t in v)
        return (x, y, w, h)
    except (TypeError, ValueError):
        return None


def _original_rects(rec: Dict, bw: int, bh: int) -> List[Tuple[int, int, int, int]]:
    """Where the record's photo region (photoOffset/originalSize) may sit in the upright backup
    (``bw`` x ``bh``): the recorded ``originalRect``, else the region the first caption took from
    the original (erase: same canvas, band/rebuild: layout sourceRect or the whole image). The
    caller checks each against the record's photoHash."""
    ow, oh = (_rect(list(rec.get("originalSize") or []) + [0, 0]) or (0, 0, 0, 0))[:2]
    c = [_rect(rec.get("originalRect"))]
    if rec.get("mode") == "erase":
        if list(rec.get("canvas") or []) == [bw, bh]:
            c.append(_rect(list(rec.get("photoOffset") or []) + [ow, oh]))
    else:
        c += [_rect((rec.get("layout") or {}).get("sourceRect")), (0, 0, bw, bh)]
    out = []
    for r in c:
        if r and r not in out and r[2:] == (ow, oh) and ow > 0 and oh > 0 and r[0] >= 0 and r[1] >= 0 \
                and r[0] + r[2] <= bw and r[1] + r[3] <= bh:
            out.append(r)
    return out


def _backup_candidates(real: str, saving: Dict) -> List[str]:
    """Existing backups of ``real`` (legacy names first, newest last), as plan_backup sees them."""
    base = backup_path_for(real, saving)
    legacy = _legacy_backup_base(real, saving)
    return (backup_versions(legacy) if legacy else []) + backup_versions(base)


def find_original_backup(real: str, saving: Dict, rec: Optional[Dict],
                         sha_fn: Callable[[str], str] = file_sha256) -> Optional[str]:
    """The backup holding the record's ``originalFile`` (same size and SHA-256), when that is the
    untouched original (not a backup of an already captioned file, ``captioned``)."""
    of = (rec or {}).get("originalFile") or {}
    if of.get("captioned"):
        return None
    try:
        size = int(of.get("size", -1))
    except (TypeError, ValueError):
        return None
    if not of.get("sha256") or size <= 0:
        return None
    for v in reversed(_backup_candidates(real, saving)):
        try:
            if os.path.getsize(v) == size and sha_fn(v) == of["sha256"]:
                return v
        except OSError:
            continue
    return None


@dataclass
class OriginalPhoto:
    path: str                          # the verified backup
    sha: str
    rect: Tuple[int, int, int, int]    # the photo region in the current (upright) file
    orig_rect: Tuple[int, int, int, int]   # the same region in the upright backup
    pixels: Optional[np.ndarray]       # backup[orig_rect] (dropped once used)


def original_photo(real: str, saving: Dict, rec: Optional[Dict], arr: np.ndarray) -> Tuple[Optional[OriginalPhoto], str]:
    """The untouched original pixels of this file's photo region, from its backup, or (None, why).

    ``rec`` must already be verified as describing ``arr`` (is_app_output). Used only when everything
    matches exactly: backup SHA-256 = the record's originalFile, the backup region hashes to the
    record's photoHash, and sample format and channels agree."""
    if not rec or not rec.get("originalFile"):
        return None, "no original recorded"
    if isinstance(rec["originalFile"], dict) and rec["originalFile"].get("captioned"):
        return None, "the backup holds an earlier captioned version, not the untouched original"
    bk = find_original_backup(real, saving, rec)
    if not bk:
        return None, "the backup is missing or was changed"
    try:
        st = os.stat(bk)
        a, _ = load_upright(bk)
        st2 = os.stat(bk)
        if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            return None, "the backup changed while it was read"
    except Exception as e:  # an optional speed-up: any failure (also MemoryError) falls back to the file
        return None, f"the backup could not be read: {e}"
    rect = _rect(list(rec.get("photoOffset") or []) + list(rec.get("originalSize") or []))
    if not rect:
        return None, "the record has no photo region"
    if a.dtype != arr.dtype or a.shape[2] != arr.shape[2]:
        return None, "the backup has another sample format"
    for r in _original_rects(rec, a.shape[1], a.shape[0]):
        x, y, w, h = r
        reg = a[y:y + h, x:x + w]
        if pixel_hash(reg) == rec.get("photoHash"):
            return OriginalPhoto(bk, (rec.get("originalFile") or {}).get("sha256", ""), rect, r,
                                 np.ascontiguousarray(reg)), ""
    return None, "the backup's photo does not match this file's photo"


def _shown_path(p: str, near: str) -> str:
    """``p`` relative to the photo's folder when it is inside it (_originals/x-original.jpg)."""
    try:
        r = os.path.relpath(p, os.path.dirname(near))
        return p if r.startswith("..") else r
    except ValueError:      # another drive
        return p


@dataclass
class BackupPlan:
    base: str
    sha: str
    size: int
    reuse: Optional[str] = None
    original: bool = False        # reuse holds the record's untouched original


def plan_backup(src: str, saving: Dict, prev_rec: Optional[Dict] = None,
                known: Optional[Tuple[str, str]] = None, sha_fn: Callable[[str], str] = file_sha256) -> BackupPlan:
    """Decide whether an existing backup already holds this file's original.

    ``prev_rec`` is the source's photoband record, passed only when the source was verified to
    be this app's output (is_app_output). Then the backup whose SHA-256 matches the record's
    ``originalFile`` is reused. Otherwise an existing backup is reused only if it is
    byte-identical to the current file; anything else gets a new versioned backup.
    ``known``: (path, sha256) of a backup hashed moments ago (original_photo), not hashed again.
    ``sha_fn``: how files are hashed (save_preview passes a cached one; saves always hash afresh)."""
    real = os.path.realpath(src)
    base = backup_path_for(real, saving)
    # older backups of this file count as existing ones, so an original is never backed up twice
    versions = _backup_candidates(real, saving)
    legacy = _legacy_backup_base(real, saving)
    size = os.path.getsize(real)
    if prev_rec:
        of = prev_rec.get("originalFile") or {}
        if of.get("sha256"):
            for v in reversed(versions):
                try:
                    if os.path.getsize(v) == int(of.get("size", -1)) > 0 and \
                            (known[1] if known and same_file(v, known[0]) else sha_fn(v)) == of["sha256"]:
                        # a backup recorded as already captioned is reused, but is not "the original"
                        return BackupPlan(base, of["sha256"], int(of["size"]), reuse=v,
                                          original=not of.get("captioned"))
                except OSError:
                    continue
        elif versions and os.path.basename(versions[0]).lower() in {os.path.basename(b).lower() for b in (base, legacy) if b} \
                and os.path.getsize(versions[0]) > 0 and _legacy_backup_matches(prev_rec, versions[0]):
            v = versions[0]
            return BackupPlan(base, sha_fn(v), os.path.getsize(v), reuse=v, original=True)
    sha = sha_fn(real)
    for v in reversed(versions):
        try:
            if os.path.getsize(v) == size > 0 and sha_fn(v) == sha:
                return BackupPlan(base, sha, size, reuse=v)
        except OSError:
            continue
    return BackupPlan(base, sha, size)


def _mark_backup_folder(d: str) -> None:
    m = os.path.join(d, BACKUP_MARKER)
    if os.path.exists(m):
        return
    try:
        with open(m, "x", encoding="utf-8") as fh:
            fh.write("Photoband keeps backups of original photos here. Photoband never overwrites files in "
                     "this folder and batch mode does not list them.\n")
    except OSError:
        pass


def store_copy(src: str, base: str, expect_sha: Optional[str] = None) -> str:
    """Write a verified copy of ``src`` at ``base`` (or base-2, base-3 ... if taken) and return
    its path. Unique temp name, SHA-256 checked, fsynced, placed without replacing anything."""
    d = os.path.dirname(base)
    os.makedirs(d, exist_ok=True)
    _mark_backup_folder(d)
    fd, tmp = tempfile.mkstemp(prefix=temp_prefix(".pbbak-"), suffix=".partial", dir=d)
    try:
        h = hashlib.sha256()
        n = 0
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            while True:
                b = inp.read(4 * 1024 * 1024)
                if not b:
                    break
                h.update(b)
                out.write(b)
                n += len(b)
            out.flush()
            os.fsync(out.fileno())
        sha = h.hexdigest()
        if expect_sha and sha != expect_sha:
            raise SaveError("The file changed while it was being backed up; nothing was written.", code="changed")
        if n == 0 or os.path.getsize(tmp) != n or n != os.path.getsize(src):
            raise SaveError("The backup copy has the wrong size; nothing was written.", code="backup")
        if file_sha256(tmp) != sha:
            raise SaveError("The backup copy could not be verified; nothing was written.", code="backup")
        try:
            shutil.copystat(src, tmp)  # dates, permissions (and xattrs on Linux)
        except OSError:
            pass
        i = 1
        while True:
            dst = _versioned(base, i)
            try:
                place_exclusive(tmp, dst)
                break
            except FileExistsError:
                # placed by someone else meanwhile: identical content is as good as ours
                try:
                    if os.path.getsize(dst) == n and file_sha256(dst) == sha:
                        os.unlink(tmp)
                        break
                except OSError:
                    pass
                i += 1
                if i > 9999:
                    raise SaveError("Too many backups of this file.", code="backup")
        fsync_dir(d)
        return dst
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_backup(src: str, saving: Dict, plan: Optional[BackupPlan] = None) -> str:
    """Back up the untouched source unless ``plan`` found a backup that already holds it."""
    real = os.path.realpath(src)
    plan = plan or plan_backup(real, saving, None)
    if plan.reuse:
        try:
            if os.path.getsize(plan.reuse) == plan.size > 0:
                return plan.reuse
        except OSError:
            pass
        plan = plan_backup(real, saving, None)
        if plan.reuse:
            return plan.reuse
    return store_copy(real, plan.base, expect_sha=plan.sha)


def labelled_backup_base(path: str, saving: Dict, label: str) -> str:
    """<backups>/<stem>-<label><ext>: a copy kept beside the backups that is not the original
    (one name rule for every such copy: replaced, before-batch, before-restore)."""
    stem, ext = os.path.splitext(backup_path_for(path, saving))
    stem = stem[:-len(BACKUP_SUFFIX)] if stem.endswith(BACKUP_SUFFIX) else stem
    return f"{stem}-{label}{ext}"


def keep_aside(path: str, saving: Dict, label: str = "replaced") -> str:
    """Keep a copy of a file that is about to be replaced (not a Photoband output) next to the
    backups: <backups>/<stem>-<label><ext>."""
    return store_copy(os.path.realpath(path), labelled_backup_base(path, saving, label))


# metadata writing lives in metawrite.py
from .metawrite import (MetadataError, build_region_updates, mwg_orientation, remap_box,  # noqa: E402,F401
                        write_metadata)


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------

LOG_ROTATE_BYTES = 10 * 1024 * 1024


def _append_rotating(name: str, text: str) -> None:
    """Append to logs/<name>; past 10 MB the file is first moved to <name>.1 (one old file kept)."""
    p = os.path.join(paths.sub("logs"), name)
    from .shared_state import interprocess_lock
    # two windows: without the lock both could rotate (the second move replaces the first's .1)
    # or one could append to the file the other is moving
    with interprocess_lock(p + ".lock"):
        try:
            if os.path.exists(p) and os.path.getsize(p) > LOG_ROTATE_BYTES:
                os.replace(p, p + ".1")
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError:
            log.debug("could not write %s", p, exc_info=True)


def append_log(entry: Dict) -> None:
    _append_rotating("save.log", json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(limit: int = 500) -> List[Dict]:
    p = os.path.join(paths.sub("logs"), "save.log")
    out = []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh.readlines()[-limit:]:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return list(reversed(out))


# --------------------------------------------------------------------------
# verification helpers
# --------------------------------------------------------------------------

def expected_output_pixels(a: np.ndarray, info: ImageInfo, out_fmt: str) -> np.ndarray:
    """What write_image stores for ``a`` in ``out_fmt`` before lossy encoding (imageio owns the
    conversions; this falls back to the basic JPEG rules if it does not provide them)."""
    from . import imageio as _imageio
    fn = getattr(_imageio, "expected_output_pixels", None)
    if fn is not None:
        return fn(a, info, out_fmt)
    if out_fmt == "JPEG":
        if a.dtype == np.uint16:
            a = (a >> 8).astype(np.uint8)
        if a.shape[2] in (2, 4):
            a = a[:, :, : a.shape[2] - 1]
    return a


def _downsample(a: np.ndarray, f: int) -> np.ndarray:
    """Block mean by ``f`` in row chunks (never a float copy of the whole image)."""
    H, W, C = a.shape
    Hb, Wb = H // f, W // f
    out = np.empty((Hb, Wb, C), np.float32)
    for r in range(Hb):
        blk = a[r * f:(r + 1) * f, :Wb * f].astype(np.float32)
        out[r] = blk.reshape(f, Wb, f, C).mean(axis=(0, 2))
    return out


def lossy_similarity(written: np.ndarray, ref: np.ndarray) -> Tuple[float, float]:
    """(mean absolute difference in 8-bit levels, PSNR in dB) on a version downsampled to
    at most 512 px, for lossy outputs where an exact hash cannot match."""
    scale = 255.0 / (65535.0 if ref.dtype == np.uint16 else 255.0)
    f = max(1, int(math.ceil(max(ref.shape[0], ref.shape[1]) / 512.0)))
    if ref.shape[0] < f or ref.shape[1] < f:
        f = 1
    a = _downsample(written, f) * scale
    b = _downsample(ref, f) * scale
    d = np.abs(a - b)
    mad = float(d.mean())
    mse = float((d * d).mean())
    psnr = float("inf") if mse == 0 else 10.0 * math.log10(255.0 ** 2 / mse)
    return mad, psnr


# JPEG re-encoding of grainy scans can cost a few levels; a wrong photo (flipped, shifted,
# another file) costs far more. Measured on block means (≤512 px), so grain averages out.
LOSSY_MAX_MAD = 6.0
LOSSY_MIN_PSNR = 28.0


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

# layout keys the payload keeps: the geometry of what is printed (never the editor's warnings, per-block
# boxes, scale or shrink diagnostics, nor the text runs, which the block text already carries)
_PAYLOAD_LAYOUT_KEYS = ("version", "mode", "sourceRect", "canvas", "photoRect", "fills", "bandColor", "protect",
                        "textAreas", "textColors")


def payload_for_marker(rec: Dict) -> bytes:
    """What the fragile payload carries: only what is printed plus layout, and how the file was
    saved (``saveMode``: a copy or the photo captioned in place), which decides whether a batch may
    caption the file again (existing.provenance) and must not change when the metadata is stripped.
    Never originalText or other metadata. Block text is kept only for blocks
    that were actually laid out (have runs); any other block is stored empty.

    The JSON is additive: readers ignore keys they don't know, and a payload without a key (one
    written before it existed) reads as "unknown". See photoband/record.py for every record field
    and whether it survives metadata stripping."""
    lay_in = rec.get("layout") or {}
    lay = {k: lay_in[k] for k in _PAYLOAD_LAYOUT_KEYS if k in lay_in}
    runs = lay_in.get("runs")
    printed = None              # unknown (no runs, or runs without block ids): keep every block
    if isinstance(runs, list):
        printed = {r["block"] for r in runs if isinstance(r, dict) and "block" in r} or None
    blocks = []
    for b in rec.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        keep = printed is None or b.get("id") in printed
        blocks.append({"id": b.get("id"), "custom": bool(b.get("custom")), "text": (b.get("text") or "") if keep else ""})
    p = {"v": 1, "blocks": blocks, "template": rec.get("template"),
         "overrides": rec.get("overrides"), "layout": lay,
         "originalSize": rec.get("originalSize"), "photoOffset": rec.get("photoOffset"),
         "canvas": rec.get("canvas"), "photoHash": rec.get("photoHash")}
    if rec.get("saveMode") in ("copy", "overwrite"):
        p["saveMode"] = rec["saveMode"]
    if rec.get("faceRows"):
        p["faceRows"] = rec["faceRows"]
    return zlib.compress(json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), 9)


def decode_marker_payload(b: Optional[bytes]) -> Optional[Dict]:
    if not b:
        return None
    try:
        from .record import safe_decompress
        return json.loads(safe_decompress(b).decode("utf-8"))  # capped: a crafted file can't zip-bomb us
    except Exception:
        return None


def _permission_error(e: PermissionError, target: str) -> SaveError:
    w = getattr(e, "winerror", None)
    if w == 32 or (w == 5 and not is_readonly(target) and held_open_elsewhere(target)):
        # replacing a file another app holds open is "access denied" on Windows, not a sharing violation
        return SaveError("The file is open in another app. Close it there and try again; your edits are kept.",
                         code="locked")
    if w == 5:
        return SaveError("Windows denied access to the file: it may be read-only, or you may not have permission "
                         "to change it. Your edits are kept.", code="denied")
    if not dir_writable(os.path.dirname(target)):
        return SaveError("The folder is not writable. Your edits are kept.", code="readonly_folder")
    if os.path.exists(target) and is_readonly(target):
        return SaveError("The file is read-only. Your edits are kept.", code="readonly")
    return SaveError("Permission denied while writing the file. Your edits are kept.", code="denied")


def cached_existing(path: str, info: Optional[ImageInfo] = None) -> Optional[Dict]:
    """The existing-text analysis the app already made of ``path`` when the photo was opened or
    pre-flighted (memory or disk cache, this version of the file only); None when none is cached."""
    try:
        from . import photos
        info = info or probe(path)
        with photos._lock:
            hit = photos._cache.get(photos._key(path, info))
        if hit and hit.get("existing") is not None:
            return hit["existing"]
        res, _ = photos._load_existing(path, info, False)
        if isinstance(res, dict):
            return res
    except Exception:
        log.debug("no cached analysis for %s", path, exc_info=True)
    return None


def cached_case(path: str, info: Optional[ImageInfo] = None) -> Optional[str]:
    """The existing-text case (A/B/C/D/None) of ``path`` from the analysis the app already made
    when the photo was opened or pre-flighted (memory or disk cache); "" when none is cached."""
    ex = cached_existing(path, info)
    return ex.get("case") if ex is not None else ""


def photoband_output_without_record(path: str) -> bool:
    """True when the cached analysis of ``path`` found this app's hidden marker although the file
    has no record (its metadata was stripped): it IS a captioned file, only its record is gone."""
    ex = cached_existing(path)
    return bool(ex) and ex.get("source") in ("marker", "marker+payload")


def detect_case(path: str, info: ImageInfo, md: Dict, arr: np.ndarray) -> Optional[str]:
    """The existing-text case of the file being saved, worked out here rather than trusted from
    the client: the cached analysis, else a quick analysis (no OCR) of the decoded pixels."""
    c = cached_case(path, info)
    if c != "":
        return c
    from .existing import analyze_existing
    try:
        return analyze_existing(arr, info, md, run_ocr=False).get("case")
    except Exception:
        # the overwrite still makes a verified backup first; don't block it on a detector bug
        log.warning("existing-text check failed for %s", path, exc_info=True)
        return None


def _creation_time_kept(path: str, want_ns: int) -> bool:
    try:
        got = creation_time_ns(os.stat(path))
    except OSError:
        return False
    return bool(want_ns and got and abs(got - want_ns) < 2_000_000_000)


def set_creation_time_macos(path: str, ns: int) -> bool:
    """macOS: set the file's creation (birth) date with setattrlist(ATTR_CMN_CRTIME)."""
    if sys.platform != "darwin" or not ns:
        return False
    try:
        import ctypes
        import ctypes.util

        class _AttrList(ctypes.Structure):   # <sys/attr.h> struct attrlist
            _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                        ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                        ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                        ("forkattr", ctypes.c_uint32)]

        class _Timespec(ctypes.Structure):
            _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

        ATTR_BIT_MAP_COUNT, ATTR_CMN_CRTIME = 5, 0x00000200
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        fn = libc.setattrlist
        fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]
        fn.restype = ctypes.c_int
        al = _AttrList(ATTR_BIT_MAP_COUNT, 0, ATTR_CMN_CRTIME, 0, 0, 0, 0)
        ts = _Timespec(int(ns // 1_000_000_000), int(ns % 1_000_000_000))
        return fn(os.fsencode(path), ctypes.byref(al), ctypes.byref(ts), ctypes.sizeof(ts), 0) == 0
    except Exception:
        log.debug("setattrlist failed for %s", path, exc_info=True)
        return False


def keep_creation_time(path: str, src_stat: os.stat_result) -> bool:
    """Give ``path`` the source's creation date where the OS allows it (Windows, macOS). True
    only when the date was read back from the file afterwards, so a False always means a note."""
    want = creation_time_ns(src_stat)
    if not want:
        return False
    if os.name == "nt":
        set_creation_time(path, want)
    elif sys.platform == "darwin":
        set_creation_time_macos(path, want)
    return _creation_time_kept(path, want)


def _unchanged(path: str, size: int, mtime_ns: int, want: Tuple, opened_hash: Optional[str],
               file_id: Optional[int] = None) -> bool:
    """Is the file still the one opened as ``want`` (size, mtime_ns[, quick_hash[, file_id]])? Same
    size, modified time and file id: yes, without reading it (the owner's choice). A file replaced
    by a same-size one whose modified time was kept (a metadata edit with "keep file dates") has
    another file id, so then, and otherwise, the content decides,
    so a sync, backup or antivirus tool that only touched the modified time doesn't block the save:
    the whole file against its full hash from when it was opened (taken in the background when the
    photo opens), or, before that hash exists, the quick hash (first and last MB)."""
    vals = list(want)
    size0, mtime0 = int(vals[0]), int(vals[1])
    fid0 = _int_or_none(vals[3]) if len(vals) > 3 else None
    replaced = bool(fid0 and file_id and fid0 != file_id)
    if (size, mtime_ns) == (size0, mtime0) and not replaced:
        return True
    if size != size0:
        return False
    full = opened_full_hash(path, size0, mtime0, fid0)
    if full:
        return file_sha256(path) == full
    h = (str(vals[2]) if len(vals) > 2 and vals[2] else None) or opened_hash
    return bool(h) and quick_hash(path) == h


def _int_or_none(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _details_for(req: SaveRequest, md: Dict, info: ImageInfo):
    """(edits, the source's caption fields) for a save that writes edited details; ({}, None) without."""
    from . import metaedit
    from .metadata import normalize
    try:
        edits = metaedit.validate(req.meta_edits)
    except metaedit.EditError as e:
        raise SaveError(f"The edited details can't be saved: {e}", code="details")
    if not edits:
        return {}, None
    fields = normalize(md, info)["fields"]
    if metaedit.missing_faces(fields, edits.get("faces") or {}):
        raise SaveError("The faces in this file changed since you edited them. Reload the photo and check your "
                        "face edits.", code="changed")
    return edits, fields


def _named_fields(fields: Optional[Dict], edits: Dict) -> Optional[Dict]:
    """The fields a file-name pattern sees: with the edited details applied."""
    if not edits or fields is None:
        return fields
    from .metaedit import apply_to_fields
    return apply_to_fields(fields, edits)


def _check_expected(req: SaveRequest, real: str, info: ImageInfo) -> None:
    if req.expected_stat and not _unchanged(real, info.size_bytes, info.mtime_ns, req.expected_stat, None,
                                            info.file_id):
        raise SaveError("The file changed on disk after it was opened.", code="changed")
    # batch jobs carry only the content fingerprint taken when the batch was staged
    if req.expected_hash and not req.expected_stat and req.expected_hash != quick_hash(real):
        raise SaveError("The file changed on disk after it was opened.", code="changed")


def save(req: SaveRequest) -> SaveResult:
    t0 = time.time()
    src = os.path.abspath(req.path)
    res = SaveResult(ok=False, path=src)
    saving = req.settings.get("saving", {})
    tmp = None
    locks = []
    try:
        if not os.path.exists(src):
            raise SaveError("The original file no longer exists.", code="missing")
        real = os.path.realpath(src)   # overwrite the file a link points to, never the link
        # destination (a "copy as" onto the source itself is an overwrite: backup, guards and all)
        if req.mode == "copyAs":
            if not req.dest_path:
                raise SaveError("No destination chosen.", code="dest")
            dest = os.path.abspath(req.dest_path)
            if same_file(dest, src):
                if req.on_exists != "overwrite":
                    # the UI routes this to its "Overwrite original" confirmation
                    raise SaveError(dest, code="source")
                req.mode = "overwrite"
        if req.mode == "overwrite":
            if is_backup_location(real, saving):
                raise SaveError("This file is in a backups folder. Backups are never overwritten; save a copy "
                                "instead.", code="backup_folder")
            if is_readonly(real):
                raise SaveError("The file is read-only. Make it writable, or save a copy.", code="readonly")
            try:
                locks.append(file_lock(real).acquire())
            except LockBusy:
                raise SaveError("This photo is being saved by another Photoband window or batch. Try again when it "
                                "finishes.", code="busy")
        info = probe(real)
        _check_expected(req, real, info)
        if info.save_blocked:
            raise SaveError(info.save_blocked, code="blocked")
        if info.pages > 1 and not saving.get("allowMultipageSave"):
            raise SaveError("This multi-page TIFF would lose its other pages. Allow it in Settings › Saving.",
                            code="multipage")
        batch_erase = bool(req.batch_job) and (req.layout or {}).get("mode") == "erase"
        if req.mode == "overwrite" and req.case == "C":
            _refuse_case_c(saving, batch_erase)
        et = get_exiftool()
        md = et.read_json(real)
        edits, src_fields = _details_for(req, md, info)

        # the source pixels, decoded once: early only if picking the destination needs the
        # photo's pixel identity (an existing file whose sourceKey did not match)
        px_cache: Dict[str, np.ndarray] = {}

        def source_pixels() -> np.ndarray:
            if "arr" not in px_cache:
                px_cache["arr"] = upright(read_pixels(info), info.orientation)
            return px_cache["arr"]

        def _ids_fn():
            if "arr" in px_cache or "gone" not in px_cache:
                return source_photo_hashes(source_pixels(), md, req.layout)
            a, _i = load_upright(real)        # pixels already freed (rare): decode again
            return source_photo_hashes(a, md, req.layout)
        src_ids = SourceIds(_ids_fn)

        # destination
        replace_existing = False
        if req.mode == "overwrite":
            out_path, out_fmt = real, info.format
            replace_existing = True
        elif req.mode == "copyAs":
            out_path = os.path.abspath(req.dest_path)
            ext = os.path.splitext(out_path)[1].lower()
            out_fmt = {".tif": "TIFF", ".tiff": "TIFF", ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG"}.get(ext)
            if not out_fmt:
                raise SaveError("Choose a .tif, .jpg or .png file name.", code="dest")
            if is_backup_location(out_path, saving):
                raise SaveError("That location is a backups folder. Choose another place for the copy.", code="dest")
            if os.path.exists(out_path):
                if req.on_exists != "overwrite":
                    # never silently replace another file (possibly another photo's original)
                    raise SaveError(out_path, code="exists")
                out_path = os.path.realpath(out_path)
                replace_existing = True
        else:
            out_path, out_fmt = destination_for(src, info, saving, _named_fields(req.fields, edits), req.template_name,
                                                req.on_exists, notes=res.notes, src_ids=src_ids)
            replace_existing = os.path.exists(out_path)
            if replace_existing:
                out_path = os.path.realpath(out_path)
        if replace_existing and req.mode != "overwrite":
            if is_backup_location(out_path, saving):
                raise SaveError("That file is in a backups folder and is never replaced.", code="dest")
            try:
                locks.append(file_lock(out_path).acquire())
            except LockBusy:
                raise SaveError("That file is being saved by another Photoband window or batch.", code="busy")
        res.out_path = out_path
        res.format = out_fmt
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

        # pixels
        arr = source_pixels()
        if req.mode == "overwrite" and (batch_erase or not saving.get("allowOverwriteHandwritten")):
            # the client's "case" is a hint only (the editor sends none in band mode): a scan
            # with a handwritten caption is protected whatever it says
            if detect_case(real, info, md, arr) == "C":
                _refuse_case_c(saving, batch_erase)
        # an earlier output of this app (verified now, before any pixel is swapped)
        md_rec = record.from_metadata(md)
        prev = md_rec if is_app_output(md_rec, arr) else None
        # its photo region is put back from the untouched original backup, so a re-caption is
        # always one generation from the original (JPEG); the geometry stays the current file's
        orig: Optional[OriginalPhoto] = None
        if prev and prev.get("originalFile"):
            orig, why = original_photo(real, saving, prev, arr)
            if orig is None:
                res.notes.append(f"The original backup was not used ({why}); the photo was taken from this file")
        elif md_rec and md_rec.get("originalFile"):
            res.notes.append("The original backup was not used (this file was changed since Photoband saved it); "
                             "the photo was taken from this file")
        if orig is not None:
            if not arr.flags.writeable:
                arr = px_cache["arr"] = arr.copy()
            x, y, w, h = orig.rect
            arr[y:y + h, x:x + w] = orig.pixels
            orig.pixels = None
            res.notes.append(f"Photo taken from the original backup ({_shown_path(orig.path, real)})")
        if info.orientation != 1:
            res.notes.append(f"Rotated upright from EXIF orientation {info.orientation}")
        lay = req.layout
        erase_mask = erase_band = None
        if lay.get("mode") == "erase":
            erase_mask, erase_band = _erase_inputs(arr, req.erase or {})
        comp: CompositeResult = composite(arr, info.icc, lay, req.tiles, erase_mask, erase_band,
                                          (req.erase or {}).get("method", "auto"))
        canvas = comp.canvas
        Hc, Wc = canvas.shape[:2]
        if lay.get("mode") == "erase":
            source_rect = (0, 0, arr.shape[1], arr.shape[0])
            px, py, pw, ph = comp.photo_rect
            photo_src = arr[py:py + ph, px:px + pw]
            # regions: identity (same canvas as source)
            region_source_rect = source_rect
            region_photo_rect = (0, 0, Wc, Hc)
        else:
            sx, sy, sw, sh = (int(v) for v in lay.get("sourceRect") or (0, 0, arr.shape[1], arr.shape[0]))
            source_rect = (sx, sy, sw, sh)
            photo_src = arr[sy:sy + sh, sx:sx + sw]
            region_source_rect = source_rect
            region_photo_rect = comp.photo_rect
        photo_hash = pixel_hash(photo_src)

        rec = {
            "app": "Photoband", "appVersion": __version__,
            "saved": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "templateId": req.state.get("templateId"), "template": req.state.get("template"),
            "overrides": req.state.get("overrides"), "blocks": req.state.get("blocks"),
            "faceRows": req.state.get("faceRows") if isinstance(req.state.get("faceRows"), int) else None,
            "layout": {k: v for k, v in lay.items() if k != "runs"} | {"runs": lay.get("runs", [])},
            "originalSize": [int(photo_src.shape[1]), int(photo_src.shape[0])],
            "photoOffset": [int(comp.photo_rect[0]), int(comp.photo_rect[1])],
            "canvas": [Wc, Hc],
            "photoHash": photo_hash,
            "mode": lay.get("mode", "band"),
            "sourceFormat": info.format, "bits": info.bits,
        }
        if req.original_text:
            rec["originalText"] = req.original_text
        if req.batch_job:
            rec["batchJob"] = req.batch_job
        # which photo this was made from: "overwrite" replaces only earlier copies of the same photo
        rec["sourceKey"] = source_key(real)
        # a file captioned in place is a photo, never "an earlier copy" (see is_output_of)
        rec["saveMode"] = "overwrite" if req.mode == "overwrite" else "copy"

        # which backup holds this file's original (decided before the record is written, since
        # the record carries the original's identity forward through re-captions)
        plan: Optional[BackupPlan] = None
        if req.mode == "overwrite":
            if saving.get("backupOriginals", True):
                plan = plan_backup(real, saving, prev, known=(orig.path, orig.sha) if orig else None)
                rec["originalFile"] = {"sha256": plan.sha, "size": plan.size}
                if not plan.original and (md_rec is not None or photoband_output_without_record(real)):
                    # the backup holds this file as it is, already captioned by this app (its untouched
                    # original wasn't found, it was changed elsewhere, or its record was stripped): say
                    # so, so no later save or preview takes it for the untouched original
                    rec["originalFile"]["captioned"] = True
            elif prev and prev.get("originalFile"):
                rec["originalFile"] = prev["originalFile"]
            # where the new photo region sits in that original, when it is all original pixels
            if orig is not None and (rec.get("originalFile") or {}).get("sha256") == orig.sha:
                ox, oy, ow, oh = orig.rect
                nx, ny, nw, nh = (int(v) for v in (comp.photo_rect if lay.get("mode") == "erase" else source_rect))
                if ox <= nx and oy <= ny and nx + nw <= ox + ow and ny + nh <= oy + oh:
                    rec["originalRect"] = [orig.orig_rect[0] + nx - ox, orig.orig_rect[1] + ny - oy, nw, nh]

        # hidden marker
        embed = req.embed_marker if req.embed_marker is not None else saving.get("embedMarker", True)
        remove_requested = req.embed_marker is False      # File > "Remove hidden marker" / save copy without it
        from . import marker
        band_units = tuple(int(v) for v in comp.band_color)
        if lay.get("mode") == "erase":
            # the band is the file's own, so an earlier marker (and its payload, which holds the OLD caption)
            # is still in it: clear it first, whether or not a new one is written
            try:
                marker.scrub(canvas, comp.photo_rect, band_units, exclude=comp.protect)
            except Exception as e:
                res.notes.append(f"Couldn't clear the old hidden marker from the band: {e}")
        written = False
        if embed:
            pay = payload_for_marker(rec) if out_fmt in ("TIFF", "PNG") else None
            try:
                mr = marker.embed(canvas, comp.photo_rect, band_units, photo_hash, payload=pay, exclude=comp.protect)
                res.marker = {"robust": mr.robust, "payload": mr.payload, "reason": mr.reason}
                if not mr.robust:
                    res.notes.append(f"Hidden marker not written: {mr.reason}")
                else:
                    written = True
                    chk = marker.read(canvas, comp.photo_rect)
                    good = (chk is not None and chk.short_hash == marker.short_hash(photo_hash)
                            and tuple(chk.photo_rect) == tuple(int(v) for v in comp.photo_rect)
                            and (not mr.payload or chk.payload == pay))
                    res.marker["verified"] = bool(good)
                    if not good:
                        res.notes.append("Hidden marker written but it could not be read back; the file is fine, "
                                         "but may not identify itself if its metadata is stripped.")
                    elif mr.reason:
                        res.notes.append(f"Hidden marker: {mr.reason}")
            except Exception as e:  # marker is a convenience layer; never block a save
                res.marker = {"robust": False, "payload": False, "reason": f"error: {e}"}
                res.notes.append(f"Hidden marker skipped: {e}")
        else:
            res.marker = {"robust": False, "payload": False,
                          "reason": "removed on request" if remove_requested else "turned off in settings"}
        if not written and lay.get("mode") == "erase":
            # nothing new was written: no old marker may be left behind either
            try:
                left = marker.read(canvas, comp.photo_rect)
            except Exception:
                left = None
            if left is not None:
                if remove_requested:
                    raise SaveError("The hidden marker could not be removed from this band, so the copy was not "
                                    "saved. Try Rebuild mode, which draws a new band.", code="marker_remove")
                res.notes.append("An old hidden marker could not be removed from the band.")

        # encode to a temp file next to the destination
        from . import imageio as _imageio
        d = os.path.dirname(out_path)
        fd, tmp = tempfile.mkstemp(prefix=temp_prefix(".pbtmp-"), suffix=EXT_FOR[out_fmt], dir=d)
        os.close(fd)
        q = int(saving.get("jpegQuality", 95))
        res.notes += _imageio.write_image(tmp, canvas, info, out_fmt, q)

        # the canvas is on disk now; don't hold it through metadata and verify
        del canvas
        comp.canvas = None
        px, py, pw, ph = comp.photo_rect
        # output photo hash as decoded (differs from source for JPEG). Lossless outputs store
        # the photo pixels exactly (checked by the verify step below), so their hash is the
        # source's and no extra decode is needed.
        if out_fmt == "JPEG":
            out_info = probe(tmp)
            out_arr = read_pixels(out_info, check_size=False)
            out_photo = out_arr[py:py + ph, px:px + pw]
            rec["outputPhotoHash"] = pixel_hash(out_photo)
            del out_arr, out_photo
        else:
            rec["outputPhotoHash"] = photo_hash
        ref = expected_output_pixels(photo_src, info, out_fmt)
        ref_dtype, ref_shape = ref.dtype, ref.shape
        want = None
        if out_fmt != "JPEG":
            # lossless: only the hash is needed; free the source pixels before decoding again
            want = photo_hash if ref is photo_src else pixel_hash(ref)
            if replace_existing and req.mode != "overwrite" and not src_ids.computed:
                # the replace below asks is_output_of again: hash now, while the pixels are in memory,
                # if the existing file's sourceKey does not already say it is ours
                rec_x = _record_of(out_path)
                if rec_x and rec_x.get("sourceKey") != source_key(real):
                    src_ids()
            del ref, photo_src, arr
            px_cache.clear()
            px_cache["gone"] = True

        res.notes += write_metadata(tmp, real, md, info, out_fmt, (Wc, Hc),
                                    comp.photo_rect if lay.get("mode") != "erase" else region_photo_rect,
                                    region_source_rect, rec, edits=edits, fields=src_fields)
        if edits:
            from .metaedit import describe
            res.notes.append("Details changed: " + "; ".join(describe(src_fields, edits)))

        # verify: size, orientation, sample format, and the photo region against the source
        v_info = probe(tmp)
        if (v_info.width, v_info.height) != (Wc, Hc):
            raise SaveError("Verification failed: the written image has the wrong size.", code="verify")
        if v_info.orientation not in (1,):
            raise SaveError("Verification failed: orientation was not reset.", code="verify")
        v_arr = read_pixels(v_info, check_size=False)
        if v_arr.dtype != ref_dtype or v_arr.shape[2] != ref_shape[2]:
            raise SaveError(f"Verification failed: the written image is {v_arr.dtype} with {v_arr.shape[2]} "
                            f"channel(s), expected {ref_dtype} with {ref_shape[2]}.", code="verify")
        v_photo = v_arr[py:py + ph, px:px + pw]
        if v_photo.shape != ref_shape:
            raise SaveError("Verification failed: the photo region has the wrong size.", code="verify")
        if out_fmt == "JPEG":
            mad, psnr = lossy_similarity(v_photo, ref)
            if not (mad < LOSSY_MAX_MAD and psnr > LOSSY_MIN_PSNR):
                raise SaveError(f"Verification failed: the photo in the written JPEG does not match the source "
                                f"(difference {mad:.1f} levels, {psnr:.1f} dB).", code="verify")
            res.notes.append(f"Verified: photo matches the source within JPEG tolerance ({psnr:.1f} dB)")
        else:
            if pixel_hash(v_photo) != want:
                raise SaveError("Verification failed: photo pixels changed during the save.", code="verify")
            res.notes.append("Verified: photo pixels identical to the source")
        del v_arr, v_photo
        ref = None
        if req.mode == "overwrite":
            failed = copy_xattrs(real, tmp)
            if failed:
                res.notes.append("Some extended attributes could not be kept: " + ", ".join(failed))
            try:
                if os.stat(real).st_nlink > 1:
                    res.notes.append("The file was hard-linked; its other links keep the old file")
            except OSError:
                pass
        # flushed while the temp file is still ours to write: the permissions of a read-only
        # original (copied next) would make it impossible to open for writing
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        # mkstemp files are 0600: keep the original's permissions (a copy stays writable)
        _copy_mode(real, tmp, writable=req.mode != "overwrite")

        # back up, then replace
        if req.mode == "overwrite":
            if plan is not None:
                res.backup_path = ensure_backup(real, saving, plan)
                restore = res.backup_path
                if req.batch_job and plan.reuse:
                    # the reused backup is the file's long-term original (from before an earlier
                    # caption); the batch must be able to put back the file as it is NOW
                    cur_sha = file_sha256(real)
                    if cur_sha != plan.sha:
                        restore = store_copy(real, labelled_backup_base(real, saving, "before-batch"),
                                             expect_sha=cur_sha)
                        res.restore_path = restore
                if req.on_backup:
                    req.on_backup(restore)
            # the source must not have changed while we worked
            st = os.stat(real)
            exp = list(req.expected_stat or ())
            opened_hash = (exp[2] if len(exp) > 2 else None) or req.expected_hash
            if not _unchanged(real, st.st_size, st.st_mtime_ns, (info.size_bytes, info.mtime_ns, None, info.file_id),
                              opened_hash, st.st_ino):
                raise SaveError("The file changed on disk during the save; nothing was written.", code="changed")
        elif replace_existing and os.path.exists(out_path) and not is_output_of(out_path, real, src_ids):
            # the user confirmed replacing this file, but it is not an earlier copy of this photo
            # (another photo, another photo's captioned copy, another app's file): keep it
            kept = keep_aside(out_path, saving)
            res.notes.append(f"The replaced file was kept at {kept}")
        src_stat = os.stat(real)
        try:
            if replace_existing:
                replace_with_retry(tmp, out_path)
            else:
                while True:
                    try:
                        place_exclusive(tmp, out_path)
                        break
                    except FileExistsError:
                        # appeared since the destination was chosen (another save or app)
                        if req.mode == "copy" and (req.on_exists or saving.get("onExists", "increment")) \
                                not in ("ask", "overwrite"):
                            out_path = _next_free(out_path)
                            res.out_path = out_path
                            continue
                        raise SaveError(out_path, code="exists")
        except PermissionError as e:
            raise _permission_error(e, out_path)
        tmp = None
        if saving.get("keepFileDates"):
            try:
                os.utime(out_path, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
            except OSError:
                res.notes.append("The file's modified date could not be kept")
            if not keep_creation_time(out_path, src_stat):
                res.notes.append("The file's creation date could not be kept")
        fsync_dir(os.path.dirname(out_path))
        res.ok = True
        res.size = (Wc, Hc)
    except SaveError as e:
        res.error, res.code = str(e), e.code
    except MetadataError as e:
        res.error, res.code = str(e), "metadata"
    except (ImageError, LayoutError, ExifToolError) as e:
        res.error, res.code = str(e), "save_failed"
    except MemoryError:
        res.error, res.code = "Not enough memory to save this image.", "memory"
    except PermissionError as e:
        se = _permission_error(e, getattr(e, "filename", None) or res.out_path or src)
        res.error, res.code = str(se), se.code
    except OSError as e:
        res.error, res.code = f"{e.strerror or e}: {getattr(e, 'filename', '') or ''}".strip(": "), "io"
    except Exception as e:  # never let an unexpected error escape without cleanup and a log entry
        import traceback
        res.error, res.code = f"Unexpected error: {e}", "save_failed"
        _append_rotating("error.log", f"{_dt.datetime.now().isoformat()} save {src}\n{traceback.format_exc()}\n")
        log.error("unexpected error saving %s", src, exc_info=True)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        for lk in reversed(locks):
            lk.release()
        res.elapsed_ms = int((time.time() - t0) * 1000)
        append_log({"time": _dt.datetime.now().isoformat(timespec="seconds"), "ok": res.ok, "source": src,
                    "output": res.out_path, "mode": req.mode, "format": res.format, "backup": res.backup_path,
                    "marker": res.marker, "notes": res.notes, "error": res.error, "ms": res.elapsed_ms,
                    "batch": req.batch_job})
    return res


CASE_C_OVERWRITE_MSG = ("This scan has a handwritten or printed caption, so overwriting it is turned off. "
                        "Save a copy instead, or allow it in Settings › Saving.")


def _refuse_case_c(saving: Dict, batch_erase: bool) -> None:
    if batch_erase:
        raise SaveError("Erasing writing on a print's border in a batch is done on copies only; the original was not "
                        "changed.", code="case_c_overwrite")
    if not saving.get("allowOverwriteHandwritten"):
        # every mode (band, rebuild, erase): spec "For case C, Overwrite original is disabled until the
        # user turns it on in Settings"; the UI disables the button for case C to match
        raise SaveError(CASE_C_OVERWRITE_MSG, code="case_c_overwrite")


def _fsync_dir(d: str) -> None:  # kept for callers of the old name
    fsync_dir(d)


def _erase_inputs(arr: np.ndarray, erase: Dict[str, Any]):
    from .detect import detect_band
    from .erase import build_mask
    from .existing import band_from_json, blocks_from_json, decode_mask_png
    band = band_from_json(erase.get("band")) if erase.get("band") else detect_band(arr)
    if erase.get("photoRect"):
        band.photo_rect = tuple(int(v) for v in erase["photoRect"])
    blocks = blocks_from_json(erase.get("blocks") or [])
    try:
        add = decode_mask_png(erase.get("brushAdd"))
        rem = decode_mask_png(erase.get("brushRemove"))
    except ValueError as e:   # an oversized brush mask: a clear refusal, not an "unexpected error"
        raise SaveError(str(e), code="mask")
    mask = build_mask(arr, band, blocks, grow=int(erase.get("grow", 2)), add_mask=add, remove_mask=rem)
    return mask, band


# --------------------------------------------------------------------------
# details only: edited metadata written into the original, pixels untouched
# --------------------------------------------------------------------------

def image_data_hash(path: str) -> str:
    """A hash of the image data alone (not the metadata): ExifTool's ImageDataHash, or, when the
    ExifTool in use can't compute it, a hash of the decoded pixels."""
    try:
        out, _ = get_exiftool().execute("-s3", "-ImageDataHash", "-api", "ImageHashType=SHA256", files=[path])
        h = out.strip()
        if re.fullmatch(r"[0-9a-f]{64}", h):
            return "x:" + h
    except ExifToolError:
        pass
    arr, _info = load_upright(path)
    return "p:" + pixel_hash(arr)


def save_details(path: str, meta_edits: Dict[str, Any], settings: Dict[str, Any],
                 expected_stat: Optional[Sequence] = None) -> SaveResult:
    """Write edited details (metaedit) into the photo itself, changing nothing else.

    The same guards as an overwrite (not in a backups folder, not read-only, locked against other
    saves, unchanged since it was opened); the work is done on a copy next to the file, which is
    read back (every edited detail as the reader sees it) and checked to hold exactly the same image
    data before it replaces the file. With Backup on, the file is backed up first unless a backup of
    it already exists (each details save would otherwise keep another full copy of the photo; the
    save log lists every old value instead)."""
    from . import metaedit
    from .exiftool import parse_warnings
    from .metadata import normalize
    from .metawrite import _check_too_large, _is_bigtiff
    t0 = time.time()
    src = os.path.abspath(path)
    res = SaveResult(ok=False, path=src)
    saving = settings.get("saving", {})
    tmp = jpath = None
    lock = None
    try:
        if not os.path.exists(src):
            raise SaveError("The file no longer exists.", code="missing")
        real = os.path.realpath(src)
        if is_backup_location(real, saving):
            raise SaveError("This file is in a backups folder. Backups are never changed.", code="backup_folder")
        if is_readonly(real):
            raise SaveError("The file is read-only. Make it writable to save its details.", code="readonly")
        try:
            lock = file_lock(real).acquire()
        except LockBusy:
            raise SaveError("This photo is being saved by another Photoband window or batch. Try again when it "
                            "finishes.", code="busy")
        info = probe(real)
        if expected_stat and not _unchanged(real, info.size_bytes, info.mtime_ns, expected_stat, None, info.file_id):
            raise SaveError("The file changed on disk after it was opened.", code="changed")
        if info.format == "TIFF" and _is_bigtiff(real):
            raise SaveError("Files over 4 GB can't carry metadata (ExifTool can't write BigTIFF).", code="blocked")
        et = get_exiftool()
        md = et.read_json(real)
        try:
            edits = metaedit.validate(meta_edits)
        except metaedit.EditError as e:
            raise SaveError(f"The edited details can't be saved: {e}", code="details")
        if not edits:
            raise SaveError("There are no edited details to save.", code="nothing")
        fields = normalize(md, info)["fields"]
        if metaedit.missing_faces(fields, edits.get("faces") or {}):
            raise SaveError("The faces in this file changed since you edited them. Reload the photo and check your "
                            "face edits.", code="changed")
        upd, dels, notes = metaedit.tag_updates(md, info, fields, edits)
        reg = metaedit.region_updates(md, info, fields, edits.get("faces") or {})
        for key, inner in (("XMP-mwg-rs:RegionInfo", "RegionList"), ("XMP-MP:RegionInfoMP", "Regions"),
                           ("XMP-iptcExt:PersonInImage", None)):
            if key not in reg:
                continue
            v = reg[key]
            if (inner and not (v or {}).get(inner)) or (not inner and not v):
                dels.append(f"-{key}=")
            else:
                upd[key] = v
        if not upd and not dels:
            # nothing in the file needs to change (it already reads as edited)
            if metaedit.check_written(metaedit.apply_to_fields(fields, edits), fields, edits, boxes=True):
                raise SaveError("These details can't be written to this file.", code="details")
            res.ok = True
            res.out_path = src
            res.format = info.format
            res.notes = ["Nothing needed changing: the file already has these details"]
            return res
        notes.append("Previous values: " + metaedit.previous_values(md, metaedit.touched_tags(upd, dels)))
        before = image_data_hash(real)

        d = os.path.dirname(real)
        fd, tmp = tempfile.mkstemp(prefix=temp_prefix(".pbtmp-"), suffix=os.path.splitext(real)[1], dir=d)
        os.close(fd)
        shutil.copyfile(real, tmp)
        args = ["-n"] + dels
        if upd:
            fd, jpath = tempfile.mkstemp(suffix=".json", dir=paths.sub("tmp"))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump([{"SourceFile": "*", **upd}], fh, ensure_ascii=False)
            args += ["-struct", f"-json={jpath}"]
        try:
            err = et.write(tmp, args, require_change=False)
        except ExifToolError as e:
            raise MetadataError(f"Writing the details failed: {e}")
        if info.format == "JPEG":
            _check_too_large(err)
        notes += [f"ExifTool: {w}" for w in parse_warnings(err)]

        # read back: every edited detail as the reader sees it, and the very same image data
        got = normalize(et.read_json(tmp), probe(tmp))["fields"]
        bad = metaedit.check_written(metaedit.apply_to_fields(fields, edits), got, edits, boxes=True)
        if bad:
            raise MetadataError("Metadata check failed: your edited " + ", ".join(bad) +
                                " did not read back as written. Nothing was changed.")
        if image_data_hash(tmp) != before:
            raise SaveError("Verification failed: writing the details changed the image data. Nothing was changed.",
                            code="verify")
        failed = copy_xattrs(real, tmp)
        if failed:
            notes.append("Some extended attributes could not be kept: " + ", ".join(failed))
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        _copy_mode(real, tmp)

        if saving.get("backupOriginals", True):
            have = _backup_candidates(real, saving)
            if have:
                notes.append(f"Backup already kept ({_shown_path(have[-1], real)}); the old details are in this log")
            else:
                res.backup_path = ensure_backup(real, saving)
        st = os.stat(real)
        if not _unchanged(real, st.st_size, st.st_mtime_ns, (info.size_bytes, info.mtime_ns, None, info.file_id),
                          None, st.st_ino):
            raise SaveError("The file changed on disk while its details were saved; nothing was written.",
                            code="changed")
        try:
            replace_with_retry(tmp, real)
        except PermissionError as e:
            raise _permission_error(e, real)
        tmp = None
        if saving.get("keepFileDates"):
            try:
                os.utime(real, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                notes.append("The file's modified date could not be kept")
            if not keep_creation_time(real, st):
                notes.append("The file's creation date could not be kept")
        fsync_dir(d)
        res.ok = True
        res.out_path = src
        res.format = info.format
        res.notes = ["Details changed: " + "; ".join(metaedit.describe(fields, edits))] + notes
        try:
            from .photos import carry_caches, forget
            forget(real)   # (a same-size file with its dates kept may look like the old version)
            carry_caches(real, info, probe(real))
        except Exception:   # a cache only: the photo is analysed again if this fails
            log.debug("could not carry caches over", exc_info=True)
        hash_in_background(real)
    except SaveError as e:
        res.error, res.code = str(e), e.code
    except MetadataError as e:
        res.error, res.code = str(e), "metadata"
    except (ImageError, ExifToolError) as e:
        res.error, res.code = str(e), "save_failed"
    except PermissionError as e:
        se = _permission_error(e, getattr(e, "filename", None) or src)
        res.error, res.code = str(se), se.code
    except OSError as e:
        res.error, res.code = f"{e.strerror or e}: {getattr(e, 'filename', '') or ''}".strip(": "), "io"
    except Exception as e:  # never let an unexpected error escape without cleanup and a log entry
        import traceback
        res.error, res.code = f"Unexpected error: {e}", "save_failed"
        _append_rotating("error.log", f"{_dt.datetime.now().isoformat()} details {src}\n{traceback.format_exc()}\n")
        log.error("unexpected error saving details of %s", src, exc_info=True)
    finally:
        for f in (tmp, jpath):
            if f and os.path.exists(f):
                try:
                    os.unlink(f)
                except OSError:
                    pass
        if lock is not None:
            lock.release()
        res.elapsed_ms = int((time.time() - t0) * 1000)
        append_log({"time": _dt.datetime.now().isoformat(timespec="seconds"), "ok": res.ok, "source": src,
                    "output": res.out_path, "mode": "details", "format": res.format, "backup": res.backup_path,
                    "marker": {}, "notes": res.notes, "error": res.error, "ms": res.elapsed_ms, "batch": None})
    return res
