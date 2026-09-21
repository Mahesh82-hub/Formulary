import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Any, Literal, get_args
from urllib.parse import quote
from uuid import UUID

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from app.core.config import Settings, get_settings
from app.fda.client import OpenFDAClient, get_openfda_client
from app.fda.models import OPENFDA_MAX_SKIP, OpenFDADataset, OpenFDAResult
from app.ingestion.models import (
    DocumentChunkPage,
    EvidenceSearchResult,
    IngestionReceipt,
    PDFIngestionReceipt,
)
from app.ingestion.pdf import OCRMode
from app.ingestion.service import FDAIngestionCoordinator, get_fda_ingestion_coordinator
from app.mcp_gateway.models import (
    MassConversion,
    SimulationEvidenceContext,
    SimulationEvidenceIntake,
    SimulationEvidenceObjective,
    SimulationIntakeQuestion,
    SimulationParameterCategory,
    UserClarificationRequest,
)
from app.schemas.bioequivalence import (
    BioequivalenceAnalysisRequest,
    BioequivalenceAnalysisResponse,
)
from app.services.bioequivalence import analyze_bioequivalence
from app.sources.federation import FederatedSearchCoordinator, FederatedSearchResult
from app.sources.registry import build_federated_coordinator

MassUnit = Literal["mcg", "mg", "g"]
MASS_IN_GRAMS: dict[MassUnit, Decimal] = {
    "mcg": Decimal("0.000001"),
    "mg": Decimal("0.001"),
    "g": Decimal("1"),
}

LabelSection = Literal[
    "boxed_warning",
    "recent_major_changes",
    "indications_and_usage",
    "dosage_and_administration",
    "dosage_forms_and_strengths",
    "contraindications",
    "warnings_and_precautions",
    "adverse_reactions",
    "drug_interactions",
    "use_in_specific_populations",
    "pregnancy",
    "nursing_mothers",
    "pediatric_use",
    "geriatric_use",
    "drug_abuse_and_dependence",
    "overdosage",
    "description",
    "clinical_pharmacology",
    "mechanism_of_action",
    "pharmacodynamics",
    "pharmacokinetics",
    "clinical_studies",
    "microbiology",
    "nonclinical_toxicology",
    "carcinogenesis_and_mutagenesis_and_impairment_of_fertility",
    "references",
    "how_supplied",
    "storage_and_handling",
    "patient_counseling_information",
    "information_for_patients",
    "instructions_for_use",
    "spl_medguide",
    "package_label_principal_display_panel",
]
SUPPORTED_LABEL_SECTIONS: frozenset[str] = frozenset(get_args(LabelSection))
LABEL_SECTION_ALIASES: dict[str, str] = {
    "dosage_forms": "dosage_forms_and_strengths",
    "strengths": "dosage_forms_and_strengths",
    "how_supplied_and_storage_and_handling": "how_supplied",
    "how_supplied_storage_and_handling": "how_supplied",
    "cmax": "pharmacokinetics",
    "tmax": "pharmacokinetics",
    "auc": "pharmacokinetics",
}
# Evidence lives in one corpus regardless of which source produced it, so the filter is a
# plain source-dataset slug rather than an openFDA-only literal. Unknown slugs are
# reported as a caveat by the coordinator instead of failing the assistant run.
EvidenceDataset = str
DEFAULT_LABEL_SECTIONS: list[LabelSection] = [
    "boxed_warning",
    "indications_and_usage",
    "dosage_and_administration",
    "contraindications",
    "warnings_and_precautions",
    "adverse_reactions",
    "clinical_pharmacology",
    "clinical_studies",
]

OPENFDA_GENERAL_CAVEAT = (
    "openFDA is a public research source. Its records can be incomplete, delayed, duplicated, "
    "reformatted, or unvalidated and must not be treated as medical advice."
)


@dataclass(frozen=True)
class OpenFDAToolLimits:
    """Per-tool payload caps.

    These bound how much upstream data can reach the model in a single tool call. They are
    settings-backed rather than hardcoded because the right values depend on the context window
    of the selected provider and model.
    """

    query_max_records: int = 10
    label_max_records: int = 5
    search_max_records: int = 10
    crl_max_records: int = 5
    ingest_max_records: int = 25
    result_max_characters: int = 48_000
    faers_max_reactions: int = 25
    evidence_max_chunks: int = 8
    federated_max_records: int = 12

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenFDAToolLimits":
        return cls(
            query_max_records=settings.openfda_query_max_records,
            label_max_records=settings.openfda_label_max_records,
            search_max_records=settings.openfda_search_max_records,
            crl_max_records=settings.openfda_crl_max_records,
            ingest_max_records=settings.openfda_ingest_max_records,
            result_max_characters=settings.openfda_result_max_characters,
            faers_max_reactions=settings.faers_max_reactions,
            evidence_max_chunks=settings.ingestion_search_max_chunks,
            federated_max_records=settings.federated_max_records,
        )


OPENFDA_COMPACTED_CAVEAT = (
    "The complete records exceeded the LLM payload boundary and were replaced with compact "
    "previews. Use ingest_openfda_query followed by search_ingested_evidence or "
    "read_ingested_document_chunks for complete evidence."
)
FAERS_CAVEATS = [
    OPENFDA_GENERAL_CAVEAT,
    (
        "FAERS is a spontaneous reporting system. A report does not prove the drug caused the "
        "event, and report counts cannot be used to calculate incidence or compare drug safety "
        "without an appropriate denominator and study design."
    ),
]
DEFAULT_SIMULATION_PARAMETER_CATEGORIES: list[SimulationParameterCategory] = [
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


def create_internal_mcp_server(
    fda: OpenFDAClient,
    ingestion: FDAIngestionCoordinator | None = None,
    limits: OpenFDAToolLimits | None = None,
    federation: FederatedSearchCoordinator | None = None,
) -> FastMCP[None]:
    ingestion_coordinator = ingestion or get_fda_ingestion_coordinator()
    tool_limits = limits or OpenFDAToolLimits()
    server = FastMCP[None](
        name="Dr. Insilico Internal Tools",
        instructions=(
            "Read-only pharmaceutical research tools maintained by Dr. Insilico. "
            "FDA results include provenance and limitations; cite them and do not present them "
            "as medical advice or proof of causality. Use the bioequivalence evidence intake "
            "tool before researching reference values or test/reference comparisons. Cache "
            "large openFDA record sets, then search or page through their bounded chunks instead "
            "of returning full records to the model. For FDA PDFs, preserve page/table provenance "
            "and treat OCR or possible graph values as requiring review."
        ),
        strict_input_validation=True,
        mask_error_details=True,
    )

    @server.tool(
        name="convert_mass",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    def convert_mass(value: float, from_unit: MassUnit, to_unit: MassUnit) -> MassConversion:
        """Convert a non-negative mass between micrograms, milligrams, and grams."""
        if value < 0:
            raise ValueError("value must be non-negative")
        converted = Decimal(str(value)) * MASS_IN_GRAMS[from_unit] / MASS_IN_GRAMS[to_unit]
        return MassConversion(value=float(converted), unit=to_unit)

    @server.tool(
        name="analyze_bioequivalence_summary",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    def analyze_bioequivalence_summary(
        request: BioequivalenceAnalysisRequest,
    ) -> BioequivalenceAnalysisResponse:
        """Deterministically assess user-supplied test/reference PK summary statistics.

        Never invent endpoint values or confidence intervals. Exploratory mode compares ratios
        without a formal conclusion. Formal-summary mode evaluates supplied 90% confidence
        intervals against the configured required endpoints and acceptance limits.
        """
        return analyze_bioequivalence(request)

    @server.tool(
        name="request_user_clarification",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def request_user_clarification(
        question: str,
        suggestions: list[str] | None = None,
        reason: str | None = None,
        title: str | None = None,
    ) -> UserClarificationRequest:
        """Pause an ordinary chat task for one material user decision or missing detail.

        Use this only when the request cannot be answered responsibly without the user's choice.
        Suggestions must be concrete answers that can be selected verbatim, never instructions,
        placeholders, examples, or requests to provide a value. For open-ended answers such as a
        drug name, product, date, identifier, or study detail, pass no suggestions and let the user
        type the answer. The next message resumes the same task.
        """
        normalized_question = " ".join(question.split())[:1_000]
        if not normalized_question:
            normalized_question = "What detail should I use before I continue?"
        normalized_suggestions: list[str] = []
        for suggestion in suggestions or []:
            normalized = " ".join(suggestion.split())[:300]
            lowered = normalized.lower()
            looks_instructional = lowered.startswith(
                (
                    "provide ",
                    "specify ",
                    "enter ",
                    "type ",
                    "name ",
                    "choose ",
                    "select ",
                    "tell me ",
                    "state ",
                )
            ) or any(marker in lowered for marker in ("e.g.", "for example", "such as"))
            if looks_instructional:
                continue
            if normalized and normalized not in normalized_suggestions:
                normalized_suggestions.append(normalized)
            if len(normalized_suggestions) >= 6:
                break
        if len(normalized_suggestions) < 2:
            normalized_suggestions = []
        normalized_reason = " ".join((reason or "").split())[:500]
        normalized_title = " ".join((title or "").split())[:120]
        return UserClarificationRequest(
            title=normalized_title or "One detail before I continue",
            questions=[
                SimulationIntakeQuestion(
                    question_id="general_clarification",
                    field_ids=["user_response"],
                    prompt=normalized_question,
                    reason=(
                        normalized_reason
                        or "This choice materially changes which evidence or answer is appropriate."
                    ),
                    suggestions=normalized_suggestions,
                )
            ],
        )

    @server.tool(
        name="prepare_bioequivalence_evidence_request",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    def prepare_bioequivalence_evidence_request(
        drug_name: Annotated[str | None, Field(max_length=300)] = None,
        objective: SimulationEvidenceObjective | None = None,
        reference_product: Annotated[str | None, Field(max_length=300)] = None,
        dosage_form: Annotated[str | None, Field(max_length=200)] = None,
        route: Annotated[str | None, Field(max_length=200)] = None,
        strength_or_dose: Annotated[str | None, Field(max_length=200)] = None,
        population: Annotated[str | None, Field(max_length=500)] = None,
        dosing_context: Annotated[str | None, Field(max_length=500)] = None,
        test_product: Annotated[str | None, Field(max_length=300)] = None,
        company_data_available: bool | None = None,
        simulator: Annotated[str | None, Field(max_length=200)] = None,
        parameter_categories: list[SimulationParameterCategory] | None = None,
    ) -> SimulationEvidenceIntake:
        """Assess an FDA reference-value or bioequivalence evidence request before research.

        Call this when a user asks for reference values, concentration curves, or a comparison
        between a reference and test product. Infer and pass `reference_values` for an explicit
        extraction request and `bioequivalence_comparison` for an explicit comparison; do not ask
        the user to choose when their wording is already clear. Never invent test or candidate
        values. Pass only context explicitly supplied by the user or established earlier in the
        conversation. If the result needs clarification, ask its questions before calling source-
        retrieval tools. A simulator name is optional because export remains simulator-neutral.
        """
        context = SimulationEvidenceContext(
            drug_name=_normalized_optional(drug_name),
            objective=objective,
            reference_product=_normalized_optional(reference_product),
            dosage_form=_normalized_optional(dosage_form),
            route=_normalized_optional(route),
            strength_or_dose=_normalized_optional(strength_or_dose),
            population=_normalized_optional(population),
            dosing_context=_normalized_optional(dosing_context),
            test_product=_normalized_optional(test_product),
            company_data_available=company_data_available,
            simulator=_normalized_optional(simulator),
        )
        missing: list[str] = []
        questions: list[SimulationIntakeQuestion] = []

        if context.drug_name is None:
            missing.append("drug_name")
            questions.append(
                SimulationIntakeQuestion(
                    question_id="drug_identity",
                    field_ids=["drug_name"],
                    prompt=(
                        "Which drug or active ingredient should I research? Include a brand name "
                        "too if a specific marketed reference product matters."
                    ),
                    reason="Drug identity is required to resolve and retrieve relevant evidence.",
                )
            )
        if context.objective is None:
            missing.append("objective")
            questions.append(
                SimulationIntakeQuestion(
                    question_id="research_objective",
                    field_ids=["objective"],
                    prompt=(
                        "Do you want source-reported FDA reference values, or do you want to "
                        "compare a company's test product with a reference product?"
                    ),
                    reason=(
                        "Reference extraction and bioequivalence comparison require different "
                        "inputs and conclusions."
                    ),
                    suggestions=[
                        "Use source-reported FDA reference values",
                        "Compare a company test product with a reference product",
                    ],
                )
            )

        formulation_fields = {
            "dosage_form": context.dosage_form,
            "route": context.route,
            "strength_or_dose": context.strength_or_dose,
        }
        missing_formulation = [
            field_name for field_name, value in formulation_fields.items() if value is None
        ]
        if missing_formulation:
            missing.extend(missing_formulation)
            questions.append(
                SimulationIntakeQuestion(
                    question_id="formulation",
                    field_ids=missing_formulation,
                    prompt=(
                        "What dosage form, route of administration, and strength or dose should I "
                        "target?"
                    ),
                    reason=(
                        "These details materially change dissolution, exposure, and which source "
                        "values are comparable."
                    ),
                    suggestions=[
                        "Oral immediate-release tablet",
                        "Oral extended-release tablet",
                        "Oral solution",
                        "Intravenous injection",
                    ],
                )
            )

        comparison_fields = {
            "reference_product": context.reference_product,
            "test_product": context.test_product,
            "company_data_available": context.company_data_available,
        }
        missing_comparison = [
            field_name for field_name, value in comparison_fields.items() if value is None
        ]
        if context.objective == "bioequivalence_comparison" and missing_comparison:
            missing.extend(missing_comparison)
            questions.append(
                SimulationIntakeQuestion(
                    question_id="comparison_products",
                    field_ids=missing_comparison,
                    prompt=(
                        "Which reference and test products are being compared, and can you provide "
                        "the company's test/reference PK summary or subject-level study data?"
                    ),
                    reason=(
                        "A formal confidence-interval assessment needs company study data; FDA "
                        "reference values alone support only an exploratory comparison."
                    ),
                    suggestions=[
                        "I have test/reference PK summary statistics",
                        "I have subject-level concentration-time data",
                        "I do not have company PK data yet",
                    ],
                )
            )

        selected_categories = parameter_categories or list(DEFAULT_SIMULATION_PARAMETER_CATEGORIES)
        assumptions: list[str] = []
        if parameter_categories is None:
            assumptions.append(
                "Use the broad simulator-neutral parameter scope until the user or simulator "
                "narrows it."
            )
        if context.simulator is None:
            assumptions.append(
                "Prepare canonical JSON/CSV concepts; defer simulator-specific field mapping."
            )
        if context.population is None:
            assumptions.append(
                "Collect reported human populations separately and preserve their study context."
            )
        if context.dosing_context is None:
            assumptions.append(
                "Keep single/multiple-dose and fed/fasted evidence separate when reported."
            )

        status: Literal["needs_clarification", "ready"] = (
            "needs_clarification" if missing else "ready"
        )
        return SimulationEvidenceIntake(
            status=status,
            recommended_next_action=(
                "ask_user" if status == "needs_clarification" else "research_evidence"
            ),
            context=context,
            missing_required_fields=missing,
            questions=questions[:3],
            parameter_categories=selected_categories,
            assumptions=assumptions,
        )

    @server.tool(
        name="ingest_openfda_query",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def ingest_openfda_query(
        dataset: OpenFDADataset,
        search: str | None = None,
        sort: str | None = None,
        limit: int = 5,
    ) -> IngestionReceipt:
        """Fetch and durably ingest complete openFDA records without returning them to the LLM.

        The raw response and records are compressed in content-addressed local storage. Stable
        records are deduplicated, section-aware chunks are stored in PostgreSQL, and only a
        compact ingestion receipt is returned. Requests are capped at 25 records. Follow with
        search_ingested_evidence or read_ingested_document_chunks.
        """
        return await ingestion_coordinator.ingest_query(
            fda,
            dataset,
            search=search,
            sort=sort,
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.ingest_max_records),
        )

    @server.tool(
        name="ingest_fda_pdf_document",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
        },
    )
    async def ingest_fda_pdf_document(
        url: Annotated[str, Field(min_length=1, max_length=2_000)],
        title: Annotated[str | None, Field(max_length=500)] = None,
        ocr_mode: OCRMode = "auto",
    ) -> PDFIngestionReceipt:
        """Fetch, extract, and durably ingest an official FDA-hosted PDF.

        Use only after local evidence and normal structured FDA API tools cannot supply the
        specifically requested information or quantitative values. Do not use for an ordinary
        drug question when API evidence is sufficient.

        Native text and tables are extracted first. In auto mode, pages with little native text
        use OCR when Tesseract is installed. OCR output requires review. Possible graph pages are
        flagged but curve coordinates are not digitized or treated as numerical evidence. Follow
        with search_ingested_evidence using dataset `fda-documents`.
        """
        return await ingestion_coordinator.ingest_fda_pdf(
            url,
            title=title,
            ocr_mode=ocr_mode,
        )

    @server.tool(
        name="search_all_sources",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def search_all_sources(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        limit: int = 10,
    ) -> FederatedSearchResult:
        """Search every available source at once and return merged, attributed evidence.

        Prefer this for any question that is not already narrowed to one dataset: it queries
        all sources concurrently, so it costs about as much time as querying one of them.

        Every record names the source it came from; cite them. The `outcomes` list reports what
        happened to each source that was asked, including any that timed out or failed - when
        `caveats` is non-empty the answer is incomplete and you must say which sources were
        unavailable. A source reporting `empty` simply had no match and is not a failure.
        """
        if federation is None:
            raise ToolError("Federated search is not configured on this deployment.")
        return await federation.search(
            query,
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.federated_max_records),
        )

    @server.tool(
        name="search_ingested_evidence",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def search_ingested_evidence(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        dataset: EvidenceDataset | None = None,
        external_key: Annotated[str | None, Field(max_length=512)] = None,
        limit: int = 5,
    ) -> EvidenceSearchResult:
        """Search the latest versions of ingested evidence using PostgreSQL full-text ranking.

        Results contain bounded source chunks, stable document and chunk identifiers, source
        URLs, and dataset update metadata. Requests are capped at 8 chunks. Cite only evidence
        returned by this or another source tool.
        """
        return await ingestion_coordinator.search_chunks(
            query,
            dataset=dataset,
            external_key=_normalized_optional(external_key),
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.evidence_max_chunks),
        )

    @server.tool(
        name="read_ingested_document_chunks",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def read_ingested_document_chunks(
        document_id: UUID,
        cursor: int = 0,
        limit: int = 5,
    ) -> DocumentChunkPage:
        """Read a bounded page from the latest version of an ingested document.

        Follow next_cursor only when the question requires exhaustive document coverage. Requests
        are capped at 8 chunks and negative cursors become zero. Prefer search_ingested_evidence
        for targeted questions.
        """
        return await ingestion_coordinator.read_document_chunks(
            document_id,
            cursor=max(0, cursor),
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.evidence_max_chunks),
        )

    @server.tool(
        name="openfda_query",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def openfda_query(
        dataset: OpenFDADataset,
        search: str | None = None,
        count: str | None = None,
        sort: str | None = None,
        limit: int = 5,
        skip: int = 0,
    ) -> OpenFDAResult:
        """Query an allowlisted openFDA dataset with native search, count, sort, and paging.

        Use openFDA field syntax such as `field:"phrase"`, `field:[start TO end]`, `.exact`,
        `AND`, and `*`. Prefer the focused tools for labels, FAERS summaries, and complete
        response letters because they constrain very large records. Requests are capped at 10
        records and skip is clamped to the supported 0-to-25000 range.
        """
        result = await fda.query(
            dataset,
            search=search,
            count=count,
            sort=sort,
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.query_max_records),
            skip=_bounded_int(skip, minimum=0, maximum=OPENFDA_MAX_SKIP),
        )
        return _cap_openfda_result(
            _with_caveats(result, [OPENFDA_GENERAL_CAVEAT]),
            tool_limits.result_max_characters,
        )

    @server.tool(
        name="get_fda_drug_labels",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def get_fda_drug_labels(
        drug_name: str,
        sections: list[str] | None = None,
        limit: int = 3,
    ) -> OpenFDAResult:
        """Retrieve selected sections and table text from up to 5 current FDA SPL drug labels.

        Common sections include dosage_forms_and_strengths, clinical_pharmacology,
        pharmacokinetics, clinical_studies, description, how_supplied, and storage_and_handling.
        Unknown names are reported in the result instead of failing the whole assistant run.
        """
        selected_sections, ignored_sections = _normalize_label_sections(sections)
        result = await fda.query(
            "drug/label",
            search=_or_exact(
                (
                    "openfda.generic_name",
                    "openfda.brand_name",
                    "openfda.substance_name",
                ),
                drug_name,
            ),
            sort="effective_time:desc",
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.label_max_records),
        )
        projected = [_project_label(record, selected_sections) for record in result.results]
        caveats = [OPENFDA_GENERAL_CAVEAT]
        if ignored_sections:
            caveats.append(
                "Unsupported requested label sections were ignored: "
                f"{', '.join(ignored_sections)}."
            )
        return _cap_openfda_result(
            _replace_results(result, projected, caveats),
            tool_limits.result_max_characters,
        )

    @server.tool(
        name="analyze_fda_adverse_event_reactions",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def analyze_fda_adverse_event_reactions(
        drug_name: str,
        top_reactions: int = 10,
        additional_search: str | None = None,
    ) -> OpenFDAResult:
        """Count the most frequently reported FAERS reactions for a named drug.

        `additional_search` can add an openFDA constraint such as a received-date range or
        seriousness field. Requests are capped at 25 reactions. Counts are reporting frequencies,
        not incidence or causality.
        """
        search = _or_exact(
            (
                "patient.drug.openfda.generic_name",
                "patient.drug.openfda.brand_name",
                "patient.drug.openfda.substance_name",
                "patient.drug.medicinalproduct",
            ),
            drug_name,
        )
        if additional_search:
            search = f"({search}) AND ({additional_search})"
        result = await fda.query(
            "drug/event",
            search=search,
            count="patient.reaction.reactionmeddrapt.exact",
            limit=_bounded_int(top_reactions, minimum=1, maximum=tool_limits.faers_max_reactions),
        )
        return _with_caveats(result, FAERS_CAVEATS)

    @server.tool(
        name="search_fda_drug_approvals",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def search_fda_drug_approvals(
        drug_name: str | None = None,
        application_number: str | None = None,
        sponsor: str | None = None,
        limit: int = 5,
    ) -> OpenFDAResult:
        """Search up to 10 Drugs@FDA applications, products, submissions, and documents."""
        clauses: list[str] = []
        if drug_name:
            clauses.append(
                _or_phrase(
                    (
                        "products.brand_name",
                        "products.active_ingredients.name",
                        "openfda.generic_name",
                        "openfda.brand_name",
                    ),
                    drug_name,
                )
            )
        if application_number:
            clauses.append(_phrase("application_number", application_number.upper()))
        if sponsor:
            clauses.append(_phrase("sponsor_name", sponsor))
        result = await fda.query(
            "drug/drugsfda",
            search=_required_clauses(clauses),
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.search_max_records),
        )
        records = [_project_approval(record) for record in result.results]
        return _replace_results(result, records, [OPENFDA_GENERAL_CAVEAT])

    @server.tool(
        name="search_fda_drug_shortages",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def search_fda_drug_shortages(
        drug_name: str,
        status: str | None = None,
        limit: int = 5,
    ) -> OpenFDAResult:
        """Search up to 10 FDA drug shortages with status, availability, and update dates."""
        clauses = [
            _or_phrase(
                ("generic_name", "proprietary_name", "openfda.brand_name"),
                drug_name,
            )
        ]
        if status:
            clauses.append(_phrase("status", status))
        result = await fda.query(
            "drug/shortages",
            search=" AND ".join(f"({clause})" for clause in clauses),
            sort="update_date:desc",
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.search_max_records),
        )
        return _with_caveats(result, [OPENFDA_GENERAL_CAVEAT])

    @server.tool(
        name="search_fda_drug_recalls",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def search_fda_drug_recalls(
        drug_name: str,
        status: str | None = None,
        limit: int = 5,
    ) -> OpenFDAResult:
        """Search up to 10 FDA drug recall enforcement reports and classifications."""
        clauses = [
            _or_phrase(
                (
                    "openfda.generic_name",
                    "openfda.brand_name",
                    "openfda.substance_name",
                    "product_description",
                ),
                drug_name,
            )
        ]
        if status:
            clauses.append(_phrase("status", status))
        result = await fda.query(
            "drug/enforcement",
            search=" AND ".join(f"({clause})" for clause in clauses),
            sort="report_date:desc",
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.search_max_records),
        )
        records = [_project_recall(record) for record in result.results]
        return _replace_results(result, records, [OPENFDA_GENERAL_CAVEAT])

    @server.tool(
        name="search_fda_complete_response_letters",
        annotations={"readOnlyHint": True, "destructiveHint": False},
    )
    async def search_fda_complete_response_letters(
        query: str | None = None,
        application_number: str | None = None,
        company_name: str | None = None,
        limit: int = 3,
    ) -> OpenFDAResult:
        """Search up to 5 FDA Complete Response Letters and return excerpts with PDFs."""
        clauses: list[str] = []
        if query:
            clauses.append(_or_phrase(("text", "company_name", "application_number"), query))
        if application_number:
            clauses.append(_phrase("application_number", application_number.upper()))
        if company_name:
            clauses.append(_phrase("company_name", company_name))
        result = await fda.query(
            "transparency/crl",
            search=_required_clauses(clauses),
            sort="letter_date:desc",
            limit=_bounded_int(limit, minimum=1, maximum=tool_limits.crl_max_records),
        )
        records = [_project_crl(record) for record in result.results]
        return _replace_results(result, records, [OPENFDA_GENERAL_CAVEAT])

    return server


@lru_cache
def get_internal_mcp_server() -> FastMCP[None]:
    settings = get_settings()
    fda = get_openfda_client()
    ingestion = get_fda_ingestion_coordinator()
    return create_internal_mcp_server(
        fda,
        ingestion=ingestion,
        limits=OpenFDAToolLimits.from_settings(settings),
        federation=build_federated_coordinator(fda, ingestion, settings),
    )


def _escaped(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 300:
        raise ValueError("FDA search term must contain between 1 and 300 characters")
    return normalized.replace("\\", "\\\\").replace('"', '\\"')


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _bounded_int(value: int, *, minimum: int, maximum: int) -> int:
    """Clamp model-selected paging values without exposing provider-rejected schema maxima."""
    return min(max(value, minimum), maximum)


def _exact(field: str, value: str) -> str:
    return f'{field}.exact:"{_escaped(value)}"'


def _phrase(field: str, value: str) -> str:
    return f'{field}:"{_escaped(value)}"'


def _or_exact(fields: tuple[str, ...], value: str) -> str:
    return " ".join(_exact(field, value) for field in fields)


def _or_phrase(fields: tuple[str, ...], value: str) -> str:
    return " ".join(_phrase(field, value) for field in fields)


def _required_clauses(clauses: list[str]) -> str:
    if not clauses:
        raise ValueError("At least one FDA search criterion is required")
    return " AND ".join(f"({clause})" for clause in clauses)


def _with_caveats(result: OpenFDAResult, caveats: list[str]) -> OpenFDAResult:
    return result.model_copy(update={"caveats": caveats})


def _replace_results(
    result: OpenFDAResult,
    records: list[dict[str, Any]],
    caveats: list[str],
) -> OpenFDAResult:
    return result.model_copy(
        update={"results": records, "returned": len(records), "caveats": caveats}
    )


def _cap_openfda_result(result: OpenFDAResult, max_characters: int) -> OpenFDAResult:
    serialized = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )
    if len(serialized) <= max_characters:
        return result
    previews = [_compact_record_preview(record) for record in result.results]
    return result.model_copy(
        update={
            "results": previews,
            "caveats": [*result.caveats, OPENFDA_COMPACTED_CAVEAT],
        }
    )


def _compact_record_preview(record: dict[str, Any]) -> dict[str, Any]:
    identifiers = (
        "id",
        "set_id",
        "version",
        "effective_time",
        "application_number",
        "product_ndc",
        "safetyreportid",
        "recall_number",
        "event_id",
        "status",
    )
    preview = {key: record[key] for key in identifiers if key in record}
    openfda = record.get("openfda")
    if isinstance(openfda, dict):
        preview["openfda"] = {
            key: value[:10] if isinstance(value, list) else value
            for key, value in openfda.items()
            if key
            in {
                "brand_name",
                "generic_name",
                "substance_name",
                "manufacturer_name",
                "product_type",
                "route",
                "rxcui",
                "unii",
            }
        }
    sections = record.get("sections")
    if isinstance(sections, dict):
        preview["available_sections"] = sorted(sections)
        preview["section_previews"] = {
            key: _text_preview(value, 600) for key, value in list(sections.items())[:12]
        }
    preview["available_fields"] = sorted(record)
    return preview


def _normalize_label_sections(sections: list[str] | None) -> tuple[list[str], list[str]]:
    if not sections:
        return list(DEFAULT_LABEL_SECTIONS), []

    selected: list[str] = []
    ignored: list[str] = []
    for raw_section in sections[:20]:
        normalized = "_".join(raw_section.strip().lower().replace("-", " ").split())
        normalized = LABEL_SECTION_ALIASES.get(normalized, normalized)
        if normalized in SUPPORTED_LABEL_SECTIONS:
            if normalized not in selected:
                selected.append(normalized)
        else:
            rendered = raw_section.strip()[:80] or "<empty>"
            if rendered not in ignored:
                ignored.append(rendered)

    if len(sections) > 20:
        ignored.append(f"{len(sections) - 20} additional section name(s)")
    if not selected:
        selected = list(DEFAULT_LABEL_SECTIONS)
    return selected, ignored


def _text_preview(value: Any, max_characters: int) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        text = "\n".join(value)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= max_characters else f"{text[:max_characters].rstrip()}…"


def _project_label(
    record: dict[str, Any],
    sections: list[str],
) -> dict[str, Any]:
    projected_sections: dict[str, Any] = {}
    truncated_sections: list[str] = []
    for section in sections:
        if section in record:
            projected_sections[section], truncated = _bounded_text(record[section])
            if truncated:
                truncated_sections.append(section)
        table_field = f"{section}_table"
        if table_field in record:
            projected_sections[table_field], truncated = _bounded_text(record[table_field])
            if truncated:
                truncated_sections.append(table_field)
    return {
        "id": record.get("id"),
        "set_id": record.get("set_id"),
        "version": record.get("version"),
        "effective_time": record.get("effective_time"),
        "openfda": record.get("openfda", {}),
        "requested_sections": sections,
        "sections_not_present": [
            section
            for section in sections
            if section not in record and f"{section}_table" not in record
        ],
        "sections": projected_sections,
        "truncated_sections": truncated_sections,
    }


def _project_approval(record: dict[str, Any]) -> dict[str, Any]:
    products = record.get("products")
    submissions = record.get("submissions")
    product_list = products if isinstance(products, list) else []
    submission_list = submissions if isinstance(submissions, list) else []
    return {
        "application_number": record.get("application_number"),
        "sponsor_name": record.get("sponsor_name"),
        "product_count": len(product_list),
        "products": product_list[:20],
        "submission_count": len(submission_list),
        "recent_submissions": submission_list[-10:],
        "openfda": record.get("openfda", {}),
    }


def _bounded_text(value: Any, max_characters: int = 12_000) -> tuple[Any, bool]:
    if isinstance(value, str):
        if len(value) <= max_characters:
            return value, False
        return f"{value[:max_characters].rstrip()}…", True
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return value, False

    remaining = max_characters
    output: list[str] = []
    for item in value:
        if len(item) <= remaining:
            output.append(item)
            remaining -= len(item)
            continue
        output.append(f"{item[:remaining].rstrip()}…")
        return output, True
    return output, False


def _project_recall(record: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "recall_number",
        "event_id",
        "status",
        "classification",
        "recalling_firm",
        "product_description",
        "reason_for_recall",
        "distribution_pattern",
        "recall_initiation_date",
        "report_date",
        "termination_date",
        "voluntary_mandated",
        "openfda",
    )
    return {field: record[field] for field in fields if field in record}


def _project_crl(record: dict[str, Any]) -> dict[str, Any]:
    file_name = record.get("file_name")
    text = record.get("text")
    excerpt = text[:4_000] if isinstance(text, str) else None
    return {
        "application_number": record.get("application_number"),
        "letter_type": record.get("letter_type"),
        "letter_date": record.get("letter_date"),
        "company_name": record.get("company_name"),
        "approval_center": record.get("approval_center"),
        "file_name": file_name,
        "document_url": (
            f"https://download.open.fda.gov/crl/{quote(file_name)}"
            if isinstance(file_name, str)
            else None
        ),
        "text_excerpt": excerpt,
        "text_truncated": isinstance(text, str) and len(text) > len(excerpt or ""),
    }
