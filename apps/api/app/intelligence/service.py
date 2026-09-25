"""Wiring for the regulatory-intelligence cycle: detect changes, then notify watchers."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from functools import lru_cache

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.intelligence.monitor import MonitorSummary, RegulatoryMonitor
from app.intelligence.notifications import (
    DispatchSummary,
    NotificationDispatcher,
    SlackNotifier,
)
from app.services.email import get_digest_sender

logger = logging.getLogger(__name__)


@lru_cache
def get_notification_dispatcher() -> NotificationDispatcher:
    return NotificationDispatcher(
        session_factory=async_session_factory,
        email=get_digest_sender(),
        slack=SlackNotifier(),
        settings=get_settings(),
    )


async def run_cycle(
    monitor: RegulatoryMonitor,
    dispatcher: NotificationDispatcher | None,
    *,
    since: date | None = None,
    until: date | None = None,
) -> tuple[MonitorSummary, DispatchSummary | None]:
    summary = await monitor.run_once(since=since, until=until)
    if summary.status != "completed" or dispatcher is None:
        return summary, None
    # Notify even when this run found nothing new: an earlier delivery may have failed and
    # still be waiting to be retried.
    return summary, await dispatcher.dispatch()


async def run_forever(
    monitor: RegulatoryMonitor,
    dispatcher: NotificationDispatcher,
    *,
    interval_minutes: int,
    initial_delay_seconds: float = 60,
) -> None:
    """Background loop started by the API process when monitoring is enabled.

    Every failure is contained to one cycle: the loop must survive an upstream outage, a
    database restart, or a bug in one detector, and simply try again next interval.
    """
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            summary, dispatched = await run_cycle(monitor, dispatcher)
            logger.info(
                "Regulatory monitor cycle %s: %d new events, %d digests sent",
                summary.status,
                summary.events_created,
                dispatched.digests_sent if dispatched else 0,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Regulatory monitor cycle failed; retrying next interval")
        await asyncio.sleep(interval_minutes * 60)
