# Architecture Decision Records (ADR) & Assumptions Log

This document tracks key architectural decisions, design choices, rejected alternatives, and assumptions made throughout the project.

---

### ADR-001: Python 3.11 Runtime & Toolchain Management via `uv`
- **Date**: 2026-10-17
- **Status**: Accepted
- **Context**: The host environment (Ubuntu 20.04 LTS) defaults to Python 3.8, and the `deadsnakes` PPA has discontinued pre-built packages for Focal. Modern typing (`int | None`) and recent releases of DuckDB, Arrow, and Transformers require Python 3.10+.
- **Decision**: Use `uv` to provision a standalone, managed Python 3.11 interpreter in user space without requiring root/PPA workarounds, while using `uv pip` for fast, deterministic virtual environment bootstrapping.
- **Alternatives Considered**: Using system Python 3.8 (rejected due to missing typing and older library compatibility); compiling Python 3.11 from source (rejected due to maintenance overhead).
- **Consequences**: Deterministic builds, fast dependency resolution, and compatibility with modern data engineering tooling.

---

### ADR-002: CPU-Only PyTorch Distribution
- **Date**: 2026-10-17
- **Status**: Accepted
- **Context**: Default PyPI distributions of `torch` package CUDA/NVIDIA binaries (~2.5–3.5 GB), which are unnecessary for lightweight local inference (`all-MiniLM-L6-v2`) in WSL/CPU environments.
- **Decision**: Explicitly install PyTorch via the official CPU index (`https://download.pytorch.org/whl/cpu`).
- **Alternatives Considered**: Standard PyPI `torch` (rejected due to excessive network and disk footprint).
- **Consequences**: Reduced package footprint to ~180 MB, faster CI/CD caching, and zero impact on CPU embedding performance.

---

### ADR-003: DuckDB as In-Process Analytical Warehouse Engine
- **Date**: 2026-10-17
- **Status**: Accepted
- **Context**: The assignment requires warehouse-style data modeling (Bronze/Silver/Gold), analytical queries (timelines, latest ARR, quarterly aggregations), and hybrid search capabilities without deploying heavy external database infrastructure.
- **Decision**: Adopt DuckDB as the embedded database engine for all local warehouse layers and SQL views.
- **Alternatives Considered**: SQLite (rejected due to poor analytical/columnar performance and limited array/vector support); PostgreSQL/MySQL container (rejected due to local operational overhead and non-zero dependency footprint).
- **Consequences**: Zero external daemon setup, high-performance columnar aggregations, zero-copy Arrow/Pandas integration, and native vector array support.

---

### ADR-004: Exploratory Notebooks Kept Out of Version Control
- **Date**: 2026-10-17
- **Status**: Accepted
- **Context**: EDA is done in Jupyter notebooks whose saved outputs contain rows of the raw dataset, which must never be committed.
- **Decision**: Ignore `*.ipynb`, `.ipynb_checkpoints/` and `notebooks/` in git. Durable findings are written to `docs/eda_notes.md`.
- **Alternatives Considered**: Committing notebooks with outputs cleared (`nbconvert --clear-output`) — rejected because one forgotten clear leaks data.
- **Consequences**: No data leakage through notebooks; exploration code is disposable, and anything that matters must be promoted to `docs/` or `src/`.

---

### ADR-005: Article Category Taxonomy (partial)
- **Date**: 2026-10-17
- **Status**: In progress
- **Context**: `category` has 19 raw values with synonyms and spelling variants. The mapping lives in code as an explicit dictionary, applied after a light normalization (case, whitespace, punctuation).
- **Decision (so far)**:
  - Two layers. (1) A formatting-only key: lowercase, trim, keep `[a-z0-9]` (no parenthetical or company-suffix rules; those are specific to company names). On the data seen this collapses only `AI/ML` and `AI & ML` into `aiml` (19 raw values → 18 keys). (2) An explicit dictionary from key to canonical bucket, which carries the meaning. Synonyms such as `analytics` / `dataanalytics` are merged in the dictionary, never by loosening the regex (removing the word `data` would also turn `bigdata` into `big`).
  - `Artificial Intelligence`, `AI/ML`, `AI & ML`, `Machine Learning` → `AI_ML`. AI and ML are treated as one subject for this project (matches the assignment's example).
  - `Analytics`, `Data Analytics` → one bucket.
  - `Big Data` stays a separate bucket: it covers large-scale data processing, not necessarily analysis.
- **Open**: `Cloud` / `Cloud Computing` / `Cloud Services`; `Financial Technology` / `FinTech` vs `Finance`; `Cybersecurity` / `InfoSec` vs `Security`; `Software` / `Enterprise Software` / `SaaS`; final bucket names; behavior for unknown categories (`OTHER` vs `UNMAPPED` + DQ flag).
- **Alternatives Considered**: Fuzzy or embedding-based automatic mapping — rejected as the mapping mechanism (silent misclassification feeds the AI export filter); acceptable only as a review suggestion for unmapped values.
- **Consequences**: Deterministic, testable mapping; new raw values surface as data-quality issues instead of being silently classified.

---

### ADR-006: Company Name Normalization Key
- **Date**: 2026-10-18
- **Status**: Accepted
- **Context**: Exact matching leaves 25 article company names (85 articles) unmatched, mostly due to casing, spacing, legal suffixes and parentheticals (`CloudFlare`, `Mongo DB`, `Stripe Inc.`, `Amazon Web Services (AWS)`).
- **Decision**: Match on a derived `name_key`, built in this order: (1) lowercase and trim; (2) drop parenthetical text; (3) drop trailing suffixes `inc`, `corp`, `corporation`, `ltd`, `llc`, `technologies` (repeatable, optional trailing dot); (4) keep only `[a-z0-9]`. The same function is applied to both article names and metadata keys. The raw name is always preserved (`company_name_raw`); the key is used only for matching, never displayed.
- **Alternatives Considered**: Exact match only (11% of articles unmatched); fuzzy matching as the first resolution layer (adds false positives without adding correct matches on this dataset).
- **Consequences**: 34 of 46 names match (46 articles remain unmatched, ~6%) with no false positives. `technologies` is a descriptive word, not a legal suffix: it increases the risk of two distinct companies colliding on the same key. A pipeline check must assert that metadata keys stay unique after normalization.

---

### ADR-007: Company Alias Policy
- **Date**: 2026-10-18
- **Status**: Accepted
- **Context**: After normalization, 7 names still refer to metadata companies through another name, a product, or a combined string.
- **Decision**:
  - **Direct aliases** are mapped to the metadata company through an explicit alias table: `AWS` → `Amazon Web Services`; `DeepMind` → `Google DeepMind`; `Meta AI Research`, `Facebook AI Research` and `FAIR` → `Meta AI` (same research organization under current and former names; `FAIR` does not appear in the data yet and is added preventively).
  - **Products / sub-entities are not merged into the parent company**: `Azure` and `Microsoft Azure` are not mapped to `Microsoft`. An ARR figure for a product is a different scope from company-level ARR; merging would mix both scopes in the company's ARR timeline and in "latest ARR". These articles stay unmatched. Deciding whether two such articles describe the same thing would require reading the article content (e.g. a dedicated text-understanding step), which is out of scope for cost and complexity.
  - **Strings naming more than one company** (`The Boring Company / SpaceX`) stay unmatched, with reason `multi_entity` recorded as a data-quality issue. The article is kept in the article fact (searchable, embedded) but produces no ARR observation.
- **Alternatives Considered**: Mapping products to the parent company (rejected: mixed ARR scopes); fuzzy matching for aliases (rejected: character similarity does not capture synonyms, scores 0.55–0.65); mapping `The Boring Company / SpaceX` to `SpaceX` (rejected: The Boring Company is a separate company, so its ARR would land in another company's timeline); splitting a multi-company article into one row per company (rejected: breaks the one-article-one-company grain and would double-count or invent ARR, since the article carries a single revenue value).
- **Consequences**: Alias table is small, versioned and reviewable. Every resolved article records how it was matched (`exact`, `normalized`, `alias`) for lineage. Products and multi-company articles are excluded from company ARR timelines and listed in the unmatched export. If multi-company articles become common, an article-to-company bridge table is the evolution path (documented in the architecture doc).

---

### ADR-008: Fuzzy Matching as Suggestion Only
- **Date**: 2026-10-18
- **Status**: Accepted
- **Context**: The EDA probe (Jaro-Winkler) showed that every correct fuzzy match is already resolved by the normalization key (ADR-006) and the alias table (ADR-007). What remains is absent companies (fuzzy invents wrong candidates, e.g. `Cohere` → `Cloudflare`), products that were deliberately kept separate (token-based scorers return 100 for subsets such as `Microsoft Azure` vs `Microsoft`), and a false positive near any reasonable cut-off (`Mistral AI` → `Meta AI`, 0.837). The normalization rules are, however, calibrated on the data seen so far; new suffixes (`GmbH`, `Holdings`) or typos would silently become unmatched.
- **Decision**: Run `rapidfuzz` on names still unresolved after the exact, normalized and alias layers, against metadata names, and write the results to a `company_match_suggestions` output (raw name, normalized key, best candidate, score, scorer, number of articles, review status). Fuzzy matching **never assigns `company_id`**. A reviewer approves a suggestion by adding it to the versioned alias table, so the next run resolves it deterministically ("fuzzy proposes, the alias table decides"). Minimal scope: one scorer, a score floor only to cut noise, one row per distinct name, and a list of known rejections so absent companies are not re-suggested on every run.
- **Alternatives Considered**: Automatic acceptance above a threshold (rejected: zero correct matches gained on this dataset, false-positive risk, and thresholds do not transfer between scorers); not using fuzzy matching at all (rejected: no early signal when the source introduces name variants the rules do not cover).
- **Consequences**: No data risk, since suggestions do not change modeled data. The number of unresolved names and suggestions becomes a data-quality signal. The real cost is human review; the floor and rejection list keep the list short.
---

> ADR-009 onwards cover topics the original ADRs (001-008) left open. Entries marked
> **Accepted (default, pending review)** record defaults for those open topics; they are
> deliberately simple and easy to change in one place.

---

### ADR-009: Category Taxonomy — Remaining Buckets and Unknown Labels
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Context**: ADR-005 left open the cloud, finance, security and software families and the behavior for unknown labels.
- **Decision**: Buckets: `AI_ML` (Artificial Intelligence, AI/ML, AI & ML, Machine Learning); `ANALYTICS` (Analytics, Data Analytics); `BIG_DATA`; `CLOUD` (Cloud, Cloud Computing, Cloud Services); `FINTECH` (Financial Technology, FinTech); `FINANCE`; `CYBERSECURITY` (Cybersecurity, InfoSec); `SECURITY`; `SOFTWARE`; `ENTERPRISE_SOFTWARE`; `SAAS`. Only exact synonyms and abbreviations are merged; broader labels (`Finance`, `Security`, `Software`) stay separate. Unknown labels map to `UNMAPPED` with a `category_unmapped` row in `dq_issues`; empty labels are `NULL` with `category_missing`. Company `industry` values go through the same key + dictionary into `industry_bucket`, so the AI filter compares buckets on both sides while the export keeps the industry text as given.
- **Alternatives Considered**: Merging `Finance` into `FINTECH` or `Security` into `CYBERSECURITY` (rejected: guesses intent; the labels are broader); an `OTHER` bucket (rejected: hides drift as if it were a real category).
- **Consequences**: 19 raw labels → 11 buckets. A new source label is visible immediately (DQ warning) and is added by editing one dictionary entry plus a test.

---

### ADR-010: ARR Parsing Rules
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Context**: `revenue` mixes currencies, scale suffixes, full integers, ranges and three "no value" sentinels. The non-USD values were generated from round USD amounts, so float error or banker's rounding would produce visibly wrong integers.
- **Decision**: `Decimal` end to end. Each amount: optional currency (symbol or ISO code, before or after), number with optional strict thousands grouping, optional unit (`k`, `m`, `mm`, `mn`, `million`, `b`, `bn`, `billion`, `t`, `trillion`). Ranges (`-`, en/em dash, `to`) convert each bound to USD, then take the midpoint; a unit-less bound inherits the other bound's unit unless it is written with thousands separators. FX from the spec (EUR × 1.1, GBP × 1.27, JPY ÷ 150). Round **once**, at the end, with `ROUND_HALF_UP`. Statuses: `ok` (with `parse_method` `single_value`/`range_midpoint`), `missing` (empty, `NULL`, `N/A`, …), `undisclosed` (`Not disclosed`, …), `unparseable`, `invalid_non_positive` (result ≤ 0 after rounding). No currency marker → assumed USD and recorded as `currency_detected = NULL` plus an info DQ row. No upper bound; values outside $100K–$1T get an `arr_out_of_expected_range` warning only. Contradictory currencies (`€5M USD`) and mixed-currency ranges are `unparseable`.
- **Alternatives Considered**: Float arithmetic (rejected: non-USD values generated from round USD amounts must convert back to that round amount, not to a float neighbour); rounding each bound before the midpoint (rejected: double rounding); treating `N/A` as undisclosed (rejected: it does not state that the company withheld the figure).
- **Consequences**: All 558 non-sentinel values in the source parse (0 unparseable). Only `ok` rows can become ARR observations.

---

### ADR-011: Ambiguous Numeric Dates
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Context**: The EDA hypothesis was "the separator decides the convention" (`/` US, `-` EU). Verification **refuted it for `-`**: 17 dash dates have a second field > 12 (month-first, e.g. `06-21-2024`) next to 63 day-first ones; `/` has 71 month-first and 0 day-first values.
- **Decision**: (1) Value-based first: if one field is > 12 it is the day, whatever the separator. (2) If both fields are ≤ 12: `/` → month/day (never contradicted), `-` and `.` → day/month (majority among unambiguous dash dates, 63 vs 17). (3) Flag `date_is_ambiguous = true` (and an info DQ row) unless both readings produce the same date (`05-05-2023`). Result: 93 ambiguous rows (37 `/`, 56 `-`); 67 of them carry an ARR observation, which also carries the flag.
- **Alternatives Considered**: Always US or always EU (rejected: contradicted by the data for one separator either way); marking ambiguous rows invalid (rejected: loses 12% of articles although the year is certain); inferring per company or per author (rejected: no evidence the convention follows either).
- **Consequences**: A day/month swap never changes the **year**, so the 2022–2024 export filter is unaffected; it can only move an observation between months/quarters. The flag lets consumers exclude ambiguous rows from quarterly analysis if needed.

---

### ADR-012: Missing and Invalid Dates
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: `published_date = NULL` with `date_parse_status` `missing` or `invalid` and a warning in `dq_issues`. The article is kept (searchable, embedded) but produces no ARR observation (`arr_excluded_reason = 'date_unresolved'`), because an observation without an as-of date cannot be placed on a timeline. Years outside 1990–2100 and two-digit years are invalid. Timestamps with an offset are converted to UTC before taking the date.
- **Consequences**: The current source has 0 missing/invalid dates; the rule is exercised by tests.

---

### ADR-013: Idempotency, Batches and Rebuilds
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Context**: Re-running the pipeline must not duplicate records, and later files (corrections, backfills) must be absorbed predictably.
- **Decision**: Bronze is append-only. Each source file is registered in `ingest_batch` with its SHA-256; a file whose hash is already registered (for the same entity) is skipped. Silver and gold are **rebuilt deterministically from all of bronze** inside one transaction per step (no run timestamps in derived tables). When an `article_id` appears in several batches, silver keeps the row from the most recent batch (then the last row within a file); superseded rows are recorded as `superseded_by_newer_row` info issues. Metadata is resolved the same way (latest batch per company). Derived tables have no physical `PRIMARY KEY` (DuckDB's constraint checking with delete + re-insert in one transaction is a known pain point); uniqueness and referential integrity are asserted by blocking DQ checks instead.
- **Alternatives Considered**: Incremental upserts (`MERGE`) into silver/gold (rejected for this scale: more state to get wrong; full rebuild of 750 rows takes milliseconds); deduplicating by row content (rejected: a corrected row must replace, not coexist).
- **Consequences**: Re-runs produce identical table hashes and byte-identical CSVs (tested). A backfill is "ingest the file, re-run"; replaying from bronze reproduces any state. Deletions at the source are not propagated (an article missing from a newer file is kept); tombstones are future work.

---

### ADR-014: Keys
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: `article_id` is the natural key (750 distinct, never null). `company_id` = first 16 hex chars of SHA-256 of the normalized company key (ADR-006). `observation_id` = first 16 hex chars of SHA-256 of `article_id` (one observation per article). `batch_id` = `B0001`, `B0002`, … in ingestion order.
- **Consequences**: Keys are stable across runs and machines. Renaming a company in metadata to a name with a different key yields a new `company_id`; an alias row is the way to keep history connected.

---

### ADR-015: Latest and Quarterly ARR
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: Latest ARR per company = the observation with the greatest `as_of_date`; ties broken by most recent batch, then highest `article_id`. Quarterly ARR = the last observation within the calendar quarter using the same ordering, exposed with `n_observations`, `min_arr_usd` and `max_arr_usd` for the quarter. Quarters without observations are absent (no carry-forward).
- **Alternatives Considered**: Average or max per quarter (rejected: ARR is a point-in-time run rate, so "as of the end of the period" is the natural reading; averages blend different dates).
- **Consequences**: "Latest" and "quarterly" are consistent with each other. Caveat: reported ARR mostly grows over time but is not monotonic (13 of 529 observations are lower than the previous one), and some quarters hold several distinct values, so the min/max spread is shown next to the chosen value.

---

### ADR-016: Company Enrichment — Age, Size and Metadata Snapshot
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: `company_age = published_year − founded_year` (integer); negative → `NULL` plus a `negative_company_age` warning. `company_size_category`: `< 10,000` Small; `10,000–30,000` Medium (inclusive); `> 30,000` Large; unknown count → `NULL`. Metadata is a **snapshot**: today's employee count and public status are applied to articles of every year. `is_public` vs `stock_ticker` inconsistencies (5 public without ticker, 4 private with ticker) are not fixed; they are reported as warnings. Metadata is used as given, never overridden with outside knowledge.
- **Alternatives Considered**: SCD Type 2 on company metadata (deferred: one snapshot exists; documented as the evolution path).
- **Consequences**: Size and public status in the export describe the company now, not at publication time; documented as a caveat.

---

### ADR-017: `ai_articles_enriched.csv` Rules
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: Filter `(category bucket = AI_ML OR industry bucket = AI_ML) AND published_date BETWEEN 2022-01-01 AND 2024-12-31 (inclusive) AND an ARR observation exists AND arr_usd > 50,000,000 (strict)`. Requiring an observation means the company must be resolved: articles of companies absent from metadata cannot qualify (8 such AI articles in the window have a valid ARR). `company_name` is the canonical metadata name (raw name stays in `fact_article`), `category` is the bucket, `published_date` is ISO. `embedding` is a JSON list of 384 floats; `top_similar_articles` is a JSON list of 3 article IDs computed over **all** articles. Rows are ordered by `published_date, article_id`.
- **Alternatives Considered**: Using the article-level parsed ARR for unresolved companies (rejected: the spec defines ARR as an observation of a company; without a resolved company there is no lineage to a company timeline).
- **Consequences**: 113 rows on the current data. Every row traces to `fact_arr_observation` → `fact_article` → `silver_articles` → `bronze_articles_raw` (batch, file, row number).

---

### ADR-018: Embeddings and Similarity
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: Text = `title + " " + summary` (nulls → empty). `all-MiniLM-L6-v2`, `normalize_embeddings=True` (cosine = dot product), stored as `FLOAT[384]` in `article_embedding` with `model_name` and `text_hash`. The table is a cache: only new/changed texts or a model change trigger encoding, and the model is not even loaded when nothing changed. The model is loaded from the local Hugging Face cache first and downloaded only if missing. `article_similarity` is recomputed from all vectors with numpy (N×N, diagonal excluded, scores rounded to 6 decimals, ties by `article_id` ascending). Hybrid search runs SQL filters and `array_cosine_similarity` in one DuckDB query.
- **Alternatives Considered**: A vector database / ANN index (rejected at 750 rows: exact search is milliseconds and deterministic; it is the scale-out path); embedding only exported rows (rejected: neighbours must come from all articles).
- **Consequences**: Caveat: the source text is heavily templated (303 distinct titles, 12 distinct summaries for 750 articles), so many neighbours are exact template duplicates (score 1.0) and search mostly discriminates on title wording.

---

### ADR-019: Data-Quality Model
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: Two levels. (1) `dq_issues`: one row per detected record problem (entity, id, field, raw value, reason, severity `info`/`warning`, batch), written by the step that detects it; issues never drop a record by themselves. (2) `dq_check_results`: table-level checks after the model. **Blocking** (export is not written): key uniqueness, not-null on required columns, referential integrity, observations only from valid ARR, every eligible article observed, bronze = silver + rejected reconciliation, metadata key uniqueness after normalization, embedding coverage, similarity sanity. **Warning** (reported): `is_public`/ticker inconsistency, unmatched-company articles, ambiguous dates, invalid dates, unmapped categories, unparseable ARR, pending match suggestions. Contract violations at ingestion (missing required column, duplicate metadata keys, metadata key collision) fail immediately.
- **Consequences**: `python -m yipit_pipeline dq` exits with status 1 on a blocking failure, so it can gate an orchestrator task.

---

### ADR-020: Fuzzy Suggestion Parameters
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: Implements ADR-008 with `rapidfuzz.fuzz.token_sort_ratio` on a space-preserving key (lowercase, parentheticals and legal suffixes removed, non-alphanumerics → single spaces), floor **60**. Known rejections are seeded with the decided unmatched names (Azure, Microsoft Azure, The Boring Company / SpaceX) and the companies confirmed absent from metadata (Cohere, xAI, Mistral AI, Hugging Face, Perplexity AI); they are emitted with status `rejected` and the reason, not `pending`.
- **Alternatives Considered**: `WRatio`, `token_set_ratio`, `partial_ratio` (rejected: they score subsets such as `Microsoft Azure` vs `Microsoft` at 100).
- **Consequences**: Current data yields 2 rows, both rejected (`Microsoft Azure` → Microsoft 75; `Mistral AI` → Meta AI 70.6); 0 pending. A new typo variant would appear as pending.

---

### ADR-021: Schema Contracts and Schema Evolution
- **Date**: 2026-10-19
- **Status**: Accepted (default, pending review)
- **Decision**: The articles file must contain the required columns (`article_id, title, company_name, published_date, category, revenue, summary, url`); otherwise ingestion fails before writing anything. Optional known columns (`author`, `word_count`) are typed in silver; unexpected extra columns are kept in bronze `_extra_json` and carried to silver `extra_json`. Silver/gold carry a `DERIVED_SCHEMA_VERSION`; when the code's version differs from the warehouse's, derived tables and views are dropped and rebuilt from bronze, which is never dropped. Inputs are located through `--data-dir` / `DATA_DIR`.
- **Consequences**: Additive source changes never break ingestion; breaking changes fail loudly; model changes are a version bump plus a re-run.
