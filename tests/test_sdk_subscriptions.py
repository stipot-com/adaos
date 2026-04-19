from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from adaos.sdk.core import decorators
from adaos.services.workspace_registry import write_workspace_registry


def test_subscription_log_suffix_includes_activation_strategy(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infrascope_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "name: Infrascope",
                "version: '0.9.0'",
                "runtime:",
                "  activation:",
                "    mode: lazy",
                "    startup_allowed: false",
                "    background_refresh: false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    write_workspace_registry(
        workspace,
        {
            "version": 1,
            "updated_at": "2026-04-19T00:00:00+00:00",
            "skills": [
                {
                    "kind": "skill",
                    "id": "infrascope_skill",
                    "name": "infrascope_skill",
                    "path": "skills/infrascope_skill",
                    "source": {"path": "skills/infrascope_skill", "manifest": "skills/infrascope_skill/skill.yaml"},
                    "activation": {
                        "mode": "lazy",
                        "startup_allowed": False,
                        "background_refresh": False,
                    },
                }
            ],
            "scenarios": [],
        },
    )

    monkeypatch.setattr(
        decorators,
        "require_ctx",
        lambda _reason: SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace)),
    )

    suffix = decorators._subscription_log_suffix("infrascope_skill")

    assert suffix == " activation=lazy subscription_strategy=early_cheap_handlers"


def test_subscription_log_suffix_is_empty_for_unknown_skill() -> None:
    assert decorators._subscription_log_suffix("<unknown>") == ""
