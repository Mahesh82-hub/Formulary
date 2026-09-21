from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies.auth import get_current_user
from app.models import User
from app.schemas.bioequivalence import (
    BioequivalenceAnalysisRequest,
    BioequivalenceAnalysisResponse,
)
from app.services.bioequivalence import analyze_bioequivalence

router = APIRouter(prefix="/api/v1/bioequivalence", tags=["bioequivalence"])
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("/analyze", response_model=BioequivalenceAnalysisResponse)
async def analyze(
    payload: BioequivalenceAnalysisRequest,
    _: CurrentUser,
) -> BioequivalenceAnalysisResponse:
    return analyze_bioequivalence(payload)
