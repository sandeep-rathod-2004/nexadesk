import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import models  # noqa: F401
from app.api.deps import get_db
from app.api.routes.auth import router as auth_router
from app.api.routes import knowledge as knowledge_routes
from app.core.config import Settings
from app.core.security import create_access_token, decode_access_token
from app.db.base import Base
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument
from app.services.embeddings import embed_texts
from app.services.knowledge_storage import DocumentStorageError
from app.services.retrieval import (
    build_rag_context,
    retrieve_chunks,
    validate_citations,
)
from app.services.r2_storage import CloudflareR2DocumentStorage

EXPECTED_ENDPOINT = "http://127.0.0.1:9000"
EXPECTED_BUCKET = "nexadesk-documents"
DOCUMENT_CONTENT = (
    "Synthetic Silo lifecycle policy: the blue lantern project code is SILO-7391. "
    "This test-only document contains no customer information."
)
RETRIEVAL_QUERY = "What is the blue lantern project code?"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _test_settings() -> Settings:
    return Settings(
        _env_file=BACKEND_DIR / ".env",
        app_env="development",
        database_url="sqlite:///:memory:",
        document_storage_backend="r2",
        r2_endpoint_url=EXPECTED_ENDPOINT,
        r2_bucket_name=EXPECTED_BUCKET,
    )


def main() -> int:
    try:
        active_settings = Settings(
            _env_file=BACKEND_DIR / ".env",
            app_env="development",
            database_url="sqlite:///:memory:",
        )
    except Exception:
        print(
            "FAIL: backend settings could not be loaded; values were not displayed."
        )
        return 1

    if active_settings.document_storage_backend != "local":
        print(
            "FAIL: refusing to run; backend/.env must keep "
            "DOCUMENT_STORAGE_BACKEND=local."
        )
        return 1

    try:
        settings = _test_settings()
    except Exception:
        print(
            "FAIL: backend settings are invalid or required credentials are missing; "
            "values were not displayed."
        )
        return 1

    if settings.configured_r2_endpoint != EXPECTED_ENDPOINT:
        print("FAIL: refusing to run; configured Silo endpoint did not match.")
        return 1
    if settings.r2_bucket_name != EXPECTED_BUCKET:
        print("FAIL: refusing to run; configured bucket did not match.")
        return 1
    if (
        not settings.r2_access_key_id
        or not settings.r2_access_key_id.strip()
        or settings.r2_secret_access_key is None
        or not settings.r2_secret_access_key.get_secret_value().strip()
    ):
        print("FAIL: Silo credentials are not configured; values were not displayed.")
        return 1

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)

    application = FastAPI()
    application.include_router(auth_router, prefix="/api/v1")
    application.include_router(knowledge_routes.router, prefix="/api/v1")

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    application.dependency_overrides[get_db] = override_get_db
    workspace_id: UUID | None = None
    document_id: UUID | None = None
    uploaded_keys: list[tuple[UUID, str]] = []
    upload_attempted = False
    checks_passed = False
    cleanup_passed = False

    storage = CloudflareR2DocumentStorage()
    original_put = CloudflareR2DocumentStorage.put

    def capture_successful_upload(
        adapter,
        file_object,
        filename,
        upload_settings,
        upload_workspace_id,
    ):
        result = original_put(
            adapter,
            file_object,
            filename,
            upload_settings,
            upload_workspace_id,
        )
        uploaded_keys.append((upload_workspace_id, result.storage_key))
        return result

    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(knowledge_routes, "get_settings", return_value=settings)
            )
            stack.enter_context(
                patch("app.core.security.get_settings", return_value=settings)
            )
            stack.enter_context(
                patch.object(
                    CloudflareR2DocumentStorage,
                    "put",
                    capture_successful_upload,
                )
            )
            with TestClient(application, raise_server_exceptions=False) as client:
                registration = client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": f"silo-lifecycle-{uuid4().hex}@example.com",
                        "password": "synthetic-test-password-7391",
                        "workspace_name": "Synthetic Silo lifecycle workspace",
                    },
                )
                _require(registration.status_code == 201, "Synthetic registration failed.")
                auth_payload = registration.json()
                workspace_id = UUID(auth_payload["workspaces"][0]["id"])
                token = auth_payload["access_token"]
                headers = {"Authorization": f"Bearer {token}"}

                upload_attempted = True
                uploaded = client.post(
                    f"/api/v1/workspaces/{workspace_id}/documents",
                    headers=headers,
                    files={
                        "file": (
                            "silo-lifecycle-synthetic.txt",
                            DOCUMENT_CONTENT.encode("utf-8"),
                            "text/plain",
                        )
                    },
                )
                _require(uploaded.status_code == 201, "Document upload API failed.")
                upload_payload = uploaded.json()
                document_id = UUID(upload_payload["id"])
                _require(upload_payload["status"] == "ready", "Document did not become ready.")
                _require(
                    len(uploaded_keys) == 1
                    and uploaded_keys[0][0] == workspace_id,
                    "The adapter did not return this upload's storage key.",
                )
                storage_key = uploaded_keys[0][1]
                _require(
                    upload_payload["workspace_id"] == str(workspace_id),
                    "Uploaded document workspace did not match.",
                )
                print("PASS: authenticated upload created a ready workspace document.")

                with session_factory() as db:
                    document = db.get(KnowledgeDocument, document_id)
                    _require(document is not None, "Document metadata was not persisted.")
                    _require(
                        document.workspace_id == workspace_id
                        and document.storage_key == storage_key
                        and document.embedding_provider == settings.embedding_provider
                        and document.embedding_model == settings.configured_embedding_model,
                        "Document workspace, storage, or embedding identity did not match.",
                    )
                    chunks = list(
                        db.scalars(
                            select(KnowledgeChunk)
                            .where(KnowledgeChunk.document_id == document_id)
                            .order_by(KnowledgeChunk.sequence)
                        )
                    )
                    _require(bool(chunks), "No document chunks were persisted.")
                    _require(
                        all(chunk.workspace_id == workspace_id for chunk in chunks),
                        "A chunk has an unexpected workspace.",
                    )
                    _require(
                        all(chunk.embedding for chunk in chunks),
                        "A chunk has no generated embedding.",
                    )
                    _require(
                        any(
                            "SILO-7391" in chunk.content
                            and "Text file" in chunk.source_refs
                            for chunk in chunks
                        ),
                        "Extracted text or TXT source reference was not persisted.",
                    )
                print("PASS: extracted text, chunks, embeddings, and workspace persisted.")

                query_task = (
                    "RETRIEVAL_QUERY"
                    if settings.embedding_provider == "gemini"
                    else None
                )
                query_vectors = asyncio.run(
                    embed_texts(
                        settings,
                        [RETRIEVAL_QUERY],
                        **({"task_type": query_task} if query_task else {}),
                    )
                )
                _require(
                    len(query_vectors) == 1 and bool(query_vectors[0]),
                    "Configured embedding provider returned no query vector.",
                )

                with session_factory() as db:
                    retrieved = retrieve_chunks(
                        db,
                        workspace_id,
                        query_vectors[0],
                        limit=settings.knowledge_retrieval_count,
                        min_similarity=settings.knowledge_min_similarity,
                        embedding_provider=settings.embedding_provider,
                        embedding_model=settings.configured_embedding_model,
                    )
                    _require(
                        any(
                            item.document_id == document_id
                            and "SILO-7391" in item.content
                            for item in retrieved
                        ),
                        "Real retrieval did not return the synthetic policy fact.",
                    )
                    _require(
                        any("Text file" in item.source_refs for item in retrieved),
                        "Retrieved source reference was missing.",
                    )
                    _context, sources = build_rag_context(retrieved)
                    selected_source = next(
                        source
                        for source in sources
                        if source["document_id"] == document_id
                    )
                    reply = (
                        f"The blue lantern project code is SILO-7391 "
                        f"{selected_source['citation_label']}."
                    )
                    cited_reply, cited_sources = validate_citations(
                        db, workspace_id, reply, sources
                    )
                    _require(
                        selected_source["citation_label"] in cited_reply
                        and any(
                            source["chunk_id"] == selected_source["chunk_id"]
                            for source in cited_sources
                        ),
                        "Citation validation did not preserve the authorized source.",
                    )
                print("PASS: provider-backed retrieval and workspace-validated citation.")

                download = client.get(
                    f"/api/v1/workspaces/{workspace_id}/documents/"
                    f"{document_id}/download",
                    headers=headers,
                )
                _require(
                    download.status_code == 200
                    and download.content == DOCUMENT_CONTENT.encode("utf-8"),
                    "Authorized download did not match the synthetic document.",
                )
                print("PASS: authorized download content matches.")

                deletion = client.delete(
                    f"/api/v1/workspaces/{workspace_id}/documents/{document_id}",
                    headers=headers,
                )
                _require(deletion.status_code == 204, "Document delete API failed.")
                with session_factory() as db:
                    _require(
                        db.get(KnowledgeDocument, document_id) is None,
                        "Document record remains after delete.",
                    )
                    remaining_chunks = list(
                        db.scalars(
                            select(KnowledgeChunk).where(
                                KnowledgeChunk.document_id == document_id
                            )
                        )
                    )
                    _require(not remaining_chunks, "Document chunks remain after delete.")
                _require(
                    not storage.exists(settings, workspace_id, storage_key),
                    "Silo object remains after API deletion.",
                )
                print("PASS: API deletion removed document, chunks, and Silo object.")
                checks_passed = True
    except Exception:
        print("FAIL: knowledge lifecycle integration failed; details were suppressed.")
    finally:
        if uploaded_keys:
            cleanup_workspace_id, cleanup_storage_key = uploaded_keys[0]
            try:
                if storage.exists(settings, cleanup_workspace_id, cleanup_storage_key):
                    storage.delete(settings, cleanup_workspace_id, cleanup_storage_key)
                if storage.exists(settings, cleanup_workspace_id, cleanup_storage_key):
                    raise DocumentStorageError("Synthetic object remains after cleanup.")
                cleanup_passed = True
                print("PASS: cleanup verified this test's Silo object is absent.")
            except Exception:
                print("FAIL: cleanup could not be verified; no other keys were touched.")
        elif upload_attempted:
            print(
                "WARNING: upload returned no storage key; safe object cleanup "
                "cannot be confirmed."
            )
        engine.dispose()

    return 0 if checks_passed and cleanup_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
