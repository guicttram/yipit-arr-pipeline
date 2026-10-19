"""Command line interface: ``python -m yipit_pipeline <command>``.

Commands: run (all steps), ingest, transform, model, embed, dq, export, search.
Global options override the environment variables documented in ``.env.example``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

from yipit_pipeline.config import Settings
from yipit_pipeline.dq import blocking_failures
from yipit_pipeline.embeddings import SentenceTransformerEmbedder
from yipit_pipeline.pipeline import (
    DataQualityError,
    run_all,
    step_dq,
    step_embed,
    step_export,
    step_ingest,
    step_model,
    step_transform,
)
from yipit_pipeline.search import SearchFilters, SemanticSearch
from yipit_pipeline.warehouse import connect

log = logging.getLogger("yipit_pipeline")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yipit_pipeline", description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="directory with the raw input files")
    parser.add_argument("--outputs-dir", type=Path, help="directory for the CSV exports")
    parser.add_argument("--warehouse", type=Path, help="DuckDB warehouse file")
    parser.add_argument("--model", help="sentence-transformers model name")
    parser.add_argument("--log-level", help="DEBUG, INFO, WARNING, ...")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="ingest -> transform -> model -> embed -> dq -> export")
    ingest = sub.add_parser("ingest", help="append raw files to bronze (skips known files)")
    ingest.add_argument("--articles-file", type=Path, help="articles CSV (default: data dir)")
    ingest.add_argument("--metadata-file", type=Path, help="metadata JSON (default: data dir)")
    sub.add_parser("transform", help="rebuild silver from bronze")
    sub.add_parser("model", help="rebuild gold from silver")
    sub.add_parser("embed", help="refresh embeddings and the similarity table")
    sub.add_parser("dq", help="run data-quality checks (exit 1 on blocking failure)")
    sub.add_parser("export", help="write all CSVs")

    search = sub.add_parser("search", help="semantic / hybrid search")
    search.add_argument("query", help="free-text query")
    search.add_argument("--top-k", type=int, default=5)
    search.add_argument("--date-from", type=date.fromisoformat)
    search.add_argument("--date-to", type=date.fromisoformat)
    search.add_argument("--category", action="append", default=[], help="repeatable")
    search.add_argument("--industry", action="append", default=[], help="repeatable")
    search.add_argument("--min-arr", type=int, help="minimum ARR in USD")
    search.add_argument("--ai-only", action="store_true", help="category OR industry is AI/ML")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings()
    overrides = {
        "data_dir": args.data_dir,
        "outputs_dir": args.outputs_dir,
        "warehouse_path": args.warehouse,
        "embedding_model": args.model,
        "log_level": args.log_level,
    }
    return replace(settings, **{k: v for k, v in overrides.items() if v is not None})


def _print_search(rows: list[dict]) -> None:
    if not rows:
        print("no results")
        return
    for row in rows:
        arr = f"${row['arr_usd']:,}" if row["arr_usd"] is not None else "-"
        print(
            f"{row['score']:.4f}  {row['article_id']}  {row['published_date']}  "
            f"{row['category'] or '-':<14} {row['company_name'] or '-':<22} ARR {arr:<18} "
            f"{row['title']}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = _settings(args)
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "huggingface_hub", "sentence_transformers", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    read_only = args.command == "search"
    con = connect(settings.warehouse_path, read_only=read_only)
    try:
        embedder = SentenceTransformerEmbedder(settings.embedding_model)
        if args.command == "run":
            counts = run_all(con, settings, lambda: embedder)
            log.info("run complete: %d files written to %s", len(counts), settings.outputs_dir)
        elif args.command == "ingest":
            step_ingest(con, settings, args.articles_file, args.metadata_file)
        elif args.command == "transform":
            step_transform(con)
        elif args.command == "model":
            step_model(con)
        elif args.command == "embed":
            step_embed(con, embedder)
        elif args.command == "dq":
            if blocking_failures(step_dq(con)):
                return 1
        elif args.command == "export":
            step_export(con, settings)
        elif args.command == "search":
            filters = SearchFilters(
                date_from=args.date_from,
                date_to=args.date_to,
                categories=tuple(args.category),
                industries=tuple(args.industry),
                min_arr_usd=args.min_arr,
                ai_only=args.ai_only,
            )
            _print_search(
                SemanticSearch(con, embedder).hybrid_search(args.query, filters, args.top_k)
            )
    except DataQualityError as exc:
        log.error("%s", exc)
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
