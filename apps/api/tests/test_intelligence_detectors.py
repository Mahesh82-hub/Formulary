from datetime import date
from typing import Any

import httpx
import pytest

from app.clinicaltrials.client import ClinicalTrialsClient
from app.fda.client import OpenFDAClient
from app.intelligence.detectors.drugsfda import DrugsFDAApprovalDetector
from app.intelligence.detectors.labels import LabelChangeDetector
from app.intelligence.detectors.trials import TrialChangeDetector
from app.intelligence.types import InMemorySnapshots
from app.sources.resilience import ResilientRequester, RetryPolicy

SINCE = date(2026, 9, 1)
UNTIL = date(2026, 9, 24)


async def _no_sleep(seconds: float) -> None: ...


def _requester() -> ResilientRequester:
    return ResilientRequester(
        source_name="test", retry_policy=RetryPolicy(max_attempts=1), sleep=_no_sleep
    )


def _fda(handler: Any) -> OpenFDAClient:
    return OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=1_000,
        transport=httpx.MockTransport(handler),
        requester=_requester(),
    )


def _page(results: list[dict[str, Any]], total: int | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "meta": {"last_updated": "2026-09-22", "results": {"total": total or len(results)}},
            "results": results,
        },
    )


# --- Drugs@FDA -----------------------------------------------------------------------------


def _application(number: str, submissions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "application_number": number,
        "sponsor_name": "NOVO NORDISK INC",
        "products": [
            {
                "brand_name": "OZEMPIC",
                "dosage_form": "INJECTABLE",
                "route": "SUBCUTANEOUS",
                "active_ingredients": [{"name": "SEMAGLUTIDE", "strength": "2MG"}],
            }
        ],
        "openfda": {"generic_name": ["SEMAGLUTIDE"]},
        "submissions": submissions,
    }


def _submission(kind: str, number: str, code: str, day: str, status: str = "AP") -> dict[str, Any]:
    return {
        "submission_type": kind,
        "submission_number": number,
        "submission_status": status,
        "submission_status_date": day,
        "submission_class_code": code,
        "review_priority": "STANDARD",
        "application_docs": [
            {"type": "Letter", "url": f"https://www.accessdata.fda.gov/{number}ltr.pdf"},
            {"type": "Label", "url": f"https://www.accessdata.fda.gov/{number}lbl.pdf"},
        ],
    }


@pytest.mark.asyncio
async def test_approvals_are_classified_by_submission_class() -> None:
    record = _application(
        "NDA209637",
        [
            _submission("SUPPL", "30", "MANUF (CMC)", "20260910"),
            _submission("SUPPL", "31", "EFFICACY", "20260912"),
            _submission("SUPPL", "32", "LABELING", "20260915"),
            # Outside the window, and not approved: both must be ignored.
            _submission("SUPPL", "29", "LABELING", "20250101"),
            _submission("SUPPL", "33", "EFFICACY", "20260916", status="TA"),
        ],
    )
    detector = DrugsFDAApprovalDetector(_fda(lambda _: _page([record])))

    result = await detector.detect(since=SINCE, until=UNTIL, snapshots=InMemorySnapshots())

    by_type = {event.event_type: event for event in result.events}
    assert set(by_type) == {"manufacturing_change", "new_indication", "labeling_supplement"}
    manufacturing = by_type["manufacturing_change"]
    assert manufacturing.significance == "medium"
    assert manufacturing.headline == (
        "FDA approved a manufacturing or formulation change for Ozempic (Novo Nordisk Inc)"
    )
    assert manufacturing.dedup_key == "drugsfda:NDA209637:SUPPL:30"
    # The approval letter is the most citable document.
    assert manufacturing.source_url.endswith("30ltr.pdf")
    assert by_type["new_indication"].significance == "high"
    assert by_type["labeling_supplement"].significance == "low"
    assert "Semaglutide" in manufacturing.drug_names


@pytest.mark.asyncio
async def test_original_approvals_distinguish_new_drugs_from_generics() -> None:
    nme = _application("NDA999001", [_submission("ORIG", "1", "TYPE 1", "20260911")])
    generic = _application("ANDA219000", [_submission("ORIG", "1", "UNKNOWN", "20260912")])
    detector = DrugsFDAApprovalDetector(_fda(lambda _: _page([nme, generic])))

    result = await detector.detect(since=SINCE, until=UNTIL, snapshots=InMemorySnapshots())

    events = {event.details["application_number"]: event for event in result.events}
    assert events["NDA999001"].event_type == "new_drug_approval"
    assert events["NDA999001"].significance == "high"
    assert "new molecular entity" in events["NDA999001"].headline
    assert events["ANDA219000"].event_type == "generic_approval"
    assert events["ANDA219000"].significance == "medium"


@pytest.mark.asyncio
async def test_approval_scan_pages_until_the_window_is_exhausted() -> None:
    skips: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        skips.append(request.url.params.get("skip", "0"))
        labeling = [_submission("SUPPL", "1", "LABELING", "20260910")]
        if len(skips) == 1:
            return _page([_application(f"NDA1{i:05d}", labeling) for i in range(100)], total=150)
        return _page([_application(f"NDA2{i:05d}", labeling) for i in range(50)], total=150)

    result = await DrugsFDAApprovalDetector(_fda(handler)).detect(
        since=SINCE, until=UNTIL, snapshots=InMemorySnapshots()
    )

    assert skips == ["0", "100"]
    assert result.records_scanned == 150
    assert len(result.events) == 150


# --- Labels --------------------------------------------------------------------------------


def _label(
    set_id: str,
    version: str,
    *,
    generic: str = "METFORMIN HYDROCHLORIDE",
    manufacturer: str = "ACME PHARMA",
    boxed: str | None = None,
    ingredients: str = "METFORMIN HYDROCHLORIDE POVIDONE MAGNESIUM STEARATE",
    indications: str = "Adjunct to diet and exercise.",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "set_id": set_id,
        "version": version,
        "effective_time": "20260915",
        "indications_and_usage": [indications],
        "spl_product_data_elements": [ingredients],
        "openfda": {"generic_name": [generic], "manufacturer_name": [manufacturer]},
    }
    if boxed is not None:
        record["boxed_warning"] = [boxed]
    return record


async def _run_labels(
    records: list[dict[str, Any]], snapshots: InMemorySnapshots
) -> Any:
    detector = LabelChangeDetector(_fda(lambda _: _page(records)))
    result = await detector.detect(since=SINCE, until=UNTIL, snapshots=snapshots)
    snapshots.apply(result.snapshots)
    return result


@pytest.mark.asyncio
async def test_first_sight_of_a_label_is_a_baseline_not_an_event() -> None:
    result = await _run_labels([_label("set-1", "4")], InMemorySnapshots())

    # Without an earlier version there is nothing to compare against.
    assert result.events == []
    assert result.baselines_recorded == 1
    assert len(result.snapshots) == 1


@pytest.mark.asyncio
async def test_an_added_boxed_warning_is_a_high_significance_change() -> None:
    snapshots = InMemorySnapshots()
    await _run_labels([_label("set-1", "4")], snapshots)

    result = await _run_labels(
        [_label("set-1", "5", boxed="Lactic acidosis: postmarketing cases resulted in death.")],
        snapshots,
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.event_type == "label_change"
    assert event.significance == "high"
    assert "boxed warning added" in event.headline
    change = event.details["changes"][0]
    assert change["before"] is None
    assert "Lactic acidosis" in change["after"]
    assert event.source_url == "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=set-1"


@pytest.mark.asyncio
async def test_a_formulation_change_reports_the_ingredients_added_and_removed() -> None:
    snapshots = InMemorySnapshots()
    await _run_labels([_label("set-1", "4")], snapshots)

    result = await _run_labels(
        [
            _label(
                "set-1",
                "5",
                ingredients="METFORMIN HYDROCHLORIDE CROSPOVIDONE MAGNESIUM STEARATE",
            )
        ],
        snapshots,
    )

    event = result.events[0]
    assert "ingredient list changed" in event.headline
    change = event.details["changes"][0]
    assert change["added_terms"] == ["CROSPOVIDONE"]
    assert change["removed_terms"] == ["POVIDONE"]
    assert "Ingredients added: CROSPOVIDONE." in event.summary
    assert "Ingredients removed: POVIDONE." in event.summary


@pytest.mark.asyncio
async def test_reformatting_is_not_mistaken_for_a_change() -> None:
    snapshots = InMemorySnapshots()
    await _run_labels([_label("set-1", "4")], snapshots)

    result = await _run_labels(
        [
            _label(
                "set-1",
                "5",
                # Same ingredients reordered, and the same indication re-wrapped.
                ingredients="MAGNESIUM STEARATE POVIDONE METFORMIN HYDROCHLORIDE",
                indications="Adjunct   to diet\nand exercise.",
            )
        ],
        snapshots,
    )

    assert result.events == []


@pytest.mark.asyncio
async def test_identical_revisions_across_manufacturers_become_one_story() -> None:
    snapshots = InMemorySnapshots()
    makers = ["ACME PHARMA", "GENERICO LABS", "ZENITH RX"]
    await _run_labels(
        [_label(f"set-{i}", "1", manufacturer=maker) for i, maker in enumerate(makers)], snapshots
    )

    boxed = "Lactic acidosis warning."
    result = await _run_labels(
        [_label(f"set-{i}", "2", manufacturer=m, boxed=boxed) for i, m in enumerate(makers)]
        + [_label("other", "1", generic="LISINOPRIL")],
        snapshots,
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert "across 3 products" in event.headline
    assert len(event.details["labels"]) == 3
    assert "class labeling change" in event.summary


@pytest.mark.asyncio
async def test_label_query_is_restricted_to_prescription_drugs_in_the_window() -> None:
    searches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        searches.append(request.url.params["search"])
        return _page([])

    await LabelChangeDetector(_fda(handler)).detect(
        since=SINCE, until=UNTIL, snapshots=InMemorySnapshots()
    )

    assert searches == [
        'effective_time:[20260901 TO 20260924] AND openfda.product_type:"HUMAN PRESCRIPTION DRUG"'
    ]


@pytest.mark.asyncio
async def test_watching_a_drug_captures_baselines_so_its_next_revision_is_reported() -> None:
    detector = LabelChangeDetector(_fda(lambda _: _page([_label("set-1", "4")])))
    snapshots = InMemorySnapshots()

    captured = await detector.capture_baselines(["metformin"], snapshots=snapshots)

    assert captured.events == []
    assert captured.baselines_recorded == 1
    snapshots.apply(captured.snapshots)
    revised = await _run_labels([_label("set-1", "5", boxed="New warning.")], snapshots)
    assert len(revised.events) == 1


# --- Trials --------------------------------------------------------------------------------


def _study(
    nct_id: str,
    *,
    status: str = "RECRUITING",
    phases: list[str] | None = None,
    first_posted: str = "2026-01-10",
    results_posted: str | None = None,
    why_stopped: str | None = None,
) -> dict[str, Any]:
    status_module: dict[str, Any] = {
        "overallStatus": status,
        "studyFirstPostDateStruct": {"date": first_posted},
        "lastUpdatePostDateStruct": {"date": "2026-09-20"},
    }
    if results_posted:
        status_module["resultsFirstPostDateStruct"] = {"date": results_posted}
    if why_stopped:
        status_module["whyStopped"] = why_stopped
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": f"Study {nct_id}"},
            "statusModule": status_module,
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": "Eli Lilly and Company"}},
            "designModule": {"phases": phases or ["PHASE3"]},
            "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "tirzepatide"}]},
        },
        "hasResults": results_posted is not None,
    }


def _trials(pages: list[list[dict[str, Any]]]) -> ClinicalTrialsClient:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        body: dict[str, Any] = {"studies": pages[calls]}
        calls += 1
        if calls < len(pages):
            body["nextPageToken"] = f"token-{calls}"
        return httpx.Response(200, json=body)

    return ClinicalTrialsClient(
        base_url="https://ctgov.test/api/v2",
        timeout_seconds=10,
        transport=httpx.MockTransport(handler),
        requester=_requester(),
    )


@pytest.mark.asyncio
async def test_registrations_and_results_are_reported_without_a_baseline() -> None:
    client = _trials(
        [[
            _study("NCT00000001", first_posted="2026-09-18"),
            _study("NCT00000002", results_posted="2026-09-19", phases=["PHASE2"]),
            _study("NCT00000003"),  # old and unremarkable: baseline only
        ]]
    )

    result = await TrialChangeDetector(client).detect(
        since=SINCE, until=UNTIL, snapshots=InMemorySnapshots()
    )

    kinds = {(event.event_type, event.details["nct_id"]) for event in result.events}
    assert kinds == {
        ("trial_registered", "NCT00000001"),
        ("trial_results_posted", "NCT00000002"),
    }
    results_event = next(e for e in result.events if e.event_type == "trial_results_posted")
    assert results_event.significance == "medium"  # Phase 2
    assert result.baselines_recorded == 3


@pytest.mark.asyncio
async def test_a_phase_3_trial_stopping_early_is_high_significance() -> None:
    snapshots = InMemorySnapshots()
    first = await TrialChangeDetector(_trials([[_study("NCT00000009")]])).detect(
        since=SINCE, until=UNTIL, snapshots=snapshots
    )
    snapshots.apply(first.snapshots)
    assert first.events == []

    second = await TrialChangeDetector(
        _trials([[_study("NCT00000009", status="TERMINATED", why_stopped="Futility")]])
    ).detect(since=SINCE, until=UNTIL, snapshots=snapshots)

    assert len(second.events) == 1
    event = second.events[0]
    assert event.event_type == "trial_status_change"
    assert event.significance == "high"
    assert event.headline == "Phase 3 trial of tirzepatide terminated (Eli Lilly and Company)"
    assert "Recruiting to Terminated" in event.summary
    assert "Reason given: Futility" in event.summary
    assert event.source_url == "https://clinicaltrials.gov/study/NCT00000009"


@pytest.mark.asyncio
async def test_trial_scan_follows_page_tokens() -> None:
    client = _trials([[_study("NCT00000011")], [_study("NCT00000012")]])

    result = await TrialChangeDetector(client).detect(
        since=SINCE, until=UNTIL, snapshots=InMemorySnapshots()
    )

    assert result.records_scanned == 2


def test_capitalised_fda_names_read_naturally() -> None:
    from app.intelligence.detectors.drugsfda import display_name

    assert display_name("ELI LILLY AND CO") == "Eli Lilly and Co"
    assert display_name("THE MEDICINES CO") == "The Medicines Co"
    # Mixed-case names are already deliberate and are left alone.
    assert display_name("mRNA-1283") == "mRNA-1283"
