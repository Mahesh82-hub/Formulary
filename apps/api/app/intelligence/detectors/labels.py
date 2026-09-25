"""Detects meaningful revisions to prescription drug labels.

openFDA serves only the current version of each label, so a change can only be described by
comparing against a baseline captured earlier. The first time a label is seen it becomes a
baseline and produces no event; from then on, a revision to any tracked section does.

Two measurements shaped this detector (September 2026, prescription labels only):

* Composition lives in ``spl_product_data_elements``, present on every prescription label and
  listing active and inactive ingredients. The dedicated ``active_ingredient`` and
  ``inactive_ingredient`` fields are OTC-oriented and appeared on 0% and 2% of prescription
  labels respectively - tracking them would silently miss every formulation change.
* When FDA mandates a class-wide change, every manufacturer of that drug revises its label at
  once. Identical revisions are grouped into one event so a single safety change does not
  become forty items in the feed.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from app.fda.client import OpenFDAClient
from app.fda.models import OPENFDA_MAX_SKIP
from app.intelligence.detectors.drugsfda import display_name
from app.intelligence.types import (
    SIGNIFICANCE_RANK,
    DetectionResult,
    EventDraft,
    Significance,
    SnapshotReader,
    SnapshotUpdate,
    content_hash,
    dedupe_names,
    truncate,
)

SNAPSHOT_SOURCE = "openFDA drug/label"
DAILYMED_URL = "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={}"
PAGE_SIZE = 100
EXCERPT_CHARACTERS = 1_500
PRESCRIPTION_FILTER = 'openfda.product_type:"HUMAN PRESCRIPTION DRUG"'

# section -> (reader-facing name, significance of a change to it)
TRACKED_SECTIONS: dict[str, tuple[str, Significance]] = {
    "boxed_warning": ("boxed warning", "high"),
    "indications_and_usage": ("indications", "high"),
    "contraindications": ("contraindications", "medium"),
    "warnings_and_cautions": ("warnings and precautions", "medium"),
    "warnings": ("warnings", "medium"),
    "dosage_forms_and_strengths": ("dosage forms and strengths", "medium"),
    "spl_product_data_elements": ("ingredients", "medium"),
}
COMPOSITION_SECTION = "spl_product_data_elements"
# Ingredient names in SPL product data are capitalised words; imprint codes and short tokens
# such as "5084;V" are noise for the purpose of spotting a formulation change.
INGREDIENT_TOKEN = re.compile(r"[A-Z][A-Z0-9\-]{3,}")


@dataclass(frozen=True)
class SectionChange:
    section: str
    label: str
    change: str  # added | removed | revised
    significance: Significance
    before: str | None
    after: str | None
    added_terms: tuple[str, ...] = ()
    removed_terms: tuple[str, ...] = ()

    @property
    def phrase(self) -> str:
        if self.section == COMPOSITION_SECTION:
            return "ingredient list changed"
        return f"{self.label} {self.change}"


@dataclass(frozen=True)
class LabelRevision:
    set_id: str
    version: str
    previous_version: str | None
    effective_on: date
    brand: str | None
    generic: str | None
    manufacturer: str | None
    changes: tuple[SectionChange, ...]


class LabelChangeDetector:
    name = "drug_label_changes"
    source = "openFDA"
    overlap_days = 14

    def __init__(self, client: OpenFDAClient) -> None:
        self._client = client

    async def detect(
        self, *, since: date, until: date, snapshots: SnapshotReader
    ) -> DetectionResult:
        search = f"effective_time:[{since:%Y%m%d} TO {until:%Y%m%d}] AND {PRESCRIPTION_FILTER}"
        return await self._scan(search, snapshots=snapshots, emit_events=True)

    async def capture_baselines(
        self, terms: Iterable[str], *, snapshots: SnapshotReader, per_term: int = 25
    ) -> DetectionResult:
        """Record baselines for the current labels of named drugs.

        Called when someone starts watching a drug, so the very next revision of its label is
        reported instead of silently becoming the first baseline.
        """
        combined = DetectionResult()
        for term in terms:
            cleaned = term.replace('"', " ").strip()
            if not cleaned:
                continue
            search = (
                f'(openfda.brand_name:"{cleaned}" OR openfda.generic_name:"{cleaned}") '
                f"AND {PRESCRIPTION_FILTER}"
            )
            partial = await self._scan(
                search, snapshots=snapshots, emit_events=False, max_records=per_term
            )
            combined.snapshots.extend(partial.snapshots)
            combined.records_scanned += partial.records_scanned
            combined.baselines_recorded += partial.baselines_recorded
        return combined

    async def _scan(
        self,
        search: str,
        *,
        snapshots: SnapshotReader,
        emit_events: bool,
        max_records: int | None = None,
    ) -> DetectionResult:
        result = DetectionResult()
        revisions: list[LabelRevision] = []
        skip = 0
        while True:
            limit = PAGE_SIZE if max_records is None else min(PAGE_SIZE, max_records - skip)
            if limit <= 0:
                break
            page = await self._client.query("drug/label", search=search, limit=limit, skip=skip)
            result.records_scanned += page.returned
            labels = [record for record in page.results if _string(record.get("set_id"))]
            baselines = await snapshots.get_many(
                SNAPSHOT_SOURCE, [str(record["set_id"]) for record in labels]
            )
            for record in labels:
                set_id = str(record["set_id"])
                state = label_state(record)
                result.snapshots.append(SnapshotUpdate(SNAPSHOT_SOURCE, set_id, state))
                previous = baselines.get(set_id)
                if previous is None:
                    result.baselines_recorded += 1
                    continue
                revision = compare(record, previous, state)
                if revision is not None:
                    revisions.append(revision)
            skip += limit
            if page.returned < limit or skip > OPENFDA_MAX_SKIP:
                break
            if page.total is not None and skip >= page.total:
                break

        if emit_events:
            provenance = {
                "source": "openFDA",
                "api_url": "https://api.fda.gov/drug/label.json",
                "dataset": "drug/label",
            }
            result.events.extend(group_revisions(revisions, provenance))
        return result


def label_state(record: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a label worth remembering: tracked sections, by hash and excerpt."""
    sections: dict[str, dict[str, Any]] = {}
    for section in TRACKED_SECTIONS:
        text = _section_text(record.get(section))
        if text is None:
            continue
        entry: dict[str, Any] = {
            "hash": content_hash(text),
            "excerpt": truncate(" ".join(text.split()), EXCERPT_CHARACTERS),
        }
        if section == COMPOSITION_SECTION:
            entry["terms"] = sorted(set(INGREDIENT_TOKEN.findall(text.upper())))
        sections[section] = entry
    return {
        "version": _string(record.get("version")),
        "effective_time": _string(record.get("effective_time")),
        "sections": sections,
    }


def compare(
    record: Mapping[str, Any], previous: Mapping[str, Any], current: Mapping[str, Any]
) -> LabelRevision | None:
    before_sections: Mapping[str, Any] = previous.get("sections") or {}
    after_sections: Mapping[str, Any] = current.get("sections") or {}
    changes: list[SectionChange] = []
    for section, (label, significance) in TRACKED_SECTIONS.items():
        before = before_sections.get(section)
        after = after_sections.get(section)
        if before is None and after is None:
            continue
        if before is not None and after is not None and before["hash"] == after["hash"]:
            continue
        if section == COMPOSITION_SECTION and before is not None and after is not None:
            added = sorted(set(after.get("terms", [])) - set(before.get("terms", [])))
            removed = sorted(set(before.get("terms", [])) - set(after.get("terms", [])))
            # Reordering or re-spacing the same ingredients is not a formulation change.
            if not added and not removed:
                continue
            changes.append(
                SectionChange(
                    section,
                    label,
                    "revised",
                    significance,
                    before.get("excerpt"),
                    after.get("excerpt"),
                    tuple(added[:20]),
                    tuple(removed[:20]),
                )
            )
            continue
        change = "added" if before is None else "removed" if after is None else "revised"
        changes.append(
            SectionChange(
                section,
                label,
                change,
                significance,
                before.get("excerpt") if before else None,
                after.get("excerpt") if after else None,
            )
        )
    if not changes:
        return None

    effective = _yyyymmdd(current.get("effective_time")) or date.today()
    raw_openfda = record.get("openfda")
    openfda: Mapping[str, Any] = raw_openfda if isinstance(raw_openfda, dict) else {}
    return LabelRevision(
        set_id=str(record["set_id"]),
        version=str(current.get("version") or "?"),
        previous_version=_string(previous.get("version")),
        effective_on=effective,
        brand=display_name(_first(openfda.get("brand_name"))),
        generic=display_name(_first(openfda.get("generic_name"))),
        manufacturer=display_name(_first(openfda.get("manufacturer_name"))),
        changes=tuple(changes),
    )


def group_revisions(
    revisions: list[LabelRevision], provenance: Mapping[str, Any]
) -> list[EventDraft]:
    """Collapse identical revisions of the same drug across manufacturers into one event."""
    groups: dict[tuple[str, tuple[str, ...]], list[LabelRevision]] = defaultdict(list)
    for revision in revisions:
        drug = (revision.generic or revision.brand or revision.set_id).casefold()
        signature = tuple(sorted(f"{c.section}:{c.change}" for c in revision.changes))
        groups[(drug, signature)].append(revision)

    events: list[EventDraft] = []
    for members in groups.values():
        members.sort(key=lambda item: (item.effective_on, item.set_id))
        lead = members[-1]
        changes = lead.changes
        significance: Significance = max(
            (change.significance for change in changes), key=lambda s: SIGNIFICANCE_RANK[s]
        )
        phrases = ", ".join(change.phrase for change in changes)
        manufacturers = dedupe_names(item.manufacturer for item in members)
        if len(members) == 1:
            subject = lead.brand or lead.generic or lead.set_id
            who = f" ({lead.manufacturer})" if lead.manufacturer else ""
            headline = f"Label updated for {subject}{who}: {phrases}"
            summary = (
                f"Version {lead.version} of the prescribing information took effect on "
                f"{lead.effective_on:%d %b %Y}"
                + (
                    f", replacing version {lead.previous_version}."
                    if lead.previous_version
                    else "."
                )
            )
        else:
            subject = lead.generic or lead.brand or lead.set_id
            headline = f"Labels updated for {subject} across {len(members)} products: {phrases}"
            summary = (
                f"{len(members)} labels from {len(manufacturers)} manufacturers were revised "
                f"between {members[0].effective_on:%d %b} and {lead.effective_on:%d %b %Y}, "
                "which usually indicates an FDA-requested class labeling change."
            )
        composition = next((c for c in changes if c.section == COMPOSITION_SECTION), None)
        if composition is not None:
            if composition.added_terms:
                summary += f" Ingredients added: {', '.join(composition.added_terms)}."
            if composition.removed_terms:
                summary += f" Ingredients removed: {', '.join(composition.removed_terms)}."

        key_material = ",".join(sorted(f"{item.set_id}:{item.version}" for item in members))
        events.append(
            EventDraft(
                dedup_key="label:" + hashlib.sha256(key_material.encode()).hexdigest()[:40],
                source="openFDA",
                event_type="label_change",
                significance=significance,
                headline=truncate(headline, 500),
                summary=summary,
                subject=subject,
                occurred_on=lead.effective_on,
                source_url=DAILYMED_URL.format(lead.set_id),
                drug_names=dedupe_names([lead.brand, lead.generic] + [m.brand for m in members]),
                sponsor=lead.manufacturer if len(members) == 1 else None,
                details={
                    "changes": [
                        {
                            "section": change.section,
                            "label": change.label,
                            "change": change.change,
                            "before": change.before,
                            "after": change.after,
                            "added_terms": list(change.added_terms),
                            "removed_terms": list(change.removed_terms),
                        }
                        for change in changes
                    ],
                    "labels": [
                        {
                            "set_id": item.set_id,
                            "version": item.version,
                            "previous_version": item.previous_version,
                            "manufacturer": item.manufacturer,
                            "effective_on": item.effective_on.isoformat(),
                            "url": DAILYMED_URL.format(item.set_id),
                        }
                        for item in members
                    ],
                },
                provenance=dict(provenance),
            )
        )
    return events


def _section_text(value: object) -> str | None:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = "\n".join(item for item in value if isinstance(item, str))
    else:
        return None
    return text if text.strip() else None


def _first(value: object) -> str | None:
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, str) and item.strip()), None)
    return value if isinstance(value, str) else None


def _string(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _yyyymmdd(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None
