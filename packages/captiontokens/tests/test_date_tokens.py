"""The photo's date vs. the scan date, human-written dates and approximate dates."""
import datetime as dt

import pytest

from captiontokens import TOKENS, parse_date, resolve, validate

T = dt.date(2026, 9, 30)


def text(fmt, **fields):
    return resolve(fmt, fields, T).text


# -- {digitized}: the scan / digitization date, its own token -----------------------

def test_digitized_token_is_listed_with_the_date_formats():
    toks = {t.name: t for t in TOKENS}
    assert "digitized" in toks
    assert toks["digitized"].formats == toks["date"].formats
    assert validate("{digitized:yyyy-mm-dd}") == []
    assert validate("{digitized|case=upper}") == []
    assert [i.kind for i in validate("{digitized:qq}")] == ["bad_option"]


def test_digitized_resolves_its_own_field():
    f = {"date": "1952:06:14", "digitized": "2023:05:01 12:00:00"}
    assert text("{date:yyyy}", **f) == "1952"
    assert text("{digitized:yyyy-mm-dd}", **f) == "2023-05-01"
    assert text("{digitized}", **f) == "May 1, 2023"


def test_date_never_borrows_the_scan_date():
    assert text("{date}", digitized="2023:05:01 12:00:00") == ""
    assert text("[Taken {date:yyyy}]", digitized="2023:05:01") == ""


# -- human-written dates that are unambiguous ---------------------------------------

@pytest.mark.parametrize("value,iso", [
    ("June 14, 1952", "1952-06-14"), ("june 14 1952", "1952-06-14"), ("Jun 14, 1952", "1952-06-14"),
    ("Jun. 14, 1952", "1952-06-14"), ("June 14th, 1952", "1952-06-14"),
    ("Saturday, June 14, 1952", "1952-06-14"), ("Sat June 14 1952", "1952-06-14"),
    ("14 June 1952", "1952-06-14"), ("14th June 1952", "1952-06-14"), ("14th of June, 1952", "1952-06-14"),
    ("14 Jun 1952", "1952-06-14"), ("14-Jun-1952", "1952-06-14"), ("14 Sept. 1952", "1952-09-14"),
    ("June 1952", "1952-06"), ("June, 1952", "1952-06"), ("Sep 1952", "1952-09"),
    ("06/14/1952", "1952-06-14"), ("6/14/1952", "1952-06-14"), ("14/06/1952", "1952-06-14"),
    ("14.06.1952", "1952-06-14"), ("14-6-1952", "1952-06-14"), ("05/05/1962", "1962-05-05"),
    ("06/1952", "1952-06"), ("6.1952", "1952-06"),
    ("1952/06/14", "1952-06-14"), ("1952.6.14", "1952-06-14"),
    ("June 14, 1952 10:30", "1952-06-14"), ("06/14/1952 10:30:00", "1952-06-14"),
])
def test_unambiguous_human_dates_parse(value, iso):
    assert parse_date(value).iso() == iso


@pytest.mark.parametrize("value", ["03/04/1962", "3.4.1962", "12-11-1962"])
def test_ambiguous_numeric_dates_are_not_guessed(value):
    # day and month could be either way round: no exact date
    assert parse_date(value) is None


@pytest.mark.parametrize("value", ["6/14/52", "14/6/52", "June 14, 52", "13/14/1952", "June 1952-1955",
                                   "June/July 1952", "Mid June 1952", "Juneteenth 1952"])
def test_other_human_shapes_are_not_exact(value):
    assert parse_date(value) is None


def test_human_date_formats_like_any_other():
    assert text("{date:yyyy-mm-dd}", date="June 14, 1952") == "1952-06-14"
    assert text("{date}", date="14 June 1952") == "June 14, 1952"
    assert text("{date:d mmmm yyyy}", date="June 1952") == "June 1952"


# -- approximate dates print as written ---------------------------------------------

@pytest.mark.parametrize("value", ["1950s", "1950's", "circa 1950", "c. 1950", "ca. 1950", "about 1950",
                                   "1950-1955", "1950–55", "1952?", "Summer 1962", "Christmas 1962",
                                   "03/04/1962", "6/14/52", "between 1950 and 1955"])
def test_approximate_date_prints_its_own_text(value):
    assert text("{date}", date=value) == value
    assert text("{date:mmmm d, yyyy}", date=value) == value
    assert text("[Taken {date:mmmm yyyy}]", date=value) == f"Taken {value}"


def test_approximate_text_keeps_options_and_markup_safe():
    assert text("{date|case=upper}", date="circa 1950") == "CIRCA 1950"
    assert resolve("{date}", {"date": "*c.* 1950 [?]"}, T).plain == "*c.* 1950 [?]"
    assert text("{date}", date="  circa   1950 \n") == "circa 1950"


@pytest.mark.parametrize("value,year", [("03/04/1962", "1962"), ("Summer 1962", "1962"), ("early 1962", "1962"),
                                        ("Late 1962", "1962"), ("Winter, 1962", "1962"), ("3.4.1962", "1962")])
def test_year_only_format_uses_a_year_that_is_certain(value, year):
    # the day/month or season is unknown or unexpressible, but the year itself is not in doubt
    assert text("{date:yyyy}", date=value) == year
    assert text("{date:'Year' yy}", date=value) == "Year " + year[2:]


@pytest.mark.parametrize("value", ["1950s", "circa 1962", "1962?", "1960-1962", "before 1962", "6/14/52",
                                   "about 1962"])
def test_year_only_format_keeps_text_when_the_year_is_uncertain(value):
    assert text("{date:yyyy}", date=value) == value


@pytest.mark.parametrize("value", ["unknown", "n/a", "0000:00:00 00:00:00", "    :  :  ", "", None, "?"])
def test_values_without_a_date_stay_empty(value):
    r = resolve("[Taken {date}]", {"date": value}, T)
    assert r.text == ""
    assert "date" in r.empty_tokens


def test_approximate_digitized_date_also_prints_as_written():
    assert text("{digitized:yyyy-mm-dd}", digitized="2023?") == "2023?"
