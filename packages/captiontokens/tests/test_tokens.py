import datetime as dt
import pytest
from captiontokens import resolve, resolve_filename, validate, parse_date, format_date, markup_to_plain

TODAY = dt.date(2026, 9, 30)

BASE = {
    "title": "Picnic at Lake Merced",
    "caption": "Sunday outing",
    "creator": "Robert Church",
    "date": "1952:06:14 10:00:00",
    "city": "San Francisco", "state": "CA", "country": "USA", "sublocation": "",
    "keywords": ["People|Ann", "Family", "Scan 2024", "Picnic", "family"],
    "filename": "IMG_001.tif", "stem": "IMG_001", "folder": "Scans",
    "faces": [
        {"name": "Carl", "box": [0.6, 0.2, 0.1, 0.15]},
        {"name": "Ann", "box": [0.1, 0.22, 0.1, 0.15]},
        {"name": "Bea", "box": [0.35, 0.21, 0.1, 0.15]},
    ],
    "faces_unnamed_count": 2,
}


def r(fmt, **over):
    f = dict(BASE); f.update(over)
    return resolve(fmt, f, TODAY)


def test_basic_tokens():
    assert r("{title}").text == "Picnic at Lake Merced"
    assert r("{names}").text == "Ann, Bea and Carl"
    assert r("{names|order=rl}").text == "Carl, Bea and Ann"
    assert r("{names|sep=; |last=; }").text == "Ann; Bea; Carl"
    assert r("{names.count} named, {faces.unnamed_count} unidentified").text == "3 named, 2 unidentified"
    assert r("{location}").text == "San Francisco, CA, USA"
    assert r("{title|case=upper}").text == "PICNIC AT LAKE MERCED"
    assert r("{title|max=10}").text == "Picnic at…"


def test_dates_partial():
    assert r("{date:d mmmm yyyy}", date="1952-06").text == "June 1952"
    assert r("{date:d mmmm yyyy}", date="1952").text == "1952"
    assert r("{date:yyyy-mm-dd}", date="1952").text == "1952"
    assert r("{date:mmmm d, yyyy}", date="1952:06:00").text == "June 1952"
    assert r("{date:mmmm d, yyyy}").text == "June 14, 1952"
    assert r("{date}").text == "June 14, 1952"
    assert r("{date:mmm yy}").text == "Jun 52"
    assert r("{today:yyyy-mm-dd}").text == "2026-09-30"
    assert parse_date("0000:00:00") is None
    # a day that does not exist is no date (it prints as written), never truncated to its month
    assert parse_date("1952:02:30") is None
    assert r("{date:d mmmm yyyy}", date="1952:02:30").text == "1952:02:30"


def test_optional_groups():
    fmt = "[Taken {date:mmmm yyyy}][ in {city}]"
    assert r(fmt).text == "Taken June 1952 in San Francisco"
    assert r(fmt, city="").text == "Taken June 1952"
    assert r(fmt, date=None).text == "in San Francisco"
    assert r(fmt, date=None, city=None).empty
    # nested groups: inner empty does not drop outer
    assert r("[A {title}[ ({caption})]]", caption="").text == "A Picnic at Lake Merced"


def test_unknown_tokens_render_nothing():
    res = r("Hi {nope} there")
    assert res.text == "Hi  there"
    assert res.issues and res.issues[0].kind == "unknown_token"
    assert res.issues[0].start == 3 and res.issues[0].end == 9
    assert [i.kind for i in validate("{title} {bogus}")] == ["unknown_token"]
    assert "{" not in r("[x {bogus}]").text


def test_blank_lines_from_empty_fields_removed():
    res = r("{title}\n{names}\n{caption}", faces=[])
    assert res.text == "Picnic at Lake Merced\nSunday outing"
    # deliberate blank lines stay
    assert r("{title}\n\n{caption}").text == "Picnic at Lake Merced\n\nSunday outing"


def test_escapes_and_markup():
    assert r(r"\{title\} \[x\]").text == "{title} [x]"
    assert r(r"**{title}** \*").text == "**Picnic at Lake Merced** \\*"
    assert markup_to_plain(r("**{title}** \\*").text) == "Picnic at Lake Merced *"
    # metadata stars are escaped, not formatting
    assert r("{title}", title="5 * stars").text == "5 \\* stars"


def test_keywords():
    assert r("{keywords}").text == "People|Ann, Family, Scan 2024, Picnic, family"
    assert r('{keywords|exclude="People,Scan"}').text == "Family, Picnic, family"
    assert r('{keywords|sep= \\/ |exclude=people}').text == "Family / Scan 2024 / Picnic / family"


def test_rows():
    faces = [
        {"name": "F1", "box": [0.1, 0.7, 0.1, 0.12]},
        {"name": "F2", "box": [0.4, 0.72, 0.1, 0.12]},
        {"name": "B1", "box": [0.15, 0.3, 0.1, 0.12]},
        {"name": "B2", "box": [0.45, 0.28, 0.1, 0.12]},
        {"name": "M1", "box": [0.3, 0.5, 0.1, 0.12]},
    ]
    t = r("{names:rows}", faces=faces).text
    assert t == "Front row, L–R: F1 and F2; Middle row, L–R: M1; Back row, L–R: B1 and B2"
    two = [f for f in faces if not f["name"].startswith("M")]
    assert r("{names:rows}", faces=two).text == "Front row, L–R: F1 and F2; Back row, L–R: B1 and B2"
    one = faces[:2]
    assert r("{names:rows}", faces=one).text == "F1 and F2"
    custom = r('{names:rows|row_labels="Front: / Back: "|row_sep=\\n}', faces=two).text
    assert custom == "Front: F1 and F2\nBack: B1 and B2"
    # no positions → plain names in metadata order
    nopos = [{"name": "X", "box": None}, {"name": "Y", "box": None}]
    assert r("{names:rows}", faces=nopos).text == "X and Y"
    # literal slash inside a label
    lab = r('{names:rows|row_labels="Front\\/row: / Back: "}', faces=two).text
    assert lab.startswith("Front/row: F1")


def test_filename():
    assert resolve_filename("{stem}-captioned", BASE, TODAY) == "IMG_001-captioned"
    assert resolve_filename("{title}: {template}", dict(BASE, template="A/B"), TODAY) == "Picnic at Lake Merced_ A_B"
    assert resolve_filename("{nope}", BASE, TODAY) == "IMG_001"


def test_syntax_errors_do_not_crash():
    for bad in ["{title", "[{title}", "}", "]", "{", "{|}", "{:}", "\\"]:
        res = r(bad)
        assert isinstance(res.text, str)


def test_keyword_prefix_boundary_keeps_combining_marks_in_the_word():
    from captiontokens.tokens import _prefix_match
    assert not _prefix_match("cafe\u0301 visit", "cafe")      # "café" (decomposed) is another word
    assert _prefix_match("cafe\u0301 visit", "cafe\u0301")
    assert not _prefix_match("हिन्दी", "हिन")                 # a vowel sign continues the word
    assert _prefix_match("scan 2024", "scan") and not _prefix_match("scandinavia", "scan")
