"""Step orchestration shared by the CLI and the integration tests."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import duckdb

from yipit_pipeline.config import Settings
from yipit_pipeline.dq import blocking_failures, issue_summary, run_checks
from yipit_pipeline.embeddings import Embedder, SentenceTransformerEmbedder, run_embed
from yipit_pipeline.export import run_export
from yipit_pipeline.ingest import ingest_articles, ingest_metadata
from yipit_pipeline.model import run_model
from yipit_pipeline.transform import run_transform

log = logging.getLogger(__name__)


class DataQualityError(RuntimeError):
    """A blocking data-quality check failed; exports are not written."""


@contextmanager
def timed(step: str):
    start = time.perf_counter()
    log.info("step %s: start", step)
    yield
    log.info("step %s: done in %.2fs", step, time.perf_counter() - start)


def step_ingest(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    articles_path: Path | None = None,
    metadata_path: Path | None = None,
):
    with timed("ingest"):
        return (
            ingest_metadata(con, metadata_path or settings.metadata_path),
            ingest_articles(con, articles_path or settings.articles_path),
        )


def step_transform(con: duckdb.DuckDBPyConnection):
    with timed("transform"):
        return run_transform(con)


def step_model(con: duckdb.DuckDBPyConnection):
    with timed("model"):
        return run_model(con)


def step_embed(con: duckdb.DuckDBPyConnection, embedder: Embedder):
    with timed("embed"):
        return run_embed(con, embedder)


def step_dq(con: duckdb.DuckDBPyConnection):
    with timed("dq"):
        results = run_checks(con)
        for severity, reason, n in issue_summary(con):
            log.info("dq issues: %-8s %-40s %d", severity, reason, n)
        return results


def step_export(con: duckdb.DuckDBPyConnection, settings: Settings):
    with timed("export"):
        return run_export(con, settings.outputs_dir)


def run_all(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    embedder_factory: Callable[[], Embedder] | None = None,
) -> dict[str, int]:
    """ingest -> transform -> model -> embed -> dq -> export. Raises DataQualityError (and
    writes no CSV) if a blocking check fails."""
    factory = embedder_factory or (lambda: SentenceTransformerEmbedder(settings.embedding_model))
    step_ingest(con, settings)
    step_transform(con)
    step_model(con)
    step_embed(con, factory())
    failures = blocking_failures(step_dq(con))
    if failures:
        names = ", ".join(f.check_name for f in failures)
        raise DataQualityError(f"blocking DQ checks failed: {names}")
    return step_export(con, settings)
