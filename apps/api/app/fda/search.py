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
from app.sources.federation import FederatedRecord

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
        ("status", "availability_information", "shortage_reason"),
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
        ("application_number", "product_name"),
        ("letter_text",),
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

    async def search(self, query: str, *, limit: int) -> list[FederatedRecord]:
        """Return this dataset's best matches, or nothing if it has none.

        A dataset with no match is not an error: most questions are relevant to only a few of
        the datasets searched. Transport failures are left to propagate so federation can
        report them as a degraded source rather than silently returning zero results.
        """
        try:
            result = await self._client.query(
                self._dataset,
                search=_free_text_expression(query),
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
                    url=result.provenance.api_url,
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
        return f"{self.name} record {index + 1}"

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


def _free_text_expression(query: str) -> str:
    """Build a safe openFDA full-text expression from a user question.

    openFDA searches across all fields when no field is named. Reserved characters are stripped
    rather than escaped because a malformed expression fails the whole dataset query, and a
    slightly broader match is a better outcome than a lost source.
    """
    reserved = set('":()[]{}\\/+-!^~*?')
    cleaned = "".join(" " if character in reserved else character for character in query)
    terms = [term for term in cleaned.split() if term.upper() not in {"AND", "OR", "NOT"}]
    if not terms:
        return '""'
    return " ".join(f'"{term}"' for term in terms[:12])


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
