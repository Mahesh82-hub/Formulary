import asyncio
from datetime import UTC, datetime

import pytest

from app.sources.federation import (
    FederatedRecord,
    FederatedSearchCoordinator,
    SearchRequest,
)
from app.sources.fusion import fuse_rankings, rank_by_fused_score, reciprocal_rank_score
from app.sources.models import SourceProvenance


def _record(source: str, key: str) -> FederatedRecord:
    return FederatedRecord(
        source=source,
        title=f"{source} {key}",
        snippet="evidence",
        external_key=key,
        provenance=SourceProvenance(
            source=source,
            api_url=f"https://{source}.test/{key}",
            retrieved_at=datetime.now(UTC),
        ),
    )


class StubSearcher:
    def __init__(
        self,
        name: str,
        keys: list[str],
        *,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self._keys = keys
        self._delay = delay
        self._error = error
        self.calls = 0

    async def search(self, request: SearchRequest, *, limit: int) -> list[FederatedRecord]:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return [_record(self.name, key) for key in self._keys[:limit]]


@pytest.mark.asyncio
async def test_every_source_is_queried_and_results_are_merged() -> None:
    openfda = StubSearcher("openFDA", ["a", "b"])
    pubmed = StubSearcher("PubMed", ["c"])
    coordinator = FederatedSearchCoordinator([openfda, pubmed])

    result = await coordinator.search("aspirin pharmacokinetics")

    assert openfda.calls == 1
    assert pubmed.calls == 1
    assert {record.source for record in result.records} == {"openFDA", "PubMed"}
    assert sorted(result.succeeded_sources) == ["PubMed", "openFDA"]


@pytest.mark.asyncio
async def test_sources_are_queried_concurrently_not_sequentially() -> None:
    slow_sources = [StubSearcher(f"source-{index}", ["a"], delay=0.1) for index in range(5)]
    coordinator = FederatedSearchCoordinator(slow_sources, per_source_timeout_seconds=5.0)

    started = asyncio.get_running_loop().time()
    result = await coordinator.search("query")
    elapsed = asyncio.get_running_loop().time() - started

    assert len(result.succeeded_sources) == 5
    # Sequential execution would take at least 0.5s; concurrent stays near one source's delay.
    assert elapsed < 0.3


@pytest.mark.asyncio
async def test_a_failing_source_never_removes_another_sources_evidence() -> None:
    healthy = StubSearcher("openFDA", ["a", "b"])
    broken = StubSearcher("PubMed", [], error=RuntimeError("upstream exploded"))
    coordinator = FederatedSearchCoordinator([healthy, broken])

    result = await coordinator.search("query")

    assert [record.source for record in result.records] == ["openFDA", "openFDA"]
    assert result.failed_sources == ["PubMed"]
    outcome = next(item for item in result.outcomes if item.source == "PubMed")
    assert outcome.status == "error"
    assert outcome.detail == "upstream exploded"


@pytest.mark.asyncio
async def test_a_slow_source_is_dropped_at_its_deadline_and_named() -> None:
    fast = StubSearcher("openFDA", ["a"])
    slow = StubSearcher("SlowSource", ["b"], delay=5.0)
    coordinator = FederatedSearchCoordinator([fast, slow], per_source_timeout_seconds=0.05)

    started = asyncio.get_running_loop().time()
    result = await coordinator.search("query")
    elapsed = asyncio.get_running_loop().time() - started

    # The deadline bounds the answer; the slow source does not block it.
    assert elapsed < 1.0
    assert result.succeeded_sources == ["openFDA"]
    timeout_outcome = next(item for item in result.outcomes if item.source == "SlowSource")
    assert timeout_outcome.status == "timeout"


@pytest.mark.asyncio
async def test_degraded_answers_carry_an_explicit_caveat() -> None:
    coordinator = FederatedSearchCoordinator(
        [
            StubSearcher("openFDA", ["a"]),
            StubSearcher("PubMed", [], error=RuntimeError("down")),
        ]
    )

    result = await coordinator.search("query")

    assert result.caveats
    assert "PubMed" in result.caveats[0]
    assert "incomplete" in result.caveats[0]


@pytest.mark.asyncio
async def test_every_returned_record_is_attributable() -> None:
    coordinator = FederatedSearchCoordinator([StubSearcher("openFDA", ["a"])])

    result = await coordinator.search("query")

    record = result.records[0]
    assert record.source == "openFDA"
    assert record.provenance.api_url.startswith("https://openFDA.test/")
    assert record.provenance.retrieved_at is not None


@pytest.mark.asyncio
async def test_a_source_returning_nothing_is_distinguished_from_one_that_failed() -> None:
    coordinator = FederatedSearchCoordinator(
        [StubSearcher("openFDA", []), StubSearcher("PubMed", [], error=RuntimeError("boom"))]
    )

    result = await coordinator.search("query")

    statuses = {outcome.source: outcome.status for outcome in result.outcomes}
    assert statuses == {"openFDA": "empty", "PubMed": "error"}
    # "Nothing found" is not a failure, so it must not raise the incomplete-answer caveat.
    assert "openFDA" not in "".join(result.caveats)


@pytest.mark.asyncio
async def test_records_are_kept_per_source_rather_than_deduplicated_across_sources() -> None:
    """Two sources describing the same subject are two pieces of evidence, not one.

    Record identity is scoped to the source, so an answer can cite both independently. Fusing
    them would silently discard one source's wording, provenance, and licence terms.
    """
    a = StubSearcher("A", ["shared"])
    b = StubSearcher("B", ["shared"])
    coordinator = FederatedSearchCoordinator([a, b])

    result = await coordinator.search("query")

    assert len(result.records) == 2
    assert {record.source for record in result.records} == {"A", "B"}


@pytest.mark.asyncio
async def test_empty_registry_returns_an_empty_result_rather_than_failing() -> None:
    result = await FederatedSearchCoordinator([]).search("query")

    assert result.records == []
    assert result.outcomes == []


@pytest.mark.asyncio
async def test_blank_queries_are_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        await FederatedSearchCoordinator([StubSearcher("openFDA", ["a"])]).search("   ")


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    scores = fuse_rankings([["x", "y"], ["y", "z"]], k=60)

    # y appears in both lists, so it must beat x which leads only one.
    assert scores["y"] > scores["x"]
    assert rank_by_fused_score(scores, limit=1) == ["y"]


def test_duplicate_items_within_one_ranking_are_not_double_counted() -> None:
    honest = fuse_rankings([["x"]], k=60)
    repeated = fuse_rankings([["x", "x", "x"]], k=60)

    assert honest["x"] == repeated["x"]


def test_fusion_rejects_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="k must be positive"):
        fuse_rankings([["x"]], k=0)
    with pytest.raises(ValueError, match="1-indexed"):
        reciprocal_rank_score(0)


@pytest.mark.asyncio
async def test_real_openfda_adapters_fan_out_across_datasets() -> None:
    """The registered openFDA datasets are queried concurrently and merged with attribution."""
    import httpx

    from app.fda.client import OpenFDAClient
    from app.fda.search import build_openfda_searchers
    from app.sources.resilience import ResilientRequester, RetryPolicy

    queried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queried.append(request.url.path)
        if request.url.path == "/drug/label.json":
            return httpx.Response(
                200,
                json={
                    "meta": {"results": {"total": 1}},
                    "results": [
                        {
                            "id": "label-1",
                            "openfda": {"brand_name": ["Bayer Aspirin"]},
                            "indications_and_usage": ["For the temporary relief of pain."],
                        }
                    ],
                },
            )
        # Every other dataset legitimately has nothing to say about this query.
        return httpx.Response(404, json={"error": {"message": "No matches found!"}})

    async def _no_sleep(seconds: float) -> None: ...

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
        requester=ResilientRequester(
            source_name="openFDA",
            retry_policy=RetryPolicy(max_attempts=1),
            sleep=_no_sleep,
        ),
    )
    coordinator = FederatedSearchCoordinator(build_openfda_searchers(fda))

    result = await coordinator.search("aspirin indications")

    # Every registered dataset was asked, not just the one that matched.
    assert len(queried) == len(set(queried)) >= 6
    assert "/drug/label.json" in queried

    assert len(result.records) == 1
    record = result.records[0]
    assert record.source == "FDA drug label"
    assert "Bayer Aspirin" in record.title
    assert "temporary relief of pain" in record.snippet
    assert record.provenance.source == "openFDA"

    # Datasets with no match report 'empty', which must not be treated as a failure.
    assert result.failed_sources == []
    assert result.caveats == []
