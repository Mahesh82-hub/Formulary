import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.fda.client import OpenFDAClient
from app.fda.names import StaticNames
from app.mcp_gateway.client import (
    FastMCPToolClient,
    MCPToolInvocationError,
    get_mcp_tool_client,
)
from app.mcp_gateway.server import OpenFDAToolLimits, create_internal_mcp_server


@pytest.mark.asyncio
async def test_fastmcp_client_discovers_and_calls_internal_tool() -> None:
    client = get_mcp_tool_client()

    tools = await client.list_tools()

    tools_by_name = {tool.name: tool for tool in tools}
    assert set(tools_by_name) == {
        "convert_mass",
        "analyze_bioequivalence_summary",
        "request_user_clarification",
        "prepare_bioequivalence_evidence_request",
        "ingest_openfda_query",
        "ingest_fda_pdf_document",
        "search_all_sources",
        "search_regulatory_events",
        "search_ingested_evidence",
        "read_ingested_document_chunks",
        "openfda_query",
        "get_fda_drug_labels",
        "get_drug_composition",
        "analyze_fda_adverse_event_reactions",
        "search_fda_drug_approvals",
        "search_fda_drug_shortages",
        "search_fda_drug_recalls",
        "search_fda_complete_response_letters",
    }
    assert tools_by_name["convert_mass"].input_schema["properties"]["from_unit"]["enum"] == [
        "mcg",
        "mg",
        "g",
    ]
    dataset_schema = tools_by_name["openfda_query"].input_schema["properties"]["dataset"]
    assert "drug/event" in dataset_schema["enum"]
    assert "transparency/crl" in dataset_schema["enum"]
    section_schema = tools_by_name["get_fda_drug_labels"].input_schema["properties"]["sections"]
    section_array_schema = next(
        option for option in section_schema["anyOf"] if option.get("type") == "array"
    )
    assert section_array_schema["items"] == {"type": "string"}
    model_selected_page_fields = {
        "ingest_openfda_query": ("limit",),
        "search_ingested_evidence": ("limit",),
        "read_ingested_document_chunks": ("cursor", "limit"),
        "openfda_query": ("limit", "skip"),
        "get_fda_drug_labels": ("limit",),
        "analyze_fda_adverse_event_reactions": ("top_reactions",),
        "search_fda_drug_approvals": ("limit",),
        "search_fda_drug_shortages": ("limit",),
        "search_fda_drug_recalls": ("limit",),
        "search_fda_complete_response_letters": ("limit",),
    }
    for tool_name, field_names in model_selected_page_fields.items():
        properties = tools_by_name[tool_name].input_schema["properties"]
        for field_name in field_names:
            assert "maximum" not in properties[field_name]
            assert "minimum" not in properties[field_name]

    result = await client.call_tool(
        "convert_mass",
        {"value": 2500, "from_unit": "mcg", "to_unit": "mg"},
    )

    assert result.is_error is False
    assert result.data == {"value": 2.5, "unit": "mg"}


@pytest.mark.asyncio
async def test_generic_clarification_tool_returns_suggestions_and_custom_input() -> None:
    client = get_mcp_tool_client()

    result = await client.call_tool(
        "request_user_clarification",
        {
            "title": "Choose the FDA scope",
            "question": "Which type of FDA information should I focus on?",
            "suggestions": ["Current labeling", "Approval history", "Current labeling"],
            "reason": "The requested scope changes which FDA dataset should be searched.",
        },
    )

    assert result.is_error is False
    assert result.data["status"] == "needs_clarification"
    assert result.data["task_type"] == "general_chat"
    assert result.data["title"] == "Choose the FDA scope"
    assert result.data["questions"][0]["suggestions"] == [
        "Current labeling",
        "Approval history",
    ]
    assert result.data["questions"][0]["allow_custom"] is True
    assert result.data["allow_additional_question"] is False

    open_ended = await client.call_tool(
        "request_user_clarification",
        {
            "title": "Drug selection needed",
            "question": "Which specific drug would you like evidence summarized for?",
            "suggestions": [
                "Provide the generic name (e.g., ibuprofen)",
                "Provide the brand name (e.g., Humira)",
                "Specify a drug class if you want a general summary",
            ],
        },
    )

    assert open_ended.is_error is False
    assert open_ended.data["questions"][0]["suggestions"] == []


@pytest.mark.asyncio
async def test_fastmcp_bioequivalence_tool_uses_deterministic_analysis() -> None:
    client = get_mcp_tool_client()

    result = await client.call_tool(
        "analyze_bioequivalence_summary",
        {
            "request": {
                "mode": "formal_summary",
                "context": {
                    "drug_name": "Example drug",
                    "reference_product": "Reference tablet",
                    "test_product": "Test tablet",
                    "dosage_form": "immediate-release tablet",
                    "route": "oral",
                    "strength": "100 mg",
                    "study_condition": "fasting",
                },
                "endpoints": [
                    {
                        "endpoint": "cmax",
                        "reference_value": 100,
                        "test_value": 96,
                        "unit": "ng/mL",
                        "ci90_lower_percent": 89,
                        "ci90_upper_percent": 104,
                    },
                    {
                        "endpoint": "auc_0_t",
                        "reference_value": 1_000,
                        "test_value": 1_020,
                        "unit": "ng*h/mL",
                        "ci90_lower_percent": 94,
                        "ci90_upper_percent": 109,
                    },
                ],
            }
        },
    )

    assert result.is_error is False
    assert result.data["overall_status"] == "meets_criteria"
    assert result.data["endpoints"][0]["geometric_mean_ratio_percent"] == 96


@pytest.mark.asyncio
async def test_simulation_evidence_intake_asks_only_high_impact_questions() -> None:
    client = get_mcp_tool_client()

    result = await client.call_tool("prepare_bioequivalence_evidence_request")

    assert result.is_error is False
    assert result.data["status"] == "needs_clarification"
    assert result.data["recommended_next_action"] == "ask_user"
    assert result.data["missing_required_fields"] == [
        "drug_name",
        "objective",
        "dosage_form",
        "route",
        "strength_or_dose",
    ]
    assert [question["field_ids"] for question in result.data["questions"]] == [
        ["drug_name"],
        ["objective"],
        ["dosage_form", "route", "strength_or_dose"],
    ]
    assert [question["question_id"] for question in result.data["questions"]] == [
        "drug_identity",
        "research_objective",
        "formulation",
    ]
    assert result.data["questions"][1]["suggestions"] == [
        "Use source-reported FDA reference values",
        "Compare a company test product with a reference product",
    ]


@pytest.mark.asyncio
async def test_bioequivalence_intake_requires_products_and_company_data() -> None:
    client = get_mcp_tool_client()

    result = await client.call_tool(
        "prepare_bioequivalence_evidence_request",
        {
            "drug_name": "metformin",
            "objective": "bioequivalence_comparison",
            "dosage_form": "immediate-release tablet",
            "route": "oral",
            "strength_or_dose": "500 mg",
        },
    )

    assert result.data["status"] == "needs_clarification"
    assert result.data["missing_required_fields"] == [
        "reference_product",
        "test_product",
        "company_data_available",
    ]
    assert result.data["questions"][0]["field_ids"] == [
        "reference_product",
        "test_product",
        "company_data_available",
    ]
    assert result.data["questions"][0]["question_id"] == "comparison_products"


@pytest.mark.asyncio
async def test_bioequivalence_intake_is_ready_with_comparison_context() -> None:
    client = get_mcp_tool_client()

    incomplete = await client.call_tool(
        "prepare_bioequivalence_evidence_request",
        {
            "drug_name": "metformin",
            "objective": "bioequivalence_comparison",
            "reference_product": "Glucophage",
            "dosage_form": "immediate-release tablet",
            "route": "oral",
            "strength_or_dose": "500 mg",
        },
    )

    assert incomplete.data["status"] == "needs_clarification"
    assert incomplete.data["missing_required_fields"] == [
        "test_product",
        "company_data_available",
    ]

    ready = await client.call_tool(
        "prepare_bioequivalence_evidence_request",
        {
            "drug_name": "metformin",
            "objective": "bioequivalence_comparison",
            "reference_product": "Glucophage",
            "dosage_form": "immediate-release tablet",
            "route": "oral",
            "strength_or_dose": "500 mg",
            "test_product": "Company metformin tablet",
            "company_data_available": True,
        },
    )

    assert ready.data["status"] == "ready"
    assert ready.data["context"]["objective"] == "bioequivalence_comparison"
    assert ready.data["recommended_next_action"] == "research_evidence"


@pytest.mark.asyncio
async def test_simulation_evidence_intake_defaults_to_simulator_neutral_scope() -> None:
    client = get_mcp_tool_client()

    result = await client.call_tool(
        "prepare_bioequivalence_evidence_request",
        {
            "drug_name": "metformin",
            "objective": "reference_values",
            "reference_product": "Glucophage",
            "dosage_form": "immediate-release tablet",
            "route": "oral",
            "strength_or_dose": "500 mg",
        },
    )

    assert result.data["status"] == "ready"
    assert result.data["recommended_next_action"] == "research_evidence"
    assert result.data["questions"] == []
    assert "physicochemical" in result.data["parameter_categories"]
    assert "concentration_time_data" in result.data["parameter_categories"]
    assert any("simulator-specific" in item for item in result.data["assumptions"])


@pytest.mark.asyncio
async def test_fastmcp_client_masks_tool_errors() -> None:
    client = get_mcp_tool_client()

    with pytest.raises(MCPToolInvocationError, match="convert_mass"):
        await client.call_tool(
            "convert_mass",
            {"value": -1, "from_unit": "mg", "to_unit": "g"},
        )


@pytest.mark.asyncio
async def test_fda_label_tool_uses_focused_query_and_projects_sections() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/drug/label.json"
        search = request.url.params["search"]
        # Phrase, not exact: exact matching on "TYLENOL" found 0 labels where the phrase found
        # 111, because brand names such as "TYLENOL Extra Strength" are longer than the query.
        assert 'openfda.generic_name:"aspirin"' in search
        assert ".exact" not in search
        return httpx.Response(
            200,
            json={
                "meta": {
                    "last_updated": "2026-07-07",
                    "results": {"total": 1},
                },
                "results": [
                    {
                        "id": "label-1",
                        "set_id": "set-1",
                        "effective_time": "20260701",
                        "openfda": {"generic_name": ["ASPIRIN"]},
                        "boxed_warning": ["Important warning"],
                        "boxed_warning_table": ["Dose | Risk"],
                        "how_supplied": ["Bottles of 100 tablets"],
                        "adverse_reactions": ["Not requested"],
                    }
                ],
            },
        )

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    client = FastMCPToolClient(create_internal_mcp_server(fda))

    result = await client.call_tool(
        "get_fda_drug_labels",
        {
            "drug_name": "aspirin",
            "sections": ["boxed_warning", "how supplied", "not_a_label_section"],
            "limit": 1,
        },
    )

    assert result.data["returned"] == 1
    record = result.data["results"][0]
    assert record["sections"] == {
        "boxed_warning": ["Important warning"],
        "boxed_warning_table": ["Dose | Risk"],
        "how_supplied": ["Bottles of 100 tablets"],
    }
    assert record["requested_sections"] == ["boxed_warning", "how_supplied"]
    assert any("not_a_label_section" in caveat for caveat in result.data["caveats"])
    assert "adverse_reactions" not in record["sections"]
    assert result.data["provenance"]["api_url"].startswith("https://api.fda.test/")


@pytest.mark.asyncio
async def test_openfda_tool_compacts_records_that_exceed_llm_payload_boundary() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "meta": {"results": {"total": 1}},
                "results": [
                    {
                        "id": "large-label-version",
                        "set_id": "large-label",
                        "openfda": {"generic_name": ["METFORMIN"]},
                        "clinical_pharmacology": ["large source text " * 20_000],
                    }
                ],
            },
        )

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    client = FastMCPToolClient(create_internal_mcp_server(fda))

    result = await client.call_tool(
        "openfda_query",
        {
            "dataset": "drug/label",
            "filters": [{"field": "openfda.generic_name", "value": "metformin"}],
            "limit": 1,
        },
    )

    serialized = json.dumps(result.data)
    assert len(serialized) < 48_000
    assert result.data["results"][0]["available_fields"]
    assert any("ingest_openfda_query" in caveat for caveat in result.data["caveats"])


@pytest.mark.asyncio
async def test_openfda_tool_clamps_model_selected_paging_values() -> None:
    observed_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed_queries.append(dict(request.url.params.multi_items()))
        return httpx.Response(
            200,
            json={"meta": {"results": {"total": 0}}, "results": []},
        )

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    client = FastMCPToolClient(create_internal_mcp_server(fda))

    upper = await client.call_tool(
        "openfda_query",
        {"dataset": "drug/label", "limit": 20, "skip": 50_000},
    )
    lower = await client.call_tool(
        "openfda_query",
        {"dataset": "drug/label", "limit": -5, "skip": -10},
    )

    assert upper.is_error is False
    assert upper.data["query"]["limit"] == 10
    assert upper.data["query"]["skip"] == 25_000
    assert lower.is_error is False
    assert lower.data["query"]["limit"] == 1
    assert lower.data["query"]["skip"] == 0
    assert observed_queries == [
        {"limit": "10", "skip": "25000"},
        {"limit": "1"},
    ]


@pytest.mark.asyncio
async def test_tool_record_caps_follow_configuration_instead_of_hardcoded_limits() -> None:
    """The per-tool caps used to be module constants that silently shadowed settings."""
    requested_limits: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_limits.append(int(request.url.params["limit"]))
        return httpx.Response(200, json={"meta": {"results": {"total": 0}}, "results": []})

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    server = create_internal_mcp_server(fda, limits=OpenFDAToolLimits(label_max_records=2))
    client = FastMCPToolClient(server)

    await client.call_tool("get_fda_drug_labels", {"drug_name": "aspirin", "limit": 25})

    # The configured cap wins over the caller-supplied limit.
    assert requested_limits == [2]


@pytest.mark.asyncio
async def test_tool_limits_are_derived_from_settings() -> None:
    settings = Settings(
        postgres_db="db",
        postgres_user="user",
        postgres_password=SecretStr("password"),
        openfda_label_max_records=4,
        openfda_query_max_records=7,
    )

    limits = OpenFDAToolLimits.from_settings(settings)

    assert limits.label_max_records == 4
    assert limits.query_max_records == 7


@pytest.mark.asyncio
async def test_label_tool_searches_international_and_us_names_with_a_manufacturer() -> None:
    searches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        searches.append(request.url.params["search"])
        return httpx.Response(200, json={"meta": {"results": {"total": 0}}, "results": []})

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    client = FastMCPToolClient(
        create_internal_mcp_server(fda, names=StaticNames({"paracetamol": "acetaminophen"}))
    )

    result = await client.call_tool(
        "get_fda_drug_labels",
        {
            "drug_name": "paracetamol",
            "manufacturer": "Kenvue",
            "sections": ["inactive_ingredients", "excipients", "composition"],
        },
    )

    search = searches[0]
    assert '"paracetamol"' in search and '"acetaminophen"' in search
    assert 'openfda.manufacturer_name:"Kenvue"' in search
    # Plural and colloquial section names map onto the real openFDA fields.
    assert result.data["caveats"] and not any(
        "Unsupported" in caveat for caveat in result.data["caveats"]
    )


@pytest.mark.asyncio
async def test_openfda_query_rejects_unknown_fields_before_sending_anything() -> None:
    """The failure that motivated structured queries: guessed fields must never reach openFDA."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json={"meta": {"results": {"total": 0}}, "results": []})

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    client = FastMCPToolClient(create_internal_mcp_server(fda))

    result = await client.call_tool(
        "openfda_query",
        {
            "dataset": "drug/label",
            "filters": [
                {"field": "labeler_name", "value": "BAYER"},
                {"field": "active_ingredients.name", "value": "ACETAMINOPHEN"},
            ],
        },
    )

    assert sent == []
    assert result.data["status"] == "invalid_query"
    assert any("openfda.manufacturer_name" in error for error in result.data["errors"])
    assert any("active_ingredient" in error for error in result.data["errors"])
    assert "not evidence that the data does not exist" in result.data["caveats"][0]


@pytest.mark.asyncio
async def test_structured_filters_compile_to_valid_openfda_syntax() -> None:
    searches: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        searches.append(dict(request.url.params))
        return httpx.Response(
            200, json={"meta": {"results": {"total": 1}}, "results": [{"id": "label-1"}]}
        )

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    client = FastMCPToolClient(
        create_internal_mcp_server(fda, names=StaticNames({"paracetamol": "acetaminophen"}))
    )

    result = await client.call_tool(
        "openfda_query",
        {
            "dataset": "drug/label",
            "filters": [
                {"field": "openfda.generic_name", "value": "paracetamol"},
                {"field": "openfda.manufacturer_name", "value": "Bayer"},
                {
                    "field": "effective_time",
                    "match": "range",
                    "start": "2025-01-01",
                    "end": "2026-09-24",
                },
            ],
            "sort_field": "effective_time",
        },
    )

    params = searches[0]
    assert params["search"] == (
        '(openfda.generic_name:"paracetamol" openfda.generic_name:"acetaminophen") AND '
        '(openfda.manufacturer_name:"Bayer") AND (effective_time:[20250101 TO 20260924])'
    )
    assert params["sort"] == "effective_time:desc"
    assert result.data["status"] == "ok"


@pytest.mark.asyncio
async def test_adverse_event_filters_are_validated_against_the_event_catalogue() -> None:
    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []})),
    )
    client = FastMCPToolClient(create_internal_mcp_server(fda))

    result = await client.call_tool(
        "analyze_fda_adverse_event_reactions",
        {
            "drug_name": "aspirin",
            "additional_filters": [
                {
                    "field": "received_date",
                    "match": "range",
                    "start": "2025-01-01",
                    "end": "2025-12-31",
                }
            ],
        },
    )

    assert result.data["status"] == "invalid_query"
    assert "receivedate" in result.data["errors"][0]


@pytest.mark.asyncio
async def test_search_all_sources_sends_the_stated_terms_with_their_synonyms() -> None:
    from app.sources.federation import FederatedRecord, FederatedSearchCoordinator, SearchRequest

    received: list[SearchRequest] = []

    class Recorder:
        name = "recorder"

        async def search(self, request: SearchRequest, *, limit: int) -> list[FederatedRecord]:
            received.append(request)
            return []

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []})),
    )
    server = create_internal_mcp_server(
        fda,
        federation=FederatedSearchCoordinator([Recorder()]),
        names=StaticNames({"paracetamol": "acetaminophen"}),
    )

    await FastMCPToolClient(server).call_tool(
        "search_all_sources",
        {"query": "paracetamol compositions from Pfizer", "terms": ["paracetamol", "Pfizer"]},
    )

    assert received[0].terms == (("paracetamol", "acetaminophen"), ("Pfizer",))
    assert received[0].boolean() == '("paracetamol" OR "acetaminophen") AND "Pfizer"'
