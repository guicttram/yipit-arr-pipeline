"""Embeddings, similarity table and search. Fast tests use the fake embedder; the real
model test is marked slow (run with ``pytest -m slow``)."""

from datetime import date

import numpy as np
import pytest

from yipit_pipeline.config import DEFAULT_EMBEDDING_MODEL
from yipit_pipeline.embeddings import article_text, top_k_neighbours
from yipit_pipeline.search import SearchFilters, SemanticSearch


def test_article_text_handles_missing_parts():
    assert article_text("Title", None) == "Title"
    assert article_text(None, None) == ""


def test_top_k_excludes_self_and_breaks_ties_by_id():
    ids = ["A", "B", "C", "D"]
    vectors = np.array([[1, 0], [1, 0], [1, 0], [0, 1]], dtype=np.float32)
    rows = top_k_neighbours(ids, vectors, k=2)
    first = [r for r in rows if r[0] == "A"]
    assert [r[2] for r in first] == ["B", "C"]  # equal scores -> article_id ascending
    assert all(r[0] != r[2] for r in rows)


def test_top_k_with_fewer_articles_than_k():
    rows = top_k_neighbours(["A", "B"], np.eye(2, dtype=np.float32), k=3)
    assert [(r[0], r[2]) for r in rows] == [("A", "B"), ("B", "A")]


def test_similarity_table(built):
    assert built.execute("SELECT count(*) FROM article_similarity").fetchone()[0] == 16 * 3
    assert (
        built.execute(
            "SELECT count(*) FROM article_similarity WHERE article_id = similar_article_id"
        ).fetchone()[0]
        == 0
    )


def test_article_is_most_similar_to_itself(built, fake_embedder):
    search = SemanticSearch(built, fake_embedder)
    text = built.execute(
        "SELECT title || ' ' || summary FROM fact_article WHERE article_id = 'T008'"
    ).fetchone()[0]
    top = search.find_similar_articles(text, top_k=3)
    assert top[0][0] == "T008"
    assert top[0][1] == pytest.approx(1.0, abs=1e-5)
    assert [s for _, s in top] == sorted((s for _, s in top), reverse=True)


def test_hybrid_search_applies_sql_filters(built, fake_embedder):
    search = SemanticSearch(built, fake_embedder)
    rows = search.hybrid_search(
        "robot warehouse",
        SearchFilters(
            date_from=date(2022, 1, 1),
            date_to=date(2024, 12, 31),
            categories=("Machine Learning",),  # raw label mapped to the AI_ML bucket
            min_arr_usd=60_000_000,
        ),
        top_k=10,
    )
    ids = [r["article_id"] for r in rows]
    assert ids[0] == "T001"
    assert set(ids) <= {"T001", "T006", "T016"}
    assert all(r["arr_usd"] >= 60_000_000 for r in rows)


def test_hybrid_search_industry_and_ai_only(built, fake_embedder):
    search = SemanticSearch(built, fake_embedder)
    by_industry = search.hybrid_search("banks", SearchFilters(industries=("FinTech",)), top_k=10)
    assert {r["article_id"] for r in by_industry} == {"T008", "T009", "T010"}
    ai = search.hybrid_search("anything", SearchFilters(ai_only=True), top_k=50)
    assert "T011" not in {r["article_id"] for r in ai}
    assert "T002" in {r["article_id"] for r in ai}  # AI by industry only


@pytest.mark.slow
def test_real_model_end_to_end(con, settings):
    """Loads all-MiniLM-L6-v2 (cached after the first download)."""
    from dataclasses import replace

    from yipit_pipeline.embeddings import SentenceTransformerEmbedder
    from yipit_pipeline.pipeline import run_all

    settings = replace(settings, embedding_model=DEFAULT_EMBEDDING_MODEL)
    embedder = SentenceTransformerEmbedder(DEFAULT_EMBEDDING_MODEL)
    run_all(con, settings, lambda: embedder)
    top = SemanticSearch(con, embedder).find_similar_articles("payments for small banks", top_k=3)
    assert top[0][0] == "T008"
