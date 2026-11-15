import logging
from contextlib import contextmanager
from functools import lru_cache
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Iterator
from uuid import UUID, uuid4

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config as BotoConfig
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.services.knowledge_storage import (
    DocumentStorageError,
    DocumentStorageNotFoundError,
    StoredDocument,
    StoredDocumentMetadata,
    _UPLOAD_CHUNK_BYTES,
    _validated_upload,
    validate_storage_key,
)

logger = logging.getLogger(__name__)
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=8 * 1024 * 1024,
    max_concurrency=2,
    use_threads=True,
)


class DocumentDestinationExistsError(DocumentStorageError):
    pass


@lru_cache(maxsize=8)
def _client(
    endpoint: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    connect_timeout: float,
    read_timeout: float,
):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"mode": "standard", "max_attempts": 3},
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        ),
    )


def _get_client(settings: Settings):
    if settings.r2_secret_access_key is None:
        raise DocumentStorageError("R2 credentials are not configured.")
    if not settings.r2_bucket_name:
        raise DocumentStorageError("R2 bucket is not configured.")
    return _client(
        settings.configured_r2_endpoint,
        settings.r2_region,
        settings.r2_access_key_id or "",
        settings.r2_secret_access_key.get_secret_value(),
        settings.r2_connect_timeout_seconds,
        settings.r2_read_timeout_seconds,
    )


def _object_key(workspace_id: UUID, storage_key: str) -> str:
    validate_storage_key(storage_key)
    return f"workspaces/{workspace_id}/documents/{storage_key}"


def _is_not_found(exc: ClientError) -> bool:
    response = exc.response
    code = response.get("Error", {}).get("Code")
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound"} or status_code == 404


def _storage_error(operation: str, exc: Exception) -> DocumentStorageError:
    logger.warning("R2 document storage operation failed: %s", operation)
    return DocumentStorageError("Document storage is unavailable.")


class CloudflareR2DocumentStorage:
    def put_existing(
        self,
        file_object: BinaryIO,
        settings: Settings,
        workspace_id: UUID,
        storage_key: str,
        size_bytes: int,
        media_type: str,
    ) -> None:
        key = _object_key(workspace_id, storage_key)
        try:
            _get_client(settings).put_object(
                Bucket=settings.r2_bucket_name,
                Key=key,
                Body=file_object,
                ContentLength=size_bytes,
                ContentType=media_type,
                Metadata={"workspace-id": str(workspace_id)},
                IfNoneMatch="*",
            )
        except ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get(
                "HTTPStatusCode"
            )
            if status_code in {409, 412}:
                raise DocumentDestinationExistsError(
                    "The destination object already exists."
                ) from None
            raise _storage_error("migration upload", exc) from None
        except (BotoCoreError, OSError) as exc:
            raise _storage_error("migration upload", exc) from None

    def put(
        self,
        file_object: BinaryIO,
        filename: str | None,
        settings: Settings,
        workspace_id: UUID,
    ) -> StoredDocument:
        with _validated_upload(file_object, filename, settings) as (
            staged,
            extension,
            size,
            media_type,
            safe_filename,
        ):
            storage_key = f"{uuid4().hex}{extension}"
            key = _object_key(workspace_id, storage_key)
            try:
                _get_client(settings).upload_fileobj(
                    staged,
                    settings.r2_bucket_name,
                    key,
                    ExtraArgs={
                        "ContentType": media_type,
                        "Metadata": {"workspace-id": str(workspace_id)},
                    },
                    Config=_TRANSFER_CONFIG,
                )
            except (BotoCoreError, ClientError, S3UploadFailedError, OSError) as exc:
                raise _storage_error("upload", exc) from None
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
        key = _object_key(workspace_id, storage_key)
        staged = SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        try:
            try:
                response = _get_client(settings).get_object(
                    Bucket=settings.r2_bucket_name, Key=key
                )
                body = response["Body"]
                try:
                    while block := body.read(_UPLOAD_CHUNK_BYTES):
                        staged.write(block)
                finally:
                    body.close()
            except ClientError as exc:
                if _is_not_found(exc):
                    raise DocumentStorageNotFoundError(
                        "The stored document is unavailable."
                    ) from None
                raise _storage_error("download", exc) from None
            except (BotoCoreError, OSError, KeyError) as exc:
                raise _storage_error("download", exc) from None
            staged.seek(0)
            yield staged
        finally:
            staged.close()

    @contextmanager
    def stream(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> Iterator[BinaryIO]:
        key = _object_key(workspace_id, storage_key)
        try:
            response = _get_client(settings).get_object(
                Bucket=settings.r2_bucket_name, Key=key
            )
            body = response["Body"]
        except ClientError as exc:
            if _is_not_found(exc):
                raise DocumentStorageNotFoundError(
                    "The stored document is unavailable."
                ) from None
            raise _storage_error("stream download", exc) from None
        except (BotoCoreError, OSError, KeyError) as exc:
            raise _storage_error("stream download", exc) from None
        try:
            yield body
        except (BotoCoreError, ClientError, OSError) as exc:
            raise _storage_error("stream download", exc) from None
        finally:
            body.close()

    def delete(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> None:
        key = _object_key(workspace_id, storage_key)
        try:
            _get_client(settings).delete_object(
                Bucket=settings.r2_bucket_name, Key=key
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            raise _storage_error("delete", exc) from None

    def exists(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> bool:
        key = _object_key(workspace_id, storage_key)
        try:
            _get_client(settings).head_object(
                Bucket=settings.r2_bucket_name, Key=key
            )
            return True
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise _storage_error("existence check", exc) from None
        except (BotoCoreError, OSError) as exc:
            raise _storage_error("existence check", exc) from None

    def get_metadata(
        self, settings: Settings, workspace_id: UUID, storage_key: str
    ) -> StoredDocumentMetadata:
        key = _object_key(workspace_id, storage_key)
        try:
            response = _get_client(settings).head_object(
                Bucket=settings.r2_bucket_name, Key=key
            )
        except ClientError as exc:
            if _is_not_found(exc):
                raise DocumentStorageNotFoundError(
                    "The stored document is unavailable."
                ) from None
            raise _storage_error("metadata lookup", exc) from None
        except (BotoCoreError, OSError) as exc:
            raise _storage_error("metadata lookup", exc) from None
        metadata = response.get("Metadata", {})
        return StoredDocumentMetadata(
            size_bytes=int(response.get("ContentLength", 0)),
            media_type=response.get("ContentType", "application/octet-stream"),
            metadata={str(name): str(value) for name, value in metadata.items()},
        )
