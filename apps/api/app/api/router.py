from fastapi import APIRouter

from app.api.routes.auth import router as auth_router
from app.api.routes.bioequivalence import router as bioequivalence_router
from app.api.routes.conversations import router as conversations_router
from app.api.routes.health import router as health_router
from app.api.routes.intelligence import router as intelligence_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(conversations_router)
api_router.include_router(bioequivalence_router)
api_router.include_router(intelligence_router)
