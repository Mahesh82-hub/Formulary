import httpx
import pytest

from app.sources.profile import OPENFDA_PROFILE, SourceProfile
from app.sources.resilience import (
    AsyncTokenBucket,
    ResilientRequester,
    RetryPolicy,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    parse_retry_after,
)


class RecordingSleeper:
    """Captures requested delays instead of waiting, so retry tests stay deterministic."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def _requester(
    *,
    sleeper: RecordingSleeper,
    max_attempts: int = 3,
    rate_limiter: AsyncTokenBucket | None = None,
) -> ResilientRequester:
    return ResilientRequester(
        source_name="test-source",
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=1.0,
            max_delay_seconds=8.0,
        ),
        rate_limiter=rate_limiter,
        sleep=sleeper,
        # Deterministic "full jitter": always take the ceiling.
        jitter=lambda ceiling: ceiling,
    )


async def _send(handler: object, requester: ResilientRequester) -> httpx.Response:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport) as client:
        request = client.build_request("GET", "https://source.test/records.json")
        return await requester.send(client, request)


@pytest.mark.asyncio
async def test_transient_server_error_is_retried_then_succeeds() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"results": []})

    sleeper = RecordingSleeper()
    response = await _send(handler, _requester(sleeper=sleeper))

    assert response.status_code == 200
    assert attempts == 3
    # Exponential backoff: 1s then 2s.
    assert sleeper.delays == [1.0, 2.0]


@pytest.mark.asyncio
async def test_exhausted_retries_on_429_raise_a_rate_limit_error() -> None:
    sleeper = RecordingSleeper()

    with pytest.raises(UpstreamRateLimitError, match="test-source"):
        await _send(lambda _: httpx.Response(429), _requester(sleeper=sleeper))

    # Two waits for three attempts; no wait after the final failure.
    assert len(sleeper.delays) == 2


@pytest.mark.asyncio
async def test_retry_after_header_overrides_computed_backoff() -> None:
    sleeper = RecordingSleeper()
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "4"})
        return httpx.Response(200, json={})

    response = await _send(handler, _requester(sleeper=sleeper))

    assert response.status_code == 200
    assert sleeper.delays == [4.0]


@pytest.mark.asyncio
async def test_absurd_retry_after_is_clamped() -> None:
    sleeper = RecordingSleeper()
    requester = ResilientRequester(
        source_name="test-source",
        retry_policy=RetryPolicy(max_attempts=2, max_retry_after_seconds=30.0),
        sleep=sleeper,
        jitter=lambda ceiling: ceiling,
    )

    with pytest.raises(UpstreamRateLimitError):
        await _send(
            lambda _: httpx.Response(429, headers={"Retry-After": "86400"}),
            requester,
        )

    assert sleeper.delays == [30.0]


@pytest.mark.asyncio
async def test_non_retryable_status_is_returned_immediately() -> None:
    attempts = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(404, json={"error": {"message": "No matches found!"}})

    sleeper = RecordingSleeper()
    response = await _send(handler, _requester(sleeper=sleeper))

    # 404 means "no results" for several sources; retrying it would waste the rate budget.
    assert response.status_code == 404
    assert attempts == 1
    assert sleeper.delays == []


@pytest.mark.asyncio
async def test_timeouts_are_retried_then_surface_as_a_timeout_error() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow", request=request)

    sleeper = RecordingSleeper()
    with pytest.raises(UpstreamTimeoutError, match="timed out"):
        await _send(handler, _requester(sleeper=sleeper))

    assert attempts == 3


@pytest.mark.asyncio
async def test_connection_failures_surface_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    sleeper = RecordingSleeper()
    with pytest.raises(UpstreamUnavailableError, match="could not be reached"):
        await _send(handler, _requester(sleeper=sleeper, max_attempts=2))


@pytest.mark.asyncio
async def test_token_bucket_paces_requests_beyond_its_burst() -> None:
    clock = 0.0
    waits: list[float] = []

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        waits.append(seconds)
        clock += seconds

    bucket = AsyncTokenBucket(rate_per_second=2.0, burst=2, sleep=sleep, monotonic=monotonic)

    # The burst is spent without waiting.
    await bucket.acquire()
    await bucket.acquire()
    assert waits == []

    # The next call must wait for a token to refill at 2/s.
    await bucket.acquire()
    assert waits == [pytest.approx(0.5)]


@pytest.mark.asyncio
async def test_rate_limiter_is_consulted_for_every_attempt() -> None:
    acquired = 0

    class CountingBucket(AsyncTokenBucket):
        async def acquire(self) -> None:
            nonlocal acquired
            acquired += 1

    sleeper = RecordingSleeper()
    bucket = CountingBucket(rate_per_second=100.0, burst=100)

    with pytest.raises(UpstreamRateLimitError):
        await _send(lambda _: httpx.Response(429), _requester(sleeper=sleeper, rate_limiter=bucket))

    # Retries must consume rate budget too, otherwise they amplify an existing overload.
    assert acquired == 3


def test_parse_retry_after_accepts_seconds_and_rejects_the_rest() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(" 0.5 ") == 0.5
    assert parse_retry_after(None) is None
    assert parse_retry_after("-1") is None
    # The HTTP-date form is deliberately unsupported.
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_retry_policy_rejects_incoherent_configuration() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="max_delay_seconds"):
        RetryPolicy(base_delay_seconds=10.0, max_delay_seconds=1.0)


def test_source_profile_overrides_only_supplied_fields() -> None:
    tuned = OPENFDA_PROFILE.with_overrides(rate_per_second=1.5)

    assert tuned.rate_per_second == 1.5
    assert tuned.burst == OPENFDA_PROFILE.burst
    assert tuned.name == "openFDA"
    # The declared default must not be mutated.
    assert OPENFDA_PROFILE.rate_per_second == 3.0


def test_source_profile_builds_an_independent_requester_per_source() -> None:
    pubmed = SourceProfile(name="PubMed", rate_per_second=3.0, burst=3)

    assert pubmed.build_requester() is not OPENFDA_PROFILE.build_requester()


def test_document_allowlist_is_the_union_of_registered_source_hosts() -> None:
    from app.sources.profile import registered_document_hosts

    hosts = registered_document_hosts()

    assert {"fda.gov", "ncbi.nlm.nih.gov", "clinicaltrials.gov"} <= hosts
