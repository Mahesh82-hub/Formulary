from app.schemas.bioequivalence import (
    BioequivalenceAnalysisRequest,
    BioequivalenceAnalysisResponse,
    ConcentrationProfile,
    EndpointAssessment,
    OverallAssessmentStatus,
    ProfileSummary,
)


def analyze_bioequivalence(
    request: BioequivalenceAnalysisRequest,
) -> BioequivalenceAnalysisResponse:
    rule = request.acceptance_rule
    endpoints = [
        EndpointAssessment(
            endpoint=item.endpoint,
            reference_value=item.reference_value,
            test_value=item.test_value,
            unit=item.unit,
            geometric_mean_ratio_percent=round(
                item.test_value / item.reference_value * 100,
                4,
            ),
            ci90_lower_percent=item.ci90_lower_percent,
            ci90_upper_percent=item.ci90_upper_percent,
            status=(
                "not_assessed"
                if request.mode == "exploratory"
                else (
                    "within_limits"
                    if item.ci90_lower_percent is not None
                    and item.ci90_upper_percent is not None
                    and item.ci90_lower_percent >= rule.lower_percent
                    and item.ci90_upper_percent <= rule.upper_percent
                    else (
                        "outside_limits"
                        if item.ci90_lower_percent is not None
                        and item.ci90_upper_percent is not None
                        else "not_assessed"
                    )
                )
            ),
            reference_source_url=(
                str(item.reference_source_url) if item.reference_source_url is not None else None
            ),
            reference_source_location=item.reference_source_location,
            notes=item.notes,
        )
        for item in request.endpoints
    ]
    overall_status = _overall_status(request, endpoints)
    caveats = [
        (
            "FDA-extracted reference values and independently supplied test values provide an "
            "exploratory comparison, not a formal bioequivalence determination."
            if request.mode == "exploratory"
            else "This assessment applies the supplied confidence intervals to the configured "
            "acceptance rule; it does not validate study design, source data, or regulatory "
            "acceptability."
        ),
        "Product-specific FDA guidance can require different endpoints or statistical methods.",
        "Simulator export is a user-controlled research handoff, not a safety or approval claim.",
    ]
    return BioequivalenceAnalysisResponse(
        mode=request.mode,
        context=request.context,
        acceptance_rule=rule,
        endpoints=endpoints,
        profile_summaries=[_profile_summary(profile) for profile in request.concentration_profiles],
        overall_status=overall_status,
        can_export_to_simulator=overall_status == "meets_criteria",
        caveats=caveats,
    )


def _overall_status(
    request: BioequivalenceAnalysisRequest,
    endpoints: list[EndpointAssessment],
) -> OverallAssessmentStatus:
    if request.mode == "exploratory":
        return "exploratory_only"
    by_name = {endpoint.endpoint: endpoint for endpoint in endpoints}
    required = [by_name.get(name) for name in request.acceptance_rule.required_endpoints]
    if any(endpoint is None or endpoint.status == "not_assessed" for endpoint in required):
        return "insufficient_data"
    if any(endpoint is not None and endpoint.status == "outside_limits" for endpoint in required):
        return "does_not_meet_criteria"
    return "meets_criteria"


def _profile_summary(profile: ConcentrationProfile) -> ProfileSummary:
    peak = max(profile.points, key=lambda point: point.concentration)
    auc = sum(
        (right.time - left.time) * (left.concentration + right.concentration) / 2
        for left, right in zip(profile.points, profile.points[1:], strict=False)
    )
    return ProfileSummary(
        product=profile.product,
        cmax=peak.concentration,
        tmax=peak.time,
        auc_0_last=round(auc, 6),
        time_unit=profile.time_unit,
        concentration_unit=profile.concentration_unit,
    )
