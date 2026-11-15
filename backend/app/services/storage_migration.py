import hashlib
import json
import os
import time
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.knowledge import KnowledgeDocument
from app.services.knowledge_storage import (
    DocumentStorageError,
    DocumentStorageNotFoundError,
    LocalFilesystemDocumentStorage,
    validate_storage_key,
)
from app.services.r2_storage import (
    CloudflareR2DocumentStorage,
    DocumentDestinationExistsError,
)

_HASH_CHUNK_BYTES = 64 * 1024


def _checksum(source) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while block := source.read(_HASH_CHUNK_BYTES):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _read_report(report_path: Path) -> dict[str, Any]:
    if not report_path.exists():
        return {"documents": {}}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Migration report is unreadable or invalid.") from exc
    if not isinstance(report, dict) or not isinstance(report.get("documents"), dict):
        raise ValueError("Migration report has an invalid structure.")
    return report


def _write_report(report_path: Path, report: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = report_path.with_name(f"{report_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, report_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _document_result(
    document: KnowledgeDocument,
    status: str,
    *,
    checksum: str | None = None,
    detail: str | None = None,
) -> dict[str, str]:
    result = {
        "workspace_id": str(document.workspace_id),
        "status": status,
    }
    if checksum:
        result["sha256"] = checksum
    if detail:
        result["detail"] = detail
    return result


def _verify_destination(
    r2_storage: CloudflareR2DocumentStorage,
    settings: Settings,
    document: KnowledgeDocument,
    source_checksum: str,
    source_size: int,
) -> bool:
    metadata = r2_storage.get_metadata(
        settings, document.workspace_id, document.storage_key
    )
    if metadata.size_bytes != source_size:
        return False
    with r2_storage.stream(
        settings, document.workspace_id, document.storage_key
    ) as destination:
        destination_checksum, destination_size = _checksum(destination)
    return (
        destination_size == source_size
        and destination_checksum == source_checksum
    )


def transfer_local_documents_to_r2(
    db: Session,
    settings: Settings,
    report_path: Path,
    *,
    dry_run: bool = True,
    verify_only: bool = False,
    local_storage: LocalFilesystemDocumentStorage | None = None,
    r2_storage: CloudflareR2DocumentStorage | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    if max_attempts < 1 or max_attempts > 5:
        raise ValueError("max_attempts must be between 1 and 5.")
    if dry_run and verify_only:
        raise ValueError("dry_run and verify_only cannot both be enabled.")

    source_storage = local_storage or LocalFilesystemDocumentStorage()
    destination_storage = r2_storage or CloudflareR2DocumentStorage()
    report = _read_report(report_path)
    records: dict[str, dict[str, str]] = report["documents"]
    documents = list(
        db.scalars(
            select(KnowledgeDocument).order_by(
                KnowledgeDocument.workspace_id, KnowledgeDocument.id
            )
        )
    )
    counts = {
        "discovered": len(documents),
        "copied": 0,
        "verified": 0,
        "skipped": 0,
        "planned": 0,
        "failed": 0,
    }

    for document in documents:
        document_id = str(document.id)
        source_snapshot: SpooledTemporaryFile | None = None
        try:
            validate_storage_key(document.storage_key)
            source_snapshot = SpooledTemporaryFile(
                max_size=1024 * 1024, mode="w+b"
            )
            digest = hashlib.sha256()
            source_size = 0
            try:
                with source_storage.open(
                    settings, document.workspace_id, document.storage_key
                ) as source:
                    while block := source.read(_HASH_CHUNK_BYTES):
                        source_snapshot.write(block)
                        digest.update(block)
                        source_size += len(block)
            except DocumentStorageNotFoundError:
                raise ValueError("source_missing") from None
            source_checksum = digest.hexdigest()
            if source_size != document.size_bytes:
                raise ValueError("source_size_mismatch")
            source_snapshot.seek(0)
            if not dry_run and destination_storage.exists(
                settings, document.workspace_id, document.storage_key
            ):
                if not _verify_destination(
                    destination_storage,
                    settings,
                    document,
                    source_checksum,
                    source_size,
                ):
                    raise ValueError("destination_checksum_mismatch")
                counts["verified"] += 1
                counts["skipped"] += 1
                records[document_id] = _document_result(
                    document, "verified_existing", checksum=source_checksum
                )
                _write_report(report_path, report)
                continue
            if verify_only:
                counts["failed"] += 1
                records[document_id] = _document_result(
                    document, "failed", detail="destination_missing"
                )
                _write_report(report_path, report)
                continue
            if dry_run:
                counts["planned"] += 1
                records[document_id] = _document_result(
                    document, "planned", checksum=source_checksum
                )
                _write_report(report_path, report)
                continue

            last_error: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    source_snapshot.seek(0)
                    destination_storage.put_existing(
                        source_snapshot,
                        settings,
                        document.workspace_id,
                        document.storage_key,
                        source_size,
                        document.media_type,
                    )
                    last_error = None
                    break
                except DocumentDestinationExistsError:
                    if _verify_destination(
                        destination_storage,
                        settings,
                        document,
                        source_checksum,
                        source_size,
                    ):
                        last_error = None
                        break
                    raise ValueError("destination_checksum_mismatch")
                except (DocumentStorageError, OSError) as exc:
                    last_error = exc
                    if attempt + 1 < max_attempts:
                        time.sleep(min(0.25 * (2**attempt), 2.0))
            if last_error is not None:
                raise last_error

            if not destination_storage.exists(
                settings, document.workspace_id, document.storage_key
            ) or not _verify_destination(
                destination_storage,
                settings,
                document,
                source_checksum,
                source_size,
            ):
                raise ValueError("destination_checksum_mismatch")

            counts["copied"] += 1
            counts["verified"] += 1
            records[document_id] = _document_result(
                document, "copied_verified", checksum=source_checksum
            )
        except DocumentStorageNotFoundError:
            counts["failed"] += 1
            records[document_id] = _document_result(
                document, "failed", detail="source_missing"
            )
        except ValueError as exc:
            counts["failed"] += 1
            detail = str(exc)
            if detail not in {
                "source_size_mismatch",
                "source_missing",
                "destination_checksum_mismatch",
                "Invalid document storage key.",
            }:
                detail = "invalid_document"
            records[document_id] = _document_result(
                document, "failed", detail=detail
            )
        except (DocumentStorageError, OSError):
            counts["failed"] += 1
            records[document_id] = _document_result(
                document, "failed", detail="storage_unavailable"
            )
        finally:
            if source_snapshot is not None:
                source_snapshot.close()
        _write_report(report_path, report)

    report["summary"] = counts
    report["mode"] = "verify" if verify_only else "dry-run" if dry_run else "apply"
    _write_report(report_path, report)
    return report
