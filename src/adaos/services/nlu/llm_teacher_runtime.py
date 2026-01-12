from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Mapping, Optional

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.root.client import RootHttpClient
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.teacher.llm")

_TEACHER_ENABLED = os.getenv("ADAOS_NLU_TEACHER") == "1"
_LLM_TEACHER_ENABLED = os.getenv("ADAOS_NLU_LLM_TEACHER") == "1"
_MODEL = os.getenv("ADAOS_NLU_LLM_MODEL") or os.getenv("OPENAI_RESPONSES_MODEL") or "gpt-4o-mini"
_MAX_TOKENS = int(os.getenv("ADAOS_NLU_LLM_MAX_TOKENS", "500") or "500")
_TIMEOUT_S = float(os.getenv("ADAOS_NLU_LLM_TIMEOUT_S", "20") or "20")


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


def _extract_webspace_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    ui = snapshot.get("ui") if isinstance(snapshot.get("ui"), dict) else {}
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
    current_scenario = ui.get("current_scenario")
    if not isinstance(current_scenario, str):
        current_scenario = None

    catalog = data.get("catalog") if isinstance(data.get("catalog"), dict) else {}
    apps = catalog.get("apps") if isinstance(catalog.get("apps"), list) else []
    widgets = catalog.get("widgets") if isinstance(catalog.get("widgets"), list) else []

    installed = data.get("installed") if isinstance(data.get("installed"), dict) else {}
    installed_apps = installed.get("apps") if isinstance(installed.get("apps"), list) else []
    installed_widgets = installed.get("widgets") if isinstance(installed.get("widgets"), list) else []

    def _strip_app(app: Any) -> Optional[dict[str, Any]]:
        if not isinstance(app, dict):
            return None
        out = {
            "id": app.get("id"),
            "title": app.get("title"),
            "scenario_id": app.get("scenario_id"),
            "launchModal": app.get("launchModal"),
            "origin": app.get("origin"),
        }
        return {k: v for k, v in out.items() if v is not None}

    def _strip_widget(w: Any) -> Optional[dict[str, Any]]:
        if not isinstance(w, dict):
            return None
        out = {"id": w.get("id"), "title": w.get("title"), "type": w.get("type"), "origin": w.get("origin")}
        return {k: v for k, v in out.items() if v is not None}

    return {
        "current_scenario": current_scenario,
        "catalog": {
            "apps": [x for x in (_strip_app(a) for a in apps) if x],
            "widgets": [x for x in (_strip_widget(w) for w in widgets) if x],
        },
        "installed": {
            "apps": [x for x in installed_apps if isinstance(x, (str, int))],
            "widgets": [x for x in installed_widgets if isinstance(x, (str, int))],
        },
    }


def _extract_first_output_text(res: Any) -> str:
    """
    Root /v1/llm/response returns OpenAI Responses API payload.
    Try to extract the first text output robustly.
    """
    if isinstance(res, dict):
        out = res.get("output")
        if isinstance(out, list):
            for item in out:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") in {"output_text", "text"}:
                        t = c.get("text")
                        if isinstance(t, str) and t.strip():
                            return t.strip()
        # Some proxies might return {choices:[{message:{content:"..."}}]}
        choices = res.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    return ""


def _truncate(text: Any, limit: int) -> Any:
    if not isinstance(text, str):
        return text
    s = text.strip()
    if len(s) <= limit:
        return s
    return s[:limit] + f"... <truncated {len(s) - limit} chars>"


def _redact_messages(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        out.append(
            {
                "role": m.get("role"),
                "content": _truncate(m.get("content"), 1200),
            }
        )
    return out


def _build_prompt(*, utterance: str, webspace_id: str, context: dict[str, Any]) -> list[dict[str, str]]:
    system = (
        "You are AdaOS NLU teacher. Decide what to do with a user utterance.\n"
        "You must return ONLY valid JSON (no markdown).\n\n"
        "Output schema:\n"
        "{\n"
        '  \"decision\": \"revise_nlu\" | \"create_skill_candidate\" | \"create_scenario_candidate\" | \"ignore\",\n'
        '  \"intent\": string|null,\n'
        '  \"examples\": string[],\n'
        '  \"slots\": object,  // e.g. {\"city\": {\"type\": \"string\"}}\n'
        '  \"confidence\": number, // 0..1\n'
        '  \"notes\": string,\n'
        '  \"candidate\": object|null\n'
        "}\n\n"
        "Rules:\n"
        "- If the utterance is not actionable for AdaOS, decision=ignore.\n"
        "- If it matches a known app/widget/scenario, prefer revise_nlu and propose an intent name.\n"
        "- If it suggests a new capability, propose create_skill_candidate or create_scenario_candidate.\n"
        "- Keep intent names short and namespaced (e.g. desktop.open_weather, smalltalk.how_are_you).\n"
    )
    user = {
        "webspace_id": webspace_id,
        "utterance": utterance,
        "context": context,
    }
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


async def _llm_call(messages: list[dict[str, str]]) -> dict[str, Any]:
    ctx = get_ctx()
    http = RootHttpClient.from_settings(ctx.settings)
    body = {"model": _MODEL, "messages": messages, "max_tokens": _MAX_TOKENS, "temperature": 0.2}
    return await asyncio.to_thread(http.request, "POST", "/v1/llm/response", json=body, timeout=_TIMEOUT_S)


async def _append_llm_log(webspace_id: str, entry: dict[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        teacher = _teacher_obj(data_map)
        logs = teacher.get("llm_logs")
        if not isinstance(logs, list):
            logs = []
        logs = [x for x in logs if isinstance(x, dict)]
        logs.append(entry)
        teacher["llm_logs"] = logs[-300:]
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)


async def _patch_llm_log(webspace_id: str, *, log_id: str, patch: dict[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        teacher = _teacher_obj(data_map)
        logs = teacher.get("llm_logs")
        if not isinstance(logs, list):
            return
        next_logs: list[dict[str, Any]] = []
        for item in logs:
            if not isinstance(item, dict):
                continue
            if item.get("id") == log_id:
                updated = dict(item)
                updated.update(patch)
                next_logs.append(updated)
            else:
                next_logs.append(item)
        teacher["llm_logs"] = next_logs[-300:]
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)


async def _update_revision_by_request_id(
    webspace_id: str,
    *,
    request_id: str,
    patch: dict[str, Any],
) -> Optional[dict[str, Any]]:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        teacher = _teacher_obj(data_map)
        revisions = teacher.get("revisions")
        if not isinstance(revisions, list):
            return None
        updated: Optional[dict[str, Any]] = None
        cleaned: list[dict[str, Any]] = []
        for item in revisions:
            if not isinstance(item, dict):
                continue
            if item.get("request_id") == request_id and item.get("status") in {"pending", "proposed"}:
                updated = dict(item)
                updated.update(patch)
                cleaned.append(updated)
            else:
                cleaned.append(item)
        teacher["revisions"] = cleaned
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)
        return updated


async def _append_candidate(webspace_id: str, candidate: dict[str, Any]) -> None:
    async with async_get_ydoc(webspace_id) as ydoc:
        data_map = ydoc.get_map("data")
        teacher = _teacher_obj(data_map)
        candidates = teacher.get("candidates")
        if not isinstance(candidates, list):
            candidates = []
        candidates = [x for x in candidates if isinstance(x, dict)]
        candidates.append(candidate)
        teacher["candidates"] = candidates[-200:]
        with ydoc.begin_transaction() as txn:
            data_map.set(txn, "nlu_teacher", teacher)


@subscribe("nlp.teacher.request")
async def _on_teacher_request(evt: Any) -> None:
    if not (_TEACHER_ENABLED and _LLM_TEACHER_ENABLED):
        return

    ctx = get_ctx()
    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)
    req = payload.get("request") if isinstance(payload.get("request"), Mapping) else None
    if not req:
        return
    req_meta = req.get("_meta") if isinstance(req.get("_meta"), Mapping) else {}

    text = req.get("text")
    request_id = req.get("request_id")
    if not isinstance(text, str) or not text.strip():
        return
    if not isinstance(request_id, str) or not request_id.strip():
        return
    text = text.strip()
    request_id = request_id.strip()

    # Build lightweight context snapshot for LLM.
    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            snapshot = ydoc.to_json()
    except Exception:
        snapshot = {}
    context = _extract_webspace_context(snapshot if isinstance(snapshot, dict) else {})

    messages = _build_prompt(utterance=text, webspace_id=webspace_id, context=context)

    log_id = f"llm.{int(time.time() * 1000)}"
    started_at = time.time()
    try:
        await _append_llm_log(
            webspace_id,
            {
                "id": log_id,
                "ts": started_at,
                "request_id": request_id,
                "webspace_id": webspace_id,
                "model": _MODEL,
                "request": {
                    "messages": _redact_messages(messages),
                    "max_tokens": _MAX_TOKENS,
                    "timeout_s": _TIMEOUT_S,
                },
                "status": "request",
            },
        )
    except Exception:
        _log.debug("failed to append llm log webspace=%s", webspace_id, exc_info=True)

    try:
        res = await _llm_call(messages)
    except Exception as exc:
        _log.warning("llm teacher call failed: %s", exc)
        try:
            await _patch_llm_log(
                webspace_id,
                log_id=log_id,
                patch={
                    "status": "error",
                    "error": str(exc),
                    "duration_s": max(0.0, time.time() - started_at),
                },
            )
        except Exception:
            _log.debug("failed to patch llm log webspace=%s", webspace_id, exc_info=True)
        return

    raw_text = _extract_first_output_text(res)
    if not raw_text:
        _log.warning("llm teacher returned empty output")
        try:
            await _patch_llm_log(
                webspace_id,
                log_id=log_id,
                patch={
                    "status": "error",
                    "error": "empty_output",
                    "response": {"raw": None},
                    "duration_s": max(0.0, time.time() - started_at),
                },
            )
        except Exception:
            _log.debug("failed to patch llm log webspace=%s", webspace_id, exc_info=True)
        return

    try:
        suggestion = json.loads(raw_text)
    except Exception:
        # Fallback: store raw in revision note.
        suggestion = {"decision": "ignore", "notes": raw_text, "confidence": 0.0}

    if not isinstance(suggestion, dict):
        suggestion = {"decision": "ignore", "notes": raw_text, "confidence": 0.0}

    try:
        await _patch_llm_log(
            webspace_id,
            log_id=log_id,
            patch={
                "status": "response",
                "response": {
                    "raw": _truncate(raw_text, 4000),
                    "parsed": suggestion,
                },
                "duration_s": max(0.0, time.time() - started_at),
            },
        )
    except Exception:
        _log.debug("failed to patch llm log webspace=%s", webspace_id, exc_info=True)

    decision = suggestion.get("decision") if isinstance(suggestion.get("decision"), str) else "ignore"
    intent = suggestion.get("intent") if isinstance(suggestion.get("intent"), str) else None
    examples = suggestion.get("examples") if isinstance(suggestion.get("examples"), list) else None
    if examples is None:
        examples = [text]
    examples = [x.strip() for x in examples if isinstance(x, str) and x.strip()]
    slots = suggestion.get("slots") if isinstance(suggestion.get("slots"), Mapping) else {}
    confidence = suggestion.get("confidence")
    try:
        confidence_f = float(confidence) if confidence is not None else 0.0
    except Exception:
        confidence_f = 0.0
    notes = suggestion.get("notes") if isinstance(suggestion.get("notes"), str) else ""

    llm_meta = {"model": _MODEL, "ts": time.time(), "decision": decision, "confidence": confidence_f}

    if decision == "revise_nlu" and intent:
        patch = {
            "status": "proposed",
            "proposal": {"intent": intent, "examples": examples, "slots": dict(slots)},
            "llm": llm_meta,
            "note": notes or "LLM proposed NLU revision.",
            "proposed_at": time.time(),
        }
        updated = await _update_revision_by_request_id(webspace_id, request_id=request_id, patch=patch)
        bus_emit(
            ctx.bus,
            "nlp.teacher.revision.suggested",
            {"webspace_id": webspace_id, "request_id": request_id, "revision": updated, "suggestion": suggestion},
            source="nlu.teacher.llm",
        )
        return

    if decision in {"create_skill_candidate", "create_scenario_candidate"}:
        candidate = suggestion.get("candidate") if isinstance(suggestion.get("candidate"), Mapping) else {}
        entry = {
            "id": f"cand.{int(time.time()*1000)}",
            "ts": time.time(),
            "kind": "skill" if decision == "create_skill_candidate" else "scenario",
            "text": text,
            "request_id": request_id,
            "candidate": dict(candidate),
            "llm": llm_meta,
            "notes": notes,
            "status": "pending",
        }
        try:
            await _append_candidate(webspace_id, entry)
        except Exception:
            _log.debug("failed to append candidate webspace=%s", webspace_id, exc_info=True)
        # Include original meta so router can respond in the right UI route (voice_chat/telegram/etc).
        bus_emit(
            ctx.bus,
            "nlp.teacher.candidate.proposed",
            {"webspace_id": webspace_id, "candidate": entry, "_meta": dict(req_meta)},
            source="nlu.teacher.llm",
        )
        return

    # ignore: just annotate revision (if present) so UI can show why.
    patch = {"status": "ignored", "llm": llm_meta, "note": notes or "LLM decided to ignore.", "ignored_at": time.time()}
    await _update_revision_by_request_id(webspace_id, request_id=request_id, patch=patch)
    bus_emit(ctx.bus, "nlp.teacher.ignored", {"webspace_id": webspace_id, "request_id": request_id, "suggestion": suggestion}, source="nlu.teacher.llm")
