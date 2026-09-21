import gzip
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.ingestion.models import StoredObject

SAFE_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")


class LocalObjectStorage:
    """Content-addressed, gzip-compressed JSON storage behind a replaceable boundary."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def put_json_gzip(self, namespace: str, payload: Any) -> StoredObject:
        if not SAFE_NAMESPACE.fullmatch(namespace) or ".." in namespace.split("/"):
            raise ValueError("Invalid storage namespace")
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        raw = rendered.encode("utf-8")
        checksum = hashlib.sha256(raw).hexdigest()
        relative_path = Path(namespace) / checksum[:2] / f"{checksum}.json.gz"
        destination = self._safe_path(relative_path)
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{checksum}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(compressed)
                temporary_path = Path(temporary.name)
            try:
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)
        return StoredObject(
            uri=f"local://{relative_path.as_posix()}",
            checksum=checksum,
            byte_size=len(raw),
            compressed_byte_size=len(compressed),
            character_count=len(rendered),
        )

    def put_bytes(
        self,
        namespace: str,
        payload: bytes,
        *,
        extension: str,
    ) -> StoredObject:
        """Store immutable binary source material without changing its bytes."""
        if not SAFE_NAMESPACE.fullmatch(namespace) or ".." in namespace.split("/"):
            raise ValueError("Invalid storage namespace")
        normalized_extension = extension.lower().lstrip(".")
        if not re.fullmatch(r"[a-z0-9]{1,12}", normalized_extension):
            raise ValueError("Invalid storage extension")
        checksum = hashlib.sha256(payload).hexdigest()
        relative_path = Path(namespace) / checksum[:2] / f"{checksum}.{normalized_extension}"
        destination = self._safe_path(relative_path)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{checksum}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(payload)
                temporary_path = Path(temporary.name)
            try:
                os.replace(temporary_path, destination)
            finally:
                temporary_path.unlink(missing_ok=True)
        return StoredObject(
            uri=f"local://{relative_path.as_posix()}",
            checksum=checksum,
            byte_size=len(payload),
            compressed_byte_size=len(payload),
            character_count=None,
        )

    def read_bytes(self, uri: str) -> bytes:
        prefix = "local://"
        if not uri.startswith(prefix):
            raise ValueError("Unsupported object URI")
        path = self._safe_path(Path(uri.removeprefix(prefix)))
        return path.read_bytes()

    def read_json_gzip(self, uri: str) -> Any:
        prefix = "local://"
        if not uri.startswith(prefix):
            raise ValueError("Unsupported object URI")
        path = self._safe_path(Path(uri.removeprefix(prefix)))
        with gzip.open(path, "rt", encoding="utf-8") as source:
            return json.load(source)

    def _safe_path(self, relative_path: Path) -> Path:
        destination = (self._root / relative_path).resolve()
        if not destination.is_relative_to(self._root):
            raise ValueError("Object path escapes the storage root")
        return destination
