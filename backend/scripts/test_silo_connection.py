import hashlib
import hmac
import sys
from pathlib import Path
from uuid import uuid4

from botocore.exceptions import ClientError

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings
from app.services.r2_storage import _get_client

EXPECTED_BUCKET = "nexadesk-documents"
NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _is_not_found(error: ClientError) -> bool:
    response = error.response
    code = response.get("Error", {}).get("Code")
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in NOT_FOUND_CODES or status_code == 404


def _object_exists(client, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        if _is_not_found(error):
            return False
        raise


def _required_configuration_missing(settings: Settings) -> list[str]:
    missing = []
    if not settings.configured_r2_endpoint:
        missing.append("R2_ENDPOINT_URL or R2_ACCOUNT_ID")
    if not settings.r2_access_key_id or not settings.r2_access_key_id.strip():
        missing.append("R2_ACCESS_KEY_ID")
    if (
        settings.r2_secret_access_key is None
        or not settings.r2_secret_access_key.get_secret_value().strip()
    ):
        missing.append("R2_SECRET_ACCESS_KEY")
    return missing


def main() -> int:
    try:
        settings = Settings(_env_file=BACKEND_DIR / ".env")
    except Exception:
        print("FAIL: backend settings could not be loaded; values were not displayed.")
        return 1

    if settings.r2_bucket_name != EXPECTED_BUCKET:
        print(f"FAIL: refusing to run; R2_BUCKET_NAME must be {EXPECTED_BUCKET}.")
        return 1

    missing = _required_configuration_missing(settings)
    if missing:
        print(f"FAIL: missing required configuration: {', '.join(missing)}.")
        return 1

    try:
        client = _get_client(settings)
    except Exception:
        print("FAIL: S3 client could not be configured; details were not displayed.")
        return 1

    run_id = uuid4().hex
    object_key = f"nexadesk-smoke-tests/{run_id}.txt"
    content = f"NexaDesk Silo smoke test {run_id}\n".encode("utf-8")
    expected_sha256 = hashlib.sha256(content).hexdigest()
    upload_attempted = False
    deletion_verified = False
    exit_code = 0

    try:
        client.head_bucket(Bucket=EXPECTED_BUCKET)
        print("PASS: configured S3 endpoint can access the expected bucket.")

        if _object_exists(client, EXPECTED_BUCKET, object_key):
            raise RuntimeError("The generated smoke-test key already exists.")

        upload_attempted = True
        client.put_object(
            Bucket=EXPECTED_BUCKET,
            Key=object_key,
            Body=content,
            ContentLength=len(content),
            ContentType="text/plain; charset=utf-8",
            Metadata={"nexadesk-smoke-test-id": run_id},
            IfNoneMatch="*",
        )
        print("PASS: uploaded a unique synthetic object.")

        head = client.head_object(Bucket=EXPECTED_BUCKET, Key=object_key)
        if head.get("ContentType") != "text/plain; charset=utf-8":
            raise RuntimeError("Uploaded content type did not match.")
        if head.get("Metadata", {}).get("nexadesk-smoke-test-id") != run_id:
            raise RuntimeError("Uploaded object metadata did not match.")
        print("PASS: object exists and content type/metadata match.")

        response = client.get_object(Bucket=EXPECTED_BUCKET, Key=object_key)
        body = response["Body"]
        try:
            downloaded = body.read()
        finally:
            body.close()
        downloaded_sha256 = hashlib.sha256(downloaded).hexdigest()
        if downloaded != content or not hmac.compare_digest(
            downloaded_sha256, expected_sha256
        ):
            raise RuntimeError("Downloaded content or SHA-256 checksum did not match.")
        print("PASS: downloaded synthetic content and SHA-256 checksum match.")

        client.delete_object(Bucket=EXPECTED_BUCKET, Key=object_key)
        if _object_exists(client, EXPECTED_BUCKET, object_key):
            raise RuntimeError("The object remained accessible after deletion.")
        deletion_verified = True
        print("PASS: deleted the synthetic object and confirmed it is absent.")
    except KeyboardInterrupt:
        print("FAIL: interrupted; attempting safe cleanup.")
        exit_code = 130
    except Exception:
        print("FAIL: Silo smoke test failed; provider details were not displayed.")
        exit_code = 1
    finally:
        if upload_attempted and not deletion_verified:
            try:
                try:
                    head = client.head_object(Bucket=EXPECTED_BUCKET, Key=object_key)
                except ClientError as error:
                    if _is_not_found(error):
                        deletion_verified = True
                    else:
                        raise
                else:
                    if head.get("Metadata", {}).get(
                        "nexadesk-smoke-test-id"
                    ) == run_id:
                        client.delete_object(Bucket=EXPECTED_BUCKET, Key=object_key)
                        if _object_exists(client, EXPECTED_BUCKET, object_key):
                            raise RuntimeError("Cleanup could not verify deletion.")
                        deletion_verified = True

                if deletion_verified:
                    print("PASS: cleanup verified the synthetic object is absent.")
                else:
                    print(
                        "FAIL: cleanup refused to delete an object whose ownership "
                        "could not be verified."
                    )
                    exit_code = 1
            except Exception:
                print("FAIL: cleanup could not be verified; no other keys were touched.")
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
