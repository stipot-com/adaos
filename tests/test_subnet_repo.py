from __future__ import annotations

from pathlib import Path

from adaos.adapters.db.sqlite_store import SQLite
from adaos.services.registry.subnet_repo import SubnetRepo


class _FakePaths:
    def __init__(self, root: Path) -> None:
        self._root = root

    def state_dir(self) -> Path:
        return self._root


def test_touch_heartbeat_does_not_rewrite_unchanged_capacity(tmp_path: Path) -> None:
    sql = SQLite(_FakePaths(tmp_path))
    repo = SubnetRepo(sql)
    repo.upsert_node(
        {
            "node_id": "member-1",
            "subnet_id": "alpha",
            "roles": ["member"],
            "hostname": "member-1",
            "base_url": "http://member-1.local",
            "node_state": "ready",
            "last_seen": 1.0,
        }
    )
    capacity = {
        "io": [{"io_type": "webrtc_media", "capabilities": ["webrtc:av"], "priority": 60}],
        "skills": [{"name": "voice_chat_skill", "version": "1.0.0", "active": True}],
        "scenarios": [{"name": "web_desktop", "version": "1.0.0", "active": True}],
    }
    repo.replace_io_capacity("member-1", capacity["io"])
    repo.replace_skill_capacity("member-1", capacity["skills"])
    repo.replace_scenario_capacity("member-1", capacity["scenarios"])

    before = {
        "io": repo.io_for_node("member-1"),
        "skills": repo.skills_for_node("member-1"),
        "scenarios": repo.scenarios_for_node("member-1"),
    }

    repo.touch_heartbeat("member-1", 2.0, capacity, node_state="ready")

    after = {
        "io": repo.io_for_node("member-1"),
        "skills": repo.skills_for_node("member-1"),
        "scenarios": repo.scenarios_for_node("member-1"),
    }
    assert after == before


def test_touch_heartbeat_rewrites_capacity_when_materially_changed(tmp_path: Path) -> None:
    sql = SQLite(_FakePaths(tmp_path))
    repo = SubnetRepo(sql)
    repo.upsert_node(
        {
            "node_id": "member-1",
            "subnet_id": "alpha",
            "roles": ["member"],
            "hostname": "member-1",
            "base_url": "http://member-1.local",
            "node_state": "ready",
            "last_seen": 1.0,
        }
    )
    repo.replace_skill_capacity("member-1", [{"name": "voice_chat_skill", "version": "1.0.0", "active": True}])
    before = repo.skills_for_node("member-1")

    repo.touch_heartbeat(
        "member-1",
        2.0,
        {"skills": [{"name": "voice_chat_skill", "version": "1.1.0", "active": True}]},
        node_state="ready",
    )

    after = repo.skills_for_node("member-1")
    assert after != before
    assert after[0]["version"] == "1.1.0"
