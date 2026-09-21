from pydantic import SecretStr

from app.core.config import Settings
from app.services.auth import normalize_email, otp_digest, session_token_digest


def test_auth_digests_do_not_expose_secrets() -> None:
    settings = Settings(
        postgres_db="database",
        postgres_user="user",
        postgres_password=SecretStr("database-password"),
        auth_otp_pepper=SecretStr("a-test-pepper-that-is-longer-than-32-characters"),
    )

    digest = otp_digest(settings, " Person@Example.com ", "123456")

    assert len(digest) == 64
    assert "123456" not in digest
    assert digest == otp_digest(settings, "person@example.com", "123456")
    assert len(session_token_digest("opaque-session-token")) == 64
    assert normalize_email(" Person@Example.com ") == "person@example.com"
