"""DuckDB connection, schema management and small helpers shared by the steps."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from importlib import resources
from pathlib import Path

import duckdb
import pyarrow as pa

from yipit_pipeline.config import DERIVED_SCHEMA_VERSION, EMBEDDING_DIM

log = logging.getLogger(__name__)

# Rebuilt from bronze on every run, so safe to drop on a schema change.
DERIVED_TABLES = (
    "silver_articles",
    "silver_company",
    "dq_issues",
    "dim_company",
    "dim_company_alias",
    "fact_article",
    "fact_arr_observation",
    "unmatched_companies",
    "company_match_suggestions",
    "dq_check_results",
    "article_similarity",
)
VIEWS = (
    "v_articles_enriched",
    "v_company_arr_quarterly",
    "v_company_arr_latest",
    "v_company_arr_timeline",
)


def read_sql(name: str) -> str:
    """Read a SQL file shipped in the ``yipit_pipeline/sql`` package directory."""
    return resources.files("yipit_pipeline").joinpath("sql", name).read_text(encoding="utf-8")


def connect(path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the warehouse (creating parent directories) and make sure the schema exists."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        ensure_schema(con)
    return con


def ensure_schema(con: duckdb.DuckDBPyConnection, embedding_dim: int = EMBEDDING_DIM) -> None:
    """Create missing tables. If the derived-layer schema version changed, drop silver/gold
    (they are rebuilt from bronze) before recreating them. Bronze is never dropped."""
    con.execute("CREATE TABLE IF NOT EXISTS pipeline_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    row = con.execute(
        "SELECT value FROM pipeline_meta WHERE key = 'derived_schema_version'"
    ).fetchone()
    if row is not None and row[0] != DERIVED_SCHEMA_VERSION:
        log.warning(
            "derived schema version %s -> %s: dropping silver/gold for rebuild",
            row[0],
            DERIVED_SCHEMA_VERSION,
        )
        for view in VIEWS:
            con.execute(f"DROP VIEW IF EXISTS {view}")
        for table in DERIVED_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(read_sql("ddl.sql").replace("{embedding_dim}", str(embedding_dim)))
    con.execute(
        "INSERT OR REPLACE INTO pipeline_meta VALUES ('derived_schema_version', ?)",
        [DERIVED_SCHEMA_VERSION],
    )


def create_views(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)create the analytical views."""
    con.execute(read_sql("views.sql"))


def insert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: Iterable[dict],
    schema: pa.Schema,
) -> int:
    """Insert dict rows into ``table`` by column name, using an explicit Arrow schema."""
    rows = list(rows)
    if not rows:
        return 0
    arrow_table = pa.Table.from_pylist(rows, schema=schema)
    con.register("_insert_rows", arrow_table)
    try:
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM _insert_rows")
    finally:
        con.unregister("_insert_rows")
    return len(rows)


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence | None = None):
    """Return the first column of the first row of a query."""
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row else None


def table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(
        scalar(
            con,
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [name],
        )
    )
