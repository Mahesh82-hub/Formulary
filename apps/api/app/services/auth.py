import hashlib
import hmac
import secrets

from app.core.config import Settings


class AuthConfigurationError(RuntimeError):
    """Raised when authentication secrets are missing or unsafe."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def protected_digest(settings: Settings, value: str) -> str:
    if settings.auth_otp_pepper is None:
        raise AuthConfigurationError("AUTH_OTP_PEPPER is not configured")
    pepper = settings.auth_otp_pepper.get_secret_value()
    if len(pepper) < 32:
        raise AuthConfigurationError("AUTH_OTP_PEPPER must contain at least 32 characters")
    return hmac.new(
        pepper.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def otp_digest(settings: Settings, email: str, code: str) -> str:
    return protected_digest(settings, f"otp:{normalize_email(email)}:{code}")


def request_value_digest(settings: Settings, value: str | None) -> str | None:
    if value is None:
        return None
    return protected_digest(settings, f"request:{value}")
