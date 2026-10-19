"""Gold layer: dimensions, facts and company-matching outputs, rebuilt from silver.

All tables are replaced inside one transaction, so a failed run leaves the previous gold
state intact and a re-run produces exactly the same rows (no duplicates, no run timestamps).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import duckdb
import pyarrow as pa

from yipit_pipeline.company import (
    ALIASES,
    company_key,
    size_category,
    suggest_matches,
)
from yipit_pipeline.dq import SEVERITY_WARNING, make_issue, replace_stage_issues
from yipit_pipeline.warehouse import create_views, insert_rows, scalar

log = logging.getLogger(__name__)

STAGE = "model"

DIM_COMPANY_SCHEMA = pa.schema(
    [
        ("company_id", pa.string()),
        ("company_name", pa.string()),
        ("company_key", pa.string()),
        ("industry", pa.string()),
        ("industry_bucket", pa.string()),
        ("founded_year", pa.int32()),
        ("headquarters", pa.string()),
        ("employee_count", pa.int32()),
        ("company_size_category", pa.string()),
        ("is_public", pa.bool_()),
        ("stock_ticker", pa.string()),
        ("source_batch_id", pa.string()),
    ]
)
ALIAS_SCHEMA = pa.schema(
    [
        ("alias_key", pa.string()),
        ("alias_name", pa.string()),
        ("company_id", pa.string()),
        ("alias_source", pa.string()),
    ]
)
SUGGESTION_SCHEMA = pa.schema(
    [
        ("company_name_raw", pa.string()),
        ("company_key", pa.string()),
        ("candidate_company_name", pa.string()),
        ("candidate_company_id", pa.string()),
        ("score", pa.float64()),
        ("scorer", pa.string()),
        ("n_articles", pa.int32()),
        ("status", pa.string()),
        ("review_note", pa.string()),
    ]
)

_FACT_ARR_SQL = """
INSERT INTO fact_arr_observation BY NAME
SELECT
    left(sha256(article_id), 16)                          AS observation_id,
    company_id,
    article_id,
    published_date                                        AS as_of_date,
    published_year                                        AS as_of_year,
    published_year::VARCHAR || '-Q' || published_quarter::VARCHAR AS as_of_quarter,
    arr_usd,
    revenue_raw                                           AS arr_raw,
    arr_currency_detected                                 AS currency_detected,
    arr_parse_method                                      AS parse_method,
    date_is_ambiguous,
    source_batch_id,
    source_batch_seq,
    source_file,
    source_row_number
FROM silver_articles
WHERE arr_parse_status = 'ok' AND company_id IS NOT NULL AND date_parse_status = 'ok'
ORDER BY article_id
"""

_FACT_ARTICLE_SQL = """
INSERT INTO fact_article BY NAME
SELECT
    s.article_id,
    s.company_id,
    s.company_name_raw,
    s.company_match_method,
    s.company_unmatched_reason,
    s.title,
    s.summary,
    s.url,
    s.author,
    s.word_count,
    s.published_date,
    s.published_year,
    s.published_quarter,
    s.published_month,
    s.date_parse_status,
    s.date_is_ambiguous,
    s.category,
    s.category_raw,
    s.revenue_raw,
    s.arr_parse_status,
    o.observation_id AS arr_observation_id,
    CASE
        WHEN o.observation_id IS NOT NULL THEN NULL
        WHEN s.arr_parse_status <> 'ok' THEN 'no_valid_arr'
        WHEN s.company_id IS NULL THEN 'company_unresolved'
        ELSE 'date_unresolved'
    END AS arr_excluded_reason,
    CASE WHEN s.published_year - c.founded_year >= 0
         THEN s.published_year - c.founded_year END AS company_age,
    s.source_batch_id,
    s.source_file,
    s.source_row_number
FROM silver_articles s
LEFT JOIN fact_arr_observation o ON o.article_id = s.article_id
LEFT JOIN silver_company c ON c.company_id = s.company_id
ORDER BY s.article_id
"""

_UNMATCHED_SQL = """
INSERT INTO unmatched_companies BY NAME
SELECT
    company_name_raw,
    any_value(company_key)                         AS company_key,
    any_value(company_unmatched_reason)            AS unmatched_reason,
    count(*)                                       AS n_articles,
    count(*) FILTER (WHERE arr_parse_status = 'ok') AS n_articles_with_valid_arr,
    min(published_date)                            AS first_published,
    max(published_date)                            AS last_published
FROM silver_articles
WHERE company_id IS NULL
GROUP BY company_name_raw
ORDER BY company_name_raw
"""


@dataclass(frozen=True)
class ModelStats:
    dim_company: int
    dim_company_alias: int
    fact_article: int
    fact_arr_observation: int
    unmatched_companies: int
    match_suggestions: int


def _dim_company_rows(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cursor = con.execute("SELECT * FROM silver_company ORDER BY company_id")
    columns = [d[0] for d in cursor.description]
    rows = []
    for values in cursor.fetchall():
        c = dict(zip(columns, values, strict=True))
        rows.append(
            {
                "company_id": c["company_id"],
                "company_name": c["company_name"],
                "company_key": c["company_key"],
                "industry": c["industry"],
                "industry_bucket": c["industry_bucket"],
                "founded_year": c["founded_year"],
                "headquarters": c["headquarters"],
                "employee_count": c["employee_count"],
                "company_size_category": size_category(c["employee_count"]),
                "is_public": c["is_public"],
                "stock_ticker": c["stock_ticker"],
                "source_batch_id": c["source_batch_id"],
            }
        )
    return rows


def _alias_rows(companies: list[dict]) -> list[dict]:
    by_key = {c["company_key"]: c for c in companies}
    rows = [
        {
            "alias_key": c["company_key"],
            "alias_name": c["company_name"],
            "company_id": c["company_id"],
            "alias_source": "metadata_key",
        }
        for c in companies
    ]
    for alias_key, target in sorted(ALIASES.items()):
        target_company = by_key.get(company_key(target))
        if target_company is not None and alias_key not in by_key:
            rows.append(
                {
                    "alias_key": alias_key,
                    "alias_name": target,
                    "company_id": target_company["company_id"],
                    "alias_source": "alias_table",
                }
            )
    return sorted(rows, key=lambda r: r["alias_key"])


def run_model(con: duckdb.DuckDBPyConnection) -> ModelStats:
    """Rebuild every gold table from silver (one transaction) and refresh the views."""
    companies = _dim_company_rows(con)
    aliases = _alias_rows(companies)

    unresolved = dict(
        con.execute(
            "SELECT company_name_raw, count(*) FROM silver_articles "
            "WHERE company_id IS NULL AND company_name_raw IS NOT NULL GROUP BY ALL"
        ).fetchall()
    )
    names_to_id = {c["company_name"]: c["company_id"] for c in companies}
    suggestions = [
        {**s.__dict__, "candidate_company_id": names_to_id.get(s.candidate_company_name)}
        for s in suggest_matches(unresolved, names_to_id)
    ]

    age_issues = [
        make_issue(
            STAGE,
            "article",
            article_id,
            "company_age",
            age,
            "negative_company_age",
            SEVERITY_WARNING,
            batch_id,
        )
        for article_id, age, batch_id in con.execute(
            "SELECT s.article_id, s.published_year - c.founded_year, s.source_batch_id "
            "FROM silver_articles s JOIN silver_company c USING (company_id) "
            "WHERE s.published_year - c.founded_year < 0 ORDER BY s.article_id"
        ).fetchall()
    ]

    con.begin()
    try:
        for table in (
            "dim_company",
            "dim_company_alias",
            "fact_arr_observation",
            "fact_article",
            "unmatched_companies",
            "company_match_suggestions",
        ):
            con.execute(f"DELETE FROM {table}")
        insert_rows(con, "dim_company", companies, DIM_COMPANY_SCHEMA)
        insert_rows(con, "dim_company_alias", aliases, ALIAS_SCHEMA)
        con.execute(_FACT_ARR_SQL)
        con.execute(_FACT_ARTICLE_SQL)
        con.execute(_UNMATCHED_SQL)
        insert_rows(con, "company_match_suggestions", suggestions, SUGGESTION_SCHEMA)
        replace_stage_issues(con, STAGE, age_issues)
        create_views(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    stats = ModelStats(
        dim_company=len(companies),
        dim_company_alias=len(aliases),
        fact_article=scalar(con, "SELECT count(*) FROM fact_article"),
        fact_arr_observation=scalar(con, "SELECT count(*) FROM fact_arr_observation"),
        unmatched_companies=scalar(con, "SELECT count(*) FROM unmatched_companies"),
        match_suggestions=len(suggestions),
    )
    log.info("model: %s", stats)
    excluded = con.execute(
        "SELECT arr_excluded_reason, count(*) FROM fact_article "
        "WHERE arr_excluded_reason IS NOT NULL GROUP BY ALL ORDER BY 1"
    ).fetchall()
    log.info("model: articles without ARR observation by reason %s", dict(excluded))
    return stats
