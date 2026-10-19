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