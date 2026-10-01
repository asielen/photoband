"""Metadata extraction and face regions (review #3 regressions).

Files are written with the exiftool CLI and read back through
photoband.exiftool (the same -j -struct -G1 -n path the app uses)."""
import datetime as dt
import json
import os
import shutil
import subprocess

import pytest
from PIL import Image

from captiontokens import resolve
from photoband.exiftool import ExifTool
from photoband.imageio import ImageInfo, probe
from photoband.metadata import normalize, parse_regions, region_frame_orientation

T = dt.date(2026, 9, 30)

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


@pytest.fixture(scope="module")
def et():
    e = ExifTool()
    yield e
    e.close()


@pytest.fixture
def mkjpg(tmp_path):
    def make(name, *args, json_payload=None):
        p = str(tmp_path / name)
        Image.new("RGB", (400, 300), "white").save(p)
        if args:
            subprocess.run(["exiftool", "-q", "-overwrite_original", *args, p], check=True)
        if json_payload:
            jp = p + ".json"
            with open(jp, "w") as fh:
                json.dump([{"SourceFile": p, **json_payload}], fh)
            subprocess.run(["exiftool", "-q", "-overwrite_original", "-struct", f"-json={jp}", p], check=True)
            os.unlink(jp)
        return p
    return make


def fields_of(et, path):
    return normalize(et.read_json(path), probe(path))["fields"]


def info(w, h, o=1):
    return ImageInfo("x.jpg", "JPEG", w, h, 3, "uint8", "RGB", orientation=o)


# -- fields ----------------------------------------------------------------------

def test_zero_exif_date_falls_through_to_iptc(et, mkjpg):
    p = mkjpg("zdate.jpg", "-EXIF:DateTimeOriginal#=0000:00:00 00:00:00", "-IPTC:DateCreated=1952:06:14")
    f = fields_of(et, p)
    assert resolve("{date:yyyy-mm-dd}", f, T).text == "1952-06-14"


def test_blank_exif_date_falls_through():
    md = {"ExifIFD:DateTimeOriginal": "    :  :     :  :  ", "IPTC:DateCreated": "1952:06"}
    assert normalize(md, info(10, 10))["fields"]["date"] == "1952:06"


@pytest.mark.parametrize("fuzzy", ["1950s", "circa 1950", "1950-1955", "Summer 1962", "03/04/1962"])
def test_fuzzy_date_is_not_replaced_by_a_lower_source(fuzzy):
    # a scanner's EXIF date is the scan date: it must not stand in for a decade or range
    md = {"XMP-photoshop:DateCreated": fuzzy, "ExifIFD:DateTimeOriginal": "2023:05:01 12:00:00"}
    f = normalize(md, info(10, 10))["fields"]
    assert f["date"] == fuzzy
    # printed as written: an approximate date is never shown as an exact one, nor dropped
    assert resolve("{date}", f, T).text == fuzzy
    assert resolve("{date:mmmm d, yyyy}", f, T).text == fuzzy


@pytest.mark.parametrize("human,iso", [("June 14, 1952", "1952-06-14"), ("14 June 1952", "1952-06-14"),
                                       ("06/14/1952", "1952-06-14"), ("Sept. 1952", "1952-09")])
def test_human_written_date_is_used_as_an_exact_date(human, iso):
    md = {"XMP-photoshop:DateCreated": human, "ExifIFD:DateTimeOriginal": "2023:05:01 12:00:00"}
    f = normalize(md, info(10, 10))["fields"]
    assert f["date"] == human
    assert resolve("{date:iso}", f, T).text == iso


@pytest.mark.parametrize("tag", ["XMP-xmp:CreateDate", "ExifIFD:CreateDate", "XMP-exif:DateTimeDigitized",
                                 "IPTC:DigitalCreationDate"])
def test_scan_date_is_its_own_field_never_the_photo_date(tag):
    md = {tag: "2023:05:01 12:00:00"}
    n = normalize(md, info(10, 10))
    f = n["fields"]
    assert f["date"] is None
    assert f["digitized"] == "2023:05:01 12:00:00"
    assert n["sources"]["digitized"] == tag
    assert "date" not in n["sources"]
    assert resolve("[Taken {date:yyyy}]", f, T).text == ""
    assert resolve("Scanned {digitized:yyyy-mm-dd}", f, T).text == "Scanned 2023-05-01"


def test_photo_date_and_scan_date_side_by_side():
    md = {"ExifIFD:DateTimeOriginal": "1952:06:14 00:00:00", "ExifIFD:CreateDate": "2023:05:01 12:00:00",
          "XMP-xmp:CreateDate": "2023:05:01 12:00:00"}
    f = normalize(md, info(10, 10))["fields"]
    assert resolve("{date:yyyy} (scanned {digitized:yyyy})", f, T).text == "1952 (scanned 2023)"


def test_xmp_exif_original_date_is_a_photo_date():
    md = {"XMP-exif:DateTimeOriginal": "1952-06-14T10:00:00", "XMP-xmp:CreateDate": "2023-05-01"}
    f = normalize(md, info(10, 10))["fields"]
    assert resolve("{date:iso}", f, T).text == "1952-06-14"


def test_scan_date_read_from_a_real_file(et, mkjpg):
    p = mkjpg("scan.jpg", "-ExifIFD:CreateDate=2023:05:01 12:00:00", "-XMP-photoshop:DateCreated=1952")
    f = fields_of(et, p)
    assert resolve("{date} / {digitized:yyyy-mm-dd}", f, T).text == "1952 / 2023-05-01"


def test_title_numeric_string_preserved(et, mkjpg):
    p = mkjpg("numtitle.jpg", "-XMP-dc:Title=1.50")
    assert fields_of(et, p)["title"] == "1.50"


def test_read_json_keeps_number_text(et, mkjpg):
    p = mkjpg("nums.jpg", "-XMP-dc:Title=2.50", "-IPTC:City=1952")
    md = et.read_json(p)
    # numbers stay numbers in the dict itself (save.py and others rely on it) ...
    assert md["XMP-dc:Title"] == 2.5 and md["IPTC:City"] == 1952
    # ... and the verbatim text rides along for text fields
    assert md.text["XMP-dc:Title"] == "2.50"
    f = normalize(md, probe(p))["fields"]
    assert (f["title"], f["city"]) == ("2.50", "1952")


def test_exclude_uses_hierarchical_subject(et, mkjpg):
    p = mkjpg("hier.jpg", "-XMP-dc:Subject=Ann", "-XMP-dc:Subject=Picnic", "-XMP-dc:Subject=Scandinavia",
              "-XMP-lr:HierarchicalSubject=People|Ann")
    f = fields_of(et, p)
    assert f["keyword_paths"] == ["People|Ann"]
    assert resolve("{keywords|exclude=People}", f, T).text == "Picnic, Scandinavia"
    assert resolve("{keywords|exclude=Scan}", f, T).text == "Ann, Picnic, Scandinavia"


def test_single_iptc_keyword_with_comma(et, mkjpg):
    p = mkjpg("kw.jpg", "-IPTC:Keywords=Smith, John")
    assert fields_of(et, p)["keywords"] == ["Smith, John"]


def test_title_non_default_language(et, mkjpg):
    p = mkjpg("lang.jpg", "-XMP-dc:Title-de=Nur Deutsch")
    assert fields_of(et, p)["title"] == "Nur Deutsch"


def test_title_prefers_default_language(et, mkjpg):
    p = mkjpg("lang2.jpg", "-XMP-dc:Title-de=Deutsch", "-XMP-dc:Title=Default")
    assert fields_of(et, p)["title"] == "Default"


def test_iptc_utf8_without_charset_marker(et, mkjpg):
    p = mkjpg("iptcutf.jpg", "-charset", "iptc=utf8", "-IPTC:ObjectName=Café", "-IPTC:Keywords=Zoë")
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-IPTC:CodedCharacterSet=", p], check=True)
    f = fields_of(et, p)
    assert f["title"] == "Café"
    assert f["keywords"] == ["Zoë"]


def test_iptc_latin1_left_alone():
    md = {"IPTC:ObjectName": "Café Ã"}  # real Latin-1 text, not UTF-8 bytes
    assert normalize(md, info(10, 10))["fields"]["title"] == "Café Ã"


def test_cr_line_endings_normalized():
    md = {"IPTC:Caption-Abstract": "Line one\r\nLine two\rLine three"}
    assert normalize(md, info(10, 10))["fields"]["caption"] == "Line one\nLine two\nLine three"


# -- regions ---------------------------------------------------------------------

def test_numeric_region_name_does_not_crash():
    md = {"XMP-mwg-rs:RegionInfo": {"RegionList": [
        {"Area": {"X": .5, "Y": .5, "W": .1, "H": .1, "Unit": "normalized"}, "Type": "Face", "Name": 1952}]}}
    assert parse_regions(md, info(100, 100))["named"][0]["name"] == "1952"


def test_numeric_mp_name_does_not_crash():
    md = {"XMP-MP:RegionInfoMP": {"Regions": [{"PersonDisplayName": 42, "Rectangle": "0.1, 0.1, 0.2, 0.2"}]}}
    assert parse_regions(md, info(100, 100))["named"][0]["name"] == "42"


def test_string_numbers_in_regions():
    md = {"XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": "100", "H": "100", "Unit": "pixel"},
                                    "RegionList": [{"Area": {"X": "0.5", "Y": "0.5", "W": "0.2", "H": "0.2",
                                                             "Unit": "normalized"}, "Type": "Face", "Name": "A"}]}}
    r = parse_regions(md, info(100, 100))
    assert r["named"][0]["box"] == pytest.approx([0.4, 0.4, 0.2, 0.2])
    assert r["warnings"] == []


def test_named_mp_suppresses_overlapping_unnamed_mwg():
    md = {"XMP-mwg-rs:RegionInfo": {"RegionList": [
        {"Area": {"X": .7, "Y": .3, "W": .1, "H": .1, "Unit": "normalized"}, "Type": "Face"}]},
          "XMP-MP:RegionInfoMP": {"Regions": [{"PersonDisplayName": "Ann", "Rectangle": "0.65, 0.25, 0.1, 0.1"}]}}
    r = parse_regions(md, info(400, 300))
    assert r["unnamed_count"] == 0 and [n["name"] for n in r["named"]] == ["Ann"]


def test_named_mwg_suppresses_overlapping_unnamed_mp():
    md = {"XMP-mwg-rs:RegionInfo": {"RegionList": [
        {"Area": {"X": .7, "Y": .3, "W": .1, "H": .1, "Unit": "normalized"}, "Type": "Face", "Name": "Ann"}]},
          "XMP-MP:RegionInfoMP": {"Regions": [{"Rectangle": "0.65, 0.25, 0.1, 0.1"},
                                              {"Rectangle": "0.1, 0.1, 0.1, 0.1"}]}}
    assert parse_regions(md, info(400, 300))["unnamed_count"] == 1


def test_region_without_area_has_no_position():
    md = {"XMP-mwg-rs:RegionInfo": {"RegionList": [{"Type": "Face", "Name": "A"},
                                                   {"Type": "Face", "Name": "B", "Area": {"X": .5, "Unit": "normalized"}}]}}
    r = parse_regions(md, info(100, 100))
    assert [n["box"] for n in r["named"]] == [None, None]
    assert not r["has_positions"]


def test_atd_scaled_upright_orientation6():
    md = {"XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": 400, "H": 500, "Unit": "pixel"},
                                    "RegionList": [{"Area": {"X": .2, "Y": .5, "W": .1, "H": .1, "Unit": "normalized"},
                                                    "Type": "Face", "Name": "A"}]}}
    i = info(1000, 800, 6)
    assert region_frame_orientation(md, i) == 1
    r = parse_regions(md, i)
    b = r["named"][0]["box"]
    assert abs(b[0] - 0.15) < 1e-6 and abs(b[1] - 0.45) < 1e-6
    assert "region dimensions mismatch" in r["warnings"]


def test_atd_stored_frame_orientation6():
    md = {"XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": 1000, "H": 800, "Unit": "pixel"},
                                    "RegionList": [{"Area": {"X": .2, "Y": .5, "W": .1, "H": .1, "Unit": "normalized"},
                                                    "Type": "Face", "Name": "A"}]}}
    i = info(1000, 800, 6)
    assert region_frame_orientation(md, i) == 6
    r = parse_regions(md, i)
    assert r["warnings"] == []
    assert r["named"][0]["box"] == pytest.approx([0.45, 0.15, 0.1, 0.1])


def test_mixed_positions_order():
    md = {"XMP-mwg-rs:RegionInfo": {"RegionList": [
        {"Type": "Face", "Name": "Z"},
        {"Area": {"X": .7, "Y": .3, "W": .1, "H": .1, "Unit": "normalized"}, "Type": "Face", "Name": "B"},
        {"Area": {"X": .2, "Y": .3, "W": .1, "H": .1, "Unit": "normalized"}, "Type": "Face", "Name": "A"}]}}
    f = normalize(md, info(100, 100))["fields"]
    assert resolve("{names}", f, T).text == "A, B and Z"
