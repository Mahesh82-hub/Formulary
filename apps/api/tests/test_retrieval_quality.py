"""Regression tests for the retrieval failures behind a wrong paracetamol answer.

Each test pins one cause: exact-match names, missing composition sections, OR-semantics in
federated search, and international drug names. See docs/retrieval-quality.md.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.fda.client import OpenFDAClient
from app.fda.composition import drug_composition, split_ingredients
from app.fda.names import RxNormNames, StaticNames, expand
from app.llm.groq import GroqChatProvider
from app.llm.models import LLMMessage, LLMToolDefinition, WebSource
from app.llm.web_citations import parse_executed_tools, rewrite_citations, sources_section
from app.sources.federation import SearchRequest
from app.sources.resilience import ResilientRequester, RetryPolicy

FIXTURES = Path(__file__).parent / "fixtures"


async def _no_sleep(seconds: float) -> None: ...


# RxNav responses, trimmed from what the live service returned in September 2026.
RXNAV = {
    "paracetamol": [{"rxcui": "161", "name": None}, {"rxcui": "161", "name": "paracetamol"}],
    "aciclovir": [{"rxcui": "281", "name": "Acyclovir"}, {"rxcui": "281", "name": "ACYCLOVIR"}],
    "Tylenol": [{"rxcui": "202433", "name": "Tylenol"}],
    "Bayer": [
        {"rxcui": "1168631", "name": "Bayer Aspirin Pill"},
        {"rxcui": "1168628", "name": "Bayer Aspirin Oral Product"},
    ],
    "Kenvue": [],
}
PROPERTIES = {
    "161": {"name": "acetaminophen", "tty": "IN"},
    "281": {"name": "acyclovir", "tty": "IN"},
    "202433": {"name": "Tylenol", "tty": "BN"},
}


def _rxnav(calls: list[str], fail: bool = False) -> RxNormNames:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if fail:
            return httpx.Response(503)
        if request.url.path.endswith("approximateTerm.json"):
            term = request.url.params["term"]
            return httpx.Response(200, json={"approximateGroup": {"candidate": RXNAV[term]}})
        rxcui = request.url.path.split("/rxcui/")[1].split("/")[0]
        return httpx.Response(200, json={"properties": PROPERTIES[rxcui]})

    return RxNormNames(
        base_url="https://rxnav.test/REST",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
        requester=ResilientRequester(
            source_name="RxNorm", retry_policy=RetryPolicy(max_attempts=1), sleep=_no_sleep
        ),
    )


@pytest.mark.asyncio
async def test_rxnorm_expands_international_names_and_nothing_else() -> None:
    resolved = await expand(_rxnav([]), ["paracetamol", "aciclovir", "Tylenol", "Bayer", "Kenvue"])

    assert resolved == {
        "paracetamol": ("paracetamol", "acetaminophen"),  # exact synonym match
        "aciclovir": ("aciclovir", "acyclovir"),  # every candidate is one ingredient
        "Tylenol": ("Tylenol",),  # a brand is never widened to its ingredient
        "Bayer": ("Bayer",),  # fuzzy matches to Bayer Aspirin products are rejected
        "Kenvue": ("Kenvue",),
    }


@pytest.mark.asyncio
async def test_rxnorm_results_are_cached_and_failures_fall_back_without_caching() -> None:
    calls: list[str] = []
    resolver = _rxnav(calls)
    await resolver.variants("paracetamol")
    first = len(calls)
    await resolver.variants("Paracetamol")
    assert len(calls) == first  # cached, case-insensitively

    failing_calls: list[str] = []
    failing = _rxnav(failing_calls, fail=True)
    assert await failing.variants("paracetamol") == ("paracetamol",)
    await failing.variants("paracetamol")
    assert len(failing_calls) == 2  # a failure is retried next time, not remembered


def test_search_requests_require_every_stated_term_and_accept_any_alternative() -> None:
    request = SearchRequest(
        query="compositions of paracetamol from Pfizer",
        terms=(("paracetamol", "acetaminophen"), ("Pfizer",)),
    )

    # openFDA treats space-separated terms as OR; this matched 16 unrelated Pfizer letters.
    assert request.boolean() == '("paracetamol" OR "acetaminophen") AND "Pfizer"'
    assert SearchRequest.from_text("semaglutide shortage").boolean() == (
        '"semaglutide" AND "shortage"'
    )
    assert SearchRequest(query="x", terms=(('bad "quote"',),)).boolean() == '"bad quote"'


def test_multi_part_inactive_ingredient_lists_are_split_cleanly() -> None:
    text = (
        "Inactive ingredients carnauba wax, hypromellose, titanium dioxide "
        "Inactive ingredients FD&C blue no. 1, polydextrose, titanium dioxide."
    )

    assert split_ingredients(text) == [
        "carnauba wax",
        "hypromellose",
        "titanium dioxide",
        "FD&C blue no. 1",
        "polydextrose",
    ]


def _label(
    set_id: str, manufacturer: str, generic: str, brand: str, inactive: str | None = None
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "set_id": set_id,
        "effective_time": "20260901",
        "active_ingredient": [f"Active ingredient {generic} 500 mg"],
        "openfda": {
            "generic_name": [generic],
            "brand_name": [brand],
            "manufacturer_name": [manufacturer],
            "product_type": ["HUMAN OTC DRUG"],
        },
    }
    if inactive:
        record["inactive_ingredient"] = [inactive]
    return record


@pytest.mark.asyncio
async def test_composition_picks_top_labelers_itself_and_prefers_single_ingredient_products() -> (
    None
):
    searches: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        searches.append(params)
        if "count" in params:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"term": "Bryant Ranch Prepack", "count": 149},
                        {"term": "Kenvue Brands LLC", "count": 83},
                        {"term": "Haleon US Holdings LLC", "count": 80},
                    ]
                },
            )
        search = params.get("search", "")
        if params.get("limit") == "1":
            return httpx.Response(200, json={"meta": {"results": {"total": 3527}}, "results": [{}]})
        if "Kenvue" in search and ".exact" in search:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _label(
                            "k1",
                            "Kenvue Brands LLC",
                            "ACETAMINOPHEN",
                            "TYLENOL",
                            "Inactive ingredients corn starch, hypromellose",
                        )
                    ]
                },
            )
        if "Kenvue" in search:
            return httpx.Response(
                200,
                json={
                    "results": [
                        _label(
                            "k2",
                            "Kenvue Brands LLC",
                            "ACETAMINOPHEN AND DEXTROMETHORPHAN",
                            "TYLENOL COLD",
                            "Inactive ingredients sucralose",
                        )
                    ]
                },
            )
        return httpx.Response(404, json={"error": {"message": "No matches found!"}})

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )

    result = await drug_composition(
        fda,
        "paracetamol",
        max_manufacturers=2,
        names_resolver=StaticNames({"paracetamol": "acetaminophen"}),
    )

    # Chosen from the data, not by asking the user; the repackager is excluded.
    assert [item.manufacturer for item in result.manufacturers] == ["Kenvue Brands LLC"]
    assert result.total_labels == 3527
    products = result.manufacturers[0].products
    assert [product.brand_name for product in products] == ["TYLENOL", "TYLENOL COLD"]
    assert products[0].is_combination is False
    assert products[0].inactive_ingredients == ["corn starch", "hypromellose"]
    assert products[1].is_combination is True
    assert products[0].label_url and "setid=k1" in products[0].label_url
    # Haleon matched nothing in this fixture and is reported, not silently dropped.
    assert any("Haleon" in caveat for caveat in result.caveats)
    # Both the international and US names were searched.
    assert any(
        '"paracetamol"' in s.get("search", "") and '"acetaminophen"' in s.get("search", "")
        for s in searches
    )


def test_browser_citations_become_real_links_with_a_source_list() -> None:
    response = json.loads((FIXTURES / "groq_browser_search.json").read_text())
    message = response["choices"][0]["message"]

    queries, pages = parse_executed_tools(message["executed_tools"])
    text, cited = rewrite_citations(message["content"], pages)

    assert queries == ["Tylenol Extra Strength caplet inactive ingredients"]
    assert "【" not in text
    assert cited and cited[0].url.startswith("https://dailymed.nlm.nih.gov/")
    assert f"]({cited[0].url})" in text
    section = sources_section(cited)
    assert "**Web sources**" in section and cited[0].url in section


def test_unknown_citation_markers_are_removed_rather_than_left_as_noise() -> None:
    text, cited = rewrite_citations("A claim【7†L1-L2】.", {})

    assert text == "A claim."
    assert cited == []


def test_the_search_engine_url_is_never_offered_as_a_source() -> None:
    _, pages = parse_executed_tools(
        [
            {
                "type": "browser_search",
                "arguments": '{"query": "x"}',
                "search_results": {"results": [{"title": "t", "url": "https://exa.ai/search?q=x"}]},
            },
            {
                "type": "browser.open",
                "arguments": '{"cursor": 0, "id": 0}',
                "search_results": {"results": [{"title": "t", "url": "https://exa.ai/search?q=x"}]},
            },
        ]
    )

    assert pages == {}


@pytest.mark.asyncio
async def test_groq_offers_web_search_only_to_supported_models_while_tools_are_allowed() -> None:
    payloads: list[dict[str, Any]] = []
    fixture = json.loads((FIXTURES / "groq_browser_search.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=fixture)

    provider = GroqChatProvider(
        api_key="key",
        base_url="https://groq.test/openai/v1",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
        web_search_models=frozenset({"openai/gpt-oss-120b"}),
    )
    tool = LLMToolDefinition(name="t", description="d", parameters={"type": "object"})
    messages = [LLMMessage(role="user", content="Tylenol inactive ingredients?")]

    completion = await provider.complete(
        model="openai/gpt-oss-120b",
        system_prompt="s",
        messages=messages,
        tools=[tool],
        allow_web_search=True,
    )
    await provider.complete(
        model="llama-3.3-70b",
        system_prompt="s",
        messages=messages,
        tools=[tool],
        allow_web_search=True,
    )
    await provider.complete(
        model="openai/gpt-oss-120b",
        system_prompt="s",
        messages=messages,
        tools=[],
        allow_web_search=True,
    )
    # Before any official tool has run, web search is withheld.
    await provider.complete(
        model="openai/gpt-oss-120b", system_prompt="s", messages=messages, tools=[tool]
    )

    kinds = [[item.get("type") for item in payload.get("tools", [])] for payload in payloads]
    assert kinds[0] == ["function", "browser_search"]
    assert kinds[1] == ["function"]  # unsupported model
    assert kinds[2] == []  # synthesis: no tools, so no back-door research
    assert kinds[3] == ["function"]  # official sources first
    assert completion.web_queries == ["Tylenol Extra Strength caplet inactive ingredients"]
    assert completion.web_sources and isinstance(completion.web_sources[0], WebSource)
    assert "【" not in completion.text


def _pubmed_record(pmid: str, title: str) -> dict[str, Any]:
    return {
        "source": "PubMed",
        "title": title,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "external_key": f"pubmed:{pmid}",
        "provenance": {"api_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"},
    }


def test_only_sources_the_answer_used_are_listed() -> None:
    """Replays a real answer: a pKa search also returned protein kinase A (PKA) papers."""
    from app.services.citations import citable_links, finalize_answer

    federated = {
        "records": [
            _pubmed_record("11111111", "Metformin promotes survivin degradation via AMPK/PKA"),
            _pubmed_record("22222222", "Metformin transport requires its strong positive charge"),
        ]
    }
    links = citable_links("search_all_sources", federated)
    answer = "Metformin's pKa is 12.4, so it is a monocation at pH 7.4【pubmed:22222222】."

    final = finalize_answer(answer, links)

    assert "[PMID 22222222](https://pubmed.ncbi.nlm.nih.gov/22222222/)" in final
    assert "11111111" not in final  # retrieved but unused: not listed
    assert "【" not in final


def test_identifiers_no_tool_returned_are_flagged_not_linked() -> None:
    from app.services.citations import finalize_answer

    final = finalize_answer("A claim【pubmed:99999999】 and【NCT00000001】.", [])

    assert "PMID 99999999, unverified" in final
    assert "NCT00000001, unverified" in final
    assert "pubmed.ncbi.nlm.nih.gov/99999999" not in final


def test_product_names_and_label_ids_count_as_references() -> None:
    from app.services.citations import citable_links, finalize_answer

    composition = {
        "manufacturers": [
            {
                "manufacturer": "Kenvue Brands LLC",
                "products": [
                    {
                        "brand_name": "Infants TYLENOL",
                        "label_url": "https://dailymed.test/k1",
                        "set_id": "k1",
                    },
                    {
                        "brand_name": "Childrens Motrin",
                        "label_url": "https://dailymed.test/k2",
                        "set_id": "k2",
                    },
                ],
            }
        ]
    }
    links = citable_links("get_drug_composition", composition)

    final = finalize_answer("Infants TYLENOL contains sucralose.", links)

    assert "https://dailymed.test/k1" in final
    assert "https://dailymed.test/k2" not in final


def test_when_nothing_is_referenced_sources_are_labelled_consulted() -> None:
    from app.services.citations import citable_links, finalize_answer

    links = citable_links(
        "search_all_sources", {"records": [_pubmed_record("33333333", "A relevant paper")]}
    )

    final = finalize_answer("An answer with no explicit references.", links)

    assert "**Sources consulted**" in final and "**Sources**\n" not in final


def test_web_sources_use_the_page_heading_when_no_title_was_given() -> None:
    _, pages = parse_executed_tools(
        [
            {
                "type": "browser.open",
                "arguments": '{"cursor": 5, "id": "https://pubmed.ncbi.nlm.nih.gov/27943295/"}',
                "output": "L0: \nL1: URL:\nL2: https://pubmed.ncbi.nlm.nih.gov/27943295/\n"
                "L3: Pharmacokinetics of ceftiofur crystalline-free acid in goats - PubMed",
            }
        ]
    )

    assert pages[0].title.startswith("Pharmacokinetics of ceftiofur")


def test_irrelevant_local_evidence_is_not_returned_and_pdf_titles_are_readable() -> None:
    from app.ingestion.service import pdf_title
    from app.sources.registry import readable_section

    assert (
        pdf_title(
            None,
            "https://www.fda.gov/media/1/download",
            [{"content": "[Page 1]\nCPG Sec. 550.235 Cherry Jam - Adulteration\nwith Mold"}],
        )
        == "CPG Sec. 550.235 Cherry Jam - Adulteration"
    )
    assert pdf_title(None, "https://www.fda.gov/media/1/download", []) == "FDA document"
    assert readable_section("$.pages[1].text") == "page 1"


def test_leaked_tool_call_fragments_are_removed_from_answers() -> None:
    from app.llm.sanitize import strip_protocol_leaks

    leaked = (
        "Tylenol Extra Strength caplets are marketed by **Kenvue Brands LLC**"
        "【functions.get_drug_composition to=assistant<|channel|>commentary <|constrain|>json"
        '<|message|>{"drug":"Tylenol Extra Strength","manufacturers":null,"note":"a } brace"}'
        "\n\nThey contain acetaminophen 500 mg."
    )

    cleaned, found = strip_protocol_leaks(leaked)

    assert found is True
    assert cleaned == (
        "Tylenol Extra Strength caplets are marketed by **Kenvue Brands LLC**"
        "\n\nThey contain acetaminophen 500 mg."
    )
    assert strip_protocol_leaks("A normal answer.") == ("A normal answer.", False)
    assert strip_protocol_leaks("Stray <|end|> token")[0] == "Stray  token"


def test_fragments_are_recognised_but_short_complete_answers_are_not() -> None:
    from app.services.chat_orchestrator import is_degenerate_answer

    assert is_degenerate_answer("")
    assert is_degenerate_answer("**Mesalamine (5-aminosalicylic acid, also")
    assert not is_degenerate_answer("Yes.")
    assert not is_degenerate_answer("No matching recall was found in openFDA.")
    assert not is_degenerate_answer("x" * 250)


def test_unterminated_markers_are_removed() -> None:
    from app.services.citations import finalize_answer

    assert finalize_answer("Rows 【 } |\n\nNext section", []) == "Rows |\n\nNext section"


@pytest.mark.asyncio
async def test_search_relaxes_terms_records_rarely_contain_but_keeps_the_subject() -> None:
    from datetime import UTC, datetime

    from app.sources.federation import FederatedRecord, FederatedSearchCoordinator, SearchRequest
    from app.sources.models import SourceProvenance

    seen: list[tuple[tuple[str, ...], ...]] = []

    class OnlyTheDrug:
        name = "FDA drug label"

        async def search(self, request: SearchRequest, *, limit: int) -> list[FederatedRecord]:
            seen.append(request.terms)
            if len(request.terms) > 1:
                return []  # no label says "ingredients" alongside the product name
            return [
                FederatedRecord(
                    source=self.name,
                    title="Panadol",
                    snippet="Paracetamol 500 mg",
                    external_key="p1",
                    provenance=SourceProvenance(
                        source="openFDA", api_url="https://x.test", retrieved_at=datetime.now(UTC)
                    ),
                )
            ]

    result = await FederatedSearchCoordinator([OnlyTheDrug()]).search(
        SearchRequest(
            query="Panadol Advance ingredients", terms=(("Panadol Advance",), ("ingredients",))
        )
    )

    assert seen == [(("Panadol Advance",), ("ingredients",)), (("Panadol Advance",),)]
    assert len(result.records) == 1
    assert "dropping 'ingredients'" in result.caveats[-1]
