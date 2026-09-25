"""Runs every change detector over the window since its last successful run.

Three guarantees:

* **Isolation.** Detectors run concurrently and independently. One source failing records a
  failed run for that detector and leaves the others untouched - the same principle as
  federated search.
* **No lost changes.** A detector's events, its baseline updates, and its run record are
  written in one transaction. The next window starts from the last *successful* run, so a
  failed run is simply retried.
* **One runner at a time.** A PostgreSQL advisory lock stops two API processes (or the
  scheduler and a manual trigger) from scanning the same window concurrently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.clinicaltrials.client import get_clinicaltrials_client
from app.core.config import get_settings
from app.db.session import async_session_factory, engine
from app.fda.client import get_openfda_client
from app.intelligence.detectors.drugsfda import DrugsFDAApprovalDetector
from app.intelligence.detectors.labels import LabelChangeDetector
from app.intelligence.detectors.trials import TrialChangeDetector
from app.intelligence.store import PostgresSnapshots, insert_events, upsert_snapshots
from app.intelligence.types import ChangeDetector
from app.models import MonitorRun

logger = logging.getLogger(__name__)

# Arbitrary but fixed: identifies the monitor's advisory lock across every process.
MONITOR_LOCK_KEY = 7_302_418_559
POLLING_PAGE_SIZE = 100


@dataclass
class DetectorOutcome:
    detector: str
    status: str
    window_start: date
    window_end: date
    records_scanned: int = 0
    events_created: int = 0
    baselines_recorded: int = 0
    error: str | None = None


@dataclass
class MonitorSummary:
    status: str
    started_at: datetime
    completed_at: datetime
    outcomes: list[DetectorOutcome] = field(default_factory=list)

    @property
    def events_created(self) -> int:
        return sum(outcome.events_created for outcome in self.outcomes)


class RegulatoryMonitor:
    def __init__(
        self,
        detectors: Sequence[ChangeDetector],
        *,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        initial_lookback_days: int = 7,
        today: Callable[[], date] | None = None,
        label_detector: LabelChangeDetector | None = None,
    ) -> None:
        self._detectors = list(detectors)
        self._session_factory = session_factory
        self._engine = engine
        self._initial_lookback_days = initial_lookback_days
        self._today = today or (lambda: datetime.now(UTC).date())
        self._label_detector = label_detector

    async def run_once(
        self, *, since: date | None = None, until: date | None = None
    ) -> MonitorSummary:
        started = datetime.now(UTC)
        async with self._engine.connect() as lock_connection:
            acquired = await lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": MONITOR_LOCK_KEY}
            )
            if not acquired:
                logger.info("Regulatory monitor already running elsewhere; skipping")
                return MonitorSummary("skipped", started, datetime.now(UTC))
            try:
                end = until or self._today()
                outcomes = await asyncio.gather(
                    *(self._run_detector(detector, since, end) for detector in self._detectors)
                )
            finally:
                await lock_connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": MONITOR_LOCK_KEY}
                )
        return MonitorSummary("completed", started, datetime.now(UTC), list(outcomes))

    async def capture_label_baselines(self, terms: Iterable[str]) -> int:
        """Baseline the current labels of newly watched drugs. Returns labels captured."""
        if self._label_detector is None:
            return 0
        result = await self._label_detector.capture_baselines(
            terms, snapshots=PostgresSnapshots(self._session_factory)
        )
        async with self._session_factory() as session:
            await upsert_snapshots(session, result.snapshots)
            await session.commit()
        return result.baselines_recorded

    async def _run_detector(
        self, detector: ChangeDetector, since_override: date | None, until: date
    ) -> DetectorOutcome:
        since = since_override or await self._window_start(detector, until)
        async with self._session_factory() as session:
            run = MonitorRun(
                detector=detector.name, status="running", window_start=since, window_end=until
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        try:
            result = await detector.detect(
                since=since, until=until, snapshots=PostgresSnapshots(self._session_factory)
            )
            async with self._session_factory() as session:
                created = await insert_events(session, result.events)
                await upsert_snapshots(session, result.snapshots)
                stored = await session.get(MonitorRun, run_id)
                if stored is not None:
                    stored.status = "completed"
                    stored.records_scanned = result.records_scanned
                    stored.events_created = created
                    stored.baselines_recorded = result.baselines_recorded
                    stored.completed_at = datetime.now(UTC)
                await session.commit()
        except Exception as error:
            logger.exception("Detector %s failed", detector.name)
            message = (str(error) or type(error).__name__)[:500]
            async with self._session_factory() as session:
                stored = await session.get(MonitorRun, run_id)
                if stored is not None:
                    stored.status = "failed"
                    stored.error = message
                    stored.completed_at = datetime.now(UTC)
                    await session.commit()
            return DetectorOutcome(detector.name, "failed", since, until, error=message)

        return DetectorOutcome(
            detector.name,
            "completed",
            since,
            until,
            records_scanned=result.records_scanned,
            events_created=created,
            baselines_recorded=result.baselines_recorded,
        )

    async def _window_start(self, detector: ChangeDetector, until: date) -> date:
        async with self._session_factory() as session:
            last_end = await session.scalar(
                select(MonitorRun.window_end)
                .where(MonitorRun.detector == detector.name, MonitorRun.status == "completed")
                .order_by(MonitorRun.window_end.desc())
                .limit(1)
            )
        # Every window, including the first, reaches back by the detector's publication lag.
        # openFDA publishes labels about a week after they take effect: on 24 September 2026
        # the newest prescription label was effective 16 September, so a first run that only
        # looked back seven days would have found none at all.
        if last_end is None:
            start = until - timedelta(days=self._initial_lookback_days + detector.overlap_days)
        else:
            start = last_end - timedelta(days=detector.overlap_days)
        return min(start, until)


@lru_cache
def get_regulatory_monitor() -> RegulatoryMonitor:
    settings = get_settings()
    # Polling shares the interactive client's rate budget; see OpenFDAClient.with_max_records.
    openfda = get_openfda_client().with_max_records(POLLING_PAGE_SIZE)
    labels = LabelChangeDetector(openfda)
    detectors: list[ChangeDetector] = [DrugsFDAApprovalDetector(openfda), labels]
    if settings.clinicaltrials_enabled:
        detectors.append(TrialChangeDetector(get_clinicaltrials_client()))
    return RegulatoryMonitor(
        detectors,
        session_factory=async_session_factory,
        engine=engine,
        initial_lookback_days=settings.intelligence_initial_lookback_days,
        label_detector=labels,
    )
