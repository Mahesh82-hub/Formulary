"""Declarative transport profiles for upstream evidence sources.

Every free source has its own published rate limit and its own tolerance for retries. Rather
than adding a new block of environment variables per source, each source declares a
``SourceProfile`` with sane defaults drawn from its published limits. Only sources that need
operator tuning are wired to settings.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.sources.resilience import (
    AsyncTokenBucket,
    ResilientRequester,
    RetryPolicy,
    RetryPredicate,
)


@dataclass(frozen=True)
class SourceProfile:
    """Transport characteristics of a single upstream source.

    ``rate_per_second`` should stay below the source's published ceiling: the limit is
    typically enforced per API key or per IP, so every process sharing a key contributes.
    """

    name: str
    rate_per_second: float
    burst: int
    # Hosts this source vouches for when the ingestion pipeline fetches a document.
    document_hosts: frozenset[str] = frozenset()
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    timeout_seconds: float = 30.0

    def with_overrides(
        self,
        *,
        rate_per_second: float | None = None,
        burst: int | None = None,
        max_attempts: int | None = None,
        base_delay_seconds: float | None = None,
        max_delay_seconds: float | None = None,
        timeout_seconds: float | None = None,
    ) -> SourceProfile:
        """Return a copy with operator-supplied settings applied over the declared defaults."""
        return replace(
            self,
            rate_per_second=self.rate_per_second if rate_per_second is None else rate_per_second,
            burst=self.burst if burst is None else burst,
            max_attempts=self.max_attempts if max_attempts is None else max_attempts,
            base_delay_seconds=(
                self.base_delay_seconds if base_delay_seconds is None else base_delay_seconds
            ),
            max_delay_seconds=(
                self.max_delay_seconds if max_delay_seconds is None else max_delay_seconds
            ),
            timeout_seconds=self.timeout_seconds if timeout_seconds is None else timeout_seconds,
        )

    def build_requester(
        self, *, retry_predicate: RetryPredicate | None = None
    ) -> ResilientRequester:
        return ResilientRequester(
            source_name=self.name,
            retry_predicate=retry_predicate,
            retry_policy=RetryPolicy(
                max_attempts=self.max_attempts,
                base_delay_seconds=self.base_delay_seconds,
                max_delay_seconds=self.max_delay_seconds,
            ),
            rate_limiter=AsyncTokenBucket(
                rate_per_second=self.rate_per_second,
                burst=self.burst,
            ),
        )


# openFDA publishes roughly 240 requests per minute (4/s) for both keyed and unkeyed callers;
# the daily ceiling differs (120,000 keyed, 1,000 unkeyed) and is not enforced client-side.
OPENFDA_PROFILE = SourceProfile(
    name="openFDA",
    rate_per_second=3.0,
    burst=6,
    document_hosts=frozenset({"fda.gov"}),
)

# NCBI E-utilities allow 3 requests/second without an API key and 10 with one. The default
# stays under the unkeyed ceiling; get_pubmed_client raises it when a key is configured.
PUBMED_PROFILE = SourceProfile(
    name="PubMed",
    rate_per_second=2.5,
    burst=3,
    document_hosts=frozenset({"ncbi.nlm.nih.gov"}),
)

# ClinicalTrials.gov asks callers to stay near 50 requests per minute per IP (~0.83/s).
CLINICALTRIALS_PROFILE = SourceProfile(
    name="ClinicalTrials.gov",
    rate_per_second=0.8,
    burst=3,
    document_hosts=frozenset({"clinicaltrials.gov", "cdn.clinicaltrials.gov"}),
)


REGISTERED_PROFILES: tuple[SourceProfile, ...] = (
    OPENFDA_PROFILE,
    PUBMED_PROFILE,
    CLINICALTRIALS_PROFILE,
)


def registered_document_hosts() -> frozenset[str]:
    """Every host a registered source vouches for, used as the document-fetch allowlist."""
    hosts: set[str] = set()
    for profile in REGISTERED_PROFILES:
        hosts |= profile.document_hosts
    return frozenset(hosts)
