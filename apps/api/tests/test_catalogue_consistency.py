"""Every openFDA field name still written in code must exist in openFDA's catalogue.

Field names typed by hand drift silently: the label tool's defaults asked for
"warnings_and_precautions", which exists on no label (the real field is
"warnings_and_cautions"), so every default lookup omitted Warnings and Precautions without an
error. This test turns that class of mistake into a failing build. When openFDA changes its
schema, run ``python -m scripts.refresh_openfda_fields`` and this test shows what broke.
"""

import pytest

from app.fda.catalogue import datasets, fields, label_sections
from app.fda.composition import NAME_FIELDS
from app.fda.search import GENERAL_SEARCH_DATASETS
from app.intelligence.detectors.labels import TRACKED_SECTIONS
from app.mcp_gateway.server import DEFAULT_LABEL_SECTIONS, LABEL_SECTION_ALIASES


def test_the_catalogue_covers_every_dataset_the_application_allows() -> None:
    from app.fda.models import OPENFDA_DATASETS

    assert datasets() == OPENFDA_DATASETS
    assert len(datasets()) == 29


def test_label_sections_come_from_the_catalogue_not_a_hand_list() -> None:
    sections = label_sections()

    assert {"abuse", "dependence", "controlled_substance", "inactive_ingredient"} <= sections
    assert not any(
        section.startswith("openfda.") or section.endswith("_table") for section in sections
    )


@pytest.mark.parametrize(
    ("label", "names"),
    [
        ("default label sections", DEFAULT_LABEL_SECTIONS),
        ("label section alias targets", list(LABEL_SECTION_ALIASES.values())),
        ("tracked label sections", list(TRACKED_SECTIONS)),
    ],
)
def test_hand_written_label_sections_exist(label: str, names: list[str]) -> None:
    missing = sorted(set(names) - label_sections())

    assert missing == [], f"{label} not in openFDA's drug/label catalogue: {missing}"


def test_composition_name_fields_exist() -> None:
    missing = [name for name in NAME_FIELDS if name not in fields("drug/label")]

    assert missing == []


def test_federated_search_title_and_snippet_fields_exist() -> None:
    missing = [
        f"{dataset}: {name}"
        for dataset, _, title_fields, snippet_fields in GENERAL_SEARCH_DATASETS
        for name in (*title_fields, *snippet_fields)
        if name not in fields(dataset)
    ]

    assert missing == []


FOCUSED_TOOL_CALLS = [
    ("get_fda_drug_labels", {"drug_name": "aspirin", "manufacturer": "Bayer"}),
    ("get_drug_composition", {"drug": "aspirin", "max_manufacturers": 1}),
    ("analyze_fda_adverse_event_reactions", {"drug_name": "aspirin"}),
    ("search_fda_drug_approvals", {"drug_name": "aspirin", "sponsor": "Bayer"}),
    ("search_fda_drug_shortages", {"drug_name": "aspirin"}),
    ("search_fda_drug_recalls", {"drug_name": "aspirin"}),
    ("search_fda_complete_response_letters", {"query": "aspirin", "company_name": "Bayer"}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("tool", "arguments"), FOCUSED_TOOL_CALLS)
async def test_every_query_a_focused_tool_sends_uses_real_fields(
    tool: str, arguments: dict[str, object]
) -> None:
    """Checks the queries tools actually generate, so no hand-typed field can slip through."""
    import httpx

    from app.fda.client import OpenFDAClient, referenced_fields
    from app.mcp_gateway.client import FastMCPToolClient
    from app.mcp_gateway.server import create_internal_mcp_server

    sent: list[tuple[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        dataset = request.url.path.strip("/").removesuffix(".json")
        params = request.url.params
        sent.append(
            (
                dataset,
                referenced_fields(params.get("search"), params.get("count"), params.get("sort")),
            )
        )
        return httpx.Response(200, json={"meta": {"results": {"total": 0}}, "results": []})

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    await FastMCPToolClient(create_internal_mcp_server(fda)).call_tool(tool, arguments)

    assert sent, f"{tool} sent no request"
    unknown = [
        f"{dataset}: {name}"
        for dataset, names in sent
        for name in names
        if name not in fields(dataset)
    ]
    assert unknown == [], f"{tool} queried fields openFDA does not have: {unknown}"
