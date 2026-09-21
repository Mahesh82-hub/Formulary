from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.dependencies.auth import get_current_user
from app.main import app
from app.models import User
from app.schemas.bioequivalence import BioequivalenceAnalysisRequest
from app.services.bioequivalence import analyze_bioequivalence


def request_payload(*, mode: str = "formal_summary") -> dict[str, object]:
    return {
        "mode": mode,
        "context": {
            "drug_name": "Example drug",
            "reference_product": "Reference tablet",
            "test_product": "Test tablet",
            "dosage_form": "immediate-release tablet",
            "route": "oral",
            "strength": "500 mg",
            "study_condition": "fasting",
            "study_design": "randomized two-period crossover",
        },
        "endpoints": [
            {
                "endpoint": "cmax",
                "reference_value": 100,
                "test_value": 96,
                "unit": "ng/mL",
                "ci90_lower_percent": 89,
                "ci90_upper_percent": 104,
            },
            {
                "endpoint": "auc_0_t",
                "reference_value": 1_000,
                "test_value": 1_020,
                "unit": "ng*h/mL",
                "ci90_lower_percent": 94,
                "ci90_upper_percent": 109,
            },
        ],
        "concentration_profiles": [
            {
                "product": "reference",
                "concentration_unit": "ng/mL",
                "points": [
                    {"time": 0, "concentration": 0},
                    {"time": 1, "concentration": 100},
                    {"time": 3, "concentration": 50},
                ],
            }
        ],
    }


def test_formal_summary_meets_configured_criteria() -> None:
    request = BioequivalenceAnalysisRequest.model_validate(request_payload())

    result = analyze_bioequivalence(request)

    assert result.overall_status == "meets_criteria"
    assert result.can_export_to_simulator is True
    assert result.endpoints[0].geometric_mean_ratio_percent == 96
    assert result.endpoints[0].status == "within_limits"
    assert result.profile_summaries[0].cmax == 100
    assert result.profile_summaries[0].tmax == 1
    assert result.profile_summaries[0].auc_0_last == 200


def test_formal_summary_fails_when_required_interval_exceeds_limit() -> None:
    payload = request_payload()
    endpoints = payload["endpoints"]
    assert isinstance(endpoints, list)
    cmax = endpoints[0]
    assert isinstance(cmax, dict)
    cmax["ci90_upper_percent"] = 126
    request = BioequivalenceAnalysisRequest.model_validate(payload)

    result = analyze_bioequivalence(request)

    assert result.overall_status == "does_not_meet_criteria"
    assert result.can_export_to_simulator is False
    assert result.endpoints[0].status == "outside_limits"


def test_formal_summary_is_incomplete_without_confidence_intervals() -> None:
    payload = request_payload()
    endpoints = payload["endpoints"]
    assert isinstance(endpoints, list)
    auc = endpoints[1]
    assert isinstance(auc, dict)
    auc.pop("ci90_lower_percent")
    auc.pop("ci90_upper_percent")
    request = BioequivalenceAnalysisRequest.model_validate(payload)

    result = analyze_bioequivalence(request)

    assert result.overall_status == "insufficient_data"
    assert result.can_export_to_simulator is False


def test_exploratory_comparison_never_claims_bioequivalence() -> None:
    request = BioequivalenceAnalysisRequest.model_validate(request_payload(mode="exploratory"))

    result = analyze_bioequivalence(request)

    assert result.overall_status == "exploratory_only"
    assert result.can_export_to_simulator is False
    assert all(endpoint.status == "not_assessed" for endpoint in result.endpoints)
    assert "not a formal bioequivalence determination" in result.caveats[0]


def test_rejects_partial_confidence_interval_and_unsorted_profile() -> None:
    payload = request_payload()
    endpoints = payload["endpoints"]
    assert isinstance(endpoints, list)
    endpoint = endpoints[0]
    assert isinstance(endpoint, dict)
    endpoint.pop("ci90_upper_percent")

    with pytest.raises(ValidationError, match="Both 90% confidence interval bounds"):
        BioequivalenceAnalysisRequest.model_validate(payload)

    payload = request_payload()
    profiles = payload["concentration_profiles"]
    assert isinstance(profiles, list)
    profile = profiles[0]
    assert isinstance(profile, dict)
    profile["points"] = [
        {"time": 1, "concentration": 100},
        {"time": 0, "concentration": 0},
    ]

    with pytest.raises(ValidationError, match="strictly increasing"):
        BioequivalenceAnalysisRequest.model_validate(payload)

    payload = request_payload()
    endpoints = payload["endpoints"]
    assert isinstance(endpoints, list)
    endpoint = endpoints[0]
    assert isinstance(endpoint, dict)
    endpoint["ci90_lower_percent"] = 101

    with pytest.raises(ValidationError, match="point estimate must lie within"):
        BioequivalenceAnalysisRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_authenticated_bioequivalence_analysis_endpoint() -> None:
    user = User(id=uuid4(), email="be-api@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/v1/bioequivalence/analyze",
                json=request_payload(),
            )

        assert response.status_code == 200
        assert response.json()["overall_status"] == "meets_criteria"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
