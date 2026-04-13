from __future__ import annotations

import os
from pathlib import Path

from adaos.services import core_slots


def test_validate_slot_structure_reports_nested_slot_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    slot_root = core_slots.slot_dir("A")
    (slot_root / "A").mkdir(parents=True, exist_ok=True)

    payload = core_slots.validate_slot_structure("A")

    assert payload["ok"] is False
    assert any("nested_slot_dir:" in item for item in payload["issues"])
    assert any(item == "missing_manifest" for item in payload["issues"])


def test_validate_slot_structure_reports_valid_slot(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    slot_root = core_slots.slot_dir("B")
    repo_dir = slot_root / "repo" / "src" / "adaos" / "apps"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "autostart_runner.py").write_text("print('ok')\n", encoding="utf-8")
    python_rel = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    python_path = slot_root / "venv" / python_rel
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text("#!/usr/bin/env python\n", encoding="utf-8")
    core_slots.write_slot_manifest(
        "B",
        {
            "slot": "B",
            "repo_dir": str(slot_root / "repo"),
            "venv_dir": str(slot_root / "venv"),
        },
    )

    payload = core_slots.validate_slot_structure("B")

    assert payload["ok"] is True
    assert payload["issues"] == []


def test_active_slot_prefers_process_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ADAOS_BASE_DIR", str(tmp_path))
    core_slots.activate_slot("A")
    monkeypatch.setenv("ADAOS_ACTIVE_CORE_SLOT", "B")

    assert core_slots.active_slot() == "B"
