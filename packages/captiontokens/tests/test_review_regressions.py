"""Regressions from review #3: token parsing, styling, groups, dates, faces, file names."""
import datetime as dt

import pytest

from captiontokens import (Face, cluster_rows, format_date, parse_date, resolve, resolve_filename,
                           validate)

T = dt.date(2026, 9, 30)


def box(cx, cy, w=0.08, h=0.1):
    return [cx - w / 2, cy - h / 2, w, h]


# -- styling -------------------------------------------------------------------

def test_empty_styled_token_leaves_no_markers():
    r = resolve("*{caption}* - {title}", {"title": "Picnic", "caption": ""}, T)
    assert "**" not in r.text
    assert r.text == "- Picnic"


@pytest.mark.parametrize("fmt,expected", [
    ("**{caption}** {title}", "Picnic"),
    ("***{caption}*** {title}", "Picnic"),
    ("**{caption} {title}**", "**Picnic**"),
    ("**{title}** rest", "**Picnic** rest"),
    ("*{title}*", "*Picnic*"),
    ("**[{caption}]**\n{title}", "Picnic"),
])
def test_style_markers_balance(fmt, expected):
    assert resolve(fmt, {"title": "Picnic"}, T).text == expected


def test_style_toggle_on_removed_line_is_kept_balanced():
    t = resolve("**{title}\n{caption}**", {"title": "Picnic"}, T).text
    assert t == "**Picnic**"


# -- malformed tokens ----------------------------------------------------------

@pytest.mark.parametrize("fmt", ["{title x}", "{ title }", "{{title}}", "{title\n}", "{date :yyyy}",
                                 "{title", "}", "a } b", "{title|case=upper"])
def test_malformed_never_renders_braces(fmt):
    r = resolve(fmt, {"title": "Picnic", "date": "1952"}, T)
    assert "{" not in r.plain and "}" not in r.plain
    assert any(i.kind == "syntax" for i in r.issues)
    assert any(i.kind == "syntax" for i in validate(fmt))


def test_malformed_token_does_not_eat_following_tokens():
    r = resolve("{title x} {caption}", {"title": "Picnic", "caption": "Sunday"}, T)
    assert r.text == "Sunday"


# -- lines and groups ----------------------------------------------------------

def test_no_double_blank_line_for_missing_field():
    f = {"title": "A", "caption": "B"}
    assert resolve("{title}\n\n{names}\n\n{caption}", f, T).text == "A\n\nB"
    assert resolve("{title}\n\n{names}\n{caption}", f, T).text == "A\n\nB"
    # deliberate blank lines stay
    assert resolve("{title}\n\n\n{caption}", f, T).text == "A\n\n\nB"


def test_outer_group_dropped_when_all_nested_tokens_empty():
    r = resolve("[People: [{names}][ and {faces.unnamed_count} unidentified]]",
                {"faces": [], "faces_unnamed_count": 0}, T)
    assert r.empty
    r = resolve("[People: [{names}][ and {faces.unnamed_count} unidentified]]",
                {"faces": [], "faces_unnamed_count": 2}, T)
    assert r.text == "People:  and 2 unidentified"


def test_cr_line_endings_normalized():
    t = resolve("{caption}", {"caption": "Line one\r\nLine two\rLine three"}, T).text
    assert t == "Line one\nLine two\nLine three"


# -- dates ---------------------------------------------------------------------

def test_cjk_date_format():
    assert resolve("{date:yyyy年m月d日}", {"date": "1952:06:14"}, T).text == "1952年6月14日"


@pytest.mark.parametrize("fmt,date,expected", [
    ("yyyy (mmmm)", "1952", "1952"),
    ("(mmmm) yyyy", "1952", "1952"),
    ("d mmmm, yyyy", "1952-06", "June 1952"),
    ("d mmmm, yyyy", "1952", "1952"),
    ("yyyy (d mmmm)", "1952", "1952"),
    ("yyyy (d mmmm)", "1952-06", "1952 (June)"),
    ("mmmm d, yyyy", "1952-06", "June 1952"),
    ("d mmmm, yyyy", "1952-06-14", "14 June, 1952"),
])
def test_partial_date_no_orphans(fmt, date, expected):
    assert resolve("{date:%s}" % fmt, {"date": date}, T).text == expected


def test_date_parsing_anchor_and_compact():
    assert parse_date("12345") is None
    assert parse_date("19520614").iso() == "1952-06-14"
    assert parse_date("    :  :  ") is None
    assert parse_date("0000:00:00 00:00:00") is None
    assert parse_date("1952:06:14 10:00:00").iso() == "1952-06-14"


@pytest.mark.parametrize("value", ["1950s", "1950's", "1950-1955", "1950–55", "1950/1955", "1952-06/07",
                                   "1950 1955", "1952?"])
def test_fuzzy_dates_are_not_read_as_exact(value):
    # a decade or a range must not print as one exact year ("1950s" -> "1950")
    assert parse_date(value) is None


@pytest.mark.parametrize("value,iso", [
    ("1952", "1952"), ("1952-06", "1952-06"), ("1952:06:14 10:00:00", "1952-06-14"),
    ("1952:06:14 10:00:00.25", "1952-06-14"), ("1952-06-14T10:00:00+02:00", "1952-06-14"),
    ("1952:06:14 10:00:00-05:00", "1952-06-14"), ("1952-06-14T10:00Z", "1952-06-14"),
    ("19520614T100000", "1952-06-14"), ("1952:06:00 00:00:00", "1952-06"), ("1952:06:14   :  :  ", "1952-06-14"),
    ("1952:  :  ", "1952"), (" 1952-06-14 ", "1952-06-14"),
])
def test_exiftool_dates_still_parse(value, iso):
    assert parse_date(value).iso() == iso


def test_today_formats_complete():
    from captiontokens import TOKENS
    today = next(t for t in TOKENS if t.name == "today")
    date = next(t for t in TOKENS if t.name == "date")
    assert today.formats == date.formats
    assert resolve("{today:d mmm yy}", {}, T).text == "30 Sep 26"


# -- names and rows ------------------------------------------------------------

def test_tilted_row_stays_one_row():
    faces = [Face(f"T{i}", box(0.1 + i * 0.15, 0.40 + i * 0.04)) for i in range(6)]
    assert len(cluster_rows(faces)) == 1


def test_two_tilted_rows():
    faces = [Face(f"F{i}", box(0.1 + i * 0.2, 0.70 + i * 0.03)) for i in range(4)] + \
            [Face(f"B{i}", box(0.1 + i * 0.2, 0.45 + i * 0.03)) for i in range(4)]
    assert [[f.name for f in r] for r in cluster_rows(faces)] == [["F0", "F1", "F2", "F3"], ["B0", "B1", "B2", "B3"]]


def _rows(n):
    return [{"name": f"R{i}", "box": box(0.5, 0.9 - i * 0.2)} for i in range(n)]


def test_row_label_fallback():
    five = '{names:rows|row_labels="A: / B: / C: / D: / E: "}'
    assert resolve(five, {"faces": _rows(3)}, T).text == "A: R0; B: R1; E: R2"
    assert resolve(five, {"faces": _rows(2)}, T).text == "A: R0; E: R1"
    assert resolve("{names:rows}", {"faces": _rows(4)}, T).text == \
        "Front row, L–R: R0; Row 2, L–R: R1; Row 3, L–R: R2; Back row, L–R: R3"
    assert resolve("{names:rows}", {"faces": _rows(3)}, T).text == \
        "Front row, L–R: R0; Middle row, L–R: R1; Back row, L–R: R2"


def test_mixed_positioned_and_unpositioned_names():
    faces = [{"name": "Z", "box": None}, {"name": "B", "box": box(0.6, 0.5)},
             {"name": "Y", "box": None}, {"name": "A", "box": box(0.2, 0.5)}]
    assert resolve("{names}", {"faces": faces}, T).text == "A, B, Z and Y"
    assert resolve("{names|order=RL}", {"faces": faces}, T).text == "B, A, Z and Y"
    assert resolve("{names|order=meta}", {"faces": faces}, T).text == "Z, B, Y and A"


def test_max_one_is_one_character():
    assert resolve("{title|max=1}", {"title": "Picnic"}, T).text == "P"
    assert resolve("{title|max=3}", {"title": "Picnic"}, T).text == "Pi…"


# -- keywords ------------------------------------------------------------------

def test_exclude_matches_hierarchy_and_word_boundary():
    f = {"keywords": ["Ann", "Picnic", "Scandinavia", "Scan 2024"], "keyword_paths": ["People|Ann"]}
    assert resolve("{keywords|exclude=People}", f, T).text == "Picnic, Scandinavia, Scan 2024"
    assert resolve("{keywords|exclude=Scan}", f, T).text == "Ann, Picnic, Scandinavia"


# -- host field types ----------------------------------------------------------

@pytest.mark.parametrize("fmt,fields", [
    ("{title}", {"title": 1952}), ("{keywords}", {"keywords": [1952, "a"]}), ("{keywords}", {"keywords": "a, b"}),
    ("{city}", {"city": 5}), ("{location}", {"city": 5, "country": ["US"]}),
    ("{names}", {"faces": [{"name": "A", "box": ["0.1", "0.1", "0.1", "0.1"]},
                           {"name": "B", "box": ["0.5", "0.1", "0.1", "0.1"]}]}),
    ("{names}", {"faces": [{"name": 123, "box": None}]}), ("{faces.unnamed_count}", {"faces_unnamed_count": "abc"}),
    ("{names:rows}", {"faces": [{"name": "A", "box": [0.1, 0.1]}, {"name": "B", "box": ["x", 1, 2, 3]}]}),
    ("{names}", {"faces": "Ann"}), ("{names}", {"faces": [None, 5, "Ann"]}), ("{date}", {"date": 1952}),
])
def test_resolver_tolerates_field_types(fmt, fields):
    r = resolve(fmt, fields, T)
    assert isinstance(r.text, str)


def test_numeric_string_boxes_are_ordered():
    f = {"faces": [{"name": "B", "box": ["0.5", "0.1", "0.1", "0.1"]}, {"name": "A", "box": ["0.1", "0.1", "0.1", "0.1"]}]}
    assert resolve("{names}", f, T).text == "A and B"
    assert resolve("{faces.unnamed_count}", {"faces_unnamed_count": "2"}, T).text == "2"
    assert resolve("{title}", {"title": 1952}, T).text == "1952"


# -- file names ----------------------------------------------------------------

@pytest.mark.parametrize("title", ["con.txt", "NUL.tar.gz", "AUX", "com1.old", "CONIN$", "COM¹", "lpt9.x"])
def test_reserved_names_with_extension(title):
    n = resolve_filename("{title}", {"title": title, "stem": "s"}, T)
    assert n.startswith("_")


def test_filename_byte_length():
    n = resolve_filename("{title}", {"title": "é" * 150, "stem": "s"}, T)
    assert len(n.encode("utf-8")) <= 200
    assert n == "é" * 100


def test_filename_truncation_no_trailing_dot():
    n = resolve_filename("{title}", {"title": "x" * 199 + ". y", "stem": "s"}, T)
    assert not n.endswith((".", " "))


def test_filename_strips_bidi_and_controls():
    n = resolve_filename("{title}", {"title": "ab‮cd⁦e\x7ff\x85g", "stem": "s"}, T)
    assert n == "abcdefg"


# -- validation ----------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["{names|seperator=;}", "{keywords|exlude=People}", "{date:dddd}", "{names:row}",
                                 "{title|case=uper}", "{title|max=abc}", "{title:yyyy}", "{names.count|sep=x}",
                                 "{names|order=up}"])
def test_validate_flags_bad_options(fmt):
    issues = validate(fmt)
    assert issues and all(i.kind == "bad_option" for i in issues), fmt
    # still renders
    assert isinstance(resolve(fmt, {"title": "T", "date": "1952"}, T).text, str)


@pytest.mark.parametrize("fmt", ["{names|order=RL}", "{names:rows|row_labels=\"A: / B: \"|row_sep=; }",
                                 "{date:yyyymmdd}", "{date:mmmm d, yyyy}", "{date:iso}", "{title|case=UPPER|max=5}",
                                 "{keywords|sep=, |exclude=\"People,Scan\"}", "{date:yyyy年m月d日}", "{today:auto}"])
def test_validate_accepts_good_options(fmt):
    assert validate(fmt) == [], fmt


def test_order_case_insensitive():
    faces = [{"name": "A", "box": box(0.2, 0.5)}, {"name": "B", "box": box(0.6, 0.5)}]
    assert resolve("{names|order=RL}", {"faces": faces}, T).text == "B and A"
