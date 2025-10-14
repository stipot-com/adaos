"""Event helpers for developer workflows."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict
from uuid import uuid4

from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit


def _emit(event_type: str, payload: Dict[str, object]) -> str:
    ctx = get_ctx()
    record = dict(payload)
    record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    record.setdefault("event_id", str(uuid4()))
    emit(ctx.bus, event_type, record, "dev.core")
    return str(record["event_id"])


def emit_created(payload: Dict[str, object]) -> str:
    return _emit("dev.artifact.created", payload)


def emit_deleted(payload: Dict[str, object]) -> str:
    return _emit("dev.artifact.deleted", payload)


def emit_pushed(payload: Dict[str, object]) -> str:
    return _emit("dev.artifact.pushed", payload)


def emit_registry_published(payload: Dict[str, object]) -> str:
    return _emit("dev.artifact.published", payload)
