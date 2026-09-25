"""Shared types for change detection.

A detector is pure with respect to storage: it reads baselines through ``SnapshotReader`` and
returns the events it found together with the baseline updates it wants. The monitor persists
both in a single transaction. That ordering matters - if a baseline were saved without its
event, the change it represented would be lost for good, because the next run would compare
against the already-updated baseline and see nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

EventType = Literal[
    "new_drug_approval",
    "generic_approval",
    "new_indication",
    "manufacturing_change",
    "safety_program_change",
    "bioequivalence_supplement",
    "labeling_supplement",
    "other_supplement",
    "label_change",
    "trial_status_change",
    "trial_registered",
    "trial_results_posted",
]
Significance = Literal["high", "medium", "low"]

SIGNIFICANCE_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def at_least(significance: str, minimum: str) -> bool:
    return SIGNIFICANCE_RANK.get(significance, 0) >= SIGNIFICANCE_RANK.get(minimum, 0)


@dataclass(frozen=True)
class EventDraft:
    """A detected change, not yet persisted."""

    dedup_key: str
    source: str
    event_type: EventType
    significance: Significance
    headline: str
    summary: str
    subject: str
    occurred_on: date
    source_url: str
    drug_names: tuple[str, ...] = ()
    sponsor: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotUpdate:
    source: str
    entity_key: str
    state: Mapping[str, Any]

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.state)


@dataclass
class DetectionResult:
    events: list[EventDraft] = field(default_factory=list)
    snapshots: list[SnapshotUpdate] = field(default_factory=list)
    records_scanned: int = 0
    # Records seen for the first time. They establish a baseline but cannot yet say what
    # changed, so they produce no event.
    baselines_recorded: int = 0


class SnapshotReader(Protocol):
    async def get_many(self, source: str, keys: Iterable[str]) -> dict[str, Mapping[str, Any]]:
        """Return the stored state for each key that has a baseline."""
        ...


class InMemorySnapshots:
    """Baseline store for tests and one-off analysis."""

    def __init__(self, initial: dict[tuple[str, str], Mapping[str, Any]] | None = None) -> None:
        self._states: dict[tuple[str, str], Mapping[str, Any]] = dict(initial or {})

    async def get_many(self, source: str, keys: Iterable[str]) -> dict[str, Mapping[str, Any]]:
        return {key: self._states[(source, key)] for key in keys if (source, key) in self._states}

    def apply(self, updates: Iterable[SnapshotUpdate]) -> None:
        for update in updates:
            self._states[(update.source, update.entity_key)] = update.state


class ChangeDetector(Protocol):
    """Finds what changed in one upstream dataset over a date window."""

    name: str
    # How far back each run re-reads beyond the previous window. Sources publish with a lag -
    # an approval dated the 17th can appear in openFDA on the 22nd - so windows overlap, and
    # dedup keys make the overlap free.
    overlap_days: int

    async def detect(
        self, *, since: date, until: date, snapshots: SnapshotReader
    ) -> DetectionResult: ...


def fingerprint(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """Hash text after collapsing whitespace, so reflowed paragraphs are not a change."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def dedupe_names(names: Iterable[str | None], *, limit: int = 10) -> tuple[str, ...]:
    """Case-insensitive de-duplication that keeps the first spelling seen."""
    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if not name:
            continue
        cleaned = " ".join(name.split())
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return tuple(result)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
