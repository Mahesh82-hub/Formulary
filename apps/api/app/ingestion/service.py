import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.fda.client import OpenFDAClient
from app.fda.models import OpenFDADataset, OpenFDAResult
from app.ingestion.chunking import LlamaIndexJSONChunker
from app.ingestion.embeddings import BGEONNXEmbeddingProvider, EmbeddingProvider
from app.ingestion.models import (
    DocumentChunkPage,
    EvidenceChunk,
    EvidenceSearchResult,
    IngestedDocument,
    IngestionReceipt,
    PDFIngestionReceipt,
)
from app.ingestion.pdf import DocumentFetcher, OCRMode, PDFEvidenceExtractor
from app.ingestion.storage import LocalObjectStorage
from app.models import (
    DataSource,
    Document,
    DocumentChunk,
    DocumentChunkEmbedding,
    DocumentVersion,
    IngestionJob,
)
from app.sources.fusion import DEFAULT_RRF_K, reciprocal_rank_score
from app.sources.profile import registered_document_hosts

logger = logging.getLogger(__name__)
EmbeddingStatus = Literal["completed", "partial", "failed", "skipped"]


@dataclass(slots=True)
class EmbeddingIndexingResult:
    status: EmbeddingStatus
    chunks_considered: int = 0
    segments_created: int = 0
    characters_indexed: int = 0


class FDAIngestionCoordinator:
    """Fetch, persist, deduplicate, chunk, and retrieve public openFDA records."""

    def __init__(
        self,
        *,
        storage: LocalObjectStorage,
        chunker: LlamaIndexJSONChunker,
        session_factory: async_sessionmaker[AsyncSession],
        max_search_chunks: int,
        rrf_k: int = DEFAULT_RRF_K,
        candidate_multiplier: int = 4,
        max_vector_distance: float | None = None,
        embedder: EmbeddingProvider | None = None,
        pdf_fetcher: DocumentFetcher | None = None,
        pdf_extractor: PDFEvidenceExtractor | None = None,
    ) -> None:
        self._storage = storage
        self._chunker = chunker
        self._session_factory = session_factory
        self._max_search_chunks = max_search_chunks
        self._rrf_k = rrf_k
        self._candidate_multiplier = candidate_multiplier
        self._max_vector_distance = max_vector_distance
        self._embedder = embedder
        self._pdf_fetcher = pdf_fetcher
        self._pdf_extractor = pdf_extractor

    async def ingest_query(
        self,
        fda: OpenFDAClient,
        dataset: OpenFDADataset,
        *,
        search: str | None = None,
        sort: str | None = None,
        limit: int = 5,
    ) -> IngestionReceipt:
        job_id = await self._create_job(
            {
                "source": "openFDA",
                "dataset": dataset,
                "search": search,
                "sort": sort,
                "limit": limit,
            }
        )
        try:
            result = await fda.query(
                dataset,
                search=search,
                sort=sort,
                limit=limit,
            )
            return await self._persist_result(job_id, result)
        except Exception:
            await self._mark_job_failed(job_id)
            raise

    async def ingest_fda_pdf(
        self,
        url: str,
        *,
        title: str | None = None,
        ocr_mode: OCRMode = "auto",
    ) -> PDFIngestionReceipt:
        if self._pdf_fetcher is None or self._pdf_extractor is None:
            raise RuntimeError("FDA PDF ingestion is not configured")
        job_id = await self._create_job(
            {"source": "FDA PDF", "url": url, "title": title, "ocr_mode": ocr_mode}
        )
        try:
            payload, final_url = await self._pdf_fetcher.fetch(url)
            extracted = await asyncio.to_thread(
                self._pdf_extractor.extract,
                payload,
                ocr_mode=ocr_mode,
            )
            return await self._persist_pdf(
                job_id,
                payload,
                final_url,
                title=title,
                extracted=extracted,
            )
        except Exception:
            await self._mark_job_failed(job_id)
            raise

    async def backfill_embeddings(self, *, batch_size: int = 16) -> dict[str, Any]:
        if self._embedder is None:
            return {"status": "skipped", "reason": "embedding_disabled"}
        bounded_batch_size = min(max(1, batch_size), 100)
        last_chunk_id: UUID | None = None
        chunks_processed = 0
        segments_created = 0
        characters_indexed = 0
        failed_batches = 0
        while True:
            async with self._session_factory() as session:
                statement = (
                    select(DocumentChunk)
                    .where(DocumentChunk.embedding_status != "completed")
                    .order_by(DocumentChunk.id)
                    .limit(bounded_batch_size)
                )
                if last_chunk_id is not None:
                    statement = statement.where(DocumentChunk.id > last_chunk_id)
                chunks = list(await session.scalars(statement))
                if not chunks:
                    break
                result = await self._index_chunks(session, chunks)
                await session.commit()
                last_chunk_id = chunks[-1].id
                chunks_processed += len(chunks)
                segments_created += result.segments_created
                characters_indexed += result.characters_indexed
                if result.status == "failed":
                    failed_batches += 1
                logger.info(
                    "Embedding backfill batch completed chunks=%d segments=%d status=%s",
                    len(chunks),
                    result.segments_created,
                    result.status,
                )
        return {
            "status": "completed" if failed_batches == 0 else "partial",
            "model_name": self._embedder.model_name,
            "model_revision": self._embedder.model_revision,
            "backend": self._embedder.backend,
            "dimensions": self._embedder.dimensions,
            "chunks_processed": chunks_processed,
            "segments_created": segments_created,
            "characters_indexed": characters_indexed,
            "failed_batches": failed_batches,
        }

    async def search_chunks(
        self,
        query: str,
        *,
        dataset: str | None = None,
        external_key: str | None = None,
        limit: int = 5,
    ) -> EvidenceSearchResult:
        normalized_query = " ".join(query.split())
        if not normalized_query or len(normalized_query) > 500:
            raise ValueError("Evidence search query must contain between 1 and 500 characters")
        bounded_limit = min(max(1, limit), self._max_search_chunks)
        candidate_limit = max(bounded_limit * self._candidate_multiplier, bounded_limit)
        async with self._session_factory() as session:
            latest_version = (
                select(DocumentVersion.id)
                .where(DocumentVersion.document_id == Document.id)
                .order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
                .limit(1)
                .correlate(Document)
                .scalar_subquery()
            )
            ts_query = func.websearch_to_tsquery("english", normalized_query)
            score = func.ts_rank_cd(DocumentChunk.search_vector, ts_query).label("score")
            statement = (
                select(DocumentChunk, DocumentVersion, Document, DataSource, score)
                .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
                .join(Document, Document.id == DocumentVersion.document_id)
                .join(DataSource, DataSource.id == Document.data_source_id)
                .where(
                    Document.deleted_at.is_(None),
                    DocumentVersion.id == latest_version,
                    DocumentChunk.search_vector.op("@@")(ts_query),
                )
                .order_by(score.desc(), DocumentChunk.id)
                .limit(candidate_limit)
            )
            if dataset:
                statement = statement.where(DataSource.slug == self._dataset_slug(dataset))
            if external_key:
                statement = statement.where(Document.external_key == external_key)
            text_rows = (await session.execute(statement)).all()

            vector_rows: list[Any] = []
            if self._embedder is not None:
                try:
                    query_embedding = await asyncio.to_thread(
                        self._embedder.embed_query, normalized_query
                    )
                    distance = DocumentChunkEmbedding.embedding.cosine_distance(
                        query_embedding
                    ).label("distance")
                    vector_statement = (
                        select(
                            DocumentChunk,
                            DocumentVersion,
                            Document,
                            DataSource,
                            distance,
                        )
                        .join(
                            DocumentChunkEmbedding,
                            DocumentChunkEmbedding.document_chunk_id == DocumentChunk.id,
                        )
                        .join(
                            DocumentVersion,
                            DocumentVersion.id == DocumentChunk.document_version_id,
                        )
                        .join(Document, Document.id == DocumentVersion.document_id)
                        .join(DataSource, DataSource.id == Document.data_source_id)
                        .where(
                            Document.deleted_at.is_(None),
                            DocumentVersion.id == latest_version,
                            DocumentChunkEmbedding.model_name == self._embedder.model_name,
                            DocumentChunkEmbedding.model_revision
                            == self._embedder.model_revision,
                        )
                    )
                    if self._max_vector_distance is not None:
                        # Nearest-neighbour search always returns something; without a floor
                        # a one-document corpus returned an FDA guidance on mold in cherry
                        # jam as "evidence" for four unrelated drug questions.
                        vector_statement = vector_statement.where(
                            DocumentChunkEmbedding.embedding.cosine_distance(query_embedding)
                            <= self._max_vector_distance
                        )
                    vector_statement = (
                        vector_statement
                        .order_by(distance, DocumentChunkEmbedding.id)
                        .limit(candidate_limit)
                    )
                    if dataset:
                        vector_statement = vector_statement.where(
                            DataSource.slug == self._dataset_slug(dataset)
                        )
                    if external_key:
                        vector_statement = vector_statement.where(
                            Document.external_key == external_key
                        )
                    vector_rows = list((await session.execute(vector_statement)).all())
                except Exception as error:
                    logger.warning("Vector retrieval failed; using full-text fallback: %s", error)

            candidates: dict[UUID, tuple[Any, Any, Any, Any]] = {}
            fusion_scores: dict[UUID, float] = {}
            for rank, row in enumerate(text_rows, start=1):
                chunk, version, document, source, _ = row
                candidates[chunk.id] = (chunk, version, document, source)
                fusion_scores[chunk.id] = fusion_scores.get(
                    chunk.id, 0.0
                ) + reciprocal_rank_score(rank, k=self._rrf_k)
            seen_vector_chunks: set[UUID] = set()
            vector_rank = 0
            for row in vector_rows:
                chunk, version, document, source, _ = row
                if chunk.id in seen_vector_chunks:
                    continue
                seen_vector_chunks.add(chunk.id)
                vector_rank += 1
                candidates[chunk.id] = (chunk, version, document, source)
                fusion_scores[chunk.id] = fusion_scores.get(
                    chunk.id, 0.0
                ) + reciprocal_rank_score(vector_rank, k=self._rrf_k)
            selected_ids = sorted(
                candidates,
                key=lambda chunk_id: (-fusion_scores[chunk_id], str(chunk_id)),
            )[:bounded_limit]
            chunks = [
                self._evidence_chunk(*candidates[chunk_id], fusion_scores[chunk_id])
                for chunk_id in selected_ids
            ]
        return EvidenceSearchResult(
            query=normalized_query,
            returned=len(chunks),
            chunks=chunks,
        )

    async def read_document_chunks(
        self,
        document_id: UUID,
        *,
        cursor: int = 0,
        limit: int = 5,
    ) -> DocumentChunkPage:
        if cursor < 0:
            raise ValueError("Chunk cursor must be non-negative")
        bounded_limit = min(max(1, limit), self._max_search_chunks)
        async with self._session_factory() as session:
            version = await session.scalar(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document_id)
                .order_by(DocumentVersion.created_at.desc(), DocumentVersion.id.desc())
                .limit(1)
            )
            if version is None:
                raise ValueError("Ingested document was not found")
            document = await session.get(Document, document_id)
            if document is None or document.deleted_at is not None:
                raise ValueError("Ingested document was not found")
            source = await session.get(DataSource, document.data_source_id)
            if source is None:
                raise ValueError("Ingested data source was not found")
            rows = (
                await session.scalars(
                    select(DocumentChunk)
                    .where(
                        DocumentChunk.document_version_id == version.id,
                        DocumentChunk.chunk_index >= cursor,
                    )
                    .order_by(DocumentChunk.chunk_index)
                    .limit(bounded_limit + 1)
                )
            ).all()
            has_more = len(rows) > bounded_limit
            selected = rows[:bounded_limit]
            chunks = [
                self._evidence_chunk(chunk, version, document, source, None) for chunk in selected
            ]
            next_cursor = selected[-1].chunk_index + 1 if has_more and selected else None
        return DocumentChunkPage(
            document_id=document_id,
            returned=len(chunks),
            next_cursor=next_cursor,
            chunks=chunks,
        )

    async def _create_job(self, request: dict[str, Any]) -> UUID:
        async with self._session_factory() as session:
            job = IngestionJob(status="queued", request=request)
            session.add(job)
            await session.commit()
            await session.refresh(job)
            return job.id

    async def _persist_result(
        self,
        job_id: UUID,
        result: OpenFDAResult,
    ) -> IngestionReceipt:
        raw_response = await asyncio.to_thread(
            self._storage.put_json_gzip,
            "raw/openfda/responses",
            result.model_dump(mode="json"),
        )
        documents_created = 0
        versions_created = 0
        chunks_created = 0
        duplicates_skipped = 0
        chunks_character_count = 0
        embedding_segments_created = 0
        embedding_statuses: list[str] = []
        ingested_documents: list[IngestedDocument] = []

        async with self._session_factory() as session:
            source = await self._get_or_create_source(session, result.query.dataset)
            job = await session.get(IngestionJob, job_id)
            if job is None:
                raise RuntimeError("Ingestion job disappeared")
            job.status = "running"
            job.started_at = datetime.now(UTC)
            job.data_source_id = source.id
            job.progress = {
                "records_total": result.returned,
                "records_processed": 0,
                "raw_response_uri": raw_response.uri,
                "raw_response_byte_count": raw_response.byte_size,
                "raw_response_compressed_byte_count": raw_response.compressed_byte_size,
                "raw_response_character_count": raw_response.character_count or 0,
                "stored_in_postgres": False,
                "indexed": False,
            }
            await session.commit()

            for record_index, record in enumerate(result.results):
                stored_record = await asyncio.to_thread(
                    self._storage.put_json_gzip,
                    f"raw/openfda/records/{result.query.dataset}",
                    record,
                )
                external_key = self._external_key(record, stored_record.checksum)
                document = await session.scalar(
                    select(Document).where(
                        Document.data_source_id == source.id,
                        Document.external_key == external_key,
                    )
                )
                document_created = document is None
                if document is None:
                    document = Document(
                        data_source_id=source.id,
                        external_key=external_key,
                        title=self._record_title(record, external_key),
                        source_metadata={
                            "dataset": result.query.dataset,
                            "source": "openFDA",
                        },
                    )
                    session.add(document)
                    await session.flush()
                    documents_created += 1
                else:
                    document.title = self._record_title(record, external_key)
                    document.source_metadata = {
                        **document.source_metadata,
                        "dataset": result.query.dataset,
                        "source": "openFDA",
                    }

                existing_version = await session.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.document_id == document.id,
                        DocumentVersion.checksum == stored_record.checksum,
                    )
                )
                if existing_version is not None:
                    existing_chunks = list(
                        await session.scalars(
                            select(DocumentChunk).where(
                            DocumentChunk.document_version_id == existing_version.id
                        )
                    )
                    )
                    duplicate_chunks = len(existing_chunks)
                    chunks_character_count += sum(
                        len(chunk.content) for chunk in existing_chunks
                    )
                    embedding_result = await self._index_chunks(session, existing_chunks)
                    embedding_segments_created += embedding_result.segments_created
                    embedding_statuses.append(embedding_result.status)
                    duplicates_skipped += 1
                    ingested_documents.append(
                        IngestedDocument(
                            document_id=document.id,
                            document_version_id=existing_version.id,
                            external_key=external_key,
                            title=document.title,
                            version=existing_version.version,
                            chunks=duplicate_chunks,
                            created=False,
                        )
                    )
                else:
                    version_name = await self._unique_version_name(
                        session,
                        document.id,
                        self._source_version(record, stored_record.checksum),
                        stored_record.checksum,
                    )
                    version = DocumentVersion(
                        document_id=document.id,
                        version=version_name,
                        checksum=stored_record.checksum,
                        object_uri=stored_record.uri,
                        media_type="application/json",
                        byte_size=stored_record.byte_size,
                        extraction_status="processing",
                        source_metadata={
                            "query": result.query.model_dump(mode="json"),
                            "provenance": result.provenance.model_dump(mode="json"),
                            "record_index": record_index,
                        },
                    )
                    session.add(version)
                    await session.flush()
                    prepared_chunks = await asyncio.to_thread(
                        self._chunker.chunk_record,
                        record,
                        dataset=result.query.dataset,
                        external_key=external_key,
                    )
                    chunk_rows = [
                            DocumentChunk(
                                document_version_id=version.id,
                                chunk_index=chunk.chunk_index,
                                section_path=chunk.section_path,
                                content=chunk.content,
                                content_hash=chunk.content_hash,
                                token_count=chunk.token_count,
                                chunk_metadata=chunk.metadata,
                            )
                            for chunk in prepared_chunks
                    ]
                    session.add_all(chunk_rows)
                    await session.flush()
                    embedding_result = await self._index_chunks(session, chunk_rows)
                    embedding_segments_created += embedding_result.segments_created
                    embedding_statuses.append(embedding_result.status)
                    chunks_character_count += sum(len(chunk.content) for chunk in chunk_rows)
                    version.extraction_status = "completed"
                    version.processed_at = datetime.now(UTC)
                    versions_created += 1
                    chunks_created += len(prepared_chunks)
                    ingested_documents.append(
                        IngestedDocument(
                            document_id=document.id,
                            document_version_id=version.id,
                            external_key=external_key,
                            title=document.title,
                            version=version.version,
                            chunks=len(prepared_chunks),
                            created=document_created,
                        )
                    )

                job.progress = {
                    **job.progress,
                    "records_processed": record_index + 1,
                    "documents_created": documents_created,
                    "versions_created": versions_created,
                    "chunks_created": chunks_created,
                    "duplicates_skipped": duplicates_skipped,
                    "chunks_character_count": chunks_character_count,
                    "embedding_segments_created": embedding_segments_created,
                    "embedding_status": self._aggregate_embedding_status(embedding_statuses),
                    "stored_in_postgres": True,
                    "indexed": self._aggregate_embedding_status(embedding_statuses)
                    == "completed",
                }
                await session.commit()

            job.status = "completed"
            job.completed_at = datetime.now(UTC)
            job.progress = {
                **job.progress,
                "records_processed": result.returned,
                "raw_response_uri": raw_response.uri,
                "stored_in_postgres": True,
                "indexed": self._aggregate_embedding_status(embedding_statuses) == "completed",
            }
            await session.commit()

        return IngestionReceipt(
            job_id=job_id,
            dataset=result.query.dataset,
            records_received=result.returned,
            documents_created=documents_created,
            versions_created=versions_created,
            chunks_created=chunks_created,
            duplicates_skipped=duplicates_skipped,
            raw_response_uri=raw_response.uri,
            source_character_count=raw_response.character_count or 0,
            chunks_character_count=chunks_character_count,
            embedding_status=self._aggregate_embedding_status(embedding_statuses),
            embedding_segments_created=embedding_segments_created,
            documents=ingested_documents,
        )

    async def _persist_pdf(
        self,
        job_id: UUID,
        payload: bytes,
        source_url: str,
        *,
        title: str | None,
        extracted: Any,
    ) -> PDFIngestionReceipt:
        stored = await asyncio.to_thread(
            self._storage.put_bytes,
            "raw/fda/documents",
            payload,
            extension="pdf",
        )
        external_key = f"url-sha256:{hashlib.sha256(source_url.encode('utf-8')).hexdigest()}"
        document_title = pdf_title(title, source_url, extracted.sections)
        extracted_character_count = sum(
            len(str(section.get("content", ""))) for section in extracted.sections
        )
        chunks_character_count = 0
        embedding_result = EmbeddingIndexingResult(status="skipped")
        async with self._session_factory() as session:
            source = await self._get_or_create_pdf_source(session)
            job = await session.get(IngestionJob, job_id)
            if job is None:
                raise RuntimeError("Ingestion job disappeared")
            job.status = "running"
            job.started_at = datetime.now(UTC)
            job.data_source_id = source.id

            document = await session.scalar(
                select(Document).where(
                    Document.data_source_id == source.id,
                    Document.external_key == external_key,
                )
            )
            document_created = document is None
            if document is None:
                document = Document(
                    data_source_id=source.id,
                    external_key=external_key,
                    title=document_title,
                    source_metadata={"source": "FDA PDF", "source_url": source_url},
                )
                session.add(document)
                await session.flush()
            else:
                document.title = document_title
                document.source_metadata = {
                    **document.source_metadata,
                    "source_url": source_url,
                }
            job.document_id = document.id

            existing = await session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.document_id == document.id,
                    DocumentVersion.checksum == stored.checksum,
                )
            )
            created_version = existing is None
            if existing is None:
                version = DocumentVersion(
                    document_id=document.id,
                    version=stored.checksum[:16],
                    checksum=stored.checksum,
                    object_uri=stored.uri,
                    media_type="application/pdf",
                    byte_size=stored.byte_size,
                    extraction_status="processing",
                    source_metadata={
                        "provenance": {"source_url": source_url},
                        "extraction": {
                            "pages": extracted.pages,
                            "native_text_pages": extracted.native_text_pages,
                            "ocr_pages": extracted.ocr_pages,
                            "tables": extracted.tables,
                            "graph_candidate_pages": extracted.graph_candidate_pages,
                            "warnings": extracted.warnings,
                        },
                    },
                )
                session.add(version)
                await session.flush()
                prepared_chunks = await asyncio.to_thread(
                    self._chunker.chunk_sections,
                    extracted.sections,
                    dataset="fda-documents",
                    external_key=external_key,
                )
                chunk_rows = [
                        DocumentChunk(
                            document_version_id=version.id,
                            chunk_index=chunk.chunk_index,
                            section_path=chunk.section_path,
                            content=chunk.content,
                            content_hash=chunk.content_hash,
                            token_count=chunk.token_count,
                            chunk_metadata=chunk.metadata,
                        )
                        for chunk in prepared_chunks
                ]
                session.add_all(chunk_rows)
                await session.flush()
                embedding_result = await self._index_chunks(session, chunk_rows)
                chunks_character_count = sum(len(chunk.content) for chunk in chunk_rows)
                version.extraction_status = "completed"
                version.processed_at = datetime.now(UTC)
                chunk_count = len(prepared_chunks)
            else:
                version = existing
                existing_chunks = list(
                    await session.scalars(
                        select(DocumentChunk).where(
                            DocumentChunk.document_version_id == version.id
                        )
                    )
                )
                chunk_count = len(existing_chunks)
                chunks_character_count = sum(len(chunk.content) for chunk in existing_chunks)
                embedding_result = await self._index_chunks(session, existing_chunks)

            job.status = "completed"
            job.completed_at = datetime.now(UTC)
            job.progress = {
                "pages": extracted.pages,
                "chunks_created": chunk_count if created_version else 0,
                "duplicate": not created_version,
                "raw_document_uri": stored.uri,
                "raw_document_byte_count": stored.byte_size,
                "extracted_character_count": extracted_character_count,
                "chunks_character_count": chunks_character_count,
                "stored_in_postgres": True,
                "indexed": embedding_result.status == "completed",
                "embedding_status": embedding_result.status,
                "embedding_segments_created": embedding_result.segments_created,
            }
            await session.commit()
            ingested_document = IngestedDocument(
                document_id=document.id,
                document_version_id=version.id,
                external_key=external_key,
                title=document.title,
                version=version.version,
                chunks=chunk_count,
                created=document_created,
            )
        return PDFIngestionReceipt(
            job_id=job_id,
            document=ingested_document,
            source_url=source_url,
            raw_document_uri=stored.uri,
            pages=extracted.pages,
            native_text_pages=extracted.native_text_pages,
            ocr_pages=extracted.ocr_pages,
            tables=extracted.tables,
            graph_candidate_pages=extracted.graph_candidate_pages,
            graph_digitization_status=(
                "requires_review" if extracted.graph_candidate_pages else "not_required"
            ),
            warnings=extracted.warnings,
            extracted_character_count=extracted_character_count,
            chunks_character_count=chunks_character_count,
            embedding_status=self._aggregate_embedding_status([embedding_result.status]),
            embedding_segments_created=embedding_result.segments_created,
        )

    async def _index_chunks(
        self,
        session: AsyncSession,
        chunks: list[DocumentChunk],
    ) -> EmbeddingIndexingResult:
        characters = sum(len(chunk.content) for chunk in chunks)
        if self._embedder is None:
            for chunk in chunks:
                chunk.embedding_status = "skipped"
                chunk.embedding_metadata = {"reason": "embedding_disabled"}
            return EmbeddingIndexingResult(
                status="skipped",
                chunks_considered=len(chunks),
                characters_indexed=0,
            )

        pending = [
            chunk
            for chunk in chunks
            if chunk.embedding_status != "completed"
            or chunk.embedding_metadata.get("model_name") != self._embedder.model_name
            or chunk.embedding_metadata.get("model_revision") != self._embedder.model_revision
        ]
        if not pending:
            return EmbeddingIndexingResult(
                status="completed",
                chunks_considered=len(chunks),
                characters_indexed=characters,
            )

        try:
            prepared: list[tuple[DocumentChunk, int, str, int]] = []
            for chunk in pending:
                segments = await asyncio.to_thread(self._embedder.segment_text, chunk.content)
                for segment_index, segment in enumerate(segments):
                    prepared.append(
                        (
                            chunk,
                            segment_index,
                            segment,
                            await asyncio.to_thread(self._embedder.count_tokens, segment),
                        )
                    )
            vectors = await asyncio.to_thread(
                self._embedder.embed_documents,
                [segment for _, _, segment, _ in prepared],
            )
            if len(vectors) != len(prepared):
                raise RuntimeError("Embedding provider returned an unexpected vector count")

            pending_ids = [chunk.id for chunk in pending]
            if pending_ids:
                await session.execute(
                    delete(DocumentChunkEmbedding).where(
                        DocumentChunkEmbedding.document_chunk_id.in_(pending_ids),
                        DocumentChunkEmbedding.model_name == self._embedder.model_name,
                        DocumentChunkEmbedding.model_revision == self._embedder.model_revision,
                    )
                )
            session.add_all(
                [
                    DocumentChunkEmbedding(
                        document_chunk_id=chunk.id,
                        segment_index=segment_index,
                        content=segment,
                        content_hash=hashlib.sha256(segment.encode("utf-8")).hexdigest(),
                        token_count=token_count,
                        model_name=self._embedder.model_name,
                        model_revision=self._embedder.model_revision,
                        backend=self._embedder.backend,
                        dimensions=self._embedder.dimensions,
                        embedding=vector,
                    )
                    for (chunk, segment_index, segment, token_count), vector in zip(
                        prepared, vectors, strict=True
                    )
                ]
            )
            now = datetime.now(UTC)
            segments_per_chunk: dict[UUID, int] = {}
            for chunk, _, _, _ in prepared:
                segments_per_chunk[chunk.id] = segments_per_chunk.get(chunk.id, 0) + 1
            for chunk in pending:
                segment_count = segments_per_chunk.get(chunk.id, 0)
                chunk.embedding_status = "completed" if segment_count else "skipped"
                chunk.embedding_metadata = {
                    "model_name": self._embedder.model_name,
                    "model_revision": self._embedder.model_revision,
                    "backend": self._embedder.backend,
                    "dimensions": self._embedder.dimensions,
                    "segments": segment_count,
                    "indexed": bool(segment_count),
                    "character_count": len(chunk.content),
                }
                chunk.embedded_at = now if segment_count else None
            return EmbeddingIndexingResult(
                status="completed" if prepared else "skipped",
                chunks_considered=len(chunks),
                segments_created=len(prepared),
                characters_indexed=characters if prepared else 0,
            )
        except Exception as error:
            logger.exception("BGE indexing failed for %d chunks", len(pending))
            for chunk in pending:
                chunk.embedding_status = "failed"
                chunk.embedding_metadata = {
                    "model_name": self._embedder.model_name,
                    "model_revision": self._embedder.model_revision,
                    "backend": self._embedder.backend,
                    "indexed": False,
                    "error_type": type(error).__name__,
                    "error": str(error)[:1_000],
                }
                chunk.embedded_at = None
            return EmbeddingIndexingResult(
                status="failed",
                chunks_considered=len(chunks),
                characters_indexed=0,
            )

    async def _get_or_create_source(
        self,
        session: AsyncSession,
        dataset: str,
    ) -> DataSource:
        slug = self._source_slug(dataset)
        source = await session.scalar(select(DataSource).where(DataSource.slug == slug))
        if source is None:
            source = DataSource(
                slug=slug,
                kind="api",
                name=f"openFDA {dataset}",
                config={"dataset": dataset, "public": True},
            )
            session.add(source)
            await session.flush()
        return source

    async def _get_or_create_pdf_source(self, session: AsyncSession) -> DataSource:
        source = await session.scalar(select(DataSource).where(DataSource.slug == "fda-documents"))
        if source is None:
            source = DataSource(
                slug="fda-documents",
                kind="website",
                name="FDA documents",
                config={"dataset": "fda-documents", "public": True, "host": "fda.gov"},
            )
            session.add(source)
            await session.flush()
        return source

    async def _unique_version_name(
        self,
        session: AsyncSession,
        document_id: UUID,
        proposed: str,
        checksum: str,
    ) -> str:
        normalized = proposed[:128]
        collision = await session.scalar(
            select(DocumentVersion.id).where(
                DocumentVersion.document_id == document_id,
                DocumentVersion.version == normalized,
            )
        )
        if collision is None:
            return normalized
        return f"{normalized[:119]}-{checksum[:8]}"

    async def _mark_job_failed(self, job_id: UUID) -> None:
        async with self._session_factory() as session:
            job = await session.get(IngestionJob, job_id)
            if job is None or job.status == "completed":
                return
            job.status = "failed"
            job.error = {"code": "ingestion_failed"}
            job.completed_at = datetime.now(UTC)
            await session.commit()

    @staticmethod
    def _aggregate_embedding_status(statuses: list[str]) -> EmbeddingStatus:
        if not statuses or set(statuses) == {"skipped"}:
            return "skipped"
        if set(statuses) == {"completed"}:
            return "completed"
        if set(statuses) == {"failed"}:
            return "failed"
        return "partial"

    @staticmethod
    def _source_slug(dataset: str) -> str:
        return f"openfda-{dataset.replace('/', '-')}"

    @classmethod
    def _dataset_slug(cls, dataset: str) -> str:
        return dataset if dataset == "fda-documents" else cls._source_slug(dataset)

    @staticmethod
    def _external_key(record: dict[str, Any], checksum: str) -> str:
        for field in (
            "set_id",
            "application_number",
            "product_ndc",
            "safetyreportid",
            "recall_number",
            "event_id",
            "k_number",
            "pma_number",
            "submission_number",
            "unii",
            "id",
        ):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                return f"{field}:{value.strip()}"
            if isinstance(value, int):
                return f"{field}:{value}"
        return f"sha256:{checksum}"

    @staticmethod
    def _source_version(record: dict[str, Any], checksum: str) -> str:
        parts = [
            value.strip()
            for field in ("effective_time", "version", "id", "date_received", "report_date")
            if isinstance((value := record.get(field)), str) and value.strip()
        ]
        return "-".join(parts)[:128] if parts else checksum[:16]

    @staticmethod
    def _record_title(record: dict[str, Any], external_key: str) -> str:
        openfda = record.get("openfda")
        if isinstance(openfda, dict):
            for field in ("brand_name", "generic_name", "substance_name"):
                value = openfda.get(field)
                if isinstance(value, list) and value and isinstance(value[0], str):
                    return value[0][:500]
        for field in ("brand_name", "generic_name", "product_description", "sponsor_name"):
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
        return external_key[:500]

    @staticmethod
    def _evidence_chunk(
        chunk: DocumentChunk,
        version: DocumentVersion,
        document: Document,
        source: DataSource,
        score: float | None,
    ) -> EvidenceChunk:
        provenance = version.source_metadata.get("provenance")
        provenance_data = provenance if isinstance(provenance, dict) else {}
        dataset_value = source.config.get("dataset")
        source_url = provenance_data.get("source_url") or provenance_data.get("api_url")
        return EvidenceChunk(
            document_id=document.id,
            document_version_id=version.id,
            chunk_id=chunk.id,
            chunk_index=chunk.chunk_index,
            section_path=chunk.section_path,
            content=chunk.content,
            token_count=chunk.token_count,
            external_key=document.external_key,
            title=document.title,
            dataset=str(dataset_value or "openfda"),
            source_url=str(source_url) if isinstance(source_url, str) else None,
            dataset_last_updated=(
                str(provenance_data["dataset_last_updated"])
                if isinstance(provenance_data.get("dataset_last_updated"), str)
                else None
            ),
            score=score,
        )


@lru_cache
def get_fda_ingestion_coordinator() -> FDAIngestionCoordinator:
    settings = get_settings()
    embedder: EmbeddingProvider | None = None
    if settings.embedding_enabled:
        embedder = BGEONNXEmbeddingProvider(
            model_name=settings.embedding_model,
            model_revision=settings.embedding_model_revision,
            dimensions=settings.embedding_dimensions,
            cache_dir=settings.resolved_embedding_cache_dir,
            batch_size=settings.embedding_batch_size,
            segment_tokens=settings.embedding_segment_tokens,
            segment_overlap_tokens=settings.embedding_segment_overlap_tokens,
            query_prefix=settings.embedding_query_prefix,
        )
    return FDAIngestionCoordinator(
        storage=LocalObjectStorage(settings.resolved_storage_root),
        chunker=LlamaIndexJSONChunker(
            chunk_size=settings.ingestion_chunk_size_tokens,
            chunk_overlap=settings.ingestion_chunk_overlap_tokens,
        ),
        session_factory=async_session_factory,
        max_search_chunks=settings.ingestion_search_max_chunks,
        rrf_k=settings.retrieval_rrf_k,
        candidate_multiplier=settings.retrieval_candidate_multiplier,
        max_vector_distance=settings.retrieval_max_vector_distance,
        embedder=embedder,
        pdf_fetcher=DocumentFetcher(
            timeout_seconds=settings.fda_pdf_timeout_seconds,
            max_bytes=settings.fda_pdf_max_bytes,
            allowed_hosts=settings.document_allowed_hosts or registered_document_hosts(),
        ),
        pdf_extractor=PDFEvidenceExtractor(
            native_text_min_characters=settings.pdf_native_text_min_characters,
            ocr_dpi=settings.pdf_ocr_dpi,
        ),
    )


GENERIC_URL_SLUGS = frozenset({"download", "view", "index", "file", "pdf", "document", "media"})


def pdf_title(title: str | None, source_url: str, sections: list[dict[str, Any]]) -> str:
    """A reader-facing title for an ingested PDF.

    FDA serves many documents from URLs ending in ".../download", which previously became
    the title and then appeared in answers as a source named "download".
    """
    if title and title.strip():
        return " ".join(title.split())[:500]
    for section in sections[:3]:
        for line in str(section.get("content", "")).splitlines():
            cleaned = " ".join(line.split())
            if len(cleaned) >= 8 and not re.fullmatch(r"\[page \d+\]", cleaned, re.I):
                return cleaned[:200]
    slug = source_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    if slug and slug.casefold() not in GENERIC_URL_SLUGS:
        return slug[:200]
    return "FDA document"
