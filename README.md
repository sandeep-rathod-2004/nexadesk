# NexaDesk — Universal AI Chatbot Platform

A local-first development starter for a reusable AI chatbot platform.

## Included
- Polished React + TypeScript + Vite chat workspace
- FastAPI backend with selectable AI-provider and document-storage interfaces; Ollama, Gemini Developer API and local filesystem implementations are included
- SQLAlchemy persistence with SQLite by default, PostgreSQL support, and Alembic migrations
- Registration, login, short-lived JWT access tokens, rotating HttpOnly refresh cookies, and workspace owner/member roles
- Workspace- and owner-scoped conversations with persisted user/assistant messages
- Workspace-isolated PDF, DOCX and TXT uploads with extraction, chunking and Ollama or Gemini embeddings
- Semantic retrieval and document-grounded chat with chunk-linked citations and persisted evidence
- Progressive AI-provider token streaming with Stop Generation and explicitly interrupted message persistence
- Knowledge Base UI for upload progress, processing status and authorized document deletion
- Configurable model, timeout, database and CORS settings
- Health/readiness endpoints and backend tests
- Architecture and security roadmap

This is a development prototype, not a production-certified system. It does not guarantee zero hallucinations. Authentication and workspace isolation are implemented, including refresh-token rotation and revocation, but the platform still lacks rate limiting and production monitoring. Uploaded files are stored locally without encryption at rest. Retrieval currently ranks workspace chunks in application memory and is intended for local development, not large production corpora. Local generation cancellation uses an in-process coordinator; multi-instance deployments require Redis. Use synthetic data only and keep services bound to localhost.

## Requirements
Python 3.12+, Node.js 20+, Ollama.

## Ollama model storage
Check that your configured chat model and `nomic-embed-text` are already installed before downloading anything:
```powershell
ollama list
```

If you do need another model and your C: drive has limited free space, configure Ollama model storage on D: first. For a one-session terminal server:
```powershell
$env:OLLAMA_MODELS = "D:\ollama\models"
ollama serve
```

## Backend
```powershell
cd D:\universal-ai-chatbot\universal-ai-chatbot\backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Set `JWT_SECRET_KEY` in `.env` to a generated secret. Generate one with:
```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```
In development, an omitted key is generated randomly for the process, so tokens become invalid after a restart. Production mode requires a configured secret.

Run database migrations before starting the API:
```powershell
alembic upgrade head
uvicorn app.main:app --reload
```
API: http://127.0.0.1:8000 | Docs: http://127.0.0.1:8000/docs

The default SQLite database is `backend/nexadesk.db`. Set `DATABASE_URL` to use another SQLAlchemy-supported database. Apply subsequent schema changes with `alembic upgrade head`. Downgrades are refused when they would discard unfinished cleanup tasks, document storage-backend mappings, or replay audit records. Preserve or safely resolve the affected data before retrying; completed cleanup tasks may be discarded by revision `0006` because they record confirmed object deletion.

Registration creates a user, workspace, and owner membership. A workspace owner can add existing registered users as members through `POST /api/v1/workspaces/{workspace_id}/members`. Authenticated conversation routes support listing, creating, retrieving, renaming (`PATCH /api/v1/conversations/{conversation_id}`), deleting (`DELETE /api/v1/conversations/{conversation_id}`), and listing messages. Conversation access requires both membership in the conversation workspace and ownership by the authenticated user.

The authenticated Knowledge Base API uses `GET` and `POST /api/v1/workspaces/{workspace_id}/documents` and `DELETE /api/v1/workspaces/{workspace_id}/documents/{document_id}`. Uploads accept PDF, DOCX and UTF-8 TXT files up to 10 MiB by default. The server verifies workspace membership, stores each file under a generated storage key, extracts text, creates overlapping chunks, and requests embeddings from the configured Ollama embedding model. Document status and processing errors are returned and persisted. Workspace members can retrieve documents; deletion is limited to the uploader or a workspace owner. A successful delete returns `202 Accepted`: the database record is removed and storage cleanup is durably queued, but the response does not claim that the stored object has already been deleted.

Run the storage cleanup worker in a separate backend process after applying migrations:
```powershell
python -m app.workers.storage_cleanup
```
The worker continuously processes due cleanup tasks; `python -m app.workers.storage_cleanup --once` processes at most one task and exits. By default, a task gets five attempts, each claim has a 120-second lease, and transient failures use capped exponential backoff with jitter (up to one hour). Permanently failed tasks remain in `storage_cleanup_tasks` for operator review. The worker must use the same database and storage configuration/credentials as the API; it selects the storage adapter from each task's recorded backend and never lists the bucket.

Administrators can replay a permanently failed task with `POST /api/v1/admin/storage-cleanup-tasks/{task_id}/replay`. Replay accepts only the task UUID, uses the task's stored workspace/backend/key, and records the operator and previous retry state in `storage_cleanup_replay_audits`; a second replay is rejected unless a later worker run has failed permanently again. The `is_admin` flag defaults to false and must be granted by an authorized operator through a trusted administrative database process; the API exposes no privilege-grant endpoint. Never promote an account based only on a user-supplied request.

Knowledge documents record their storage backend when uploaded, and reads and cleanup use that recorded backend rather than a later global setting change. Revision `0007_document_storage_backend` leaves existing documents' backend unset because it cannot be inferred safely from the current configuration. Those legacy documents return `409` for download or deletion until an operator verifies their actual storage location and backfills the per-document backend. Do not bulk-assign the current backend unless that has been independently verified for every affected document.

When a workspace has ready documents, chat embeds the question and retrieves the most similar chunks from that workspace only. Duplicate chunk text in the same document is collapsed. Answers are instructed to treat excerpts as untrusted data, cite each supported factual claim with its exact chunk label, and identify unsupported parts. Only cited, workspace-validated chunks are returned as expandable evidence; an explicit abstention has no attached sources. These prompt and citation checks improve traceability but cannot prove semantic entailment or prevent every hallucination. If no chunk meets `KNOWLEDGE_MIN_SIMILARITY`, chat abstains instead of answering from unrelated sources. With no ready documents, the existing general chat path is unchanged. The current JSON vector store is a simple exact cosine scan; large deployments should move to a dedicated vector database and add ingestion queues, OCR, encryption at rest, rate limits and monitoring.

Ollama connection and read timeouts are configured separately by `OLLAMA_CONNECT_TIMEOUT_SECONDS` (10 seconds) and `OLLAMA_READ_TIMEOUT_SECONDS` (600 seconds); `OLLAMA_TIMEOUT_SECONDS` continues to set write timeouts. Increase the read timeout when first-time model loading takes longer. Stream performance logs correlate request receipt, history loading, embedding, retrieval, prompt construction and size, model first token, and server token emission by request ID; prompt text is not logged. Cold model loading can still delay the first request; startup warm-up is not enabled. A stopped stream is persisted with an `interrupted` message status and can be retried with the same request ID without duplicating its message pair.

### Distributed generation cancellation

The default `GENERATION_COORDINATION_BACKEND=local` preserves single-process local development and does not coordinate across workers. For multi-instance operation, set `GENERATION_COORDINATION_BACKEND=redis` and backend-only `REDIS_URL`. Production settings reject the local backend and require Redis; application startup also fails if Redis cannot be reached rather than silently falling back. `GENERATION_LEASE_SECONDS` controls the expiring owner lease and `GENERATION_COORDINATION_POLL_SECONDS` controls owner-side shared-state checks. Cancellation uses a Redis Pub/Sub wake-up plus authoritative shared state, so missed notifications are detected by polling. Redis stores only generation, user, workspace and owner identifiers plus lifecycle status; prompts, messages, documents and credentials are not published or stored there.

For local testing with Docker Desktop, start an isolated Redis service without a persistent volume:
```powershell
docker run --name nexadesk-redis --publish 127.0.0.1:6379:6379 --detach redis:7-alpine
```
Set `GENERATION_COORDINATION_BACKEND=redis` and `REDIS_URL=redis://127.0.0.1:6379/0` in the backend `.env`, then restart the API. Stop only that test container when finished with `docker stop nexadesk-redis` (remove it only if you no longer need it). For production, use a managed/private Redis endpoint and keep its URL and credentials in the backend secret manager. Redis must be shared by all API instances; the database must also be shared. A Redis restart or coordination loss cancels a currently running generation safely, while leases expire if an owner disappears.

`DATABASE_URL` supports SQLite locally and PostgreSQL through the `psycopg` 3 driver. Plain `postgresql://` and legacy `postgres://` URLs are normalized to `postgresql+psycopg://`. The backend uses a bounded connection pool for PostgreSQL and keeps SQLite's foreign-key and thread settings for local development.

`AI_PROVIDER` and `EMBEDDING_PROVIDER` independently accept `ollama` or `gemini`. Ollama remains the local default. To test Gemini inference, obtain a Gemini Developer API key and configure the backend environment with `AI_PROVIDER=gemini`, `GEMINI_API_KEY`, and optionally `GEMINI_MODEL` (default `gemini-2.5-flash`). Gemini embeddings are independently selected with `EMBEDDING_PROVIDER=gemini`, `GEMINI_EMBEDDING_MODEL` (default `gemini-embedding-001`), and `GEMINI_EMBEDDING_DIMENSIONS` (default 768). `EMBEDDING_MODEL`, when set, overrides the provider's default embedding model. Configure only the backend; these variables must never use a frontend `VITE_*` prefix. No provider is selected as a fallback if the configured provider fails. Provider timeouts and bounded retries are controlled by `GEMINI_TIMEOUT_SECONDS` and `GEMINI_MAX_RETRIES`.

The Gemini Developer API free tier may use submitted content to improve Google's products; do not send confidential customer data, private knowledge-base documents, credentials, or other sensitive material using a free-tier key. Check Google's current terms and data-use settings before controlled testing. Free-tier quotas are limited and may change; this project does not enable billing or promise free/unlimited inference. Use synthetic, non-confidential prompts for an optional smoke test. The browser never receives the Gemini API key.

For a controlled local smoke test, configure the variables only in `backend/.env`, use a fresh test workspace and synthetic content, and restart FastAPI. For chat-only validation without Ollama, set `AI_PROVIDER=gemini`. To use Gemini for embeddings too, also set `EMBEDDING_PROVIDER=gemini`, run the intended database migration with `alembic upgrade head`, then upload only a synthetic TXT file and ask a question answerable from it. Do not copy customer material into this test. Never place the API key in frontend `.env` files, source code, command history, issue reports, or logs. Disable/delete the test key when finished. An API request can consume quota and may have cost depending on the Google project; this application does not enable billing.

Gemini embeddings use retrieval task types and a configured output dimension. Each newly ingested document records embedding provider, model and vector dimension; RAG rejects known mismatches rather than comparing incompatible vectors. The migration leaves these fields unset for older indexed documents because their original model cannot be inferred safely. Existing legacy documents remain usable with Ollama for backward compatibility, but must be re-uploaded/re-indexed before switching their workspace to Gemini embeddings. Re-index documents whenever changing the embedding provider, model or dimensions. Changing `AI_PROVIDER` alone does not require re-indexing if `EMBEDDING_PROVIDER` and the embedding model remain unchanged.

`DOCUMENT_STORAGE_BACKEND` accepts `local` (default) or `r2`. Local files remain under `KNOWLEDGE_STORAGE_PATH`. For R2, configure either `R2_ACCOUNT_ID` or `R2_ENDPOINT_URL`, plus `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, and `R2_SECRET_ACCESS_KEY`; `R2_REGION` defaults to `auto`. The application uses the private S3-compatible API, not public URLs. Each object key is generated by the backend beneath a workspace-specific prefix; workspace membership and the document record are checked in the database on upload, download, and deletion. R2 errors do not fall back to local storage. `R2_CONNECT_TIMEOUT_SECONDS` and `R2_READ_TIMEOUT_SECONDS` configure client timeouts. Create/configure the bucket and scoped credentials outside the application, keep credentials only in the backend secret manager or `backend/.env`, and never use frontend `VITE_*` variables for secrets. Switching an existing installation from `local` to `r2` does not copy old files: transfer existing document objects to the workspace-prefixed R2 keys before switching, or those records will have missing objects. There is no automatic copy or fallback. R2 has not been live-tested as part of this milestone.

Knowledge Base configuration is in `backend/.env.example`: `OLLAMA_EMBEDDING_MODEL`, `EMBEDDING_MODEL`, `DOCUMENT_STORAGE_BACKEND`, `KNOWLEDGE_STORAGE_PATH`, `R2_ACCOUNT_ID`, `R2_ENDPOINT_URL`, `R2_BUCKET_NAME`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_REGION`, `R2_CONNECT_TIMEOUT_SECONDS`, `R2_READ_TIMEOUT_SECONDS`, `KNOWLEDGE_MAX_FILE_SIZE_BYTES`, `KNOWLEDGE_MAX_PDF_PAGES`, `KNOWLEDGE_CHUNK_SIZE`, `KNOWLEDGE_CHUNK_OVERLAP`, `KNOWLEDGE_RETRIEVAL_COUNT`, and `KNOWLEDGE_MIN_SIMILARITY`. No database schema change is needed for R2: the DB continues storing an opaque generated storage key, while the adapter derives the workspace-prefixed object key. Apply any other schema changes only to the intended database with `alembic upgrade head`; local uploads default to `backend/data/knowledge`.

### Local-to-R2 document transfer

The administrative command reads document records from PostgreSQL, resolves each source through the filesystem adapter, and copies to the R2 workspace-prefixed key without changing document rows, chunks, or embeddings. It writes per-document status and SHA-256 checkpoints to a local JSON report. It never removes local files. Dry-run is the default and does not contact R2:

```powershell
cd D:\universal-ai-chatbot\universal-ai-chatbot\backend
.\.venv\Scripts\python.exe -m scripts.migrate_local_documents_to_r2 --report-file .\data\local-to-r2-report.json
```

Before applying, ensure `DATABASE_URL` points to the intended staging PostgreSQL database and R2 settings are present in the backend environment, while leaving `DOCUMENT_STORAGE_BACKEND=local`. Apply requires the exact database name as an additional confirmation; inject database and storage credentials via a secret manager rather than putting them in command arguments:

```powershell
.\.venv\Scripts\python.exe -m scripts.migrate_local_documents_to_r2 --apply --confirm-database <staging-database-name> --report-file .\data\local-to-r2-report.json
.\.venv\Scripts\python.exe -m scripts.migrate_local_documents_to_r2 --verify-only --confirm-database <staging-database-name> --report-file .\data\local-to-r2-report.json
```

The report summary separates discovered, copied, verified, skipped, planned, and failed documents. Rerunning apply resumes by rechecking each destination; matching objects are checksum-verified and skipped, while an existing mismatched object is reported as failed and never overwritten. Object creation uses a conditional write to prevent races from replacing an object. Exit status is nonzero if any record fails verification. Review the report and require `failed: 0` plus all expected documents verified before changing `DOCUMENT_STORAGE_BACKEND` to `r2`. For rollback, switch back to `local`; original files and database metadata remain unchanged. Do not remove the R2 copy until the cloud deployment and recovery process are independently verified.

For live staging validation, use a private, empty/test R2 bucket and a synthetic TXT document only. First run the transfer command in `--verify-only` mode after a controlled apply. Then select `DOCUMENT_STORAGE_BACKEND=r2` in staging, upload the synthetic file, confirm it is listed and can be downloaded by an authorized workspace member, verify a user from another workspace receives a denial/not-found response, exercise a question whose answer is in that file and confirm its source citation, then delete the test record and confirm the R2 object is gone. Check the bucket configuration out of band to ensure public access is disabled. Never use confidential documents. These live checks require operator-supplied R2 credentials; no credentials or bucket are created by this application.

## Frontend (second terminal)
```powershell
cd D:\universal-ai-chatbot\universal-ai-chatbot\frontend
npm install
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
npm run dev
```
Open http://localhost:5173.

## Tests
```powershell
cd D:\universal-ai-chatbot\universal-ai-chatbot\backend
.\.venv\Scripts\Activate.ps1
pytest
```

Frontend tests:
```powershell
cd D:\universal-ai-chatbot\universal-ai-chatbot\frontend
npm test
```

### PostgreSQL integration tests

The PostgreSQL integration suite only accepts a loopback `psycopg` URL for the dedicated `nexadesk_m2_test` database. It skips when `TEST_POSTGRESQL_URL` is unset. Use a disposable PostgreSQL 16 container with generated test-only credentials and no mounted volumes; never point this at a normal or production database. From PowerShell in `backend/`:

```powershell
$containerName = "nexadesk-m2-pg-" + [guid]::NewGuid().ToString("N").Substring(0, 10)
$testPassword = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
docker run --rm --name $containerName `
  --env POSTGRES_USER=nexadesk_test `
  --env "POSTGRES_PASSWORD=$testPassword" `
  --env POSTGRES_DB=nexadesk_m2_test `
  --publish "127.0.0.1::5432" --detach postgres:16-alpine
$portMapping = docker port $containerName 5432/tcp
$port = [int](($portMapping -split ":")[-1])
$env:DATABASE_URL = "postgresql+psycopg://nexadesk_test:$testPassword@127.0.0.1:$port/nexadesk_m2_test"
$env:TEST_POSTGRESQL_URL = $env:DATABASE_URL
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
  docker exec $containerName pg_isready -q -h 127.0.0.1 -U nexadesk_test -d nexadesk_m2_test
  if ($LASTEXITCODE -eq 0) { $ready = $true; break }
  Start-Sleep -Seconds 2
}
if (-not $ready) { throw "Disposable PostgreSQL did not become ready." }
alembic upgrade head
Remove-Item Env:DATABASE_URL
pytest -q tests\test_postgresql_integration.py
```

Run the complete backend suite while `TEST_POSTGRESQL_URL` remains set to include the PostgreSQL tests alongside the SQLite-backed tests. Stop only the container created above with `docker stop $containerName`. The command does not mount or reference an existing volume; the PostgreSQL image uses a container-owned anonymous data volume that `--rm` removes with this disposable container. If a command fails, still stop that exact container.

## Next milestones
The PostgreSQL 16 integration tests require `TEST_POSTGRESQL_URL`; they were skipped in this local run. The local backend suite currently has six failures in migration downgrade tests, so validate the migrations against disposable databases before staging. Before deployment, verify the target database includes the refresh-token schema and review cookie, CORS, and secret settings for the intended domains. Cloud deployment still needs a cloud AI adapter, cloud document storage, large-corpus vector storage and ingestion queues, prompt-injection and citation evaluations, output policy checks, rate limiting, audit logging, observability, shared cancellation coordination, and deployment hardening.
