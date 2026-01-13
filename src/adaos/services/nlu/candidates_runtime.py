from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import yaml

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit as bus_emit
from adaos.services.nlu.teacher_events import append_event, make_event
from adaos.services.nlu.ycoerce import coerce_dict, iter_mappings
from adaos.services.yjs.doc import async_get_ydoc
from adaos.services.yjs.webspace import default_webspace_id

_log = logging.getLogger("adaos.nlu.teacher.candidates")


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


def _find_candidate(teacher: Mapping[str, Any], candidate_id: str) -> Optional[dict[str, Any]]:
    candidates = teacher.get("candidates")
    for item in iter_mappings(candidates):
        if item.get("id") == candidate_id:
            return dict(item)
    return None


def _read_current_scenario_id(ydoc: Any) -> str | None:
    try:
        ui_map = ydoc.get_map("ui")
        token = ui_map.get("current_scenario")
    except Exception:
        return None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _extract_callskill_targets_for_intent(*, scenario_id: str, intent: str) -> list[str]:
    try:
        from adaos.services.scenarios import loader as scenarios_loader  # local import to avoid cycles

        content = scenarios_loader.read_content(scenario_id)
    except Exception:
        return []
    if not isinstance(content, dict):
        return []
    nlu = content.get("nlu")
    if not isinstance(nlu, dict):
        return []
    intents = nlu.get("intents")
    if not isinstance(intents, dict):
        return []
    spec = intents.get(intent)
    if not isinstance(spec, dict):
        return []
    actions = spec.get("actions")
    if not isinstance(actions, list):
        return []
    out: list[str] = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        if a.get("type") != "callSkill":
            continue
        target = a.get("target")
        if isinstance(target, str) and target.strip():
            out.append(target.strip())
    return out


def _find_skill_subscribing_to(topic: str) -> str | None:
    if not isinstance(topic, str) or not topic.strip():
        return None
    ctx = get_ctx()
    skills_dir = Path(ctx.paths.skills_dir())
    try:
        skill_yamls = list(skills_dir.glob("*/skill.yaml"))
    except Exception:
        return None
    for path in skill_yamls:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        events = payload.get("events")
        if not isinstance(events, dict):
            continue
        subs = events.get("subscribe")
        if not isinstance(subs, list):
            continue
        if any(isinstance(x, str) and x.strip() == topic for x in subs):
            return path.parent.name
    return None


@subscribe("nlp.teacher.candidate.apply")
async def _on_candidate_apply(evt: Any) -> None:
    """
    Apply a teacher candidate.

    For now this supports:
    - kind=regex_rule -> delegates to nlp.teacher.regex_rule.apply
    - kind=skill|scenario -> marks as applied and adds into data.nlu_teacher.plan

    Payload:
      - candidate_id
      - webspace_id (optional; falls back to meta/default)
      - _meta (optional; preserved for downstream responses)
    """
    ctx = get_ctx()
    payload = _payload(evt)
    webspace_id = _resolve_webspace_id(payload)
    meta = coerce_dict(payload.get("_meta"))
    payload_target = payload.get("target") if isinstance(payload.get("target"), Mapping) else None

    candidate_id = payload.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        return
    candidate_id = candidate_id.strip()

    candidate: Optional[dict[str, Any]] = None
    request_id: Optional[str] = None
    request_text: str = ""

    try:
        async with async_get_ydoc(webspace_id) as ydoc:
            data_map = ydoc.get_map("data")
            teacher = _teacher_obj(data_map)

            candidate = _find_candidate(teacher, candidate_id)
            if not candidate:
                return

            request_id = candidate.get("request_id") if isinstance(candidate.get("request_id"), str) else None
            request_text = candidate.get("text") if isinstance(candidate.get("text"), str) else ""

            kind = candidate.get("kind")
            if kind == "regex_rule":
                rr = candidate.get("regex_rule") if isinstance(candidate.get("regex_rule"), Mapping) else {}
                intent = rr.get("intent")
                pattern = rr.get("pattern")
                if isinstance(intent, str) and intent.strip() and isinstance(pattern, str) and pattern.strip():
                    target: dict[str, Any] | None = None
                    # UI override has the highest priority.
                    if isinstance(payload_target, Mapping):
                        t_type = payload_target.get("type")
                        t_id = payload_target.get("id")
                        if isinstance(t_type, str) and isinstance(t_id, str) and t_type.strip() and t_id.strip():
                            target = {"type": t_type.strip(), "id": t_id.strip()}

                    # If the candidate already carries a preferred target, keep it.
                    if target is None:
                        cand_target = candidate.get("target") if isinstance(candidate.get("target"), Mapping) else None
                        if isinstance(cand_target, Mapping):
                            t_type = cand_target.get("type")
                            t_id = cand_target.get("id")
                            if isinstance(t_type, str) and isinstance(t_id, str) and t_type.strip() and t_id.strip():
                                target = {"type": t_type.strip(), "id": t_id.strip()}

                    scenario_id = _read_current_scenario_id(ydoc)
                    if target is None and scenario_id:
                        # Prefer attaching regex rules to the skill that actually handles the intent,
                        # so they survive scenario tweaks and remain reusable.
                        for call_target in _extract_callskill_targets_for_intent(scenario_id=scenario_id, intent=intent.strip()):
                            skill = _find_skill_subscribing_to(call_target)
                            if skill:
                                target = {"type": "skill", "id": skill}
                                break

                        # Fallback: scenario itself owns the intent mapping.
                        if target is None:
                            try:
                                from adaos.services.scenarios import loader as scenarios_loader  # local import to avoid cycles

                                content = scenarios_loader.read_content(scenario_id)
                                intents = (content.get("nlu") or {}).get("intents") if isinstance(content, dict) else None
                                if isinstance(intents, dict) and intent.strip() in intents:
                                    target = {"type": "scenario", "id": scenario_id}
                            except Exception:
                                target = None
                    bus_emit(
                        ctx.bus,
                        "nlp.teacher.regex_rule.apply",
                        {
                            "webspace_id": webspace_id,
                            "candidate_id": candidate_id,
                            "intent": intent.strip(),
                            "pattern": pattern,
                            **({"target": target} if target else {}),
                            "_meta": dict(meta),
                        },
                        source="nlu.teacher.candidates",
                    )
                return

            if kind not in {"skill", "scenario"}:
                return

            # mark applied
            next_candidates: list[dict[str, Any]] = []
            for item in iter_mappings(teacher.get("candidates")):
                d = dict(item)
                if d.get("id") == candidate_id:
                    d["status"] = "applied"
                    d["applied_at"] = time.time()
                    d["applied"] = {"type": "plan"}
                next_candidates.append(d)
            teacher["candidates"] = next_candidates

            # add to plan
            plan = teacher.get("plan")
            plan = [dict(x) for x in iter_mappings(plan)]
            plan_item = {
                "id": f"plan.{int(time.time() * 1000)}",
                "ts": time.time(),
                "status": "pending",
                "candidate_id": candidate_id,
                "kind": kind,
                "request_id": request_id,
                "text": request_text,
                "candidate": coerce_dict(candidate.get("candidate")),
                "notes": candidate.get("notes"),
            }
            plan.append(plan_item)
            teacher["plan"] = plan[-200:]

            with ydoc.begin_transaction() as txn:
                data_map.set(txn, "nlu_teacher", teacher)
    except Exception:
        _log.warning("failed to apply candidate webspace=%s candidate_id=%s", webspace_id, candidate_id, exc_info=True)
        return

    try:
        await append_event(
            webspace_id,
            make_event(
                webspace_id=webspace_id,
                request_id=request_id,
                request_text=request_text,
                kind="candidate.applied",
                title="Candidate applied",
                subtitle=str((candidate or {}).get("kind") or ""),
                raw={"candidate_id": candidate_id, "candidate": candidate},
                meta=meta,
            ),
        )
    except Exception:
        _log.debug("failed to append teacher event (candidate.applied) webspace=%s", webspace_id, exc_info=True)

    bus_emit(
        ctx.bus,
        "nlp.teacher.candidate.applied",
        {"webspace_id": webspace_id, "candidate": candidate, "_meta": dict(meta)},
        source="nlu.teacher.candidates",
    )
