from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from adaos.services.skill.context import SkillContextService
from adaos.services.workspace_registry import write_workspace_registry


class _SkillCtx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def set(self, name: str, path: Path) -> bool:
        self.calls.append((name, path))
        return path.exists()

    def clear(self) -> None:
        return None

    def get(self):
        return None


class _ExplodingRepo:
    def get(self, name: str):
        raise AssertionError("skills_repo.get should not be used when registry metadata is available")


def test_set_current_skill_prefers_workspace_registry(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "weather_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text("id: weather_skill\nversion: '1.0.0'\n", encoding="utf-8")

    write_workspace_registry(
        workspace,
        {
            "version": 1,
            "updated_at": "2026-04-18T00:00:00+00:00",
            "skills": [
                {
                    "kind": "skill",
                    "id": "weather_skill",
                    "name": "weather_skill",
                    "path": "skills/weather_skill",
                    "source": {"path": "skills/weather_skill", "manifest": "skills/weather_skill/skill.yaml"},
                }
            ],
            "scenarios": [],
        },
    )

    skill_ctx = _SkillCtx()
    service = SkillContextService(
        ctx=SimpleNamespace(
            paths=SimpleNamespace(workspace_dir=lambda: workspace),
            skill_ctx=skill_ctx,
            skills_repo=_ExplodingRepo(),
        )
    )

    assert service.set_current_skill("weather_skill") is True
    assert skill_ctx.calls == [("weather_skill", skill_dir.resolve())]
