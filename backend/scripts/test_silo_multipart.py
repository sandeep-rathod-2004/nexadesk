import hashlib
import sys
from io import BytesIO
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings
from app.services.r2_storage import CloudflareR2DocumentStorage

EXPECTED_BUCKET = "nexadesk-documents"
EXPECTED_ENDPOINT = "http://127.0.0.1:9000"
EXPECTED_SIZE_BYTES = 9 * 1024 * 1024
EXPECTED_MEDIA_TYPE = "text/plain"
CONTENT_PATTERN = b"NexaDesk synthetic multipart upload test content.\n"
READ_CHUNK_BYTES = 64 * 1024


def _sha256_stream(source: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while block := source.read(READ_CHUNK_BYTES):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def _synthetic_content() -> bytes:
    repetitions = (EXPECTED_SIZE_BYTES + len(CONTENT_PATTERN) - 1) // len(
        CONTENT_PATTERN
    )
    return (CONTENT_PATTERN * repetitions)[:EXPECTED_SIZE_BYTES]


def main() -> int:
    try:
        settings = Settings(_env_file=BACKEND_DIR / ".env")
    except Exception:
        print("FAIL: backend settings could not be loaded; values were not displayed.")
        return 1

    if settings.r2_bucket_name != EXPECTED_BUCKET:
        print(f"FAIL: refusing to run; R2_BUCKET_NAME must be {EXPECTED_BUCKET}.")
        return 1
    if settings.configured_r2_endpoint != EXPECTED_ENDPOINT:
        print(
            "FAIL: refusing to run; the configured endpoint must be "
            f"{EXPECTED_ENDPOINT}."
        )
        return 1
    if settings.document_storage_backend != "local":
        print("FAIL: refusing to run; DOCUMENT_STORAGE_BACKEND must remain local.")
        return 1
    if settings.knowledge_max_file_size_bytes < EXPECTED_SIZE_BYTES:
        print(
            "FAIL: KNOWLEDGE_MAX_FILE_SIZE_BYTES must be at least "
            f"{EXPECTED_SIZE_BYTES} for this test."
        )
        return 1
    if (
        not settings.r2_access_key_id
        or not settings.r2_access_key_id.strip()
        or settings.r2_secret_access_key is None
        or not settings.r2_secret_access_key.get_secret_value().strip()
    ):
        print("FAIL: R2 credentials are not configured; values were not displayed.")
        return 1

    storage = CloudflareR2DocumentStorage()
    workspace_id = uuid4()
    content = _synthetic_content()
    original_digest = hashlib.sha256(content).hexdigest()
    stored = None
    upload_attempted = False
    checks_passed = False
    cleanup_passed = False

    try:
        upload_attempted = True
        stored = storage.put(
            BytesIO(content),
            "silo-multipart-synthetic-test.txt",
            settings,
            workspace_id,
        )
        print("PASS: uploaded the 9 MiB synthetic text document.")

        if not storage.exists(settings, workspace_id, stored.storage_key):
            raise RuntimeError("Uploaded object was not found.")
        print("PASS: exists() returned True.")

        metadata = storage.get_metadata(settings, workspace_id, stored.storage_key)
        if metadata.size_bytes != EXPECTED_SIZE_BYTES:
            raise RuntimeError("Stored size did not match 9 MiB.")
        if metadata.media_type != EXPECTED_MEDIA_TYPE:
            raise RuntimeError("Stored content type was not text/plain.")
        print("PASS: metadata size is 9 MiB and content type is text/plain.")

        with storage.open(settings, workspace_id, stored.storage_key) as source:
            open_digest, open_size = _sha256_stream(source)
        if open_size != EXPECTED_SIZE_BYTES or open_digest != original_digest:
            raise RuntimeError("open() content size or SHA-256 did not match.")
        print("PASS: open() download size and SHA-256 match.")

        with storage.stream(settings, workspace_id, stored.storage_key) as source:
            stream_digest, stream_size = _sha256_stream(source)
        if stream_size != EXPECTED_SIZE_BYTES or stream_digest != original_digest:
            raise RuntimeError("stream() content size or SHA-256 did not match.")
        print("PASS: stream() download size and SHA-256 match.")

        storage.delete(settings, workspace_id, stored.storage_key)
        if storage.exists(settings, workspace_id, stored.storage_key):
            raise RuntimeError("Deleted object still exists.")
        print("PASS: deleted object is no longer present.")
        checks_passed = True
    except Exception:
        print("FAIL: multipart integration check failed; provider details were suppressed.")
        if upload_attempted and stored is None:
            print(
                "WARNING: upload failed before returning a storage key; "
                "safe cleanup cannot be confirmed."
            )
    finally:
        if stored is not None:
            try:
                if storage.exists(settings, workspace_id, stored.storage_key):
                    storage.delete(settings, workspace_id, stored.storage_key)
                if storage.exists(settings, workspace_id, stored.storage_key):
                    raise RuntimeError("Cleanup could not verify object absence.")
                cleanup_passed = True
                print("PASS: cleanup verified this test object is absent.")
            except Exception:
                print("FAIL: cleanup could not be verified; no other keys were touched.")
        elif upload_attempted:
            print(
                "FAIL: cleanup was not verifiable because no storage key was returned."
            )

    return 0 if checks_passed and cleanup_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
