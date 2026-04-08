from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from adaos.adapters.db import SqliteScenarioRegistry, SqliteSkillRegistry
from adaos.services.workspace_sync import reconcile_workspace_db_to_materialized
from adaos.services.workspace_registry import (
    list_workspace_registry_entries,
    rebuild_workspace_registry,
    registry_pattern_set,
    upsert_workspace_registry_entry,
    workspace_registry_path,
)


def test_rebuild_workspace_registry_reads_skill_and_scenario_manifests(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "weather_skill"
    scenario_dir = workspace / "scenarios" / "greet_on_boot"
    skill_dir.mkdir(parents=True)
    scenario_dir.mkdir(parents=True)

    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "id: weather_skill",
                "name: Weather",
                "version: '1.2.3'",
                "description: Forecast provider",
                "entry: handlers/main.py",
                "runtime:",
                "  python: '3.11'",
                "tools:",
                "  - name: weather.get",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (scenario_dir / "scenario.json").write_text(
        json.dumps(
            {
                "id": "greet_on_boot",
                "name": "Greeting",
                "version": "0.4.0",
                "trigger": "manual",
                "io": {"input": ["text"], "output": ["text", "voice"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = rebuild_workspace_registry(workspace)

    assert payload["version"] == 1
    assert payload["skills"][0]["name"] == "weather_skill"
    assert payload["skills"][0]["id"] == "weather_skill"
    assert payload["skills"][0]["title"] == "Weather"
    assert payload["skills"][0]["runtime_python"] == "3.11"
    assert payload["skills"][0]["tools_count"] == 1
    assert payload["skills"][0]["install"]["kind"] == "skill"
    assert payload["scenarios"][0]["name"] == "greet_on_boot"
    assert payload["scenarios"][0]["id"] == "greet_on_boot"
    assert payload["scenarios"][0]["trigger"] == "manual"
    assert payload["scenarios"][0]["io"]["output"] == ["text", "voice"]


def test_upsert_workspace_registry_entry_preserves_existing_entries(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "weather_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "name: Weather",
                "version: '2.0.0'",
                "description: Fresh forecast",
                "",
            ]
        ),
        encoding="utf-8",
    )
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_registry_path(workspace).write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-03-06T00:00:00+00:00",
                "skills": [{"kind": "skill", "name": "alarm_skill", "version": "1.0.0"}],
                "scenarios": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    entry = upsert_workspace_registry_entry(
        workspace,
        "skills",
        skill_dir,
        version="2.0.0",
        updated_at="2026-03-06T10:00:00+00:00",
    )

    assert entry["name"] == "weather_skill"
    items = list_workspace_registry_entries(workspace, kind="skills", fallback_to_scan=False)
    names = [item["name"] for item in items]
    assert names == ["alarm_skill", "weather_skill"]
    assert items[1]["version"] == "2.0.0"


def test_registry_entry_includes_tags_and_publisher(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "infra_skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        "\n".join(
            [
                "id: infra_skill",
                "name: Infra",
                "version: '1.0.0'",
                "tags:",
                "  - infra",
                "  - ops",
                "publisher:",
                "  owner_id: owner-1",
                "",
            ]
        ),
        encoding="utf-8",
    )

    entry = upsert_workspace_registry_entry(
        workspace,
        "skills",
        skill_dir,
        extra={"publisher": {"owner_id": "owner-1", "node_id": "hub-1"}},
    )

    assert entry["tags"] == ["infra", "ops"]
    assert entry["publisher"]["owner_id"] == "owner-1"
    assert entry["publisher"]["node_id"] == "hub-1"


def test_registry_pattern_set_keeps_registry_json_first():
    patterns = registry_pattern_set(["skills/weather_skill", "registry.json", "scenarios/greet_on_boot"])
    assert patterns[0] == "registry.json"
    assert patterns.count("registry.json") == 1


class _Sql:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self):
        return sqlite3.connect(self.path)


def test_reconcile_workspace_db_to_materialized_updates_sqlite(tmp_path: Path):
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "weather_skill"
    scenario_dir = workspace / "scenarios" / "greet_on_boot"
    skill_dir.mkdir(parents=True, exist_ok=True)
    scenario_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text("id: weather_skill\nversion: '1.2.3'\n", encoding="utf-8")
    (scenario_dir / "scenario.yaml").write_text("id: greet_on_boot\nversion: '0.4.0'\n", encoding="utf-8")

    sql = _Sql(tmp_path / "adaos.db")
    skill_registry = SqliteSkillRegistry(sql)
    scenario_registry = SqliteScenarioRegistry(sql)
    skill_registry.register("ghost_skill", active_version="9.9.9")
    scenario_registry.register("ghost_scene", active_version="8.8.8")

    ctx = SimpleNamespace(paths=SimpleNamespace(workspace_dir=lambda: workspace), sql=sql)

    result = reconcile_workspace_db_to_materialized(ctx)

    assert result["ok"] is True
    assert result["skills"] == ["weather_skill"]
    assert result["scenarios"] == ["greet_on_boot"]
    assert result["skills_removed"] == ["ghost_skill"]
    assert result["scenarios_removed"] == ["ghost_scene"]

    skill_rows = {row.name: row for row in skill_registry.list()}
    scenario_rows = {row.name: row for row in scenario_registry.list()}
    assert list(skill_rows) == ["weather_skill"]
    assert skill_rows["weather_skill"].active_version == "1.2.3"
    assert list(scenario_rows) == ["greet_on_boot"]
    assert scenario_rows["greet_on_boot"].active_version == "0.4.0"
