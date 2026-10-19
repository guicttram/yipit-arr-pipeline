"""Bronze ingestion: copy source files into append-only raw tables with lineage.

- Every value is stored as the raw string (no cleaning here).
- A file whose SHA-256 was already ingested for the same entity is skipped, so re-running
  the pipeline never duplicates bronze rows.
- Missing required columns fail loudly; extra columns are kept in ``_extra_json``.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa

from yipit_pipeline.config import (
    OPTIONAL_ARTICLE_COLUMNS,
    PIPELINE_VERSION,
    REQUIRED_ARTICLE_COLUMNS,
)
from yipit_pipeline.warehouse import insert_rows, scalar

log = logging.getLogger(__name__)

ENTITY_ARTICLES = "articles"
ENTITY_METADATA = "company_metadata"

_LINEAGE_FIELDS = [
    pa.field("_batch_id", pa.string()),
    pa.field("_source_file", pa.string()),
    pa.field("_file_hash", pa.string()),
    pa.field("_row_number", pa.int32()),
    pa.field("_ingested_at", pa.timestamp("us")),
]
_ARTICLE_COLUMNS = REQUIRED_ARTICLE_COLUMNS + OPTIONAL_ARTICLE_COLUMNS
ARTICLES_SCHEMA = pa.schema(
    _LINEAGE_FIELDS
    + [pa.field(c, pa.string()) for c in _ARTICLE_COLUMNS]
    + [pa.field("_extra_json", pa.string())]
)
METADATA_SCHEMA = pa.schema(
    _LINEAGE_FIELDS + [pa.field("company_name", pa.string()), pa.field("raw_json", pa.string())]
)


class SchemaContractError(ValueError):
    """The source file does not satisfy the expected contract (columns / shape)."""


@dataclass(frozen=True)
class IngestResult:
    entity: str
    source_file: str
    file_hash: str
    batch_id: str | None  # None when the file was already ingested
    rows: int

    @property
    def skipped(self) -> bool:
        return self.batch_id is None


def file_sha256(path: Path) -> str:
    """SHA-256 of the file bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _already_ingested(con: duckdb.DuckDBPyConnection, entity: str, file_hash: str) -> bool:
    return bool(
        scalar(
            con,
            "SELECT count(*) FROM ingest_batch WHERE entity = ? AND file_hash = ?",
            [entity, file_hash],
        )
    )


def _register_batch(
    con: duckdb.DuckDBPyConnection,
    entity: str,
    path: Path,
    file_hash: str,
    row_count: int,
    ingested_at: datetime,
) -> str:
    seq = scalar(con, "SELECT coalesce(max(batch_seq), 0) + 1 FROM ingest_batch")
    batch_id = f"B{seq:04d}"
    con.execute(
        "INSERT INTO ingest_batch VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [batch_id, seq, entity, path.name, file_hash, row_count, ingested_at, PIPELINE_VERSION],
    )
    return batch_id


def read_articles_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """Read the articles CSV as raw strings. Returns (rows, extra column names).

    Raises SchemaContractError if a required column is missing.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_ARTICLE_COLUMNS if c not in header]
        if missing:
            raise SchemaContractError(
                f"{path.name}: missing required column(s) {missing}; found {header}"
            )
        reader.fieldnames = header
        rows = [dict(row) for row in reader]
    extras = [h for h in header if h not in _ARTICLE_COLUMNS]
    return rows, extras


def ingest_articles(con: duckdb.DuckDBPyConnection, path: Path) -> IngestResult:
    """Append the articles file to ``bronze_articles_raw`` (skipped if already ingested)."""
    path = Path(path)
    file_hash = file_sha256(path)
    if _already_ingested(con, ENTITY_ARTICLES, file_hash):
        log.info(
            "ingest articles: %s already ingested (sha256 %s), skipping", path.name, file_hash[:12]
        )
        return IngestResult(ENTITY_ARTICLES, path.name, file_hash, None, 0)

    rows, extras = read_articles_csv(path)
    if extras:
        log.warning("ingest articles: extra column(s) kept in _extra_json: %s", extras)
    ingested_at = datetime.now(UTC).replace(tzinfo=None)
    con.begin()
    try:
        batch_id = _register_batch(con, ENTITY_ARTICLES, path, file_hash, len(rows), ingested_at)
        records = []
        for number, row in enumerate(rows, start=1):
            record = {c: row.get(c) for c in _ARTICLE_COLUMNS}
            extra = {c: row.get(c) for c in extras}
            # Rows longer than the header land under the None key in csv.DictReader.
            if row.get(None):
                extra["_overflow"] = row[None]
            record.update(
                _batch_id=batch_id,
                _source_file=path.name,
                _file_hash=file_hash,
                _row_number=number,
                _ingested_at=ingested_at,
                _extra_json=json.dumps(extra, ensure_ascii=False) if extra else None,
            )
            records.append(record)
        insert_rows(con, "bronze_articles_raw", records, ARTICLES_SCHEMA)
        con.commit()
    except Exception:
        con.rollback()
        raise
    log.info("ingest articles: %s -> batch %s, %d rows", path.name, batch_id, len(rows))
    return IngestResult(ENTITY_ARTICLES, path.name, file_hash, batch_id, len(rows))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise SchemaContractError(f"duplicate JSON key {key!r} in company metadata")
        seen[key] = value
    return seen


def read_metadata_json(path: Path) -> list[tuple[str, dict]]:
    """Read the metadata JSON object ``{company_name: {...}}`` preserving key order.

    Duplicate keys (legal JSON, silently collapsed by most parsers) raise SchemaContractError.
    """
    with path.open(encoding="utf-8-sig") as handle:
        data = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(data, dict):
        raise SchemaContractError(f"{path.name}: expected a JSON object keyed by company name")
    items = list(data.items())
    for name, value in items:
        if not isinstance(value, dict):
            raise SchemaContractError(f"{path.name}: company {name!r} is not a JSON object")
    return items


def ingest_metadata(con: duckdb.DuckDBPyConnection, path: Path) -> IngestResult:
    """Append the company metadata snapshot to ``bronze_company_metadata_raw``."""
    path = Path(path)
    file_hash = file_sha256(path)
    if _already_ingested(con, ENTITY_METADATA, file_hash):
        log.info(
            "ingest metadata: %s already ingested (sha256 %s), skipping", path.name, file_hash[:12]
        )
        return IngestResult(ENTITY_METADATA, path.name, file_hash, None, 0)

    items = read_metadata_json(path)
    ingested_at = datetime.now(UTC).replace(tzinfo=None)
    con.begin()
    try:
        batch_id = _register_batch(con, ENTITY_METADATA, path, file_hash, len(items), ingested_at)
        records = [
            {
                "_batch_id": batch_id,
                "_source_file": path.name,
                "_file_hash": file_hash,
                "_row_number": number,
                "_ingested_at": ingested_at,
                "company_name": name,
                "raw_json": json.dumps(value, ensure_ascii=False),
            }
            for number, (name, value) in enumerate(items, start=1)
        ]
        insert_rows(con, "bronze_company_metadata_raw", records, METADATA_SCHEMA)
        con.commit()
    except Exception:
        con.rollback()
        raise
    log.info("ingest metadata: %s -> batch %s, %d companies", path.name, batch_id, len(items))
    return IngestResult(ENTITY_METADATA, path.name, file_hash, batch_id, len(items))
