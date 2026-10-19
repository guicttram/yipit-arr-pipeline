"""Company name resolution against metadata (ADR-006, ADR-007, ADR-008).

Resolution order for an article's raw company name:
1. ``exact``      raw name (trimmed) equals a metadata company name;
2. ``normalized`` ``company_key`` equals a metadata company key;
3. ``alias``      ``company_key`` is in the versioned alias table;
otherwise unmatched with a reason. Fuzzy matching only *suggests* (never assigns).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from yipit_pipeline.config import SIZE_MEDIUM_MAX, SIZE_MEDIUM_MIN

METHOD_EXACT = "exact"
METHOD_NORMALIZED = "normalized"
METHOD_ALIAS = "alias"

STATUS_MATCHED = "matched"
STATUS_UNMATCHED = "unmatched"

REASON_ABSENT = "absent_from_metadata"
REASON_PRODUCT = "product_not_company"
REASON_MULTI_ENTITY = "multi_entity"
REASON_MISSING_NAME = "missing_name"

# alias key (company_key of the alias) -> canonical metadata company name (ADR-007).
ALIASES: dict[str, str] = {
    "aws": "Amazon Web Services",
    "deepmind": "Google DeepMind",
    "metaairesearch": "Meta AI",
    "facebookairesearch": "Meta AI",
    "fair": "Meta AI",
}

# Names deliberately left unmatched (ADR-007), keyed by company_key.
KNOWN_UNMATCHED: dict[str, str] = {
    "azure": REASON_PRODUCT,
    "microsoftazure": REASON_PRODUCT,
    "theboringcompanyspacex": REASON_MULTI_ENTITY,
}

# Fuzzy suggestions already reviewed and rejected (ADR-008): not re-suggested as pending.
KNOWN_REJECTIONS: dict[str, str] = {
    **KNOWN_UNMATCHED,
    "cohere": REASON_ABSENT,
    "xai": REASON_ABSENT,
    "mistralai": REASON_ABSENT,
    "huggingface": REASON_ABSENT,
    "perplexityai": REASON_ABSENT,
}

FUZZY_SCORER_NAME = "token_sort_ratio"
FUZZY_SCORE_FLOOR = 60.0

_PARENTHETICAL_RE = re.compile(r"\s*\([^)]*\)")
_SUFFIX_RE = re.compile(r"(\s+(inc|corp|corporation|ltd|llc|technologies)\.?)+$")


class MetadataKeyCollisionError(ValueError):
    """Two metadata companies normalize to the same key, so matching would be ambiguous."""


def _strip_name(name: str) -> str:
    text = name.strip().lower()
    text = _PARENTHETICAL_RE.sub("", text).strip()
    return _SUFFIX_RE.sub("", text)


def company_key(name: object) -> str | None:
    """Matching key (ADR-006): lowercase/trim, drop parentheticals and legal suffixes,
    keep ``[a-z0-9]``. Returns ``None`` for missing or empty names."""
    if name is None:
        return None
    key = re.sub(r"[^a-z0-9]", "", _strip_name(str(name)))
    return key or None


def fuzzy_key(name: str) -> str:
    """Like ``company_key`` but keeps word boundaries as single spaces (for token scorers)."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", _strip_name(name))).strip()


def company_id_for(key: str) -> str:
    """Deterministic surrogate key: short SHA-256 of the normalized company key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class MatchResult:
    """How one raw article company name resolved against metadata."""

    company_name: str | None  # canonical metadata name
    company_id: str | None
    status: str
    method: str | None = None
    unmatched_reason: str | None = None


class CompanyResolver:
    """Resolves raw company names against a fixed set of metadata company names."""

    def __init__(
        self,
        metadata_names: Iterable[str],
        aliases: Mapping[str, str] | None = None,
        known_unmatched: Mapping[str, str] | None = None,
    ) -> None:
        self.names = sorted(set(metadata_names))
        self.by_key: dict[str, str] = {}
        for name in self.names:
            key = company_key(name)
            if key is None:
                raise MetadataKeyCollisionError(f"metadata company name {name!r} has an empty key")
            if key in self.by_key:
                raise MetadataKeyCollisionError(
                    f"metadata companies {self.by_key[key]!r} and {name!r} share key {key!r}"
                )
            self.by_key[key] = name
        self.aliases = dict(ALIASES if aliases is None else aliases)
        self.known_unmatched = dict(KNOWN_UNMATCHED if known_unmatched is None else known_unmatched)

    def _matched(self, name: str, method: str) -> MatchResult:
        return MatchResult(name, company_id_for(company_key(name)), STATUS_MATCHED, method)  # type: ignore[arg-type]

    def resolve(self, raw_name: object) -> MatchResult:
        """Resolve one raw name: exact -> normalized -> alias -> unmatched (with reason)."""
        key = company_key(raw_name)
        if key is None:
            return MatchResult(None, None, STATUS_UNMATCHED, None, REASON_MISSING_NAME)
        stripped = str(raw_name).strip()
        if stripped in self.names:
            return self._matched(stripped, METHOD_EXACT)
        if key in self.by_key:
            return self._matched(self.by_key[key], METHOD_NORMALIZED)
        target = self.aliases.get(key)
        if target is not None and company_key(target) in self.by_key:
            return self._matched(self.by_key[company_key(target)], METHOD_ALIAS)  # type: ignore[index]
        reason = self.known_unmatched.get(key, REASON_ABSENT)
        return MatchResult(None, None, STATUS_UNMATCHED, None, reason)


@dataclass(frozen=True)
class Suggestion:
    """A fuzzy-match proposal for human review. Never used to assign a company_id."""

    company_name_raw: str
    company_key: str
    candidate_company_name: str
    score: float
    scorer: str
    n_articles: int
    status: str  # pending | rejected (approved suggestions move to ALIASES)
    review_note: str | None


def suggest_matches(
    unresolved_names: Mapping[str, int],
    metadata_names: Iterable[str],
    known_rejections: Mapping[str, str] | None = None,
    score_floor: float = FUZZY_SCORE_FLOOR,
) -> list[Suggestion]:
    """Best metadata candidate per unresolved raw name (one row per distinct name).

    ``unresolved_names`` maps raw name -> number of articles. Candidates below
    ``score_floor`` are dropped as noise; names in ``known_rejections`` are emitted with
    status ``rejected`` so reviewers see them but they are not pending again.
    """
    rejections = KNOWN_REJECTIONS if known_rejections is None else known_rejections
    choices = {name: fuzzy_key(name) for name in sorted(set(metadata_names))}
    suggestions: list[Suggestion] = []
    for raw_name in sorted(unresolved_names):
        key = company_key(raw_name)
        if key is None:
            continue
        best = process.extractOne(
            fuzzy_key(raw_name), choices, scorer=fuzz.token_sort_ratio, score_cutoff=score_floor
        )
        if best is None:
            continue
        _, score, candidate = best
        rejected_reason = rejections.get(key)
        suggestions.append(
            Suggestion(
                company_name_raw=raw_name,
                company_key=key,
                candidate_company_name=candidate,
                score=round(float(score), 2),
                scorer=FUZZY_SCORER_NAME,
                n_articles=unresolved_names[raw_name],
                status="rejected" if rejected_reason else "pending",
                review_note=rejected_reason,
            )
        )
    return suggestions


def size_category(employee_count: int | None) -> str | None:
    """Small < 10,000; Medium 10,000-30,000 inclusive; Large > 30,000; None stays None."""
    if employee_count is None:
        return None
    if employee_count < SIZE_MEDIUM_MIN:
        return "Small"
    if employee_count <= SIZE_MEDIUM_MAX:
        return "Medium"
    return "Large"


def company_age(published_year: int | None, founded_year: int | None) -> int | None:
    """Age in whole years at publication; negative or unknown -> None (flagged upstream)."""
    if published_year is None or founded_year is None:
        return None
    age = published_year - founded_year
    return age if age >= 0 else None
