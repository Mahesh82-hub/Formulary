"""Queries behind the news feed, the chat tool, and watch previews."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import Date, func, literal, select, tuple_
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.intelligence.types import SIGNIFICANCE_RANK
from app.models import RegulatoryEvent


def significance_at_least(minimum: str) -> list[str]:
    floor = SIGNIFICANCE_RANK.get(minimum, 0)
    return [level for level, rank in SIGNIFICANCE_RANK.items() if rank >= floor]


def encode_cursor(event: RegulatoryEvent) -> str:
    return f"{event.occurred_on.isoformat()}_{event.id}"


def decode_cursor(cursor: str) -> tuple[date, UUID]:
    try:
        day, identifier = cursor.split("_", 1)
        return date.fromisoformat(day), UUID(identifier)
    except ValueError as error:
        raise ValueError("Invalid feed cursor") from error


async def list_events(
    session: AsyncSession,
    *,
    min_significance: str = "low",
    event_types: Sequence[str] = (),
    sources: Sequence[str] = (),
    query: str | None = None,
    since: date | None = None,
    limit: int = 30,
    cursor: str | None = None,
) -> tuple[list[RegulatoryEvent], str | None]:
    """Newest-first feed with keyset pagination.

    Keyset rather than offset pagination keeps pages stable while the monitor inserts new
    events at the top of the feed.
    """
    statement = select(RegulatoryEvent).where(
        RegulatoryEvent.significance.in_(significance_at_least(min_significance))
    )
    if event_types:
        statement = statement.where(RegulatoryEvent.event_type.in_(list(event_types)))
    if sources:
        statement = statement.where(RegulatoryEvent.source.in_(list(sources)))
    if since is not None:
        statement = statement.where(RegulatoryEvent.occurred_on >= since)
    normalized = " ".join((query or "").split())
    if normalized:
        statement = statement.where(
            RegulatoryEvent.search_vector.op("@@")(
                func.websearch_to_tsquery("english", normalized)
            )
        )
    if cursor:
        day, identifier = decode_cursor(cursor)
        statement = statement.where(
            tuple_(RegulatoryEvent.occurred_on, RegulatoryEvent.id)
            < tuple_(literal(day, Date()), literal(identifier, PostgreSQLUUID(as_uuid=True)))
        )
    bounded = min(max(1, limit), 100)
    rows = list(
        await session.scalars(
            statement.order_by(RegulatoryEvent.occurred_on.desc(), RegulatoryEvent.id.desc())
            .limit(bounded + 1)
        )
    )
    next_cursor = encode_cursor(rows[bounded - 1]) if len(rows) > bounded else None
    return rows[:bounded], next_cursor


async def significance_counts(session: AsyncSession, *, since: date) -> dict[str, int]:
    rows = await session.execute(
        select(RegulatoryEvent.significance, func.count())
        .where(RegulatoryEvent.occurred_on >= since)
        .group_by(RegulatoryEvent.significance)
    )
    counts = {level: 0 for level in SIGNIFICANCE_RANK}
    counts.update({level: count for level, count in rows.all()})
    return counts
