from fastapi import APIRouter, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import engine
from app.schemas.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Report whether the API process is alive."""
    return HealthResponse(status="ok")


@router.get("/ready", response_model=ReadinessResponse)
async def readiness() -> ReadinessResponse:
    """Verify PostgreSQL connectivity and the pgvector extension."""
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT
                        current_setting('server_version') AS postgres_version,
                        extversion AS pgvector_version
                    FROM pg_extension
                    WHERE extname = 'vector'
                    """
                )
            )
            row = result.mappings().one_or_none()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable",
        ) from error

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pgvector extension is unavailable",
        )

    return ReadinessResponse(
        status="ready",
        postgres_version=row["postgres_version"],
        pgvector_version=row["pgvector_version"],
    )
