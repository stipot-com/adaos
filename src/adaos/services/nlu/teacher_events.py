from __future__ import annotations

import os
import time
from collections.abc import Iterable
from typing import Any, Mapping, Optional

from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.nlu.ycoerce import coerce_dict, iter_mappings

_MAX_EVENTS = int(os.getenv("ADAOS_NLU_TEACHER_EVENTS_MAX", "500") or "500")
_MAX_EVENTS_BY_CANDIDATE = int(os.getenv("ADAOS_NLU_TEACHER_EVENTS_BY_CANDIDATE_MAX", "1500") or "1500")
_MAX_THREADS = int(os.getenv("ADAOS_NLU_TEACHER_THREADS_MAX", "250") or "250")


def _as_list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, bytearray)) or isinstance(value, Mapping) or not isinstance(value, Iterable):
        return []
    return [dict(x) for x in iter_mappings(value)]


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        try:
            return str(value)
        except Exception:
            return ""


def _thread_log_text(
    *,
    request_id: str,
    request_text: str,
    events: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    revisions: list[dict[str, Any]],
    llm_logs: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append(f"request_id: {request_id}")
    if request_text:
        lines.append(f"text: {request_text}")
    lines.append("")

    if candidates:
        lines.append("candidates:")
        for c in candidates:
            cand = coerce_dict(c.get("candidate"))
            name = cand.get("name") if isinstance(cand.get("name"), str) else ""
            kind = c.get("kind") if isinstance(c.get("kind"), str) else ""
            status = c.get("status") if isinstance(c.get("status"), str) else ""
            lines.append(f"- id={c.get('id')} kind={kind} status={status} name={name}")
            desc = cand.get("description") if isinstance(cand.get("description"), str) else ""
            if desc:
                lines.append(f"  description: {desc}")
            if kind == "regex_rule":
                rr = coerce_dict(c.get("regex_rule"))
                intent = rr.get("intent") if isinstance(rr.get("intent"), str) else ""
                pattern = rr.get("pattern") if isinstance(rr.get("pattern"), str) else ""
                if intent:
                    lines.append(f"  regex.intent: {intent}")
                if pattern:
                    lines.append(f"  regex.pattern: {pattern}")
        lines.append("")

    if revisions:
        lines.append("revisions:")
        for r in revisions:
            status = r.get("status") if isinstance(r.get("status"), str) else ""
            proposal = coerce_dict(r.get("proposal"))
            intent = proposal.get("intent") if isinstance(proposal.get("intent"), str) else ""
            lines.append(f"- id={r.get('id')} status={status} intent={intent}")
            examples = proposal.get("examples")
            if isinstance(examples, list) and examples:
                ex = [x for x in examples if isinstance(x, str)]
                if ex:
                    lines.append("  examples:")
                    for x in ex[:25]:
                        lines.append(f"  - {x}")
        lines.append("")

    # Events are the primary canonical chronological record.
    if events:
        lines.append("events:")
        for e in sorted(events, key=lambda x: float(x.get("ts") or 0.0)):
            ts = e.get("ts")
            kind = e.get("kind") if isinstance(e.get("kind"), str) else ""
            title = e.get("title") if isinstance(e.get("title"), str) else ""
            subtitle = e.get("subtitle") if isinstance(e.get("subtitle"), str) else ""
            lines.append(f"- ts={ts} kind={kind} title={title} subtitle={subtitle}".rstrip())
            raw = e.get("raw")
            raw_txt = _json_text(raw).strip()
            if raw_txt:
                # Keep the log readable; raw can be large.
                raw_lines = raw_txt.splitlines()
                for ln in raw_lines[:120]:
                    lines.append(f"  {ln}")
                if len(raw_lines) > 120:
                    lines.append("  ... (truncated)")
        lines.append("")

    if llm_logs:
        lines.append("llm_logs:")
        for log in sorted(llm_logs, key=lambda x: float(x.get("ts") or 0.0)):
            status = log.get("status") if isinstance(log.get("status"), str) else ""
            model = log.get("model") if isinstance(log.get("model"), str) else ""
            lines.append(f"- id={log.get('id')} status={status} model={model}".rstrip())
            resp = coerce_dict(log.get("response"))
            raw_txt = resp.get("raw") if isinstance(resp.get("raw"), str) else ""
            if raw_txt:
                for ln in raw_txt.splitlines()[:60]:
                    lines.append(f"  {ln}")
                if len(raw_txt.splitlines()) > 60:
                    lines.append("  ... (truncated)")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def rebuild_threads(teacher: dict[str, Any]) -> dict[str, Any]:
    """
    Builds derived thread views for schema-driven UI:

    - threads_by_request: 1 item per request_id
    - threads_by_candidate: 1 item per (candidate_id) with header=LLM candidate name
    """
    events = _as_list_of_dicts(teacher.get("events"))
    candidates = _as_list_of_dicts(teacher.get("candidates"))
    revisions = _as_list_of_dicts(teacher.get("revisions"))
    llm_logs = _as_list_of_dicts(teacher.get("llm_logs"))

    request_ids: set[str] = set()
    for e in events:
        rid = e.get("request_id")
        if isinstance(rid, str) and rid:
            request_ids.add(rid)
    for c in candidates:
        rid = c.get("request_id")
        if isinstance(rid, str) and rid:
            request_ids.add(rid)
    for r in revisions:
        rid = r.get("request_id")
        if isinstance(rid, str) and rid:
            request_ids.add(rid)
    for l in llm_logs:
        rid = l.get("request_id")
        if isinstance(rid, str) and rid:
            request_ids.add(rid)

    def _request_text_for(rid: str) -> str:
        for e in events:
            if e.get("request_id") == rid and isinstance(e.get("request_text"), str) and e.get("request_text"):
                return e.get("request_text") or ""
        for c in candidates:
            if c.get("request_id") == rid and isinstance(c.get("text"), str) and c.get("text"):
                return c.get("text") or ""
        for r in revisions:
            if r.get("request_id") == rid and isinstance(r.get("text"), str) and r.get("text"):
                return r.get("text") or ""
        return ""

    threads_by_request: list[dict[str, Any]] = []
    threads_by_candidate: list[dict[str, Any]] = []

    for rid in sorted(request_ids):
        req_text = _request_text_for(rid)
        ev = [e for e in events if e.get("request_id") == rid]
        cand = [c for c in candidates if c.get("request_id") == rid]
        rev = [r for r in revisions if r.get("request_id") == rid]
        llm = [l for l in llm_logs if l.get("request_id") == rid]

        # Default "Apply" action for the request thread: apply the first pending candidate.
        pending_candidate_id = ""
        for c in cand:
            if c.get("status") == "pending" and isinstance(c.get("id"), str):
                pending_candidate_id = c.get("id") or ""
                break

        details = _thread_log_text(
            request_id=rid,
            request_text=req_text,
            events=ev,
            candidates=cand,
            revisions=rev,
            llm_logs=llm,
        )

        subtitle_parts: list[str] = []
        if cand:
            subtitle_parts.append(f"candidates={len(cand)}")
        if rev:
            subtitle_parts.append(f"revisions={len(rev)}")
        subtitle = ", ".join(subtitle_parts)

        threads_by_request.append(
            {
                "id": f"req.{rid}",
                "request_id": rid,
                "title": req_text or rid,
                "subtitle": subtitle,
                "details": details,
                "candidate_id": pending_candidate_id,
            }
        )

        for c in cand:
            cand_obj = coerce_dict(c.get("candidate"))
            name = cand_obj.get("name") if isinstance(cand_obj.get("name"), str) else ""
            description = cand_obj.get("description") if isinstance(cand_obj.get("description"), str) else ""
            target_obj = c.get("target") if isinstance(c.get("target"), Mapping) else None
            target_type = target_obj.get("type") if isinstance(target_obj, Mapping) else None
            target_id = target_obj.get("id") if isinstance(target_obj, Mapping) else None
            if not isinstance(target_type, str) or not target_type.strip():
                target_type = ""
            if not isinstance(target_id, str) or not target_id.strip():
                target_id = ""
            target_label = f"{target_type}:{target_id}".strip(":") if target_type and target_id else ""

            cand_kind = c.get("kind") if isinstance(c.get("kind"), str) else ""
            candidate_meta = cand_kind
            if target_label:
                candidate_meta = f"{cand_kind} → {target_label}".strip()

            cid = c.get("id") if isinstance(c.get("id"), str) else ""
            if not cid:
                continue
            threads_by_candidate.append(
                {
                    "id": cid,
                    "candidate_id": cid,
                    "candidate_kind": cand_kind,
                    "candidate_name": name,
                    "candidate_description": description,
                    "candidate_target": dict(target_obj) if isinstance(target_obj, Mapping) else None,
                    "candidate_target_type": target_type,
                    "candidate_target_id": target_id,
                    "candidate_target_label": target_label,
                    "candidate_meta": candidate_meta,
                    "candidate_origin_scenario_id": c.get("origin_scenario_id")
                    if isinstance(c.get("origin_scenario_id"), str)
                    else "",
                    "candidate_status": c.get("status") if isinstance(c.get("status"), str) else "",
                    "request_id": rid,
                    "title": name or cid,
                    "subtitle": req_text or rid,
                    "details": details,
                }
            )

    # Keep the newest threads (roughly by embedded timestamps).
    if _MAX_THREADS > 0 and len(threads_by_request) > _MAX_THREADS:
        threads_by_request = threads_by_request[-_MAX_THREADS:]
    if _MAX_THREADS > 0 and len(threads_by_candidate) > _MAX_THREADS * 2:
        threads_by_candidate = threads_by_candidate[-(_MAX_THREADS * 2) :]

    teacher["threads_by_request"] = threads_by_request
    teacher["threads_by_candidate"] = threads_by_candidate
    return teacher

def rebuild_events_by_candidate(teacher: dict[str, Any]) -> dict[str, Any]:
    """
    Builds a derived list that allows grouping the *full* request log by candidate name.

    UI use-case: candidate_name -> request_id -> events (full log).
    """
    events = teacher.get("events")
    candidates = teacher.get("candidates")

    if isinstance(events, (str, bytes, bytearray)) or isinstance(events, Mapping) or not isinstance(events, Iterable):
        teacher["events_by_candidate"] = []
        return teacher

    cleaned_events = [dict(x) for x in iter_mappings(events)]

    if isinstance(candidates, (str, bytes, bytearray)) or isinstance(candidates, Mapping) or not isinstance(candidates, Iterable):
        cleaned_candidates = []
    else:
        cleaned_candidates = [dict(x) for x in iter_mappings(candidates)]

    req_to_candidates: dict[str, list[dict[str, Any]]] = {}

    def _add_candidate(req_id: Any, *, name: Any, description: Any = "", kind: str = "") -> None:
        if not isinstance(req_id, str) or not req_id.strip():
            return
        if not isinstance(name, str) or not name.strip():
            return
        rid = req_id.strip()
        row = {
            "name": name.strip(),
            "description": description.strip() if isinstance(description, str) else "",
            "kind": kind,
        }
        req_to_candidates.setdefault(rid, []).append(row)

    # 1) Canonical source: teacher.candidates list
    for c in cleaned_candidates:
        req_id = c.get("request_id")
        cand_obj = coerce_dict(c.get("candidate"))
        _add_candidate(
            req_id,
            name=cand_obj.get("name"),
            description=cand_obj.get("description"),
            kind=str(c.get("kind") or "candidate"),
        )

    # 2) Fallback: derive candidates from events (more robust across partial persistence).
    # This also lets us show suggested revisions as "intent candidates" grouped by intent name.
    for e in cleaned_events:
        req_id = e.get("request_id")
        kind = e.get("kind")
        raw = coerce_dict(e.get("raw"))

        if kind in {"candidate.proposed", "candidate.applied"}:
            cand_obj = coerce_dict(raw.get("candidate"))
            _add_candidate(
                req_id,
                name=cand_obj.get("name"),
                description=cand_obj.get("description"),
                kind=str(raw.get("kind") or "candidate"),
            )
        if kind in {"revision.proposed", "revision.suggested", "revision.applied"}:
            proposal = coerce_dict(raw.get("proposal"))
            intent = proposal.get("intent")
            _add_candidate(req_id, name=intent, description="Intent suggestion", kind="intent")

    by_candidate: list[dict[str, Any]] = []
    if req_to_candidates:
        # Stabilize order and avoid duplicate candidate rows per request.
        for rid, rows in list(req_to_candidates.items()):
            seen: set[tuple[str, str]] = set()
            deduped: list[dict[str, Any]] = []
            for row in rows:
                name = str(row.get("name") or "")
                kind = str(row.get("kind") or "")
                key = (name, kind)
                if not name or key in seen:
                    continue
                seen.add(key)
                deduped.append(row)
            req_to_candidates[rid] = deduped

        for req_id, cand_list in req_to_candidates.items():
            req_events = [e for e in cleaned_events if isinstance(e, Mapping) and e.get("request_id") == req_id]
            for cand in cand_list:
                for e in req_events:
                    row = dict(e) if isinstance(e, Mapping) else {}
                    row["candidate_name"] = cand.get("name") or ""
                    row["candidate_description"] = cand.get("description") or ""
                    row["candidate_kind"] = cand.get("kind") or ""
                    by_candidate.append(row)

    if _MAX_EVENTS_BY_CANDIDATE > 0 and len(by_candidate) > _MAX_EVENTS_BY_CANDIDATE:
        by_candidate = by_candidate[-_MAX_EVENTS_BY_CANDIDATE:]
    teacher["events_by_candidate"] = by_candidate
    rebuild_threads(teacher)
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
        "raw": coerce_dict(raw) if raw is not None else None,
        "_meta": coerce_dict(meta),
    }


async def append_event(webspace_id: str, event: Mapping[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        current = data_map.get("nlu_teacher")
        teacher: dict[str, Any] = coerce_dict(current)

        events = teacher.get("events")
        if isinstance(events, (str, bytes, bytearray)) or isinstance(events, Mapping) or not isinstance(events, Iterable):
            events = []
        events = [dict(x) for x in iter_mappings(events)]
        events.append(dict(event) if isinstance(event, Mapping) else {})
        if _MAX_EVENTS > 0 and len(events) > _MAX_EVENTS:
            events = events[-_MAX_EVENTS:]
        teacher["events"] = events

        rebuild_events_by_candidate(teacher)

        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)
