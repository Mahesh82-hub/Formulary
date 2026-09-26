"""Detects meaningful changes to registered drug trials on ClinicalTrials.gov.

Three kinds of change are reported:

* A trial was registered, and a trial's results were posted. Both carry their own date in the
  registry, so they are detectable from the very first run.
* A trial's status changed - most importantly to terminated, suspended, or withdrawn. The
  registry exposes only the current status, so this needs a baseline from an earlier run.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.clinicaltrials.client import EARLY_STOP_STATUSES, ClinicalTrialsClient, TrialRecord
from app.intelligence.types import (
    DetectionResult,
    EventDraft,
    EventType,
    Significance,
    SnapshotReader,
    SnapshotUpdate,
    dedupe_names,
    truncate,
)

SNAPSHOT_SOURCE = "ClinicalTrials.gov"
# Guards against an unexpectedly broad window: 20 pages of 1,000 is ~14 weeks of the slice.
MAX_PAGES = 20

STATUS_PHRASES = {
    "TERMINATED": "terminated",
    "SUSPENDED": "suspended",
    "WITHDRAWN": "withdrawn",
    "COMPLETED": "completed",
    "RECRUITING": "now recruiting",
    "ACTIVE_NOT_RECRUITING": "closed to enrollment",
    "NOT_YET_RECRUITING": "not yet recruiting",
    "ENROLLING_BY_INVITATION": "enrolling by invitation",
}


class TrialChangeDetector:
    name = "clinical_trial_changes"
    source = "ClinicalTrials.gov"
    overlap_days = 3

    def __init__(self, client: ClinicalTrialsClient) -> None:
        self._client = client

    async def detect(
        self, *, since: date, until: date, snapshots: SnapshotReader
    ) -> DetectionResult:
        result = DetectionResult()
        token: str | None = None
        for _ in range(MAX_PAGES):
            page = await self._client.list_updated_studies(
                since=since, until=until, page_token=token
            )
            result.records_scanned += len(page.studies)
            provenance = self._client.provenance(page.api_url).model_dump(mode="json")
            baselines = await snapshots.get_many(
                SNAPSHOT_SOURCE, [trial.nct_id for trial in page.studies]
            )
            for trial in page.studies:
                state = trial_state(trial)
                result.snapshots.append(SnapshotUpdate(SNAPSHOT_SOURCE, trial.nct_id, state))
                previous = baselines.get(trial.nct_id)
                if previous is None:
                    result.baselines_recorded += 1
                result.events.extend(
                    trial_events(trial, previous, since=since, until=until, provenance=provenance)
                )
            token = page.next_page_token
            if token is None:
                break
        return result


def trial_state(trial: TrialRecord) -> dict[str, Any]:
    return {
        "status": trial.overall_status,
        "why_stopped": trial.why_stopped,
        "has_results": trial.has_results,
        "phases": trial.phases,
        "last_update_posted": (
            trial.last_update_posted.isoformat() if trial.last_update_posted else None
        ),
    }


def trial_events(
    trial: TrialRecord,
    previous: dict[str, Any] | Any | None,
    *,
    since: date,
    until: date,
    provenance: dict[str, Any],
) -> list[EventDraft]:
    is_phase3 = "PHASE3" in trial.phases
    drug = trial.interventions[0] if trial.interventions else trial.nct_id
    by = f" ({trial.sponsor})" if trial.sponsor else ""

    def make(
        *,
        dedup_key: str,
        event_type: EventType,
        significance: Significance,
        headline: str,
        summary: str,
        occurred_on: date,
        details: dict[str, Any],
    ) -> EventDraft:
        return EventDraft(
            dedup_key=dedup_key,
            source=SNAPSHOT_SOURCE,
            event_type=event_type,
            significance=significance,
            headline=truncate(headline, 500),
            summary=summary,
            subject=truncate(drug, 255),
            occurred_on=occurred_on,
            source_url=trial.url,
            drug_names=dedupe_names(trial.interventions),
            sponsor=trial.sponsor,
            details=details,
            provenance=provenance,
        )

    details = {
        "nct_id": trial.nct_id,
        "title": trial.title,
        "phases": trial.phases,
        "status": trial.overall_status,
        "why_stopped": trial.why_stopped,
        "conditions": trial.conditions[:10],
        "interventions": trial.interventions[:10],
    }
    conditions = f" Conditions: {', '.join(trial.conditions[:3])}." if trial.conditions else ""
    events: list[EventDraft] = []

    if trial.first_posted is not None and since <= trial.first_posted <= until:
        significance: Significance = "medium" if is_phase3 else "low"
        events.append(
            make(
                dedup_key=f"nct:{trial.nct_id}:registered",
                event_type="trial_registered",
                significance=significance,
                headline=f"New {trial.phase_label} trial of {drug} registered{by}",
                summary=(
                    f"{trial.nct_id}: {trial.title}. Status: {trial.status_label}.{conditions}"
                ),
                occurred_on=trial.first_posted,
                details=details,
            )
        )

    if trial.results_first_posted is not None and since <= trial.results_first_posted <= until:
        significance = "high" if is_phase3 else "medium"
        events.append(
            make(
                dedup_key=f"nct:{trial.nct_id}:results",
                event_type="trial_results_posted",
                significance=significance,
                headline=f"Results posted for {trial.phase_label} trial of {drug}{by}",
                summary=f"{trial.nct_id}: {trial.title}.{conditions}",
                occurred_on=trial.results_first_posted,
                details=details,
            )
        )

    previous_status = previous.get("status") if isinstance(previous, dict) else None
    if previous is not None and previous_status != trial.overall_status:
        current = trial.overall_status or "UNKNOWN"
        if current in EARLY_STOP_STATUSES:
            significance = "high" if is_phase3 else "medium"
        elif current == "COMPLETED":
            significance = "medium" if is_phase3 else "low"
        else:
            significance = "low"
        phrase = STATUS_PHRASES.get(current, f"status changed to {trial.status_label}")
        before = (previous_status or "UNKNOWN").replace("_", " ").title()
        summary = (
            f"{trial.nct_id}: {trial.title}. Status moved from {before} to {trial.status_label}."
        )
        if trial.why_stopped:
            summary += f" Reason given: {trial.why_stopped}"
        events.append(
            make(
                dedup_key=(
                    f"nct:{trial.nct_id}:status:{previous_status}:{current}:"
                    f"{trial.last_update_posted or 'unknown'}"
                ),
                event_type="trial_status_change",
                significance=significance,
                headline=f"{trial.phase_label} trial of {drug} {phrase}{by}",
                summary=summary + conditions,
                occurred_on=trial.last_update_posted or until,
                details={**details, "previous_status": previous_status},
            )
        )
    return events
