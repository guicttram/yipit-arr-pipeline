"""Data quality: record-level issues (``dq_issues``) and table-level checks (``dq_check_results``).

Record-level issues are written by the step that detects them (transform/model) and never
drop a record by themselves: they explain why a value is NULL or why an article produced no
ARR observation. Table-level checks run after the model is built; a failed ``blocking`` check
stops the export, a failed ``warning`` check is reported only.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass

import duckdb
import pyarrow as pa

from yipit_pipeline.warehouse import insert_rows, scalar, table_exists

log = logging.getLogger(__name__)

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
BLOCKING = "blocking"
WARNING = "warning"

DQ_ISSUE_SCHEMA = pa.schema(
    [
        pa.field("issue_id", pa.string()),
        pa.field("stage", pa.string()),
        pa.field("entity", pa.string()),
        pa.field("entity_id", pa.string()),
        pa.field("field", pa.string()),
        pa.field("raw_value", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("severity", pa.string()),
        pa.field("source_batch_id", pa.string()),
    ]
)


def make_issue(
    stage: str,
    entity: str,
    entity_id: str | None,
    field: str,
    raw_value: object,
    reason: str,
    severity: str,
    source_batch_id: str | None,
) -> dict:
    """Build one ``dq_issues`` row with a deterministic ``issue_id``."""
    raw = None if raw_value is None else str(raw_value)
    key = "|".join(str(p) for p in (stage, entity, entity_id, field, reason, raw))
    return {
        "issue_id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
        "stage": stage,
        "entity": entity,
        "entity_id": entity_id,
        "field": field,
        "raw_value": raw,
        "reason": reason,
        "severity": severity,
        "source_batch_id": source_batch_id,
    }


def replace_stage_issues(con: duckdb.DuckDBPyConnection, stage: str, issues: Iterable[dict]) -> int:
    """Replace all issues owned by ``stage`` (call inside the step's transaction)."""
    con.execute("DELETE FROM dq_issues WHERE stage = ?", [stage])
    return insert_rows(con, "dq_issues", issues, DQ_ISSUE_SCHEMA)


@dataclass(frozen=True)
class CheckResult:
    check_name: str
    severity: str
    passed: bool
    observed: int
    details: str


def _count_check(
    con: duckdb.DuckDBPyConnection, name: str, severity: str, sql: str, details: str
) -> CheckResult:
    observed = int(scalar(con, sql) or 0)
    return CheckResult(name, severity, observed == 0, observed, details)


def _duplicates_sql(table: str, key: str) -> str:
    return f"SELECT count(*) FROM (SELECT {key} FROM {table} GROUP BY ALL HAVING count(*) > 1)"


def run_checks(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """Run all table-level checks and persist them to ``dq_check_results``."""
    c: list[CheckResult] = []

    def add(name: str, severity: str, sql: str, details: str) -> None:
        c.append(_count_check(con, name, severity, sql, details))

    # Uniqueness of keys.
    for table, key in [
        ("silver_articles", "article_id"),
        ("silver_company", "company_id"),
        ("fact_article", "article_id"),
        ("dim_company", "company_id"),
        ("dim_company", "company_key"),
        ("dim_company_alias", "alias_key"),
        ("fact_arr_observation", "observation_id"),
        ("fact_arr_observation", "article_id"),
        ("article_embedding", "article_id"),
        ("article_similarity", "article_id, rank"),
    ]:
        add(f"unique:{table}.{key}", BLOCKING, _duplicates_sql(table, key), "duplicate keys")

    # Not-null on required columns.
    for table, column in [
        ("fact_article", "article_id"),
        ("dim_company", "company_id"),
        ("fact_arr_observation", "company_id"),
        ("fact_arr_observation", "article_id"),
        ("fact_arr_observation", "as_of_date"),
        ("fact_arr_observation", "arr_usd"),
    ]:
        add(
            f"not_null:{table}.{column}",
            BLOCKING,
            f"SELECT count(*) FROM {table} WHERE {column} IS NULL",
            "NULL in a required column",
        )

    # Referential integrity.
    add(
        "fk:fact_arr_observation.article_id->fact_article",
        BLOCKING,
        "SELECT count(*) FROM fact_arr_observation o ANTI JOIN fact_article a USING (article_id)",
        "observation without source article",
    )
    add(
        "fk:fact_arr_observation.company_id->dim_company",
        BLOCKING,
        "SELECT count(*) FROM fact_arr_observation o ANTI JOIN dim_company d USING (company_id)",
        "observation with unknown company",
    )
    add(
        "fk:fact_article.company_id->dim_company",
        BLOCKING,
        "SELECT count(*) FROM fact_article a ANTI JOIN dim_company d USING (company_id) "
        "WHERE a.company_id IS NOT NULL",
        "article with unknown company",
    )
    add(
        "fk:fact_article.arr_observation_id->fact_arr_observation",
        BLOCKING,
        "SELECT count(*) FROM fact_article a WHERE arr_observation_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM fact_arr_observation o WHERE o.observation_id = a.arr_observation_id)",
        "dangling observation reference",
    )
    add(
        "fk:article_similarity->fact_article",
        BLOCKING,
        "SELECT count(*) FROM article_similarity s WHERE s.similar_article_id NOT IN "
        "(SELECT article_id FROM fact_article) OR s.article_id NOT IN "
        "(SELECT article_id FROM fact_article)",
        "similarity pointing to unknown article",
    )

    # ARR observations only from valid ARR.
    add(
        "arr:observation_from_valid_arr_only",
        BLOCKING,
        "SELECT count(*) FROM fact_arr_observation o JOIN silver_articles s USING (article_id) "
        "WHERE s.arr_parse_status <> 'ok' OR o.arr_usd IS NULL OR o.arr_usd <= 0",
        "observation from missing/undisclosed/invalid ARR",
    )
    add(
        "arr:every_eligible_article_observed",
        BLOCKING,
        "SELECT count(*) FROM silver_articles s WHERE s.arr_parse_status = 'ok' "
        "AND s.company_id IS NOT NULL AND s.date_parse_status = 'ok' "
        "AND s.article_id NOT IN (SELECT article_id FROM fact_arr_observation)",
        "eligible article without observation",
    )

    # Row reconciliation: bronze = silver + rejected (superseded or keyless rows).
    add(
        "reconcile:bronze_eq_silver_plus_rejected",
        BLOCKING,
        "SELECT abs((SELECT count(*) FROM bronze_articles_raw) - (SELECT count(*) FROM "
        "silver_articles) - (SELECT count(*) FROM dq_issues WHERE stage = 'transform' AND "
        "reason IN ('superseded_by_newer_row', 'missing_article_id')))",
        "bronze rows not accounted for",
    )
    add(
        "reconcile:silver_eq_fact_article",
        BLOCKING,
        "SELECT abs((SELECT count(*) FROM silver_articles) - (SELECT count(*) FROM fact_article))",
        "silver and fact_article row counts differ",
    )
    add(
        "metadata:company_key_unique_after_normalization",
        BLOCKING,
        _duplicates_sql("silver_company", "company_key"),
        "two metadata companies share a normalized key",
    )

    # Search layer.
    add(
        "embedding:every_article_embedded",
        BLOCKING,
        "SELECT count(*) FROM fact_article ANTI JOIN article_embedding USING (article_id)",
        "article without embedding",
    )
    add(
        "similarity:never_self",
        BLOCKING,
        "SELECT count(*) FROM article_similarity WHERE article_id = similar_article_id",
        "article listed as its own neighbour",
    )
    add(
        "similarity:top3_per_article",
        BLOCKING,
        "SELECT count(*) FROM fact_article a WHERE (SELECT count(*) FROM fact_article) > 3 AND "
        "(SELECT count(*) FROM article_similarity s WHERE s.article_id = a.article_id) <> 3",
        "article without exactly 3 neighbours",
    )

    # Warnings: known source inconsistencies, monitored as signals.
    add(
        "warn:is_public_vs_stock_ticker",
        WARNING,
        "SELECT count(*) FROM dim_company WHERE (is_public AND stock_ticker IS NULL) OR "
        "(NOT is_public AND stock_ticker IS NOT NULL)",
        "public without ticker or private with ticker (source data, not fixed)",
    )
    add(
        "warn:unmatched_company_articles",
        WARNING,
        "SELECT count(*) FROM fact_article WHERE company_id IS NULL",
        "articles whose company is not in metadata (see unmatched_companies)",
    )
    add(
        "warn:ambiguous_dates",
        WARNING,
        "SELECT count(*) FROM silver_articles WHERE date_is_ambiguous",
        "day/month ambiguous numeric dates resolved by the separator rule",
    )
    add(
        "warn:invalid_or_missing_dates",
        WARNING,
        "SELECT count(*) FROM silver_articles WHERE date_parse_status <> 'ok'",
        "articles without a usable date",
    )
    add(
        "warn:unmapped_categories",
        WARNING,
        "SELECT count(*) FROM silver_articles WHERE category_status = 'unmapped'",
        "category labels not in the taxonomy (drift)",
    )
    add(
        "warn:unparseable_arr",
        WARNING,
        "SELECT count(*) FROM silver_articles WHERE arr_parse_status IN "
        "('unparseable', 'invalid_non_positive')",
        "revenue strings that are neither valid nor declared missing",
    )
    add(
        "warn:pending_match_suggestions",
        WARNING,
        "SELECT count(*) FROM company_match_suggestions WHERE status = 'pending'",
        "fuzzy suggestions awaiting review",
    )

    con.begin()
    try:
        con.execute("DELETE FROM dq_check_results")
        insert_rows(
            con,
            "dq_check_results",
            [r.__dict__ for r in c],
            pa.schema(
                [
                    pa.field("check_name", pa.string()),
                    pa.field("severity", pa.string()),
                    pa.field("passed", pa.bool_()),
                    pa.field("observed", pa.int64()),
                    pa.field("details", pa.string()),
                ]
            ),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise

    failed = [r for r in c if not r.passed]
    for r in failed:
        level = logging.ERROR if r.severity == BLOCKING else logging.WARNING
        log.log(
            level,
            "dq %s check failed: %s (%d) - %s",
            r.severity,
            r.check_name,
            r.observed,
            r.details,
        )
    log.info(
        "dq: %d checks, %d blocking failures, %d warnings",
        len(c),
        sum(1 for r in failed if r.severity == BLOCKING),
        sum(1 for r in failed if r.severity == WARNING),
    )
    return c


def blocking_failures(results: Iterable[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.severity == BLOCKING and not r.passed]


def issue_summary(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, int]]:
    """(severity, reason, count) of record-level issues, most frequent first."""
    if not table_exists(con, "dq_issues"):
        return []
    return con.execute(
        "SELECT severity, reason, count(*) AS n FROM dq_issues GROUP BY ALL ORDER BY n DESC, reason"
    ).fetchall()
