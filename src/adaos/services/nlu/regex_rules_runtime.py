from __future__ import annotations

import logging
import time
from typing import Any, Dict, Mapping, Optional

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.nlu.teacher_events import append_event, make_event
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.regex_rules")


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


def _teacher_obj(data_map: Any) -> dict[str, Any]:
    current = data_map.get("nlu_teacher")
    return dict(current) if isinstance(current, dict) else {}


def _read_nlu_obj(data_map: Any) -> dict[str, Any]:
    current = data_map.get("nlu")
    return dict(current) if isinstance(current, dict) else {}


def _normalize_rule(rule: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    intent = rule.get("intent")
    pattern = rule.get("pattern")
    if not isinstance(intent, str) or not intent.strip():
        return None
    if not isinstance(pattern, str) or not pattern.strip():
        return None
    enabled = rule.get("enabled")
    return {
        "id": rule.get("id"),
        "created_at": rule.get("created_at"),
        "intent": intent.strip(),
        "pattern": pattern,
        "enabled": bool(enabled) if enabled is not None else True,
        "source": rule.get("source"),
    }


@subscribe("nlp.teacher.regex_rule.apply")
async def _on_regex_rule_apply(evt: Any) -> None:
    """
    Apply a proposed regex rule (typically from NLU Teacher UI).

    Payload:
      - webspace_id
      - candidate_id (optional)
      - intent
      - pattern
      - _meta
    """
    ctx = get_ctx()
    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)

    candidate_id = payload.get("candidate_id")
    intent = payload.get("intent")
    pattern = payload.get("pattern")
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), Mapping) else {}

    if not isinstance(intent, str) or not intent.strip():
        return
    if not isinstance(pattern, str) or not pattern.strip():
        return

    rule_id = f"rx.{int(time.time() * 1000)}"
    rule = {
        "id": rule_id,
        "created_at": time.time(),
        "intent": intent.strip(),
        "pattern": pattern,
        "enabled": True,
        "source": "teacher",
        "candidate_id": candidate_id if isinstance(candidate_id, str) else None,
    }
    request_id: str | None = None
    request_text: str = ""

    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")

            # Store under data.nlu.regex_rules
            nlu_obj = _read_nlu_obj(data_map)
            rules = nlu_obj.get("regex_rules")
            if not isinstance(rules, list):
                rules = []
            cleaned: list[dict[str, Any]] = []
            for item in rules:
                if not isinstance(item, Mapping):
                    continue
                normalized = _normalize_rule(item)
                if normalized:
                    cleaned.append(normalized)
            cleaned.append(rule)
            nlu_obj["regex_rules"] = cleaned[-200:]

            # Mark candidate as applied (if present)
            teacher = _teacher_obj(data_map)
            candidates = teacher.get("candidates")
            if isinstance(candidates, list) and isinstance(candidate_id, str) and candidate_id:
                next_candidates: list[dict[str, Any]] = []
                for item in candidates:
                    if not isinstance(item, Mapping):
                        continue
                    d = dict(item)
                    if d.get("id") == candidate_id:
                        request_id = d.get("request_id") if isinstance(d.get("request_id"), str) else None
                        request_text = d.get("text") if isinstance(d.get("text"), str) else ""
                        d["status"] = "applied"
                        d["applied_at"] = time.time()
                        d["applied"] = {"type": "regex_rule", "rule_id": rule_id}
                    next_candidates.append(d)
                teacher["candidates"] = next_candidates

            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu", nlu_obj)
                data_map.set(txn, "nlu_teacher", teacher)
    except Exception:
        _log.warning("failed to apply regex rule webspace=%s intent=%s", webspace_id, intent, exc_info=True)
        return

    try:
        await append_event(
            webspace_id,
            make_event(
                webspace_id=webspace_id,
                request_id=request_id,
                request_text=request_text,
                kind="regex_rule.applied",
                title="Regex rule applied",
                subtitle=f"{intent}".strip(),
                raw=rule,
                meta=meta,
            ),
        )
    except Exception:
        _log.debug("failed to append teacher event (regex_rule.applied) webspace=%s", webspace_id, exc_info=True)

    bus_emit(
        ctx.bus,
        "nlp.teacher.regex_rule.applied",
        {"webspace_id": webspace_id, "rule": rule, "_meta": dict(meta)},
        source="nlu.regex_rules",
    )
