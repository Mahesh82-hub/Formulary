from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Formulary API"
    app_env: Literal["development", "test", "staging", "production"] = "development"
    app_debug: bool = False
    sql_echo: bool = False
    cors_origins: list[str] = ["http://localhost:3000"]

    auth_otp_pepper: SecretStr | None = None
    auth_otp_ttl_seconds: int = 300
    auth_otp_max_attempts: int = 5
    auth_otp_requests_per_15_minutes: int = 5
    auth_otp_requests_per_ip_15_minutes: int = 20
    auth_session_ttl_days: int = 30
    session_cookie_name: str = "formulary_session"

    default_llm_provider: Literal["openai", "groq"] = "openai"
    default_llm_model: str = ""
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    llm_request_timeout_seconds: float = Field(default=120, gt=0)
    llm_transient_max_retries: int = Field(default=1, ge=0, le=5)
    llm_retry_base_delay_seconds: float = Field(default=0.25, ge=0, le=10)
    chat_max_tool_rounds: int = Field(default=8, ge=0, le=20)
    chat_max_tool_calls: int = Field(default=12, ge=1, le=50)
    chat_expose_error_details: bool = False
    openfda_api_key: SecretStr | None = None
    openfda_base_url: str = "https://api.fda.gov"
    openfda_timeout_seconds: float = 30
    openfda_tool_max_records: int = 25

    # Upstream resilience. openFDA allows roughly 240 requests per minute (4/s); the default
    # pacing stays below that so a burst of concurrent tool calls cannot trip the limit.
    openfda_rate_limit_per_second: float = Field(default=3.0, gt=0, le=100)
    openfda_rate_limit_burst: int = Field(default=6, ge=1, le=100)
    openfda_max_attempts: int = Field(default=3, ge=1, le=10)
    openfda_retry_base_delay_seconds: float = Field(default=0.5, ge=0, le=30)
    openfda_retry_max_delay_seconds: float = Field(default=8.0, ge=0, le=120)

    # Per-tool record caps. These bound how much upstream data reaches the model in one call.
    openfda_query_max_records: int = Field(default=10, ge=1, le=100)
    openfda_label_max_records: int = Field(default=5, ge=1, le=100)
    openfda_search_max_records: int = Field(default=10, ge=1, le=100)
    openfda_crl_max_records: int = Field(default=5, ge=1, le=100)
    openfda_ingest_max_records: int = Field(default=25, ge=1, le=100)
    openfda_result_max_characters: int = Field(default=48_000, ge=1_000, le=500_000)
    faers_max_reactions: int = Field(default=25, ge=1, le=100)
    chat_system_prompt: str = (
        "You are Formulary, a careful pharmaceutical research assistant. "
        "For an ordinary drug question, answer only the requested FDA or pharmaceutical scope. "
        "When an ordinary chat request cannot be answered responsibly without one material user "
        "choice or missing detail, call request_user_clarification with one focused question and "
        "two to four concise suggestions only when they are concrete answers the user can select "
        "verbatim. Never use instructions, placeholders, examples, or requests such as 'provide "
        "the drug name' as suggestions. For open-ended values such as a drug name, product name, "
        "date, identifier, or study detail, pass an empty suggestions list so the UI shows only a "
        "text box. Do not invent arbitrary domain values merely to populate suggestions. Do not "
        "use clarification for optional details: make a reasonable, disclosed assumption instead. "
        "After requesting clarification, stop that turn and wait for the user's response. "
        "Do not volunteer Cmax, Tmax, AUC, concentration-time data, simulator fields, or a "
        "bioequivalence assessment. Enter the quantitative PK/reference workflow only when the "
        "user explicitly requests PK or exposure values, reference values for a simulator, a "
        "concentration-time profile, or a test/reference bioequivalence comparison. A drug name, "
        "dose, strength, dosage form, manufacturer, approval, label, recall, adverse-event, or "
        "shortage question alone is not a request for that workflow. "
        "Your quantitative-research role is simulator-neutral: find, extract, reconcile, and "
        "explain reference evidence that users may export to pharmaceutical simulators; do not "
        "run or claim to have run a simulation. Never predict or invent candidate PK values. "
        "Infer whether the user wants general FDA information, source-reported reference values, "
        "or a test/reference bioequivalence comparison. Do not ask the user to choose when the "
        "request already makes the objective clear. For quantitative reference evidence or a "
        "bioequivalence comparison, call prepare_bioequivalence_evidence_request before source "
        "retrieval and pass the inferred objective explicitly. Pass "
        "only facts the user supplied or that were already established in the conversation. If "
        "the intake needs clarification, ask at most its three returned questions and stop that "
        "turn without researching. Treat the simulator name as optional and default to a "
        "simulator-neutral evidence package. Distinguish source-reported, digitized, and "
        "deterministically derived values. FDA-extracted reference values compared with "
        "independent company values are exploratory unless suitable test/reference study "
        "statistics are provided. Do not claim bioequivalence from point estimates: the "
        "configured deterministic analysis must assess the required 90-percent confidence "
        "intervals. When suitable company summary statistics are supplied, call "
        "analyze_bioequivalence_summary rather than calculating ratios or pass/fail yourself. "
        "Answer from live sources. For any question not already narrowed to a single dataset, "
        "call search_all_sources first: it queries every available source concurrently and "
        "returns merged, attributed evidence. Do not substitute previously ingested evidence "
        "for a live query - the local corpus enriches an answer but can be stale, which matters "
        "for shortages, recalls, and label changes. search_ingested_evidence remains available "
        "for drilling into a document already retrieved. "
        "When search_all_sources reports caveats, the answer is incomplete: name the sources "
        "that were unavailable rather than presenting the remaining evidence as complete. "
        "Cite the source of every claim, and when sources disagree, report the disagreement and "
        "attribute each position rather than silently choosing one. "
        "When an FDA request may return large or multi-section records, use "
        "ingest_openfda_query and then retrieve bounded evidence with "
        "search_ingested_evidence or read_ingested_document_chunks. Use normal structured FDA "
        "API tools before PDF retrieval. Call ingest_fda_pdf_document only when the local cache "
        "and normal FDA API results do not contain the specifically requested information or "
        "quantitative values and an official FDA PDF is available; never retrieve a PDF merely "
        "because a drug was mentioned. After PDF ingestion, search its bounded chunks. Prefer "
        "native PDF text and "
        "tables; OCR is a fallback for scanned pages and its values require review. Never claim "
        "that OCR digitized graph coordinates; graph-derived numbers require a separate reviewed "
        "digitization step. Every numerical value must retain its source document, page and table "
        "or figure when available, extraction method, units, study context, and review status. "
        "Never cite a publication, "
        "database, or numerical source that was not returned by an available tool. "
        "Be explicit about uncertainty and never claim to have consulted a source or tool unless "
        "it was provided in this conversation. Do not diagnose, prescribe, or replace a qualified "
        "healthcare professional. Use available tools when they materially improve accuracy. "
        "When using FDA data, distinguish reported data from inference, cite the returned source "
        "URLs, state the dataset update date when available, and preserve the supplied caveats. "
        "Never treat spontaneous adverse-event reports as proof of causality or incidence."
    )

    email_delivery_mode: Literal["console", "smtp"] = "console"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_email: str | None = None
    smtp_start_tls: bool = True

    storage_root: Path = REPOSITORY_ROOT / "storage"
    ingestion_chunk_size_tokens: int = 1_024
    ingestion_chunk_overlap_tokens: int = 128
    ingestion_search_max_chunks: int = 8

    # Federated search. Every question is asked of every registered source concurrently, so
    # total latency tracks the per-source deadline rather than the number of sources.
    federated_per_source_timeout_seconds: float = Field(default=8.0, gt=0, le=120)
    federated_per_source_limit: int = Field(default=5, ge=1, le=50)
    federated_max_records: int = Field(default=12, ge=1, le=100)

    # Reciprocal rank fusion. 'k' damps the influence of top ranks; the candidate multiplier
    # controls how deep each retrieval arm looks before the arms are merged.
    retrieval_rrf_k: int = Field(default=60, ge=1, le=1_000)
    retrieval_candidate_multiplier: int = Field(default=4, ge=1, le=20)
    embedding_enabled: bool = True
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_model_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    embedding_dimensions: int = Field(default=384, ge=1, le=4_096)
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    embedding_segment_tokens: int = Field(default=384, ge=64, le=510)
    embedding_segment_overlap_tokens: int = Field(default=48, ge=0, le=256)
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embedding_cache_dir: Path = REPOSITORY_ROOT / "storage/models"
    fda_pdf_timeout_seconds: float = Field(default=60, gt=0)
    fda_pdf_max_bytes: int = Field(default=50_000_000, ge=1_000_000, le=250_000_000)
    pdf_native_text_min_characters: int = Field(default=80, ge=0, le=10_000)
    pdf_ocr_dpi: int = Field(default=200, ge=100, le=400)
    chat_pdf_max_bytes: int = Field(default=20_000_000, ge=1_000_000, le=50_000_000)
    chat_pdf_max_pages: int = Field(default=100, ge=1, le=500)
    chat_pdf_max_extracted_characters: int = Field(
        default=80_000,
        ge=10_000,
        le=500_000,
    )

    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: SecretStr

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )

    @property
    def resolved_storage_root(self) -> Path:
        if self.storage_root.is_absolute():
            return self.storage_root
        return REPOSITORY_ROOT / self.storage_root

    @property
    def resolved_embedding_cache_dir(self) -> Path:
        if self.embedding_cache_dir.is_absolute():
            return self.embedding_cache_dir
        return REPOSITORY_ROOT / self.embedding_cache_dir

    @property
    def chat_error_details_enabled(self) -> bool:
        return self.chat_expose_error_details and self.app_env != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
