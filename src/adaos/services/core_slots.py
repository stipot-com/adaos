from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from adaos.services.agent_context import get_ctx


SLOTS = ("A", "B")


def _base_dir() -> Path:
    try:
        ctx = get_ctx()
        base = ctx.paths.base_dir()
        base = base() if callable(base) else base
        return Path(base).expanduser().resolve()
    except Exception:
        pass
    raw = str(os.getenv("ADAOS_BASE_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".adaos").resolve()


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
        }
    out["slots"] = slots
    return out


def active_slot_manifest() -> dict[str, Any] | None:
    current = active_slot()
    if not current:
        return None
    return read_slot_manifest(current)
