from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Mapping, Optional

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.nlu.teacher_events import append_event, make_event
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.teacher.runtime")

_MAX_ITEMS = int(os.getenv("ADAOS_NLU_TEACHER_MAX", "200") or "200")
_ENABLED = os.getenv("ADAOS_NLU_TEACHER") == "1"


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _read_teacher_obj(data_map: Any) -> dict[str, Any]:
    current = data_map.get("nlu_teacher")
    return dict(current) if isinstance(current, dict) else {}


def _bounded(items: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    if max_items <= 0:
        return items
    if len(items) <= max_items:
        return items
    return items[-max_items:]


async def _append_revision(webspace_id: str, revision: dict[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        teacher = _read_teacher_obj(data_map)
        revisions = teacher.get("revisions")
        if not isinstance(revisions, list):
            revisions = []
        revisions = [x for x in revisions if isinstance(x, dict)]
        revisions.append(revision)
        revisions = _bounded(revisions, max_items=_MAX_ITEMS)
        teacher["revisions"] = revisions
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)


async def _update_revision(
    webspace_id: str,
    *,
    revision_id: str,
    patch: dict[str, Any],
) -> Optional[dict[str, Any]]:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        teacher = _read_teacher_obj(data_map)
        revisions = teacher.get("revisions")
        if not isinstance(revisions, list):
            return None

        cleaned: list[dict[str, Any]] = []
        updated: Optional[dict[str, Any]] = None
        for item in revisions:
            if not isinstance(item, dict):
                continue
            if item.get("id") == revision_id:
                updated = dict(item)
                updated.update(patch)
                cleaned.append(updated)
            else:
                cleaned.append(item)

        teacher["revisions"] = _bounded(cleaned, max_items=_MAX_ITEMS)
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)
        return updated


async def _append_dataset_item(webspace_id: str, item: dict[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        teacher = _read_teacher_obj(data_map)
        dataset = teacher.get("dataset")
        if not isinstance(dataset, list):
            dataset = []
        dataset = [x for x in dataset if isinstance(x, dict)]
        dataset.append(item)
        dataset = _bounded(dataset, max_items=_MAX_ITEMS)
        teacher["dataset"] = dataset
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)


@subscribe("nlp.teacher.request")
async def _on_teacher_request(evt: Any) -> None:
    if not _ENABLED:
        return

    ctx = get_ctx()
    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)

    req = payload.get("request") if isinstance(payload.get("request"), Mapping) else None
    if not req:
        return

    text = req.get("text")
    if not isinstance(text, str) or not text.strip():
        return

    revision = {
        "id": f"rev.{int(time.time()*1000)}",
        "ts": time.time(),
        "status": "proposed",
        "request_id": req.get("request_id"),
        "text": text.strip(),
        "meta": dict(req.get("_meta") or {}) if isinstance(req.get("_meta"), Mapping) else {},
        "proposal": {
            "intent": None,
            "examples": [text.strip()],
            "slots": {},
        },
        "note": "Awaiting teacher/LLM suggestion (stub).",
    }

    try:
        await _append_revision(webspace_id, revision)
    except Exception:
        _log.debug("failed to append teacher revision webspace=%s", webspace_id, exc_info=True)
        return

    try:
        await append_event(
            webspace_id,
            make_event(
                webspace_id=webspace_id,
                request_id=revision.get("request_id") if isinstance(revision.get("request_id"), str) else None,
                request_text=text.strip(),
                kind="revision.proposed",
                title="Revision proposed",
                subtitle="pending",
                raw=revision,
                meta=revision.get("meta") if isinstance(revision.get("meta"), Mapping) else {},
            ),
        )
    except Exception:
        _log.debug("failed to append nlu_teacher event webspace=%s", webspace_id, exc_info=True)

    bus_emit(
        ctx.bus,
        "nlp.teacher.revision.proposed",
        {"webspace_id": webspace_id, "revision": revision},
        source="nlu.teacher.runtime",
    )


@subscribe("nlp.teacher.revision.apply")
async def _on_revision_apply(evt: Any) -> None:
    if not _ENABLED:
        return

    ctx = get_ctx()
    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)

    revision_id = payload.get("revision_id")
    if not isinstance(revision_id, str) or not revision_id.strip():
        return
    revision_id = revision_id.strip()

    intent = payload.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return
    intent = intent.strip()

    examples = payload.get("examples")
    if isinstance(examples, list):
        examples = [x.strip() for x in examples if isinstance(x, str) and x.strip()]
    else:
        examples = []

    slots = payload.get("slots") if isinstance(payload.get("slots"), Mapping) else {}

    apply_patch = {
        "status": "applied",
        "applied_at": time.time(),
        "applied": {
            "intent": intent,
            "examples": examples,
            "slots": dict(slots),
        },
    }
    try:
        updated = await _update_revision(webspace_id, revision_id=revision_id, patch=apply_patch)
    except Exception:
        _log.debug("failed to apply teacher revision webspace=%s revision_id=%s", webspace_id, revision_id, exc_info=True)
        return

    dataset_item = {
        "id": f"ds.{int(time.time()*1000)}",
        "ts": time.time(),
        "status": "revision",
        "intent": intent,
        "examples": examples,
        "slots": dict(slots),
        "revision_id": revision_id,
    }
    try:
        await _append_dataset_item(webspace_id, dataset_item)
    except Exception:
        _log.debug("failed to append teacher dataset item webspace=%s", webspace_id, exc_info=True)

    try:
        await append_event(
            webspace_id,
            make_event(
                webspace_id=webspace_id,
                request_id=updated.get("request_id") if isinstance(updated, dict) and isinstance(updated.get("request_id"), str) else None,
                request_text=updated.get("text") if isinstance(updated, dict) and isinstance(updated.get("text"), str) else intent,
                kind="revision.applied",
                title="Revision applied",
                subtitle=intent,
                raw=updated if isinstance(updated, Mapping) else {"revision_id": revision_id, "intent": intent},
                meta=updated.get("meta") if isinstance(updated, dict) and isinstance(updated.get("meta"), Mapping) else {},
            ),
        )
    except Exception:
        _log.debug("failed to append nlu_teacher event webspace=%s", webspace_id, exc_info=True)

    bus_emit(
        ctx.bus,
        "nlp.teacher.revision.applied",
        {"webspace_id": webspace_id, "revision": updated, "dataset_item": dataset_item},
        source="nlu.teacher.runtime",
    )
