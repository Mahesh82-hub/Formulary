import hashlib
import json
from typing import Any

from llama_index.core import Document as LlamaDocument
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.utils import get_tokenizer

from app.ingestion.models import PreparedChunk


class LlamaIndexJSONChunker:
    """Turn top-level JSON sections into bounded LlamaIndex text nodes."""

    def __init__(self, *, chunk_size: int, chunk_overlap: int) -> None:
        if chunk_size < 128:
            raise ValueError("chunk_size must be at least 128 tokens")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be non-negative and smaller than chunk_size")
        self._pipeline = IngestionPipeline(
            transformations=[SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)]
        )
        self._tokenizer = get_tokenizer()

    def chunk_record(
        self,
        record: dict[str, Any],
        *,
        dataset: str,
        external_key: str,
    ) -> list[PreparedChunk]:
        documents = [
            LlamaDocument(
                text=f"[{field}]\n{rendered}",
                metadata={
                    "dataset": dataset,
                    "external_key": external_key,
                    "section_path": f"$.{field}",
                    "source_field": field,
                },
                excluded_llm_metadata_keys=[
                    "dataset",
                    "external_key",
                    "section_path",
                    "source_field",
                ],
                excluded_embed_metadata_keys=[
                    "dataset",
                    "external_key",
                    "section_path",
                    "source_field",
                ],
            )
            for field, value in sorted(record.items())
            if (rendered := self._render(value))
        ]
        if not documents:
            documents = [
                LlamaDocument(
                    text="[record]\n{}",
                    metadata={
                        "dataset": dataset,
                        "external_key": external_key,
                        "section_path": "$",
                        "source_field": "record",
                    },
                )
            ]

        nodes = self._pipeline.run(documents=documents, show_progress=False)
        chunks: list[PreparedChunk] = []
        for chunk_index, node in enumerate(nodes):
            content = node.get_content().strip()
            if not content:
                continue
            chunks.append(
                PreparedChunk(
                    chunk_index=chunk_index,
                    section_path=str(node.metadata.get("section_path", "$")),
                    content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    token_count=max(1, len(self._tokenizer(content))),
                    metadata=dict(node.metadata),
                )
            )
        return chunks

    def chunk_sections(
        self,
        sections: list[dict[str, Any]],
        *,
        dataset: str,
        external_key: str,
    ) -> list[PreparedChunk]:
        """Chunk already-extracted document sections while retaining page/table provenance."""
        documents: list[LlamaDocument] = []
        for section in sections:
            content = str(section.get("content", "")).strip()
            if not content:
                continue
            section_path = str(section.get("section_path", "$"))
            metadata = {
                "dataset": dataset,
                "external_key": external_key,
                "section_path": section_path,
                **dict(section.get("metadata") or {}),
            }
            documents.append(
                LlamaDocument(
                    text=content,
                    metadata=metadata,
                    excluded_llm_metadata_keys=list(metadata),
                    excluded_embed_metadata_keys=list(metadata),
                )
            )
        nodes = self._pipeline.run(documents=documents, show_progress=False) if documents else []
        chunks: list[PreparedChunk] = []
        for chunk_index, node in enumerate(nodes):
            content = node.get_content().strip()
            if not content:
                continue
            chunks.append(
                PreparedChunk(
                    chunk_index=chunk_index,
                    section_path=str(node.metadata.get("section_path", "$")),
                    content=content,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    token_count=max(1, len(self._tokenizer(content))),
                    metadata=dict(node.metadata),
                )
            )
        return chunks

    @staticmethod
    def _render(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int | float):
            return str(value)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return "\n\n".join(item.strip() for item in value if item.strip())
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
