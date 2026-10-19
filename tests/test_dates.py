"""Date parser: every format from the EDA plus the ambiguity rule (ADR-011)."""

from datetime import date

import pytest

from yipit_pipeline.cleaning.dates import (
    FMT_ISO_DATE,
    FMT_ISO_TIMESTAMP,
    FMT_MONTH_NAME,
    FMT_NUMERIC_DMY,
    FMT_NUMERIC_MDY,
    STATUS_INVALID,
    STATUS_MISSING,
    STATUS_OK,
    parse_date,
)


@pytest.mark.parametrize(
    ("raw", "expected", "fmt", "ambiguous"),
    [
        ("2023-02-07", date(2023, 2, 7), FMT_ISO_DATE, False),
        ("2023/02/07", date(2023, 2, 7), FMT_ISO_DATE, False),
        ("2023-05-22T00:00:00Z", date(2023, 5, 22), FMT_ISO_TIMESTAMP, False),
        ("2023-05-22T23:30:00-03:00", date(2023, 5, 23), FMT_ISO_TIMESTAMP, False),  # -> UTC
        ("2023-05-22 08:15", date(2023, 5, 22), FMT_ISO_TIMESTAMP, False),
        ("August 15, 2023", date(2023, 8, 15), FMT_MONTH_NAME, False),
        ("Aug 15 2023", date(2023, 8, 15), FMT_MONTH_NAME, False),
        ("Sept 3, 2022", date(2022, 9, 3), FMT_MONTH_NAME, False),
        ("21 Feb 2020", date(2020, 2, 21), FMT_MONTH_NAME, False),
        ("21 February 2020", date(2020, 2, 21), FMT_MONTH_NAME, False),
        ("1st March 2021", date(2021, 3, 1), FMT_MONTH_NAME, False),
        # Unambiguous numeric: the field > 12 is the day, whatever the separator.
        ("08/30/2020", date(2020, 8, 30), FMT_NUMERIC_MDY, False),
        ("24-04-2020", date(2020, 4, 24), FMT_NUMERIC_DMY, False),
        ("06-21-2024", date(2024, 6, 21), FMT_NUMERIC_MDY, False),  # dash but month first
        ("8/5/2021", date(2021, 8, 5), FMT_NUMERIC_MDY, True),
        # Ambiguous numeric: separator convention, flagged.
        ("02-04-2023", date(2023, 4, 2), FMT_NUMERIC_DMY, True),
        ("03/12/2024", date(2024, 3, 12), FMT_NUMERIC_MDY, True),
        ("03.12.2024", date(2024, 12, 3), FMT_NUMERIC_DMY, True),
        # Same day and month: both readings agree, so not ambiguous.
        ("05-05-2023", date(2023, 5, 5), FMT_NUMERIC_DMY, False),
        ("  2023-02-07  ", date(2023, 2, 7), FMT_ISO_DATE, False),
    ],
)
def test_valid_formats(raw, expected, fmt, ambiguous):
    result = parse_date(raw)
    assert result.status == STATUS_OK
    assert result.value == expected
    assert result.date_format == fmt
    assert result.is_ambiguous is ambiguous


def test_derived_parts():
    result = parse_date("2024-08-15")
    assert (result.year, result.quarter, result.month) == (2024, 3, 8)
    assert parse_date("2024-12-31").quarter == 4
    assert parse_date("2024-01-01").quarter == 1


@pytest.mark.parametrize("raw", [None, "", " ", "NULL", "n/a", "NaT", "unknown"])
def test_missing(raw):
    result = parse_date(raw)
    assert result.status == STATUS_MISSING
    assert result.value is None
    assert result.year is None and result.quarter is None and result.month is None


@pytest.mark.parametrize(
    "raw",
    [
        "13/13/2023",  # both fields > 12
        "02/30/2023",  # day does not exist
        "2023-13-01",
        "2023-02-30T00:00:00Z",
        "Foo 12, 2023",
        "12 Foo 2023",
        "yesterday",
        "12/31/23",  # two-digit year not supported
        "1850-01-01",  # outside plausible range
        "02-04/2023",  # mixed separators
    ],
)
def test_invalid(raw):
    result = parse_date(raw)
    assert result.status == STATUS_INVALID
    assert result.value is None
