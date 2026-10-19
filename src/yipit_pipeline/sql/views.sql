-- Analytical views over the gold layer. Recreated by the model and embed steps.
--
-- Ordering rule used everywhere an observation must be picked ("latest"):
--   as_of_date DESC, then source_batch_seq DESC (newest ingestion), then article_id DESC.

-- ARR of a company over time: one row per observation, with the source article attached.
CREATE OR REPLACE VIEW v_company_arr_timeline AS
SELECT
    d.company_id,
    d.company_name,
    o.as_of_date,
    o.as_of_quarter,
    o.arr_usd,
    o.arr_usd - lag(o.arr_usd) OVER (
        PARTITION BY o.company_id ORDER BY o.as_of_date, o.source_batch_seq, o.article_id
    ) AS arr_change_usd,
    o.observation_id,
    o.article_id,
    a.title,
    a.url,
    o.arr_raw,
    o.currency_detected,
    o.parse_method,
    o.date_is_ambiguous,
    o.source_batch_id,
    o.source_file,
    o.source_row_number
FROM fact_arr_observation o
JOIN dim_company d ON d.company_id = o.company_id
JOIN fact_article a ON a.article_id = o.article_id;

-- Latest ARR per company (one row per company with at least one observation).
CREATE OR REPLACE VIEW v_company_arr_latest AS
WITH ranked AS (
    SELECT
        o.*,
        row_number() OVER (
            PARTITION BY o.company_id
            ORDER BY o.as_of_date DESC, o.source_batch_seq DESC, o.article_id DESC
        ) AS rn,
        count(*) OVER (PARTITION BY o.company_id) AS n_observations,
        min(o.as_of_date) OVER (PARTITION BY o.company_id) AS first_observed_date
    FROM fact_arr_observation o
)
SELECT
    d.company_id,
    d.company_name,
    r.arr_usd AS latest_arr_usd,
    r.as_of_date AS latest_as_of_date,
    r.observation_id,
    r.article_id AS source_article_id,
    r.date_is_ambiguous,
    r.n_observations,
    r.first_observed_date
FROM ranked r
JOIN dim_company d ON d.company_id = r.company_id
WHERE r.rn = 1;

-- Quarterly ARR per company: the last observation in each quarter (same ordering as latest),
-- plus the spread of observations within the quarter. Quarters without observations are
-- absent (no carry-forward: that would invent data).
CREATE OR REPLACE VIEW v_company_arr_quarterly AS
WITH ranked AS (
    SELECT
        o.*,
        row_number() OVER q_desc AS rn,
        count(*) OVER q AS n_observations,
        min(o.arr_usd) OVER q AS min_arr_usd,
        max(o.arr_usd) OVER q AS max_arr_usd
    FROM fact_arr_observation o
    WINDOW
        q AS (PARTITION BY o.company_id, o.as_of_quarter),
        q_desc AS (
            PARTITION BY o.company_id, o.as_of_quarter
            ORDER BY o.as_of_date DESC, o.source_batch_seq DESC, o.article_id DESC
        )
)
SELECT
    d.company_id,
    d.company_name,
    r.as_of_year AS year,
    r.as_of_quarter AS quarter,
    r.arr_usd AS quarter_arr_usd,
    r.as_of_date AS quarter_arr_as_of_date,
    r.observation_id,
    r.article_id AS source_article_id,
    r.n_observations,
    r.min_arr_usd,
    r.max_arr_usd
FROM ranked r
JOIN dim_company d ON d.company_id = r.company_id
WHERE r.rn = 1;

-- One row per article with company enrichment, ARR observation, embedding and neighbours.
-- Base for ai_articles_enriched.csv and for hybrid search.
CREATE OR REPLACE VIEW v_articles_enriched AS
WITH neighbours AS (
    SELECT article_id, list(similar_article_id ORDER BY rank) AS top_similar_articles
    FROM article_similarity
    GROUP BY article_id
)
SELECT
    a.article_id,
    a.title,
    coalesce(d.company_name, a.company_name_raw) AS company_name,
    a.company_id,
    a.company_name_raw,
    a.company_match_method,
    a.published_date,
    a.published_year,
    a.published_quarter,
    a.category,
    a.category_raw,
    o.arr_usd,
    o.observation_id,
    a.arr_parse_status,
    a.arr_excluded_reason,
    a.summary,
    a.url,
    d.industry,
    d.industry_bucket,
    d.founded_year,
    d.headquarters,
    d.employee_count,
    d.is_public,
    d.stock_ticker,
    a.company_age,
    d.company_size_category,
    -- AI-related = article category OR company industry (bucket names from the taxonomy).
    coalesce(a.category = 'AI_ML', false) OR coalesce(d.industry_bucket = 'AI_ML', false)
        AS is_ai_related,
    e.embedding,
    n.top_similar_articles
FROM fact_article a
LEFT JOIN dim_company d ON d.company_id = a.company_id
LEFT JOIN fact_arr_observation o ON o.observation_id = a.arr_observation_id
LEFT JOIN article_embedding e ON e.article_id = a.article_id
LEFT JOIN neighbours n ON n.article_id = a.article_id;
