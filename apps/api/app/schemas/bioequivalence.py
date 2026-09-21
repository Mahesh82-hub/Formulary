from typing import Annotated, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

BioequivalenceMode = Literal["exploratory", "formal_summary"]
BioequivalenceEndpoint = Literal["cmax", "auc_0_t", "auc_0_inf", "partial_auc"]
EndpointStatus = Literal["within_limits", "outside_limits", "not_assessed"]
OverallAssessmentStatus = Literal[
    "exploratory_only",
    "meets_criteria",
    "does_not_meet_criteria",
    "insufficient_data",
]


def default_required_endpoints() -> list[BioequivalenceEndpoint]:
    return ["cmax", "auc_0_t"]


class StudyContext(BaseModel):
    drug_name: Annotated[str, Field(min_length=1, max_length=300)]
    reference_product: Annotated[str, Field(min_length=1, max_length=300)]
    test_product: Annotated[str, Field(min_length=1, max_length=300)]
    dosage_form: Annotated[str, Field(min_length=1, max_length=200)]
    route: Annotated[str, Field(min_length=1, max_length=200)]
    strength: Annotated[str, Field(min_length=1, max_length=200)]
    study_condition: Literal["fasting", "fed", "other"]
    study_design: Annotated[str | None, Field(max_length=500)] = None


class ConcentrationPoint(BaseModel):
    time: Annotated[float, Field(ge=0)]
    concentration: Annotated[float, Field(ge=0)]


class ConcentrationProfile(BaseModel):
    product: Literal["reference", "test"]
    time_unit: Annotated[str, Field(min_length=1, max_length=30)] = "h"
    concentration_unit: Annotated[str, Field(min_length=1, max_length=50)]
    points: Annotated[list[ConcentrationPoint], Field(min_length=2, max_length=1_000)]

    @model_validator(mode="after")
    def validate_times(self) -> "ConcentrationProfile":
        times = [point.time for point in self.points]
        if any(current <= previous for previous, current in zip(times, times[1:], strict=False)):
            raise ValueError("Concentration profile times must be strictly increasing")
        return self


class EndpointComparisonInput(BaseModel):
    endpoint: BioequivalenceEndpoint
    reference_value: Annotated[float, Field(gt=0)]
    test_value: Annotated[float, Field(gt=0)]
    unit: Annotated[str, Field(min_length=1, max_length=50)]
    ci90_lower_percent: Annotated[float | None, Field(gt=0)] = None
    ci90_upper_percent: Annotated[float | None, Field(gt=0)] = None
    reference_source_url: HttpUrl | None = None
    reference_source_location: Annotated[str | None, Field(max_length=500)] = None
    notes: Annotated[str | None, Field(max_length=1_000)] = None

    @model_validator(mode="after")
    def validate_confidence_interval(self) -> "EndpointComparisonInput":
        supplied = (self.ci90_lower_percent is not None, self.ci90_upper_percent is not None)
        if supplied[0] != supplied[1]:
            raise ValueError("Both 90% confidence interval bounds must be supplied together")
        if (
            self.ci90_lower_percent is not None
            and self.ci90_upper_percent is not None
            and self.ci90_lower_percent > self.ci90_upper_percent
        ):
            raise ValueError("The lower confidence bound cannot exceed the upper bound")
        ratio = self.test_value / self.reference_value * 100
        if (
            self.ci90_lower_percent is not None
            and self.ci90_upper_percent is not None
            and not self.ci90_lower_percent <= ratio <= self.ci90_upper_percent
        ):
            raise ValueError("The test/reference point estimate must lie within its 90% interval")
        return self


class AcceptanceRule(BaseModel):
    lower_percent: Annotated[float, Field(gt=0)] = 80.0
    upper_percent: Annotated[float, Field(gt=0)] = 125.0
    required_endpoints: Annotated[
        list[BioequivalenceEndpoint],
        Field(min_length=1, max_length=4),
    ] = Field(default_factory=default_required_endpoints)
    source_url: HttpUrl | None = None
    description: Annotated[str, Field(min_length=1, max_length=500)] = (
        "Unscaled average bioequivalence acceptance limits"
    )

    @model_validator(mode="after")
    def validate_limits(self) -> "AcceptanceRule":
        if self.lower_percent >= self.upper_percent:
            raise ValueError("The lower acceptance limit must be below the upper limit")
        if len(set(self.required_endpoints)) != len(self.required_endpoints):
            raise ValueError("Required endpoints must be unique")
        return self


class BioequivalenceAnalysisRequest(BaseModel):
    mode: BioequivalenceMode
    context: StudyContext
    acceptance_rule: AcceptanceRule = Field(default_factory=AcceptanceRule)
    endpoints: Annotated[list[EndpointComparisonInput], Field(min_length=1, max_length=12)]
    concentration_profiles: Annotated[
        list[ConcentrationProfile],
        Field(max_length=2),
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_series(self) -> "BioequivalenceAnalysisRequest":
        endpoint_names = [item.endpoint for item in self.endpoints]
        if len(set(endpoint_names)) != len(endpoint_names):
            raise ValueError("Each endpoint can appear only once")
        profile_products = [profile.product for profile in self.concentration_profiles]
        if len(set(profile_products)) != len(profile_products):
            raise ValueError("Only one concentration profile per product can be supplied")
        return self


class EndpointAssessment(BaseModel):
    endpoint: BioequivalenceEndpoint
    reference_value: float
    test_value: float
    unit: str
    geometric_mean_ratio_percent: float
    ci90_lower_percent: float | None
    ci90_upper_percent: float | None
    status: EndpointStatus
    reference_source_url: str | None
    reference_source_location: str | None
    notes: str | None


class ProfileSummary(BaseModel):
    product: Literal["reference", "test"]
    cmax: float
    tmax: float
    auc_0_last: float
    time_unit: str
    concentration_unit: str


class BioequivalenceAnalysisResponse(BaseModel):
    mode: BioequivalenceMode
    context: StudyContext
    acceptance_rule: AcceptanceRule
    endpoints: list[EndpointAssessment]
    profile_summaries: list[ProfileSummary]
    overall_status: OverallAssessmentStatus
    can_export_to_simulator: bool
    caveats: list[str]
