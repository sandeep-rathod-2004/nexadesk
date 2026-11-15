import argparse
import json
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.services.storage_migration import transfer_local_documents_to_r2


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Copy local knowledge documents to private R2 without modifying "
            "database records or deleting local files."
        )
    )
    parser.add_argument(
        "--report-file",
        required=True,
        type=Path,
        help="Local JSON report/checkpoint file used to resume the operation.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Copy documents. Requires --confirm-database matching the database name.",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify every DB document against its R2 object without copying.",
    )
    parser.add_argument(
        "--confirm-database",
        help="Exact PostgreSQL database name required with --apply or --verify-only.",
    )
    parser.add_argument("--max-attempts", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    settings = get_settings()
    database_url = make_url(settings.database_url)
    if database_url.get_backend_name() != "postgresql":
        print(
            "Refusing to run: document transfer requires a PostgreSQL DATABASE_URL.",
            file=sys.stderr,
        )
        return 2
    if args.apply or args.verify_only:
        if not args.confirm_database:
            print(
                "--confirm-database is required for apply or verify-only mode.",
                file=sys.stderr,
            )
            return 2
        if database_url.database != args.confirm_database:
            print(
                "Refusing to run: confirmation does not match the configured database.",
                file=sys.stderr,
            )
            return 2
    if args.max_attempts < 1 or args.max_attempts > 5:
        print("--max-attempts must be between 1 and 5.", file=sys.stderr)
        return 2

    try:
        with get_session_factory()() as db:
            report = transfer_local_documents_to_r2(
                db,
                settings,
                args.report_file,
                dry_run=not args.apply and not args.verify_only,
                verify_only=args.verify_only,
                max_attempts=args.max_attempts,
            )
    except Exception:
        print(
            "Document transfer failed. Check backend configuration and the report file.",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report["summary"], sort_keys=True))
    return 1 if report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
