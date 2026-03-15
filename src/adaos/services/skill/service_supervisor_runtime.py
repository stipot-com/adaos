from __future__ import annotations

import logging
from typing import Any, Dict

from adaos.sdk.core.decorators import subscribe
from adaos.services.skill.service_supervisor import get_service_supervisor

_log = logging.getLogger("adaos.skill.service.runtime")


async def _restart_if_service(skill_name: str | None, *, reason: str) -> None:
    if not skill_name:
        return
    supervisor = get_service_supervisor()
    supervisor.ensure_discovered()
    if skill_name not in supervisor.list():
        return
    try:
        await supervisor.restart(skill_name)
        _log.info("service restarted skill=%s reason=%s", skill_name, reason)
    except Exception:
        _log.warning("failed to restart service skill=%s reason=%s", skill_name, reason, exc_info=True)


@subscribe("skills.activated")
async def _on_skill_activated(payload: Dict[str, Any]) -> None:
    await _restart_if_service(payload.get("skill_name"), reason="skills.activated")


@subscribe("skills.rolledback")
async def _on_skill_rolledback(payload: Dict[str, Any]) -> None:
    await _restart_if_service(payload.get("skill_name"), reason="skills.rolledback")

