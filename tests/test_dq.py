"""Table-level DQ checks detect broken invariants and block the export."""

import pytest

from yipit_pipeline.dq import blocking_failures, run_checks
from yipit_pipeline.export import run_export
from yipit_pipeline.pipeline import DataQualityError, run_all


def failing(con):
    return {r.check_name for r in blocking_failures(run_checks(con))}


def test_clean_build_has_no_blocking_failures(built):
    assert failing(built) == set()


def test_orphan_observation_is_blocking(built):
    built.execute(
        "INSERT INTO fact_arr_observation (observation_id, company_id, article_id, as_of_date, "
        "arr_usd) VALUES ('x', 'nope', 'T999', DATE '2023-01-01', 1)"
    )
    assert {
        "fk:fact_arr_observation.article_id->fact_article",
        "fk:fact_arr_observation.company_id->dim_company",
    } <= failing(built)


def test_duplicate_article_is_blocking(built):
    built.execute("INSERT INTO fact_article SELECT * FROM fact_article WHERE article_id = 'T001'")
    assert "unique:fact_article.article_id" in failing(built)


def test_observation_from_undisclosed_arr_is_blocking(built):
    built.execute(
        "UPDATE silver_articles SET arr_parse_status = 'undisclosed' WHERE article_id = 'T001'"
    )
    assert "arr:observation_from_valid_arr_only" in failing(built)


def test_blocking_failure_stops_export(con, settings, fake_embedder, monkeypatch):
    def broken_model(connection):
        from yipit_pipeline.model import run_model

        stats = run_model(connection)
        connection.execute("DELETE FROM dim_company WHERE company_name = 'Initech'")
        return stats

    monkeypatch.setattr("yipit_pipeline.pipeline.run_model", broken_model)
    with pytest.raises(DataQualityError):
        run_all(con, settings, lambda: fake_embedder)
    assert not settings.outputs_dir.exists()


def test_export_can_run_standalone(built, settings, tmp_path):
    counts = run_export(built, tmp_path / "again")
    assert counts["ai_articles_enriched.csv"] == 4
