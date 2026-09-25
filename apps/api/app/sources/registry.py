"""Assembles the set of sources a federated question is asked against.

Adding a source means adding it here. Nothing else in the chat path needs to change: the
coordinator queries whatever is registered, concurrently, and reports on each one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.clinicaltrials.client import ClinicalTrialsClient
from app.clinicaltrials.search import ClinicalTrialsSearcher
from app.core.config import Settings
from app.fda.client import OpenFDAClient
from app.fda.search import build_openfda_searchers
from app.ingestion.service import FDAIngestionCoordinator
from app.pubmed.client import PubMedClient
from app.pubmed.search import PubMedSearcher
from app.sources.federation import (
    FederatedRecord,
    FederatedSearchCoordinator,
    SourceSearcher,
)
from app.sources.models import SourceProvenance

LOCAL_EVIDENCE_SOURCE_NAME = "Previously ingested evidence"
LOCAL_SNIPPET_MAX_CHARACTERS = 600


class IngestedEvidenceSearcher:
    """Searches the local corpus of previously ingested documents.

    This source *enriches* an answer; it never replaces a live query. Anything it returns was
    true when it was ingested, which may no longer hold for fast-moving datasets such as
    shortages and recalls, so its records are labelled with their ingestion date.
    """

    name = LOCAL_EVIDENCE_SOURCE_NAME

    def __init__(self, coordinator: FDAIngestionCoordinator) -> None:
        self._coordinator = coordinator

    async def search(self, query: str, *, limit: int) -> list[FederatedRecord]:
        result = await self._coordinator.search_chunks(query, limit=limit)
        records: list[FederatedRecord] = []
        for chunk in result.chunks:
            snippet = chunk.content.strip()
            if len(snippet) > LOCAL_SNIPPET_MAX_CHARACTERS:
                snippet = snippet[: LOCAL_SNIPPET_MAX_CHARACTERS - 1].rstrip() + "…"
            records.append(
                FederatedRecord(
                    source=self.name,
                    title=f"{chunk.title} ({readable_section(chunk.section_path)})",
                    snippet=snippet,
                    url=chunk.source_url,
                    external_key=f"{chunk.dataset}:{chunk.external_key}:{chunk.chunk_index}",
                    provenance=SourceProvenance(
                        source=f"{self.name} - {chunk.dataset}",
                        api_url=chunk.source_url or chunk.external_key,
                        retrieved_at=datetime.now(UTC),
                        dataset_last_updated=chunk.dataset_last_updated,
                        disclaimer=(
                            "Served from the local ingested corpus. Confirm against a live "
                            "source before relying on it for time-sensitive information."
                        ),
                    ),
                )
            )
        return records


def readable_section(path: str) -> str:
    """Turn a JSON path such as "$.pages[1].text" into "page 1"."""
    import re

    page = re.search(r"pages\[(\d+)\]", path)
    if page:
        return f"page {page.group(1)}"
    cleaned = re.sub(r"^\$\.?", "", path).replace("_", " ").replace(".", " > ")
    return cleaned or "document"


def build_source_searchers(
    fda: OpenFDAClient,
    ingestion: FDAIngestionCoordinator | None = None,
    pubmed: PubMedClient | None = None,
    clinicaltrials: ClinicalTrialsClient | None = None,
) -> list[SourceSearcher]:
    """Assemble every source a federated question is asked against.

    Order carries no meaning: the coordinator queries them concurrently and merges by rank.
    """
    searchers: list[SourceSearcher] = list(build_openfda_searchers(fda))
    if pubmed is not None:
        searchers.append(PubMedSearcher(pubmed))
    if clinicaltrials is not None:
        searchers.append(ClinicalTrialsSearcher(clinicaltrials))
    if ingestion is not None:
        searchers.append(IngestedEvidenceSearcher(ingestion))
    return searchers


def build_federated_coordinator(
    fda: OpenFDAClient,
    ingestion: FDAIngestionCoordinator | None,
    settings: Settings,
    pubmed: PubMedClient | None = None,
    clinicaltrials: ClinicalTrialsClient | None = None,
) -> FederatedSearchCoordinator:
    return FederatedSearchCoordinator(
        build_source_searchers(fda, ingestion, pubmed, clinicaltrials),
        per_source_timeout_seconds=settings.federated_per_source_timeout_seconds,
        per_source_limit=settings.federated_per_source_limit,
        rrf_k=settings.retrieval_rrf_k,
    )
