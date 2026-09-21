import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import get_current_user
from app.core.config import Settings, get_settings
from app.db.session import get_db_session
from app.models import AuthEvent, OTPChallenge, User, UserSession
from app.schemas.auth import (
    AuthenticationResponse,
    OTPRequest,
    OTPRequestResponse,
    OTPVerifyRequest,
    UserResponse,
)
from app.services.auth import (
    AuthConfigurationError,
    generate_otp,
    generate_session_token,
    normalize_email,
    otp_digest,
    request_value_digest,
    session_token_digest,
)
from app.services.email import EmailDeliveryError, EmailSender, get_email_sender

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])
INVALID_CODE_DETAIL = "The login code is invalid or expired"
DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]
EmailSenderDependency = Annotated[EmailSender, Depends(get_email_sender)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


def user_response(user: User) -> UserResponse:
    return UserResponse(id=user.id, email=user.email, display_name=user.display_name)


@router.post(
    "/otp/request", response_model=OTPRequestResponse, status_code=status.HTTP_202_ACCEPTED
)
async def request_otp(
    payload: OTPRequest,
    request: Request,
    session: DatabaseSession,
    email_sender: EmailSenderDependency,
    settings: SettingsDependency,
) -> OTPRequestResponse:
    email = normalize_email(str(payload.email))
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=15)
    ip_hash: str | None
    try:
        ip_hash = request_value_digest(settings, client_ip(request))
        digest = otp_digest(settings, email, code := generate_otp())
    except AuthConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        ) from error

    recent_email_requests = (
        await session.scalar(
            select(func.count())
            .select_from(OTPChallenge)
            .where(OTPChallenge.email == email, OTPChallenge.created_at >= cutoff)
        )
        or 0
    )
    if recent_email_requests >= settings.auth_otp_requests_per_15_minutes:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Try again later")

    if ip_hash is not None:
        recent_ip_requests = (
            await session.scalar(
                select(func.count())
                .select_from(OTPChallenge)
                .where(
                    OTPChallenge.requested_ip_hash == ip_hash,
                    OTPChallenge.created_at >= cutoff,
                )
            )
            or 0
        )
        if recent_ip_requests >= settings.auth_otp_requests_per_ip_15_minutes:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Try again later"
            )

    await session.execute(
        update(OTPChallenge)
        .where(
            OTPChallenge.email == email,
            OTPChallenge.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    challenge = OTPChallenge(
        email=email,
        code_digest=digest,
        max_attempts=settings.auth_otp_max_attempts,
        expires_at=now + timedelta(seconds=settings.auth_otp_ttl_seconds),
        requested_ip_hash=ip_hash,
        user_agent_hash=request_value_digest(settings, request.headers.get("user-agent")),
    )
    session.add(challenge)
    session.add(
        AuthEvent(
            email=email,
            event_type="otp_requested",
            success=True,
            ip_hash=ip_hash,
            user_agent=request.headers.get("user-agent"),
        )
    )
    await session.commit()
    await session.refresh(challenge)

    try:
        await email_sender.send_login_code(
            email,
            code,
            max(1, settings.auth_otp_ttl_seconds // 60),
        )
    except EmailDeliveryError as error:
        challenge.consumed_at = datetime.now(UTC)
        session.add(
            AuthEvent(
                email=email,
                event_type="otp_delivery_failed",
                success=False,
                ip_hash=ip_hash,
            )
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to send login code",
        ) from error

    return OTPRequestResponse(
        challenge_id=challenge.id,
        message="If the address can receive email, a login code has been sent.",
    )


@router.post("/otp/verify", response_model=AuthenticationResponse)
async def verify_otp(
    payload: OTPVerifyRequest,
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: SettingsDependency,
) -> AuthenticationResponse:
    now = datetime.now(UTC)
    challenge = await session.scalar(
        select(OTPChallenge).where(OTPChallenge.id == payload.challenge_id).with_for_update()
    )
    if (
        challenge is None
        or challenge.consumed_at is not None
        or challenge.expires_at <= now
        or challenge.attempt_count >= challenge.max_attempts
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CODE_DETAIL)

    try:
        expected_digest = otp_digest(settings, challenge.email, payload.code)
        ip_hash = request_value_digest(settings, client_ip(request))
    except AuthConfigurationError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        ) from error

    if not hmac.compare_digest(challenge.code_digest, expected_digest):
        challenge.attempt_count += 1
        if challenge.attempt_count >= challenge.max_attempts:
            challenge.consumed_at = now
        session.add(
            AuthEvent(
                email=challenge.email,
                event_type="otp_verification_failed",
                success=False,
                ip_hash=ip_hash,
                user_agent=request.headers.get("user-agent"),
            )
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CODE_DETAIL)

    challenge.consumed_at = now
    user = await session.scalar(select(User).where(User.email == challenge.email).with_for_update())
    if user is None:
        user = User(email=challenge.email)
        session.add(user)
        await session.flush()
    if user.status != "active" or user.deleted_at is not None:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is unavailable")

    user.last_login_at = now
    raw_session_token = generate_session_token()
    login_session = UserSession(
        user_id=user.id,
        token_digest=session_token_digest(raw_session_token),
        expires_at=now + timedelta(days=settings.auth_session_ttl_days),
        last_seen_at=now,
        ip_hash=ip_hash,
        user_agent=request.headers.get("user-agent"),
    )
    session.add(login_session)
    session.add(
        AuthEvent(
            user_id=user.id,
            email=user.email,
            event_type="login_succeeded",
            success=True,
            ip_hash=ip_hash,
            user_agent=request.headers.get("user-agent"),
        )
    )
    await session.commit()

    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_session_token,
        max_age=settings.auth_session_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.app_env in {"staging", "production"},
        samesite="lax",
        path="/",
    )
    return AuthenticationResponse(user=user_response(user))


@router.get("/me", response_model=UserResponse)
async def me(user: CurrentUser) -> UserResponse:
    return user_response(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    session: DatabaseSession,
    settings: SettingsDependency,
) -> None:
    raw_token = request.cookies.get(settings.session_cookie_name)
    if raw_token is not None:
        digest = session_token_digest(raw_token)
        login_session = await session.scalar(
            select(UserSession).where(UserSession.token_digest == digest).with_for_update()
        )
        if login_session is not None and login_session.revoked_at is None:
            login_session.revoked_at = datetime.now(UTC)
            session.add(
                AuthEvent(
                    user_id=login_session.user_id,
                    event_type="logout",
                    success=True,
                    user_agent=request.headers.get("user-agent"),
                )
            )
            await session.commit()
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        secure=settings.app_env in {"staging", "production"},
        samesite="lax",
        path="/",
    )
