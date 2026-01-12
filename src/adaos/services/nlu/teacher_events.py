from __future__ import annotations

import os
import time
from typing import Any, Mapping, Optional

from adaos.services.yjs.doc import async_get_ydoc

_MAX_EVENTS = int(os.getenv("ADAOS_NLU_TEACHER_EVENTS_MAX", "500") or "500")
_MAX_EVENTS_BY_CANDIDATE = int(os.getenv("ADAOS_NLU_TEACHER_EVENTS_BY_CANDIDATE_MAX", "1500") or "1500")

def rebuild_events_by_candidate(teacher: dict[str, Any]) -> dict[str, Any]:
    """
    Builds a derived list that allows grouping the *full* request log by candidate name.

    UI use-case: candidate_name -> request_id -> events (full log).
    """
    events = teacher.get("events")
    candidates = teacher.get("candidates")
    if not isinstance(events, list) or not isinstance(candidates, list):
        teacher["events_by_candidate"] = []
        return teacher

    cleaned_events = [x for x in events if isinstance(x, dict)]
    cleaned_candidates = [x for x in candidates if isinstance(x, dict)]

    req_to_candidates: dict[str, list[dict[str, Any]]] = {}
    for c in cleaned_candidates:
        req_id = c.get("request_id")
        cand_obj = c.get("candidate") if isinstance(c.get("candidate"), dict) else {}
        cand_name = cand_obj.get("name")
        if not isinstance(req_id, str) or not req_id:
            continue
        if not isinstance(cand_name, str) or not cand_name.strip():
            continue
        req_to_candidates.setdefault(req_id, []).append(
            {
                "name": cand_name.strip(),
                "description": cand_obj.get("description") if isinstance(cand_obj.get("description"), str) else "",
            }
        )

    by_candidate: list[dict[str, Any]] = []
    if req_to_candidates:
        for req_id, cand_list in req_to_candidates.items():
            req_events = [e for e in cleaned_events if isinstance(e, dict) and e.get("request_id") == req_id]
            for cand in cand_list:
                for e in req_events:
                    row = dict(e)
                    row["candidate_name"] = cand.get("name") or ""
                    row["candidate_description"] = cand.get("description") or ""
                    by_candidate.append(row)

    if _MAX_EVENTS_BY_CANDIDATE > 0 and len(by_candidate) > _MAX_EVENTS_BY_CANDIDATE:
        by_candidate = by_candidate[-_MAX_EVENTS_BY_CANDIDATE:]
    teacher["events_by_candidate"] = by_candidate
    return teacher


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

        rebuild_events_by_candidate(teacher)

        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)
