"""The photoband XMP record: what this app needs to re-open its own output exactly."""
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
