from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.nlu.teacher_events import append_event, make_event
from adaos.services.nlu.ycoerce import coerce_dict, iter_mappings
from adaos.services.scenarios import loader as scenarios_loader
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
    meta = coerce_dict(payload.get("_meta"))
    token = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return default_webspace_id()


def _teacher_obj(data_map: Any) -> dict[str, Any]:
    return coerce_dict(getattr(data_map, "get", lambda _k: None)("nlu_teacher"))


def _read_nlu_obj(data_map: Any) -> dict[str, Any]:
    return coerce_dict(getattr(data_map, "get", lambda _k: None)("nlu"))


def _read_current_scenario_id(snapshot: dict[str, Any]) -> str | None:
    ui = coerce_dict(snapshot.get("ui"))
    token = ui.get("current_scenario")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _append_or_update_rule(existing: list[dict[str, Any]], rule: dict[str, Any]) -> list[dict[str, Any]]:
    intent = rule.get("intent")
    pattern = rule.get("pattern")
    if not isinstance(intent, str) or not intent.strip():
        return existing
    if not isinstance(pattern, str) or not pattern.strip():
        return existing

    cleaned: list[dict[str, Any]] = []
    for item in existing:
        if not isinstance(item, dict):
            continue
        if item.get("intent") == intent and item.get("pattern") == pattern:
            updated = dict(item)
            if updated.get("enabled") is None:
                updated["enabled"] = True
            cleaned.append(updated)
        else:
            cleaned.append(dict(item))

    if any(x.get("intent") == intent and x.get("pattern") == pattern for x in cleaned):
        return cleaned
    cleaned.append(rule)
    return cleaned


def _write_scenario_regex_rule(*, scenario_id: str, rule: dict[str, Any]) -> bool:
    root = scenarios_loader.scenario_root(scenario_id)
    path = root / "scenario.json"
    if not path.exists():
        return False

    try:
        raw = path.read_text(encoding="utf-8-sig")
        payload = json.loads(raw)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False

    nlu = payload.get("nlu")
    if not isinstance(nlu, dict):
        nlu = {}
        payload["nlu"] = nlu

    rules = nlu.get("regex_rules")
    if not isinstance(rules, list):
        rules = []
    nlu["regex_rules"] = _append_or_update_rule([dict(x) for x in rules if isinstance(x, dict)], rule)[-200:]

    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        return False

    scenarios_loader.invalidate_cache(scenario_id=scenario_id, space="workspace")
    return True


def _write_skill_regex_rule(*, skill_name: str, rule: dict[str, Any]) -> bool:
    ctx = get_ctx()
    skill_root = Path(ctx.paths.skills_dir()) / skill_name
    path = skill_root / "skill.yaml"
    if not path.exists():
        return False

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False

    nlu = payload.get("nlu")
    if not isinstance(nlu, dict):
        nlu = {}
        payload["nlu"] = nlu

    rules = nlu.get("regex_rules")
    if not isinstance(rules, list):
        rules = []
    nlu["regex_rules"] = _append_or_update_rule([dict(x) for x in rules if isinstance(x, dict)], rule)[-200:]

    try:
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    except Exception:
        return False
    return True


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
    meta = coerce_dict(payload.get("_meta"))

    if not isinstance(intent, str) or not intent.strip():
        return
    if not isinstance(pattern, str) or not pattern.strip():
        return
    try:
        re.compile(pattern)
    except re.error:
        _log.warning("invalid regex pattern intent=%s pattern=%s", intent, pattern)
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
    applied_to: dict[str, Any] | None = None

    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")

            # Prefer writing regex rules into scenario/skill definitions (workspace),
            # so NLU can evolve as part of skills/scenarios rather than per-webspace state.
            target = payload.get("target") if isinstance(payload.get("target"), Mapping) else None
            target_type = target.get("type") if isinstance(target, Mapping) else None
            target_id = target.get("id") if isinstance(target, Mapping) else None

            ui_map = ydoc.get_map("ui")
            token = ui_map.get("current_scenario")
            scenario_id = token.strip() if isinstance(token, str) and token.strip() else None

            applied_ok = False
            if target_type == "scenario" and isinstance(target_id, str) and target_id.strip():
                applied_ok = _write_scenario_regex_rule(scenario_id=target_id.strip(), rule=rule)
                if applied_ok:
                    applied_to = {"type": "scenario", "id": target_id.strip()}
            elif target_type == "skill" and isinstance(target_id, str) and target_id.strip():
                applied_ok = _write_skill_regex_rule(skill_name=target_id.strip(), rule=rule)
                if applied_ok:
                    applied_to = {"type": "skill", "id": target_id.strip()}
            elif scenario_id:
                try:
                    content = scenarios_loader.read_content(scenario_id)
                except Exception:
                    content = {}
                intents = (content.get("nlu") or {}).get("intents") if isinstance(content, dict) else None
                if isinstance(intents, dict) and intent.strip() in intents:
                    applied_ok = _write_scenario_regex_rule(scenario_id=scenario_id, rule=rule)
                    if applied_ok:
                        applied_to = {"type": "scenario", "id": scenario_id}

            if not applied_ok:
                # Backward-compatible fallback: keep per-webspace storage if we can't
                # resolve a skill/scenario target.
                nlu_obj = _read_nlu_obj(data_map)
                rules = nlu_obj.get("regex_rules")
                rules = [dict(x) for x in iter_mappings(rules)]
                cleaned: list[dict[str, Any]] = []
                for item in rules:
                    normalized = _normalize_rule(item)
                    if normalized:
                        cleaned.append(normalized)
                cleaned.append(rule)
                nlu_obj["regex_rules"] = cleaned[-200:]
                with ydoc.begin_transaction() as txn:
                    data_map.set(txn, "nlu", nlu_obj)
                applied_to = {"type": "webspace", "id": webspace_id}

            # Mark candidate as applied (if present)
            teacher = _teacher_obj(data_map)
            candidates = teacher.get("candidates")
            if isinstance(candidate_id, str) and candidate_id:
                next_candidates: list[dict[str, Any]] = []
                for item in iter_mappings(candidates):
                    d = dict(item)
                    if d.get("id") == candidate_id:
                        request_id = d.get("request_id") if isinstance(d.get("request_id"), str) else None
                        request_text = d.get("text") if isinstance(d.get("text"), str) else ""
                        d["status"] = "applied"
                        d["applied_at"] = time.time()
                        d["applied"] = {"type": "regex_rule", "rule_id": rule_id, "target": dict(applied_to or {})}
                    next_candidates.append(d)
                teacher["candidates"] = next_candidates

            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)
    except Exception:
        _log.warning("failed to apply regex rule webspace=%s intent=%s", webspace_id, intent, exc_info=True)
        return

    try:
        from adaos.services.nlu.pipeline import invalidate_dynamic_regex_cache  # local import to avoid cycles

        invalidate_dynamic_regex_cache(webspace_id=webspace_id)
    except Exception:
        pass

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
                raw={**rule, "target": dict(applied_to or {})},
                meta=meta,
            ),
        )
    except Exception:
        _log.debug("failed to append teacher event (regex_rule.applied) webspace=%s", webspace_id, exc_info=True)

    bus_emit(
        ctx.bus,
        "nlp.teacher.regex_rule.applied",
        {"webspace_id": webspace_id, "rule": {**rule, "target": dict(applied_to or {})}, "_meta": dict(meta)},
        source="nlu.regex_rules",
    )
