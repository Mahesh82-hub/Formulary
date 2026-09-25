"""Decides whether a detected event is relevant to a watch."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Protocol

from app.intelligence.types import at_least

MAX_TERMS = 25
MAX_TERM_LENGTH = 100


class MatchableEvent(Protocol):
    event_type: str
    significance: str
    headline: str
    subject: str
    sponsor: str | None
    drug_names: list[str]


def normalize_terms(terms: Iterable[str]) -> list[str]:
    """Trim, de-duplicate case-insensitively, and bound the terms a watch follows."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for term in terms:
        value = " ".join(term.split())
        if not value or len(value) > MAX_TERM_LENGTH or value.casefold() in seen:
            continue
        seen.add(value.casefold())
        cleaned.append(value)
    return cleaned[:MAX_TERMS]


def _pattern(term: str) -> re.Pattern[str]:
    # Whole-word matching: a watch on "insulin" must not fire for "insulinoma".
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def event_matches(
    event: MatchableEvent,
    *,
    terms: Sequence[str],
    event_types: Sequence[str],
    min_significance: str,
) -> bool:
    if not at_least(event.significance, min_significance):
        return False
    if event_types and event.event_type not in event_types:
        return False
    haystack = " \n".join([*event.drug_names, event.subject, event.sponsor or "", event.headline])
    return any(_pattern(term).search(haystack) for term in terms)
