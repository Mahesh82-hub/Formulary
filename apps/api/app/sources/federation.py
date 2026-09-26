"""Federated search across every upstream evidence source.

The chatbot answers from all sources at once rather than one at a time. Three properties are
load-bearing:

* **Parallel.** Sources are queried concurrently, so total latency tracks the slowest source
  that answers within its deadline, not the sum of all of them.
* **Independent.** One source failing, timing out, or being misconfigured must never remove
  another source's evidence from the answer. Every source is given the same treatment it would
  get if it were the only source being searched.
* **Attributable.** Every returned record carries its own provenance, and every source that
  was asked reports an outcome, including the ones that failed. An answer that silently
  dropped a source would look identical to one where the source had nothing to say.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.sources.fusion import DEFAULT_RRF_K, fuse_rankings, rank_by_fused_score
from app.sources.models import SourceProvenance

logger = logging.getLogger(__name__)

SourceSearchStatus = Literal["ok", "timeout", "error", "empty"]
# Characters with meaning in openFDA, PubMed, or ClinicalTrials.gov query syntax.
RESERVED = set('":()[]{}\\/+!^~*?<>')


def _plain(value: str) -> str:
    return " ".join(
        "".join(" " if character in RESERVED else character for character in value).split()
    )


@dataclass(frozen=True)
class SearchRequest:
    """What to look for, stated structurally rather than as text to be guessed at.

    ``query`` is the question in natural language, used by sources that rank by relevance.
    ``terms`` are the things every matching record must mention - a drug, a company, a
    condition - each with the alternative names it may appear under. Previously the
    application guessed these by stripping a hand-written list of filler words from the
    question; the model, which understands the question, now states them directly.
    """

    query: str
    terms: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_text(cls, text: str) -> SearchRequest:
        """A request with no stated terms: every word of the text is required."""
        words = tuple((word,) for word in _plain(text).split()[:8])
        return cls(query=" ".join(text.split()), terms=words)

    def boolean(self, *, quote: bool = True) -> str:
        """Every term required, alternatives within a term interchangeable:
        (a OR b) AND (c). Understood by openFDA, PubMed, and ClinicalTrials.gov alike."""
        groups = []
        for alternatives in self.terms:
            cleaned = [_plain(value) for value in alternatives if _plain(value)]
            if not cleaned:
                continue
            rendered = [f'"{value}"' if quote else value for value in cleaned]
            groups.append(rendered[0] if len(rendered) == 1 else "(" + " OR ".join(rendered) + ")")
        return " AND ".join(groups)


class FederatedRecord(BaseModel):
    """One piece of evidence, always attributable to the source that produced it."""

    source: str
    title: str
    snippet: str
    url: str | None = None
    external_key: str | None = None
    provenance: SourceProvenance
    fused_score: float = 0.0

    def identity(self) -> str:
        """Stable key for fusion and de-duplication within a single source."""
        return f"{self.source}:{self.external_key or self.url or self.title}"


class SourceOutcome(BaseModel):
    """What happened when one source was asked, including failures."""

    source: str
    status: SourceSearchStatus
    returned: int = 0
    elapsed_ms: int = 0
    detail: str | None = None


class FederatedSearchResult(BaseModel):
    query: str
    records: list[FederatedRecord]
    outcomes: list[SourceOutcome]
    caveats: list[str] = Field(default_factory=list)

    @property
    def succeeded_sources(self) -> list[str]:
        return [outcome.source for outcome in self.outcomes if outcome.status == "ok"]

    @property
    def failed_sources(self) -> list[str]:
        return [
            outcome.source for outcome in self.outcomes if outcome.status in ("timeout", "error")
        ]


class SourceSearcher(Protocol):
    """A single upstream source that can answer a free-text query.

    Implementations keep their own purpose-built query construction; federation deliberately
    does not flatten them into a lowest-common-denominator interface.
    """

    name: str

    async def search(self, request: SearchRequest, *, limit: int) -> list[FederatedRecord]: ...


class FederatedSearchCoordinator:
    """Fans a query out to every registered source and merges the answers."""

    def __init__(
        self,
        searchers: Sequence[SourceSearcher],
        *,
        per_source_timeout_seconds: float = 8.0,
        per_source_limit: int = 5,
        rrf_k: int = DEFAULT_RRF_K,
    ) -> None:
        if per_source_timeout_seconds <= 0:
            raise ValueError("per_source_timeout_seconds must be positive")
        if per_source_limit < 1:
            raise ValueError("per_source_limit must be at least 1")
        self._searchers = list(searchers)
        self._timeout = per_source_timeout_seconds
        self._per_source_limit = per_source_limit
        self._rrf_k = rrf_k

    async def search(
        self, request: SearchRequest | str, *, limit: int = 10
    ) -> FederatedSearchResult:
        if isinstance(request, str):
            request = SearchRequest.from_text(request)
        normalized = " ".join(request.query.split())
        if not normalized or not request.boolean():
            raise ValueError("Federated search query must not be empty")
        if not self._searchers:
            return FederatedSearchResult(query=normalized, records=[], outcomes=[])

        gathered = await asyncio.gather(
            *(self._search_one(searcher, request) for searcher in self._searchers),
        )
        relaxed_from: SearchRequest | None = None
        # Models sometimes state a term records rarely contain ("ingredients", "manufacturer").
        # Rather than guess which words are filler, drop the last-stated term and retry,
        # always keeping the first - usually the drug - so the search stays on subject.
        while (
            not any(records for _, records in gathered)
            and not any(outcome.status in ("timeout", "error") for outcome, _ in gathered)
            and len(request.terms) > 1
        ):
            relaxed_from = relaxed_from or request
            request = SearchRequest(query=request.query, terms=request.terms[:-1])
            gathered = await asyncio.gather(
                *(self._search_one(searcher, request) for searcher in self._searchers),
            )

        rankings: list[list[str]] = []
        by_identity: dict[str, FederatedRecord] = {}
        outcomes: list[SourceOutcome] = []
        for outcome, records in gathered:
            outcomes.append(outcome)
            ranking: list[str] = []
            for record in records:
                identity = record.identity()
                # First occurrence wins: a source's own ordering is its relevance opinion.
                by_identity.setdefault(identity, record)
                ranking.append(identity)
            if ranking:
                rankings.append(ranking)

        scores = fuse_rankings(rankings, k=self._rrf_k)
        selected = rank_by_fused_score(scores, limit=limit)
        records = []
        for identity in selected:
            record = by_identity[identity]
            records.append(record.model_copy(update={"fused_score": scores[identity]}))

        caveats = self._caveats(outcomes)
        if relaxed_from is not None:
            dropped = [group[0] for group in relaxed_from.terms[len(request.terms) :]]
            caveats.append(
                f"No record mentioned every stated term; the search was relaxed by dropping "
                f"{', '.join(repr(term) for term in dropped)}. Results may be broader than asked."
            )
        return FederatedSearchResult(
            query=normalized,
            records=records,
            outcomes=outcomes,
            caveats=caveats,
        )

    async def _search_one(
        self,
        searcher: SourceSearcher,
        request: SearchRequest,
    ) -> tuple[SourceOutcome, list[FederatedRecord]]:
        """Query one source, converting any failure into a reported outcome.

        This never raises: a source that blows up must not take the whole answer with it.
        """
        started = time.monotonic()
        try:
            records = await asyncio.wait_for(
                searcher.search(request, limit=self._per_source_limit),
                timeout=self._timeout,
            )
        except TimeoutError:
            return (
                SourceOutcome(
                    source=searcher.name,
                    status="timeout",
                    elapsed_ms=self._elapsed_ms(started),
                    detail=f"Did not respond within {self._timeout:g}s",
                ),
                [],
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Federated search source %s failed: %s", searcher.name, error, exc_info=error
            )
            return (
                SourceOutcome(
                    source=searcher.name,
                    status="error",
                    elapsed_ms=self._elapsed_ms(started),
                    # Upstream messages are already safe application errors; the class name is
                    # a last-resort fallback that cannot leak a URL or key.
                    detail=str(error) or type(error).__name__,
                ),
                [],
            )

        elapsed_ms = self._elapsed_ms(started)
        if not records:
            return (
                SourceOutcome(source=searcher.name, status="empty", elapsed_ms=elapsed_ms),
                [],
            )
        return (
            SourceOutcome(
                source=searcher.name,
                status="ok",
                returned=len(records),
                elapsed_ms=elapsed_ms,
            ),
            records,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    @staticmethod
    def _caveats(outcomes: Sequence[SourceOutcome]) -> list[str]:
        caveats: list[str] = []
        degraded = [outcome for outcome in outcomes if outcome.status in ("timeout", "error")]
        if degraded:
            names = ", ".join(sorted(outcome.source for outcome in degraded))
            caveats.append(
                f"This answer is incomplete: {names} could not be reached, so evidence held "
                "only by those sources is missing. Say so when answering."
            )
        return caveats
