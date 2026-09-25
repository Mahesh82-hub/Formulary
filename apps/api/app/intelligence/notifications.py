"""Routes newly detected events to the people watching for them, by email and Slack.

Delivery is exactly-once per watch, event, and channel: a ``watch_deliveries`` row is written
only after a channel accepts the digest, and events already delivered are excluded. A channel
that fails is retried on the next cycle; a failure on one watch or channel never blocks
another. Only events detected after a watch was created are sent, so starting to watch a drug
does not flood the inbox with its history - its past is available as a preview instead.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.intelligence.matching import event_matches
from app.intelligence.types import SIGNIFICANCE_RANK
from app.models import RegulatoryEvent, User, Watch, WatchDelivery
from app.services.email import DigestSender

logger = logging.getLogger(__name__)

SLACK_HOST = "hooks.slack.com"
SIGNIFICANCE_LABEL = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
SLACK_EMOJI = {"high": ":red_circle:", "medium": ":large_orange_circle:", "low": ":white_circle:"}


class NotificationError(RuntimeError):
    """A safe, loggable delivery failure that never contains a credential."""


def validate_slack_webhook(url: str) -> str:
    """Only Slack incoming webhooks are accepted.

    The server POSTs to this URL, so it is an SSRF boundary as much as a credential: an
    unchecked value would let a user make the server call internal addresses.
    """
    parsed = urlparse(url.strip())
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != SLACK_HOST
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not parsed.path.startswith("/services/")
    ):
        raise ValueError("Slack webhook must be an https://hooks.slack.com/services/... URL")
    return url.strip()


def mask_webhook(url: str | None) -> str | None:
    if not url:
        return None
    return f"https://{SLACK_HOST}/services/…{url[-4:]}"


class SlackSender(Protocol):
    async def send(self, webhook_url: str, payload: dict[str, object]) -> None: ...


class SlackNotifier:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def send(self, webhook_url: str, payload: dict[str, object]) -> None:
        url = validate_slack_webhook(webhook_url)
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await client.post(url, json=payload)
        except httpx.HTTPError as error:
            raise NotificationError("Slack could not be reached") from error
        if response.status_code >= 400:
            # Slack's error bodies are short codes such as "invalid_token"; the URL is omitted.
            raise NotificationError(f"Slack rejected the message ({response.status_code})")


@dataclass
class DispatchSummary:
    watches_checked: int = 0
    digests_sent: int = 0
    events_delivered: int = 0
    failures: list[str] = field(default_factory=list)


def order_events(events: Sequence[RegulatoryEvent]) -> list[RegulatoryEvent]:
    """Most significant first, and newest first within the same significance."""
    return sorted(
        events,
        key=lambda event: (
            -SIGNIFICANCE_RANK.get(event.significance, 0),
            -event.occurred_on.toordinal(),
        ),
    )


def format_email(
    watch: Watch, events: Sequence[RegulatoryEvent], app_url: str
) -> tuple[str, str]:
    high = sum(1 for event in events if event.significance == "high")
    noun = "change" if len(events) == 1 else "changes"
    subject = f"Formulary: {len(events)} new {noun} for “{watch.name}”"
    if high:
        subject += f" ({high} high significance)"
    lines = [
        f"{len(events)} new regulatory {noun} matched your watch “{watch.name}”.",
        "",
    ]
    for event in events:
        lines += [
            f"[{SIGNIFICANCE_LABEL.get(event.significance, '')}] {event.headline}",
            f"  {event.occurred_on:%d %b %Y} · {event.source}",
            f"  {event.summary}",
            f"  Source: {event.source_url}",
            "",
        ]
    lines += [
        f"Open the news feed: {app_url.rstrip('/')}/news",
        "Every item links to the official record it was detected from. Confirm details "
        "against that record before acting on them.",
    ]
    return subject, "\n".join(lines)


def format_slack(
    watch: Watch, events: Sequence[RegulatoryEvent], app_url: str
) -> dict[str, object]:
    blocks: list[dict[str, object]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{len(events)} new changes · {watch.name}"},
        }
    ]
    for event in events[:20]:  # Slack caps a message at 50 blocks.
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{SLACK_EMOJI.get(event.significance, '')} *<{event.source_url}|"
                        f"{_slack_escape(event.headline)}>*\n"
                        f"{event.occurred_on:%d %b %Y} · {_slack_escape(event.source)}\n"
                        f"{_slack_escape(event.summary[:280])}"
                    ),
                },
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"<{app_url.rstrip('/')}/news|Open the Formulary feed>"}
            ],
        }
    )
    return {"text": f"{len(events)} new changes for {watch.name}", "blocks": blocks}


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class NotificationDispatcher:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        email: DigestSender,
        slack: SlackSender,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._email = email
        self._slack = slack
        self._settings = settings

    async def dispatch(self) -> DispatchSummary:
        summary = DispatchSummary()
        horizon = datetime.now(UTC) - timedelta(
            days=self._settings.intelligence_notification_horizon_days
        )
        async with self._session_factory() as session:
            events = list(
                await session.scalars(
                    select(RegulatoryEvent).where(RegulatoryEvent.detected_at >= horizon)
                )
            )
            watches = (
                await session.execute(
                    select(Watch, User.email)
                    .join(User, User.id == Watch.user_id)
                    .where(Watch.active.is_(True), User.deleted_at.is_(None))
                )
            ).all()

        for watch, email in watches:
            summary.watches_checked += 1
            matched = [
                event
                for event in events
                if event.detected_at >= watch.created_at
                and event_matches(
                    event,
                    terms=watch.terms,
                    event_types=watch.event_types,
                    min_significance=watch.min_significance,
                )
            ]
            if not matched:
                continue
            channels: list[str] = []
            if watch.notify_email:
                channels.append("email")
            if watch.slack_webhook_url:
                channels.append("slack")
            for channel in channels:
                await self._deliver(watch, email, channel, matched, summary)
        return summary

    async def _deliver(
        self,
        watch: Watch,
        email: str,
        channel: str,
        matched: list[RegulatoryEvent],
        summary: DispatchSummary,
    ) -> None:
        async with self._session_factory() as session:
            delivered = set(
                await session.scalars(
                    select(WatchDelivery.event_id).where(
                        WatchDelivery.watch_id == watch.id, WatchDelivery.channel == channel
                    )
                )
            )
        pending = order_events([event for event in matched if event.id not in delivered])
        pending = pending[: self._settings.intelligence_digest_max_events]
        if not pending:
            return

        app_url = self._settings.app_public_url
        try:
            if channel == "email":
                subject, body = format_email(watch, pending, app_url)
                await self._email.send_digest(email, subject, body)
            else:
                assert watch.slack_webhook_url is not None
                payload = format_slack(watch, pending, app_url)
                await self._slack.send(watch.slack_webhook_url, payload)
        except Exception as error:
            message = f"{channel}: {str(error)[:200] or type(error).__name__}"
            logger.warning("Delivery failed for watch %s on %s", watch.id, channel)
            summary.failures.append(f"{watch.name} - {message}")
            async with self._session_factory() as session:
                stored = await session.get(Watch, watch.id)
                if stored is not None:
                    stored.last_delivery_error = message
                    await session.commit()
            return

        async with self._session_factory() as session:
            await session.execute(
                insert(WatchDelivery)
                .values(
                    [
                        {"watch_id": watch.id, "event_id": event.id, "channel": channel}
                        for event in pending
                    ]
                )
                .on_conflict_do_nothing()
            )
            stored = await session.get(Watch, watch.id)
            if stored is not None:
                stored.last_notified_at = datetime.now(UTC)
                stored.last_delivery_error = None
            await session.commit()
        summary.digests_sent += 1
        summary.events_delivered += len(pending)
