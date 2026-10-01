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


# -- impossible components are rejected the same way on every parse path -------------

@pytest.mark.parametrize("value", [
    # a second number after the year is a range or a typo, not a month
    "1950-55", "1939-45", "1952/53", "1950-59", "1952-13", "1952:13:01",
    # a day the month does not have is not truncated to the month
    "1952-02-30", "1953-02-29", "1952-6-31", "1952:06:31 10:00:00", "19520230",
    # the human-format path agrees
    "June 31, 1952", "Feb 29, 1953", "13/1952", "31/02/1952",
])
def test_out_of_range_month_or_day_is_no_date(value):
    assert parse_date(value) is None
    assert text("{date}", date=value) == value
    assert text("{date:yyyy}", date=value) == value


@pytest.mark.parametrize("value,iso", [
    ("1952:06:00", "1952-06"), ("1952:00:00", "1952"), ("1952-06-00 00:00:00", "1952-06"),
    ("1952:00:14", "1952"), ("1952-02-29", "1952-02-29"), ("29.02.2000", "2000-02-29"),
])
def test_exiftool_zero_still_means_missing(value, iso):
    assert parse_date(value).iso() == iso


@pytest.mark.parametrize("value", ["June 0, 1952", "0 June 1952", "00-Jun-1952", "00/1952", "0/1952",
                                   "00/00/1952", "13/00/1952"])
def test_a_typed_zero_day_or_month_is_no_date(value):
    # ExifTool writes an unknown part as 0; a person writing it made a typo
    assert parse_date(value) is None
    assert text("{date}", date=value) == value


def test_partial_date_stores_missing_parts_as_none():
    from captiontokens import PartialDate, format_date
    d = PartialDate(1952, 6, 0)
    assert d == PartialDate(1952, 6) and d.day is None
    assert d.iso() == "1952-06"
    assert format_date(d, "d mmmm yyyy") == "June 1952"
    assert format_date(d, "auto") == "June 1952"
    assert PartialDate(1952, 0, 14) == PartialDate(1952)  # a day without its month
    assert format_date(PartialDate(1952, None, 14), "auto") == "1952"
    for bad in [(1952, 13), (1952, 2, 30), (1953, 2, 29), (0,), (1952, -1)]:
        with pytest.raises(ValueError):
            PartialDate(*bad)


@pytest.mark.parametrize("value", ["Monday, June 14, 1952", "Sun 14 June 1952", "Fri June 14th, 1952"])
def test_weekday_that_contradicts_the_date_is_doubtful(value):
    assert parse_date(value) is None
    assert text("{date}", date=value) == value


@pytest.mark.parametrize("value,iso", [("Saturday, June 14, 1952", "1952-06-14"), ("Wed. 18 June 1952", "1952-06-18"),
                                       ("Thurs June 19 1952", "1952-06-19"), ("Tuesday 17th of June, 1952", "1952-06-17")])
def test_weekday_that_matches_the_date_is_fine(value, iso):
    assert parse_date(value).iso() == iso


@pytest.mark.parametrize("value", ["0952", "0952-06-14", "June 14, 0952", "2999", "9999:12:31", "03/04/0962",
                                   "Summer 0962", f"{dt.date.today().year + 2}"])
def test_implausible_year_prints_as_written(value):
    assert parse_date(value) is None
    assert text("{date}", date=value) == value
    assert text("{date:yyyy}", date=value) == value


def test_plausible_year_bounds():
    assert parse_date("1000").iso() == "1000"
    assert parse_date("1839").iso() == "1839"
    assert parse_date(str(dt.date.today().year + 1)) is not None


@pytest.mark.parametrize("value", ["03/04/1962 10:00", "03/04/1962 10:00:00", "3.4.1962T10:00", "Summer 1962 10:00"])
def test_certain_year_ignores_a_trailing_time(value):
    assert text("{date:yyyy}", date=value) == "1962"
    assert text("{date}", date=value) == value


def test_certain_year_needs_the_whole_value():
    assert text("{date:yyyy}", date="03/04/1962 or so") == "03/04/1962 or so"
