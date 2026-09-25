import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.core.config import get_settings
from app.db.session import engine

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    monitor_task: asyncio.Task[None] | None = None
    if settings.intelligence_monitor_enabled:
        from app.intelligence.monitor import get_regulatory_monitor
        from app.intelligence.service import get_notification_dispatcher, run_forever

        monitor_task = asyncio.create_task(
            run_forever(
                get_regulatory_monitor(),
                get_notification_dispatcher(),
                interval_minutes=settings.intelligence_poll_interval_minutes,
            )
        )
    yield
    if monitor_task is not None:
        monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor_task
    await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    debug=settings.app_debug,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.get("/", tags=["system"])
async def root() -> dict[str, str]:
    return {"name": settings.app_name, "version": __version__}
