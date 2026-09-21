from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import numpy as np
from huggingface_hub import hf_hub_download

if TYPE_CHECKING:
    import onnxruntime as ort  # type: ignore[import-untyped]
    from tokenizers import Tokenizer


class EmbeddingProvider(Protocol):
    model_name: str
    model_revision: str
    dimensions: int
    backend: str

    def segment_text(self, content: str) -> list[str]: ...

    def count_tokens(self, content: str) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, query: str) -> list[float]: ...


class BGEONNXEmbeddingProvider:
    """Local BGE-small inference using the model's ONNX export on CPU."""

    backend = "onnxruntime"

    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str,
        dimensions: int,
        cache_dir: Path,
        batch_size: int,
        segment_tokens: int,
        segment_overlap_tokens: int,
        query_prefix: str,
    ) -> None:
        if segment_overlap_tokens >= segment_tokens:
            raise ValueError("Embedding segment overlap must be smaller than segment size")
        self.model_name = model_name
        self.model_revision = model_revision
        self.dimensions = dimensions
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._segment_tokens = segment_tokens
        self._segment_overlap_tokens = segment_overlap_tokens
        self._query_prefix = query_prefix
        self._lock = threading.RLock()
        self._tokenizer: Tokenizer | None = None
        self._session: ort.InferenceSession | None = None

    def preload(self) -> None:
        self._ensure_loaded()

    def segment_text(self, content: str) -> list[str]:
        tokenizer, _ = self._ensure_loaded()
        with self._lock:
            tokenizer.no_truncation()
            token_ids = tokenizer.encode(content, add_special_tokens=False).ids
            self._configure_tokenizer(tokenizer)
        if not token_ids:
            return []
        step = self._segment_tokens - self._segment_overlap_tokens
        segments: list[str] = []
        for start in range(0, len(token_ids), step):
            selected = token_ids[start : start + self._segment_tokens]
            if not selected:
                break
            rendered = tokenizer.decode(selected, skip_special_tokens=True).strip()
            if rendered:
                segments.append(rendered)
            if start + self._segment_tokens >= len(token_ids):
                break
        return segments

    def count_tokens(self, content: str) -> int:
        tokenizer, _ = self._ensure_loaded()
        with self._lock:
            tokenizer.no_truncation()
            count = len(tokenizer.encode(content, add_special_tokens=False).ids)
            self._configure_tokenizer(tokenizer)
        return count

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, query: str) -> list[float]:
        return self._embed([f"{self._query_prefix}{query}"])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        tokenizer, session = self._ensure_loaded()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            with self._lock:
                encoded = tokenizer.encode_batch(batch)
                expected_inputs = {model_input.name for model_input in session.get_inputs()}
                feeds: dict[str, np.ndarray] = {
                    "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
                    "attention_mask": np.asarray(
                        [item.attention_mask for item in encoded], dtype=np.int64
                    ),
                }
                if "token_type_ids" in expected_inputs:
                    feeds["token_type_ids"] = np.asarray(
                        [item.type_ids for item in encoded], dtype=np.int64
                    )
                output = np.asarray(session.run(None, feeds)[0], dtype=np.float32)
            pooled = output[:, 0, :] if output.ndim == 3 else output
            if pooled.shape[1] != self.dimensions:
                raise RuntimeError(
                    f"Embedding model returned {pooled.shape[1]} dimensions; "
                    f"expected {self.dimensions}"
                )
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            normalized = pooled / np.maximum(norms, np.finfo(np.float32).eps)
            vectors.extend(normalized.tolist())
        return vectors

    def _ensure_loaded(self) -> tuple[Tokenizer, ort.InferenceSession]:
        with self._lock:
            if self._tokenizer is not None and self._session is not None:
                return self._tokenizer, self._session
            import onnxruntime as ort
            from tokenizers import Tokenizer

            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tokenizer_path = hf_hub_download(
                repo_id=self.model_name,
                filename="tokenizer.json",
                revision=self.model_revision,
                cache_dir=self._cache_dir,
            )
            model_path = hf_hub_download(
                repo_id=self.model_name,
                filename="onnx/model.onnx",
                revision=self.model_revision,
                cache_dir=self._cache_dir,
            )
            tokenizer = Tokenizer.from_file(tokenizer_path)
            self._configure_tokenizer(tokenizer)
            session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            self._tokenizer = tokenizer
            self._session = session
            return tokenizer, session

    @staticmethod
    def _configure_tokenizer(tokenizer: Tokenizer) -> None:
        pad_id = tokenizer.token_to_id("[PAD]")
        tokenizer.enable_truncation(max_length=512)
        tokenizer.enable_padding(pad_id=pad_id or 0, pad_token="[PAD]")
