from __future__ import annotations

import os
import time
from typing import Any, Mapping, Optional

from adaos.services.yjs.doc import async_get_ydoc

_MAX_EVENTS = int(os.getenv("ADAOS_NLU_TEACHER_EVENTS_MAX", "500") or "500")


def make_event(
    *,
    webspace_id: str,
    request_id: Optional[str],
    request_text: str,
    kind: str,
    title: str,
    subtitle: str = "",
    raw: Optional[Mapping[str, Any]] = None,
    meta: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "id": f"evt.{int(time.time() * 1000)}",
        "ts": time.time(),
        "webspace_id": webspace_id,
        "request_id": request_id,
        "request_text": request_text,
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "raw": dict(raw) if isinstance(raw, Mapping) else None,
        "_meta": dict(meta) if isinstance(meta, Mapping) else {},
    }


async def append_event(webspace_id: str, event: Mapping[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        current = data_map.get("nlu_teacher")
        teacher: dict[str, Any] = dict(current) if isinstance(current, dict) else {}

        events = teacher.get("events")
        if not isinstance(events, list):
            events = []
        events = [x for x in events if isinstance(x, dict)]
        events.append(dict(event))
        if _MAX_EVENTS > 0 and len(events) > _MAX_EVENTS:
            events = events[-_MAX_EVENTS:]
        teacher["events"] = events

        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)

