"""Article embeddings (title + summary) and the top-3 similarity table.

- Text = ``title + " " + summary`` (missing parts become empty strings).
- Vectors are L2-normalized, so cosine similarity equals the dot product.
- ``article_embedding`` is a cache: an article is re-embedded only when its ``text_hash``
  or the model changes; rows for articles that no longer exist are removed.
- ``article_similarity`` is recomputed from all cached vectors on every run (N x N, numpy):
  the diagonal is excluded, ties are broken by ``article_id`` ascending.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

import duckdb
import numpy as np
import pyarrow as pa

from yipit_pipeline.config import SIMILAR_TOP_N
from yipit_pipeline.warehouse import create_views, insert_rows

log = logging.getLogger(__name__)


class Embedder(Protocol):
    """Anything that turns texts into L2-normalized float32 vectors."""

    model_name: str

    @property
    def dimension(self) -> int: ...

    def encode(self, texts: list[str]) -> np.ndarray: ...


class SentenceTransformerEmbedder:
    """Lazy wrapper around ``sentence_transformers.SentenceTransformer`` (CPU)."""

    def __init__(self, model_name: str, batch_size: int = 64) -> None:
        self.model_name = model_name
        self.batch_size = batch_size
        self._model = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # heavy import, load lazily

            log.info("loading embedding model %s", self.model_name)
            try:  # use the local cache first: no network after the first download
                self._model = SentenceTransformer(
                    self.model_name, device="cpu", local_files_only=True
                )
            except OSError:
                log.info("model not cached locally, downloading %s", self.model_name)
                self._model = SentenceTransformer(self.model_name, device="cpu")
        return self._model

    @property
    def dimension(self) -> int:
        model = self._load()
        getter = getattr(model, "get_embedding_dimension", None) or getattr(
            model, "get_sentence_embedding_dimension"
        )
        return int(getter())

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = self._load().encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return np.asarray(vectors, dtype=np.float32)


def article_text(title: str | None, summary: str | None) -> str:
    """Text that is embedded for an article."""
    return f"{title or ''} {summary or ''}".strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmbedStats:
    articles: int
    embedded: int
    reused: int
    removed: int
    similarity_rows: int


def _embedding_dim(con: duckdb.DuckDBPyConnection) -> int:
    data_type = con.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = 'article_embedding' AND column_name = 'embedding'"
    ).fetchone()[0]
    return int(data_type.split("[")[1].rstrip("]"))


def _recreate_embedding_table(con: duckdb.DuckDBPyConnection, dim: int) -> None:
    log.warning("embedding dimension changed to %d: recreating article_embedding", dim)
    con.execute("DROP VIEW IF EXISTS v_articles_enriched")
    con.execute("DROP TABLE IF EXISTS article_embedding")
    con.execute(
        f"CREATE TABLE article_embedding (article_id VARCHAR, embedding FLOAT[{dim}], "
        "model_name VARCHAR, text_hash VARCHAR)"
    )


def top_k_neighbours(
    ids: list[str], vectors: np.ndarray, k: int = SIMILAR_TOP_N
) -> list[tuple[str, int, str, float]]:
    """(article_id, rank, similar_article_id, score) for the k most similar other articles.

    ``ids`` must be sorted ascending; ties on score are then broken by ``article_id`` ascending.
    """
    n = len(ids)
    if n < 2:
        return []
    scores = vectors.astype(np.float64) @ vectors.astype(np.float64).T
    np.fill_diagonal(scores, -np.inf)
    k = min(k, n - 1)
    order = np.arange(n)
    rows = []
    for i, article_id in enumerate(ids):
        # Stable sort on -score keeps index (= article_id) order among equal scores.
        rounded = np.round(scores[i], 6)
        ranked = np.lexsort((order, -rounded))[:k]
        for rank, j in enumerate(ranked, start=1):
            rows.append((article_id, rank, ids[j], float(rounded[j])))
    return rows


def run_embed(con: duckdb.DuckDBPyConnection, embedder: Embedder) -> EmbedStats:
    """Refresh the embedding cache for all articles and rebuild ``article_similarity``."""
    articles = con.execute(
        "SELECT article_id, title, summary FROM fact_article ORDER BY article_id"
    ).fetchall()
    wanted = {aid: text_hash(article_text(t, s)) for aid, t, s in articles}
    texts = {aid: article_text(t, s) for aid, t, s in articles}
    cached = {
        aid: (model, h)
        for aid, model, h in con.execute(
            "SELECT article_id, model_name, text_hash FROM article_embedding"
        ).fetchall()
    }
    stale = [aid for aid in cached if aid not in wanted]
    todo = [aid for aid in sorted(wanted) if cached.get(aid) != (embedder.model_name, wanted[aid])]

    con.begin()
    try:
        if todo and embedder.dimension != _embedding_dim(con):
            _recreate_embedding_table(con, embedder.dimension)
            todo = sorted(wanted)
            stale = []
        if todo:
            log.info("embed: encoding %d article(s) with %s", len(todo), embedder.model_name)
            vectors = embedder.encode([texts[aid] for aid in todo])
            dim = vectors.shape[1]
            con.execute(
                "DELETE FROM article_embedding WHERE article_id IN (SELECT unnest(?::VARCHAR[]))",
                [todo],
            )
            insert_rows(
                con,
                "article_embedding",
                [
                    {
                        "article_id": aid,
                        "embedding": vectors[i].tolist(),
                        "model_name": embedder.model_name,
                        "text_hash": wanted[aid],
                    }
                    for i, aid in enumerate(todo)
                ],
                pa.schema(
                    [
                        ("article_id", pa.string()),
                        ("embedding", pa.list_(pa.float32(), dim)),
                        ("model_name", pa.string()),
                        ("text_hash", pa.string()),
                    ]
                ),
            )
        if stale:
            con.execute(
                "DELETE FROM article_embedding WHERE article_id IN (SELECT unnest(?::VARCHAR[]))",
                [stale],
            )

        ids, vectors = _load_vectors(con)
        neighbours = top_k_neighbours(ids, vectors)
        con.execute("DELETE FROM article_similarity")
        insert_rows(
            con,
            "article_similarity",
            [
                {
                    "article_id": a,
                    "rank": r,
                    "similar_article_id": b,
                    "score": s,
                    "model_name": embedder.model_name,
                }
                for a, r, b, s in neighbours
            ],
            pa.schema(
                [
                    ("article_id", pa.string()),
                    ("rank", pa.int32()),
                    ("similar_article_id", pa.string()),
                    ("score", pa.float64()),
                    ("model_name", pa.string()),
                ]
            ),
        )
        create_views(con)
        con.commit()
    except Exception:
        con.rollback()
        raise

    stats = EmbedStats(
        articles=len(wanted),
        embedded=len(todo),
        reused=len(wanted) - len(todo),
        removed=len(stale),
        similarity_rows=len(neighbours),
    )
    log.info("embed: %s", stats)
    return stats


def _load_vectors(con: duckdb.DuckDBPyConnection) -> tuple[list[str], np.ndarray]:
    rows = con.execute(
        "SELECT article_id, embedding FROM article_embedding ORDER BY article_id"
    ).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    ids = [r[0] for r in rows]
    vectors = np.asarray([r[1] for r in rows], dtype=np.float32)
    return ids, vectors
