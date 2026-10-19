# Data Architecture

This document describes the data model, how every output traces back to its source, how dirty
records are handled, and how the same design runs reliably beyond a local batch.
Design decisions and their alternatives are in [decisions.md](decisions.md); data facts in
[eda_notes.md](eda_notes.md).

## 1. Overview

The pipeline turns a dirty tech-news file into (a) **ARR observations of companies over time**
and (b) a **vector search index**, inside one DuckDB file.

```
data/raw/  tech_news.csv, company_metadata.json
   │  ingest   (file skipped if its SHA-256 is already in ingest_batch)
   ▼
BRONZE   bronze_articles_raw, bronze_company_metadata_raw, ingest_batch
   │     raw strings + lineage columns, append-only
   │  transform   (pure cleaners + company resolution)
   ▼
SILVER   silver_articles, silver_company, dq_issues
   │     one row per entity, typed, *_raw original + status per field
   │  model
   ▼
GOLD     dim_company, dim_company_alias, fact_article, fact_arr_observation,
   │     unmatched_companies, company_match_suggestions
   │     views: v_company_arr_timeline / _latest / _quarterly, v_articles_enriched
   │  embed
   ▼
SEARCH   article_embedding (FLOAT[384], cached by text hash), article_similarity (top-3)
   │  dq   (blocking checks gate the export → dq_check_results)
   ▼
outputs/ ai_articles_enriched.csv + one CSV per modeled table / view
```

| Layer | Rule | Rebuilt? |
|---|---|---|
| Bronze | Raw strings + lineage columns. Append-only. A file whose SHA-256 is already in `ingest_batch` is skipped. | Never |
| Silver | One row per entity, typed, every cleaned field next to its `*_raw` original and a status. | Fully, from bronze, in one transaction |
| Gold | Dimensions, facts, match outputs; analytical views. | Fully, from silver, in one transaction |
| Embeddings | Cache keyed by `article_id`, invalidated by `text_hash` or model change. | Incrementally |

Code map: `ingest.py` (bronze), `cleaning/` (pure parsers), `company.py` (name resolution),
`transform.py` (silver), `model.py` + `sql/views.sql` (gold), `embeddings.py` / `search.py`,
`dq.py`, `export.py`, `cli.py`. Schema: `sql/ddl.sql`.

## 2. Tables: grain and keys

| Table | Grain (one row per …) | Key | Notes |
|---|---|---|---|
| `ingest_batch` | ingested source file | `batch_id` (`B0001`…) | `file_hash` unique per entity |
| `bronze_articles_raw` | file row per batch | `(_batch_id, _row_number)` | all strings; `_extra_json` for unexpected columns |
| `bronze_company_metadata_raw` | company key per batch | `(_batch_id, company_name)` | `raw_json` as received |
| `silver_articles` | article | `article_id` | latest batch wins; parse statuses, match method |
| `silver_company` | metadata company | `company_id` | typed, validated |
| `dq_issues` | detected record problem | `issue_id` | severity `info` / `warning`; owned by a stage |
| `dim_company` | metadata company | `company_id` | descriptive only, **no ARR** |
| `dim_company_alias` | name key resolving to a company | `alias_key` | metadata keys + alias table |
| `fact_article` | article | `article_id` | `company_id` nullable; `arr_observation_id`, `arr_excluded_reason` |
| `fact_arr_observation` | article with valid ARR **and** resolved company **and** valid date | `observation_id` | `arr_usd BIGINT`, `as_of_date`, source batch/file/row |
| `unmatched_companies` | distinct unresolved raw company name | `company_name_raw` | reason + article counts |
| `company_match_suggestions` | distinct unresolved name with a fuzzy candidate | `company_name_raw` | never assigns `company_id` |
| `article_embedding` | article | `article_id` | `FLOAT[384]`, `model_name`, `text_hash` |
| `article_similarity` | (article, rank) | `(article_id, rank)` | top-3 over all articles, self excluded |
| `dq_check_results` | DQ check of the last run | `check_name` | `blocking` / `warning` |

Keys: `article_id` is natural; `company_id` = SHA-256(normalized company key)[:16];
`observation_id` = SHA-256(`article_id`)[:16]. All are deterministic across runs.

### Relationships

```
dim_company 1 ──── 0..n fact_article 1 ──── 0..1 fact_arr_observation n ──── 1 dim_company
     │                       │ 1                                      (same company_id)
     │ 1                     ├──── 1 article_embedding
     └── 0..n dim_company_alias└─── 0..3 article_similarity ──▶ fact_article (similar_article_id)
```

### The ARR observation

The source column is called `revenue`, but each valid value is modeled as an **ARR observation**:
"company X had ARR Y as of the article's published date". It is a *reported* figure from a news
article, not company master data, which is why:

- ARR never appears in `dim_company`; it lives only in `fact_arr_observation`;
- each observation keeps `arr_raw`, `currency_detected`, `parse_method` (single value or range
  midpoint), `date_is_ambiguous` and the exact source row (`source_batch_id`, `source_file`,
  `source_row_number`);
- "latest" and "quarterly" ARR are **views** over observations, so the rule that picks a value is
  visible and changeable.

### Query patterns

| Need | How |
|---|---|
| ARR of a company over time | `v_company_arr_timeline` (one row per observation, with title/URL and change vs previous) |
| Latest ARR | `v_company_arr_latest`: latest `as_of_date`, then newest batch, then highest `article_id` |
| Quarterly ARR | `v_company_arr_quarterly`: last observation in the quarter (same ordering) + count/min/max |
| Source article of an observation | `fact_arr_observation.article_id` → `fact_article` (title, URL) → bronze row |
| Filter by date / category / industry / ARR | `v_articles_enriched` (`published_date`, `category`, `industry_bucket`, `arr_usd`, `is_ai_related`) |
| Hybrid search | `SemanticSearch.hybrid_search`: same filters + `array_cosine_similarity` in one query |
| Re-run without duplicates | file-hash skip in bronze + deterministic rebuild of silver/gold (tested) |

## 3. Lineage

Every exported row can be walked back to a byte range of a specific input file:

```
ai_articles_enriched.csv (article_id, arr_usd)
  └─ v_articles_enriched
       └─ fact_arr_observation (observation_id, arr_raw, parse_method, source_batch_id, source_row_number)
            └─ fact_article (article_id, revenue_raw, arr_parse_status, arr_excluded_reason)
                 └─ silver_articles (*_raw originals + parse statuses + company match method)
                      └─ bronze_articles_raw (_batch_id, _row_number, raw strings)
                           └─ ingest_batch (source_file, file_hash SHA-256, ingested_at, pipeline_version)
```

```sql
-- From an exported observation back to the raw file row
SELECT o.observation_id, o.arr_usd, o.arr_raw, b.revenue AS bronze_revenue,
       i.source_file, b._row_number, i.file_hash
FROM fact_arr_observation o
JOIN bronze_articles_raw b ON b._batch_id = o.source_batch_id AND b._row_number = o.source_row_number
JOIN ingest_batch i ON i.batch_id = b._batch_id
WHERE o.article_id = $article_id;  -- any observed article
```

The reverse question — "why does this article have no ARR observation?" — is answered by
`fact_article.arr_excluded_reason` (`no_valid_arr`, `company_unresolved`, `date_unresolved`)
plus the matching rows in `dq_issues`.

## 4. Invalid and missing records

Records are **kept and flagged**, never silently dropped. A problem removes a record only from the
places where it would be wrong (e.g. the ARR fact), and the reason is recorded.

| Problem | Silver | Gold effect | `dq_issues` reason (severity) |
|---|---|---|---|
| Revenue empty / `NULL` / `N/A` | `arr_parse_status = missing` | no observation | `arr_missing` (info) |
| `Not disclosed` | `undisclosed` | no observation | `arr_undisclosed` (info) |
| Garbage revenue | `unparseable` | no observation | `arr_unparseable` (warning) |
| Revenue ≤ 0 | `invalid_non_positive` | no observation | `arr_invalid_non_positive` (warning) |
| Missing / invalid date | `published_date = NULL` | article kept, no observation | `date_missing` / `date_invalid` (warning) |
| Ambiguous day/month | parsed by rule, `date_is_ambiguous = true` | observation flagged | `date_ambiguous_day_month` (info) |
| Unknown category | `category = UNMAPPED` | kept | `category_unmapped` (warning) |
| Company not in metadata / product / multi-entity | `company_id = NULL`, reason | no observation; listed in `unmatched_companies` | `company_<reason>` (warning) |
| Same article in a newer batch | newer row wins | — | `superseded_by_newer_row` (info) |
| Row without `article_id` | rejected | — | `missing_article_id` (warning) |
| Founded after publication | `company_age = NULL` | — | `negative_company_age` (warning) |
| Public without ticker / private with ticker | kept as given | — | `public_without_ticker` / `private_with_ticker` (warning) |
| Missing required column, duplicate JSON keys, metadata key collision | — | **run fails** | — |

Table-level checks (`dq_check_results`) then assert the invariants: unique keys, required
not-nulls, referential integrity, observations only from valid ARR, bronze = silver + rejected,
embedding coverage, similarity sanity. A **blocking** failure stops the export (exit code 1); a
**warning** is reported (current warnings: 9 metadata inconsistencies, 34 unmatched-company
articles, 93 ambiguous dates).

## 5. Idempotency and re-runs

- **Bronze** registers each file's SHA-256; the same file is never ingested twice.
- **Silver/gold** are rebuilt from bronze inside a transaction per step: a failed step leaves the
  previous state intact; a successful re-run yields the same rows. Derived tables contain no run
  timestamps, so table hashes are identical across runs.
- **Embeddings** are reused when `text_hash` and `model_name` are unchanged (the model is not even
  loaded); similarity is recomputed deterministically (ties by `article_id`).
- **Exports** are written to a temp file, renamed atomically, with a fixed `ORDER BY`: byte-identical
  across runs.
- Tests: `test_rerun_is_idempotent`, `test_same_file_reingested_adds_nothing`,
  `test_correction_batch_supersedes_older_row`.

## 6. Caveats

- **Metadata is a snapshot.** Employee count, size category and public status describe the company
  today and are applied to articles from 2020–2024. Company age uses the article year.
- **Unmatched companies.** 34 articles (Cohere, xAI, Mistral AI, Hugging Face, Perplexity AI, Azure
  as a product, one multi-company string) have no `company_id`. They are searchable and embedded
  but produce no ARR observation and cannot reach the AI export, even when their category is AI.
- **Product vs company.** `Azure` / `Microsoft Azure` are not merged into Microsoft: product ARR is a
  different scope from company ARR (ADR-007).
- **Source inconsistencies kept as given.** `is_public` vs `stock_ticker` disagrees for 9 companies;
  industry labels do not follow real-world intuition (e.g. SpaceX = AI/ML). The pipeline never
  overrides metadata with outside knowledge.
- **Ambiguous dates.** 93 rows depend on the separator rule; the flag travels to the observation.
- **Reported ARR is noisy.** Values for one company swing widely between articles; quarterly views
  expose min/max next to the chosen value.
- **Templated text.** 303 distinct titles and 12 distinct summaries for 750 articles limit how much
  semantic search can discriminate; many nearest neighbours are template duplicates (score 1.0).

## 7. Beyond a local batch

The local design maps one-to-one onto a production stack; the important properties
(immutable raw layer, deterministic rebuilds, explicit contracts, lineage columns) carry over.

**Orchestration.** Each CLI step becomes a task in Airflow / Dagster / Prefect
(`ingest → transform → model → embed → dq → export`), with retries, alerting and SLAs. The `dq`
step's exit code gates publication: consumers only ever see outputs that passed blocking checks.
Dagster's asset model fits particularly well (each table is an asset with checks).

**Storage.** Raw files land in object storage (S3/GCS) under immutable, partitioned paths
(`raw/tech_news/ingest_date=YYYY-MM-DD/<sha256>.csv`). Bronze/silver/gold become Parquet tables
managed by Iceberg or Delta Lake (ACID, time travel, schema evolution), queried by a warehouse
(Snowflake, BigQuery, Databricks) or still by DuckDB for smaller volumes.

**Transformations.** Silver/gold SQL moves to dbt models with tests (`unique`, `not_null`,
`relationships`, accepted values) mirroring `dq.py`, and dbt docs/lineage graphs. Python parsers
(ARR, dates) stay as UDFs or a pre-processing step with the same unit tests.

**Incremental processing.** The full rebuild is the right choice at 750 rows. At scale, silver and
gold become incremental `MERGE`s keyed by `article_id`, processing only new batches, with the
same "latest batch wins" rule expressed as `ORDER BY batch_seq DESC`.

**Backfills.** Because bronze is immutable and keyed by `batch_id`, a backfill is either
(a) ingesting historical files as new batches, or (b) re-running transformations for a range of
batches/partitions after a logic change. Running the same partition twice yields the same result
(idempotent writes by key + partition overwrite). A `pipeline_version` column (already recorded in
`ingest_batch`) should also be stamped on derived rows so mixed-version partitions are detectable.

**Late data and source corrections.** A corrected article arrives as a newer batch and supersedes
the older row (already implemented and tested). Deletions need explicit tombstones from the
source. Late-arriving articles simply add observations; "latest ARR" is recomputed by the view.

**Schema changes.** Inputs are validated against a contract (required columns, types) at
ingestion: additive changes are absorbed (`_extra_json` today; promoted to real columns
deliberately), breaking changes fail before writing. Derived schemas are versioned
(`DERIVED_SCHEMA_VERSION`); in a lakehouse, Iceberg/Delta schema evolution plus a schema registry
and drift alerts (new category labels → `UNMAPPED` rate, new revenue patterns → `unparseable`
rate, new company names → pending match suggestions) replace the local mechanism.

**Data quality.** Blocking vs warning severities map to orchestrator behavior (fail the run vs
open a ticket / post an alert). DQ issue counts per batch become time series with thresholds (e.g.
unmatched rate > 10%). Great Expectations / Soda / dbt tests are the off-the-shelf equivalents.

**Company master data.** Metadata becomes an SCD Type 2 dimension (`valid_from`, `valid_to`) so
size and public status are joined as of the article date. The alias table and fuzzy-suggestion
review move to a small stewardship workflow (suggestions reviewed in a UI, approvals written to
the versioned alias table). If articles naming several companies become common, an
article-to-company bridge table replaces the single `company_id`.

**Embeddings and search.** Re-embedding stays incremental by `text_hash` + `model_name`; a model
upgrade is a full backfill into a new versioned column/table, swapped atomically. At larger scale
vectors go to a vector store (pgvector, OpenSearch, a managed vector DB, or DuckDB's VSS/HNSW
extension) with metadata filters for hybrid search; embedding runs as a batch job on GPU or via
an embedding API with rate limiting and caching.

**Security and operations.** Credentials in a secrets manager (never in `.env` files in git),
least-privilege service accounts per layer, PII review of free-text fields (author), and
observability (row counts, durations and DQ results per run are already logged per step).
