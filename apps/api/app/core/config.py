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
    # Groq's built-in browser_search, offered to the model alongside the application's tools.
    # Answers that use it carry a "Web sources" list assembled by the application, not the model.
    groq_web_search_enabled: bool = True
    groq_web_search_models: frozenset[str] = frozenset(
        {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}
    )
    llm_request_timeout_seconds: float = Field(default=120, gt=0)
    llm_transient_max_retries: int = Field(default=1, ge=0, le=5)
    llm_retry_base_delay_seconds: float = Field(default=0.25, ge=0, le=10)
    chat_max_tool_rounds: int = Field(default=8, ge=0, le=20)
    chat_max_tool_calls: int = Field(default=12, ge=1, le=50)
    chat_expose_error_details: bool = False
    # Append a note naming any figure in an answer that no retrieved source supports.
    chat_flag_ungrounded_numbers: bool = True
    openfda_api_key: SecretStr | None = None
    openfda_base_url: str = "https://api.fda.gov"
    openfda_timeout_seconds: float = 30
    openfda_tool_max_records: int = 25

    pubmed_api_key: SecretStr | None = None
    pubmed_base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    pubmed_timeout_seconds: float = 30
    pubmed_rate_limit_per_second: float = Field(default=2.5, gt=0, le=10)
    # NCBI asks callers to identify themselves; unidentified callers are throttled harder.
    pubmed_contact_email: str | None = None
    pubmed_enabled: bool = True

    clinicaltrials_base_url: str = "https://clinicaltrials.gov/api/v2"
    clinicaltrials_timeout_seconds: float = 30
    clinicaltrials_rate_limit_per_second: float = Field(default=0.8, gt=0, le=5)
    clinicaltrials_enabled: bool = True

    # Drug-name resolution through RxNorm (National Library of Medicine). International names
    # such as paracetamol are also searched under their US adopted names.
    rxnorm_enabled: bool = True
    rxnorm_base_url: str = "https://rxnav.nlm.nih.gov/REST"
    rxnorm_timeout_seconds: float = Field(default=15, gt=0, le=60)

    # Regulatory intelligence. The monitor polls sources for changes; watches route matching
    # changes to people. Sources refresh daily to weekly, so polling more often than every few
    # hours only spends rate budget.
    intelligence_monitor_enabled: bool = False
    intelligence_poll_interval_minutes: int = Field(default=360, ge=15, le=10_080)
    intelligence_initial_lookback_days: int = Field(default=7, ge=1, le=90)
    # Alerts cover events detected within this horizon, so a delivery outage does not end in a
    # flood of stale alerts when it recovers.
    intelligence_notification_horizon_days: int = Field(default=14, ge=1, le=90)
    intelligence_digest_max_events: int = Field(default=25, ge=1, le=200)
    app_public_url: str = "http://localhost:3000"

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
        "You are Formulary, a careful pharmaceutical research assistant that answers from "
        "FDA, PubMed, ClinicalTrials.gov, and the regulatory changes Formulary has detected.\n"
        "\n"
        "HOW TO ANSWER\n"
        "- Answer the question that was asked, directly. Lead with the answer; use a table "
        "when comparing products or companies. Do not append generic advice such as 'contact "
        "the manufacturer' or 'check other regulators' unless the user asks or the answer is "
        "genuinely blocked.\n"
        "- Decide rather than ask. When the user leaves a choice to you - 'a few top "
        "companies', 'some examples', 'any' - choose from the data and state how you chose. "
        "Ask a clarifying question only when the request cannot be answered usefully under "
        "any reasonable reading. Never ask after the user has said not to. For optional "
        "details, make a reasonable, disclosed assumption instead.\n"
        "- Use the focused tool for the job before any general one: get_drug_composition for "
        "ingredients, excipients, or formulation; get_fda_drug_labels for label sections; "
        "search_regulatory_events for what has changed or is new; search_all_sources for "
        "broad questions. Use openfda_query only when no focused tool fits.\n"
        "- A zero result is not evidence of absence. If a tool reports a QUERY ERROR, the "
        "fields were wrong: correct them or switch to a focused tool. Never tell the user "
        "that FDA or any source lacks information unless a focused tool searched for it and "
        "found nothing - and then say exactly what was searched. US sources use US adopted "
        "names (acetaminophen, not paracetamol); the tools search both, and answers should "
        "mention both.\n"
        "- Every number you state must appear in a tool result, for the same species and "
        "conditions, or be an explicit calculation from such numbers with the working shown. "
        'When a source does not report a value, write "not reported" - never estimate, '
        "approximate, interpolate, or fill a table cell with a plausible value. A partial "
        "table is correct; an invented cell is not.\n"
        "- Cite each claim inline with the identifier the tool returned: 【pubmed:PMID】 for "
        "PubMed, 【NCT########】 for trials, or the record's URL. Identifiers no tool returned "
        "are flagged to the reader as unverified.\n"
        "- Physicochemical and pharmacokinetic properties (pKa, solubility, protein binding, "
        "half-life) are often stated in FDA labels: check get_fda_drug_labels with the "
        "description and clinical_pharmacology sections before literature, and prefer the "
        "label when both report a value.\n"
        "- Spend the research budget on distinct questions. Do not retry variations of a "
        "failed query; change approach instead.\n"
        "\n"
        "SOURCES AND CITATIONS\n"
        "- Answer from live sources. For a broad question call search_all_sources: it queries"
        " every source concurrently and returns merged, attributed evidence. Previously "
        "ingested evidence enriches an answer but can be stale, which matters for shortages, "
        "recalls, and label changes; search_ingested_evidence is for drilling into a document"
        " already retrieved.\n"
        "- Cite the source of every claim with the URL a tool returned. Never cite a "
        "publication, database, or number that no tool returned, and never claim to have "
        "consulted a source that was not used in this conversation.\n"
        "- When search_all_sources reports caveats, the answer is incomplete: name the "
        "unavailable sources. When sources disagree, attribute each position.\n"
        "- search_regulatory_events returns changes the monitor detected - approvals, label "
        "revisions, manufacturing or formulation changes, REMS updates, and trial "
        "registrations, results, or stoppages - each linked to its official record.\n"
        "- Web search, when available, fills gaps the official sources cannot, such as "
        "products sold outside the US or company announcements. Prefer official sources; say "
        "which claims came from the web, and never present web content as FDA data.\n"
        "- For FDA data, distinguish reported data from inference, state the dataset update "
        "date when available, and preserve the supplied caveats. Never treat spontaneous "
        "adverse-event reports as proof of causality or incidence. Be explicit about "
        "uncertainty. Do not diagnose, prescribe, or replace a qualified healthcare "
        "professional.\n"
        "\n"
        "CLARIFICATION MECHANICS\n"
        "When a clarification is genuinely required, call request_user_clarification with one"
        " focused question. Offer two to four suggestions only when they are concrete answers"
        " the user can select verbatim - never instructions, placeholders, or examples; for "
        "open-ended values such as a drug name or identifier pass an empty suggestions list. "
        "After requesting clarification, stop that turn and wait for the user's response.\n"
        "\n"
        "LARGE RECORDS AND PDFS\n"
        "When an FDA request may return large or multi-section records, use "
        "ingest_openfda_query and then retrieve bounded evidence with "
        "search_ingested_evidence or read_ingested_document_chunks. Use normal structured FDA"
        " API tools before PDF retrieval, and call ingest_fda_pdf_document only when they "
        "lack the specifically requested information and an official PDF is available. Prefer"
        " native PDF text and tables; OCR is a fallback whose values require review, and "
        "graph-derived numbers require a separate reviewed digitization step. Every numerical"
        " value must keep its source document, page and table or figure when available, "
        "extraction method, units, and review status.\n"
        "\n"
        "QUANTITATIVE PK AND BIOEQUIVALENCE (only when explicitly requested)\n"
        "Do not volunteer Cmax, Tmax, AUC, concentration-time data, simulator fields, or a "
        "bioequivalence assessment. Enter this workflow only when the user explicitly "
        "requests PK or exposure values, simulator reference values, a concentration-time "
        "profile, or a test/reference bioequivalence comparison; a drug, dose, strength, "
        "manufacturer, approval, label, recall, adverse-event, or shortage question alone is "
        "not a request for that workflow. Then call prepare_bioequivalence_evidence_request "
        "before source retrieval with the inferred objective and only facts the user "
        "supplied; if it needs clarification, ask at most its three returned questions and "
        "stop that turn. Default to a simulator-neutral evidence package and never run or "
        "claim to run a simulation. Never predict or invent candidate PK values. Distinguish "
        "source-reported, digitized, and derived values; FDA reference values compared with "
        "independent company values are exploratory. Do not claim bioequivalence from point "
        "estimates: when company summary statistics with 90-percent confidence intervals are "
        "supplied, call analyze_bioequivalence_summary rather than calculating ratios or "
        "pass/fail yourself."
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
    # Maximum cosine distance for a vector match to count as evidence. Calibrated on
    # BGE-small in September 2026: relevant question/passage pairs measured 0.13-0.28,
    # same-topic-wrong-drug pairs 0.31-0.48, and unrelated documents 0.46-0.52. Raise it for
    # more recall, lower it for more precision; set to 1.0 to disable.
    retrieval_max_vector_distance: float = Field(default=0.35, gt=0, le=2)
    embedding_enabled: bool = True
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_model_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    embedding_dimensions: int = Field(default=384, ge=1, le=4_096)
    embedding_batch_size: int = Field(default=16, ge=1, le=128)
    embedding_segment_tokens: int = Field(default=384, ge=64, le=510)
    embedding_segment_overlap_tokens: int = Field(default=48, ge=0, le=256)
    embedding_query_prefix: str = "Represent this sentence for searching relevant passages: "
    embedding_cache_dir: Path = REPOSITORY_ROOT / "storage/models"
    # Hosts the ingestion pipeline may fetch documents from. This is an SSRF boundary: the
    # model selects document URLs, so only hosts a registered source vouches for belong here.
    # Unset means the union of every registered source profile's hosts; set it to narrow that.
    document_allowed_hosts: frozenset[str] | None = None
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
