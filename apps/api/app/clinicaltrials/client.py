"""ClinicalTrials.gov client built on the public v2 REST API.

Two callers use it. Federated search asks free-text questions; the regulatory monitor pages
through every drug trial updated in a date window so it can spot status changes, new
registrations, and newly posted results. Both share one rate budget through one requester.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.sources.models import SourceProvenance
from app.sources.profile import CLINICALTRIALS_PROFILE
from app.sources.resilience import (
    ResilientRequester,
    UpstreamRateLimitError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"
TERMS_URL = "https://clinicaltrials.gov/about-site/terms-conditions"
DISCLAIMER = (
    "Listing a study on ClinicalTrials.gov does not mean it has been evaluated by the U.S. "
    "Federal Government. Study information is submitted by sponsors and investigators."
)
MAX_PAGE_SIZE = 1_000
MAX_SUMMARY_CHARACTERS = 2_000
# Only the modules parse_study reads. Measured on a 1,000-study page in September 2026, this
# cut the payload from 30.9 MB to 3.2 MB and the response time from 8.3 s to 2.5 s.
STUDY_FIELDS = ",".join(
    (
        "protocolSection.identificationModule",
        "protocolSection.statusModule",
        "protocolSection.sponsorCollaboratorsModule",
        "protocolSection.designModule.phases",
        "protocolSection.conditionsModule",
        "protocolSection.armsInterventionsModule.interventions",
        "protocolSection.descriptionModule.briefSummary",
        "hasResults",
    )
)
# Statuses that end a trial before it runs its planned course.
EARLY_STOP_STATUSES = frozenset({"TERMINATED", "SUSPENDED", "WITHDRAWN"})


class ClinicalTrialsError(RuntimeError):
    """Safe application error raised for an unsuccessful ClinicalTrials.gov request."""


class TrialRecord(BaseModel):
    """A registered study, reduced to the fields answers and change detection need."""

    nct_id: str
    title: str
    overall_status: str | None = None
    why_stopped: str | None = None
    phases: list[str] = Field(default_factory=list)
    sponsor: str | None = None
    conditions: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    brief_summary: str | None = None
    has_results: bool = False
    first_posted: date | None = None
    last_update_posted: date | None = None
    results_first_posted: date | None = None

    @property
    def url(self) -> str:
        return STUDY_URL.format(nct_id=self.nct_id)

    @property
    def phase_label(self) -> str:
        if not self.phases:
            return "Phase not applicable"
        return "/".join(
            phase.replace("EARLY_", "Early ").replace("PHASE", "Phase ") for phase in self.phases
        )

    @property
    def status_label(self) -> str:
        return (self.overall_status or "UNKNOWN").replace("_", " ").title()


class TrialPage(BaseModel):
    studies: list[TrialRecord]
    next_page_token: str | None = None
    total_count: int | None = None
    api_url: str


class ClinicalTrialsClient:
    """Read-only client for the ClinicalTrials.gov v2 studies endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        requester: ResilientRequester | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._requester = requester or CLINICALTRIALS_PROFILE.build_requester()

    async def search(self, query: str, *, limit: int = 5) -> TrialPage:
        """Relevance-ranked free-text search across every registered study."""
        normalized = " ".join(query.split())
        if not normalized:
            raise ClinicalTrialsError("ClinicalTrials.gov query must not be empty")
        if len(normalized) > 1_000:
            raise ClinicalTrialsError("ClinicalTrials.gov query is too long")
        return await self._studies(
            {"query.term": normalized, "pageSize": str(min(max(1, limit), 50))}
        )

    async def list_updated_studies(
        self,
        *,
        since: date,
        until: date | None = None,
        phases: tuple[str, ...] = ("PHASE2", "PHASE3", "PHASE4"),
        intervention_type: str | None = "DRUG",
        page_size: int = MAX_PAGE_SIZE,
        page_token: str | None = None,
    ) -> TrialPage:
        """One page of studies whose record was updated within the window, newest first.

        The default slice - interventional drug trials in phases 2 to 4 - is the part of the
        registry a regulatory team acts on. Measured in September 2026 it was about 1,400
        updates a week, against about 4,900 across the whole registry.
        """
        if until is not None and until < since:
            raise ClinicalTrialsError("The monitoring window ends before it starts")
        upper = until.isoformat() if until is not None else "MAX"
        clauses = [f"AREA[LastUpdatePostDate]RANGE[{since.isoformat()},{upper}]"]
        if intervention_type:
            clauses.append(f"AREA[InterventionType]{intervention_type}")
        if phases:
            clauses.append(f"AREA[Phase]({' OR '.join(phases)})")
        params = {
            "filter.advanced": " AND ".join(clauses),
            "sort": "LastUpdatePostDate:desc",
            "pageSize": str(min(max(1, page_size), MAX_PAGE_SIZE)),
            "countTotal": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._studies(params)

    def provenance(self, api_url: str) -> SourceProvenance:
        return SourceProvenance(
            source="ClinicalTrials.gov",
            api_url=api_url,
            retrieved_at=datetime.now(UTC),
            disclaimer=DISCLAIMER,
            terms_url=TERMS_URL,
        )

    async def _studies(self, params: dict[str, str]) -> TrialPage:
        params = {**params, "format": "json", "fields": STUDY_FIELDS}
        url = f"{self._base_url}/studies"
        api_url = str(httpx.URL(url, params=params))
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                request = client.build_request("GET", url, params=params)
                response = await self._requester.send(client, request)
        except UpstreamTimeoutError as error:
            raise ClinicalTrialsError("ClinicalTrials.gov timed out") from error
        except UpstreamRateLimitError as error:
            raise ClinicalTrialsError("ClinicalTrials.gov rate limit reached") from error
        except UpstreamUnavailableError as error:
            raise ClinicalTrialsError("ClinicalTrials.gov could not be reached") from error

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise ClinicalTrialsError(
                f"ClinicalTrials.gov returned HTTP {error.response.status_code}"
            ) from error
        try:
            payload: object = response.json()
        except ValueError as error:
            raise ClinicalTrialsError("ClinicalTrials.gov returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ClinicalTrialsError("ClinicalTrials.gov returned an invalid response")

        raw_studies = payload.get("studies")
        studies: list[TrialRecord] = []
        if isinstance(raw_studies, list):
            for item in raw_studies:
                if isinstance(item, dict):
                    record = parse_study(item)
                    if record is not None:
                        studies.append(record)
        token = payload.get("nextPageToken")
        total = payload.get("totalCount")
        return TrialPage(
            studies=studies,
            next_page_token=token if isinstance(token, str) and token else None,
            total_count=total if isinstance(total, int) else None,
            api_url=api_url,
        )


def parse_study(study: dict[str, Any]) -> TrialRecord | None:
    """Parse one study, tolerating any module the registry omits."""
    protocol = _dict(study.get("protocolSection"))
    identification = _dict(protocol.get("identificationModule"))
    nct_id = identification.get("nctId")
    if not isinstance(nct_id, str) or not nct_id.startswith("NCT"):
        return None
    title = identification.get("briefTitle") or identification.get("officialTitle")
    if not isinstance(title, str) or not title.strip():
        return None

    status = _dict(protocol.get("statusModule"))
    design = _dict(protocol.get("designModule"))
    sponsor = _dict(_dict(protocol.get("sponsorCollaboratorsModule")).get("leadSponsor"))
    conditions = _dict(protocol.get("conditionsModule")).get("conditions")
    interventions = _dict(protocol.get("armsInterventionsModule")).get("interventions")
    summary = _dict(protocol.get("descriptionModule")).get("briefSummary")
    if isinstance(summary, str) and len(summary) > MAX_SUMMARY_CHARACTERS:
        summary = summary[: MAX_SUMMARY_CHARACTERS - 1].rstrip() + "…"

    return TrialRecord(
        nct_id=nct_id,
        title=title.strip(),
        overall_status=_string(status.get("overallStatus")),
        why_stopped=_string(status.get("whyStopped")),
        phases=_strings(design.get("phases")),
        sponsor=_string(sponsor.get("name")),
        conditions=_strings(conditions),
        interventions=[
            name
            for item in (interventions if isinstance(interventions, list) else [])
            if isinstance(item, dict) and (name := _string(item.get("name")))
        ],
        brief_summary=summary if isinstance(summary, str) else None,
        has_results=study.get("hasResults") is True,
        first_posted=_date(_dict(status.get("studyFirstPostDateStruct")).get("date")),
        last_update_posted=_date(_dict(status.get("lastUpdatePostDateStruct")).get("date")),
        results_first_posted=_date(_dict(status.get("resultsFirstPostDateStruct")).get("date")),
    )


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _date(value: object) -> date | None:
    """Parse the registry's dates, which are sometimes only a year and month."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


@lru_cache
def get_clinicaltrials_client() -> ClinicalTrialsClient:
    settings = get_settings()
    profile = CLINICALTRIALS_PROFILE.with_overrides(
        rate_per_second=settings.clinicaltrials_rate_limit_per_second,
        timeout_seconds=settings.clinicaltrials_timeout_seconds,
    )
    return ClinicalTrialsClient(
        base_url=settings.clinicaltrials_base_url,
        timeout_seconds=settings.clinicaltrials_timeout_seconds,
        requester=profile.build_requester(),
    )
