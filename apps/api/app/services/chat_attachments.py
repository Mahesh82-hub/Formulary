from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from fastapi import UploadFile, status

from app.ingestion.pdf import PDFEvidenceExtractor


class ChatAttachmentError(ValueError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ChatPDFAttachmentProcessor:
    """Validate and extract a bounded PDF context block for a chat message."""

    def __init__(
        self,
        *,
        max_bytes: int,
        max_pages: int,
        max_extracted_characters: int,
        native_text_min_characters: int,
        ocr_dpi: int,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_pages = max_pages
        self._max_extracted_characters = max_extracted_characters
        self._extractor = PDFEvidenceExtractor(
            native_text_min_characters=native_text_min_characters,
            ocr_dpi=ocr_dpi,
        )

    async def process(self, upload: UploadFile) -> dict[str, Any]:
        filename = self._safe_filename(upload.filename)
        content_type = (upload.content_type or "").lower()
        if content_type not in {"", "application/pdf", "application/octet-stream"}:
            raise ChatAttachmentError(
                "Only PDF attachments are supported",
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            )

        try:
            payload = await upload.read(self._max_bytes + 1)
        finally:
            await upload.close()

        if len(payload) > self._max_bytes:
            raise ChatAttachmentError(
                f"PDF attachments must be smaller than {self._max_bytes // 1_000_000} MB",
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        if not payload.startswith(b"%PDF-"):
            raise ChatAttachmentError(
                "The selected file is not a valid PDF",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        try:
            extracted = await asyncio.to_thread(self._extractor.extract, payload, ocr_mode="auto")
        except Exception as error:
            raise ChatAttachmentError(
                "The PDF could not be read. It may be encrypted or damaged.",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            ) from error

        if extracted.pages > self._max_pages:
            raise ChatAttachmentError(
                f"PDF attachments are limited to {self._max_pages} pages",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        extracted_text = "\n\n".join(
            str(section.get("content", "")).strip()
            for section in extracted.sections
            if str(section.get("content", "")).strip()
        )
        if not extracted_text:
            raise ChatAttachmentError(
                "No readable text could be extracted from this PDF",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )

        full_character_count = len(extracted_text)
        truncated = full_character_count > self._max_extracted_characters
        warnings = list(extracted.warnings)
        if truncated:
            extracted_text = (
                extracted_text[: self._max_extracted_characters].rstrip()
                + "\n\n[Attachment text truncated at the configured chat limit.]"
            )
            warnings.append(
                "Only the first "
                f"{self._max_extracted_characters:,} extracted characters were included in chat."
            )

        return {
            "type": "document",
            "media_type": "application/pdf",
            "filename": filename,
            "byte_size": len(payload),
            "pages": extracted.pages,
            "native_text_pages": extracted.native_text_pages,
            "ocr_pages": extracted.ocr_pages,
            "tables": extracted.tables,
            "graph_candidate_pages": extracted.graph_candidate_pages,
            "warnings": warnings,
            "extracted_character_count": full_character_count,
            "included_character_count": len(extracted_text),
            "truncated": truncated,
            "extracted_text": extracted_text,
        }

    @staticmethod
    def _safe_filename(value: str | None) -> str:
        filename = Path(value or "document.pdf").name
        filename = re.sub(r"[\x00-\x1f\x7f]+", " ", filename).strip()[:200]
        if not filename:
            filename = "document.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        return filename
