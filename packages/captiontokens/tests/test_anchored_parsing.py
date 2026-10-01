"""A value is read only when the WHOLE of it matches: a date followed by anything but one
whole, real time is no exact date, and an option value must be a number all the way through.
(Regression: the time after a date was matched as a prefix, so "1952-06-14 12:34 approximate"
printed as an exact 1952.)"""
import datetime as dt

import pytest

from captiontokens import parse_date, resolve, validate
from captiontokens.dates import render_date

T = dt.date(2026, 9, 30)


@pytest.mark.parametrize("value", [
    "1952-06-14 12:34 approximate", "1952-06-14 99:99 nonsense", "1952:06:14 10:00:00 (scanned)",
    "1952-06-14T10:00:00+02:00 or so", "June 14, 1952 10:30 approx.", "06/14/1952 10:30 ?",
    "19520614T100000 later",
])
def test_time_followed_by_text_is_not_exact(value):
    assert parse_date(value) is None
    assert render_date(value, "yyyy") == value
    assert resolve("{date:yyyy}", {"date": value}, T).text == value


@pytest.mark.parametrize("value", [
    "1952-06-14 24:00", "1952-06-14 12:60", "1952:06:14 10:00:60", "1952-06-14 99:99",
    "1952:06:14 10:00:00+15:00", "1952:06:14 10:00:00-05:60", "19520614T2500", "June 14, 1952 13:30 PM",
    "June 14, 1952 0:30 AM", "03/04/1962 25:00",
])
def test_impossible_time_is_not_exact(value):
    assert parse_date(value) is None
    assert render_date(value, "yyyy") == value


@pytest.mark.parametrize("value", [
    "1952:06:14 12:34", "1952:06:14 12:34:56", "1952:06:14 23:59:59", "1952:06:14 00:00:00",
    "1952:06:14 12:34:56.78", "1952:06:14 12:34:56,5", "1952:06:14 12:34:56Z", "1952:06:14 12:34:56+02:00",
    "1952:06:14 12:34:56.123-05:00", "1952:06:14 12:34:56+0530", "1952:06:14 12:34:56+14:00",
    "1952-06-14T10:00Z", "1952-06-14T10:00:00.000+02:00", "19520614T100000", "19520614T1000",
    "19520614T100000.5+0200", "1952:06:14   :  :  ", "1952:06:14 12:34  ", "June 14, 1952 10:30 AM",
    "June 14, 1952 12:05pm", "14.06.1952 10:30:00 +01:00",
])
def test_exiftool_and_iso_times_still_parse(value):
    assert parse_date(value).iso() == "1952-06-14"


def test_certain_year_needs_whole_time():
    assert render_date("03/04/1962 10:00", "yyyy") == "1962"
    assert render_date("Summer 1962 10:00:00+01:00", "yyyy") == "1962"
    assert render_date("03/04/1962 10:00 or so", "yyyy") == "03/04/1962 10:00 or so"
    assert render_date("Summer 1962 99:99", "yyyy") == "Summer 1962 99:99"


@pytest.mark.parametrize("v", ["²", "5x", "1.5", "-3", "0", "５"])
def test_max_option_must_be_plain_number(v):
    fmt = "{title|max=" + v + "}"
    assert any(i.kind == "bad_option" for i in validate(fmt))
    assert resolve(fmt, {"title": "Hello world"}, T).text == "Hello world"  # never raises, never truncates


def test_max_option_plain_number():
    assert not validate("{title|max=5}")
    assert resolve("{title|max= 5 }", {"title": "Hello world"}, T).text == "Hell…"
