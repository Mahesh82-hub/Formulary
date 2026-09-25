import httpx
import pytest

from app.fda.client import OpenFDAClient, OpenFDAError
from app.sources.resilience import ResilientRequester, RetryPolicy


async def _no_sleep(seconds: float) -> None:
    """Collapse retry backoff so transport tests stay fast and deterministic."""


def _fast_requester(max_attempts: int = 3) -> ResilientRequester:
    return ResilientRequester(
        source_name="openFDA",
        retry_policy=RetryPolicy(max_attempts=max_attempts),
        sleep=_no_sleep,
    )


@pytest.mark.asyncio
async def test_openfda_client_preserves_provenance_without_exposing_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "secret-key"
        assert request.url.params["search"] == 'openfda.generic_name.exact:"aspirin"'
        return httpx.Response(
            200,
            headers={
                "Link": (
                    "<https://api.fda.test/drug/label.json?api_key=secret-key&limit=2"
                    '&search_after=cursor>; rel="next"'
                )
            },
            json={
                "meta": {
                    "disclaimer": "Research use only",
                    "license": "https://open.fda.gov/license/",
                    "terms": "https://open.fda.gov/terms/",
                    "last_updated": "2026-07-07",
                    "results": {"skip": 0, "limit": 2, "total": 42},
                },
                "results": [{"id": "label-1"}, {"id": "label-2"}],
            },
        )

    client = OpenFDAClient(
        api_key="secret-key",
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )

    result = await client.query(
        "drug/label",
        search='openfda.generic_name.exact:"aspirin"',
        limit=2,
    )

    assert result.total == 42
    assert result.returned == 2
    assert result.provenance.dataset_last_updated == "2026-07-07"
    assert "api_key" not in result.provenance.api_url
    assert result.provenance.next_page_url is not None
    assert "api_key" not in result.provenance.next_page_url
    assert "search_after=cursor" in result.provenance.next_page_url


@pytest.mark.asyncio
async def test_openfda_client_treats_no_matches_as_an_empty_result() -> None:
    client = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(404, json={"error": {"message": "No matches found!"}})
        ),
    )

    result = await client.query("drug/shortages", search='generic_name:"not-a-drug"')

    assert result.total == 0
    assert result.results == []
    assert result.returned == 0


@pytest.mark.asyncio
async def test_openfda_client_reports_rate_limits_and_rejects_unsafe_queries() -> None:
    client = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(lambda _: httpx.Response(429)),
        requester=_fast_requester(),
    )

    with pytest.raises(OpenFDAError, match="rate limit"):
        await client.query("drug/event")

    with pytest.raises(OpenFDAError, match="Invalid openFDA search"):
        await client.query("drug/event", search="https://malicious.example")

    with pytest.raises(OpenFDAError, match="limit"):
        await client.query("drug/event", limit=26)


@pytest.mark.asyncio
async def test_openfda_client_recovers_from_a_transient_upstream_error() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={"meta": {"results": {"total": 1}}, "results": [{"id": "label-1"}]},
        )

    client = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
        requester=_fast_requester(),
    )

    result = await client.query("drug/label", search='openfda.generic_name:"aspirin"')

    assert attempts == 2
    assert result.returned == 1


@pytest.mark.asyncio
async def test_openfda_client_still_reports_skip_beyond_the_supported_ceiling() -> None:
    client = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": []})),
        requester=_fast_requester(),
    )

    with pytest.raises(OpenFDAError, match="25000"):
        await client.query("drug/event", skip=25_001)


@pytest.mark.asyncio
async def test_malformed_query_500_is_not_retried() -> None:
    """openFDA reports a parse failure as HTTP 500; replaying it can only fail again."""
    from app.fda.client import openfda_should_retry

    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            500,
            json={"error": {"code": "SERVER_ERROR", "details": "[parse_exception] Encountered"}},
        )

    client = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
        requester=ResilientRequester(
            source_name="openFDA",
            retry_policy=RetryPolicy(max_attempts=3),
            sleep=_no_sleep,
            retry_predicate=openfda_should_retry,
        ),
    )

    with pytest.raises(OpenFDAError, match="HTTP 500"):
        await client.query("drug/label", search="bad query")

    assert attempts == 1


@pytest.mark.asyncio
async def test_genuine_server_errors_are_still_retried() -> None:
    from app.fda.client import openfda_should_retry

    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, json={"meta": {"results": {"total": 0}}, "results": []})

    client = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
        requester=ResilientRequester(
            source_name="openFDA",
            retry_policy=RetryPolicy(max_attempts=3),
            sleep=_no_sleep,
            retry_predicate=openfda_should_retry,
        ),
    )

    await client.query("drug/label", search="aspirin")
    assert attempts == 2


def test_larger_page_client_shares_the_rate_budget() -> None:
    client = OpenFDAClient(
        api_key=None, base_url="https://api.fda.test", timeout_seconds=10, max_records=25
    )

    poller = client.with_max_records(500)

    assert poller._requester is client._requester
    with pytest.raises(ValueError, match="between 1 and 1000"):
        client.with_max_records(5_000)
