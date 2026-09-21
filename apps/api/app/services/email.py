import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from functools import lru_cache
from typing import Protocol

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class EmailDeliveryError(RuntimeError):
    """Raised when a transactional email cannot be delivered."""


class EmailSender(Protocol):
    async def send_login_code(self, recipient: str, code: str, expires_in_minutes: int) -> None:
        """Send a passwordless login code."""


class ConsoleEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_login_code(self, recipient: str, code: str, expires_in_minutes: int) -> None:
        if self._settings.app_env == "production":
            raise EmailDeliveryError("Console email delivery is disabled in production")
        logger.warning(
            "Development email OTP for %s: %s (expires in %s minutes)",
            recipient,
            code,
            expires_in_minutes,
        )


class SMTPEmailSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_login_code(self, recipient: str, code: str, expires_in_minutes: int) -> None:
        await asyncio.to_thread(self._send, recipient, code, expires_in_minutes)

    def _send(self, recipient: str, code: str, expires_in_minutes: int) -> None:
        settings = self._settings
        if settings.smtp_host is None or settings.smtp_from_email is None:
            raise EmailDeliveryError("SMTP_HOST and SMTP_FROM_EMAIL must be configured")

        message = EmailMessage()
        message["Subject"] = "Your Dr. Insilico login code"
        message["From"] = settings.smtp_from_email
        message["To"] = recipient
        message.set_content(
            f"Your Dr. Insilico login code is {code}. "
            f"It expires in {expires_in_minutes} minutes and can only be used once."
        )

        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.ehlo()
                if settings.smtp_start_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if settings.smtp_username is not None and settings.smtp_password is not None:
                    server.login(
                        settings.smtp_username,
                        settings.smtp_password.get_secret_value(),
                    )
                server.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise EmailDeliveryError("Unable to deliver login email") from error


@lru_cache
def get_email_sender() -> EmailSender:
    settings = get_settings()
    if settings.email_delivery_mode == "smtp":
        return SMTPEmailSender(settings)
    return ConsoleEmailSender(settings)
