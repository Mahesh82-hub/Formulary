from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.db.session import async_session_factory
from app.fda.client import OpenFDAClient
from app.ingestion.chunking import LlamaIndexJSONChunker
from app.ingestion.pdf import DocumentFetcher, PDFEvidenceExtractor, validate_document_url
from app.ingestion.service import FDAIngestionCoordinator
from app.ingestion.storage import LocalObjectStorage
from app.models import DataSource, Document, DocumentChunk, DocumentVersion, IngestionJob


class FakeEmbeddingProvider:
    model_name = "test/bge-small"
    model_revision = "test-revision"
    dimensions = 384
    backend = "test"

    def segment_text(self, content: str) -> list[str]:
        return [content] if content.strip() else []

    def count_tokens(self, content: str) -> int:
        return max(1, len(content.split()))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)

    @staticmethod
    def _vector(_: str) -> list[float]:
        return [1.0, *([0.0] * 383)]


def test_local_object_storage_is_content_addressed_and_compressed(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    payload = {"results": [{"set_id": "label-1", "text": "metformin"}]}

    first = storage.put_json_gzip("raw/openfda/responses", payload)
    second = storage.put_json_gzip("raw/openfda/responses", payload)

    assert first == second
    assert first.uri.endswith(".json.gz")
    assert first.compressed_byte_size > 0
    assert first.character_count == len(
        '{"results":[{"set_id":"label-1","text":"metformin"}]}'
    )
    assert storage.read_json_gzip(first.uri) == payload
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1


def test_local_object_storage_preserves_binary_sources(tmp_path: Path) -> None:
    storage = LocalObjectStorage(tmp_path)
    payload = b"%PDF-1.4\nexample"

    first = storage.put_bytes("raw/fda/documents", payload, extension="pdf")
    second = storage.put_bytes("raw/fda/documents", payload, extension="pdf")

    assert first == second
    assert first.uri.endswith(".pdf")
    assert storage.read_bytes(first.uri) == payload
    assert len(list(tmp_path.rglob("*.pdf"))) == 1


def _single_page_text_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(payload)


def test_pdf_extractor_preserves_page_method_and_flags_possible_graph() -> None:
    extractor = PDFEvidenceExtractor(native_text_min_characters=10)
    payload = _single_page_text_pdf(
        "Plasma concentration-time profile: Cmax 123 ng/mL and AUC 456 ng h/mL"
    )

    result = extractor.extract(payload, ocr_mode="never")

    assert result.pages == 1
    assert result.native_text_pages == 1
    assert result.ocr_pages == 0
    assert result.graph_candidate_pages == [1]
    assert result.sections[0]["metadata"] == {
        "page_number": 1,
        "content_type": "page_text",
        "extraction_method": "native_text",
        "review_status": "source_extracted",
    }
    assert "Cmax 123 ng/mL" in result.sections[0]["content"]


@pytest.mark.asyncio
async def test_fda_pdf_fetcher_enforces_host_and_size() -> None:
    payload = _single_page_text_pdf("FDA pharmacokinetic evidence")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.fda.gov"
        return httpx.Response(200, content=payload, headers={"content-type": "application/pdf"})

    fetcher = DocumentFetcher(
        timeout_seconds=10,
        max_bytes=len(payload) + 1,
        allowed_hosts={"fda.gov"},
        transport=httpx.MockTransport(handler),
    )
    fetched, final_url = await fetcher.fetch("https://www.fda.gov/media/example/download")

    assert fetched == payload
    assert final_url == "https://www.fda.gov/media/example/download"
    with pytest.raises(ValueError, match="fda.gov"):
        validate_document_url("https://example.com/document.pdf", allowed_hosts={"fda.gov"})


def test_llamaindex_chunker_preserves_json_section_provenance() -> None:
    chunker = LlamaIndexJSONChunker(chunk_size=128, chunk_overlap=16)
    record = {
        "set_id": "label-1",
        "clinical_pharmacology": [
            "Metformin bioavailability is reported under fasting conditions. " * 80
        ],
        "openfda": {"generic_name": ["METFORMIN"], "route": ["ORAL"]},
    }

    chunks = chunker.chunk_record(
        record,
        dataset="drug/label",
        external_key="set_id:label-1",
    )

    assert len(chunks) > 3
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))
    assert {chunk.section_path for chunk in chunks} >= {
        "$.clinical_pharmacology",
        "$.openfda",
        "$.set_id",
    }
    assert all(chunk.token_count > 0 for chunk in chunks)
    assert all(chunk.metadata["dataset"] == "drug/label" for chunk in chunks)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_openfda_ingestion_deduplicates_and_retrieves_chunks(tmp_path: Path) -> None:
    set_id = f"ingestion-test-{uuid4()}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/drug/label.json"
        return httpx.Response(
            200,
            json={
                "meta": {
                    "last_updated": "2026-07-16",
                    "results": {"total": 1},
                },
                "results": [
                    {
                        "id": f"version-{set_id}",
                        "set_id": set_id,
                        "effective_time": "20260716",
                        "openfda": {
                            "generic_name": ["METFORMIN"],
                            "route": ["ORAL"],
                        },
                        "clinical_pharmacology": [
                            "Absolute bioavailability under fasting conditions is "
                            "50 to 60 percent. " * 80
                        ],
                        "dosage_and_administration": ["One 500 mg oral tablet."],
                    }
                ],
            },
        )

    fda = OpenFDAClient(
        api_key=None,
        base_url="https://api.fda.test",
        timeout_seconds=10,
        max_records=25,
        transport=httpx.MockTransport(handler),
    )
    coordinator = FDAIngestionCoordinator(
        storage=LocalObjectStorage(tmp_path),
        chunker=LlamaIndexJSONChunker(chunk_size=128, chunk_overlap=16),
        session_factory=async_session_factory,
        max_search_chunks=8,
        embedder=FakeEmbeddingProvider(),
    )
    job_ids: list[UUID] = []
    try:
        first = await coordinator.ingest_query(
            fda,
            "drug/label",
            search='openfda.generic_name.exact:"METFORMIN"',
            limit=1,
        )
        job_ids.append(first.job_id)
        second = await coordinator.ingest_query(
            fda,
            "drug/label",
            search='openfda.generic_name.exact:"METFORMIN"',
            limit=1,
        )
        job_ids.append(second.job_id)

        assert first.documents_created == 1
        assert first.versions_created == 1
        assert first.chunks_created > 3
        assert first.embedding_status == "completed"
        assert first.embedding_segments_created == first.chunks_created
        assert first.source_character_count > 0
        assert first.chunks_character_count > 0
        assert second.documents_created == 0
        assert second.versions_created == 0
        assert second.chunks_created == 0
        assert second.duplicates_skipped == 1
        assert second.embedding_status == "completed"
        assert second.documents[0].document_version_id == first.documents[0].document_version_id

        search_result = await coordinator.search_chunks(
            "bioavailability fasting",
            dataset="drug/label",
            external_key=f"set_id:{set_id}",
            limit=3,
        )
        assert search_result.returned > 0
        assert all(
            chunk.document_id == first.documents[0].document_id for chunk in search_result.chunks
        )
        assert search_result.chunks[0].source_url is not None

        semantic_result = await coordinator.search_chunks(
            "kidney removal",
            dataset="drug/label",
            external_key=f"set_id:{set_id}",
            limit=1,
        )
        assert semantic_result.returned == 1
        assert semantic_result.chunks[0].document_id == first.documents[0].document_id

        page = await coordinator.read_document_chunks(
            first.documents[0].document_id,
            limit=1,
        )
        assert page.returned == 1
        assert page.next_cursor == 1

        async with async_session_factory() as session:
            document = await session.scalar(
                select(Document).where(Document.external_key == f"set_id:{set_id}")
            )
            assert document is not None
            versions = await session.scalar(
                select(func.count(DocumentVersion.id)).where(
                    DocumentVersion.document_id == document.id
                )
            )
            chunks = await session.scalar(
                select(func.count(DocumentChunk.id))
                .join(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
            )
            assert versions == 1
            assert chunks == first.chunks_created
    finally:
        async with async_session_factory() as session:
            if job_ids:
                await session.execute(delete(IngestionJob).where(IngestionJob.id.in_(job_ids)))
            await session.execute(
                delete(Document).where(Document.external_key == f"set_id:{set_id}")
            )
            source = await session.scalar(
                select(DataSource).where(DataSource.slug == "openfda-drug-label")
            )
            if source is not None:
                remaining = await session.scalar(
                    select(func.count(Document.id)).where(Document.data_source_id == source.id)
                )
                if remaining == 0:
                    await session.delete(source)
            await session.commit()


@pytest.mark.asyncio
async def test_document_fetcher_accepts_hosts_from_any_registered_source() -> None:
    """A second source's documents must be fetchable without touching the pipeline."""
    payload = _single_page_text_pdf("PubMed Central article text")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-type": "application/pdf"})

    fetcher = DocumentFetcher(
        timeout_seconds=10,
        max_bytes=len(payload) + 1,
        allowed_hosts={"fda.gov", "ncbi.nlm.nih.gov"},
        transport=httpx.MockTransport(handler),
    )

    fetched, _ = await fetcher.fetch("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC1/pdf")
    assert fetched == payload


def test_document_url_validation_rejects_lookalike_and_unsafe_hosts() -> None:
    allowed = {"fda.gov"}

    # A suffix that merely ends with the allowed string must not pass.
    with pytest.raises(ValueError, match="fda.gov"):
        validate_document_url("https://notfda.gov/doc.pdf", allowed_hosts=allowed)
    # Plain HTTP is never acceptable.
    with pytest.raises(ValueError, match="fda.gov"):
        validate_document_url("http://www.fda.gov/doc.pdf", allowed_hosts=allowed)
    # Embedded credentials are an SSRF smell.
    with pytest.raises(ValueError, match="authority"):
        validate_document_url("https://user:pw@www.fda.gov/doc.pdf", allowed_hosts=allowed)
    # An empty allowlist fetches nothing rather than everything.
    with pytest.raises(ValueError, match="no configured host"):
        validate_document_url("https://www.fda.gov/doc.pdf", allowed_hosts=set())

    # Subdomains of an allowed host are fine.
    assert validate_document_url("https://www.fda.gov/doc.pdf", allowed_hosts=allowed)
