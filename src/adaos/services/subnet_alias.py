from __future__ import annotations

import re
import time

from adaos.adapters.db.sqlite import durable_state_delete, durable_state_get, durable_state_put


_GENERIC_HUB_ALIAS_RE = re.compile(r"^hub(?:-\d+)?$", re.IGNORECASE)
_ALIAS_NAMESPACE = "subnet_alias"
_ALIAS_KEY = "local"


def display_subnet_alias(alias: str | None, subnet_id: str | None) -> str | None:
    raw_alias = str(alias or "").strip()
    raw_subnet = str(subnet_id or "").strip()
    if raw_alias and not _GENERIC_HUB_ALIAS_RE.fullmatch(raw_alias):
        return raw_alias
    if raw_subnet:
        return raw_subnet
    return raw_alias or None


def _clear_legacy_alias_from_node_yaml() -> None:
    try:
        from adaos.services.capacity import _load_node_yaml, _save_node_yaml
    except Exception:
        return
    try:
        payload = _load_node_yaml()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return
    nats = payload.get("nats")
    if not isinstance(nats, dict) or "alias" not in nats:
        return
    next_nats = dict(nats)
    next_nats.pop("alias", None)
    next_payload = dict(payload)
    if next_nats:
        next_payload["nats"] = next_nats
    else:
        next_payload.pop("nats", None)
    _save_node_yaml(next_payload)


def save_subnet_alias(alias: str | None, *, subnet_id: str | None = None) -> str | None:
    token = str(alias or "").strip()
    if token:
        durable_state_put(
            _ALIAS_NAMESPACE,
            _ALIAS_KEY,
            {
                "alias": token,
                "subnet_id": str(subnet_id or "").strip() or None,
                "updated_at": time.time(),
            },
        )
    else:
        durable_state_delete(_ALIAS_NAMESPACE, _ALIAS_KEY)
    _clear_legacy_alias_from_node_yaml()
    return token or None


def load_subnet_alias(*, subnet_id: str | None = None) -> str | None:
    payload = durable_state_get(_ALIAS_NAMESPACE, _ALIAS_KEY) or {}
    if isinstance(payload, dict):
        alias = str(payload.get("alias") or "").strip()
        stored_subnet_id = str(payload.get("subnet_id") or "").strip()
        current_subnet_id = str(subnet_id or "").strip()
        if alias and (not current_subnet_id or not stored_subnet_id or stored_subnet_id == current_subnet_id):
            return alias

    try:
        from adaos.services.capacity import _load_node_yaml
    except Exception:
        return None
    try:
        legacy_payload = _load_node_yaml()
    except Exception:
        legacy_payload = {}
    if not isinstance(legacy_payload, dict):
        return None
    nats = legacy_payload.get("nats")
    if not isinstance(nats, dict):
        return None
    alias = str(nats.get("alias") or "").strip()
    if not alias:
        return None
    legacy_subnet_id = str(
        legacy_payload.get("subnet_id")
        or ((legacy_payload.get("subnet") or {}).get("id") if isinstance(legacy_payload.get("subnet"), dict) else "")
        or ""
    ).strip() or None
    save_subnet_alias(alias, subnet_id=legacy_subnet_id)
    current_subnet_id = str(subnet_id or "").strip()
    if current_subnet_id and legacy_subnet_id and current_subnet_id != legacy_subnet_id:
        return None
    return alias

