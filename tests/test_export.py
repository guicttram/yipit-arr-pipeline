"""ai_articles_enriched.csv: filter edge cases, columns and formats."""

import csv
import json

import pytest

from yipit_pipeline.export import AI_EXPORT_COLUMNS, AI_EXPORT_FILE


@pytest.fixture
def exported(built, settings):
    with (settings.outputs_dir / AI_EXPORT_FILE).open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, {row["article_id"]: row for row in reader}


def test_columns_in_required_order(exported):
    header, _ = exported
    assert tuple(header) == AI_EXPORT_COLUMNS


def test_exact_selection(exported):
    _, rows = exported
    assert set(rows) == {"T001", "T002", "T006", "T016"}


@pytest.mark.parametrize(
    ("article_id", "why"),
    [
        ("T001", "2022-01-01 boundary, AI by category and industry"),
        ("T002", "2024-12-31 boundary, AI by industry only, EUR converted > 50M"),
        ("T006", "AI by category only, industry is Cloud Computing"),
        ("T016", "exact company match, empty summary"),
    ],
)
def test_included(exported, article_id, why):
    assert article_id in exported[1], why


@pytest.mark.parametrize(
    ("article_id", "why"),
    [
        ("T003", "ARR exactly $50M is not > $50M"),
        ("T004", "2021-12-31 is before the window"),
        ("T005", "2025-01-01 is after the window"),
        ("T007", "company not in metadata: no ARR observation"),
        ("T011", "not AI by category nor industry"),
        ("T015", "unparseable ARR"),
    ],
)
def test_excluded(exported, article_id, why):
    assert article_id not in exported[1], why


def test_values_and_formats(exported):
    _, rows = exported
    t002 = rows["T002"]
    assert t002["arr_usd"] == "55000000"  # €50,000,000 x 1.1
    assert t002["category"] == "SOFTWARE"
    assert t002["industry"] == "AI/ML"
    assert t002["company_name"] == "Acme Robotics Inc."  # canonical metadata name
    assert t002["published_date"] == "2024-12-31"
    assert t002["company_age"] == "14"
    assert t002["company_size_category"] == "Medium"
    assert t002["is_public"] == "true"
    embedding = json.loads(t002["embedding"])
    assert len(embedding) == 384 and all(isinstance(v, float) for v in embedding)
    neighbours = json.loads(t002["top_similar_articles"])
    assert len(neighbours) == 3 and "T002" not in neighbours


def test_neighbours_are_not_limited_to_exported_rows(exported):
    _, rows = exported
    neighbours = {n for row in rows.values() for n in json.loads(row["top_similar_articles"])}
    assert neighbours - set(rows), "neighbours are computed over all articles"


def test_one_csv_per_modeled_table(built, settings):
    names = {p.name for p in settings.outputs_dir.glob("*.csv")}
    for table in [
        "dim_company",
        "dim_company_alias",
        "fact_article",
        "fact_arr_observation",
        "article_embedding",
        "article_similarity",
        "dq_issues",
        "dq_check_results",
        "unmatched_companies",
        "company_match_suggestions",
        "v_company_arr_timeline",
        "v_company_arr_latest",
        "v_company_arr_quarterly",
    ]:
        assert f"{table}.csv" in names
    assert not list(settings.outputs_dir.glob("*.tmp"))
