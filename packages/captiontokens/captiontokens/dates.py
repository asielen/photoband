"""Partial-date parsing and formatting.

A date is a ``PartialDate`` with a required year and optional month and day.
Formatting drops missing parts together with an adjacent separator, so
``d mmmm yyyy`` on 1952-06 gives ``June 1952`` and ``yyyy-mm-dd`` on 1952
gives ``1952``.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from typing import Optional

MONTHS = [
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]

# Longest first so "mmmm" wins over "mm".
_FIELDS = ["yyyy", "mmmm", "mmm", "yy", "mm", "dd", "m", "d"]
_FIELD_PART = {"yyyy": "y", "yy": "y", "mmmm": "m", "mmm": "m", "mm": "m", "m": "m", "dd": "d", "d": "d"}


@dataclass(frozen=True)
class PartialDate:
    year: int
    month: Optional[int] = None
    day: Optional[int] = None

    def iso(self) -> str:
        s = f"{self.year:04d}"
        if self.month:
            s += f"-{self.month:02d}"
            if self.day:
                s += f"-{self.day:02d}"
        return s

    @classmethod
    def from_date(cls, d: _dt.date) -> "PartialDate":
        return cls(d.year, d.month, d.day)


_DATE_RE = re.compile(
    r"^\s*(?P<y>\d{4})(?!\d)(?:[-:/.](?P<m>\d{1,2})(?!\d)(?:[-:/.](?P<d>\d{1,2})(?!\d))?)?"
)
# Compact YYYYMMDD (optionally followed by a time, e.g. 19520614T100000).
_COMPACT_RE = re.compile(r"^\s*(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?!\d)")


def parse_date(value) -> Optional[PartialDate]:
    """Parse ExifTool-style dates: ``1952``, ``1952-06``, ``1952:06:05 12:00:00``,
    ``1952-06-05T10:00:00+02:00``, ``19520605``. Zero months/days (``1952:00:00``)
    count as missing; a zero or unreadable year gives None."""
    if value is None:
        return None
    if isinstance(value, PartialDate):
        return value
    if isinstance(value, (_dt.date, _dt.datetime)):
        return PartialDate(value.year, value.month, value.day)
    if isinstance(value, (int, float)):
        value = str(int(value))
    s = str(value)
    m = _COMPACT_RE.match(s) or _DATE_RE.match(s)
    if not m:
        return None
    y = int(m.group("y"))
    if y <= 0:
        return None
    mo = int(m.group("m")) if m.group("m") else None
    d = int(m.group("d")) if m.group("d") else None
    if not mo or not (1 <= mo <= 12):
        return PartialDate(y)
    if not d or not (1 <= d <= 31):
        return PartialDate(y, mo)
    try:
        _dt.date(y, mo, d)
    except ValueError:
        return PartialDate(y, mo)
    return PartialDate(y, mo, d)


def _tokenize(fmt: str):
    """Split a date format into ('field', name) and ('sep', text) items.
    Text in single quotes is always literal."""
    items = []
    i = 0
    buf = ""
    while i < len(fmt):
        ch = fmt[i]
        if ch == "'":
            j = fmt.find("'", i + 1)
            if j == -1:
                j = len(fmt)
            buf += fmt[i + 1:j]
            i = j + 1
            continue
        for f in _FIELDS:
            if fmt.startswith(f, i):
                # Avoid matching a letter inside an ordinary word, e.g. "d" in "and".
                prev = fmt[i - 1] if i > 0 else ""
                nxt = fmt[i + len(f)] if i + len(f) < len(fmt) else ""
                # Only ASCII letters count as word characters, so "yyyy年m月d日" works.
                if (_is_word(prev) and prev not in "ymd") or (_is_word(nxt) and nxt not in "ymd"):
                    continue
                if buf:
                    items.append(("sep", buf))
                    buf = ""
                items.append(("field", f))
                i += len(f)
                break
        else:
            buf += ch
            i += 1
    if buf:
        items.append(("sep", buf))
    return items


def _is_word(ch: str) -> bool:
    return bool(ch) and ch.isascii() and ch.isalpha()


def check_format(fmt: Optional[str]) -> Optional[str]:
    """Problem with a date format, or None when it is fine. A run of ASCII
    letters made only of y/m/d must split into known fields using each of
    year, month and day at most once ("dddd" and "yyy" are errors)."""
    if not fmt or fmt in ("auto", "iso"):
        return None
    i, runs, cur = 0, [], ""
    while i < len(fmt):
        ch = fmt[i]
        if ch == "'":
            j = fmt.find("'", i + 1)
            i = len(fmt) if j == -1 else j + 1
            if cur:
                runs.append(cur)
                cur = ""
            continue
        if _is_word(ch):
            cur += ch
        elif cur:
            runs.append(cur)
            cur = ""
        i += 1
    if cur:
        runs.append(cur)
    found = False
    for run in runs:
        if set(run) - set("ymd"):
            continue  # an ordinary word, rendered literally
        parts, k = set(), 0
        while k < len(run):
            f = next((f for f in _FIELDS if run.startswith(f, k)), None)
            if f is None or _FIELD_PART[f] in parts:
                return f"Unknown date format \"{run}\" (use yyyy yy mmmm mmm mm m dd d)"
            parts.add(_FIELD_PART[f])
            k += len(f)
        found = True
    if not found:
        return "Date format has no yyyy, mmmm, mm, dd or similar field"
    return None


def _field_value(f: str, d: PartialDate) -> Optional[str]:
    if f == "yyyy":
        return f"{d.year:04d}"
    if f == "yy":
        return f"{d.year % 100:02d}"
    if d.month is None:
        return None
    if f == "mmmm":
        return MONTHS[d.month - 1]
    if f == "mmm":
        return MONTHS[d.month - 1][:3]
    if f == "mm":
        return f"{d.month:02d}"
    if f == "m":
        return str(d.month)
    if d.day is None:
        return None
    if f == "dd":
        return f"{d.day:02d}"
    if f == "d":
        return str(d.day)
    return None


def auto_format(d: PartialDate) -> str:
    if d.day:
        return "mmmm d, yyyy"
    if d.month:
        return "mmmm yyyy"
    return "yyyy"


def format_date(d: Optional[PartialDate], fmt: Optional[str] = None) -> str:
    if d is None:
        return ""
    if not fmt or fmt == "auto":
        fmt = auto_format(d)
    if fmt == "iso":
        return d.iso()
    items = _tokenize(fmt)
    # Resolve fields; mark missing ones.
    resolved = []
    for kind, val in items:
        if kind == "field":
            resolved.append(["field", val, _field_value(val, d)])
        else:
            resolved.append(["sep", val, val])
    # Drop each missing field together with the separator that follows it,
    # or the one before it when it is the last field.
    dropped_day = False
    changed = True
    while changed:
        changed = False
        for idx, it in enumerate(resolved):
            if it[0] == "field" and it[2] is None:
                if _FIELD_PART[it[1]] == "d":
                    dropped_day = True
                del resolved[idx]
                if idx < len(resolved) and resolved[idx][0] == "sep" and _has_field_after(resolved, idx):
                    del resolved[idx]
                elif idx - 1 >= 0 and resolved[idx - 1][0] == "sep" and _has_field_before(resolved, idx - 1):
                    del resolved[idx - 1]
                elif idx < len(resolved) and resolved[idx][0] == "sep":
                    del resolved[idx]
                changed = True
                break
    if dropped_day:
        # "d mmmm, yyyy" on 1952-06: the comma only separates a day from the year.
        for idx, it in enumerate(resolved):
            if it[0] == "sep" and "," in it[2] and 0 < idx < len(resolved) - 1:
                a, b = resolved[idx - 1], resolved[idx + 1]
                if a[0] == b[0] == "field" and {_FIELD_PART[a[1]], _FIELD_PART[b[1]]} == {"m", "y"}:
                    it[2] = it[2].replace(",", "") or " "
    out = "".join(it[2] for it in resolved)
    if len(resolved) != len(items):
        out = _squeeze(_balance_brackets(out))
    return out.strip()


def _squeeze(s: str) -> str:
    while "  " in s:
        s = s.replace("  ", " ")
    return s


_PAIRS = {")": "(", "]": "["}


def _balance_brackets(s: str) -> str:
    """Remove brackets left unmatched or empty after dropping date parts:
    "1952)" -> "1952", "(1952" -> "1952", "1952 ()" -> "1952"."""
    chars = list(s)
    stack = []
    for i, ch in enumerate(chars):
        if ch in "([":
            stack.append(i)
        elif ch in ")]":
            if stack and chars[stack[-1]] == _PAIRS[ch]:
                j = stack.pop()
                if not "".join(c for c in chars[j + 1:i] if c).strip():
                    for k in range(j, i + 1):
                        chars[k] = ""
            else:
                chars[i] = ""
    for j in stack:
        chars[j] = ""
    return "".join(chars)


def _has_field_after(items, idx):
    return any(it[0] == "field" for it in items[idx + 1:])


def _has_field_before(items, idx):
    return any(it[0] == "field" for it in items[:idx])
