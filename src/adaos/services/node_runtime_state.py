from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from adaos.services.runtime_paths import current_state_dir


_UNSET = object()


def _state_path() -> Path:
    path = (current_state_dir() / "node_runtime.json").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def runtime_state_mtime_ns() -> int | None:
    path = _state_path()
    try:
        if not path.exists():
            return None
        return int(path.stat().st_mtime_ns)
    except Exception:
        return None


def load_node_runtime_state() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        raw = {}
    return dict(raw) if isinstance(raw, dict) else {}


def _clear_node_config_cache() -> None:
    with contextlib.suppress(Exception):
        import adaos.services.node_config as node_config_mod

        node_config_mod._NODE_CONFIG_CACHE.clear()


def save_node_runtime_state(
    *,
    hub_url: str | None | object = _UNSET,
    token: str | None | object = _UNSET,
    nats: dict[str, Any] | None | object = _UNSET,
    node_display: dict[str, Any] | None | object = _UNSET,
) -> dict[str, Any]:
    payload = load_node_runtime_state()
    if hub_url is not _UNSET:
        value = str(hub_url or "").strip()
        if value:
            payload["hub_url"] = value
        else:
            payload.pop("hub_url", None)
    if token is not _UNSET:
        value = str(token or "").strip()
        if value:
            payload["token"] = value
        else:
            payload.pop("token", None)
    if nats is not _UNSET:
        if isinstance(nats, dict) and nats:
            payload["nats"] = dict(nats)
        else:
            payload.pop("nats", None)
    if node_display is not _UNSET:
        if isinstance(node_display, dict) and node_display:
            payload["node_display"] = dict(node_display)
        else:
            payload.pop("node_display", None)
    payload["updated_at"] = time.time()
    path = _state_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _clear_node_config_cache()
    return dict(payload)


def load_node_display_runtime_state() -> dict[str, Any]:
    payload = load_node_runtime_state()
    node_display = payload.get("node_display")
    return dict(node_display) if isinstance(node_display, dict) else {}


def load_nats_runtime_config() -> dict[str, Any]:
    payload = load_node_runtime_state()
    nats = payload.get("nats")
    return dict(nats) if isinstance(nats, dict) else {}


def save_nats_runtime_config(
    *,
    ws_url: str | None = None,
    user: str | None = None,
    password: str | None = None,
    alias: str | None | object = _UNSET,
) -> dict[str, Any]:
    current = load_nats_runtime_config()
    next_payload = dict(current)
    ws_value = str(ws_url or "").strip()
    user_value = str(user or "").strip()
    pass_value = str(password or "").strip()
    if ws_value:
        next_payload["ws_url"] = ws_value
    else:
        next_payload.pop("ws_url", None)
    if user_value:
        next_payload["user"] = user_value
    else:
        next_payload.pop("user", None)
    if pass_value:
        next_payload["pass"] = pass_value
    else:
        next_payload.pop("pass", None)
    if alias is not _UNSET:
        alias_value = str(alias or "").strip()
        if alias_value:
            next_payload["alias"] = alias_value
        else:
            next_payload.pop("alias", None)
    save_node_runtime_state(nats=next_payload or None)
    return next_payload


def migrate_legacy_nats_runtime_config(*, base_dir: Path | None = None, clear_legacy: bool = True) -> dict[str, Any]:
    try:
        from adaos.services.capacity import _load_node_yaml, _save_node_yaml
    except Exception:
        return load_nats_runtime_config()

    try:
        payload = _load_node_yaml(base_dir)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return load_nats_runtime_config()

    legacy_nats = payload.get("nats")
    if not isinstance(legacy_nats, dict) or not legacy_nats:
        return load_nats_runtime_config()

    subnet_id = str(
        payload.get("subnet_id")
        or ((payload.get("subnet") or {}).get("id") if isinstance(payload.get("subnet"), dict) else "")
        or ""
    ).strip() or None
    alias = str(legacy_nats.get("alias") or "").strip() or None
    if alias:
        with contextlib.suppress(Exception):
            from adaos.services.subnet_alias import load_subnet_alias, save_subnet_alias

            if not load_subnet_alias(subnet_id=subnet_id):
                save_subnet_alias(alias, subnet_id=subnet_id)

    current = load_nats_runtime_config()
    current_has_credentials = any(str(current.get(key) or "").strip() for key in ("ws_url", "user", "pass"))
    if not current_has_credentials:
        save_nats_runtime_config(
            ws_url=str(legacy_nats.get("ws_url") or "").strip() or None,
            user=str(legacy_nats.get("user") or "").strip() or None,
            password=str(legacy_nats.get("pass") or "").strip() or None,
        )
    if clear_legacy:
        next_payload = dict(payload)
        next_payload.pop("nats", None)
        with contextlib.suppress(Exception):
            _save_node_yaml(next_payload, base_dir)
    return load_nats_runtime_config()
