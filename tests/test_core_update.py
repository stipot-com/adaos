from __future__ import annotations

from adaos.services.core_update import (
    clear_plan,
    configured_update_command,
    read_plan,
    read_status,
    write_plan,
    write_status,
)


def test_core_update_plan_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    clear_plan()
    payload = {"target_rev": "rev2026", "expires_at": 9999999999.0}
    write_plan(payload)
    assert read_plan()["target_rev"] == "rev2026"


def test_core_update_command_formats_placeholders(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("ADAOS_CORE_UPDATE_CMD", "echo {target_rev} {target_version} {base_dir}")
    cmd = configured_update_command({"target_rev": "rev2026", "target_version": "1.2.3"})
    assert cmd is not None
    assert "rev2026" in cmd
    assert "1.2.3" in cmd
    assert str(tmp_path) in cmd


def test_core_update_status_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    write_status({"state": "countdown", "message": "scheduled"})
    assert read_status()["state"] == "countdown"
