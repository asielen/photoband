"""Partial-date parsing and formatting.

A date is a ``PartialDate`` with a required year and optional month and day.
Formatting drops missing parts together with an adjacent separator, so
``d mmmm yyyy`` on 1952-06 gives ``June 1952`` and ``yyyy-mm-dd`` on 1952
gives ``1952``.

``parse_date`` reads exact dates only: ExifTool's forms, ISO, English month
names ("June 14, 1952", "14 Jun 1952") and numeric dates whose day/month order
is certain ("06/14/1952", "14.06.1952"). A day/month order that could go either
way ("03/04/1962") is never guessed. ``render_date`` prints anything else that
names a year ("1950s", "circa 1950", "Summer 1962", "03/04/1962") as written,
so an approximate date is neither dropped nor shown as an exact one.
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
    r"^\s*(?P<y>\d{4})(?!\d)(?:(?P<sep>[-:/.])(?P<m>\d{1,2})(?!\d)(?:(?P=sep)(?P<d>\d{1,2})(?!\d))?)?"
)
# Compact YYYYMMDD (optionally followed by a time, e.g. 19520614T100000).
_COMPACT_RE = re.compile(r"^\s*(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?!\d)")
# What may follow the date: nothing (or EXIF's blank "  :  :  " parts) or a time. Anything
# else ("1950s", "1950-1955", "1952?") is a decade, range or guess, not this exact date.
_TAIL_RE = re.compile(r"[\s:]*$|\s+\d{1,2}:\d{2}|T\d{1,2}:?\d{2}")


_MONTH_WORDS = {**{n.lower(): i + 1 for i, n in enumerate(MONTHS)},
                **{n[:3].lower(): i + 1 for i, n in enumerate(MONTHS)}, "sept": 9}
_WEEKDAY = r"(?:(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?\.?,?\s+)?"
_ORD = r"(?:st|nd|rd|th)?"
# English month names: "June 14, 1952", "14 June 1952", "14th of June, 1952", "14-Jun-1952", "June 1952"
_HUMAN_RES = [re.compile(p, re.IGNORECASE) for p in (
    rf"^\s*{_WEEKDAY}(?P<mon>[a-z]+)\.?\s+(?P<d>\d{{1,2}}){_ORD},?\s+(?P<y>\d{{4}})(?!\d)",
    rf"^\s*{_WEEKDAY}(?P<d>\d{{1,2}}){_ORD}(?:\s+of)?[\s-]+(?P<mon>[a-z]+)\.?,?[\s-]+(?P<y>\d{{4}})(?!\d)",
    r"^\s*(?P<mon>[a-z]+)\.?,?\s+(?P<y>\d{4})(?!\d)",
)]
# Day and month in either order before a four-digit year: "06/14/1952", "14.06.1952", "06/1952"
_NUMERIC_DMY_RE = re.compile(r"^\s*(?P<a>\d{1,2})(?P<sep>[-/.])(?P<b>\d{1,2})(?P=sep)(?P<y>\d{4})(?!\d)")
_NUMERIC_MY_RE = re.compile(r"^\s*(?P<m>\d{1,2})[-/.](?P<y>\d{4})(?!\d)")


def parse_date(value) -> Optional[PartialDate]:
    """Parse an exact date: ExifTool-style ``1952``, ``1952-06``, ``1952:06:05 12:00:00``,
    ``1952-06-05T10:00:00+02:00``, ``19520605``; English month names (``June 14, 1952``,
    ``14 Jun 1952``, ``June 1952``); and numeric dates whose order is certain
    (``06/14/1952``, ``14.06.1952``, ``05/05/1962``, ``06/1952``). Zero months/days
    (``1952:00:00``) count as missing; a zero or unreadable year, a decade or range
    (``1950s``, ``1950-1955``), an approximate date (``circa 1950``) or a day/month
    order that could go either way (``03/04/1962``) gives None."""
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
        return _parse_human(s)
    if not _TAIL_RE.match(s, m.end()):
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


def _parse_human(s: str) -> Optional[PartialDate]:
    """Dates as people write them, when they can be read only one way; else None."""
    for rx in _HUMAN_RES:
        m = rx.match(s)
        if m and m.group("mon").lower() in _MONTH_WORDS:
            return _exact(s, m, int(m.group("y")), _MONTH_WORDS[m.group("mon").lower()],
                          int(m.group("d")) if "d" in m.groupdict() and m.group("d") else None)
    m = _NUMERIC_DMY_RE.match(s)
    if m:
        a, b = int(m.group("a")), int(m.group("b"))
        if a > 12 >= b:
            mo, d = b, a
        elif b > 12 >= a or a == b:
            mo, d = a, b
        else:
            return None  # 03/04/1962: March 4 or 3 April - never guessed
        return _exact(s, m, int(m.group("y")), mo, d)
    m = _NUMERIC_MY_RE.match(s)
    if m:
        return _exact(s, m, int(m.group("y")), int(m.group("m")), None)
    return None


def _exact(s: str, m, y: int, mo: int, d: Optional[int]) -> Optional[PartialDate]:
    """The date a human-format match names, or None when anything else follows it or it
    does not exist (``June 31, 1952``): a typed date is taken whole or not at all."""
    if not _TAIL_RE.match(s, m.end()) or y <= 0:
        return None
    try:
        _dt.date(y, mo, d or 1)
    except ValueError:
        return None
    return PartialDate(y, mo, d)


# A year that is certain although the date is not exact: a day/month order that could go
# either way ("03/04/1962"), or a season or part of the year ("Summer 1962", "early 1962").
_YEAR_OF_AMBIGUOUS_RE = re.compile(r"^\s*(?P<a>\d{1,2})(?P<sep>[-/.])(?P<b>\d{1,2})(?P=sep)(?P<y>\d{4})\s*$")
_YEAR_OF_PART_RE = re.compile(
    r"^\s*(?:(?:early|mid|late|spring|summer|autumn|fall|winter|" + "|".join(_MONTH_WORDS) +
    r")\.?[\s,-]+)+(?:of\s+)?(?P<y>\d{4})\s*$", re.IGNORECASE)


def _certain_year(s: str) -> Optional[int]:
    m = _YEAR_OF_AMBIGUOUS_RE.match(s)
    if m and 1 <= int(m.group("a")) <= 12 and 1 <= int(m.group("b")) <= 12:
        return int(m.group("y")) or None
    m = _YEAR_OF_PART_RE.match(s)
    return (int(m.group("y")) or None) if m else None


def approximate_text(value) -> str:
    """A date value that is no exact date, as it should be printed: its own text with
    whitespace collapsed, or "" when it names no date at all ("unknown", "0000:00:00")."""
    if value is None or isinstance(value, (PartialDate, _dt.date)) or parse_date(value) is not None:
        return ""
    s = " ".join(str(value).split())
    return s if re.search(r"[1-9]", s) else ""


def render_date(value, fmt: Optional[str] = None) -> str:
    """A date value as a caption prints it. Exact dates use ``fmt``; an approximate one is
    printed as written ("circa 1950" stays "circa 1950"), except that a format of only
    year fields uses the year when that is certain ("Summer 1962" with ``yyyy`` -> 1962)."""
    d = parse_date(value)
    if d is not None:
        return format_date(d, fmt)
    s = approximate_text(value)
    if not s:
        return ""
    y = _certain_year(s)
    if y is not None and fmt and fmt not in ("auto", "iso") and _year_only(fmt):
        return format_date(PartialDate(y), fmt)
    return s


def _year_only(fmt: str) -> bool:
    fields = [v for k, v in _tokenize(fmt) if k == "field"]
    return bool(fields) and all(_FIELD_PART[f] == "y" for f in fields)


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
