"""ClinicalTrials.gov adapter for federated search."""

from __future__ import annotations

from app.clinicaltrials.client import ClinicalTrialsClient
from app.sources.federation import FederatedRecord, SearchRequest

SNIPPET_MAX_CHARACTERS = 900


class ClinicalTrialsSearcher:
    """Searches registered clinical studies."""

    name = "ClinicalTrials.gov"

    def __init__(self, client: ClinicalTrialsClient) -> None:
        self._client = client

    async def search(self, request: SearchRequest, *, limit: int) -> list[FederatedRecord]:
        query = request.boolean() or request.query
        page = await self._client.search(query, limit=limit)
        provenance = self._client.provenance(page.api_url)
        records: list[FederatedRecord] = []
        for trial in page.studies:
            facts = [trial.phase_label, trial.status_label]
            if trial.sponsor:
                facts.append(f"Sponsor: {trial.sponsor}")
            if trial.conditions:
                facts.append(f"Conditions: {', '.join(trial.conditions[:3])}")
            if trial.why_stopped:
                facts.append(f"Stopped because: {trial.why_stopped}")
            snippet = " · ".join(facts)
            if trial.brief_summary:
                snippet = f"{snippet} - {trial.brief_summary}"
            if len(snippet) > SNIPPET_MAX_CHARACTERS:
                snippet = snippet[: SNIPPET_MAX_CHARACTERS - 1].rstrip() + "…"
            records.append(
                FederatedRecord(
                    source=self.name,
                    title=f"{trial.nct_id}: {trial.title}",
                    snippet=snippet,
                    url=trial.url,
                    external_key=f"nct:{trial.nct_id}",
                    # Cite the study page itself, not the search that surfaced it.
                    provenance=provenance.model_copy(update={"api_url": trial.url}),
                )
            )
        return records
