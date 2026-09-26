"""openFDA adapters for federated search.

Each openFDA dataset registers as its own searcher rather than hiding behind a single "FDA"
entry. Datasets answer genuinely different questions - a label is not a recall - so treating
them as separate sources lets federation query them concurrently and lets an answer cite the
specific dataset a claim came from.
"""

from __future__ import annotations

from typing import Any

from app.fda.client import OpenFDAClient, OpenFDAError
from app.fda.models import OpenFDADataset
from app.sources.federation import FederatedRecord, SearchRequest

# Datasets searched for a general question, with the fields that best serve as a title and a
# snippet. Order is irrelevant: federation queries them concurrently.
DatasetSpec = tuple[OpenFDADataset, str, tuple[str, ...], tuple[str, ...]]
GENERAL_SEARCH_DATASETS: tuple[DatasetSpec, ...] = (
    (
        "drug/label",
        "FDA drug label",
        ("openfda.brand_name", "openfda.generic_name", "openfda.substance_name"),
        ("indications_and_usage", "description", "clinical_pharmacology"),
    ),
    (
        "drug/drugsfda",
        "FDA drug approval",
        ("openfda.brand_name", "openfda.generic_name", "sponsor_name"),
        ("application_number", "sponsor_name"),
    ),
    (
        "drug/shortages",
        "FDA drug shortage",
        ("generic_name", "company_name"),
        ("status", "availability", "shortage_reason"),
    ),
    (
        "drug/enforcement",
        "FDA drug recall",
        ("product_description", "recalling_firm"),
        ("reason_for_recall", "status", "classification"),
    ),
    (
        "drug/event",
        "FDA adverse event report",
        ("patient.drug.medicinalproduct",),
        ("patient.reaction.reactionmeddrapt", "serious"),
    ),
    (
        "transparency/crl",
        "FDA complete response letter",
        ("approval_name", "application_number", "company_name"),
        ("text",),
    ),
)

SNIPPET_MAX_CHARACTERS = 600


class OpenFDADatasetSearcher:
    """Searches one openFDA dataset with its own free-text query."""

    def __init__(
        self,
        client: OpenFDAClient,
        *,
        dataset: OpenFDADataset,
        name: str,
        title_fields: tuple[str, ...],
        snippet_fields: tuple[str, ...],
    ) -> None:
        self._client = client
        self._dataset = dataset
        self.name = name
        self._title_fields = title_fields
        self._snippet_fields = snippet_fields

    async def search(self, request: SearchRequest, *, limit: int) -> list[FederatedRecord]:
        """Return this dataset's best matches, or nothing if it has none.

        A dataset with no match is not an error: most questions are relevant to only a few of
        the datasets searched. Transport failures are left to propagate so federation can
        report them as a degraded source rather than silently returning zero results.
        """
        try:
            result = await self._client.query(
                self._dataset,
                # openFDA full-text search over every field, requiring each stated term.
                search=request.boolean(),
                limit=limit,
            )
        except OpenFDAError:
            raise

        records: list[FederatedRecord] = []
        for index, record in enumerate(result.results):
            records.append(
                FederatedRecord(
                    source=self.name,
                    title=self._title(record, index),
                    snippet=self._snippet(record),
                    # Link the human-readable record, not the API query that found it.
                    url=_record_url(record, self._dataset) or result.provenance.api_url,
                    external_key=_external_key(record, self._dataset, index),
                    provenance=result.provenance,
                )
            )
        return records

    def _title(self, record: dict[str, Any], index: int) -> str:
        for field in self._title_fields:
            value = _first_text(_traverse(record, field))
            if value:
                return f"{self.name}: {value}"
        # Many labels lack the harmonised openfda block; their product data still names the
        # product, which beats an anonymous "record 1".
        product = _first_text(record.get("spl_product_data_elements"))
        if product:
            return f"{self.name}: {' '.join(product.split()[:6])}"
        key = _external_key(record, self._dataset, index)
        return f"{self.name} {key.split(':', 1)[-1]}"

    def _snippet(self, record: dict[str, Any]) -> str:
        parts: list[str] = []
        for field in self._snippet_fields:
            value = _first_text(_traverse(record, field))
            if value:
                parts.append(f"{field}: {value}")
            if sum(len(part) for part in parts) >= SNIPPET_MAX_CHARACTERS:
                break
        snippet = " | ".join(parts) or "No preview field available for this record."
        if len(snippet) > SNIPPET_MAX_CHARACTERS:
            return snippet[: SNIPPET_MAX_CHARACTERS - 1].rstrip() + "…"
        return snippet


def build_openfda_searchers(client: OpenFDAClient) -> list[OpenFDADatasetSearcher]:
    return [
        OpenFDADatasetSearcher(
            client,
            dataset=dataset,
            name=name,
            title_fields=title_fields,
            snippet_fields=snippet_fields,
        )
        for dataset, name, title_fields, snippet_fields in GENERAL_SEARCH_DATASETS
    ]


def _traverse(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and current:
            first = current[0]
            current = first.get(part) if isinstance(first, dict) else None
        else:
            return None
    return current


def _first_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    if isinstance(value, int | float):
        return str(value)
    return None


def _external_key(record: dict[str, Any], dataset: str, index: int) -> str:
    for field in ("id", "application_number", "safetyreportid", "recall_number", "set_id"):
        value = _first_text(_traverse(record, field))
        if value:
            return f"{dataset}:{value}"
    return f"{dataset}:{index}"


def _record_url(record: dict[str, Any], dataset: str) -> str | None:
    """The public page for a record, when the dataset has one."""
    set_id = record.get("set_id")
    if dataset == "drug/label" and isinstance(set_id, str):
        return f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={set_id}"
    application = record.get("application_number")
    if dataset == "drug/drugsfda" and isinstance(application, str):
        digits = "".join(character for character in application if character.isdigit())
        return (
            "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm"
            f"?event=overview.process&ApplNo={digits}"
        )
    return None
