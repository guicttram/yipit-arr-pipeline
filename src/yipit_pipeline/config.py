"""Runtime settings and business constants.

Settings come from (highest priority first): CLI flags, environment variables, defaults.
Business rules (FX rates, export filter, size thresholds) are constants here so they are
versioned with the code and covered by tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

PIPELINE_VERSION = "1.0.0"

# Bump when the shape of any silver/gold table changes. Derived layers are dropped and
# rebuilt from bronze on the next run; bronze is never touched (see ensure_schema).
DERIVED_SCHEMA_VERSION = "3"

ARTICLES_FILE_NAME = "tech_news.csv"
METADATA_FILE_NAME = "company_metadata.json"

# Columns the articles file must contain. Extra columns are kept in bronze `_extra_json`.
REQUIRED_ARTICLE_COLUMNS = (
    "article_id",
    "title",
    "company_name",
    "published_date",
    "category",
    "revenue",
    "summary",
    "url",
)
OPTIONAL_ARTICLE_COLUMNS = ("author", "word_count")

METADATA_FIELDS = (
    "founded_year",
    "headquarters",
    "employee_count",
    "industry",
    "is_public",
    "stock_ticker",
)

# FX conversion to USD, as given by the assignment. EUR/GBP multiply, JPY divides.
FX_TO_USD: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.1"),
    "GBP": Decimal("1.27"),
    "JPY": Decimal("1") / Decimal("150"),
}

# Company size thresholds (employee_count): Small < 10,000; Medium 10,000-30,000; Large > 30,000.
SIZE_MEDIUM_MIN = 10_000
SIZE_MEDIUM_MAX = 30_000

# ai_articles_enriched.csv filter.
AI_BUCKET = "AI_ML"
EXPORT_DATE_FROM = date(2022, 1, 1)
EXPORT_DATE_TO = date(2024, 12, 31)
EXPORT_MIN_ARR_USD = 50_000_000  # strictly greater than

# ARR outside this range is kept but flagged as a data-quality warning.
ARR_WARN_MIN_USD = 100_000
ARR_WARN_MAX_USD = 1_000_000_000_000

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SIMILAR_TOP_N = 3
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default)


@dataclass(frozen=True)
class Settings:
    """Paths and runtime options for one pipeline invocation."""

    data_dir: Path = field(default_factory=lambda: _env_path("DATA_DIR", "data/raw"))
    outputs_dir: Path = field(default_factory=lambda: _env_path("OUTPUTS_DIR", "outputs"))
    warehouse_path: Path = field(
        default_factory=lambda: _env_path("WAREHOUSE_PATH", "data/warehouse/pipeline.duckdb")
    )
    embedding_model: str = field(
        default_factory=lambda: os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL
    )
    log_level: str = field(default_factory=lambda: os.environ.get("LOG_LEVEL") or "INFO")

    @property
    def articles_path(self) -> Path:
        return self.data_dir / ARTICLES_FILE_NAME

    @property
    def metadata_path(self) -> Path:
        return self.data_dir / METADATA_FILE_NAME
