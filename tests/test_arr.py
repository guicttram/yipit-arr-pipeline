"""ARR parser. Every revenue shape found in the EDA pattern catalog is a case
(digits -> 9, letters -> a). Values are invented; only the shapes come from the source."""

import pytest

from yipit_pipeline.cleaning.arr import (
    METHOD_RANGE,
    METHOD_SINGLE,
    STATUS_INVALID_NON_POSITIVE,
    STATUS_MISSING,
    STATUS_OK,
    STATUS_UNDISCLOSED,
    STATUS_UNPARSEABLE,
    parse_arr,
)


@pytest.mark.parametrize(
    ("raw", "expected_usd", "method", "currency"),
    [
        # '$9.9a'
        ("$215.0M", 215_000_000, METHOD_SINGLE, "USD"),
        ("$2.470B", 2_470_000_000, METHOD_SINGLE, "USD"),
        ("$64320.0M", 64_320_000_000, METHOD_SINGLE, "USD"),  # large value kept in millions
        # '$9.9 a'
        ("$0.640 billion", 640_000_000, METHOD_SINGLE, "USD"),
        ("$5.2 million", 5_200_000, METHOD_SINGLE, "USD"),
        # '9.9a a'
        ("1230.0M USD", 1_230_000_000, METHOD_SINGLE, "USD"),
        # '$9.9a - $9.9a'
        ("$41.6M - $45.2M", 43_400_000, METHOD_RANGE, "USD"),
        ("$11970.0M - $13230.0M", 12_600_000_000, METHOD_RANGE, "USD"),
        ("$0.491B - $0.543B", 517_000_000, METHOD_RANGE, "USD"),
        # '$9,9,9,9' / '$9,9,9'
        ("$5,200,000,000", 5_200_000_000, METHOD_SINGLE, "USD"),
        ("$750,000", 750_000, METHOD_SINGLE, "USD"),
        # '€9,9,9,9' / '€9,9,9' (x 1.1)
        ("€27,272,727", 30_000_000, METHOD_SINGLE, "EUR"),  # 29,999,999.7 -> rounds up
        ("€500,000", 550_000, METHOD_SINGLE, "EUR"),
        # '£9,9,9,9' / '£9,9,9' (x 1.27)
        ("£12,345,678,901", 15_679_012_204, METHOD_SINGLE, "GBP"),  # ...204.27 -> rounds down
        ("£31,496,062,992", 40_000_000_000, METHOD_SINGLE, "GBP"),
        ("£1,000,000", 1_270_000, METHOD_SINGLE, "GBP"),
        # '¥9,9,9,9' / '¥9,9,9,9,9' / '¥9,9,9' (/ 150)
        ("¥123,456,000,000", 823_040_000, METHOD_SINGLE, "JPY"),
        ("¥1,500,000,000,000", 10_000_000_000, METHOD_SINGLE, "JPY"),
        ("¥150,000", 1_000, METHOD_SINGLE, "JPY"),
    ],
)
def test_eda_patterns(raw, expected_usd, method, currency):
    result = parse_arr(raw)
    assert result.status == STATUS_OK
    assert result.arr_usd == expected_usd
    assert isinstance(result.arr_usd, int)
    assert result.parse_method == method
    assert result.currency == currency


@pytest.mark.parametrize(
    ("raw", "expected_usd"),
    [
        ("€10M – €20M", 16_500_000),  # en dash, each bound converted then midpoint
        ("$10M — $20M", 15_000_000),  # em dash
        ("$10M to $20M", 15_000_000),
        ("$10M-$20M", 15_000_000),  # no spaces
        ("10-20M", 15_000_000),  # first bound inherits the unit
        ("$900M - $1.1B", 1_000_000_000),  # mixed units
        ("$5,000,000 - $7M", 6_000_000),  # grouped bound does not inherit the unit
        (" $5.2 BILLION ", 5_200_000_000),  # case and spaces
        ("$5.2bn", 5_200_000_000),
        ("5.2B", 5_200_000_000),  # no currency: assumed USD
        ("500M USD", 500_000_000),
        ("USD 500M", 500_000_000),
        ("US$ 2.5k", 2_500),
        ("$1.5 trillion", 1_500_000_000_000),
        ("$0.5", 1),  # ROUND_HALF_UP (banker's rounding would give 0)
        ("$2.5", 3),  # ROUND_HALF_UP (banker's rounding would give 2)
    ],
)
def test_variants(raw, expected_usd):
    result = parse_arr(raw)
    assert result.status == STATUS_OK
    assert result.arr_usd == expected_usd


def test_missing_currency_is_reported_as_none():
    assert parse_arr("5.2B").currency is None


@pytest.mark.parametrize("raw", [None, "", "   ", "NULL", "null", "N/A", "n/a", "NaN", "-", "None"])
def test_missing(raw):
    result = parse_arr(raw)
    assert result.status == STATUS_MISSING
    assert result.arr_usd is None
    assert not result.is_valid


@pytest.mark.parametrize("raw", ["Not disclosed", "not  disclosed", "NOT DISCLOSED", "Undisclosed"])
def test_undisclosed_is_distinct_from_missing(raw):
    result = parse_arr(raw)
    assert result.status == STATUS_UNDISCLOSED
    assert result.arr_usd is None


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "$abc",
        "$",
        "5M5",
        "$5M - $6M - $7M",
        "€5M USD",  # contradictory currencies
        "€5M - $6M",  # mixed currencies in a range
        "1,2,3",  # invalid thousands grouping
        "about $5M",
        "+-5M",
        "-$10M - -$5M",  # negative ranges are not supported
    ],
)
def test_unparseable(raw):
    result = parse_arr(raw)
    assert result.status == STATUS_UNPARSEABLE
    assert result.arr_usd is None


@pytest.mark.parametrize("raw", ["$0", "0M", "-$5M", "$-5M", "-5M USD", "$0.4"])
def test_non_positive_is_invalid(raw):
    result = parse_arr(raw)
    assert result.status == STATUS_INVALID_NON_POSITIVE
    assert result.arr_usd is None


def test_non_string_input_is_accepted():
    assert parse_arr(1500).arr_usd == 1500
