"""Silver layer: typed, cleaned, one row per entity, rebuilt from bronze in one transaction.

- ``silver_company``: latest metadata batch per company, typed and validated.
- ``silver_articles``: latest bronze row per ``article_id`` (most recent batch, then last row
  in the file), with every cleaned field next to its raw original and parse status.
- ``dq_issues`` (stage ``transform``): one row per problem found while cleaning.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass

import duckdb
import pyarrow as pa

from yipit_pipeline.cleaning import arr as arr_mod
from yipit_pipeline.cleaning import dates as dates_mod
from yipit_pipeline.cleaning.categories import (
    STATUS_MISSING as CATEGORY_MISSING,
)
from yipit_pipeline.cleaning.categories import (
    STATUS_UNMAPPED,
    map_category,
)
from yipit_pipeline.company import CompanyResolver, company_id_for, company_key
from yipit_pipeline.config import ARR_WARN_MAX_USD, ARR_WARN_MIN_USD
from yipit_pipeline.dq import SEVERITY_INFO, SEVERITY_WARNING, make_issue, replace_stage_issues
from yipit_pipeline.warehouse import insert_rows

log = logging.getLogger(__name__)

STAGE = "transform"

SILVER_COMPANY_SCHEMA = pa.schema(
    [
        ("company_id", pa.string()),
        ("company_name", pa.string()),
        ("company_key", pa.string()),
        ("founded_year", pa.int32()),
        ("headquarters", pa.string()),
        ("employee_count", pa.int32()),
        ("industry", pa.string()),
        ("industry_bucket", pa.string()),
        ("is_public", pa.bool_()),
        ("stock_ticker", pa.string()),
        ("source_batch_id", pa.string()),
        ("source_batch_seq", pa.int32()),
        ("source_file", pa.string()),
    ]
)

SILVER_ARTICLES_SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),
        ("title", pa.string()),
        ("summary", pa.string()),
        ("url", pa.string()),
        ("author", pa.string()),
        ("word_count", pa.int32()),
        ("company_name_raw", pa.string()),
        ("company_key", pa.string()),
        ("company_id", pa.string()),
        ("company_match_status", pa.string()),
        ("company_match_method", pa.string()),
        ("company_unmatched_reason", pa.string()),
        ("published_date_raw", pa.string()),
        ("published_date", pa.date32()),
        ("published_year", pa.int32()),
        ("published_quarter", pa.int32()),
        ("published_month", pa.int32()),
        ("date_parse_status", pa.string()),
        ("date_format", pa.string()),
        ("date_is_ambiguous", pa.bool_()),
        ("category_raw", pa.string()),
        ("category_key", pa.string()),
        ("category", pa.string()),
        ("category_status", pa.string()),
        ("revenue_raw", pa.string()),
        ("arr_usd", pa.int64()),
        ("arr_parse_status", pa.string()),
        ("arr_parse_method", pa.string()),
        ("arr_currency_detected", pa.string()),
        ("extra_json", pa.string()),
        ("source_batch_id", pa.string()),
        ("source_batch_seq", pa.int32()),
        ("source_file", pa.string()),
        ("source_row_number", pa.int32()),
    ]
)

_ARR_SEVERITY = {
    arr_mod.STATUS_MISSING: SEVERITY_INFO,
    arr_mod.STATUS_UNDISCLOSED: SEVERITY_INFO,
    arr_mod.STATUS_UNPARSEABLE: SEVERITY_WARNING,
    arr_mod.STATUS_INVALID_NON_POSITIVE: SEVERITY_WARNING,
}


@dataclass(frozen=True)
class TransformStats:
    bronze_rows: int
    silver_articles: int
    silver_companies: int
    rejected_rows: int
    issues: int
    arr_status: dict[str, int]
    date_status: dict[str, int]
    company_status: dict[str, int]


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _as_int(value: object) -> int | None:
    """Strict integer: ints, or strings/floats holding a whole number. Booleans rejected."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return int(number) if number.is_integer() else None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(
            value.strip().lower()
        )
    return None


def build_silver_company(con: duckdb.DuckDBPyConnection) -> tuple[list[dict], list[dict]]:
    """Latest metadata row per company, typed. Returns (rows, dq issues)."""
    bronze = con.execute(
        """
        SELECT m.company_name, m.raw_json, m._batch_id, b.batch_seq, m._source_file
        FROM bronze_company_metadata_raw m JOIN ingest_batch b ON b.batch_id = m._batch_id
        QUALIFY row_number() OVER (PARTITION BY m.company_name ORDER BY b.batch_seq DESC) = 1
        ORDER BY m.company_name
        """
    ).fetchall()
    rows: list[dict] = []
    issues: list[dict] = []
    for name, raw_json, batch_id, batch_seq, source_file in bronze:
        attrs = json.loads(raw_json)
        key = company_key(name)

        def flag(field: str, reason: str, severity: str = SEVERITY_WARNING) -> None:
            issues.append(
                make_issue(
                    STAGE, "company", name, field, attrs.get(field), reason, severity, batch_id
                )
            )

        founded = _as_int(attrs.get("founded_year"))
        if founded is not None and not 1800 <= founded <= 2100:
            founded = None
        if founded is None:
            flag("founded_year", "invalid_or_missing_founded_year")
        employees = _as_int(attrs.get("employee_count"))
        if employees is not None and employees < 0:
            employees = None
        if employees is None:
            flag("employee_count", "invalid_or_missing_employee_count")
        is_public = _as_bool(attrs.get("is_public"))
        if is_public is None:
            flag("is_public", "invalid_or_missing_is_public")
        ticker = attrs.get("stock_ticker")
        ticker = ticker.strip() or None if isinstance(ticker, str) else None
        industry = attrs.get("industry")
        industry = industry.strip() or None if isinstance(industry, str) else None
        industry_mapping = map_category(industry)
        if industry is None:
            flag("industry", "missing_industry")
        elif industry_mapping.status == STATUS_UNMAPPED:
            flag("industry", "unmapped_industry")
        if is_public is True and ticker is None:
            flag("stock_ticker", "public_without_ticker")
        if is_public is False and ticker is not None:
            flag("stock_ticker", "private_with_ticker")
        hq = attrs.get("headquarters")
        rows.append(
            {
                "company_id": company_id_for(key) if key else None,
                "company_name": name,
                "company_key": key,
                "founded_year": founded,
                "headquarters": hq.strip() or None if isinstance(hq, str) else None,
                "employee_count": employees,
                "industry": industry,
                "industry_bucket": industry_mapping.bucket,
                "is_public": is_public,
                "stock_ticker": ticker,
                "source_batch_id": batch_id,
                "source_batch_seq": batch_seq,
                "source_file": source_file,
            }
        )
    return rows, issues


def clean_article(raw: dict, resolver: CompanyResolver) -> tuple[dict, list[dict]]:
    """Clean one bronze article row. Returns (silver row, dq issues). Pure apart from resolver."""
    article_id = raw["article_id"].strip()
    batch_id = raw["_batch_id"]
    issues: list[dict] = []

    def flag(field: str, value: object, reason: str, severity: str = SEVERITY_WARNING) -> None:
        issues.append(
            make_issue(STAGE, "article", article_id, field, value, reason, severity, batch_id)
        )

    revenue_raw = raw["revenue"]
    arr = arr_mod.parse_arr(revenue_raw)
    if not arr.is_valid:
        flag("revenue", revenue_raw, f"arr_{arr.status}", _ARR_SEVERITY[arr.status])
    else:
        if not ARR_WARN_MIN_USD <= arr.arr_usd <= ARR_WARN_MAX_USD:  # type: ignore[operator]
            flag("revenue", revenue_raw, "arr_out_of_expected_range")
        if arr.currency is None:
            flag("revenue", revenue_raw, "arr_currency_assumed_usd", SEVERITY_INFO)

    date_raw = raw["published_date"]
    parsed_date = dates_mod.parse_date(date_raw)
    if parsed_date.status != dates_mod.STATUS_OK:
        flag("published_date", date_raw, f"date_{parsed_date.status}")
    elif parsed_date.is_ambiguous:
        flag("published_date", date_raw, "date_ambiguous_day_month", SEVERITY_INFO)

    category_raw = raw["category"]
    category = map_category(category_raw)
    if category.status == STATUS_UNMAPPED:
        flag("category", category_raw, "category_unmapped")
    elif category.status == CATEGORY_MISSING:
        flag("category", category_raw, "category_missing")

    company_raw = raw["company_name"]
    match = resolver.resolve(company_raw)
    if match.company_id is None:
        flag("company_name", company_raw, f"company_{match.unmatched_reason}")

    for field in ("title", "url"):
        if _blank_to_none(raw[field]) is None:
            flag(field, raw[field], "missing_required_field")

    word_count_raw = _blank_to_none(raw.get("word_count"))
    word_count = _as_int(word_count_raw)
    if word_count_raw is not None and word_count is None:
        flag("word_count", word_count_raw, "invalid_word_count")

    row = {
        "article_id": article_id,
        "title": _blank_to_none(raw["title"]),
        "summary": _blank_to_none(raw["summary"]),
        "url": _blank_to_none(raw["url"]),
        "author": _blank_to_none(raw.get("author")),
        "word_count": word_count,
        "company_name_raw": company_raw,
        "company_key": company_key(company_raw),
        "company_id": match.company_id,
        "company_match_status": match.status,
        "company_match_method": match.method,
        "company_unmatched_reason": match.unmatched_reason,
        "published_date_raw": date_raw,
        "published_date": parsed_date.value,
        "published_year": parsed_date.year,
        "published_quarter": parsed_date.quarter,
        "published_month": parsed_date.month,
        "date_parse_status": parsed_date.status,
        "date_format": parsed_date.date_format,
        "date_is_ambiguous": parsed_date.is_ambiguous,
        "category_raw": category_raw,
        "category_key": category.key,
        "category": category.bucket,
        "category_status": category.status,
        "revenue_raw": revenue_raw,
        "arr_usd": arr.arr_usd,
        "arr_parse_status": arr.status,
        "arr_parse_method": arr.parse_method,
        "arr_currency_detected": arr.currency,
        "extra_json": raw.get("_extra_json"),
        "source_batch_id": batch_id,
        "source_batch_seq": raw["batch_seq"],
        "source_file": raw["_source_file"],
        "source_row_number": raw["_row_number"],
    }
    return row, issues


def _bronze_articles(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cursor = con.execute(
        """
        SELECT a.*, b.batch_seq
        FROM bronze_articles_raw a JOIN ingest_batch b ON b.batch_id = a._batch_id
        ORDER BY b.batch_seq, a._row_number
        """
    )
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]


def run_transform(con: duckdb.DuckDBPyConnection) -> TransformStats:
    """Rebuild silver tables and transform-stage DQ issues from bronze (one transaction)."""
    companies, issues = build_silver_company(con)
    resolver = CompanyResolver(c["company_name"] for c in companies)  # asserts unique keys

    bronze = _bronze_articles(con)
    latest: dict[str, dict] = {}
    for raw in bronze:  # ordered by batch_seq, row_number: later rows win
        article_id = _blank_to_none(raw["article_id"])
        if article_id is None:
            issues.append(
                make_issue(
                    STAGE,
                    "article",
                    f"{raw['_batch_id']}:{raw['_row_number']}",
                    "article_id",
                    raw["article_id"],
                    "missing_article_id",
                    SEVERITY_WARNING,
                    raw["_batch_id"],
                )
            )
            continue
        previous = latest.get(article_id)
        if previous is not None:
            issues.append(
                make_issue(
                    STAGE,
                    "article",
                    article_id,
                    "article_id",
                    f"{previous['_batch_id']}:{previous['_row_number']}",
                    "superseded_by_newer_row",
                    SEVERITY_INFO,
                    previous["_batch_id"],
                )
            )
        latest[article_id] = raw

    articles: list[dict] = []
    for article_id in sorted(latest):
        row, row_issues = clean_article(latest[article_id], resolver)
        articles.append(row)
        issues.extend(row_issues)

    con.begin()
    try:
        con.execute("DELETE FROM silver_company")
        con.execute("DELETE FROM silver_articles")
        insert_rows(con, "silver_company", companies, SILVER_COMPANY_SCHEMA)
        insert_rows(con, "silver_articles", articles, SILVER_ARTICLES_SCHEMA)
        replace_stage_issues(con, STAGE, issues)
        con.commit()
    except Exception:
        con.rollback()
        raise

    stats = TransformStats(
        bronze_rows=len(bronze),
        silver_articles=len(articles),
        silver_companies=len(companies),
        rejected_rows=len(bronze) - len(articles),
        issues=len(issues),
        arr_status=dict(Counter(a["arr_parse_status"] for a in articles)),
        date_status=dict(Counter(a["date_parse_status"] for a in articles)),
        company_status=dict(
            Counter(a["company_match_method"] or a["company_unmatched_reason"] for a in articles)
        ),
    )
    log.info(
        "transform: %d bronze rows -> %d silver articles (%d rejected/superseded), %d companies",
        stats.bronze_rows,
        stats.silver_articles,
        stats.rejected_rows,
        stats.silver_companies,
    )
    log.info("transform: ARR status %s", stats.arr_status)
    log.info("transform: date status %s", stats.date_status)
    log.info("transform: company resolution %s", stats.company_status)
    return stats
