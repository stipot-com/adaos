# src\adaos\services\skill\context.py
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from adaos.services.agent_context import AgentContext
from adaos.ports.skill_context import CurrentSkill
from adaos.services.workspace_registry import find_workspace_registry_entry


@dataclass(slots=True)
class SkillContextService:
    ctx: AgentContext

    def set_current_skill(self, name: str) -> bool:
        token = str(name or "").strip()
        if not token:
            return False

        entry = find_workspace_registry_entry(
            self.ctx.paths.workspace_dir(),
            kind="skills",
            name_or_id=token,
            fallback_to_scan=True,
        )
        if isinstance(entry, dict):
            rel_path = str((entry.get("source") or {}).get("path") or entry.get("path") or "").strip()
            if rel_path:
                skill_path = (self.ctx.paths.workspace_dir() / rel_path).resolve()
                if skill_path.exists():
                    return self.ctx.skill_ctx.set(token, skill_path)

        meta = self.ctx.skills_repo.get(token)
        if not meta:
            return False
        return self.ctx.skill_ctx.set(token, Path(meta.path))

    def clear_current_skill(self) -> None:
        self.ctx.skill_ctx.clear()

    def get_current_skill(self) -> Optional[CurrentSkill]:
        return self.ctx.skill_ctx.get()
