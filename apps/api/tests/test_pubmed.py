import httpx
import pytest

from app.pubmed.client import PubMedClient, PubMedError
from app.pubmed.search import PubMedSearcher
from app.sources.federation import FederatedSearchCoordinator
from app.sources.resilience import ResilientRequester, RetryPolicy

EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <Journal>
          <JournalIssue><PubDate><Year>2025</Year><Month>Mar</Month></PubDate></JournalIssue>
          <Title>Journal of Clinical Pharmacology</Title>
        </Journal>
        <ArticleTitle>Bioequivalence of generic aspirin formulations</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Generic substitution is common.</AbstractText>
          <AbstractText Label="RESULTS">The confidence interval fell within limits.</AbstractText>
        </Abstract>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList><ArticleId IdType="doi">10.1000/example</ArticleId></ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


async def _no_sleep(seconds: float) -> None: ...


def _client(handler: object, *, api_key: str | None = None) -> PubMedClient:
    return PubMedClient(
        api_key=api_key,
        base_url="https://eutils.test/entrez/eutils",
        timeout_seconds=10,
        contact_email="research@example.com",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        requester=ResilientRequester(
            source_name="PubMed",
            retry_policy=RetryPolicy(max_attempts=1),
            sleep=_no_sleep,
        ),
    )


@pytest.mark.asyncio
async def test_two_step_esearch_then_efetch_returns_parsed_articles() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("esearch.fcgi"):
            assert request.url.params["term"] == "aspirin bioequivalence"
            assert request.url.params["tool"] == "dr-insilico"
            return httpx.Response(200, json={"esearchresult": {"idlist": ["12345678"]}})
        assert request.url.params["id"] == "12345678"
        return httpx.Response(200, text=EFETCH_XML)

    articles = await _client(handler).search("aspirin bioequivalence", limit=5)

    assert [call.split("/")[-1] for call in calls] == ["esearch.fcgi", "efetch.fcgi"]
    assert len(articles) == 1
    article = articles[0]
    assert article.pmid == "12345678"
    assert article.title == "Bioequivalence of generic aspirin formulations"
    assert article.journal == "Journal of Clinical Pharmacology"
    assert article.published == "2025 Mar"
    assert article.doi == "10.1000/example"
    assert article.url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    # Labelled abstract sections are preserved rather than flattened.
    assert "BACKGROUND: Generic substitution is common." in article.abstract
    assert "RESULTS:" in article.abstract


@pytest.mark.asyncio
async def test_no_matches_skips_the_second_request() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"esearchresult": {"idlist": []}})

    articles = await _client(handler).search("a query with no matches")

    assert articles == []
    # Fetching zero identifiers would be a wasted request against the rate budget.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_api_key_is_sent_but_never_appears_in_a_citable_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "secret-key"
        return httpx.Response(200, json={"esearchresult": {"idlist": []}})

    client = _client(handler, api_key="secret-key")
    await client.search("aspirin")

    public = client.public_url("esearch.fcgi", {"db": "pubmed", "term": "aspirin"})
    assert "api_key" not in public
    assert "secret-key" not in public


@pytest.mark.asyncio
async def test_malformed_xml_becomes_a_safe_application_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})
        return httpx.Response(200, text="<not-valid-xml")

    with pytest.raises(PubMedError, match="malformed XML"):
        await _client(handler).search("aspirin")


@pytest.mark.asyncio
async def test_records_without_a_title_are_skipped_rather_than_breaking_the_batch() -> None:
    partial = """<?xml version="1.0"?>
    <PubmedArticleSet>
      <PubmedArticle><MedlineCitation><PMID>1</PMID></MedlineCitation></PubmedArticle>
      <PubmedArticle><MedlineCitation><PMID>2</PMID>
        <Article><ArticleTitle>A usable record</ArticleTitle></Article>
      </MedlineCitation></PubmedArticle>
    </PubmedArticleSet>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": ["1", "2"]}})
        return httpx.Response(200, text=partial)

    articles = await _client(handler).search("query")

    assert [article.pmid for article in articles] == ["2"]
    assert articles[0].abstract == "No abstract is available for this record."


@pytest.mark.asyncio
async def test_pubmed_federates_alongside_fda_without_new_transport_code() -> None:
    """The point of the abstraction: a second source needs an adapter, nothing more."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"idlist": ["12345678"]}})
        return httpx.Response(200, text=EFETCH_XML)

    searcher = PubMedSearcher(_client(handler))
    result = await FederatedSearchCoordinator([searcher]).search("aspirin bioequivalence")

    assert len(result.records) == 1
    record = result.records[0]
    assert record.source == "PubMed"
    # A citation must point at the article, not at the search that found it.
    assert record.provenance.api_url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
    assert record.provenance.source == "PubMed"
    assert record.provenance.disclaimer is not None
    assert result.succeeded_sources == ["PubMed"]


@pytest.mark.asyncio
async def test_a_pubmed_outage_does_not_remove_other_sources_from_an_answer() -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    class StubFDA:
        name = "FDA drug label"

        async def search(self, query: str, *, limit: int):  # type: ignore[no-untyped-def]
            from datetime import UTC, datetime

            from app.sources.federation import FederatedRecord
            from app.sources.models import SourceProvenance

            return [
                FederatedRecord(
                    source=self.name,
                    title="Aspirin label",
                    snippet="Indications",
                    external_key="label-1",
                    provenance=SourceProvenance(
                        source="openFDA",
                        api_url="https://api.fda.gov/drug/label.json",
                        retrieved_at=datetime.now(UTC),
                    ),
                )
            ]

    coordinator = FederatedSearchCoordinator(
        [StubFDA(), PubMedSearcher(_client(failing))]
    )
    result = await coordinator.search("aspirin")

    assert result.succeeded_sources == ["FDA drug label"]
    assert result.failed_sources == ["PubMed"]
    assert "PubMed" in result.caveats[0]
