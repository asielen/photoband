"""The photoband XMP record: what this app needs to re-open its own output exactly.

When a file's metadata is stripped, the record is gone but the hidden band marker
(photoband/marker.py) may remain: on PNG/TIFF with its payload (save.payload_for_marker),
on JPEG and after any lossy step without. Every decision made from a record field must
therefore either read the same value from the payload, or be explicitly conservative
when the field is missing (never treat such a file as safe to replace, reuse or caption
again as an ordinary photo). Per field:

==================  ===============================  ====================================================
field               decisions that read it           without the record
==================  ===============================  ====================================================
saveMode            existing.provenance: isCopy       payload carries it. No payload (JPEG): copyUnknown,
                    (a batch never captions copies)   and the batch leaves the file alone as a maybe-copy.
template, overrides editor state of case A            payload carries them (blocks: printed text only).
blocks, templateId
mode (erase)        editor: re-edit in place          payload layout.mode; existing returns ``record``
                    (ex.record.mode) vs rebuild       built from the payload, so the editor decides alike.
photoHash, canvas,  case A match, is_app_output       payload carries them (case A via marker+payload).
photoOffset,        (re-save from the backup,         save() reads only the record: no record = not a
originalSize        backup reuse)                     verified output, so the photo is taken from the file
                                                      and a fresh backup of the file as it is is made.
outputPhotoHash     case A after Orientation edits    not carried; marker rect + hash used instead.
sourceKey           save.is_output_of ("overwrite"    not carried (a path hash is metadata). No record =
                    replaces an earlier copy)         never an earlier copy: the copy gets a new name.
originalFile,       re-save from the original         not carried. No record = no verified original: the
originalRect        backup, plan_backup reuse,        file itself is the pixel source and is backed up as
                    save_preview pixelSource          it is; save_preview says "current", never
                                                      "the untouched original" (captioned from the
                                                      analysis's marker when the record is gone).
originalText        kept in the next save's record    not carried, by design (never metadata in the
                                                      band); a re-save of a stripped file loses it.
batchJob            batch restore: "our output"       not carried; no record = not restored (treated
                                                      as edited since), the safe side.
lossyRecaption      (informational)                   set from the file format on both paths.
==================  ===============================  ====================================================
"""
from __future__ import annotations

import base64
import json
import zlib
from typing import Any, Dict, Optional

RECORD_TAG = "XMP-photoband:Record"
VERSION_TAG = "XMP-photoband:Version"
PREFIX = "pb1:"
MAX_DECOMPRESSED = 8 * 1024 * 1024   # a record is a few KB; anything near this is hostile
MAX_ENCODED = 4 * 1024 * 1024


class DecompressionLimit(ValueError):
    pass


def safe_decompress(data: bytes, max_out: int = MAX_DECOMPRESSED) -> bytes:
    """zlib.decompress with an output cap, so a small 'zip bomb' in a file's metadata cannot
    exhaust memory. Raises DecompressionLimit if the output would exceed ``max_out`` and
    zlib.error for corrupt or truncated data."""
    d = zlib.decompressobj()
    out = d.decompress(data, max_out)
    if d.unconsumed_tail or len(out) > max_out:
        raise DecompressionLimit("Compressed data expands beyond the allowed size")
    out += d.flush(max_out + 1 - len(out)) if hasattr(d, "flush") else b""
    if len(out) > max_out:
        raise DecompressionLimit("Compressed data expands beyond the allowed size")
    if not d.eof:
        raise zlib.error("Incomplete or truncated compressed data")
    return out


def encode(record: Dict[str, Any]) -> str:
    raw = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return PREFIX + base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def decode(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if not value or not isinstance(value, str) or not value.startswith(PREFIX):
        return None
    if len(value) > MAX_ENCODED:
        return None
    try:
        rec = json.loads(safe_decompress(base64.b64decode(value[len(PREFIX):])).decode("utf-8"))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def from_metadata(md: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for k, v in md.items():
        if k.endswith("photoband:Record") or k == RECORD_TAG:
            return decode(v)
    return None
