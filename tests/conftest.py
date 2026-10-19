"""Shared fixtures. All data used by tests is synthetic (tests/fixtures/)."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import numpy as np
import pytest

from yipit_pipeline.config import EMBEDDING_DIM, Settings
from yipit_pipeline.pipeline import run_all
from yipit_pipeline.warehouse import connect

FIXTURES = Path(__file__).parent / "fixtures"


class FakeEmbedder:
    """Deterministic bag-of-words embedder: shared words => higher cosine. No model download."""

    model_name = "fake-bow"
    dimension = EMBEDDING_DIM

    def __init__(self) -> None:
        self.calls = 0
        self.texts_encoded = 0

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        self.texts_encoded += len(texts)
        out = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                slot = int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimension
                out[i, slot] += 1.0
            norm = np.linalg.norm(out[i])
            if norm == 0:
                out[i, 0] = 1.0
                norm = 1.0
            out[i] /= norm
        return out


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A raw-data directory holding copies of the synthetic fixtures."""
    target = tmp_path / "raw"
    target.mkdir()
    shutil.copy(FIXTURES / "tech_news_sample.csv", target / "tech_news.csv")
    shutil.copy(FIXTURES / "company_metadata_sample.json", target / "company_metadata.json")
    return target


@pytest.fixture
def settings(tmp_path: Path, data_dir: Path) -> Settings:
    return Settings(
        data_dir=data_dir,
        outputs_dir=tmp_path / "outputs",
        warehouse_path=tmp_path / "warehouse" / "test.duckdb",
        embedding_model="fake-bow",
        log_level="WARNING",
    )


@pytest.fixture
def con(settings: Settings):
    connection = connect(settings.warehouse_path)
    yield connection
    connection.close()


@pytest.fixture
def built(con, settings: Settings, fake_embedder: FakeEmbedder):
    """Warehouse after one full pipeline run on the fixtures."""
    run_all(con, settings, lambda: fake_embedder)
    return con


def table_hash(con, table: str) -> str:
    """Order-independent content hash of a table or view."""
    return con.execute(
        f"SELECT md5(coalesce(string_agg(t::VARCHAR, '|' ORDER BY t::VARCHAR), '')) FROM {table} t"
    ).fetchone()[0]
