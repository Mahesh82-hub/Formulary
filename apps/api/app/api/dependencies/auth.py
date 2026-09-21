from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db_session
from app.models import User, UserSession
from app.services.auth import session_token_digest

DatabaseSession = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_user(
    request: Request,
    session: DatabaseSession,
) -> User:
    settings = get_settings()
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    digest = session_token_digest(session_token)
    user = await session.scalar(
        select(User)
        .join(UserSession, UserSession.user_id == User.id)
        .where(
            UserSession.token_digest == digest,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > datetime.now(UTC),
            User.status == "active",
            User.deleted_at.is_(None),
        )
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    return user
