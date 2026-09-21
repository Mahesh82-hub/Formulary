from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

OCRMode = Literal["auto", "never", "always"]
ALLOWED_FDA_HOSTS = {"fda.gov"}
GRAPH_TERMS = re.compile(
    r"(?:plasma|serum|blood)\s+concentration|concentration[- ]time|pharmacokinetic\s+profile",
    re.IGNORECASE,
)


def validate_fda_pdf_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not any(
        host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_FDA_HOSTS
    ):
        raise ValueError("Only HTTPS documents hosted on fda.gov are allowed")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("FDA document URL contains unsupported authority information")
    return url


class FDAPDFFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._transport = transport

    async def fetch(self, url: str) -> tuple[bytes, str]:
        validate_fda_pdf_url(url)
        async with httpx.AsyncClient(
            timeout=self._timeout_seconds,
            follow_redirects=True,
            transport=self._transport,
        ) as client, client.stream(
            "GET", url, headers={"Accept": "application/pdf"}
        ) as response:
            response.raise_for_status()
            final_url = str(response.url)
            validate_fda_pdf_url(final_url)
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > self._max_bytes:
                raise ValueError("FDA PDF exceeds the configured download limit")
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self._max_bytes:
                    raise ValueError("FDA PDF exceeds the configured download limit")
                chunks.append(chunk)
        payload = b"".join(chunks)
        if not payload.startswith(b"%PDF-"):
            raise ValueError("The FDA URL did not return a PDF document")
        return payload, final_url


@dataclass(slots=True)
class ExtractedPDF:
    sections: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    native_text_pages: int = 0
    ocr_pages: int = 0
    tables: int = 0
    graph_candidate_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PDFEvidenceExtractor:
    """Extract native text/tables first and OCR only pages that need it."""

    def __init__(self, *, native_text_min_characters: int = 80, ocr_dpi: int = 200) -> None:
        self._native_text_min_characters = native_text_min_characters
        self._ocr_dpi = ocr_dpi

    def extract(self, payload: bytes, *, ocr_mode: OCRMode = "auto") -> ExtractedPDF:
        import pdfplumber

        result = ExtractedPDF()
        ocr_unavailable_reported = False
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            result.pages = len(pdf.pages)
            for page_number, page in enumerate(pdf.pages, start=1):
                native_text = (page.extract_text() or "").strip()
                tables = page.extract_tables() or []
                if native_text:
                    result.native_text_pages += 1
                should_ocr = ocr_mode == "always" or (
                    ocr_mode == "auto" and len(native_text) < self._native_text_min_characters
                )
                ocr_text = ""
                if should_ocr:
                    try:
                        ocr_text = self._ocr_page(page)
                    except (ImportError, RuntimeError) as exc:
                        if not ocr_unavailable_reported:
                            result.warnings.append(str(exc))
                            ocr_unavailable_reported = True
                if ocr_text:
                    result.ocr_pages += 1

                extraction_method = self._method(native_text, ocr_text)
                text = native_text
                if ocr_text and (ocr_mode == "always" or not native_text):
                    text = ocr_text
                elif ocr_text and ocr_text not in native_text:
                    text = f"{native_text}\n\n[OCR supplement]\n{ocr_text}".strip()
                if text:
                    result.sections.append(
                        {
                            "section_path": f"$.pages[{page_number}].text",
                            "content": f"[Page {page_number}]\n{text}",
                            "metadata": {
                                "page_number": page_number,
                                "content_type": "page_text",
                                "extraction_method": extraction_method,
                                "review_status": (
                                    "requires_review" if ocr_text else "source_extracted"
                                ),
                            },
                        }
                    )

                for table_number, table in enumerate(tables, start=1):
                    rendered = self._render_table(table)
                    if not rendered:
                        continue
                    result.tables += 1
                    result.sections.append(
                        {
                            "section_path": (
                                f"$.pages[{page_number}].tables[{table_number}]"
                            ),
                            "content": (
                                f"[Page {page_number}, Table {table_number}]\n{rendered}"
                            ),
                            "metadata": {
                                "page_number": page_number,
                                "table_number": table_number,
                                "content_type": "table",
                                "extraction_method": "native_table",
                                "review_status": "source_extracted",
                            },
                        }
                    )

                curves = len(getattr(page, "curves", []) or [])
                images = len(getattr(page, "images", []) or [])
                if GRAPH_TERMS.search(text) or images > 0 or curves >= 8:
                    result.graph_candidate_pages.append(page_number)
        if result.graph_candidate_pages:
            result.warnings.append(
                "Possible graph pages were retained for review; OCR does not digitize curve "
                "coordinates or establish numerical accuracy."
            )
        return result

    def _ocr_page(self, page: Any) -> str:
        try:
            import pytesseract  # type: ignore[import-untyped]
            from pytesseract import TesseractNotFoundError
        except ImportError as exc:
            raise ImportError("OCR Python support is not installed") from exc
        try:
            image = page.to_image(resolution=self._ocr_dpi).original
            return str(pytesseract.image_to_string(image)).strip()
        except TesseractNotFoundError as exc:
            raise RuntimeError(
                "OCR was required, but the Tesseract executable is not installed; run "
                "`brew install tesseract` and retry."
            ) from exc

    @staticmethod
    def _method(native_text: str, ocr_text: str) -> str:
        if native_text and ocr_text:
            return "native_text+ocr"
        if ocr_text:
            return "ocr"
        if native_text:
            return "native_text"
        return "none"

    @staticmethod
    def _render_table(table: list[list[Any]]) -> str:
        rows: list[str] = []
        for row in table:
            cells = [" ".join(str(cell or "").split()).replace("|", "\\|") for cell in row]
            if any(cells):
                rows.append(" | ".join(cells))
        return "\n".join(rows)
