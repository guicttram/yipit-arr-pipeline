"""Parse the raw `published_date` string into a date.

Pure function, no I/O. Supported formats (see docs/eda_notes.md for counts):
- ISO date ``2023-02-07`` and ISO timestamp ``2023-05-22T00:00:00Z`` (converted to UTC date);
- month names ``August 15, 2023``, ``Aug 15 2023``, ``21 Feb 2020``, ``21 February 2020``;
- numeric ``NN/NN/YYYY`` and ``NN-NN-YYYY`` (also ``.``).

Rule for numeric dates (ADR-011):
1. If one field is > 12 it must be the day, whatever the separator (value-based).
2. If both fields are <= 12 the value is ambiguous. The separator decides the convention:
   ``/`` -> US month/day (never contradicted in the data); ``-`` and ``.`` -> EU day/month
   (the majority convention among unambiguous ``-`` values, 63 vs 17). The row is flagged
   ``is_ambiguous`` unless both readings give the same date (e.g. ``05-05-2023``).
Day/month swaps never change the year, so ambiguity cannot move an article across a
year-based filter, only across months/quarters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_INVALID = "invalid"

FMT_ISO_DATE = "iso_date"
FMT_ISO_TIMESTAMP = "iso_timestamp"
FMT_MONTH_NAME = "month_name"
FMT_NUMERIC_MDY = "numeric_mdy"
FMT_NUMERIC_DMY = "numeric_dmy"

MIN_YEAR = 1990
MAX_YEAR = 2100

MISSING_TOKENS = frozenset({"", "null", "none", "nan", "nat", "n/a", "na", "-", "unknown"})

_MONTHS = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}

_ISO_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(z|[+-]\d{2}:?\d{2})?")
_MONTH_FIRST_RE = re.compile(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
_DAY_FIRST_RE = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?,?\s+(\d{4})")
_NUMERIC_RE = re.compile(r"(\d{1,2})([/.-])(\d{1,2})\2(\d{4})")

# Separator -> convention used when both numeric fields are <= 12.
AMBIGUOUS_CONVENTION = {"/": FMT_NUMERIC_MDY, "-": FMT_NUMERIC_DMY, ".": FMT_NUMERIC_DMY}


@dataclass(frozen=True)
class DateResult:
    """Outcome of parsing one raw date value."""

    value: date | None
    status: str
    date_format: str | None = None
    is_ambiguous: bool = False

    @property
    def year(self) -> int | None:
        return self.value.year if self.value else None

    @property
    def quarter(self) -> int | None:
        return (self.value.month - 1) // 3 + 1 if self.value else None

    @property
    def month(self) -> int | None:
        return self.value.month if self.value else None


def _build(year: int, month: int, day: int, fmt: str, ambiguous: bool = False) -> DateResult:
    if not MIN_YEAR <= year <= MAX_YEAR:
        return DateResult(None, STATUS_INVALID, fmt)
    try:
        return DateResult(date(year, month, day), STATUS_OK, fmt, ambiguous)
    except ValueError:  # e.g. 02/30/2023
        return DateResult(None, STATUS_INVALID, fmt)


def _parse_numeric(first: int, sep: str, second: int, year: int) -> DateResult:
    if first > 12 and second > 12:
        return DateResult(None, STATUS_INVALID)
    if first > 12:
        return _build(year, second, first, FMT_NUMERIC_DMY)
    if second > 12:
        return _build(year, first, second, FMT_NUMERIC_MDY)
    convention = AMBIGUOUS_CONVENTION[sep]
    ambiguous = first != second
    if convention == FMT_NUMERIC_MDY:
        return _build(year, first, second, convention, ambiguous)
    return _build(year, second, first, convention, ambiguous)


def parse_date(raw: object) -> DateResult:
    """Parse a raw published date. Never raises; unknown shapes return status ``invalid``."""
    if raw is None:
        return DateResult(None, STATUS_MISSING)
    text = re.sub(r"\s+", " ", str(raw).strip().lower())
    if text in MISSING_TOKENS:
        return DateResult(None, STATUS_MISSING)

    if _ISO_TS_RE.fullmatch(text):
        try:
            parsed = datetime.fromisoformat(text.upper().replace(" ", "T"))
        except ValueError:
            return DateResult(None, STATUS_INVALID, FMT_ISO_TIMESTAMP)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc)
        return _build(parsed.year, parsed.month, parsed.day, FMT_ISO_TIMESTAMP)

    if m := _ISO_DATE_RE.fullmatch(text):
        return _build(int(m[1]), int(m[2]), int(m[3]), FMT_ISO_DATE)

    if m := _NUMERIC_RE.fullmatch(text):
        return _parse_numeric(int(m[1]), m[2], int(m[3]), int(m[4]))

    if m := _MONTH_FIRST_RE.fullmatch(text):
        month = _MONTHS.get(m[1])
        if month:
            return _build(int(m[3]), month, int(m[2]), FMT_MONTH_NAME)
        return DateResult(None, STATUS_INVALID)

    if m := _DAY_FIRST_RE.fullmatch(text):
        month = _MONTHS.get(m[2])
        if month:
            return _build(int(m[3]), month, int(m[1]), FMT_MONTH_NAME)

    return DateResult(None, STATUS_INVALID)
