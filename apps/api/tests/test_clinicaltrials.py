from datetime import date

import httpx
import pytest

from app.clinicaltrials.client import ClinicalTrialsClient, ClinicalTrialsError, parse_study
from app.clinicaltrials.search import ClinicalTrialsSearcher
from app.sources.federation import FederatedSearchCoordinator
from app.sources.resilience import ResilientRequester, RetryPolicy


def _study(
    nct_id: str = "NCT01234567",
    *,
    status: str = "RECRUITING",
    why_stopped: str | None = None,
    results_posted: str | None = None,
) -> dict[str, object]:
    status_module: dict[str, object] = {
        "overallStatus": status,
        "studyFirstPostDateStruct": {"date": "2026-09-01", "type": "ACTUAL"},
        "lastUpdatePostDateStruct": {"date": "2026-09-23", "type": "ACTUAL"},
    }
    if why_stopped:
        status_module["whyStopped"] = why_stopped
    if results_posted:
        status_module["resultsFirstPostDateStruct"] = {"date": results_posted}
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": "Semaglutide in obesity"},
            "statusModule": status_module,
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Novo Nordisk A/S"}},
            "designModule": {"phases": ["PHASE3"]},
            "conditionsModule": {"conditions": ["Obesity"]},
            "armsInterventionsModule": {
                "interventions": [{"type": "DRUG", "name": "semaglutide"}]
            },
            "descriptionModule": {"briefSummary": "A randomised trial."},
        },
        "hasResults": results_posted is not None,
    }


async def _no_sleep(seconds: float) -> None: ...


def _client(handler: object) -> ClinicalTrialsClient:
    return ClinicalTrialsClient(
        base_url="https://ctgov.test/api/v2",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        requester=ResilientRequester(
            source_name="ClinicalTrials.gov",
            retry_policy=RetryPolicy(max_attempts=1),
            sleep=_no_sleep,
        ),
    )


def test_study_parsing_extracts_the_fields_change_detection_needs() -> None:
    trial = parse_study(
        _study(status="TERMINATED", why_stopped="Sponsor decision", results_posted="2026-09-20")
    )

    assert trial is not None
    assert trial.nct_id == "NCT01234567"
    assert trial.overall_status == "TERMINATED"
    assert trial.why_stopped == "Sponsor decision"
    assert trial.phases == ["PHASE3"]
    assert trial.phase_label == "Phase 3"
    assert trial.sponsor == "Novo Nordisk A/S"
    assert trial.interventions == ["semaglutide"]
    assert trial.first_posted == date(2026, 9, 1)
    assert trial.results_first_posted == date(2026, 9, 20)
    assert trial.url == "https://clinicaltrials.gov/study/NCT01234567"


def test_partial_dates_and_missing_modules_are_tolerated() -> None:
    minimal = {
        "protocolSection": {
            "identificationModule": {"nctId": "NCT09999999", "officialTitle": "Untitled"},
            "statusModule": {"studyFirstPostDateStruct": {"date": "2026-09"}},
        }
    }

    trial = parse_study(minimal)

    assert trial is not None
    assert trial.first_posted == date(2026, 9, 1)
    assert trial.phases == []
    assert trial.phase_label == "Phase not applicable"
    # A record with no identifier cannot be cited, so it is dropped.
    assert parse_study({"protocolSection": {}}) is None


@pytest.mark.asyncio
async def test_monitoring_query_targets_recent_drug_trials_in_phases_two_to_four() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json={"studies": [_study()], "totalCount": 1})

    page = await _client(handler).list_updated_studies(
        since=date(2026, 9, 17), until=date(2026, 9, 24)
    )

    assert "AREA[LastUpdatePostDate]RANGE[2026-09-17,2026-09-24]" in seen["filter.advanced"]
    assert "AREA[InterventionType]DRUG" in seen["filter.advanced"]
    assert "AREA[Phase](PHASE2 OR PHASE3 OR PHASE4)" in seen["filter.advanced"]
    assert seen["sort"] == "LastUpdatePostDate:desc"
    # Requesting only parsed modules keeps a 1,000-study page to a few megabytes.
    assert "protocolSection.statusModule" in seen["fields"]
    assert "protocolSection.outcomesModule" not in seen["fields"]
    assert page.total_count == 1
    assert page.next_page_token is None


@pytest.mark.asyncio
async def test_pagination_token_is_surfaced_and_forwarded() -> None:
    tokens: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.url.params.get("pageToken"))
        return httpx.Response(200, json={"studies": [], "nextPageToken": "page-2"})

    client = _client(handler)
    first = await client.list_updated_studies(since=date(2026, 9, 17))
    await client.list_updated_studies(since=date(2026, 9, 17), page_token=first.next_page_token)

    assert first.next_page_token == "page-2"
    assert tokens == [None, "page-2"]


@pytest.mark.asyncio
async def test_an_inverted_window_is_rejected_before_any_request() -> None:
    with pytest.raises(ClinicalTrialsError, match="ends before it starts"):
        await _client(lambda _: httpx.Response(200)).list_updated_studies(
            since=date(2026, 9, 24), until=date(2026, 9, 1)
        )


@pytest.mark.asyncio
async def test_trials_federate_with_the_study_page_as_the_citation() -> None:
    client = _client(lambda _: httpx.Response(200, json={"studies": [_study()]}))

    result = await FederatedSearchCoordinator([ClinicalTrialsSearcher(client)]).search(
        "semaglutide obesity"
    )

    record = result.records[0]
    assert record.source == "ClinicalTrials.gov"
    assert record.provenance.api_url == "https://clinicaltrials.gov/study/NCT01234567"
    assert "Phase 3" in record.snippet and "Recruiting" in record.snippet
    assert record.provenance.disclaimer is not None
