"""Company resolution, aliases, fuzzy suggestions and derived attributes."""

import pytest
from rapidfuzz import fuzz

from yipit_pipeline.company import (
    METHOD_ALIAS,
    METHOD_EXACT,
    METHOD_NORMALIZED,
    REASON_ABSENT,
    REASON_MISSING_NAME,
    REASON_MULTI_ENTITY,
    REASON_PRODUCT,
    STATUS_UNMATCHED,
    CompanyResolver,
    MetadataKeyCollisionError,
    company_age,
    company_id_for,
    company_key,
    fuzzy_key,
    size_category,
    suggest_matches,
)

METADATA = [
    "Amazon Web Services",
    "Google DeepMind",
    "Meta AI",
    "Microsoft",
    "Acme Robotics Inc.",
    "Globex Cloud",
]


@pytest.fixture
def resolver():
    return CompanyResolver(METADATA)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("Amazon Web Services (AWS)", "amazonwebservices"),
        ("Stripe Inc.", "stripe"),
        ("Stripe Inc", "stripe"),
        ("Palantir Technologies", "palantir"),
        ("NVIDIA Corporation", "nvidia"),
        ("Foo Corp. Ltd.", "foo"),  # repeated suffixes
        ("Mongo DB", "mongodb"),
        ("  CloudFlare ", "cloudflare"),
        ("Open AI", "openai"),
        ("The Boring Company / SpaceX", "theboringcompanyspacex"),
        ("Incorporated Systems", "incorporatedsystems"),  # suffix only stripped at the end
        ("", None),
        (None, None),
    ],
)
def test_company_key(raw, key):
    assert company_key(raw) == key


def test_fuzzy_key_keeps_word_boundaries():
    assert fuzzy_key("Mistral AI (Paris) Inc.") == "mistral ai"


def test_exact(resolver):
    result = resolver.resolve("Globex Cloud")
    assert result.method == METHOD_EXACT
    assert result.company_name == "Globex Cloud"
    assert result.company_id == company_id_for("globexcloud")


@pytest.mark.parametrize(
    ("raw", "target"),
    [
        ("ACME Robotics", "Acme Robotics Inc."),
        ("Acme Robotics", "Acme Robotics Inc."),
        ("Amazon Web Services (AWS)", "Amazon Web Services"),
        ("Google Deepmind", "Google DeepMind"),
        ("globex  cloud", "Globex Cloud"),
    ],
)
def test_normalized(resolver, raw, target):
    result = resolver.resolve(raw)
    assert result.method == METHOD_NORMALIZED
    assert result.company_name == target


@pytest.mark.parametrize(
    ("raw", "target"),
    [
        ("AWS", "Amazon Web Services"),
        ("DeepMind", "Google DeepMind"),
        ("Meta AI Research", "Meta AI"),
        ("Facebook AI Research", "Meta AI"),
        ("FAIR", "Meta AI"),
    ],
)
def test_alias(resolver, raw, target):
    result = resolver.resolve(raw)
    assert result.method == METHOD_ALIAS
    assert result.company_name == target
    assert result.company_id == company_id_for(company_key(target))


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("Azure", REASON_PRODUCT),
        ("Microsoft Azure", REASON_PRODUCT),  # product is not merged into the parent
        ("The Boring Company / SpaceX", REASON_MULTI_ENTITY),
        ("Zyx Labs", REASON_ABSENT),
        ("", REASON_MISSING_NAME),
        (None, REASON_MISSING_NAME),
    ],
)
def test_unmatched(resolver, raw, reason):
    result = resolver.resolve(raw)
    assert result.status == STATUS_UNMATCHED
    assert result.company_id is None
    assert result.unmatched_reason == reason


def test_alias_to_company_missing_from_metadata_stays_unmatched():
    result = CompanyResolver(["Globex Cloud"]).resolve("AWS")
    assert result.company_id is None


def test_metadata_key_collision_is_detected():
    with pytest.raises(MetadataKeyCollisionError):
        CompanyResolver(["Stripe", "Stripe Inc."])


def test_company_id_is_deterministic():
    assert company_id_for("acmerobotics") == company_id_for("acmerobotics")
    assert len(company_id_for("acmerobotics")) == 16


def test_suggestions_never_assign_and_respect_rejections(resolver):
    unresolved = {"Microsoft Azure": 3, "Globex Clowd": 2, "Zzzz": 1}
    suggestions = {s.company_name_raw: s for s in suggest_matches(unresolved, METADATA)}
    # Typo gets a pending suggestion, but the resolver still does not match it.
    assert suggestions["Globex Clowd"].candidate_company_name == "Globex Cloud"
    assert suggestions["Globex Clowd"].status == "pending"
    assert suggestions["Globex Clowd"].n_articles == 2
    assert resolver.resolve("Globex Clowd").company_id is None
    # Decided product name is listed as rejected, not pending.
    assert suggestions["Microsoft Azure"].status == "rejected"
    assert suggestions["Microsoft Azure"].review_note == REASON_PRODUCT
    # Below the score floor: no row at all.
    assert "Zzzz" not in suggestions


def test_scorer_does_not_score_subsets_as_perfect():
    # token_set_ratio / partial_ratio would return 100 here; token_sort_ratio does not.
    assert fuzz.token_set_ratio("microsoft azure", "microsoft") == 100
    assert fuzz.token_sort_ratio("microsoft azure", "microsoft") < 100


@pytest.mark.parametrize(
    ("employees", "category"),
    [
        (0, "Small"),
        (9_999, "Small"),
        (10_000, "Medium"),
        (30_000, "Medium"),
        (30_001, "Large"),
        (None, None),
    ],
)
def test_size_category_boundaries(employees, category):
    assert size_category(employees) == category


@pytest.mark.parametrize(
    ("published", "founded", "age"),
    [(2023, 2010, 13), (2023, 2023, 0), (2021, 2023, None), (None, 2010, None), (2023, None, None)],
)
def test_company_age(published, founded, age):
    assert company_age(published, founded) == age
