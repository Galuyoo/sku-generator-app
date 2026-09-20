from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1
APP_NAME = "sku_generator_app"


def _path() -> Path:
    configured = os.getenv("METRICS_PATH", "").strip()
    return Path(configured) if configured else Path("data") / "metrics" / "events.jsonl"


def app_version() -> str:
    return (
        os.getenv("APP_VERSION")
        or os.getenv("GIT_COMMIT")
        or os.getenv("COMMIT_SHA")
        or os.getenv("SOURCE_VERSION")
        or "unversioned"
    )


def classify_error(error: Any) -> str:
    text = str(error or "").lower()
    if "429" in text or "rate limit" in text or "daily variant" in text:
        return "rate_limit"
    if "auth" in text or "token" in text or "credential" in text:
        return "authentication_error"
    if "timeout" in text or "connection" in text or "network" in text:
        return "network_error"
    if "duplicate" in text or "already used" in text:
        return "duplicate"
    if "valid" in text or "missing" in text or "required" in text:
        return "validation_error"
    return "internal_error"


def log_event(
    event_type: str,
    *,
    status: str = "success",
    job_id: str = "",
    batch_id: str = "",
    operator_id: str = "",
    channel: str = "",
    duration_ms: int | None = None,
    products_count: int | None = None,
    variants_count: int | None = None,
    skus_count: int | None = None,
    duplicate_count: int | None = None,
    image_mappings_count: int | None = None,
    retry_count: int | None = None,
    error: Any = "",
    metadata: dict[str, Any] | None = None,
) -> bool:
    try:
        event = {
            "event_id": str(uuid4()),
            "occurred_at_utc": datetime.now(timezone.utc).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "app_name": APP_NAME,
            "app_version": app_version(),
            "environment": os.getenv("APP_ENVIRONMENT", ""),
            "event_type": str(event_type),
            "status": str(status),
            "job_id": str(job_id or ""),
            "batch_id": str(batch_id or ""),
            "operator_id": str(operator_id or ""),
            "channel": str(channel or ""),
            "duration_ms": duration_ms,
            "products_count": products_count,
            "variants_count": variants_count,
            "skus_count": skus_count,
            "duplicate_count": duplicate_count,
            "image_mappings_count": image_mappings_count,
            "retry_count": retry_count,
            "error_category": classify_error(error) if error else "",
            "error_message": str(error or "")[:1000] if error else "",
            "metadata": metadata or {},
        }
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False
