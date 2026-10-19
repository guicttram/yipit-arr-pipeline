"""Semantic and hybrid search over the article embeddings stored in DuckDB.

``find_similar_articles`` ranks every article by cosine similarity to the query.
``hybrid_search`` applies SQL filters (date range, category, industry, ARR threshold,
AI-related) first and ranks the remaining articles by similarity, in a single query using
DuckDB's ``array_cosine_similarity`` on ``FLOAT[n]`` columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb

from yipit_pipeline.cleaning.categories import map_category
from yipit_pipeline.embeddings import Embedder


@dataclass(frozen=True)
class SearchFilters:
    """Structured filters for ``hybrid_search``. ``None``/empty means "no filter".

    ``categories`` / ``industries`` accept raw labels or bucket names (``AI/ML``,
    ``Machine Learning`` and ``AI_ML`` all mean the same bucket).
    """

    date_from: date | None = None
    date_to: date | None = None
    categories: tuple[str, ...] = field(default_factory=tuple)
    industries: tuple[str, ...] = field(default_factory=tuple)
    min_arr_usd: int | None = None
    ai_only: bool = False


def _bucket(label: str) -> str:
    """Accept either a bucket name (AI_ML) or any raw label that maps to one."""
    mapped = map_category(label)
    return mapped.bucket if mapped.status == "mapped" else label.strip().upper()


class SemanticSearch:
    """Search entry point bound to a warehouse connection and an embedder."""

    def __init__(self, con: duckdb.DuckDBPyConnection, embedder: Embedder) -> None:
        self.con = con
        self.embedder = embedder

    def _query_vector(self, query_text: str) -> list[float]:
        return self.embedder.encode([query_text])[0].tolist()

    def _dim(self) -> int:
        data_type = self.con.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'article_embedding' AND column_name = 'embedding'"
        ).fetchone()[0]
        return int(data_type.split("[")[1].rstrip("]"))

    def find_similar_articles(self, query_text: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Return ``[(article_id, cosine_score), ...]`` for the ``top_k`` closest articles."""
        return [
            (row["article_id"], row["score"])
            for row in self.hybrid_search(query_text, SearchFilters(), top_k)
        ]

    def hybrid_search(
        self, query_text: str, filters: SearchFilters | None = None, top_k: int = 5
    ) -> list[dict]:
        """SQL filters + vector similarity. Returns dict rows ordered by score desc."""
        filters = filters or SearchFilters()
        where, params = ["e.embedding IS NOT NULL"], []
        if filters.date_from:
            where.append("a.published_date >= ?")
            params.append(filters.date_from)
        if filters.date_to:
            where.append("a.published_date <= ?")
            params.append(filters.date_to)
        if filters.categories:
            where.append("a.category IN (SELECT unnest(?::VARCHAR[]))")
            params.append([_bucket(c) for c in filters.categories])
        if filters.industries:
            where.append("a.industry_bucket IN (SELECT unnest(?::VARCHAR[]))")
            params.append([_bucket(i) for i in filters.industries])
        if filters.min_arr_usd is not None:
            where.append("a.arr_usd >= ?")
            params.append(filters.min_arr_usd)
        if filters.ai_only:
            where.append("a.is_ai_related")

        sql = f"""
            SELECT a.article_id, a.title, a.company_name, a.published_date, a.category,
                   a.industry, a.arr_usd, a.url,
                   round(array_cosine_similarity(e.embedding, ?::FLOAT[{self._dim()}]), 6) AS score
            FROM v_articles_enriched a
            JOIN article_embedding e ON e.article_id = a.article_id
            WHERE {" AND ".join(where)}
            ORDER BY score DESC, a.article_id
            LIMIT ?
        """
        cursor = self.con.execute(sql, [self._query_vector(query_text), *params, top_k])
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, values, strict=True)) for values in cursor.fetchall()]


def find_similar_articles(
    query_text: str, top_k: int = 5, settings=None
) -> list[tuple[str, float]]:
    """Convenience wrapper: open the configured warehouse read-only and search it."""
    from yipit_pipeline.config import Settings
    from yipit_pipeline.embeddings import SentenceTransformerEmbedder
    from yipit_pipeline.warehouse import connect

    settings = settings or Settings()
    con = connect(settings.warehouse_path, read_only=True)
    try:
        search = SemanticSearch(con, SentenceTransformerEmbedder(settings.embedding_model))
        return search.find_similar_articles(query_text, top_k)
    finally:
        con.close()
