from typing import Any, Literal

from pydantic import BaseModel, Field


class MCPToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None


class MCPToolResult(BaseModel):
    tool_name: str
    data: Any = None
    structured_content: dict[str, Any] | None = None
    is_error: bool = False


class MassConversion(BaseModel):
    value: float = Field(ge=0)
    unit: str


SimulationEvidenceObjective = Literal["reference_values", "bioequivalence_comparison"]
SimulationParameterCategory = Literal[
    "identity_and_formulation",
    "physicochemical",
    "absorption_and_dissolution",
    "distribution",
    "metabolism",
    "elimination",
    "exposure_metrics",
    "concentration_time_data",
    "safety_constraints",
]


class SimulationEvidenceContext(BaseModel):
    drug_name: str | None = None
    objective: SimulationEvidenceObjective | None = None
    reference_product: str | None = None
    dosage_form: str | None = None
    route: str | None = None
    strength_or_dose: str | None = None
    population: str | None = None
    dosing_context: str | None = None
    test_product: str | None = None
    company_data_available: bool | None = None
    simulator: str | None = None


class SimulationIntakeQuestion(BaseModel):
    question_id: str
    field_ids: list[str]
    prompt: str
    reason: str
    suggestions: list[str] = Field(default_factory=list)
    allow_custom: bool = True


class SimulationEvidenceIntake(BaseModel):
    status: Literal["needs_clarification", "ready"]
    recommended_next_action: Literal["ask_user", "research_evidence"]
    context: SimulationEvidenceContext
    missing_required_fields: list[str]
    questions: list[SimulationIntakeQuestion]
    parameter_categories: list[SimulationParameterCategory]
    assumptions: list[str]


class UserClarificationRequest(BaseModel):
    status: Literal["needs_clarification"] = "needs_clarification"
    recommended_next_action: Literal["ask_user"] = "ask_user"
    task_type: Literal["general_chat"] = "general_chat"
    title: str = "One detail before I continue"
    context: dict[str, Any] = Field(default_factory=dict)
    missing_required_fields: list[str] = Field(default_factory=lambda: ["user_response"])
    questions: list[SimulationIntakeQuestion]
    allow_additional_question: bool = False
    submit_label: str = "Continue"
