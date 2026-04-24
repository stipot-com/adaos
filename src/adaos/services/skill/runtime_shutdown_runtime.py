from __future__ import annotations

import logging
from typing import Any, Dict, Iterable

from adaos.adapters.db import SqliteSkillRegistry
from adaos.sdk.core.decorators import subscribe
from adaos.services.agent_context import get_ctx
from adaos.services.skill.manager import SkillManager

_log = logging.getLogger("adaos.skill.runtime.shutdown")


def _manager() -> SkillManager:
    ctx = get_ctx()
    return SkillManager(
        repo=ctx.skills_repo,
        registry=SqliteSkillRegistry(ctx.sql),
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
    )


def _shutdown_active_skills(*, reason: str, event_type: str, hooks: Iterable[str]) -> dict[str, Any]:
    report = _manager().shutdown_active_runtimes(reason=reason, event_type=event_type, hooks=hooks)
    if report.get("failed_total"):
        _log.warning(
            "skill runtime shutdown hooks reported failures event_type=%s failed_total=%s",
            event_type,
            report.get("failed_total"),
        )
    elif report.get("active_total"):
        _log.info(
            "skill runtime shutdown hooks completed event_type=%s active_total=%s",
            event_type,
            report.get("active_total"),
        )
    return report


@subscribe("subnet.draining")
def _on_subnet_draining(payload: Dict[str, Any]) -> None:
    reason = str((payload or {}).get("reason") or "runtime_draining").strip() or "runtime_draining"
    _shutdown_active_skills(reason=reason, event_type="subnet.draining", hooks=("drain",))


@subscribe("subnet.stopping")
def _on_subnet_stopping(payload: Dict[str, Any]) -> None:
    reason = str((payload or {}).get("reason") or "runtime_stopping").strip() or "runtime_stopping"
    _shutdown_active_skills(reason=reason, event_type="subnet.stopping", hooks=("dispose", "before_deactivate"))
