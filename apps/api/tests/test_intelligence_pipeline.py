import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.api.dependencies.auth import get_current_user
from app.core.config import get_settings
from app.db.session import async_session_factory, engine
from app.intelligence.monitor import RegulatoryMonitor, get_regulatory_monitor
from app.intelligence.notifications import NotificationDispatcher, NotificationError
from app.intelligence.types import DetectionResult, EventDraft, SnapshotReader, SnapshotUpdate
from app.main import app
from app.models import MonitorRun, RegulatoryEvent, SourceSnapshot, User, Watch, WatchDelivery

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
TODAY = date(2026, 9, 24)


def _draft(key: str, *, drug: str, significance: str = "high", **extra: Any) -> EventDraft:
    return EventDraft(
        dedup_key=key,
        source="openFDA",
        event_type=extra.pop("event_type", "manufacturing_change"),
        significance=significance,  # type: ignore[arg-type]
        headline=extra.pop("headline", f"FDA approved a manufacturing change for {drug}"),
        summary="Supplement approved.",
        subject=drug,
        occurred_on=extra.pop("occurred_on", TODAY),
        source_url="https://www.accessdata.fda.gov/letter.pdf",
        drug_names=(drug,),
        sponsor="Test Pharma",
        **extra,
    )


class StubDetector:
    overlap_days = 2

    def __init__(self, name: str, events: list[EventDraft], *, fail: bool = False) -> None:
        self.name = name
        self.source = "openFDA"
        self._events = events
        self._fail = fail
        self.windows: list[tuple[date, date]] = []

    async def detect(
        self, *, since: date, until: date, snapshots: SnapshotReader
    ) -> DetectionResult:
        self.windows.append((since, until))
        if self._fail:
            raise RuntimeError("upstream exploded")
        return DetectionResult(
            events=list(self._events),
            snapshots=[SnapshotUpdate(f"test:{self.name}", "entity-1", {"v": len(self.windows)})],
            records_scanned=len(self._events),
            baselines_recorded=1,
        )


def _monitor(*detectors: StubDetector) -> RegulatoryMonitor:
    return RegulatoryMonitor(
        detectors,
        session_factory=async_session_factory,
        engine=engine,
        initial_lookback_days=7,
        today=lambda: TODAY,
    )


async def _cleanup(prefix: str, detectors: list[str]) -> None:
    async with async_session_factory() as session:
        await session.execute(
            delete(RegulatoryEvent).where(RegulatoryEvent.dedup_key.like(f"{prefix}%"))
        )
        await session.execute(delete(MonitorRun).where(MonitorRun.detector.in_(detectors)))
        await session.execute(
            delete(SourceSnapshot).where(
                SourceSnapshot.source.in_([f"test:{name}" for name in detectors])
            )
        )
        await session.commit()


async def test_monitor_persists_events_and_rescans_overlapping_windows_without_duplicates() -> None:
    prefix = f"test-{uuid4()}:"
    name = f"stub-{uuid4().hex[:8]}"
    detector = StubDetector(
        name, [_draft(f"{prefix}a", drug="Alpha"), _draft(f"{prefix}b", drug="Beta")]
    )
    monitor = _monitor(detector)
    try:
        first = await monitor.run_once()
        second = await monitor.run_once()

        assert first.outcomes[0].events_created == 2
        # The overlapping rescan re-finds the same changes; dedup keys make it free.
        assert second.outcomes[0].events_created == 0
        # The first run looks back 7 days plus the detector's 2-day publication lag; later runs
        # start that lag before the previous window's end.
        assert detector.windows == [
            (TODAY - timedelta(days=9), TODAY),
            (TODAY - timedelta(days=2), TODAY),
        ]
        async with async_session_factory() as session:
            stored = await session.scalars(
                select(RegulatoryEvent.dedup_key).where(
                    RegulatoryEvent.dedup_key.like(f"{prefix}%")
                )
            )
            assert sorted(stored) == [f"{prefix}a", f"{prefix}b"]
            snapshot = await session.scalar(
                select(SourceSnapshot).where(SourceSnapshot.source == f"test:{name}")
            )
            assert snapshot is not None and snapshot.state == {"v": 2}
    finally:
        await _cleanup(prefix, [name])


async def test_a_failing_detector_is_isolated_and_its_window_is_retried() -> None:
    prefix = f"test-{uuid4()}:"
    healthy_name, broken_name = f"ok-{uuid4().hex[:8]}", f"bad-{uuid4().hex[:8]}"
    healthy = StubDetector(healthy_name, [_draft(f"{prefix}ok", drug="Gamma")])
    broken = StubDetector(broken_name, [], fail=True)
    monitor = _monitor(healthy, broken)
    try:
        summary = await monitor.run_once()

        statuses = {outcome.detector: outcome.status for outcome in summary.outcomes}
        assert statuses == {healthy_name: "completed", broken_name: "failed"}
        assert summary.events_created == 1
        failed = next(o for o in summary.outcomes if o.detector == broken_name)
        assert failed.error == "upstream exploded"

        # With no successful run on record, the broken detector's next window starts from the
        # initial lookback again, so nothing it should have seen is skipped.
        await monitor.run_once()
        assert broken.windows[1][0] == TODAY - timedelta(days=9)
    finally:
        await _cleanup(prefix, [healthy_name, broken_name])


async def test_concurrent_runs_are_serialised_by_the_advisory_lock() -> None:
    name = f"slow-{uuid4().hex[:8]}"

    class SlowDetector(StubDetector):
        async def detect(self, **kwargs: Any) -> DetectionResult:
            await asyncio.sleep(0.3)
            return await super().detect(**kwargs)

    monitor = _monitor(SlowDetector(name, []))
    try:
        results = await asyncio.gather(monitor.run_once(), monitor.run_once())
        assert sorted(result.status for result in results) == ["completed", "skipped"]
    finally:
        await _cleanup("never-matches", [name])


class RecordingEmail:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_digest(self, recipient: str, subject: str, body: str) -> None:
        self.sent.append((recipient, subject, body))


class RecordingSlack:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.payloads: list[dict[str, object]] = []

    async def send(self, webhook_url: str, payload: dict[str, object]) -> None:
        if self.fail:
            raise NotificationError("Slack rejected the message (403)")
        self.payloads.append(payload)


async def _user_with_watch(drug: str, **watch_fields: Any) -> tuple[User, Watch]:
    async with async_session_factory() as session:
        user = User(email=f"watch-{uuid4()}@example.com")
        session.add(user)
        await session.flush()
        watch = Watch(user_id=user.id, name=f"{drug} watch", terms=[drug], **watch_fields)
        session.add(watch)
        await session.commit()
        await session.refresh(user)
        await session.refresh(watch)
    return user, watch


async def _insert(draft: EventDraft, *, detected_at: datetime | None = None) -> None:
    from app.intelligence.store import insert_events

    async with async_session_factory() as session:
        await insert_events(session, [draft])
        if detected_at is not None:
            event = await session.scalar(
                select(RegulatoryEvent).where(RegulatoryEvent.dedup_key == draft.dedup_key)
            )
            assert event is not None
            event.detected_at = detected_at
        await session.commit()


async def test_matching_events_are_delivered_exactly_once_and_history_is_not_replayed() -> None:
    drug = f"Drug{uuid4().hex[:8]}"
    prefix = f"test-{uuid4()}:"
    user, _ = await _user_with_watch(drug)
    email, slack = RecordingEmail(), RecordingSlack()
    dispatcher = NotificationDispatcher(
        session_factory=async_session_factory, email=email, slack=slack, settings=get_settings()
    )
    try:
        # Detected before the watch existed: must not be sent.
        await _insert(
            _draft(f"{prefix}old", drug=drug),
            detected_at=datetime.now(UTC) - timedelta(days=1),
        )
        await _insert(_draft(f"{prefix}new", drug=drug))
        await _insert(_draft(f"{prefix}low", drug=drug, significance="low"))  # below the floor
        await _insert(_draft(f"{prefix}other", drug="SomethingElse"))

        first = await dispatcher.dispatch()
        await dispatcher.dispatch()

        # Counted after both cycles: one digest proves the second cycle sent nothing new.
        mine = [sent for sent in email.sent if sent[0] == user.email]
        assert len(mine) == 1
        _, subject, body = mine[0]
        assert "1 new change" in subject and "(1 high significance)" in subject
        assert f"FDA approved a manufacturing change for {drug}" in body
        assert "Source: https://www.accessdata.fda.gov/letter.pdf" in body
        assert first.events_delivered >= 1
    finally:
        await _cleanup(prefix, [])
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


async def test_a_failed_slack_delivery_is_recorded_and_retried_next_cycle() -> None:
    drug = f"Drug{uuid4().hex[:8]}"
    prefix = f"test-{uuid4()}:"
    user, watch = await _user_with_watch(
        drug,
        notify_email=False,
        slack_webhook_url="https://hooks.slack.com/services/T000/B000/xyz1",
    )
    slack = RecordingSlack(fail=True)
    dispatcher = NotificationDispatcher(
        session_factory=async_session_factory,
        email=RecordingEmail(),
        slack=slack,
        settings=get_settings(),
    )
    try:
        await _insert(_draft(f"{prefix}e", drug=drug))

        await dispatcher.dispatch()
        async with async_session_factory() as session:
            stored = await session.get(Watch, watch.id)
            assert stored is not None
            assert stored.last_delivery_error == "slack: Slack rejected the message (403)"
            delivered = await session.scalars(
                select(WatchDelivery).where(WatchDelivery.watch_id == watch.id)
            )
            assert list(delivered) == []

        slack.fail = False
        await dispatcher.dispatch()
        assert any(drug in str(payload) for payload in slack.payloads)
        async with async_session_factory() as session:
            stored = await session.get(Watch, watch.id)
            assert stored is not None and stored.last_delivery_error is None
    finally:
        await _cleanup(prefix, [])
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()


async def test_watch_api_hides_credentials_and_scopes_watches_to_their_owner() -> None:
    async with async_session_factory() as session:
        owner = User(email=f"owner-{uuid4()}@example.com")
        stranger = User(email=f"stranger-{uuid4()}@example.com")
        session.add_all([owner, stranger])
        await session.commit()
        await session.refresh(owner)
        await session.refresh(stranger)

    class NoBaselines:
        async def capture_label_baselines(self, terms: list[str]) -> int:
            return 0

    app.dependency_overrides[get_regulatory_monitor] = lambda: NoBaselines()
    webhook = "https://hooks.slack.com/services/T111/B222/secretvalue9"
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            app.dependency_overrides[get_current_user] = lambda: owner
            created = await client.post(
                "/api/v1/intelligence/watches",
                json={
                    "name": "GLP-1 competitors",
                    "terms": ["semaglutide", " Semaglutide ", "tirzepatide"],
                    "slack_webhook_url": webhook,
                },
            )
            assert created.status_code == 201
            body = created.json()
            assert body["terms"] == ["semaglutide", "tirzepatide"]
            assert body["slack_configured"] is True
            assert "secretvalue" not in created.text
            assert body["slack_webhook_hint"].endswith("lue9")

            rejected = await client.post(
                "/api/v1/intelligence/watches",
                json={"name": "x", "terms": ["a"], "slack_webhook_url": "https://evil.test/hook"},
            )
            assert rejected.status_code == 422

            app.dependency_overrides[get_current_user] = lambda: stranger
            assert (await client.get("/api/v1/intelligence/watches")).json() == []
            probe = await client.delete(f"/api/v1/intelligence/watches/{body['id']}")
            assert probe.status_code == 404

            app.dependency_overrides[get_current_user] = lambda: owner
            cleared = await client.patch(
                f"/api/v1/intelligence/watches/{body['id']}", json={"clear_slack": True}
            )
            assert cleared.json()["slack_configured"] is False
    finally:
        app.dependency_overrides.clear()
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id.in_([owner.id, stranger.id])))
            await session.commit()


async def test_feed_filters_by_significance_and_paginates_stably() -> None:
    prefix = f"test-{uuid4()}:"
    drug = f"Feed{uuid4().hex[:8]}"
    async with async_session_factory() as session:
        user = User(email=f"feed-{uuid4()}@example.com")
        session.add(user)
        await session.commit()
        await session.refresh(user)
    for index in range(5):
        await _insert(
            _draft(
                f"{prefix}{index}",
                drug=drug,
                significance="high" if index % 2 == 0 else "low",
                occurred_on=TODAY - timedelta(days=index),
            )
        )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            high = await client.get(
                "/api/v1/intelligence/events", params={"q": drug, "min_significance": "high"}
            )
            assert [e["significance"] for e in high.json()["events"]] == ["high"] * 3

            page_one = await client.get(
                "/api/v1/intelligence/events", params={"q": drug, "limit": 2}
            )
            payload = page_one.json()
            assert len(payload["events"]) == 2 and payload["next_cursor"]
            page_two = await client.get(
                "/api/v1/intelligence/events",
                params={"q": drug, "limit": 2, "cursor": payload["next_cursor"]},
            )
            first_ids = {e["id"] for e in payload["events"]}
            second_ids = {e["id"] for e in page_two.json()["events"]}
            assert first_ids.isdisjoint(second_ids) and len(second_ids) == 2

            bad = await client.get("/api/v1/intelligence/events", params={"cursor": "junk"})
            assert bad.status_code == 400
    finally:
        app.dependency_overrides.clear()
        await _cleanup(prefix, [])
        async with async_session_factory() as session:
            await session.execute(delete(User).where(User.id == user.id))
            await session.commit()
