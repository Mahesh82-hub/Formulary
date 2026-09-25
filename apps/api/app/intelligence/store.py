"""PostgreSQL persistence for detected events and change-detection baselines."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.intelligence.types import EventDraft, SnapshotUpdate
from app.models import RegulatoryEvent, SourceSnapshot

# Keeps each statement well under PostgreSQL's bind-parameter limit.
BATCH_SIZE = 500


class PostgresSnapshots:
    """Reads baselines in short-lived sessions so a long scan holds no connection open."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_many(self, source: str, keys: Iterable[str]) -> dict[str, Mapping[str, Any]]:
        wanted = list(dict.fromkeys(keys))
        found: dict[str, Mapping[str, Any]] = {}
        async with self._session_factory() as session:
            for start in range(0, len(wanted), BATCH_SIZE):
                batch = wanted[start : start + BATCH_SIZE]
                rows = await session.execute(
                    select(SourceSnapshot.entity_key, SourceSnapshot.state).where(
                        SourceSnapshot.source == source,
                        SourceSnapshot.entity_key.in_(batch),
                    )
                )
                found.update({key: state for key, state in rows.all()})
        return found


async def insert_events(session: AsyncSession, events: Sequence[EventDraft]) -> int:
    """Insert new events, silently skipping any already recorded. Returns the number added."""
    unique = list({event.dedup_key: event for event in events}.values())
    created = 0
    for start in range(0, len(unique), BATCH_SIZE):
        batch = unique[start : start + BATCH_SIZE]
        statement = (
            insert(RegulatoryEvent)
            .values(
                [
                    {
                        "dedup_key": event.dedup_key,
                        "source": event.source,
                        "event_type": event.event_type,
                        "significance": event.significance,
                        "headline": event.headline,
                        "summary": event.summary,
                        "subject": event.subject,
                        "drug_names": list(event.drug_names),
                        "sponsor": event.sponsor,
                        "occurred_on": event.occurred_on,
                        "source_url": event.source_url,
                        "details": dict(event.details),
                        "provenance": dict(event.provenance),
                    }
                    for event in batch
                ]
            )
            .on_conflict_do_nothing(index_elements=["dedup_key"])
            .returning(RegulatoryEvent.id)
        )
        created += len((await session.execute(statement)).all())
    return created


async def upsert_snapshots(session: AsyncSession, updates: Sequence[SnapshotUpdate]) -> None:
    # A key seen twice in one scan (overlapping pages) must appear once per statement, or
    # PostgreSQL refuses to update the same row twice.
    latest = {(update.source, update.entity_key): update for update in updates}
    rows = list(latest.values())
    observed = datetime.now(UTC)
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        statement = insert(SourceSnapshot).values(
            [
                {
                    "source": update.source,
                    "entity_key": update.entity_key,
                    "fingerprint": update.fingerprint,
                    "state": dict(update.state),
                    "observed_at": observed,
                }
                for update in batch
            ]
        )
        statement = statement.on_conflict_do_update(
            index_elements=["source", "entity_key"],
            set_={
                "fingerprint": statement.excluded.fingerprint,
                "state": statement.excluded.state,
                "observed_at": statement.excluded.observed_at,
                "updated_at": func.now(),
            },
        )
        await session.execute(statement)
