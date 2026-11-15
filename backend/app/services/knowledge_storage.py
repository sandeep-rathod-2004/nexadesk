import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, ContextManager, Iterator, Protocol
from uuid import UUID, uuid4

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.core.config import Settings
from app.services.document_processing import (
    InvalidDocumentError,
    allowed_extension,
    safe_display_filename,
    validate_file_content,
)

_STORAGE_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}\.(?:pdf|docx|txt)$")
_UPLOAD_CHUNK_BYTES = 64 * 1024
_UPLOAD_SPOOL_MEMORY_BYTES = 1024 * 1024


class DocumentStorageError(RuntimeError):
    pass


class DocumentStorageNotFoundError(DocumentStorageError):
    pass


@dataclass(frozen=True)
class StoredDocument:
    storage_key: str
    extension: str
    size_bytes: int
    media_type: str
    filename: str


@dataclass(frozen=True)
class StoredDocumentMetadata:
    size_bytes: int
    media_type: str
    metadata: dict[str, str]


class DocumentStorage(Protocol):
    def put(
        self,
        file_object: BinaryIO,
        filename: str | None,
        settings: Settings,
        workspace_id: UUID,
    ) -> StoredDocument: ...

    def open(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> ContextManager[BinaryIO]: ...

    def stream(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> ContextManager[BinaryIO]: ...

    def delete(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> None: ...

    def exists(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> bool: ...

    def get_metadata(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> StoredDocumentMetadata: ...


def validate_storage_key(storage_key: str) -> None:
    if not _STORAGE_KEY_PATTERN.fullmatch(storage_key):
        raise ValueError("Invalid document storage key.")


@contextmanager
def _validated_upload(
    file_object: BinaryIO, filename: str | None, settings: Settings
) -> Iterator[tuple[SpooledTemporaryFile, str, int, str, str]]:
    extension = allowed_extension(filename)
    staged = SpooledTemporaryFile(
        max_size=_UPLOAD_SPOOL_MEMORY_BYTES, mode="w+b"
    )
    size = 0
    try:
        file_object.seek(0)
        while True:
            block = file_object.read(_UPLOAD_CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            if size > settings.knowledge_max_file_size_bytes:
                raise InvalidDocumentError(
                    f"File exceeds the {settings.knowledge_max_file_size_bytes}-byte upload limit.",
                    status_code=413,
                )
            staged.write(block)
        if size == 0:
            raise InvalidDocumentError("The uploaded file is empty.", status_code=422)
        staged.seek(0)
        media_type = validate_file_content(staged, extension)
        staged.seek(0)
        yield (
            staged,
            extension,
            size,
            media_type,
            safe_display_filename(filename),
        )
    finally:
        staged.close()


def _storage_root(settings: Settings) -> Path:
    configured = settings.knowledge_storage_path
    root = configured.expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def _local_document_path(settings: Settings, storage_key: str) -> Path:
    validate_storage_key(storage_key)
    root = _storage_root(settings)
    path = (root / storage_key).resolve()
    if path.parent != root:
        raise ValueError("Document path is outside the configured storage directory.")
    return path


class LocalFilesystemDocumentStorage:
    def put(
        self,
        file_object: BinaryIO,
        filename: str | None,
        settings: Settings,
        workspace_id: UUID,
    ) -> StoredDocument:
        del workspace_id
        with _validated_upload(file_object, filename, settings) as (
            staged,
            extension,
            size,
            media_type,
            safe_filename,
        ):
            root = _storage_root(settings)
            root.mkdir(parents=True, exist_ok=True)
            storage_key = f"{uuid4().hex}{extension}"
            destination = _local_document_path(settings, storage_key)
            temporary = root / f"{uuid4().hex}.part"
            try:
                staged.seek(0)
                with temporary.open("xb") as target:
                    while block := staged.read(_UPLOAD_CHUNK_BYTES):
                        target.write(block)
                os.replace(temporary, destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise
        return StoredDocument(
            storage_key=storage_key,
            extension=extension,
            size_bytes=size,
            media_type=media_type,
            filename=safe_filename,
        )

    @contextmanager
    def open(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> Iterator[BinaryIO]:
        del workspace_id
        path = _local_document_path(settings, storage_key)
        try:
            with path.open("rb") as source:
                yield source
        except FileNotFoundError as exc:
            raise DocumentStorageNotFoundError(
                "The stored document is unavailable."
            ) from exc
        except OSError as exc:
            raise DocumentStorageError(
                "Document storage is unavailable."
            ) from None

    @contextmanager
    def stream(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> Iterator[BinaryIO]:
        with self.open(settings, workspace_id, storage_key) as source:
            yield source

    def delete(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> None:
        del workspace_id
        try:
            _local_document_path(settings, storage_key).unlink(missing_ok=True)
        except OSError:
            raise DocumentStorageError(
                "Document storage is unavailable."
            ) from None

    def exists(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> bool:
        del workspace_id
        try:
            return _local_document_path(settings, storage_key).is_file()
        except OSError:
            raise DocumentStorageError(
                "Document storage is unavailable."
            ) from None

    def get_metadata(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> StoredDocumentMetadata:
        del workspace_id
        path = _local_document_path(settings, storage_key)
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise DocumentStorageNotFoundError(
                "The stored document is unavailable."
            ) from exc
        except OSError:
            raise DocumentStorageError(
                "Document storage is unavailable."
            ) from None
        media_type = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".txt": "text/plain",
        }[path.suffix.lower()]
        return StoredDocumentMetadata(stat.st_size, media_type, {})


def get_document_storage(settings: Settings) -> DocumentStorage:
    return get_document_storage_for_backend(
        settings, settings.document_storage_backend
    )


def get_document_storage_for_backend(
    settings: Settings, backend: str
) -> DocumentStorage:
    if backend == "local":
        return LocalFilesystemDocumentStorage()
    if backend == "r2":
        from app.services.r2_storage import CloudflareR2DocumentStorage

        return CloudflareR2DocumentStorage()
    raise ValueError("Unsupported document storage backend.")


async def store_upload(
    upload: UploadFile, settings: Settings, workspace_id: UUID
) -> StoredDocument:
    return await run_in_threadpool(
        get_document_storage(settings).put,
        upload.file,
        upload.filename,
        settings,
        workspace_id,
    )


def remove_document_file(
    settings: Settings, workspace_id: UUID, storage_key: str
) -> None:
    get_document_storage(settings).delete(settings, workspace_id, storage_key)


def document_exists(
    settings: Settings, workspace_id: UUID, storage_key: str
) -> bool:
    return get_document_storage(settings).exists(settings, workspace_id, storage_key)


@contextmanager
def open_document(
    settings: Settings,
    workspace_id: UUID,
    storage_key: str,
    backend: str | None = None,
) -> Iterator[BinaryIO]:
    storage = (
        get_document_storage(settings)
        if backend is None
        else get_document_storage_for_backend(settings, backend)
    )
    with storage.open(
        settings, workspace_id, storage_key
    ) as source:
        yield source


@contextmanager
def stream_document(
    settings: Settings,
    workspace_id: UUID,
    storage_key: str,
    backend: str | None = None,
) -> Iterator[BinaryIO]:
    storage = (
        get_document_storage(settings)
        if backend is None
        else get_document_storage_for_backend(settings, backend)
    )
    with storage.stream(
        settings, workspace_id, storage_key
    ) as source:
        yield source
