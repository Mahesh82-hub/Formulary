"""PubMed client built on NCBI E-utilities.

PubMed is deliberately a very different shape from openFDA: two requests instead of one
(esearch returns identifiers, efetch returns records), XML instead of JSON, and a rate limit
tied to whether an API key is supplied. It reuses the shared resilience layer unchanged, which
is the point - a new source should not need new transport code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any
from xml.etree import ElementTree

import httpx

from app.core.config import get_settings
from app.sources.models import SourceProvenance
from app.sources.profile import PUBMED_PROFILE
from app.sources.resilience import (
    ResilientRequester,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

PUBMED_ARTICLE_URL = "https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
# NCBI asks callers to identify themselves; an unidentified caller is throttled harder.
DEFAULT_TOOL_NAME = "dr-insilico"
MAX_ABSTRACT_CHARACTERS = 4_000
# E-utilities responses are small; anything larger signals a malformed or hostile payload and
# is refused before it reaches the XML parser.
MAX_RESPONSE_BYTES = 8_000_000


class PubMedError(RuntimeError):
    """Safe application error raised for an unsuccessful PubMed request."""


class PubMedArticle:
    """One PubMed record, reduced to what an answer needs to cite it."""

    def __init__(
        self,
        *,
        pmid: str,
        title: str,
        abstract: str,
        journal: str | None,
        published: str | None,
        doi: str | None,
    ) -> None:
        self.pmid = pmid
        self.title = title
        self.abstract = abstract
        self.journal = journal
        self.published = published
        self.doi = doi

    @property
    def url(self) -> str:
        return PUBMED_ARTICLE_URL.format(pmid=self.pmid)

    def citation(self) -> str:
        parts = [part for part in (self.journal, self.published) if part]
        return " · ".join(parts) if parts else "PubMed"


class PubMedClient:
    """Read-only client for the E-utilities endpoints this application uses."""

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        timeout_seconds: float,
        tool_name: str = DEFAULT_TOOL_NAME,
        contact_email: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        requester: ResilientRequester | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._tool_name = tool_name
        self._contact_email = contact_email
        self._transport = transport
        self._requester = requester or PUBMED_PROFILE.build_requester()

    async def search(self, query: str, *, limit: int = 5) -> list[PubMedArticle]:
        """Return the most relevant articles for a free-text query.

        An empty identifier list is a normal outcome, not an error: it simply means PubMed has
        nothing matching, and the second request is skipped.
        """
        normalized = " ".join(query.split())
        if not normalized:
            raise PubMedError("PubMed query must not be empty")
        if len(normalized) > 1_000:
            raise PubMedError("PubMed query is too long")
        bounded_limit = min(max(1, limit), 50)

        pmids = await self._esearch(normalized, bounded_limit)
        if not pmids:
            return []
        return await self._efetch(pmids)

    def provenance(self, api_url: str) -> SourceProvenance:
        return SourceProvenance(
            source="PubMed",
            api_url=api_url,
            retrieved_at=datetime.now(UTC),
            disclaimer=(
                "PubMed indexes published literature. An indexed study is not an endorsement, "
                "and abstracts omit the limitations stated in the full text."
            ),
            license_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
            terms_url="https://www.ncbi.nlm.nih.gov/home/about/policies/",
        )

    async def _esearch(self, query: str, limit: int) -> list[str]:
        payload = await self._request_json(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": query,
                "retmax": str(limit),
                "retmode": "json",
                "sort": "relevance",
            },
        )
        result = payload.get("esearchresult")
        if not isinstance(result, dict):
            raise PubMedError("PubMed returned an unexpected search response")
        idlist = result.get("idlist")
        if not isinstance(idlist, list):
            return []
        return [str(value) for value in idlist if str(value).isdigit()]

    async def _efetch(self, pmids: list[str]) -> list[PubMedArticle]:
        text = await self._request_text(
            "efetch.fcgi",
            {"db": "pubmed", "id": ",".join(pmids), "retmode": "xml"},
        )
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as error:
            raise PubMedError("PubMed returned malformed XML") from error

        articles: list[PubMedArticle] = []
        for element in root.findall(".//PubmedArticle"):
            article = _parse_article(element)
            if article is not None:
                articles.append(article)
        return articles

    def _params(self, params: dict[str, str]) -> list[tuple[str, str | int | float | bool | None]]:
        merged = dict(params)
        merged["tool"] = self._tool_name
        if self._contact_email:
            merged["email"] = self._contact_email
        items: list[tuple[str, str | int | float | bool | None]] = list(merged.items())
        if self._api_key:
            items.insert(0, ("api_key", self._api_key))
        return items

    async def _send(self, endpoint: str, params: dict[str, str]) -> httpx.Response:
        url = f"{self._base_url}/{endpoint}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                request = client.build_request("GET", url, params=self._params(params))
                response = await self._requester.send(client, request)
        except UpstreamTimeoutError as error:
            raise PubMedError("PubMed timed out") from error
        except UpstreamRateLimitError as error:
            raise PubMedError("PubMed rate limit reached") from error
        except UpstreamUnavailableError as error:
            raise PubMedError("PubMed could not be reached") from error

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise PubMedError(f"PubMed returned HTTP {error.response.status_code}") from error
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise PubMedError("PubMed response exceeded the supported size")
        return response

    async def _request_json(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        response = await self._send(endpoint, params)
        try:
            payload: object = response.json()
        except ValueError as error:
            raise PubMedError("PubMed returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise PubMedError("PubMed returned an invalid response")
        return payload

    async def _request_text(self, endpoint: str, params: dict[str, str]) -> str:
        response = await self._send(endpoint, params)
        return response.text

    def public_url(self, endpoint: str, params: dict[str, str]) -> str:
        """Build the citable request URL, never including the API key."""
        merged = dict(params)
        merged["tool"] = self._tool_name
        return str(httpx.URL(f"{self._base_url}/{endpoint}", params=merged))


def _parse_article(element: ElementTree.Element) -> PubMedArticle | None:
    pmid = _text(element.find(".//PMID"))
    title = _text(element.find(".//ArticleTitle"))
    if not pmid or not title:
        return None

    # An abstract can be split into labelled sections (Background, Methods, ...); joining them
    # preserves the structure the authors intended.
    parts: list[str] = []
    for node in element.findall(".//Abstract/AbstractText"):
        label = node.get("Label")
        body = "".join(node.itertext()).strip()
        if not body:
            continue
        parts.append(f"{label}: {body}" if label else body)
    abstract = " ".join(parts)
    if len(abstract) > MAX_ABSTRACT_CHARACTERS:
        abstract = abstract[: MAX_ABSTRACT_CHARACTERS - 1].rstrip() + "…"

    doi = None
    for node in element.findall(".//ArticleId"):
        if node.get("IdType") == "doi":
            doi = _text(node)
            break

    return PubMedArticle(
        pmid=pmid,
        title=title,
        abstract=abstract or "No abstract is available for this record.",
        journal=_text(element.find(".//Journal/Title")),
        published=_published(element),
        doi=doi,
    )


def _published(element: ElementTree.Element) -> str | None:
    node = element.find(".//Journal/JournalIssue/PubDate")
    if node is None:
        return None
    year = _text(node.find("Year"))
    if year:
        month = _text(node.find("Month"))
        return f"{year} {month}".strip() if month else year
    return _text(node.find("MedlineDate"))


def _text(node: ElementTree.Element | None) -> str | None:
    if node is None:
        return None
    value = "".join(node.itertext()).strip()
    return value or None


@lru_cache
def get_pubmed_client() -> PubMedClient:
    settings = get_settings()
    api_key = settings.pubmed_api_key.get_secret_value() if settings.pubmed_api_key else None
    profile = PUBMED_PROFILE.with_overrides(
        # NCBI allows 10 requests/second with a key and 3 without.
        rate_per_second=settings.pubmed_rate_limit_per_second if api_key is None else 8.0,
        timeout_seconds=settings.pubmed_timeout_seconds,
    )
    return PubMedClient(
        api_key=api_key,
        base_url=settings.pubmed_base_url,
        timeout_seconds=settings.pubmed_timeout_seconds,
        contact_email=settings.pubmed_contact_email,
        requester=profile.build_requester(),
    )
