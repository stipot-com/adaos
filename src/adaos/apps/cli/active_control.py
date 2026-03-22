from __future__ import annotations

import os
from typing import Any

import requests


def _pick_env_url() -> str | None:
    for key in ("ADAOS_SELF_BASE_URL", "ADAOS_API_BASE", "ADAOS_BASE", "ADAOS_CONTROL_BASE", "ADAOS_CONTROL_URL"):
        raw = str(os.getenv(key, "") or "").strip()
        if raw:
            return raw.rstrip("/")
    return None


def _pick_env_token() -> str | None:
    for key in ("ADAOS_TOKEN", "ADAOS_HUB_TOKEN", "HUB_TOKEN"):
        raw = str(os.getenv(key, "") or "").strip()
        if raw:
            return raw
    return None


def resolve_control_base_url(
    *,
    explicit: str | None = None,
    hub_url: str | None = None,
) -> str:
    """
    Resolve which control API base URL to use.

    Precedence:
    1) explicit (if provided)
    2) hub_url (if provided)
    3) env (ADAOS_SELF_BASE_URL / ADAOS_API_BASE / ADAOS_BASE / ...)
    4) localhost fallback
    """
    if explicit is not None:
        txt = str(explicit or "").strip()
        if txt:
            return txt.rstrip("/")
    if hub_url is not None:
        txt = str(hub_url or "").strip()
        if txt:
            return txt.rstrip("/")
    env_url = _pick_env_url()
    if env_url:
        return env_url
    return "http://127.0.0.1:8777"


def resolve_control_token(*, explicit: str | None = None) -> str:
    if explicit is not None:
        txt = str(explicit or "").strip()
        if txt:
            return txt
    env_tok = _pick_env_token()
    if env_tok:
        return env_tok
    return "dev-local-token"


def probe_control_api(*, base_url: str, token: str, timeout_s: float = 2.0) -> tuple[int | None, dict[str, Any] | None]:
    """
    Best-effort probe: GET /api/node/status.
    Returns (status_code, json_payload_or_None). status_code None means unreachable.
    """
    url = str(base_url).rstrip("/") + "/api/node/status"
    headers = {"X-AdaOS-Token": str(token or "")}
    try:
        resp = requests.get(url, headers=headers, timeout=float(timeout_s))
    except Exception:
        return None, None
    try:
        payload = resp.json()
    except Exception:
        payload = None
    return int(resp.status_code), payload if isinstance(payload, dict) else None

