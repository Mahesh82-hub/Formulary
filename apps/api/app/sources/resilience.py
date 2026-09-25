"""Transport resilience shared by every upstream evidence source.

This module is deliberately source-agnostic. openFDA is the first consumer, but PubMed,
ClinicalTrials.gov, and any other free source should reuse the same rate limiting and retry
behaviour rather than reimplementing it. Nothing here may import source-specific modules.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

Sleeper = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]
Jitter = Callable[[float], float]
# Lets a source veto a retry for a response whose status looks transient but is not. openFDA,
# for example, answers a malformed query with HTTP 500, which no amount of retrying will fix.
RetryPredicate = Callable[[httpx.Response], bool]

RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class UpstreamError(RuntimeError):
    """Base class for a safe, user-presentable upstream transport failure."""


class UpstreamTimeoutError(UpstreamError):
    """The upstream source did not respond within the configured timeout."""


class UpstreamUnavailableError(UpstreamError):
    """The upstream source could not be reached."""


class UpstreamRateLimitError(UpstreamError):
    """The upstream source rejected the request with a rate limit response."""


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with full jitter.

    ``max_attempts`` counts the first try, so ``3`` means one request plus two retries.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    retry_statuses: frozenset[int] = RETRYABLE_STATUS_CODES
    respect_retry_after: bool = True
    max_retry_after_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must not be below base_delay_seconds")

    def backoff_seconds(self, attempt: int, *, jitter: Jitter) -> float:
        """Full-jitter backoff for a 1-indexed attempt number."""
        ceiling = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        return jitter(ceiling)


class AsyncTokenBucket:
    """Client-side request pacing for a single upstream source.

    A token bucket smooths bursts without serialising every call, which matters because the
    chat orchestrator may issue several tool calls concurrently within one turn.
    """

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int,
        sleep: Sleeper | None = None,
        monotonic: Monotonic | None = None,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._monotonic: Monotonic = monotonic or time.monotonic
        self._tokens = float(burst)
        self._updated_at = self._monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = self._monotonic()
                elapsed = max(0.0, now - self._updated_at)
                self._updated_at = now
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait_seconds = (1.0 - self._tokens) / self._rate
            await self._sleep(wait_seconds)


class ResilientRequester:
    """Sends HTTP requests for one upstream source with pacing and bounded retries.

    Only idempotent reads should be routed through this class: a retried request is replayed
    verbatim.
    """

    def __init__(
        self,
        *,
        source_name: str,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: AsyncTokenBucket | None = None,
        sleep: Sleeper | None = None,
        jitter: Jitter | None = None,
        retry_predicate: RetryPredicate | None = None,
    ) -> None:
        self._source_name = source_name
        self._policy = retry_policy or RetryPolicy()
        self._rate_limiter = rate_limiter
        self._retry_predicate = retry_predicate
        self._sleep: Sleeper = sleep or asyncio.sleep
        self._jitter: Jitter = jitter or (lambda ceiling: random.uniform(0.0, ceiling))

    async def send(self, client: httpx.AsyncClient, request: httpx.Request) -> httpx.Response:
        """Return the upstream response, retrying transient failures.

        Non-retryable responses are returned unchanged so the caller keeps full control over
        status interpretation; only exhausted retries raise.
        """
        last_response: httpx.Response | None = None
        for attempt in range(1, self._policy.max_attempts + 1):
            final_attempt = attempt == self._policy.max_attempts
            if self._rate_limiter is not None:
                await self._rate_limiter.acquire()
            try:
                response = await client.send(request)
            except httpx.TimeoutException as error:
                if final_attempt:
                    raise UpstreamTimeoutError(f"{self._source_name} timed out") from error
                await self._sleep(self._policy.backoff_seconds(attempt, jitter=self._jitter))
                continue
            except httpx.HTTPError as error:
                if final_attempt:
                    raise UpstreamUnavailableError(
                        f"{self._source_name} could not be reached"
                    ) from error
                await self._sleep(self._policy.backoff_seconds(attempt, jitter=self._jitter))
                continue

            if response.status_code not in self._policy.retry_statuses:
                return response
            if self._retry_predicate is not None and not self._retry_predicate(response):
                return response

            last_response = response
            if final_attempt:
                break
            await self._sleep(self._retry_delay(response, attempt))

        if last_response is not None and last_response.status_code == 429:
            raise UpstreamRateLimitError(f"{self._source_name} rate limit reached")
        if last_response is not None:
            return last_response
        raise UpstreamUnavailableError(f"{self._source_name} could not be reached")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        backoff = self._policy.backoff_seconds(attempt, jitter=self._jitter)
        if not self._policy.respect_retry_after:
            return backoff
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        if retry_after is None:
            return backoff
        return min(retry_after, self._policy.max_retry_after_seconds)


def parse_retry_after(value: str | None) -> float | None:
    """Parse the delay-seconds form of ``Retry-After``.

    The HTTP-date form is intentionally ignored: clock skew between the client and an upstream
    source makes it unreliable, and falling back to jittered backoff is safer than trusting it.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds
