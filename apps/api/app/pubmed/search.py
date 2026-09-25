"""PubMed adapter for federated search."""

from __future__ import annotations

from app.pubmed.client import PubMedClient
from app.sources.federation import FederatedRecord

SNIPPET_MAX_CHARACTERS = 900


class PubMedSearcher:
    """Searches published literature indexed by PubMed."""

    name = "PubMed"

    def __init__(self, client: PubMedClient) -> None:
        self._client = client

    async def search(self, query: str, *, limit: int) -> list[FederatedRecord]:
        articles = await self._client.search(query, limit=limit)
        api_url = self._client.public_url(
            "esearch.fcgi",
            {"db": "pubmed", "term": query, "retmode": "json"},
        )
        provenance = self._client.provenance(api_url)

        records: list[FederatedRecord] = []
        for article in articles:
            snippet = article.abstract
            if len(snippet) > SNIPPET_MAX_CHARACTERS:
                snippet = snippet[: SNIPPET_MAX_CHARACTERS - 1].rstrip() + "…"
            records.append(
                FederatedRecord(
                    source=self.name,
                    title=article.title,
                    snippet=f"{article.citation()} - {snippet}",
                    url=article.url,
                    external_key=f"pubmed:{article.pmid}",
                    # Each article carries its own landing page so a citation points at the
                    # article, not at the search that found it.
                    provenance=provenance.model_copy(update={"api_url": article.url}),
                )
            )
        return records
