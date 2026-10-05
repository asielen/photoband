"""What photokin writes into a photo, as Photoband's captions read it: its analysis notes
(UserComment), its marker keywords ("DATE: Y!M~", "... Analyzed", back/negative) and the
date certainty those DATE: keywords record."""
import datetime as dt
import shutil
import subprocess

import pytest
from PIL import Image

from captiontokens import resolve, validate
from captiontokens.dates import PartialDate, apply_certainty, certainty_from_keywords
from captiontokens.tokens import is_marker_keyword
from photoband.imageio import ImageInfo, probe
from photoband.metadata import normalize

T = dt.date(2026, 9, 30)


def info():
    return ImageInfo("x.tif", "TIFF", 10, 10, 3, "uint8", "RGB", orientation=1)


def fields(md):
    return normalize(md, info())["fields"]


def text(fmt, md):
    return resolve(fmt, fields(md), T).text


# -- {notes} ------------------------------------------------------------------------

@pytest.mark.parametrize("tag", ["ExifIFD:UserComment", "XMP-exif:UserComment", "XMP-photoshop:Instructions",
                                 "IPTC:SpecialInstructions"])
def test_notes_come_from_user_comment_or_instructions(tag):
    n = normalize({tag: "Taken at the lake"}, info())
    assert n["fields"]["notes"] == "Taken at the lake"
    assert n["sources"]["notes"] == tag
    assert resolve("{notes}", n["fields"], T).text == "Taken at the lake"


def test_user_comment_wins_over_instructions():
    md = {"IPTC:SpecialInstructions": "Do not crop", "ExifIFD:UserComment": "[AI Analysis]: Two boys on a dock"}
    assert text("{notes}", md) == "[AI Analysis]: Two boys on a dock"


def test_blank_notes_leave_the_line_out():
    assert text("[Notes: {notes}]", {"ExifIFD:UserComment": "   "}) == ""
    assert validate("{notes|max=40}") == []


# -- marker keywords ----------------------------------------------------------------

@pytest.mark.parametrize("kw", ["DATE: Y~", "date: Y!M!D!", "DATE:Y!", "Claude claude-opus-5-5 Analyzed",
                                "Gemini Analyzed", "back", "Negative"])
def test_photokin_markers_are_markers(kw):
    assert is_marker_keyword(kw)


@pytest.mark.parametrize("kw", ["Backyard", "Dating", "Psychoanalyzed photos", "negatives box 3", "family"])
def test_ordinary_keywords_are_not_markers(kw):
    assert not is_marker_keyword(kw)


def test_keywords_leave_markers_out_unless_asked():
    md = {"XMP-dc:Subject": ["family", "DATE: Y!M~", "Claude claude-opus-5-5 Analyzed", "picnic", "back"]}
    assert text("{keywords}", md) == "family, picnic"
    assert text("{keywords|markers=show}", md) == "family, DATE: Y!M~, Claude claude-opus-5-5 Analyzed, picnic, back"
    assert text("[Tags: {keywords}]", {"XMP-dc:Subject": ["DATE: Y~", "back"]}) == ""
    assert [i.kind for i in validate("{keywords|markers=maybe}")] == ["bad_option"]


# -- date certainty -----------------------------------------------------------------

@pytest.mark.parametrize("pattern, expect", [
    ("Y!M!D!", (PartialDate(1942, 11, 25), False)),
    ("Y!M!", (PartialDate(1942, 11), False)),        # day not rated: photokin's filler
    ("Y!M~", (PartialDate(1942), False)),            # guessed month left out
    ("Y!M!D~", (PartialDate(1942, 11), False)),
    ("Y!", (PartialDate(1942), False)),
    ("Y~", (PartialDate(1942), True)),               # guessed year: "c. 1942"
    ("Y@M!", (PartialDate(1942), True)),             # "@" is an older "~"
    ("Y?M!D!", (None, False)),                       # unknown year: no date
    ("y!m!", (PartialDate(1942, 11), False)),
    ("banana", (PartialDate(1942, 11, 25), False)),  # not a pattern: the date as stored
    (None, (PartialDate(1942, 11, 25), False)),
])
def test_apply_certainty(pattern, expect):
    assert apply_certainty(PartialDate(1942, 11, 25), pattern) == expect


def test_certainty_keyword_is_read_strictly():
    assert certainty_from_keywords(["family", "date: y!m~"]) == "Y!M~"
    assert certainty_from_keywords(["DATE: Y!", "DATE: Y~"]) == "Y!"   # the first one
    assert certainty_from_keywords(["DATE: soon", "DATE:", "Y!M!"]) is None
    assert certainty_from_keywords("DATE: Y~") == "Y~"
    assert certainty_from_keywords(None) is None


def _photokin(date, pattern, **more):
    return {"ExifIFD:DateTimeOriginal": date, "XMP-dc:Subject": ["family", f"DATE: {pattern}"], **more}


def test_decade_guess_prints_as_circa_year():
    md = _photokin("1925:06:15 00:00:00", "Y~")
    assert text("{date}", md) == "c. 1925"
    assert text("{date:mmmm d, yyyy}", md) == "c. 1925"
    assert text("{date:yyyy}", md) == "c. 1925"
    assert text("{date|circa=about }", md) == "about 1925"
    assert text("{date|circa=}", md) == "1925"
    assert text("{date|certainty=ignore}", md) == "June 15, 1925"


def test_sure_year_guessed_month_prints_the_year():
    md = _photokin("1960:05:15 00:00:00", "Y!M~")
    assert text("{date:mmmm d, yyyy}", md) == "1960"
    assert text("Taken {date:d mmmm yyyy}", md) == "Taken 1960"


def test_sure_month_drops_only_the_filler_day():
    assert text("{date:mmmm d, yyyy}", _photokin("1960:05:15", "Y!M!")) == "May 1960"
    assert text("{date:mmmm d, yyyy}", _photokin("1942:11:25", "Y!M!D!")) == "November 25, 1942"


def test_unknown_year_prints_no_date():
    assert text("[Taken {date}]", _photokin("1950:06:15", "Y?M!D!")) == ""


def test_certainty_only_rates_date_time_original():
    # a person's own XMP DateCreated that differs is not what photokin's keyword rated
    md = _photokin("1925:06:15 00:00:00", "Y~", **{"XMP-photoshop:DateCreated": "1931-07-04"})
    f = fields(md)
    assert f["date_certainty"] is None
    assert resolve("{date}", f, T).text == "July 4, 1931"
    # Lightroom's DateCreated kept in step with DateTimeOriginal: same date, so the keyword applies
    md = _photokin("1925:06:15 00:00:00", "Y~", **{"XMP-photoshop:DateCreated": "1925-06-15T00:00:00"})
    assert text("{date}", md) == "c. 1925"


def test_a_date_with_a_clock_time_is_not_the_guess_the_keyword_rates():
    # photokin's model tags every photo "DATE: <its guess's pattern>", but writes the guess
    # (at midnight) only when it replaces the date: a camera's own date keeps its time and stays exact
    md = _photokin("2017:04:05 17:01:07", "Y~", **{"XMP-photoshop:DateCreated": "2017:04:05 17:01:07"})
    assert fields(md)["date_certainty"] is None
    assert text("{date}", md) == "April 5, 2017"
    assert text("{date}", _photokin("1925:06:15 00:00", "Y~")) == "c. 1925"
    assert text("{date}", _photokin("1925-06-15T00:00:00Z", "Y~")) == "c. 1925"


def test_certainty_never_touches_the_scan_date_or_a_written_out_date():
    md = _photokin("1960:05:15", "Y!M~", **{"ExifIFD:CreateDate": "2023:05:01 12:00:00"})
    assert text("{digitized}", md) == "May 1, 2023"
    assert text("{date}", _photokin("1950s", "Y~")) == "1950s"


def test_no_keyword_no_change():
    assert text("{date}", {"ExifIFD:DateTimeOriginal": "1960:05:15 00:00:00"}) == "May 15, 1960"


def test_date_options_are_validated():
    assert validate("{date|circa=ca. |certainty=ignore}") == []
    assert [i.kind for i in validate("{date|certainty=maybe}")] == ["bad_option"]
    assert [i.kind for i in validate("{digitized|circa=x}")] == ["bad_option"]


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")
def test_what_photokin_writes_reads_back_from_a_real_file(tmp_path):
    from photoband.exiftool import ExifTool
    p = str(tmp_path / "scan.tif")
    Image.new("RGB", (40, 30), "white").save(p)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-EXIF:DateTimeOriginal=1925:06:15 00:00:00",
                    "-EXIF:UserComment=[AI Analysis]: Two boys on a dock", "-XMP-dc:Subject=family",
                    "-XMP-dc:Subject=DATE: Y~", "-XMP-dc:Subject=Claude claude-opus-5-5 Analyzed", p], check=True)
    et = ExifTool()
    try:
        f = normalize(et.read_json(p), probe(p))["fields"]
    finally:
        et.close()
    assert resolve("{date} | {keywords} | {notes}", f, T).text == "c. 1925 | family | [AI Analysis]: Two boys on a dock"
