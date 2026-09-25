"""Detects FDA approvals of new applications and supplements from Drugs@FDA.

Classification follows the submission class codes openFDA actually publishes. Sampled across
June-September 2026, approved submissions broke down as: labeling supplements (~50%), REMS
modifications (~23%), original applications (~20%), and a long tail of efficacy, manufacturing
(CMC) and bioequivalence supplements. A manufacturing supplement is how a formulation or
process change surfaces - the "company changed its drug" case.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.fda.client import OpenFDAClient
from app.fda.models import OPENFDA_MAX_SKIP, OpenFDAResult
from app.intelligence.types import (
    DetectionResult,
    EventDraft,
    EventType,
    Significance,
    SnapshotReader,
    dedupe_names,
)

DRUGS_AT_FDA_URL = (
    "https://www.accessdata.fda.gov/scripts/cder/daf/index.cfm?event=overview.process&ApplNo={}"
)
PAGE_SIZE = 100

# (event type, significance, phrase used in the headline)
SUPPLEMENT_CLASSES: dict[str, tuple[EventType, Significance, str]] = {
    "EFFICACY": ("new_indication", "high", "a new or expanded indication"),
    "MANUF (CMC)": ("manufacturing_change", "medium", "a manufacturing or formulation change"),
    "REMS": ("safety_program_change", "medium", "a safety program (REMS) modification"),
    "BIOEQUIV": ("bioequivalence_supplement", "low", "a bioequivalence supplement"),
    "LABELING": ("labeling_supplement", "low", "labeling changes"),
}


class DrugsFDAApprovalDetector:
    name = "drugsfda_approvals"
    source = "openFDA"
    overlap_days = 14

    def __init__(self, client: OpenFDAClient) -> None:
        self._client = client

    async def detect(
        self, *, since: date, until: date, snapshots: SnapshotReader
    ) -> DetectionResult:
        result = DetectionResult()
        window = f"submissions.submission_status_date:[{since:%Y%m%d} TO {until:%Y%m%d}]"
        skip = 0
        while True:
            page = await self._client.query(
                "drug/drugsfda", search=window, limit=PAGE_SIZE, skip=skip
            )
            result.records_scanned += page.returned
            for record in page.results:
                result.events.extend(self._events(record, page, since=since, until=until))
            skip += PAGE_SIZE
            if page.returned < PAGE_SIZE or skip > OPENFDA_MAX_SKIP:
                break
            if page.total is not None and skip >= page.total:
                break
        return result

    def _events(
        self, record: dict[str, Any], page: OpenFDAResult, *, since: date, until: date
    ) -> list[EventDraft]:
        application = _string(record.get("application_number"))
        if application is None:
            return []
        submissions = record.get("submissions")
        if not isinstance(submissions, list):
            return []

        products = [item for item in record.get("products") or [] if isinstance(item, dict)]
        raw_openfda = record.get("openfda")
        openfda: dict[str, Any] = raw_openfda if isinstance(raw_openfda, dict) else {}
        names = dedupe_names(
            [display_name(_string(item.get("brand_name"))) for item in products]
            + [display_name(name) for name in _strings(openfda.get("brand_name"))]
            + [display_name(name) for name in _strings(openfda.get("generic_name"))]
            + [
                display_name(_string(ingredient.get("name")))
                for item in products
                for ingredient in item.get("active_ingredients") or []
                if isinstance(ingredient, dict)
            ]
        )
        subject = names[0] if names else application
        sponsor = display_name(_string(record.get("sponsor_name")))

        events: list[EventDraft] = []
        for submission in submissions:
            if not isinstance(submission, dict) or submission.get("submission_status") != "AP":
                continue
            approved_on = _yyyymmdd(submission.get("submission_status_date"))
            if approved_on is None or not since <= approved_on <= until:
                continue
            event = self._classify(
                application=application,
                submission=submission,
                approved_on=approved_on,
                subject=subject,
                names=names,
                sponsor=sponsor,
                products=products,
                page=page,
            )
            events.append(event)
        return events

    def _classify(
        self,
        *,
        application: str,
        submission: dict[str, Any],
        approved_on: date,
        subject: str,
        names: tuple[str, ...],
        sponsor: str | None,
        products: list[dict[str, Any]],
        page: OpenFDAResult,
    ) -> EventDraft:
        kind = _string(submission.get("submission_type")) or "UNKNOWN"
        number = _string(submission.get("submission_number")) or "?"
        class_code = _string(submission.get("submission_class_code")) or ""
        class_label = _string(submission.get("submission_class_code_description"))
        priority = _string(submission.get("review_priority"))
        by = f" ({sponsor})" if sponsor else ""

        event_type: EventType
        significance: Significance
        if kind == "ORIG":
            if application.upper().startswith("ANDA"):
                event_type, significance = "generic_approval", "medium"
                headline = f"FDA approved a generic version of {subject}{by}"
            else:
                event_type, significance = "new_drug_approval", "high"
                qualifier = " - a new molecular entity" if class_code == "TYPE 1" else ""
                headline = f"FDA approved {subject}{by}{qualifier}"
        elif class_code in SUPPLEMENT_CLASSES:
            event_type, significance, phrase = SUPPLEMENT_CLASSES[class_code]
            headline = f"FDA approved {phrase} for {subject}{by}"
        else:
            event_type, significance = "other_supplement", "low"
            headline = f"FDA approved a supplement for {subject}{by}"

        documents = [
            {"type": doc.get("type"), "url": doc.get("url"), "date": doc.get("date")}
            for doc in submission.get("application_docs") or []
            if isinstance(doc, dict) and isinstance(doc.get("url"), str)
        ]
        letter = next((doc["url"] for doc in documents if doc["type"] == "Letter"), None)
        label = next((doc["url"] for doc in documents if doc["type"] == "Label"), None)
        digits = re.sub(r"\D", "", application)
        source_url = letter or label or DRUGS_AT_FDA_URL.format(digits)

        submission_label = "Original application" if kind == "ORIG" else f"Supplement {number}"
        parts = [
            f"{submission_label} to {application}"
            + (f" ({class_label})" if class_label else "")
            + f", approved {approved_on:%d %b %Y}"
            + (f" under {priority.lower()} review." if priority else "."),
        ]
        forms = dedupe_names(
            " ".join(
                value for value in (item.get("dosage_form"), item.get("route")) if value
            ).title()
            for item in products
        )
        if forms:
            parts.append(f"Products: {', '.join(forms)}.")
        if documents:
            parts.append("The approval letter and label are linked from the source.")

        return EventDraft(
            dedup_key=f"drugsfda:{application}:{kind}:{number}",
            source=self.source,
            event_type=event_type,
            significance=significance,
            headline=headline,
            summary=" ".join(parts),
            subject=subject,
            occurred_on=approved_on,
            source_url=source_url,
            drug_names=names,
            sponsor=sponsor,
            details={
                "application_number": application,
                "submission_type": kind,
                "submission_number": number,
                "submission_class_code": class_code or None,
                "submission_class": class_label,
                "review_priority": priority,
                "documents": documents,
                "drugs_at_fda_url": DRUGS_AT_FDA_URL.format(digits),
            },
            provenance=page.provenance.model_dump(mode="json"),
        )


SMALL_WORDS = frozenset({"and", "of", "the", "for", "in", "on", "with", "a", "an", "to"})


def display_name(value: str | None) -> str | None:
    """Title-case names FDA publishes in capitals, leaving mixed-case names untouched.

    Connecting words stay lower case after the first word, so "ELI LILLY AND CO" reads as
    "Eli Lilly and Co" rather than "Eli Lilly And Co".
    """
    if not value:
        return None
    cleaned = " ".join(value.split())
    if not cleaned.isupper():
        return cleaned
    words = cleaned.title().split(" ")
    return " ".join(
        word.lower() if index > 0 and word.lower() in SMALL_WORDS else word
        for index, word in enumerate(words)
    )


def _string(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _yyyymmdd(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
