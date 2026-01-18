from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any, Mapping, Optional

from adaos.sdk.core.decorators import subscribe
from adaos.services.nlu.teacher_events import rebuild_events_by_candidate
from adaos.services.nlu.teacher_store import load_teacher_state, save_teacher_state
from adaos.services.nlu.ycoerce import coerce_dict, is_mapping_like, iter_mappings
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.teacher.store.runtime")

_PERSIST_DEBOUNCE_S = 0.25

_pending: dict[str, asyncio.Task] = {}


def _payload(evt: Any) -> dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        p = getattr(evt, "payload")
        return p if isinstance(p, dict) else {}
    return {}


def _resolve_webspace_id(payload: Mapping[str, Any]) -> str:
    meta = coerce_dict(payload.get("_meta"))
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _jsonable(value: Any) -> Any:
    if is_mapping_like(value):
        return {str(k): _jsonable(v) for k, v in coerce_dict(value).items()}
    if isinstance(value, (str, bytes, bytearray)):
        return value if isinstance(value, str) else value.decode("utf-8", errors="ignore")
    if isinstance(value, Iterable):
        return [_jsonable(v) for v in list(value)]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return str(value)
    except Exception:
        return None


def _merge_list_by_id(
    *,
    current: Any,
    saved: Any,
    max_items: Optional[int] = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    def _push_many(raw: Any) -> None:
        if isinstance(raw, (str, bytes, bytearray)) or isinstance(raw, Mapping) or not isinstance(raw, Iterable):
            return
        for item in iter_mappings(raw):
            item = dict(item)
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                prev = by_id.get(item_id)
                if prev is None:
                    by_id[item_id] = item
                else:
                    prev_ts = prev.get("ts")
                    next_ts = item.get("ts")
                    if isinstance(prev_ts, (int, float)) and isinstance(next_ts, (int, float)) and next_ts >= prev_ts:
                        by_id[item_id] = item
            else:
                items.append(item)

    _push_many(saved)
    _push_many(current)

    items.extend(by_id.values())
    items.sort(key=lambda x: float(x.get("ts") or 0.0))
    if isinstance(max_items, int) and max_items > 0 and len(items) > max_items:
        items = items[-max_items:]
    return items


def _merge_teacher(*, current: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(saved)
    merged.update(current)

    merged["events"] = _merge_list_by_id(current=current.get("events"), saved=saved.get("events"), max_items=500)
    merged["revisions"] = _merge_list_by_id(current=current.get("revisions"), saved=saved.get("revisions"), max_items=200)
    merged["candidates"] = _merge_list_by_id(current=current.get("candidates"), saved=saved.get("candidates"), max_items=200)
    merged["dataset"] = _merge_list_by_id(current=current.get("dataset"), saved=saved.get("dataset"), max_items=500)
    merged["items"] = _merge_list_by_id(current=current.get("items"), saved=saved.get("items"), max_items=200)
    merged["plan"] = _merge_list_by_id(current=current.get("plan"), saved=saved.get("plan"), max_items=200)
    merged["llm_logs"] = _merge_list_by_id(current=current.get("llm_logs"), saved=saved.get("llm_logs"), max_items=300)

    rebuild_events_by_candidate(merged)
    return merged


async def _read_teacher_from_ydoc(webspace_id: str) -> dict[str, Any]:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        current = data_map.get("nlu_teacher")
        teacher = coerce_dict(current)
        return _jsonable(teacher)


async def _write_teacher_to_ydoc(webspace_id: str, teacher: dict[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)


async def _persist_now(webspace_id: str) -> None:
    try:
        teacher = await _read_teacher_from_ydoc(webspace_id)
        if not teacher:
            return
        save_teacher_state(webspace_id=webspace_id, teacher=teacher)
    except Exception:
        _log.debug("persist failed webspace=%s", webspace_id, exc_info=True)


def _schedule_persist(webspace_id: str) -> None:
    existing = _pending.get(webspace_id)
    if existing and not existing.done():
        return

    async def _job() -> None:
        await asyncio.sleep(_PERSIST_DEBOUNCE_S)
        await _persist_now(webspace_id)

    _pending[webspace_id] = asyncio.create_task(_job())


@subscribe("scenarios.synced")
async def _on_scenarios_synced(evt: Any) -> None:
    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)

    saved = load_teacher_state(webspace_id=webspace_id)
    if not saved:
        return

    try:
        current = await _read_teacher_from_ydoc(webspace_id)
        merged = _merge_teacher(current=current, saved=saved)
        await _write_teacher_to_ydoc(webspace_id, merged)
        save_teacher_state(webspace_id=webspace_id, teacher=merged)
        _log.info("rehydrated nlu_teacher from store webspace=%s", webspace_id)
    except Exception:
        _log.debug("rehydrate failed webspace=%s", webspace_id, exc_info=True)


# Persist on all meaningful teacher mutations.
@subscribe("nlp.teacher.revision.proposed")
async def _on_revision_proposed(evt: Any) -> None:
    _schedule_persist(_resolve_webspace_id(_payload(evt)))


@subscribe("nlp.teacher.revision.suggested")
async def _on_revision_suggested(evt: Any) -> None:
    _schedule_persist(_resolve_webspace_id(_payload(evt)))


@subscribe("nlp.teacher.revision.applied")
async def _on_revision_applied(evt: Any) -> None:
    _schedule_persist(_resolve_webspace_id(_payload(evt)))


@subscribe("nlp.teacher.candidate.proposed")
async def _on_candidate_proposed(evt: Any) -> None:
    _schedule_persist(_resolve_webspace_id(_payload(evt)))


@subscribe("nlp.teacher.candidate.applied")
async def _on_candidate_applied(evt: Any) -> None:
    _schedule_persist(_resolve_webspace_id(_payload(evt)))


@subscribe("nlp.teacher.regex_rule.applied")
async def _on_regex_rule_applied(evt: Any) -> None:
    _schedule_persist(_resolve_webspace_id(_payload(evt)))
