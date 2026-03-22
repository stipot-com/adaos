from __future__ import annotations

import os
from typing import Any

import requests


def _is_local_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "localhost", "::1"}


def _pick_env_url() -> str | None:
    # Prefer explicit control base variables, then hub URL. Avoid `ADAOS_API_BASE` by default: it is Root API base
    # in prod and would make CLI call the wrong server.
    for key in ("ADAOS_CONTROL_URL", "ADAOS_CONTROL_BASE", "ADAOS_SELF_BASE_URL", "ADAOS_HUB_URL"):
        raw = str(os.getenv(key, "") or "").strip()
        if raw:
            return raw.rstrip("/")
    # Backward-compat: accept legacy ADAOS_BASE/ADAOS_API_BASE only for local URLs.
    for key in ("ADAOS_BASE", "ADAOS_API_BASE"):
        raw = str(os.getenv(key, "") or "").strip()
        if raw and _is_local_url(raw):
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
    3) env (ADAOS_CONTROL_URL / ADAOS_CONTROL_BASE / ADAOS_SELF_BASE_URL / ADAOS_HUB_URL, plus legacy local-only ADAOS_BASE/ADAOS_API_BASE)
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
    # node.yaml fallback (role-aware).
    try:
        from adaos.services.node_config import load_config

        conf = load_config()
        role = str(getattr(conf, "role", "") or "").strip().lower()
        cfg_url = str(getattr(conf, "hub_url", "") or "").strip()
        if role == "member" and cfg_url:
            return cfg_url.rstrip("/")
        if role == "hub" and cfg_url and _is_local_url(cfg_url):
            return cfg_url.rstrip("/")
    except Exception:
        pass
    return "http://127.0.0.1:8777"


def resolve_control_token(*, explicit: str | None = None) -> str:
    if explicit is not None:
        txt = str(explicit or "").strip()
        if txt:
            return txt
    env_tok = _pick_env_token()
    if env_tok:
        return env_tok
    try:
        from adaos.services.node_config import load_config

        conf = load_config()
        tok = str(getattr(conf, "token", "") or "").strip()
        if tok:
            return tok
    except Exception:
        pass
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
