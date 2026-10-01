"""Format-string parser and resolver.

Syntax
------
``{token}``                 insert a value
``{token:format}``          format it (dates, names:rows)
``{token|opt=val|opt=val}`` options; values may be "quoted"; ``\\|`` ``\\/`` ``\\"`` escape
``[ ... ]``                 optional group, dropped when any token directly inside is
                            empty, or when it holds tokens (at any depth) and none of
                            them produced text
newline                     line break
``**bold**`` ``*italic*``   inline styling: ``**`` toggles bold, ``*`` toggles italic
``\\{ \\[ \\] \\} \\* \\\\``    literal characters

A malformed token (``{title x}``, ``{ title }``, ``{title`` without ``}``) or a
stray ``}`` is reported as a "syntax" issue and renders as nothing; captions
never show raw braces.

Output markup
-------------
The resolved text keeps ``**``/``*`` as style markers. Literal asterisks and
backslashes (from ``\\*`` or from metadata values) are escaped as ``\\*`` and
``\\\\`` so the renderer can tell them apart from style markers. Use
:func:`markup_to_plain` to strip markup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union


@dataclass
class Literal:
    text: str  # already in output markup form
    start: int = 0
    end: int = 0


@dataclass
class Token:
    name: str
    fmt: Optional[str]
    options: Dict[str, str]
    start: int
    end: int
    raw: str = ""


@dataclass
class Group:
    items: List["Node"]
    start: int
    end: int


@dataclass
class Style:
    """A ``**`` (bold) or ``*`` (italic) toggle in the format string."""
    kind: str  # "b" | "i"
    start: int = 0
    end: int = 0


Node = Union[Literal, Token, Group, Style]


@dataclass
class Issue:
    kind: str  # "unknown_token" | "syntax" | "bad_option"
    start: int
    end: int
    message: str


@dataclass
class ParseResult:
    nodes: List[Node]
    issues: List[Issue] = field(default_factory=list)


_LITERAL_ESCAPES = {"{": "{", "}": "}", "[": "[", "]": "]", "*": "\\*", "\\": "\\\\", "|": "|", "/": "/"}


def escape_value(s: str) -> str:
    """Escape a plain value for inclusion in output markup."""
    return s.replace("\\", "\\\\").replace("*", "\\*")


def markup_to_plain(s: str) -> str:
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
            continue
        if ch == "*":
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class _Parser:
    def __init__(self, src: str):
        self.s = src
        self.i = 0
        self.issues: List[Issue] = []

    def parse(self) -> List[Node]:
        nodes = self._items(top=True)
        return nodes

    def _items(self, top: bool) -> List[Node]:
        nodes: List[Node] = []
        buf: List[str] = []
        buf_start = self.i

        def flush():
            nonlocal buf, buf_start
            if buf:
                nodes.append(Literal("".join(buf), buf_start, self.i))
            buf = []
            buf_start = self.i

        s = self.s
        while self.i < len(s):
            ch = s[self.i]
            if ch == "\\" and self.i + 1 < len(s):
                nxt = s[self.i + 1]
                buf.append(_LITERAL_ESCAPES.get(nxt, "\\\\" + nxt if nxt != "n" else "\n"))
                self.i += 2
                continue
            if ch == "{":
                flush()
                nodes.append(self._token())
                buf_start = self.i
                continue
            if ch == "*":
                flush()
                start = self.i
                kind = "b" if s.startswith("**", self.i) else "i"
                self.i += 2 if kind == "b" else 1
                nodes.append(Style(kind, start, self.i))
                buf_start = self.i
                continue
            if ch == "[":
                flush()
                start = self.i
                self.i += 1
                inner = self._items(top=False)
                if self.i < len(s) and s[self.i] == "]":
                    self.i += 1
                    nodes.append(Group(inner, start, self.i))
                else:
                    self.issues.append(Issue("syntax", start, start + 1, "Unclosed [ optional group"))
                    nodes.append(Group(inner, start, self.i))
                buf_start = self.i
                continue
            if ch == "]":
                if not top:
                    flush()
                    return nodes
                self.issues.append(Issue("syntax", self.i, self.i + 1, "Unmatched ]; write \\] for a literal bracket"))
                buf.append("]")
                self.i += 1
                continue
            if ch == "}":
                flush()
                self.issues.append(Issue("syntax", self.i, self.i + 1, "Unmatched }; write \\} for a literal brace"))
                self.i += 1
                buf_start = self.i
                continue
            buf.append(ch)
            self.i += 1
        flush()
        return nodes

    def _token(self) -> Token:
        """Parse ``{name[:fmt][|opt=val...]}``. A malformed token is reported
        and skipped (up to its ``}`` on the same line); it yields a Token with
        an empty name, which renders as an empty token."""
        s = self.s
        start = self.i
        self.i += 1  # past {
        j = self.i
        while j < len(s) and (s[j].isalnum() or s[j] in "_."):
            j += 1
        name = s[self.i:j]
        self.i = j
        fmt: Optional[str] = None
        options: Dict[str, str] = {}
        bad: Optional[str] = None
        if not name:
            bad = "Empty token name" if self.i < len(s) and s[self.i] in ":|}" else "Invalid token"
        if self.i < len(s) and s[self.i] == ":":
            self.i += 1
            fmt = self._read_until("|}")
        while self.i < len(s) and s[self.i] == "|":
            self.i += 1
            opt_start = self.i
            key = self._read_until("=|}").strip()
            if self.i < len(s) and s[self.i] == "=":
                self.i += 1
                val = self._read_value()
            else:
                val = "true"
            if not key:
                self.issues.append(Issue("bad_option", opt_start, self.i, "Empty option"))
                continue
            options[key] = val
        if self.i < len(s) and s[self.i] == "}" and not bad:
            self.i += 1
            return Token(name, fmt, options, start, self.i, s[start:self.i])
        if bad is None:
            bad = "Unclosed { token" if self.i >= len(s) or s[self.i] == "\n" else "Invalid token"
        # Skip the rest of the malformed token: up to and including the next
        # "}" on this line, stopping before a new "{" or a line break.
        k = self.i
        while k < len(s) and s[k] not in "{}\n":
            k += 2 if s[k] == "\\" else 1
        k = min(k, len(s))
        if k < len(s) and s[k] == "}":
            k += 1
        self.i = k
        msg = bad
        if bad == "Invalid token":
            msg = "Invalid token; write {name}, {name:format} or {name|option=value}, or \\{ for a literal brace"
        self.issues.append(Issue("syntax", start, max(self.i, start + 1), msg))
        # an invalid token renders as an empty token (name "")
        return Token("", None, {}, start, self.i, s[start:self.i])

    def _read_until(self, stops: str) -> str:
        s = self.s
        out = []
        while self.i < len(s) and s[self.i] not in stops and s[self.i] != "\n":
            if s[self.i] == "\\" and self.i + 1 < len(s):
                out.append(s[self.i + 1])
                self.i += 2
                continue
            out.append(s[self.i])
            self.i += 1
        return "".join(out)

    def _read_value(self) -> str:
        s = self.s
        if self.i < len(s) and s[self.i] == '"':
            self.i += 1
            out = []
            while self.i < len(s) and s[self.i] != '"':
                if s[self.i] == "\\" and self.i + 1 < len(s):
                    nxt = s[self.i + 1]
                    # keep "\/" escaped so list splitting can honour it
                    out.append("\\/" if nxt == "/" else ("\n" if nxt == "n" else nxt))
                    self.i += 2
                    continue
                out.append(s[self.i])
                self.i += 1
            if self.i < len(s):
                self.i += 1
            return "".join(out)
        out = []
        while self.i < len(s) and s[self.i] not in "|}" and s[self.i] != "\n":
            if s[self.i] == "\\" and self.i + 1 < len(s):
                nxt = s[self.i + 1]
                out.append("\\/" if nxt == "/" else ("\n" if nxt == "n" else nxt))
                self.i += 2
                continue
            out.append(s[self.i])
            self.i += 1
        return "".join(out)


def parse(src: str) -> ParseResult:
    p = _Parser(src or "")
    nodes = p.parse()
    return ParseResult(nodes, p.issues)


def split_list(value: str, sep: str = "/") -> List[str]:
    """Split an option value on ``sep`` honouring ``\\/`` escapes."""
    parts, buf, i = [], [], 0
    while i < len(value):
        if value.startswith("\\" + sep, i):
            buf.append(sep)
            i += 2
            continue
        if value[i] == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(value[i])
        i += 1
    parts.append("".join(buf))
    return parts


def unescape_value(value: str) -> str:
    return value.replace("\\/", "/")
