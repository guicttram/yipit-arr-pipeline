# Tech-News ARR Pipeline

A local Python pipeline that turns a dirty tech-news dataset into:

1. **ARR observations of companies over time**: every valid `revenue` value is modeled as
   "company X reported ARR Y as of the article's published date", with full lineage back to the
   source file row;
2. a **vector search index** over the articles (`all-MiniLM-L6-v2`), with semantic and hybrid
   (SQL filters + vector similarity) search.

Everything runs in-process on a single **DuckDB** file using a bronze / silver / gold (medallion)
layout. The pipeline is idempotent: re-running it never duplicates records.

| Document | Content |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Data Architecture Document: model, grain and keys, lineage, invalid-record handling, **running beyond a local batch** |
| [docs/decisions.md](docs/decisions.md) | Architecture decision records (ADR-001 … ADR-021) |
| [docs/eda_notes.md](docs/eda_notes.md) | Profiling of the raw inputs (aggregates only) |

## Outputs

`python -m yipit_pipeline run` writes to `outputs/`:

| File | Grain |
|---|---|
| `ai_articles_enriched.csv` | AI/ML article (category **or** company industry), published 2022–2024, valid ARR > US$50M. Required columns in order + `top_similar_articles` |
| `dim_company.csv` | metadata company (descriptive attributes only, no ARR) |
| `dim_company_alias.csv` | name key that resolves to a company |
| `fact_article.csv` | article (company nullable; why it has no ARR observation) |
| `fact_arr_observation.csv` | ARR observation (article with valid ARR + resolved company + valid date) |
| `article_embedding.csv` | article embedding (JSON list of 384 floats) |
| `article_similarity.csv` | (article, rank 1–3) nearest other articles |
| `unmatched_companies.csv` | raw company name not resolved to metadata, with reason |
| `company_match_suggestions.csv` | fuzzy-match proposals for review (never applied automatically) |
| `dq_issues.csv` | detected record-level data-quality problem |
| `dq_check_results.csv` | table-level check (blocking / warning) of the last run |
| `v_company_arr_timeline.csv` | ARR over time per company (one row per observation) |
| `v_company_arr_latest.csv` | latest ARR per company |
| `v_company_arr_quarterly.csv` | ARR per company per quarter (last observation in the quarter) |

The warehouse itself is `data/warehouse/pipeline.duckdb` (optional deliverable).

## System requirements

- Linux, macOS or Windows via WSL2 (developed on Ubuntu 20.04 / WSL2).
- Python **3.11** (the project uses [`uv`](https://docs.astral.sh/uv/) to provision it without
  root; any Python 3.11+ works).
- ~2 GB RAM free; ~1.5 GB disk for the virtual environment (CPU-only PyTorch) and ~90 MB for the
  embedding model.
- Internet access only for installing packages and the **first** model download (cached in
  `~/.cache/huggingface`; later runs load it locally). No GPU needed.
- A full run takes ~15 s on a laptop CPU (most of it loading the model and embedding 750 texts);
  re-runs with unchanged text skip the model entirely.

## Installation

```bash
# 1. Python 3.11 virtual environment
uv venv --python 3.11 .venv            # or: python3.11 -m venv .venv
source .venv/bin/activate

# 2. Dependencies. requirements.txt points at the CPU-only PyTorch index (no CUDA download).
uv pip install -r requirements.txt     # or: pip install -r requirements.txt
uv pip install -e . --no-deps          # makes `python -m yipit_pipeline` importable

# 3. (optional) environment overrides
cp .env.example .env                   # then `set -a; source .env; set +a`
```

Place the raw inputs in `data/raw/` (or point `--data-dir` / `DATA_DIR` elsewhere):

```
data/raw/tech_news.csv
data/raw/company_metadata.json
```

## How to run

```bash
python -m yipit_pipeline run
```

Steps can also be run one by one (each is idempotent):

```bash
python -m yipit_pipeline ingest      # raw files -> bronze (skips files already ingested, by SHA-256)
python -m yipit_pipeline transform   # bronze -> silver (cleaning, company resolution, dq_issues)
python -m yipit_pipeline model       # silver -> gold (dims, facts, views, match suggestions)
python -m yipit_pipeline embed       # embeddings (only new/changed texts) + top-3 similarity
python -m yipit_pipeline dq          # table checks; exit code 1 on a blocking failure
python -m yipit_pipeline export      # all CSVs
```

Global options (before the command) override the environment variables in `.env.example`:

```bash
python -m yipit_pipeline --data-dir /path/to/raw --outputs-dir /tmp/out \
    --warehouse /tmp/wh.duckdb --log-level DEBUG run
```

Ingesting an extra file (e.g. a correction or backfill) appends a new batch; the newest batch wins
per `article_id`:

```bash
python -m yipit_pipeline ingest --articles-file path/to/corrections.csv
python -m yipit_pipeline transform && python -m yipit_pipeline model && \
python -m yipit_pipeline embed && python -m yipit_pipeline dq && python -m yipit_pipeline export
```

### Regenerating all CSVs from scratch

```bash
rm -rf data/warehouse outputs
python -m yipit_pipeline run
```

Re-running without deleting anything gives byte-identical CSVs (no duplicates).

## Example queries

Open the warehouse read-only from Python (or any DuckDB client):

```python
import duckdb

con = duckdb.connect("data/warehouse/pipeline.duckdb", read_only=True)
con.sql("SELECT * FROM v_company_arr_latest ORDER BY latest_arr_usd DESC").show()
```

**ARR of a company over time** (most important):

```sql
SELECT as_of_date, arr_usd, arr_change_usd, date_is_ambiguous, article_id, title
FROM v_company_arr_timeline
WHERE company_name = 'Snowflake'
ORDER BY as_of_date;
```

**Latest ARR per company** (latest date, then newest batch, then highest `article_id`):

```sql
SELECT company_name, latest_arr_usd, latest_as_of_date, source_article_id, n_observations
FROM v_company_arr_latest
ORDER BY latest_arr_usd DESC;
```

**Quarterly ARR** (last observation in each quarter, with the quarter's spread):

```sql
SELECT quarter, quarter_arr_usd, n_observations, min_arr_usd, max_arr_usd
FROM v_company_arr_quarterly
WHERE company_name = 'Snowflake'
ORDER BY quarter;
```

**Source article of an ARR observation** (down to the raw file row):

```sql
SELECT o.observation_id, o.arr_usd, o.arr_raw, o.parse_method, o.currency_detected,
       a.title, a.url, o.source_file, o.source_row_number
FROM fact_arr_observation o
JOIN fact_article a USING (article_id)
WHERE o.observation_id = (SELECT observation_id FROM v_company_arr_latest
                          WHERE company_name = 'Snowflake');
```

**Filter articles by date, category, industry and ARR threshold**:

```sql
SELECT article_id, published_date, category, industry, arr_usd, title
FROM v_articles_enriched
WHERE published_date BETWEEN DATE '2023-01-01' AND DATE '2023-12-31'
  AND category = 'CLOUD'               -- taxonomy bucket (raw label in category_raw)
  AND industry_bucket = 'ANALYTICS'    -- company industry, same taxonomy
  AND arr_usd >= 1000000000
ORDER BY arr_usd DESC;
```

**Why an article has no ARR observation**:

```sql
SELECT arr_excluded_reason, count(*) FROM fact_article GROUP BY ALL;
SELECT reason, severity, count(*) FROM dq_issues GROUP BY ALL ORDER BY 3 DESC;
```

## Semantic and hybrid search

CLI (filters are optional and combine with AND; `--category` / `--industry` accept raw labels
such as `"Machine Learning"` or bucket names such as `AI_ML`, and are repeatable):

```bash
python -m yipit_pipeline search "autonomous driving safety" --top-k 5
python -m yipit_pipeline search "fraud detection for banks" --category FinTech \
    --date-from 2023-01-01 --date-to 2024-12-31 --min-arr 1000000000
python -m yipit_pipeline search "GPU supply for model training" --ai-only
```

Python API:

```python
import duckdb
from yipit_pipeline.embeddings import SentenceTransformerEmbedder
from yipit_pipeline.search import SearchFilters, SemanticSearch, find_similar_articles

# Convenience function: [(article_id, cosine_score), ...]
find_similar_articles("large language model funding", top_k=5)

con = duckdb.connect("data/warehouse/pipeline.duckdb", read_only=True)
search = SemanticSearch(con, SentenceTransformerEmbedder("sentence-transformers/all-MiniLM-L6-v2"))
search.hybrid_search(
    "cloud data platform growth",
    SearchFilters(categories=("Cloud",), industries=("Data Analytics",), min_arr_usd=500_000_000),
    top_k=5,
)
```

Hybrid search runs as one DuckDB query: SQL filters on `v_articles_enriched`, ranking with
`array_cosine_similarity` on the `FLOAT[384]` embedding column.

## Tests

```bash
pytest                                   # fast suite (fake embedder, synthetic fixtures)
pytest -m slow                           # also loads the real all-MiniLM-L6-v2 model
pytest --cov=yipit_pipeline --cov-report=term-missing
ruff check . && ruff format --check .
```

Test data in `tests/fixtures/` is small, synthetic and hand-written (invented companies). The suite
covers every revenue pattern found in the data, every date format and the ambiguity rule, the
category taxonomy, company resolution (exact / normalized / alias / product / multi-entity /
absent, key collisions, fuzzy suggestions never assigning), size boundaries 9,999 / 10,000 /
30,000 / 30,001, negative company age, export boundaries (ARR exactly $50M, 2021-12-31,
2022-01-01, 2024-12-31, 2025-01-01, AI only by category, AI only by industry, unresolved company),
idempotent re-runs (identical table hashes and files), correction batches, schema contract
violations and schema version changes, DQ blocking behavior and search. The cleaners have 100%
line coverage.

## Project layout

```
src/yipit_pipeline/
  config.py            settings (CLI > env > defaults) and business constants (FX, thresholds)
  cleaning/arr.py      revenue -> integer USD ARR (pure)
  cleaning/dates.py    published_date -> date (pure)
  cleaning/categories.py  category/industry taxonomy (pure)
  company.py           company name key, alias table, resolver, fuzzy suggestions, size/age
  warehouse.py         DuckDB connection, schema versioning, helpers
  ingest.py            raw files -> bronze (file hash dedup, schema contract)
  transform.py         bronze -> silver (+ dq_issues)
  model.py             silver -> gold (+ views)
  embeddings.py        embedding cache + top-3 similarity
  search.py            semantic / hybrid search
  dq.py                record issues + blocking/warning table checks
  export.py            CSV exports
  pipeline.py, cli.py  orchestration and command line
  sql/ddl.sql          warehouse schema (grain and keys documented per table)
  sql/views.sql        ARR timeline / latest / quarterly, enriched articles
tests/                 unit + integration tests, synthetic fixtures
docs/                  architecture, decisions (ADRs), EDA notes
```

Raw data, the warehouse and generated CSVs are git-ignored by design.
