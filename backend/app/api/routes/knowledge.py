import logging
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import CurrentUser, DbSession
from app.core.config import get_settings
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.models.storage_cleanup import StorageCleanupTask
from app.schemas.knowledge import KnowledgeDocumentResponse
from app.services.document_processing import (
    DocumentProcessingError,
    InvalidDocumentError,
    chunk_sections,
    extract_document,
)
from app.services.embeddings import (
    EmbeddingUnavailableError,
    embed_texts,
)
from app.services.knowledge_storage import (
    DocumentStorageError,
    DocumentStorageNotFoundError,
    get_document_storage,
    get_document_storage_for_backend,
    open_document,
    stream_document,
    store_upload,
    validate_storage_key,
)
from app.services.workspaces import require_workspace_member
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)
router = APIRouter(tags=["knowledge"])


@router.get(
    "/workspaces/{workspace_id}/documents",
    response_model=list[KnowledgeDocumentResponse],
)
def list_documents(
    workspace_id: UUID, user: CurrentUser, db: DbSession
) -> list[KnowledgeDocument]:
    require_workspace_member(db, workspace_id, user)
    return list(
        db.scalars(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.workspace_id == workspace_id)
            .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id)
        )
    )


@router.post(
    "/workspaces/{workspace_id}/documents",
    response_model=KnowledgeDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    workspace_id: UUID,
    user: CurrentUser,
    db: DbSession,
    file: UploadFile = File(...),
) -> KnowledgeDocument:
    require_workspace_member(db, workspace_id, user)
    settings = get_settings()
    try:
        stored = await store_upload(file, settings, workspace_id)
    except InvalidDocumentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Knowledge document could not be stored.")
        raise HTTPException(
            status_code=500, detail="Document storage is unavailable."
        ) from exc
    except DocumentStorageError as exc:
        raise HTTPException(
            status_code=503, detail="Document storage is unavailable."
        ) from exc
    finally:
        await file.close()

    document = KnowledgeDocument(
        workspace_id=workspace_id,
        uploaded_by_id=user.id,
        filename=stored.filename,
        storage_key=stored.storage_key,
        storage_backend=settings.document_storage_backend,
        media_type=stored.media_type,
        size_bytes=stored.size_bytes,
        status="processing",
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.configured_embedding_model,
    )
    try:
        db.add(document)
        db.commit()
        db.refresh(document)
    except SQLAlchemyError as exc:
        db.rollback()
        try:
            get_document_storage(settings).delete(
                settings, workspace_id, stored.storage_key
            )
        except (DocumentStorageError, OSError):
            logger.error("Uploaded document cleanup failed after metadata error.")
        raise HTTPException(
            status_code=500, detail="Document metadata could not be saved."
        ) from exc

    try:
        def extract_stored_document():
            with open_document(
                settings,
                workspace_id,
                stored.storage_key,
                document.storage_backend,
            ) as source:
                return extract_document(
                    source, stored.extension, settings.knowledge_max_pdf_pages
                )

        sections = await run_in_threadpool(extract_stored_document)
        chunks = await run_in_threadpool(
            chunk_sections,
            sections,
            settings.knowledge_chunk_size,
            settings.knowledge_chunk_overlap,
        )
        vectors: list[list[float]] = []
        for offset in range(0, len(chunks), 16):
            task_type = (
                "RETRIEVAL_DOCUMENT"
                if settings.embedding_provider == "gemini"
                else None
            )
            vectors.extend(
                await embed_texts(
                    settings,
                    [chunk.content for chunk in chunks[offset : offset + 16]],
                    **({"task_type": task_type} if task_type is not None else {}),
                )
            )
        if len(vectors) != len(chunks):
            raise EmbeddingUnavailableError("The embedding service returned an incomplete result.")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) > 1:
            raise EmbeddingUnavailableError(
                "The embedding provider returned incompatible vector dimensions."
            )
        document.embedding_dimensions = next(iter(dimensions), None)
        db.add_all(
            [
                KnowledgeChunk(
                    document_id=document.id,
                    workspace_id=workspace_id,
                    sequence=sequence,
                    content=chunk.content,
                    source_refs=chunk.source_refs,
                    embedding=vector,
                )
                for sequence, (chunk, vector) in enumerate(zip(chunks, vectors))
            ]
        )
        document.status = "ready"
        document.error_message = None
        db.commit()
    except (
        DocumentProcessingError,
        DocumentStorageError,
        EmbeddingUnavailableError,
    ) as exc:
        db.rollback()
        document = db.get(KnowledgeDocument, document.id)
        if document is None:
            raise HTTPException(
                status_code=500, detail="Document processing state could not be saved."
            ) from exc
        document.status = "failed"
        if isinstance(exc, DocumentProcessingError):
            document.error_message = str(exc)
        elif isinstance(exc, DocumentStorageError):
            document.error_message = "The stored document could not be read."
        elif settings.embedding_provider == "ollama":
            document.error_message = (
                "The local embedding model is unavailable. Check Ollama and the "
                "configured embedding model."
            )
        else:
            document.error_message = (
                "The configured embedding provider is unavailable. Check its "
                "backend configuration."
            )
        db.commit()
    db.refresh(document)
    return document


@router.get(
    "/workspaces/{workspace_id}/documents/{document_id}/download",
    response_class=StreamingResponse,
)
def download_document(
    workspace_id: UUID,
    document_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> StreamingResponse:
    require_workspace_member(db, workspace_id, user)
    document = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.workspace_id == workspace_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if document.storage_backend not in {"local", "r2"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "The document storage location must be verified before it can be "
                "downloaded."
            ),
        )

    settings = get_settings()
    storage = get_document_storage_for_backend(settings, document.storage_backend)
    try:
        if not storage.exists(settings, workspace_id, document.storage_key):
            raise HTTPException(status_code=404, detail="Document not found.")
        stored_metadata = storage.get_metadata(
            settings, workspace_id, document.storage_key
        )
    except DocumentStorageNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Document not found.") from exc
    except DocumentStorageError as exc:
        raise HTTPException(
            status_code=503, detail="Document storage is unavailable."
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="Document storage is unavailable."
        ) from exc

    def content():
        with stream_document(
            settings,
            workspace_id,
            document.storage_key,
            document.storage_backend,
        ) as source:
            while block := source.read(64 * 1024):
                yield block

    return StreamingResponse(
        content(),
        media_type=stored_metadata.media_type,
        headers={
            "Content-Length": str(stored_metadata.size_bytes),
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(document.filename)}"
            ),
            "Cache-Control": "private, no-store",
        },
    )


@router.delete(
    "/workspaces/{workspace_id}/documents/{document_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def delete_document(
    workspace_id: UUID,
    document_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, str]:
    membership = require_workspace_member(db, workspace_id, user)
    document = db.scalar(
        select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id,
            KnowledgeDocument.workspace_id == workspace_id,
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if document.uploaded_by_id != user.id and membership.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only the uploader or a workspace owner can delete this document.",
        )
    if document.storage_backend not in {"local", "r2"}:
        raise HTTPException(
            status_code=409,
            detail=(
                "The document storage location must be verified before it can be "
                "deleted."
            ),
        )
    settings = get_settings()
    try:
        validate_storage_key(document.storage_key)
        backend = document.storage_backend
        cleanup_task = db.scalar(
            select(StorageCleanupTask).where(
                StorageCleanupTask.workspace_id == workspace_id,
                StorageCleanupTask.document_id == document.id,
                StorageCleanupTask.storage_key == document.storage_key,
                StorageCleanupTask.status != "completed",
            )
        )
        if cleanup_task is None:
            cleanup_task = StorageCleanupTask(
                workspace_id=workspace_id,
                document_id=document.id,
                storage_backend=backend,
                storage_key=document.storage_key,
                status="pending",
                attempt_count=0,
            )
            db.add(cleanup_task)
        elif cleanup_task.status == "permanently_failed":
            cleanup_task.status = "pending"
            cleanup_task.attempt_count = 0
            cleanup_task.next_attempt_at = func.now()
            cleanup_task.last_error_code = None
            cleanup_task.lease_token = None
            cleanup_task.lease_expires_at = None
            cleanup_task.completed_at = None
        db.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.document_id == document.id,
                KnowledgeChunk.workspace_id == workspace_id,
            )
        )
        db.delete(document)
        db.commit()
        return {
            "status": "deleted",
            "storage_cleanup": "pending",
        }
    except (SQLAlchemyError, ValueError) as exc:
        db.rollback()
        logger.error("Document deletion could not be scheduled.")
        raise HTTPException(
            status_code=500,
            detail="The document could not be deleted.",
        ) from exc
