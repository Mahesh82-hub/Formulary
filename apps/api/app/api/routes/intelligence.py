from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import delete, select

from app.api.dependencies.auth import DatabaseSession, get_current_user
from app.intelligence.matching import event_matches
from app.intelligence.monitor import RegulatoryMonitor, get_regulatory_monitor
from app.intelligence.notifications import NotificationDispatcher, mask_webhook
from app.intelligence.repository import list_events, significance_counts
from app.intelligence.service import get_notification_dispatcher, run_cycle
from app.intelligence.types import EventType, Significance
from app.models import MonitorRun, RegulatoryEvent, User, Watch
from app.schemas.intelligence import (
    EventFeedResponse,
    MonitorRunResponse,
    MonitorTriggerResponse,
    RegulatoryEventResponse,
    SignificanceCounts,
    WatchCreate,
    WatchResponse,
    WatchUpdate,
)

router = APIRouter(prefix="/api/v1/intelligence", tags=["regulatory intelligence"])
CurrentUser = Annotated[User, Depends(get_current_user)]
Monitor = Annotated[RegulatoryMonitor, Depends(get_regulatory_monitor)]
Dispatcher = Annotated[NotificationDispatcher, Depends(get_notification_dispatcher)]
PREVIEW_DAYS = 30


def _watch_response(watch: Watch) -> WatchResponse:
    return WatchResponse(
        id=watch.id,
        name=watch.name,
        terms=watch.terms,
        event_types=watch.event_types,
        min_significance=watch.min_significance,
        notify_email=watch.notify_email,
        slack_configured=watch.slack_webhook_url is not None,
        slack_webhook_hint=mask_webhook(watch.slack_webhook_url),
        active=watch.active,
        last_notified_at=watch.last_notified_at,
        last_delivery_error=watch.last_delivery_error,
        created_at=watch.created_at,
    )


async def _owned_watch(session: DatabaseSession, user: User, watch_id: UUID) -> Watch:
    watch = await session.get(Watch, watch_id)
    # Another user's watch is reported as missing rather than forbidden, so identifiers
    # cannot be probed.
    if watch is None or watch.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watch not found")
    return watch


@router.get("/events", response_model=EventFeedResponse)
async def feed(
    session: DatabaseSession,
    _: CurrentUser,
    min_significance: Significance = "low",
    event_type: Annotated[list[EventType] | None, Query()] = None,
    source: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    days: Annotated[int | None, Query(ge=1, le=365)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: str | None = None,
) -> EventFeedResponse:
    today = datetime.now(UTC).date()
    try:
        events, next_cursor = await list_events(
            session,
            min_significance=min_significance,
            event_types=event_type or (),
            sources=source or (),
            query=q,
            since=today - timedelta(days=days) if days else None,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    counts = await significance_counts(session, since=today - timedelta(days=7))
    return EventFeedResponse(
        events=[RegulatoryEventResponse.model_validate(event) for event in events],
        next_cursor=next_cursor,
        last_7_days=SignificanceCounts(**counts),
    )


@router.get("/events/{event_id}", response_model=RegulatoryEventResponse)
async def get_event(
    event_id: UUID, session: DatabaseSession, _: CurrentUser
) -> RegulatoryEventResponse:
    event = await session.get(RegulatoryEvent, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
    return RegulatoryEventResponse.model_validate(event)


@router.get("/watches", response_model=list[WatchResponse])
async def list_watches(session: DatabaseSession, user: CurrentUser) -> list[WatchResponse]:
    watches = await session.scalars(
        select(Watch).where(Watch.user_id == user.id).order_by(Watch.created_at.desc())
    )
    return [_watch_response(watch) for watch in watches]


@router.post("/watches", response_model=WatchResponse, status_code=status.HTTP_201_CREATED)
async def create_watch(
    payload: WatchCreate,
    session: DatabaseSession,
    user: CurrentUser,
    monitor: Monitor,
    background: BackgroundTasks,
) -> WatchResponse:
    watch = Watch(
        user_id=user.id,
        name=payload.name.strip(),
        terms=payload.terms,
        event_types=list(payload.event_types),
        min_significance=payload.min_significance,
        notify_email=payload.notify_email,
        slack_webhook_url=payload.slack_webhook_url,
    )
    session.add(watch)
    await session.commit()
    await session.refresh(watch)
    # Baseline the watched drugs' current labels now, so their next revision is reported
    # rather than silently becoming the first baseline.
    background.add_task(monitor.capture_label_baselines, payload.terms)
    return _watch_response(watch)


@router.patch("/watches/{watch_id}", response_model=WatchResponse)
async def update_watch(
    watch_id: UUID,
    payload: WatchUpdate,
    session: DatabaseSession,
    user: CurrentUser,
    monitor: Monitor,
    background: BackgroundTasks,
) -> WatchResponse:
    watch = await _owned_watch(session, user, watch_id)
    if payload.name is not None:
        watch.name = payload.name.strip()
    if payload.terms is not None:
        added = [term for term in payload.terms if term not in watch.terms]
        watch.terms = payload.terms
        if added:
            background.add_task(monitor.capture_label_baselines, added)
    if payload.event_types is not None:
        watch.event_types = list(payload.event_types)
    if payload.min_significance is not None:
        watch.min_significance = payload.min_significance
    if payload.notify_email is not None:
        watch.notify_email = payload.notify_email
    if payload.clear_slack:
        watch.slack_webhook_url = None
    elif payload.slack_webhook_url is not None:
        watch.slack_webhook_url = payload.slack_webhook_url
    if payload.active is not None:
        watch.active = payload.active
    await session.commit()
    await session.refresh(watch)
    return _watch_response(watch)


@router.delete("/watches/{watch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watch(watch_id: UUID, session: DatabaseSession, user: CurrentUser) -> None:
    watch = await _owned_watch(session, user, watch_id)
    await session.execute(delete(Watch).where(Watch.id == watch.id))
    await session.commit()


@router.get("/watches/{watch_id}/matches", response_model=list[RegulatoryEventResponse])
async def preview_matches(
    watch_id: UUID, session: DatabaseSession, user: CurrentUser
) -> list[RegulatoryEventResponse]:
    """What this watch would have caught over the last 30 days, including before it existed."""
    watch = await _owned_watch(session, user, watch_id)
    since = datetime.now(UTC).date() - timedelta(days=PREVIEW_DAYS)
    candidates = await session.scalars(
        select(RegulatoryEvent)
        .where(RegulatoryEvent.occurred_on >= since)
        .order_by(RegulatoryEvent.occurred_on.desc(), RegulatoryEvent.id.desc())
        .limit(5_000)
    )
    matched = [
        event
        for event in candidates
        if event_matches(
            event,
            terms=watch.terms,
            event_types=watch.event_types,
            min_significance=watch.min_significance,
        )
    ]
    return [RegulatoryEventResponse.model_validate(event) for event in matched[:50]]


@router.get("/runs", response_model=list[MonitorRunResponse])
async def recent_runs(session: DatabaseSession, _: CurrentUser) -> list[MonitorRunResponse]:
    runs = await session.scalars(
        select(MonitorRun).order_by(MonitorRun.started_at.desc()).limit(30)
    )
    return [MonitorRunResponse.model_validate(run) for run in runs]


@router.post("/runs", response_model=MonitorTriggerResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_run(
    _: CurrentUser, monitor: Monitor, dispatcher: Dispatcher, background: BackgroundTasks
) -> MonitorTriggerResponse:
    """Start a monitoring cycle now. Progress is visible through GET /runs.

    Concurrent triggers are safe: the monitor's advisory lock turns duplicates into no-ops.
    """
    background.add_task(run_cycle, monitor, dispatcher)
    return MonitorTriggerResponse(
        status="started", message="Checking FDA and ClinicalTrials.gov for changes."
    )
