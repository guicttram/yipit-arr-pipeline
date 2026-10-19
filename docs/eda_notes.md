# EDA Notes

Findings from profiling the raw inputs (`data/raw/tech_news.csv`, `data/raw/company_metadata.json`) before writing any cleaner.
Exploration was done interactively in a local notebook (not versioned, see ADR-004); this file is the durable record.

Status: **in progress** (Phase 1).

---

## 1. Articles (`tech_news.csv`)

- 750 rows.
- `article_id`: 750 distinct, 0 nulls → reliable natural key.
- `url`: 750 distinct → no duplicate articles by URL. Duplicates by title not checked yet.

### 1.1 `revenue` (source of ARR observations)

- 192 rows (25.6%) have no usable value:
  - `Not disclosed`: 85
  - `NULL`: 66
  - `N/A`: 41
- Currencies: `$`, `€`, `£`, `¥`, and a trailing `USD` code.
- Scale notations found:
  - Suffixes: `M`, `B`, `billion` (e.g. `$340.0M`, `$1.380B`, `$0.760 billion`).
  - Full integers with thousand separators (e.g. `€86,363,636`, `¥177,000,000,000`).
  - Code after the number (e.g. `1010.0M USD`).
  - Unusual magnitudes kept in the "wrong" unit (e.g. `$79170.0M`, i.e. ~$79B expressed in millions).
- Ranges with a hyphen (e.g. `$87.2M - $96.4M`, `$0.982B - $1.086B`) → midpoint required.
- Non-USD values look like they were generated from round USD amounts and converted back with the assignment's FX rates (e.g. `£19,685,039,370 × 1.27 ≈ $25,000,000,000`; `¥360,000,000,000 ÷ 150 = $2,400,000,000`). Converting back may yield values a few dollars off a round number, so the rounding rule matters and must be documented.
- Pitfall: DuckDB `LIKE` is case-sensitive; profiling patterns with `LIKE '%B%'` would miss lowercase variants. Use `ILIKE` or regex-based pattern classification.

### 1.2 `published_date`

At least 5 formats observed:

| Format | Example |
|---|---|
| ISO date | `2023-02-07` |
| ISO timestamp (UTC) | `2023-05-22T00:00:00Z` |
| Long month name | `August 15, 2023` |
| Day + short month | `21 Feb 2020` |
| `NN/NN/YYYY` | `08/30/2020`, `04/14/2021` (unambiguously US: month/day) |
| `NN-NN-YYYY` | `24-04-2020`, `28-02-2023` (unambiguously EU: day-month) |

- Ambiguous values exist (both parts ≤ 12, e.g. `02-04-2023`, `03/12/2024`).
- Hypothesis to verify: the separator determines the convention (`/` → US, `-` → EU). If no counter-example exists (a `/` value with day > 12 in the first position, or a `-` value with month > 12 in the second), the separator can resolve ambiguity deterministically.
- Pending: count per format and number of ambiguous values.

### 1.3 `category`

19 distinct values (count of articles):

| Raw value | n |
|---|---|
| Cloud Computing | 56 |
| Artificial Intelligence | 54 |
| Financial Technology | 48 |
| Big Data | 45 |
| Analytics | 43 |
| Data Analytics | 43 |
| AI/ML | 42 |
| FinTech | 40 |
| Software | 39 |
| Cybersecurity | 39 |
| Enterprise Software | 38 |
| Finance | 36 |
| AI & ML | 36 |
| Cloud | 35 |
| SaaS | 34 |
| InfoSec | 34 |
| Security | 31 |
| Machine Learning | 29 |
| Cloud Services | 28 |

- The four AI variants (`Artificial Intelligence`, `AI/ML`, `AI & ML`, `Machine Learning`) total 161 articles (~21%).
- Mapping decisions so far: see ADR-005.

### 1.4 `company_name` vs metadata

- 46 distinct company names in articles; 21 companies in metadata.
- **Exact match**: 21 names match, 25 names exist only in articles, 0 metadata companies are unreferenced.
- The 25 unmatched names cover **85 articles (~11%)**.
- After the normalization key (ADR-006): 34 of 46 names match; 12 remain unmatched, covering **46 articles (~6%)**. No false positives were introduced, and the join did not fan out (46 rows in, 46 rows out), which confirms the 21 metadata keys stay distinct after normalization.

Remaining 12 names, by type:

| Type | Names | Articles |
|---|---|---|
| Absent from metadata | Cohere (8), xAI (6), Mistral AI (5), Hugging Face (3), Perplexity AI (3) | 25 |
| Direct alias → resolved via alias table (ADR-007) | AWS, DeepMind, Meta AI Research, Facebook AI Research | 12 |
| Product / sub-entity of a metadata company → unmatched | Azure, Microsoft Azure | 6 |
| Two entities in one string → unmatched (`multi_entity`) | The Boring Company / SpaceX | 3 |

- After the alias table: **34 articles (~4.5%) remain unmatched** (25 absent + 6 product + 3 multi-entity).

- All absent companies are AI companies. Their articles can still qualify for `ai_articles_enriched.csv` by category, but would carry no metadata enrichment.

#### Fuzzy matching probe (Jaro-Winkler, DuckDB)

Best metadata candidate per unmatched name, on lowercased raw names:

- True matches scored 0.867–1.0. All of them are already resolved by the deterministic normalization key.
- `Mistral AI` → `Meta AI` scored 0.837: a false positive close to any reasonable cut-off.
- Absent companies get plausible-looking but wrong candidates (`Cohere` → `Cloudflare` 0.69, `xAI` → `Airbnb` 0.67).
- Aliases score low (`AWS`, `Azure`, `DeepMind`, `Facebook AI Research`: 0.55–0.65); character similarity cannot detect synonyms.
- `Microsoft Azure` → `Microsoft` scored 0.92: high score, but a product vs company decision, not a spelling variant.
- Conclusion so far: on this dataset fuzzy matching adds no correct match beyond normalization and adds false-positive risk. Thresholds are scorer-specific and do not transfer between DuckDB `jaro_winkler_similarity` and `rapidfuzz` scorers. Final role of fuzzy matching is still open.

---

## 2. Company metadata (`company_metadata.json`)

- Shape: a single JSON object keyed by company name (`{"Snowflake": {...}, ...}`), not an array of records. The company name exists only as the object key.
  - Risk: duplicate keys are legal JSON but most parsers silently keep the last one. Check the raw file for duplicate top-level keys before trusting a parsed count.
- 21 companies.
- Field inventory: every company has the same set of attributes (homogeneous schema). This measures key presence only, not filled values.
- Flattened in DuckDB with `read_text` → `::JSON` → `unnest(json_keys(j))` → `j -> '$."<key>"'`, extracting fields with `->>` (all as text, casting deferred to silver).

Pending:
- ~~Distinct `industry` values and AI/ML variants.~~ Done: 6 values, no spelling variants (Data Analytics 7, Cloud Computing 5, AI/ML 3, Cybersecurity 2, SaaS 2, FinTech 2). AI/ML companies: NVIDIA, SpaceX, Scale AI.

Finding: `industry` (company-level label) and article `category` (per-article topic) are **uncorrelated** once articles are joined to metadata through the normalization key (e.g. Scale AI articles tagged `Financial Technology`, Tesla articles tagged `Security`). `industry` also does not follow real-world intuition (Tesla = Cloud Computing, Anthropic = FinTech, SpaceX = AI/ML). Consequences: (1) metadata is used as given and never overridden with outside knowledge; (2) "AI by industry only" articles are common, so the OR rule of the AI export matters; (3) crossing category with industry is **not** a useful way to resolve ambiguous categories such as `Finance` / `Security`: decide those from the label semantics.
AI-related articles before date and ARR filters (category AI = `Artificial Intelligence`, `AI/ML`, `AI & ML`, `Machine Learning`; 161 articles):

| Bucket | Articles |
|---|---|
| AI by category only (company industry is not AI/ML) | 122 |
| AI by both category and industry | 26 |
| AI by category, company unresolved (industry unknown) | 13 |
| AI by industry only (category is not AI) | 78 |

So the industry branch of the OR adds 78 articles on top of the 161 category-AI ones. The 13 unresolved-company articles cannot carry an ARR observation, so they cannot reach the export.
- Nulls and sentinel strings per field (`null`, `""`, `N/A`).
- Types and ranges of `employee_count`, `founded_year`, `is_public`.

---

## 3. Open items before closing Phase 1

- [ ] Metadata profile (section 2, pending list).
- [ ] `revenue` format catalog via regex pattern classification (digits → `9`, letters → `a`).
- [ ] Date separator hypothesis and count of ambiguous dates.
- [ ] Null % per column; duplicate titles.
- [ ] Finish category taxonomy (ADR-005 open points).
- [x] Decide role of fuzzy matching (suggestion only, ADR-008).
- [x] Decide `Meta AI Research` / `Facebook AI Research` (alias of `Meta AI`, ADR-007).
