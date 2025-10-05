"""Persistence helpers for Root CLI device state."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import json
import os

__all__ = ["RootCliState", "load_cli_state", "save_cli_state", "state_path"]


@dataclass(slots=True)
class RootCliState:
    """Minimal persisted context for the Root CLI device."""

    device_id: str
    subnet_id: str
    thumbprint: str
    private_key_pem: str
    public_key_pem: str
    created_at: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "RootCliState":
        return cls(
            device_id=str(data.get("device_id", "")),
            subnet_id=str(data.get("subnet_id", "")),
            thumbprint=str(data.get("thumbprint", "")),
            private_key_pem=str(data.get("private_key_pem", "")),
            public_key_pem=str(data.get("public_key_pem", "")),
            created_at=str(data.get("created_at", "")),
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def state_path(base_dir: Path) -> Path:
    return base_dir / "root-cli" / "state.json"


def load_cli_state(base_dir: Path) -> RootCliState | None:
    path = state_path(base_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    state = RootCliState.from_mapping(data)
    if not state.device_id or not state.subnet_id or not state.thumbprint:
        return None
    return state


def save_cli_state(base_dir: Path, state: RootCliState) -> Path:
    path = state_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.as_json()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except PermissionError:
        pass
    return path
