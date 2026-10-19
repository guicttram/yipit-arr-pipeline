"""CSV exports: ``ai_articles_enriched.csv`` plus one CSV per modeled table / view.

Every file is written to a temporary name and atomically renamed, with a deterministic row
order, so a re-run produces byte-identical files and readers never see a half-written CSV.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import duckdb

from yipit_pipeline.config import AI_BUCKET, EXPORT_DATE_FROM, EXPORT_DATE_TO, EXPORT_MIN_ARR_USD

log = logging.getLogger(__name__)

AI_EXPORT_FILE = "ai_articles_enriched.csv"

AI_EXPORT_COLUMNS = (
    "article_id",
    "title",
    "company_name",
    "published_date",
    "category",
    "arr_usd",
    "summary",
    "url",
    "industry",
    "founded_year",
    "headquarters",
    "employee_count",
    "is_public",
    "stock_ticker",
    "company_age",
    "company_size_category",
    "embedding",
    "top_similar_articles",
)

# Spec: (category AI/ML OR industry AI/ML) AND published 2022-2024 AND valid ARR > $50M.
# A valid ARR means an ARR observation exists, i.e. the company is resolved as well.
AI_EXPORT_SQL = """
SELECT
    article_id, title, company_name, published_date, category, arr_usd, summary, url,
    industry, founded_year, headquarters, employee_count, is_public, stock_ticker,
    company_age, company_size_category,
    to_json(embedding)            AS embedding,
    to_json(top_similar_articles) AS top_similar_articles
FROM v_articles_enriched
WHERE (category = $ai OR industry_bucket = $ai)
  AND published_date BETWEEN $date_from AND $date_to
  AND observation_id IS NOT NULL
  AND arr_usd > $min_arr
ORDER BY published_date, article_id
"""

# Modeled tables and views exported one CSV each: name -> ORDER BY.
TABLE_EXPORTS: dict[str, str] = {
    "dim_company": "company_id",
    "dim_company_alias": "alias_key",
    "fact_article": "article_id",
    "fact_arr_observation": "company_id, as_of_date, article_id",
    "article_embedding": "article_id",
    "article_similarity": "article_id, rank",
    "unmatched_companies": "company_name_raw",
    "company_match_suggestions": "company_name_raw",
    "dq_issues": "stage, entity, entity_id, field, reason, issue_id",
    "dq_check_results": "severity, check_name",
    "v_company_arr_timeline": "company_name, as_of_date, article_id",
    "v_company_arr_latest": "company_name",
    "v_company_arr_quarterly": "company_name, quarter",
}

# Columns that are lists/arrays are serialized as JSON strings.
_JSON_COLUMNS = {"article_embedding": ["embedding"]}


def _copy(con: duckdb.DuckDBPyConnection, select_sql: str, target: Path, params=None) -> int:
    tmp = target.with_suffix(target.suffix + ".tmp")
    con.execute(f"COPY ({select_sql}) TO '{tmp.as_posix()}' (HEADER, DELIMITER ',')", params)
    os.replace(tmp, target)
    return int(con.execute(f"SELECT count(*) FROM ({select_sql})", params).fetchone()[0])


def _table_select(con: duckdb.DuckDBPyConnection, name: str, order_by: str) -> str:
    columns = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
    json_cols = _JSON_COLUMNS.get(name, [])
    select = ", ".join(f"to_json({c}) AS {c}" if c in json_cols else c for c in columns)
    return f"SELECT {select} FROM {name} ORDER BY {order_by}"


def export_ai_articles(con: duckdb.DuckDBPyConnection, outputs_dir: Path) -> int:
    """Write ``ai_articles_enriched.csv``; returns the number of rows."""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    params = {
        "ai": AI_BUCKET,
        "date_from": EXPORT_DATE_FROM,
        "date_to": EXPORT_DATE_TO,
        "min_arr": EXPORT_MIN_ARR_USD,
    }
    n = _copy(con, AI_EXPORT_SQL, outputs_dir / AI_EXPORT_FILE, params)
    log.info("export: %s -> %d rows", AI_EXPORT_FILE, n)
    return n


def run_export(con: duckdb.DuckDBPyConnection, outputs_dir: Path) -> dict[str, int]:
    """Write every export CSV to ``outputs_dir``. Returns {file name: row count}."""
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    counts = {AI_EXPORT_FILE: export_ai_articles(con, outputs_dir)}
    for name, order_by in TABLE_EXPORTS.items():
        file_name = f"{name}.csv"
        counts[file_name] = _copy(con, _table_select(con, name, order_by), outputs_dir / file_name)
        log.info("export: %s -> %d rows", file_name, counts[file_name])
    return counts
