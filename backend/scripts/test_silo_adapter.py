import sys
from io import BytesIO
from pathlib import Path
from uuid import uuid4

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings
from app.services.knowledge_storage import DocumentStorageNotFoundError
from app.services.r2_storage import CloudflareR2DocumentStorage

EXPECTED_BUCKET = "nexadesk-documents"
EXPECTED_ENDPOINT = "http://127.0.0.1:9000"
EXPECTED_MEDIA_TYPE = "text/plain"


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
    content = f"NexaDesk Silo adapter integration test {uuid4()}\n".encode("utf-8")
    stored = None
    checks_passed = False

    try:
        stored = storage.put(
            BytesIO(content),
            "silo-adapter-smoke-test.txt",
            settings,
            workspace_id,
        )
        print("PASS: synthetic text document uploaded.")

        if not storage.exists(settings, workspace_id, stored.storage_key):
            raise RuntimeError("Uploaded object was not found.")
        print("PASS: exists() returned True.")

        metadata = storage.get_metadata(settings, workspace_id, stored.storage_key)
        if metadata.size_bytes != len(content):
            raise RuntimeError("Stored size did not match.")
        if metadata.media_type != EXPECTED_MEDIA_TYPE:
            raise RuntimeError("Stored content type did not match.")
        print("PASS: metadata size and content type match.")

        with storage.open(settings, workspace_id, stored.storage_key) as source:
            if source.read() != content:
                raise RuntimeError("open() content did not match.")
        print("PASS: open() returned the complete expected content.")

        with storage.stream(settings, workspace_id, stored.storage_key) as source:
            if source.read() != content:
                raise RuntimeError("stream() content did not match.")
        print("PASS: stream() returned the complete expected content.")

        storage.delete(settings, workspace_id, stored.storage_key)
        if storage.exists(settings, workspace_id, stored.storage_key):
            raise RuntimeError("Deleted object still exists.")
        print("PASS: delete() completed and exists() returned False.")

        for read_operation in (storage.open, storage.stream):
            try:
                with read_operation(settings, workspace_id, stored.storage_key):
                    pass
            except DocumentStorageNotFoundError:
                continue
            raise RuntimeError("Reading the deleted object did not report not-found.")
        print("PASS: open() and stream() raise the expected not-found error.")
        checks_passed = True
    except Exception:
        print("FAIL: adapter integration check failed; provider details were suppressed.")
    finally:
        if stored is not None:
            try:
                if storage.exists(settings, workspace_id, stored.storage_key):
                    storage.delete(settings, workspace_id, stored.storage_key)
                if storage.exists(settings, workspace_id, stored.storage_key):
                    raise RuntimeError("Cleanup could not verify object absence.")
                print("PASS: cleanup verified the synthetic object is absent.")
            except Exception:
                print("FAIL: cleanup could not be verified; no other keys were touched.")
                checks_passed = False

    return 0 if checks_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
