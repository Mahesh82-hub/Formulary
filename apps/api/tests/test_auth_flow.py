from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.db.session import async_session_factory
from app.main import app
from app.models import AuthEvent, OTPChallenge, User
from app.services.email import get_email_sender


class RecordingEmailSender:
    def __init__(self) -> None:
        self.recipient: str | None = None
        self.code: str | None = None

    async def send_login_code(self, recipient: str, code: str, expires_in_minutes: int) -> None:
        self.recipient = recipient
        self.code = code
        assert expires_in_minutes > 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_email_otp_login_session_and_logout_flow() -> None:
    email = f"auth-{uuid4()}@example.com"
    email_sender = RecordingEmailSender()
    app.dependency_overrides[get_email_sender] = lambda: email_sender

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            request_response = await client.post(
                "/api/v1/auth/otp/request",
                json={"email": email.upper()},
            )
            assert request_response.status_code == 202
            challenge_id = request_response.json()["challenge_id"]
            assert email_sender.recipient == email
            assert email_sender.code is not None
            delivered_code = email_sender.code
            wrong_code = "000000" if delivered_code != "000000" else "999999"

            wrong_code_response = await client.post(
                "/api/v1/auth/otp/verify",
                json={"challenge_id": challenge_id, "code": wrong_code},
            )
            assert wrong_code_response.status_code == 401

            verify_response = await client.post(
                "/api/v1/auth/otp/verify",
                json={"challenge_id": challenge_id, "code": delivered_code},
            )
            assert verify_response.status_code == 200
            assert verify_response.json()["user"]["email"] == email
            assert "formulary_session" in client.cookies

            me_response = await client.get("/api/v1/auth/me")
            assert me_response.status_code == 200
            assert me_response.json()["email"] == email

            logout_response = await client.post("/api/v1/auth/logout")
            assert logout_response.status_code == 204

            logged_out_response = await client.get("/api/v1/auth/me")
            assert logged_out_response.status_code == 401

        async with async_session_factory() as session:
            challenge = await session.scalar(
                select(OTPChallenge).where(OTPChallenge.id == challenge_id)
            )
            assert challenge is not None
            assert challenge.code_digest != delivered_code
    finally:
        app.dependency_overrides.pop(get_email_sender, None)
        async with async_session_factory() as session:
            await session.execute(delete(AuthEvent).where(AuthEvent.email == email))
            await session.execute(delete(OTPChallenge).where(OTPChallenge.email == email))
            await session.execute(delete(User).where(User.email == email))
            await session.commit()
