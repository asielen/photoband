"""Token registry and resolver.

``fields`` is a plain dict produced by the host app (Photoband or photokin)::

    {
      "title": str | None, "caption": str | None, "creator": str | None,
      "notes": str | None,                         # UserComment / Instructions
      "date": "1952-06" | PartialDate | None,      # when the photo was taken, never the scan date
      "date_certainty": "Y!M~" | None,             # what of "date" is known (dates.apply_certainty)
      "digitized": "2023:05:01 12:00:00" | None,   # when it was scanned / the file was made
      "sublocation": str, "city": str, "state": str, "country": str,
      "keywords": [str, ...],
      "keyword_paths": ["People|Ann", ...],   # optional, Lightroom hierarchy
      "filename": str, "stem": str, "folder": str,
      "faces": [{"name": str, "box": [x, y, w, h] | None}, ...],
      "faces_unnamed_count": int,
      "face_rows": int | None,                     # this photo's row count for {names:rows} (None: auto)
      "template": str,
    }

Values of unexpected types (numbers, numeric strings, short boxes) are
coerced defensively; an invalid box counts as "no position".
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from .dates import PartialDate, check_format, format_date, render_date
from .faces import Face, cluster_rows, order_names
from .parser import (Group, Issue, Literal, Style, Token, escape_value, markup_to_plain,
                     parse, split_list, unescape_value)

DEFAULT_ROW_LABELS = "Front row, L–R: / Middle row, L–R: / Back row, L–R: "
TEXT_OPTIONS = {"case", "max"}


@dataclass
class TokenInfo:
    name: str
    description: str
    example: str
    options: List[str] = field(default_factory=list)
    formats: List[str] = field(default_factory=list)


TOKENS: List[TokenInfo] = [
    TokenInfo("title", "Photo title (XMP dc:Title, IPTC ObjectName, Headline, XPTitle)", "{title}", ["case", "max"]),
    TokenInfo("caption", "Photo description (XMP dc:Description, IPTC Caption, ImageDescription)", "{caption}", ["case", "max"]),
    TokenInfo("notes", "Notes about the photo (EXIF UserComment, IPTC/XMP Instructions); at most 200 characters "
              "unless max= says otherwise (max=0: no limit)", "{notes}", ["case", "max"]),
    TokenInfo("creator", "Photographer or creator", "{creator}", ["case", "max"]),
    TokenInfo("date", "When the photo was taken (XMP DateCreated, EXIF DateTimeOriginal, IPTC DateCreated; never "
              "the scan date); partial dates drop missing parts, approximate ones (\"circa 1950\") print as written. "
              "A \"DATE: Y!M~\" keyword (photokin) leaves out guessed parts and puts circa= (\"c. \") before a "
              "guessed year; certainty=ignore prints the date as stored",
              "{date:mmmm d, yyyy}", ["case", "circa", "certainty"],
              ["auto", "yyyy", "yy", "mmmm", "mmm", "mm", "m", "dd", "d", "iso"]),
    TokenInfo("digitized", "When the photo was scanned or the file was made (EXIF/XMP CreateDate, DateTimeDigitized)",
              "{digitized:yyyy-mm-dd}", ["case"], ["auto", "yyyy", "yy", "mmmm", "mmm", "mm", "m", "dd", "d", "iso"]),
    TokenInfo("today", "Today's date", "{today:yyyy-mm-dd}", ["case"],
              ["auto", "yyyy", "yy", "mmmm", "mmm", "mm", "m", "dd", "d", "iso"]),
    TokenInfo("names", "Names from face regions, left to right", "{names}", ["sep", "last", "order", "case", "max"],
              ["rows"]),
    TokenInfo("names.count", "Number of named people", "{names.count}"),
    TokenInfo("faces.unnamed_count", "Number of face regions without a name", "{faces.unnamed_count}"),
    TokenInfo("location", "Sublocation, city, state, country joined with \", \"", "{location}", ["case", "max"]),
    TokenInfo("city", "City", "{city}", ["case", "max"]),
    TokenInfo("state", "State or province", "{state}", ["case", "max"]),
    TokenInfo("country", "Country", "{country}", ["case", "max"]),
    TokenInfo("keywords", "Keywords, merged and deduplicated; photokin's markers (\"DATE: Y~\", \"... Analyzed\", "
              "back, negative) are left out unless markers=show", "{keywords|sep=, |exclude=\"People,Scan\"}",
              ["sep", "exclude", "markers", "case", "max"]),
    TokenInfo("filename", "File name with extension", "{filename}"),
    TokenInfo("stem", "File name without extension", "{stem}"),
    TokenInfo("folder", "Name of the containing folder", "{folder}"),
    TokenInfo("template", "Active template name (file names only)", "{template}"),
]
TOKEN_NAMES = {t.name for t in TOKENS}
_TOKEN_INFO = {t.name: t for t in TOKENS}
# Options accepted beyond those listed for the autocomplete.
_EXTRA_OPTIONS = {"names": ["row_labels", "row_sep"]}
_OPTION_VALUES = {"case": ("upper", "lower", "title"), "order": ("lr", "rl", "meta"),
                  "markers": ("hide", "show"), "certainty": ("keyword", "ignore")}
DATE_TOKENS = ("date", "digitized", "today")


@dataclass
class Resolution:
    text: str                      # output markup (see parser docs)
    plain: str                     # markup stripped
    issues: List[Issue]
    empty: bool                    # nothing visible: the block takes no space
    tokens_used: List[str]
    empty_tokens: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "plain": self.plain,
            "empty": self.empty,
            "tokens_used": self.tokens_used,
            "empty_tokens": self.empty_tokens,
            "issues": [i.__dict__ for i in self.issues],
        }


# -- defensive coercion of host-provided values -----------------------------

def _s(v) -> str:
    """Any field value as text; None -> ""; line endings normalized to \\n."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = ", ".join(t for t in (_s(x).strip() for x in v) if t)
    elif isinstance(v, dict):
        v = v.get("x-default") or next((x for x in v.values() if x), "")
        v = _s(v)
    elif not isinstance(v, str):
        v = str(v)
    return v.replace("\r\n", "\n").replace("\r", "\n")


def _str_list(v) -> List[str]:
    if v is None:
        return []
    if not isinstance(v, (list, tuple)):
        v = [v]
    return [t for t in (_s(x).strip() for x in v) if t]


def _box(b) -> Optional[Tuple[float, float, float, float]]:
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        return None
    try:
        x, y, w, h = (float(p) for p in b)
    except (TypeError, ValueError):
        return None
    if any(v != v or v in (float("inf"), float("-inf")) for v in (x, y, w, h)) or w < 0 or h < 0:
        return None
    return (x, y, w, h)


def _int(v) -> int:
    try:
        n = int(float(_s(v).strip() or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, n)


# {notes} holds free text (photokin writes a paragraph of analysis there): cut unless asked
DEFAULT_MAX = {"notes": 200}


def _max_chars(v) -> Optional[int]:
    """The ``max=N`` option's N: a whole number in ASCII digits, or None (0: no limit). The check
    and the renderer both use this, so they agree; ``isdigit`` let "²" through to ``int``, which raised."""
    m = re.fullmatch(r"\s*([0-9]+)\s*", v) if isinstance(v, str) else None
    return int(m.group(1)) if m else None


def _apply_text_options(value: str, opts: Dict[str, str]) -> str:
    case = opts.get("case", "").lower()
    if case == "upper":
        value = value.upper()
    elif case == "lower":
        value = value.lower()
    elif case == "title":
        value = " ".join(w[:1].upper() + w[1:] for w in value.split(" "))
    n = _max_chars(opts.get("max"))
    if n and len(value) > n:
        if n == 1:
            return value[:1]
        head = value[: n - 1]
        # end on a word boundary when one is near (not "Two boys on a do…")
        if value[n - 1].strip() and _word_char(value[n - 1]) and _word_char(head[-1]):
            sp = max(head.rfind(" "), head.rfind("\n"))
            if sp >= 0.6 * (n - 1):
                head = head[:sp]
        value = head.rstrip() + "…"
    return value


def join_names(names: List[str], sep: str = ", ", last: str = " and ") -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return sep.join(names[:-1]) + last + names[-1]


def _faces(fields) -> List[Face]:
    raw = fields.get("faces")
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for f in raw:
        if isinstance(f, Face):
            name, box = f.name, f.box
        elif isinstance(f, dict):
            name, box = f.get("name"), f.get("box")
        elif isinstance(f, str):
            name, box = f, None
        else:
            continue
        name = _s(name).strip()
        if name:
            out.append(Face(name, _box(box), getattr(f, "source", "") if isinstance(f, Face) else ""))
    return out


def _face_rows(v) -> Optional[int]:
    """A photo's own row count for {names:rows}: a whole number 1..9, else None (automatic)."""
    try:
        n = int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None
    return n if n is not None and 1 <= n <= 9 else None


def face_row_groups(fields: Dict[str, Any]) -> List[List[int]]:
    """How {names:rows} groups this photo's named faces, front row first, each row left to right,
    as indexes into ``fields["faces"]`` (for the UI's People list). Faces without a position are
    left out; a single row is still one group."""
    raw = fields.get("faces") if isinstance(fields, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    pos: List[Face] = []
    index: Dict[int, int] = {}
    for i, r in enumerate(raw):
        one = _faces({"faces": [r]})
        if one and one[0].box is not None:
            index[id(one[0])] = i
            pos.append(one[0])
    if not pos:
        return []
    return [[index[id(f)] for f in row] for row in cluster_rows(pos, rows=_face_rows(fields.get("face_rows")))]


def _row_label(labels: List[str], i: int, nr: int) -> str:
    """Front row gets the first label, the back row the last, middle rows the
    labels in between in order; ordinal labels when there are too few."""
    if i == 0:
        return labels[0]
    if i == nr - 1 and len(labels) > 1:
        return labels[-1]
    middle = labels[1:-1]
    if len(middle) >= nr - 2:
        return middle[i - 1]
    return f"Row {i + 1}, L–R: "


def _prefix_match(text: str, prefix: str) -> bool:
    """``prefix`` matches ``text`` at its start and ends on a word boundary:
    "scan" matches "Scan 2024" and "Scan|Negatives" but not "Scandinavia"."""
    t = text.lower()
    if not t.startswith(prefix):
        return False
    return len(t) == len(prefix) or not _word_char(t[len(prefix)]) or not _word_char(prefix[-1])


def _word_char(ch: str) -> bool:
    """Part of a word in any script: letters, digits and combining marks (``str.isalnum`` is
    False for marks, so "cafe" + U+0301 looked like "cafe" followed by a boundary)."""
    return unicodedata.category(ch)[0] in "LNM"


def is_marker_keyword(kw: str) -> bool:
    """A keyword photokin adds as a processing marker, not a description of the photo: its
    date-certainty "DATE: Y!M~", its provenance "<Provider> <Model> Analyzed", and the part
    markers "back" and "negative" (which side or form of the object a scan shows)."""
    k = kw.strip().lower()
    return k.startswith("date:") or k.endswith(" analyzed") or k in ("back", "negative")


def _keyword_excluded(kw: str, paths: List[str], prefixes: List[str]) -> bool:
    cands = [kw] + [p for p in paths if p.split("|")[-1].strip().lower() == kw.lower()]
    return any(_prefix_match(c, e) for c in cands for e in prefixes)


def check_token(tok: Token) -> List[Issue]:
    """bad_option issues for a known token: unknown format or option, bad value."""
    info = _TOKEN_INFO.get(tok.name)
    if info is None:
        return []
    out: List[Issue] = []

    def bad(msg):
        out.append(Issue("bad_option", tok.start, tok.end, msg))

    fmt = tok.fmt
    if fmt:
        if tok.name in DATE_TOKENS:
            msg = check_format(fmt)
            if msg:
                bad(msg)
        elif not info.formats:
            bad(f"{{{tok.name}}} takes no format (\"{fmt}\")")
        elif fmt.lower() not in info.formats:
            bad(f"Unknown format \"{fmt}\" for {{{tok.name}}} (use {', '.join(info.formats)})")
    allowed = list(info.options) + _EXTRA_OPTIONS.get(tok.name, [])
    for k, v in tok.options.items():
        if k not in allowed:
            if allowed:
                bad(f"Unknown option \"{k}\" for {{{tok.name}}} (use {', '.join(allowed)})")
            else:
                bad(f"{{{tok.name}}} takes no options (\"{k}\")")
        elif k in _OPTION_VALUES and v.lower() not in _OPTION_VALUES[k]:
            bad(f"Unknown {k}=\"{v}\" (use {', '.join(_OPTION_VALUES[k])})")
        elif k == "max" and _max_chars(v) is None:
            bad(f"max must be a whole number (\"{v}\"; 0 for no limit)")
    return out


# -- resolver ----------------------------------------------------------------

_EMPTY = "\x00"   # a token or group that produced nothing
_BOLD = "\x01"    # ** toggle from the format string
_ITAL = "\x02"    # *  toggle from the format string
_SENTINELS = _EMPTY + _BOLD + _ITAL


class Resolver:
    def __init__(self, fields: Dict[str, Any], today: Optional[_dt.date] = None):
        self.f = fields if isinstance(fields, dict) else {}
        self.today = today or _dt.date.today()

    # -- individual tokens -------------------------------------------------
    def value(self, tok: Token) -> Optional[str]:
        """Plain (unescaped) value, or None if unknown. Empty string = empty."""
        v = self._value(tok)
        return None if v is None else v.replace("\r\n", "\n").replace("\r", "\n")

    def _value(self, tok: Token) -> Optional[str]:
        n, fmt, o = tok.name, tok.fmt, tok.options
        f = self.f
        if n in ("title", "caption", "notes", "creator", "city", "state", "country"):
            if n in DEFAULT_MAX and "max" not in o:
                o = {**o, "max": str(DEFAULT_MAX[n])}
            return _apply_text_options(_s(f.get(n)).strip(), o)
        if n == "date":
            cert = None if o.get("certainty", "").lower() == "ignore" else _s(f.get("date_certainty")).strip()
            return _apply_text_options(render_date(f.get(n), fmt, cert or None, unescape_value(o.get("circa", "c. "))), o)
        if n == "digitized":
            return _apply_text_options(render_date(f.get(n), fmt), o)
        if n == "today":
            return _apply_text_options(format_date(PartialDate.from_date(self.today), fmt), o)
        if n == "names":
            faces = _faces(f)
            sep = unescape_value(o.get("sep", ", "))
            last = unescape_value(o.get("last", " and "))
            order = o.get("order", "lr").lower()
            if (fmt or "").lower() == "rows" and faces and all(x.box is not None for x in faces):
                rows = cluster_rows(faces, rows=_face_rows(f.get("face_rows")))
                if len(rows) > 1:
                    labels = [unescape_value(x).lstrip() for x in split_list(o.get("row_labels", DEFAULT_ROW_LABELS))]
                    labels = [lb for lb in labels if lb.strip()] or ["Row 1: "]
                    row_sep = unescape_value(o.get("row_sep", "; "))
                    parts = []
                    for i, row in enumerate(rows):
                        seq = [x.name for x in (row if order != "rl" else list(reversed(row)))]
                        parts.append(_row_label(labels, i, len(rows)) + join_names(seq, sep, last))
                    return _apply_text_options(row_sep.join(parts), o)
            return _apply_text_options(join_names(order_names(faces, order), sep, last), o)
        if n == "names.count":
            c = len(_faces(f))
            return str(c) if c else ""
        if n == "faces.unnamed_count":
            c = _int(f.get("faces_unnamed_count"))
            return str(c) if c else ""
        if n == "location":
            parts = [_s(f.get(k)).strip() for k in ("sublocation", "city", "state", "country")]
            return _apply_text_options(", ".join(p for p in parts if p), o)
        if n == "keywords":
            kws = _str_list(f.get("keywords"))
            if o.get("markers", "").lower() != "show":
                kws = [k for k in kws if not is_marker_keyword(k)]
            excl = [e.strip().lower() for e in split_list(o.get("exclude", ""), ",") if e.strip()]
            if excl:
                paths = _str_list(f.get("keyword_paths"))
                kws = [k for k in kws if not _keyword_excluded(k, paths, excl)]
            return _apply_text_options(unescape_value(o.get("sep", ", ")).join(kws), o)
        if n in ("filename", "stem", "folder", "template"):
            return _apply_text_options(_s(f.get(n)), o)
        return None

    # -- tree walk ---------------------------------------------------------
    def _render(self, nodes, used, empties, issues) -> Tuple[str, bool, int, int]:
        """Returns (markup, any_direct_token_empty, tokens_at_any_depth,
        tokens_rendered_at_any_depth)."""
        out = []
        any_empty = False
        total = rendered = 0
        for node in nodes:
            if isinstance(node, Literal):
                out.append(node.text)
            elif isinstance(node, Style):
                out.append(_BOLD if node.kind == "b" else _ITAL)
            elif isinstance(node, Token) and not node.name:
                # malformed token, already reported by the parser: renders empty
                total += 1
                any_empty = True
                out.append(_EMPTY)
            elif isinstance(node, Token):
                total += 1
                v = self.value(node)
                used.append(node.name + (":" + node.fmt if node.fmt else ""))
                if v is None:
                    issues.append(Issue("unknown_token", node.start, node.end, f"Unknown token {{{node.name}}}"))
                    any_empty = True
                    out.append(_EMPTY)
                    continue
                issues.extend(check_token(node))
                if v == "":
                    empties.append(node.name)
                    any_empty = True
                    out.append(_EMPTY)
                    continue
                rendered += 1
                out.append(escape_value(v))
            elif isinstance(node, Group):
                inner, inner_empty, n, r = self._render(node.items, used, empties, issues)
                total += n
                if inner_empty or (n and not r):
                    out.append(_EMPTY)
                else:
                    rendered += r
                    out.append(inner)
        return "".join(out), any_empty, total, rendered

    def resolve(self, fmt: str) -> Resolution:
        pr = parse(fmt)
        issues = list(pr.issues)
        used: List[str] = []
        empties: List[str] = []
        text, _, _, _ = self._render(pr.nodes, used, empties, issues)
        text = _finish(_tidy(_drop_empty_styles(text)))
        plain = markup_to_plain(text)
        return Resolution(text, plain, issues, plain.strip() == "", used, sorted(set(empties)))


_RUN_RE = re.compile("[ \t" + _SENTINELS + "]+")
_MARKS_RE = re.compile("[" + _BOLD + _ITAL + "]+")


def _cancel(marks: str) -> str:
    """Net effect of adjacent toggles: bold if an odd number of **, italic if odd *."""
    return (_BOLD if marks.count(_BOLD) % 2 else "") + (_ITAL if marks.count(_ITAL) % 2 else "")


def _drop_empty_styles(text: str) -> str:
    """Style toggles around something that rendered empty cancel out, so
    ``*{caption}* - {title}`` with no caption gives ``- title``, never ``**``."""
    def fix(m):
        run = m.group(0)
        if _EMPTY not in run or (_BOLD not in run and _ITAL not in run):
            return run
        out, seen = [], {_BOLD: 0, _ITAL: 0}
        total = {_BOLD: run.count(_BOLD), _ITAL: run.count(_ITAL)}
        for ch in run:
            if ch in seen:
                seen[ch] += 1
                # keep only the last toggle of a kind with an odd count
                if total[ch] % 2 and seen[ch] == total[ch]:
                    out.append(ch)
            else:
                out.append(ch)
        return "".join(out)
    return _RUN_RE.sub(fix, text)


def _visible(ln: str) -> bool:
    return markup_to_plain("".join(c for c in ln if c not in _SENTINELS)).strip() != ""


def _marks(ln: str) -> str:
    return "".join(c for c in ln if c in (_BOLD, _ITAL))


def _tidy(text: str) -> str:
    """Remove lines left blank by empty fields (they contain an empty marker
    and nothing visible), trailing spaces, and blank lines at either end.
    Deliberate blank lines in the format string are kept, but removing a line
    never leaves two blank lines where the format had one. Style toggles on
    removed lines move to the next kept line so bold/italic stay balanced."""
    lines: List[str] = []
    pending = ""
    dropped_since_last = False
    for ln in text.split("\n"):
        had_empty = _EMPTY in ln
        m = re.match("[ \t" + _SENTINELS + "]*", ln)
        lead = m.group(0)
        if _EMPTY in lead:
            ln = _marks(lead) + ln[len(lead):]
        m = re.search("[ \t" + _SENTINELS + "]*$", ln)
        tail = m.group(0)
        ln = ln[: len(ln) - len(tail)] + _marks(tail)
        ln = ln.replace(_EMPTY, "")
        if not _visible(ln):
            if had_empty:
                pending += _marks(ln)
                dropped_since_last = True
                continue
            if lines and not _visible(lines[-1]) and dropped_since_last:
                pending += _marks(ln)
                continue  # would double a blank line the format wrote once
        lines.append(pending + ln)
        pending = ""
        dropped_since_last = False
    head = ""
    while lines and not _visible(lines[0]):
        head += _marks(lines.pop(0))
    if head and lines:
        lines[0] = head + lines[0]
    while lines and not _visible(lines[-1]):
        pending = _marks(lines.pop()) + pending
    if pending and lines:
        lines[-1] += pending
    return "\n".join(lines)


def _finish(text: str) -> str:
    """Collapse adjacent toggles by parity and write them as ``**`` / ``*``."""
    text = _MARKS_RE.sub(lambda m: _cancel(m.group(0)), text)
    return text.replace(_EMPTY, "").replace(_BOLD, "**").replace(_ITAL, "*")


def resolve(fmt: str, fields: Dict[str, Any], today: Optional[_dt.date] = None) -> Resolution:
    return Resolver(fields, today).resolve(fmt)


_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$",
             *(f"{p}{i}" for p in ("COM", "LPT") for i in (*range(0, 10), "¹", "²", "³"))}
_FILENAME_BYTES = 200
_STRIP_CHARS = set(chr(c) for c in (*range(0x7F, 0xA0), 0x200E, 0x200F, *range(0x202A, 0x202F),
                                    *range(0x2066, 0x206A), 0xFEFF))


def resolve_filename(pattern: str, fields: Dict[str, Any], today: Optional[_dt.date] = None) -> str:
    """Resolve a file-name pattern; strips markup and characters unsafe on
    Windows or macOS, bidi and control characters; avoids Windows reserved
    names (also with an extension, e.g. ``con.txt``); at most 200 UTF-8 bytes."""
    r = resolve(pattern, fields, today)
    name = r.plain.replace("\n", " ")
    bad = '<>:"/\\|?*' + "".join(chr(c) for c in range(32))
    name = "".join("_" if ch in bad else ch for ch in name if ch not in _STRIP_CHARS).strip(" .")
    if len(name.encode("utf-8")) > _FILENAME_BYTES:
        cut = name.encode("utf-8")[:_FILENAME_BYTES].decode("utf-8", "ignore")
        name = cut.strip(" .")
    base = name.split(".")[0].rstrip(" ").upper()
    if base in _RESERVED:
        name = "_" + name
    if not name:
        name = _s(fields.get("stem") if isinstance(fields, dict) else "") or "photo"
    return name


def validate(fmt: str) -> List[Issue]:
    """Issues for the settings editor: syntax problems, unknown tokens and
    bad formats or options (kind "bad_option")."""
    pr = parse(fmt)
    issues = list(pr.issues)

    def walk(nodes):
        for n in nodes:
            if isinstance(n, Token) and n.name and n.name not in TOKEN_NAMES:
                issues.append(Issue("unknown_token", n.start, n.end, f"Unknown token {{{n.name}}}"))
            elif isinstance(n, Token):
                issues.extend(check_token(n))
            elif isinstance(n, Group):
                walk(n.items)
    walk(pr.nodes)
    return issues
