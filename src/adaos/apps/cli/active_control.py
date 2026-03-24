from __future__ import annotations

import json
import os
from pathlib import Path
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


def _normalize_url(raw: str | None) -> str | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    return txt.rstrip("/")


def _pick_env_override_url() -> str | None:
    # Explicit control base variables are authoritative and may point to a non-local server.
    for key in ("ADAOS_CONTROL_URL", "ADAOS_CONTROL_BASE"):
        raw = _normalize_url(os.getenv(key, ""))
        if raw:
            return raw
    return None


def _pick_local_env_url() -> str | None:
    # Accept self-advertised URLs only when they point to local host.
    for key in ("ADAOS_SELF_BASE_URL", "ADAOS_HUB_URL"):
        raw = _normalize_url(os.getenv(key, ""))
        if raw and _is_local_url(raw):
            return raw
    # Backward-compat: accept legacy ADAOS_BASE/ADAOS_API_BASE only for local URLs.
    for key in ("ADAOS_BASE", "ADAOS_API_BASE"):
        raw = _normalize_url(os.getenv(key, ""))
        if raw and _is_local_url(raw):
            return raw
    return None


def _pick_env_token() -> str | None:
    for key in ("ADAOS_TOKEN", "ADAOS_HUB_TOKEN", "HUB_TOKEN"):
        raw = str(os.getenv(key, "") or "").strip()
        if raw:
            return raw
    return None


def _append_candidate(candidates: list[str], seen: set[str], raw: str | None) -> None:
    url = _normalize_url(raw)
    if not url or url in seen:
        return
    candidates.append(url)
    seen.add(url)


def _node_config_control_url() -> tuple[str | None, str | None]:
    try:
        from adaos.services.node_config import load_config

        conf = load_config()
        role = str(getattr(conf, "role", "") or "").strip().lower() or None
        cfg_url = _normalize_url(getattr(conf, "hub_url", None))
        return role, cfg_url
    except Exception:
        return None, None


def _autostart_control_url() -> str | None:
    try:
        from adaos.services.agent_context import get_ctx
        from adaos.services.autostart import status as autostart_status

        info = autostart_status(get_ctx())
        raw = _normalize_url((info or {}).get("url") if isinstance(info, dict) else None)
        if raw and _is_local_url(raw):
            return raw
    except Exception:
        pass
    return None


def _pidfile_control_urls() -> list[str]:
    try:
        from adaos.services.agent_context import get_ctx

        state_root = get_ctx().paths.state_dir()
        state_dir = Path(state_root() if callable(state_root) else state_root)
        api_dir = state_dir / "api"
        if not api_dir.exists():
            return []
        found: list[tuple[float, str]] = []
        for path in api_dir.glob("serve-*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            raw = _normalize_url(data.get("advertised_base"))
            if not raw or not _is_local_url(raw):
                continue
            try:
                started_at = float(data.get("started_at") or 0.0)
            except Exception:
                started_at = 0.0
            found.append((started_at, raw))
        found.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in found]
    except Exception:
        return []


def _looks_like_control_api_response(code: int | None, payload: dict[str, Any] | None) -> bool:
    if code is None:
        return False
    if isinstance(payload, dict):
        return True
    return int(code) in {401, 403}


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
    3) env (ADAOS_CONTROL_URL / ADAOS_CONTROL_BASE, plus local-only ADAOS_SELF_BASE_URL / ADAOS_HUB_URL / ADAOS_BASE / ADAOS_API_BASE)
    4) localhost fallback
    """
    if explicit is not None:
        txt = _normalize_url(explicit)
        if txt:
            return txt
    if hub_url is not None:
        txt = _normalize_url(hub_url)
        if txt:
            return txt

    role, cfg_url = _node_config_control_url()
    if role == "member" and cfg_url:
        return cfg_url

    env_override = _pick_env_override_url()
    if env_override:
        return env_override

    candidates: list[str] = []
    seen: set[str] = set()
    if role == "hub" and cfg_url and _is_local_url(cfg_url):
        _append_candidate(candidates, seen, cfg_url)
    _append_candidate(candidates, seen, _pick_local_env_url())
    _append_candidate(candidates, seen, _autostart_control_url())
    for raw in _pidfile_control_urls():
        _append_candidate(candidates, seen, raw)
    for raw in (
        "http://127.0.0.1:8777",
        "http://127.0.0.1:8778",
        "http://127.0.0.1:8779",
        "http://localhost:8777",
        "http://localhost:8778",
        "http://localhost:8779",
    ):
        _append_candidate(candidates, seen, raw)

    token = resolve_control_token()
    for candidate in candidates:
        code, payload = probe_control_api(base_url=candidate, token=token, timeout_s=0.35)
        if _looks_like_control_api_response(code, payload):
            return candidate
    if candidates:
        return candidates[0]
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
    base = str(base_url).rstrip("/")
    headers = {"X-AdaOS-Token": str(token or "")}
    sess = requests.Session()
    try:
        sess.trust_env = False
    except Exception:
        pass
    try:
        resp = sess.get(base + "/api/node/status", headers=headers, timeout=float(timeout_s))
    except Exception:
        resp = None
    if resp is not None:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        return int(resp.status_code), payload if isinstance(payload, dict) else None
    try:
        resp = sess.get(base + "/api/ping", headers={"Accept": "application/json"}, timeout=float(timeout_s))
    except Exception:
        return None, None
    if int(resp.status_code) != 200:
        return int(resp.status_code), None
    return int(resp.status_code), {"ok": True, "ping": True}
