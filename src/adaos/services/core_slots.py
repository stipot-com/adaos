from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from adaos.services.runtime_paths import current_base_dir


SLOTS = ("A", "B")


def _base_dir() -> Path:
    return current_base_dir()


def _slots_root() -> Path:
    root = _base_dir() / "state" / "core_slots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def slot_dir(slot: str) -> Path:
    s = str(slot or "").strip().upper()
    if s not in SLOTS:
        raise ValueError(f"invalid slot: {slot}")
    path = _slots_root() / "slots" / s
    path.mkdir(parents=True, exist_ok=True)
    return path


def slot_manifest_path(slot: str) -> Path:
    return slot_dir(slot) / "manifest.json"


def _marker_path(name: str) -> Path:
    return _slots_root() / name


def _read_text(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return text or None


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value), encoding="utf-8")


def active_slot() -> str | None:
    value = _read_text(_marker_path("active"))
    return value if value in SLOTS else None


def previous_slot() -> str | None:
    value = _read_text(_marker_path("previous"))
    return value if value in SLOTS else None


def choose_inactive_slot() -> str:
    active = active_slot()
    if active == "A":
        return "B"
    return "A"


def read_slot_manifest(slot: str) -> dict[str, Any] | None:
    try:
        data = json.loads(slot_manifest_path(slot).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _slot_expected_paths(slot: str, manifest: dict[str, Any] | None) -> tuple[Path, Path]:
    root = slot_dir(slot)
    repo_dir = root / "repo"
    venv_dir = root / "venv"
    if isinstance(manifest, dict):
        repo_raw = str(manifest.get("repo_dir") or "").strip()
        venv_raw = str(manifest.get("venv_dir") or "").strip()
        if repo_raw:
            repo_dir = Path(repo_raw).expanduser().resolve()
        if venv_raw:
            venv_dir = Path(venv_raw).expanduser().resolve()
    return repo_dir, venv_dir


def validate_slot_structure(slot: str) -> dict[str, Any]:
    slot_name = str(slot or "").strip().upper()
    root = slot_dir(slot_name)
    manifest = read_slot_manifest(slot_name)
    repo_dir, venv_dir = _slot_expected_paths(slot_name, manifest)
    issues: list[str] = []
    nested_slot_dir = root / slot_name
    if nested_slot_dir.exists():
        issues.append(f"nested_slot_dir:{nested_slot_dir}")
    if manifest is None:
        issues.append("missing_manifest")
    elif str(manifest.get("slot") or "").strip().upper() != slot_name:
        issues.append(f"manifest_slot_mismatch:{manifest.get('slot')}")
    if not repo_dir.exists():
        issues.append(f"missing_repo_dir:{repo_dir}")
    elif not _path_is_within(repo_dir, root):
        issues.append(f"repo_dir_outside_slot:{repo_dir}")
    if not venv_dir.exists():
        issues.append(f"missing_venv_dir:{venv_dir}")
    elif not _path_is_within(venv_dir, root):
        issues.append(f"venv_dir_outside_slot:{venv_dir}")
    app_entry = repo_dir / "src" / "adaos" / "apps" / "autostart_runner.py"
    if repo_dir.exists() and not app_entry.exists():
        issues.append(f"missing_runtime_entry:{app_entry}")
    if venv_dir.exists():
        python_path = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python_path.exists():
            issues.append(f"missing_venv_python:{python_path}")
    return {
        "slot": slot_name,
        "path": str(root),
        "manifest_present": isinstance(manifest, dict),
        "manifest_path": str(slot_manifest_path(slot_name)),
        "repo_dir": str(repo_dir),
        "venv_dir": str(venv_dir),
        "issues": issues,
        "ok": not issues,
    }


def write_slot_manifest(slot: str, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = dict(payload)
    manifest["slot"] = str(slot).upper()
    path = slot_manifest_path(slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def activate_slot(slot: str) -> None:
    s = str(slot).upper()
    if s not in SLOTS:
        raise ValueError(f"invalid slot: {slot}")
    current = active_slot()
    if current and current != s:
        _write_text(_marker_path("previous"), current)
    _write_text(_marker_path("active"), s)


def rollback_to_previous_slot() -> str | None:
    prev = previous_slot()
    if not prev:
        return None
    activate_slot(prev)
    return prev


def slot_status() -> dict[str, Any]:
    out: dict[str, Any] = {
        "active_slot": active_slot(),
        "previous_slot": previous_slot(),
        "slots": {},
    }
    slots: dict[str, Any] = {}
    for slot in SLOTS:
        slots[slot] = {
            "manifest": read_slot_manifest(slot),
            "path": str(slot_dir(slot)),
            "structure": validate_slot_structure(slot),
        }
    out["slots"] = slots
    return out


def active_slot_manifest() -> dict[str, Any] | None:
    current = active_slot()
    if not current:
        return None
    return read_slot_manifest(current)
