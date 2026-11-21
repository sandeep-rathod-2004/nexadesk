import json
import logging
import time
from uuid import UUID

_logger = logging.getLogger("app.chat.performance")
_auth_logger = logging.getLogger("app.auth.performance")


def request_id_for_log(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def log_stage(
    stage: str,
    started_at: float | None = None,
    request_id: UUID | str | None = None,
    metrics: dict[str, int | float] | None = None,
) -> None:
    event: dict[str, str | int | float] = {
        "event": "chat_performance",
        "stage": stage,
    }
    if request_id is not None:
        event["request_id"] = str(request_id)
    if started_at is not None:
        event["duration_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
    if metrics is not None:
        event.update(metrics)
    _logger.info(json.dumps(event, separators=(",", ":")))


def log_auth_stage(
    stage: str,
    started_at: float | None = None,
    request_id: UUID | str | None = None,
) -> None:
    try:
        event: dict[str, str | float] = {
            "event": "auth_performance",
            "stage": stage,
        }
        if request_id is not None:
            event["request_id"] = str(request_id)
        if started_at is not None:
            event["duration_ms"] = round((time.perf_counter() - started_at) * 1000, 2)
        _auth_logger.info(json.dumps(event, separators=(",", ":")))
    except Exception:
        pass
