from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class StoredObject(BaseModel):
    uri: str
    checksum: str
    byte_size: int = Field(ge=0)
    compressed_byte_size: int = Field(ge=0)
    character_count: int | None = Field(default=None, ge=0)


class PreparedChunk(BaseModel):
    chunk_index: int = Field(ge=0)
    section_path: str
    content: str
    content_hash: str
    token_count: int = Field(gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestedDocument(BaseModel):
    document_id: UUID
    document_version_id: UUID
    external_key: str
    title: str
    version: str
    chunks: int = Field(ge=0)
    created: bool


class IngestionReceipt(BaseModel):
    job_id: UUID
    status: Literal["completed"] = "completed"
    dataset: str
    records_received: int = Field(ge=0)
    documents_created: int = Field(ge=0)
    versions_created: int = Field(ge=0)
    chunks_created: int = Field(ge=0)
    duplicates_skipped: int = Field(ge=0)
    raw_response_uri: str
    source_character_count: int = Field(default=0, ge=0)
    chunks_character_count: int = Field(default=0, ge=0)
    embedding_status: Literal["completed", "partial", "failed", "skipped"] = "skipped"
    embedding_segments_created: int = Field(default=0, ge=0)
    documents: list[IngestedDocument] = Field(default_factory=list)


class PDFIngestionReceipt(BaseModel):
    job_id: UUID
    status: Literal["completed"] = "completed"
    document: IngestedDocument
    source_url: str
    raw_document_uri: str
    pages: int = Field(ge=0)
    native_text_pages: int = Field(ge=0)
    ocr_pages: int = Field(ge=0)
    tables: int = Field(ge=0)
    graph_candidate_pages: list[int] = Field(default_factory=list)
    graph_digitization_status: Literal["not_required", "requires_review"]
    warnings: list[str] = Field(default_factory=list)
    extracted_character_count: int = Field(default=0, ge=0)
    chunks_character_count: int = Field(default=0, ge=0)
    embedding_status: Literal["completed", "partial", "failed", "skipped"] = "skipped"
    embedding_segments_created: int = Field(default=0, ge=0)


class EvidenceChunk(BaseModel):
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    chunk_index: int = Field(ge=0)
    section_path: str
    content: str
    token_count: int = Field(gt=0)
    external_key: str
    title: str
    dataset: str
    source_url: str | None = None
    dataset_last_updated: str | None = None
    score: float | None = None


class EvidenceSearchResult(BaseModel):
    query: str
    returned: int = Field(ge=0)
    chunks: list[EvidenceChunk]


class DocumentChunkPage(BaseModel):
    document_id: UUID
    returned: int = Field(ge=0)
    next_cursor: int | None = Field(default=None, ge=0)
    chunks: list[EvidenceChunk]
