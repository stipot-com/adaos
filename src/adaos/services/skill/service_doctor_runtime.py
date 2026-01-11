from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Mapping

from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.eventbus import emit

_log = logging.getLogger("adaos.skill.service.doctor")


def _payload(evt: Any) -> Dict[str, Any]:
    if isinstance(evt, dict):
        return evt
    if hasattr(evt, "payload"):
        data = getattr(evt, "payload")
        return data if isinstance(data, dict) else {}
    return {}


def _service_state_dir(ctx, skill: str) -> Path:
    state_raw = ctx.paths.state_dir()
    state_dir = Path(state_raw() if callable(state_raw) else state_raw)
    return state_dir / "services" / skill


def _reports_path(ctx, skill: str) -> Path:
    return _service_state_dir(ctx, skill) / "doctor_reports.json"


def _load_reports(ctx, skill: str) -> list[dict[str, Any]]:
    path = _reports_path(ctx, skill)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        pass
    return []


def _persist_reports(ctx, skill: str, items: list[dict[str, Any]]) -> None:
    path = _reports_path(ctx, skill)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _recommendations(issue_type: str | None, *, reason: str) -> list[str]:
    rec: list[str] = []
    if issue_type == "crash_loop":
        rec.extend(
            [
                "Inspect service log tail and full log for the crash reason.",
                "Verify skill dependencies / venv are consistent with the service runtime.",
                "Consider increasing crash cooloff or reducing restart aggressiveness.",
            ]
        )
    elif issue_type == "healthcheck_failed":
        rec.extend(
            [
                "Check if the process is alive and bound to host/port.",
                "Verify health endpoint path and timeout.",
                "If healthcheck is slow but OK, increase timeout_ms or reduce interval.",
            ]
        )
    elif issue_type == "rasa_timeout":
        rec.extend(
            [
                "Increase ADAOS_NLU_RASA_PARSE_TIMEOUT_S (and/or reduce concurrency) if service is slow.",
                "Check Rasa service CPU/RAM usage and service log.",
                "Restart the service if it got stuck; consider model size/featurizers.",
            ]
        )
    elif issue_type == "rasa_failed":
        rec.extend(
            [
                "Check Rasa service log for traceback and missing modules.",
                "Restart the service; validate the venv and pinned dependency versions.",
            ]
        )
    elif issue_type == "hook_failed":
        rec.extend(["Verify hook entrypoint module:function and its dependencies in the service venv."])
    elif issue_type == "hook_timeout":
        rec.extend(["Increase hooks.timeout_s or make hook logic faster/non-blocking."])
    else:
        rec.extend(
            [
                "Inspect service log tail and recent issues.",
                "Try restart; if persistent, add a self-heal hook or doctor integration.",
            ]
        )

    if reason and reason != "issue":
        rec.append(f"Triggered by manual doctor request: reason={reason}")
    return rec


@subscribe("skill.service.doctor.request")
async def _on_doctor_request(evt: Any) -> None:
    ctx = get_ctx()
    payload = _payload(evt)

    skill = payload.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        return
    skill = skill.strip()

    reason = payload.get("reason") if isinstance(payload.get("reason"), str) else "unknown"
    issue = payload.get("issue") if isinstance(payload.get("issue"), Mapping) else None
    issue_type = issue.get("type") if issue and isinstance(issue.get("type"), str) else None

    report: dict[str, Any] = {
        "id": f"rep.{int(time.time()*1000)}",
        "ts": time.time(),
        "skill": skill,
        "reason": reason,
        "issue_type": issue_type,
        "issue": dict(issue) if issue else None,
        "recommendations": _recommendations(issue_type, reason=reason),
        "service": payload.get("service") if isinstance(payload.get("service"), Mapping) else None,
    }

    # Persist report history (bounded).
    items = _load_reports(ctx, skill)
    items.append(report)
    if len(items) > 100:
        del items[: len(items) - 100]
    _persist_reports(ctx, skill, items)

    emit(ctx.bus, "skill.service.doctor.report", {"skill": skill, "report": report}, source="skill.service.doctor")
    _log.info("doctor report created skill=%s issue_type=%s reason=%s", skill, issue_type, reason)

