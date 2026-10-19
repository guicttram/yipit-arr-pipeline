-- Warehouse schema (DuckDB). Executed on every connection; all statements are idempotent.
--
-- Layers:
--   registry : ingest_batch, pipeline_meta
--   BRONZE   : raw strings + lineage, append-only, deduplicated by file SHA-256
--   SILVER   : one row per entity, typed and cleaned, with *_raw originals and status flags
--   GOLD     : dimensions, facts, match outputs, DQ results (views live in views.sql)
--
-- Silver and gold are rebuilt from bronze inside one transaction on every run, so they carry
-- no physical PRIMARY KEY (uniqueness and referential integrity are asserted by dq.py).

------------------------------------------------------------------ registry
CREATE TABLE IF NOT EXISTS pipeline_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

-- grain: one row per ingested source file (per entity). Same file hash => skipped.
CREATE TABLE IF NOT EXISTS ingest_batch (
    batch_id         VARCHAR PRIMARY KEY,   -- B0001, B0002, ... (ingestion order)
    batch_seq        INTEGER NOT NULL,
    entity           VARCHAR NOT NULL,      -- articles | company_metadata
    source_file      VARCHAR NOT NULL,
    file_hash        VARCHAR NOT NULL,      -- SHA-256 of the file bytes
    row_count        INTEGER NOT NULL,
    ingested_at      TIMESTAMP NOT NULL,
    pipeline_version VARCHAR NOT NULL,
    UNIQUE (entity, file_hash)
);

------------------------------------------------------------------ bronze
-- grain: one row per source file row per batch. All business columns are raw strings.
CREATE TABLE IF NOT EXISTS bronze_articles_raw (
    _batch_id       VARCHAR NOT NULL,
    _source_file    VARCHAR NOT NULL,
    _file_hash      VARCHAR NOT NULL,
    _row_number     INTEGER NOT NULL,       -- 1-based data row number in the file
    _ingested_at    TIMESTAMP NOT NULL,
    article_id      VARCHAR,
    title           VARCHAR,
    company_name    VARCHAR,
    published_date  VARCHAR,
    category        VARCHAR,
    revenue         VARCHAR,
    summary         VARCHAR,
    url             VARCHAR,
    author          VARCHAR,
    word_count      VARCHAR,
    _extra_json     VARCHAR,                -- unexpected source columns, kept as JSON
    PRIMARY KEY (_batch_id, _row_number)
);

-- grain: one row per company key per batch. raw_json is the company's object as received.
CREATE TABLE IF NOT EXISTS bronze_company_metadata_raw (
    _batch_id     VARCHAR NOT NULL,
    _source_file  VARCHAR NOT NULL,
    _file_hash    VARCHAR NOT NULL,
    _row_number   INTEGER NOT NULL,         -- 1-based position of the key in the JSON object
    _ingested_at  TIMESTAMP NOT NULL,
    company_name  VARCHAR NOT NULL,         -- the JSON object key
    raw_json      VARCHAR NOT NULL,
    PRIMARY KEY (_batch_id, company_name)
);

------------------------------------------------------------------ silver
-- grain: one row per article_id (latest batch wins; within a batch, the last row wins).
CREATE TABLE IF NOT EXISTS silver_articles (
    article_id               VARCHAR,
    title                    VARCHAR,
    summary                  VARCHAR,
    url                      VARCHAR,
    author                   VARCHAR,
    word_count               INTEGER,
    company_name_raw         VARCHAR,
    company_key              VARCHAR,
    company_id               VARCHAR,       -- NULL when unmatched
    company_match_status     VARCHAR,       -- matched | unmatched
    company_match_method     VARCHAR,       -- exact | normalized | alias | NULL
    company_unmatched_reason VARCHAR,       -- absent_from_metadata | product_not_company | ...
    published_date_raw       VARCHAR,
    published_date           DATE,
    published_year           INTEGER,
    published_quarter        INTEGER,
    published_month          INTEGER,
    date_parse_status        VARCHAR,       -- ok | missing | invalid
    date_format              VARCHAR,
    date_is_ambiguous        BOOLEAN,
    category_raw             VARCHAR,
    category_key             VARCHAR,
    category                 VARCHAR,       -- taxonomy bucket, e.g. AI_ML; UNMAPPED if unknown
    category_status          VARCHAR,       -- mapped | unmapped | missing
    revenue_raw              VARCHAR,
    arr_usd                  BIGINT,        -- NULL unless arr_parse_status = 'ok'
    arr_parse_status         VARCHAR,       -- ok | missing | undisclosed | unparseable | invalid_non_positive
    arr_parse_method         VARCHAR,       -- single_value | range_midpoint
    arr_currency_detected    VARCHAR,       -- USD | EUR | GBP | JPY | NULL (assumed USD)
    extra_json               VARCHAR,
    source_batch_id          VARCHAR,
    source_batch_seq         INTEGER,
    source_file              VARCHAR,
    source_row_number        INTEGER
);

-- grain: one row per metadata company (latest batch wins).
CREATE TABLE IF NOT EXISTS silver_company (
    company_id       VARCHAR,
    company_name     VARCHAR,
    company_key      VARCHAR,
    founded_year     INTEGER,
    headquarters     VARCHAR,
    employee_count   INTEGER,
    industry         VARCHAR,               -- as given in metadata
    industry_bucket  VARCHAR,               -- same taxonomy as article category
    is_public        BOOLEAN,
    stock_ticker     VARCHAR,
    source_batch_id  VARCHAR,
    source_batch_seq INTEGER,
    source_file      VARCHAR
);

-- grain: one row per detected problem. stage = step that owns the row (rebuilt by it).
CREATE TABLE IF NOT EXISTS dq_issues (
    issue_id        VARCHAR,                -- hash(stage, entity, entity_id, field, reason)
    stage           VARCHAR,                -- transform | model
    entity          VARCHAR,                -- article | company
    entity_id       VARCHAR,
    field           VARCHAR,
    raw_value       VARCHAR,
    reason          VARCHAR,
    severity        VARCHAR,                -- info | warning
    source_batch_id VARCHAR
);

------------------------------------------------------------------ gold
-- grain: one row per metadata company. Descriptive attributes only: NO ARR here.
CREATE TABLE IF NOT EXISTS dim_company (
    company_id            VARCHAR,
    company_name          VARCHAR,
    company_key           VARCHAR,
    industry              VARCHAR,
    industry_bucket       VARCHAR,
    founded_year          INTEGER,
    headquarters          VARCHAR,
    employee_count        INTEGER,
    company_size_category VARCHAR,          -- Small | Medium | Large
    is_public             BOOLEAN,
    stock_ticker          VARCHAR,
    source_batch_id       VARCHAR
);

-- grain: one row per name key that resolves to a company (own key + alias table).
CREATE TABLE IF NOT EXISTS dim_company_alias (
    alias_key    VARCHAR,
    alias_name   VARCHAR,
    company_id   VARCHAR,
    alias_source VARCHAR                    -- metadata_key | alias_table
);

-- grain: one row per article. company_id is NULL when the company is unresolved.
CREATE TABLE IF NOT EXISTS fact_article (
    article_id               VARCHAR,
    company_id               VARCHAR,
    company_name_raw         VARCHAR,
    company_match_method     VARCHAR,
    company_unmatched_reason VARCHAR,
    title                    VARCHAR,
    summary                  VARCHAR,
    url                      VARCHAR,
    author                   VARCHAR,
    word_count               INTEGER,
    published_date           DATE,
    published_year           INTEGER,
    published_quarter        INTEGER,
    published_month          INTEGER,
    date_parse_status        VARCHAR,
    date_is_ambiguous        BOOLEAN,
    category                 VARCHAR,
    category_raw             VARCHAR,
    revenue_raw              VARCHAR,
    arr_parse_status         VARCHAR,
    arr_observation_id       VARCHAR,       -- FK to fact_arr_observation, NULL if none
    arr_excluded_reason      VARCHAR,       -- why no observation: no_valid_arr | company_unresolved | date_unresolved
    company_age              INTEGER,       -- published_year - founded_year (NULL if unknown/negative)
    source_batch_id          VARCHAR,
    source_file              VARCHAR,
    source_row_number        INTEGER
);

-- grain: one row per article with valid ARR AND resolved company AND valid date.
-- An ARR observation is "company X reported ARR Y as of the article's published date".
CREATE TABLE IF NOT EXISTS fact_arr_observation (
    observation_id    VARCHAR,              -- hash of article_id
    company_id        VARCHAR,
    article_id        VARCHAR,
    as_of_date        DATE,
    as_of_year        INTEGER,
    as_of_quarter     VARCHAR,              -- e.g. 2023-Q2
    arr_usd           BIGINT,
    arr_raw           VARCHAR,
    currency_detected VARCHAR,
    parse_method      VARCHAR,
    date_is_ambiguous BOOLEAN,
    source_batch_id   VARCHAR,
    source_batch_seq  INTEGER,
    source_file       VARCHAR,
    source_row_number INTEGER
);

-- grain: one row per distinct unresolved raw company name.
CREATE TABLE IF NOT EXISTS unmatched_companies (
    company_name_raw  VARCHAR,
    company_key       VARCHAR,
    unmatched_reason  VARCHAR,
    n_articles        INTEGER,
    n_articles_with_valid_arr INTEGER,
    first_published   DATE,
    last_published    DATE
);

-- grain: one row per distinct unresolved raw name with a fuzzy candidate above the floor.
-- Suggestions never assign company_id (ADR-008).
CREATE TABLE IF NOT EXISTS company_match_suggestions (
    company_name_raw       VARCHAR,
    company_key            VARCHAR,
    candidate_company_name VARCHAR,
    candidate_company_id   VARCHAR,
    score                  DOUBLE,
    scorer                 VARCHAR,
    n_articles             INTEGER,
    status                 VARCHAR,         -- pending | approved | rejected
    review_note            VARCHAR
);

-- grain: one row per DQ check of the last run.
CREATE TABLE IF NOT EXISTS dq_check_results (
    check_name VARCHAR,
    severity   VARCHAR,                     -- blocking | warning
    passed     BOOLEAN,
    observed   BIGINT,                      -- offending rows (0 when passed)
    details    VARCHAR
);

------------------------------------------------------------------ embeddings / search
-- grain: one row per article. Cache: rows are kept across runs and only re-embedded when
-- text_hash or model_name changes. {embedding_dim} is filled in by warehouse.py.
CREATE TABLE IF NOT EXISTS article_embedding (
    article_id VARCHAR,
    embedding  FLOAT[{embedding_dim}],      -- L2-normalized: cosine similarity = dot product
    model_name VARCHAR,
    text_hash  VARCHAR                      -- SHA-256 of title + ' ' + summary
);

-- grain: one row per (article, rank). Top-3 nearest other articles over ALL articles.
CREATE TABLE IF NOT EXISTS article_similarity (
    article_id         VARCHAR,
    rank               INTEGER,             -- 1..3
    similar_article_id VARCHAR,
    score              DOUBLE,              -- cosine similarity, rounded to 6 decimals
    model_name         VARCHAR
);
