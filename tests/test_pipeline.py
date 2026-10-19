"""End-to-end pipeline on synthetic fixtures: counts, lineage, idempotency, backfills,
schema contract and schema changes."""

import csv
import json

import pytest

from conftest import table_hash
from yipit_pipeline import config
from yipit_pipeline.ingest import SchemaContractError, ingest_articles, ingest_metadata
from yipit_pipeline.pipeline import run_all, step_model, step_transform
from yipit_pipeline.warehouse import DERIVED_TABLES, VIEWS, connect

GOLD_AND_SILVER = [t for t in DERIVED_TABLES] + list(VIEWS) + ["article_embedding"]


def scalar(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def test_counts_on_fixtures(built):
    con = built
    assert scalar(con, "SELECT count(*) FROM bronze_articles_raw") == 16
    assert scalar(con, "SELECT count(*) FROM silver_articles") == 16
    assert scalar(con, "SELECT count(*) FROM dim_company") == 5
    assert scalar(con, "SELECT count(*) FROM fact_article") == 16
    # T001-T006, T008, T011, T013, T016 have valid ARR + resolved company + valid date.
    assert scalar(con, "SELECT count(*) FROM fact_arr_observation") == 10
    assert scalar(con, "SELECT count(*) FROM unmatched_companies") == 1


def test_no_arr_in_dim_company(built):
    columns = [r[0] for r in built.execute("DESCRIBE dim_company").fetchall()]
    assert not [c for c in columns if "arr" in c or "revenue" in c]


def test_arr_excluded_reasons(built):
    reasons = dict(
        built.execute(
            "SELECT article_id, arr_excluded_reason FROM fact_article "
            "WHERE arr_excluded_reason IS NOT NULL"
        ).fetchall()
    )
    assert reasons == {
        "T007": "company_unresolved",
        "T009": "no_valid_arr",
        "T010": "no_valid_arr",
        "T012": "date_unresolved",
        "T014": "no_valid_arr",
        "T015": "no_valid_arr",
    }


def test_missing_and_undisclosed_never_become_observations(built):
    statuses = dict(
        built.execute("SELECT article_id, arr_parse_status FROM silver_articles").fetchall()
    )
    assert statuses["T009"] == "undisclosed"
    assert statuses["T010"] == "missing"
    assert statuses["T014"] == "invalid_non_positive"
    assert statuses["T015"] == "unparseable"
    observed = {
        r[0] for r in built.execute("SELECT article_id FROM fact_arr_observation").fetchall()
    }
    assert not observed & {"T009", "T010", "T014", "T015"}


def test_lineage_observation_to_bronze_row(built):
    row = built.execute(
        """
        SELECT o.arr_usd, o.arr_raw, b.revenue, b._row_number, b._source_file, i.file_hash
        FROM fact_arr_observation o
        JOIN fact_article a USING (article_id)
        JOIN bronze_articles_raw b
          ON b._batch_id = o.source_batch_id AND b._row_number = o.source_row_number
        JOIN ingest_batch i ON i.batch_id = b._batch_id
        WHERE o.article_id = 'T008'
        """
    ).fetchone()
    assert row[0] == 80_000_000  # midpoint of $70M - $90M
    assert row[1] == row[2] == "$70M - $90M"
    assert row[3] == 8 and row[4] == "tech_news.csv" and len(row[5]) == 64


def test_company_resolution_methods(built):
    methods = dict(
        built.execute("SELECT article_id, company_match_method FROM silver_articles").fetchall()
    )
    assert methods["T016"] == "exact"
    assert methods["T002"] == "normalized"
    assert methods["T007"] is None


def test_dates_and_ambiguity(built):
    rows = {
        r[0]: r[1:]
        for r in built.execute(
            "SELECT article_id, published_date::VARCHAR, date_is_ambiguous, date_parse_status "
            "FROM silver_articles"
        ).fetchall()
    }
    assert rows["T009"] == ("2023-04-02", True, "ok")  # '-' ambiguous -> day first
    assert rows["T010"] == ("2023-03-04", True, "ok")  # '/' ambiguous -> month first
    assert rows["T012"] == (None, False, "invalid")


def test_company_derived_attributes(built):
    sizes = dict(
        built.execute("SELECT company_name, company_size_category FROM dim_company").fetchall()
    )
    assert sizes == {
        "Globex Cloud": "Small",  # 9,999
        "Hooli": "Medium",  # 10,000
        "Initech": "Medium",  # 30,000
        "Acme Robotics Inc.": "Medium",  # 25,000
        "Umbrella Data": "Large",  # 30,001
    }
    age = scalar(built, "SELECT company_age FROM fact_article WHERE article_id = 'T013'")
    assert age is None  # 2021 article, company founded 2023
    issue = scalar(built, "SELECT count(*) FROM dq_issues WHERE reason = 'negative_company_age'")
    assert issue == 1


def test_dq_issues_cover_dirty_records(built):
    reasons = {r[0] for r in built.execute("SELECT DISTINCT reason FROM dq_issues").fetchall()}
    assert {
        "arr_missing",
        "arr_undisclosed",
        "arr_unparseable",
        "arr_invalid_non_positive",
        "date_invalid",
        "date_ambiguous_day_month",
        "category_unmapped",
        "company_absent_from_metadata",
        "public_without_ticker",
        "private_with_ticker",
        "negative_company_age",
    } <= reasons


def test_dq_checks_pass(built):
    failed = built.execute(
        "SELECT check_name FROM dq_check_results WHERE severity = 'blocking' AND NOT passed"
    ).fetchall()
    assert failed == []


def test_rerun_is_idempotent(built, settings, fake_embedder):
    before = {t: table_hash(built, t) for t in GOLD_AND_SILVER}
    counts_before = {t: scalar(built, f"SELECT count(*) FROM {t}") for t in GOLD_AND_SILVER}
    files_before = {p.name: p.read_bytes() for p in settings.outputs_dir.glob("*.csv")}
    encoded_before = fake_embedder.texts_encoded

    run_all(built, settings, lambda: fake_embedder)

    assert scalar(built, "SELECT count(*) FROM bronze_articles_raw") == 16  # file skipped
    assert scalar(built, "SELECT count(*) FROM ingest_batch") == 2
    assert {t: scalar(built, f"SELECT count(*) FROM {t}") for t in GOLD_AND_SILVER} == counts_before
    assert {t: table_hash(built, t) for t in GOLD_AND_SILVER} == before
    assert {p.name: p.read_bytes() for p in settings.outputs_dir.glob("*.csv")} == files_before
    assert fake_embedder.texts_encoded == encoded_before  # nothing re-embedded


def test_same_file_reingested_adds_nothing(con, settings):
    first = ingest_articles(con, settings.articles_path)
    second = ingest_articles(con, settings.articles_path)
    assert not first.skipped and second.skipped
    assert scalar(con, "SELECT count(*) FROM bronze_articles_raw") == 16


def test_correction_batch_supersedes_older_row(built, settings, tmp_path, fake_embedder):
    correction = tmp_path / "correction.csv"
    with settings.articles_path.open() as src:
        rows = list(csv.DictReader(src))
    fieldnames = list(rows[0])
    t001 = next(r for r in rows if r["article_id"] == "T001")
    t001["revenue"] = "$65M"  # source correction of an existing article
    new = dict(t001, article_id="T100", url="https://example.test/t100")
    with correction.open("w", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([t001, new])

    ingest_articles(built, correction)
    step_transform(built)
    step_model(built)

    assert scalar(built, "SELECT count(*) FROM bronze_articles_raw") == 18  # append-only
    assert scalar(built, "SELECT count(*) FROM silver_articles") == 17
    arr, batch = built.execute(
        "SELECT arr_usd, source_batch_id FROM fact_arr_observation WHERE article_id = 'T001'"
    ).fetchone()
    assert arr == 65_000_000 and batch == "B0003"
    assert scalar(built, "SELECT count(*) FROM fact_arr_observation WHERE article_id = 'T001'") == 1
    assert (
        scalar(built, "SELECT count(*) FROM dq_issues WHERE reason = 'superseded_by_newer_row'")
        == 1
    )


def test_extra_column_is_kept_and_does_not_break(con, settings, tmp_path):
    path = tmp_path / "extra.csv"
    with settings.articles_path.open() as src:
        rows = list(csv.DictReader(src))
    with path.open("w", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=[*rows[0], "sentiment"])
        writer.writeheader()
        writer.writerows([dict(r, sentiment="positive") for r in rows])
    ingest_metadata(con, settings.metadata_path)
    ingest_articles(con, path)
    step_transform(con)
    extra = scalar(con, "SELECT extra_json FROM silver_articles WHERE article_id = 'T001'")
    assert json.loads(extra) == {"sentiment": "positive"}


def test_missing_required_column_fails_clearly(con, tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text("article_id,title\nT001,Only two columns\n")
    with pytest.raises(SchemaContractError, match="missing required column"):
        ingest_articles(con, path)
    assert scalar(con, "SELECT count(*) FROM ingest_batch") == 0  # nothing half-written


def test_duplicate_metadata_keys_fail(con, tmp_path):
    path = tmp_path / "dup.json"
    path.write_text('{"Acme": {"industry": "SaaS"}, "Acme": {"industry": "AI/ML"}}')
    with pytest.raises(SchemaContractError, match="duplicate JSON key"):
        ingest_metadata(con, path)


def test_schema_version_change_rebuilds_derived_layers(built, settings, monkeypatch, fake_embedder):
    bronze_before = table_hash(built, "bronze_articles_raw")
    obs_before = table_hash(built, "fact_arr_observation")
    built.close()
    monkeypatch.setattr("yipit_pipeline.warehouse.DERIVED_SCHEMA_VERSION", "test-next")
    con = connect(settings.warehouse_path)
    try:
        assert scalar(con, "SELECT count(*) FROM fact_arr_observation") == 0  # dropped
        assert table_hash(con, "bronze_articles_raw") == bronze_before  # bronze untouched
        run_all(con, settings, lambda: fake_embedder)
        assert table_hash(con, "fact_arr_observation") == obs_before
    finally:
        con.close()


def test_data_dir_setting_is_respected(settings):
    assert settings.articles_path.parent == settings.data_dir
    assert settings.articles_path.name == config.ARTICLES_FILE_NAME
