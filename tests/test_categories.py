"""Category taxonomy (ADR-005, ADR-009)."""

import pytest

from yipit_pipeline.cleaning.categories import (
    CATEGORY_BUCKETS,
    STATUS_MAPPED,
    STATUS_MISSING,
    STATUS_UNMAPPED,
    UNMAPPED,
    category_key,
    map_category,
)

# The 19 raw labels observed in the source and their expected bucket.
OBSERVED = {
    "Cloud Computing": "CLOUD",
    "Artificial Intelligence": "AI_ML",
    "Financial Technology": "FINTECH",
    "Big Data": "BIG_DATA",
    "Analytics": "ANALYTICS",
    "Data Analytics": "ANALYTICS",
    "AI/ML": "AI_ML",
    "FinTech": "FINTECH",
    "Software": "SOFTWARE",
    "Cybersecurity": "CYBERSECURITY",
    "Enterprise Software": "ENTERPRISE_SOFTWARE",
    "Finance": "FINANCE",
    "AI & ML": "AI_ML",
    "Cloud": "CLOUD",
    "SaaS": "SAAS",
    "InfoSec": "CYBERSECURITY",
    "Security": "SECURITY",
    "Machine Learning": "AI_ML",
    "Cloud Services": "CLOUD",
}


@pytest.mark.parametrize(("raw", "bucket"), sorted(OBSERVED.items()))
def test_observed_labels(raw, bucket):
    result = map_category(raw)
    assert result.status == STATUS_MAPPED
    assert result.bucket == bucket


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("AI/ML", "aiml"),
        ("AI & ML", "aiml"),
        ("  ai / ml ", "aiml"),
        ("Big Data", "bigdata"),
        ("Data Analytics", "dataanalytics"),
        ("Cloud (Public)", "cloudpublic"),  # no parenthetical rule for categories
        (None, None),
    ],
)
def test_key_is_formatting_only(raw, key):
    assert category_key(raw) == key


def test_formatting_variants_map_like_the_original():
    assert map_category("machine  learning").bucket == "AI_ML"
    assert map_category("FINTECH").bucket == "FINTECH"
    assert map_category("Info-Sec").bucket == "CYBERSECURITY"


def test_big_data_is_not_merged_with_analytics():
    assert map_category("Big Data").bucket != map_category("Data Analytics").bucket


def test_broad_labels_stay_separate():
    assert map_category("Finance").bucket != map_category("FinTech").bucket
    assert map_category("Security").bucket != map_category("Cybersecurity").bucket
    assert map_category("Software").bucket != map_category("SaaS").bucket


def test_unknown_label_is_unmapped_not_other():
    result = map_category("Quantum Computing")
    assert result.status == STATUS_UNMAPPED
    assert result.bucket == UNMAPPED
    assert result.key == "quantumcomputing"


@pytest.mark.parametrize("raw", [None, "", "  ", "NULL"])
def test_missing(raw):
    result = map_category(raw)
    assert result.status == STATUS_MISSING
    assert result.bucket is None


def test_metadata_industries_share_the_taxonomy():
    for industry, bucket in {
        "AI/ML": "AI_ML",
        "Data Analytics": "ANALYTICS",
        "Cloud Computing": "CLOUD",
        "Cybersecurity": "CYBERSECURITY",
        "SaaS": "SAAS",
        "FinTech": "FINTECH",
    }.items():
        assert map_category(industry).bucket == bucket


def test_every_bucket_key_is_already_a_key():
    for key in CATEGORY_BUCKETS:
        assert category_key(key) == key
