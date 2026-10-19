"""Map raw article categories (and company industries) to one taxonomy.

Two layers (ADR-005):
1. ``category_key``: formatting only (lowercase, trim, keep ``[a-z0-9]``).
2. ``CATEGORY_BUCKETS``: explicit key -> bucket dictionary that carries the meaning.
Synonyms are merged in the dictionary, never by loosening the key regex.
Unknown values map to ``UNMAPPED`` so new source labels surface as data-quality drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNMAPPED = "UNMAPPED"

STATUS_MAPPED = "mapped"
STATUS_UNMAPPED = "unmapped"
STATUS_MISSING = "missing"

CATEGORY_BUCKETS: dict[str, str] = {
    # AI and ML are treated as one subject (ADR-005).
    "artificialintelligence": "AI_ML",
    "aiml": "AI_ML",  # AI/ML, AI & ML
    "machinelearning": "AI_ML",
    "analytics": "ANALYTICS",
    "dataanalytics": "ANALYTICS",
    "bigdata": "BIG_DATA",
    "cloud": "CLOUD",
    "cloudcomputing": "CLOUD",
    "cloudservices": "CLOUD",
    "financialtechnology": "FINTECH",
    "fintech": "FINTECH",
    "finance": "FINANCE",
    "cybersecurity": "CYBERSECURITY",
    "infosec": "CYBERSECURITY",
    "security": "SECURITY",
    "software": "SOFTWARE",
    "enterprisesoftware": "ENTERPRISE_SOFTWARE",
    "saas": "SAAS",
}

_MISSING_KEYS = frozenset({"", "null", "none", "nan", "na"})


@dataclass(frozen=True)
class CategoryResult:
    """Outcome of mapping one raw category label."""

    key: str | None
    bucket: str | None
    status: str


def category_key(raw: object) -> str | None:
    """Formatting-only key: lowercase, trim, keep ``[a-z0-9]``. ``None`` stays ``None``."""
    if raw is None:
        return None
    return re.sub(r"[^a-z0-9]", "", str(raw).strip().lower())


def map_category(raw: object) -> CategoryResult:
    """Map a raw label to its bucket; unknown labels -> ``UNMAPPED``, empty -> missing."""
    key = category_key(raw)
    if key is None or key in _MISSING_KEYS:
        return CategoryResult(key or None, None, STATUS_MISSING)
    bucket = CATEGORY_BUCKETS.get(key)
    if bucket is None:
        return CategoryResult(key, UNMAPPED, STATUS_UNMAPPED)
    return CategoryResult(key, bucket, STATUS_MAPPED)
