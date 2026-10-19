"""Parse the raw `revenue` string into an integer ARR in USD.

Pure function, no I/O. Rules (see docs/decisions.md):
- Decimal arithmetic end to end; each bound is converted to USD, then the midpoint is taken,
  and the value is rounded once at the end with ROUND_HALF_UP.
- Missing (`NULL`, empty, `N/A`) and undisclosed (`Not disclosed`) are distinct statuses and
  never produce a value.
- Values <= 0 after rounding are `invalid_non_positive`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from yipit_pipeline.config import FX_TO_USD

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_UNDISCLOSED = "undisclosed"
STATUS_UNPARSEABLE = "unparseable"
STATUS_INVALID_NON_POSITIVE = "invalid_non_positive"

METHOD_SINGLE = "single_value"
METHOD_RANGE = "range_midpoint"

MISSING_TOKENS = frozenset({"", "null", "none", "nan", "n/a", "na", "-", "--", "—", "?"})
UNDISCLOSED_TOKENS = frozenset(
    {"not disclosed", "undisclosed", "not publicly disclosed", "confidential", "withheld"}
)

_SYMBOL_TO_CODE = {"$": "USD", "us$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_CODES = {"usd": "USD", "eur": "EUR", "gbp": "GBP", "jpy": "JPY"}

_UNIT_MULTIPLIER = {
    "k": Decimal(10) ** 3,
    "thousand": Decimal(10) ** 3,
    "m": Decimal(10) ** 6,
    "mm": Decimal(10) ** 6,
    "mn": Decimal(10) ** 6,
    "million": Decimal(10) ** 6,
    "b": Decimal(10) ** 9,
    "bn": Decimal(10) ** 9,
    "billion": Decimal(10) ** 9,
    "t": Decimal(10) ** 12,
    "tn": Decimal(10) ** 12,
    "trillion": Decimal(10) ** 12,
}

# One monetary amount: optional sign, currency (symbol or code) before or after, a number with
# optional thousands separators, an optional scale unit.
_AMOUNT_RE = re.compile(
    r"""
    (?P<sign1>[+-])?\s*
    (?P<pre>us\$|[$€£¥]|usd|eur|gbp|jpy)?\s*
    (?P<sign2>[+-])?
    (?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)\s*
    (?P<unit>thousand|million|billion|trillion|mm|mn|bn|tn|k|m|b|t)?\.?\s*
    (?P<post>usd|eur|gbp|jpy)?
    """,
    re.VERBOSE,
)

# Range separator: hyphen / en dash / em dash / "to", only between two amounts (so a leading
# minus sign is never mistaken for a separator).
_RANGE_SEP_RE = re.compile(r"(?<=[\w.])\s*(?:-|–|—|\bto\b)\s*(?=[+$€£¥.]|\d|usd|eur|gbp|jpy|us\$)")


@dataclass(frozen=True)
class ArrResult:
    """Outcome of parsing one raw revenue value."""

    arr_usd: int | None
    status: str
    parse_method: str | None = None
    currency: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.status == STATUS_OK


@dataclass(frozen=True)
class _Amount:
    value: Decimal
    unit: str | None
    currency: str | None
    grouped: bool  # written with thousands separators, i.e. already a full figure


def _normalize(raw: str) -> str:
    text = raw.strip().lower().replace(" ", " ")
    return re.sub(r"\s+", " ", text)


def _parse_amount(text: str) -> _Amount | None:
    match = _AMOUNT_RE.fullmatch(text.strip())
    if not match:
        return None
    pre, post = match["pre"], match["post"]
    pre_code = _SYMBOL_TO_CODE.get(pre) or _CODES.get(pre) if pre else None
    post_code = _CODES.get(post) if post else None
    if pre_code and post_code and pre_code != post_code:
        return None  # e.g. "€5M USD": contradictory currencies
    if match["sign1"] and match["sign2"]:
        return None
    try:
        value = Decimal(match["num"].replace(",", ""))
    except InvalidOperation:  # pragma: no cover - regex guarantees a number
        return None
    if (match["sign1"] or match["sign2"]) == "-":
        value = -value
    return _Amount(
        value=value,
        unit=match["unit"],
        currency=pre_code or post_code,
        grouped="," in match["num"],
    )


def _to_usd(amount: _Amount, unit: str | None, currency: str) -> Decimal:
    multiplier = _UNIT_MULTIPLIER[unit] if unit else Decimal(1)
    return amount.value * multiplier * FX_TO_USD[currency]


def parse_arr(raw: object) -> ArrResult:
    """Parse a raw revenue value into USD ARR.

    Accepts formats such as ``$215.0M``, ``$2.470B``, ``$0.640 billion``, ``1230.0M USD``,
    ``€27,272,727``, ``¥123,456,000,000`` and ranges ``$41.6M - $45.2M`` (midpoint).
    A value without any currency marker is assumed to be USD (``currency`` is then ``None``).
    """
    if raw is None:
        return ArrResult(None, STATUS_MISSING)
    text = _normalize(str(raw))
    if text in MISSING_TOKENS:
        return ArrResult(None, STATUS_MISSING)
    if text in UNDISCLOSED_TOKENS:
        return ArrResult(None, STATUS_UNDISCLOSED)

    parts = _RANGE_SEP_RE.split(text)
    if len(parts) > 2:
        return ArrResult(None, STATUS_UNPARSEABLE)
    amounts = [_parse_amount(part) for part in parts]
    if any(a is None for a in amounts):
        return ArrResult(None, STATUS_UNPARSEABLE)

    # In a range, a bound without unit/currency inherits it from the other bound ("10-20M").
    units = {a.unit for a in amounts if a.unit}
    currencies = {a.currency for a in amounts if a.currency}
    if len(currencies) > 1:
        return ArrResult(None, STATUS_UNPARSEABLE)
    currency = currencies.pop() if currencies else None
    shared_unit = units.pop() if len(units) == 1 else None

    usd_values = [
        _to_usd(a, a.unit or (None if a.grouped else shared_unit), currency or "USD")
        for a in amounts  # type: ignore[union-attr]
    ]
    method = METHOD_RANGE if len(usd_values) == 2 else METHOD_SINGLE
    total = sum(usd_values, Decimal(0)) / len(usd_values)
    arr_usd = int(total.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if arr_usd <= 0:
        return ArrResult(None, STATUS_INVALID_NON_POSITIVE, method, currency)
    return ArrResult(arr_usd, STATUS_OK, method, currency)
