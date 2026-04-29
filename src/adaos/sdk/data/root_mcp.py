from __future__ import annotations

import logging
import os
import time
from typing import Any

from adaos.sdk.core._ctx import require_ctx
from adaos.services.root.client import RootHttpClient, RootHttpError
from adaos.services.root_mcp.client import RootMcpClient, RootMcpClientConfig
from adaos.services.zone_hosts import canonical_zone_id, zone_public_base_url

_log = logging.getLogger("adaos.sdk.root_mcp")

_EMBEDDED_FALLBACK_UNTIL: dict[str, float] = {}
_EMBEDDED_FALLBACK_DEFAULT_TTL_SEC = 120.0


def _norm_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def _load_config(ctx: Any) -> Any:
    from adaos.services.node_config import load_config

    return load_config(ctx=ctx)


def _expand_cert_path(cfg: Any, raw: Any, fallback: str) -> str:
    from adaos.services.node_config import _expand_path

    path = _expand_path(raw, fallback)
    return str(path) if path else ""


def _default_root_url(*, root_url: str | None = None) -> str:
    ctx = require_ctx("sdk.data.root_mcp")
    if root_url:
        return _norm_url(root_url)
    cfg = _load_config(ctx)
    configured = str(getattr(getattr(cfg, "root_settings", None), "base_url", None) or "").strip()
    if configured:
        return _norm_url(configured)
    zone_id = canonical_zone_id(
        str(os.getenv("ADAOS_ZONE_ID") or getattr(cfg, "zone_id", None) or os.getenv("ZONE_ID") or "").strip().lower()
    )
    return _norm_url(zone_public_base_url(zone_id))


def _root_http_client(*, root_url: str | None = None) -> tuple[RootHttpClient, Any]:
    ctx = require_ctx("sdk.data.root_mcp")
    cfg = _load_config(ctx)
    ca = _expand_cert_path(cfg, cfg.root_settings.ca_cert, "keys/ca.cert")
    cert = _expand_cert_path(cfg, cfg.subnet_settings.hub.cert, "keys/hub_cert.pem")
    key = _expand_cert_path(cfg, cfg.subnet_settings.hub.key, "keys/hub_private.pem")
    verify: str | bool = True
    if os.getenv("ADAOS_ROOT_VERIFY_CA", "0") == "1":
        verify = ca or True
    cert_tuple = (cert, key) if cert and key else None
    return (
        RootHttpClient(
            base_url=_default_root_url(root_url=root_url),
            verify=verify,
            cert=cert_tuple,
        ),
        cfg,
    )


def get_management_client(
    *,
    root_url: str | None = None,
    owner_token: str | None = None,
) -> RootMcpClient:
    from adaos.services.root.service import RootAuthError, RootAuthService

    http, cfg = _root_http_client(root_url=root_url)
    explicit_owner_token = str(
        owner_token
        or os.getenv("ADAOS_ROOT_OWNER_TOKEN")
        or os.getenv("ADAOS_ROOT_TOKEN")
        or os.getenv("ROOT_TOKEN")
        or ""
    ).strip() or None
    if explicit_owner_token:
        headers = {"X-Owner-Token": explicit_owner_token}
        managed_http = RootHttpClient(
            base_url=http.base_url,
            verify=http.verify,
            cert=http.cert,
            default_headers=headers,
        )
        return RootMcpClient(
            RootMcpClientConfig(root_url=http.base_url, extra_headers=headers),
            http=managed_http,
        )

    try:
        access_token = RootAuthService(http=http).get_access_token(cfg)
    except RootAuthError as exc:
        raise RuntimeError(
            f"{exc}. Configure ADAOS_ROOT_OWNER_TOKEN or run root login before using infra_access UI."
        ) from exc
    managed_http = RootHttpClient(
        base_url=http.base_url,
        verify=http.verify,
        cert=http.cert,
        default_headers={"Authorization": f"Bearer {access_token}"},
    )
    return RootMcpClient(
        RootMcpClientConfig(root_url=http.base_url, access_token=access_token),
        http=managed_http,
    )


def _result_response(result: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "response": {"result": result}}


def _is_bridge_upstream_fetch_failure(exc: BaseException) -> bool:
    if not isinstance(exc, RootHttpError):
        return False
    if int(getattr(exc, "status_code", 0) or 0) not in {0, 502, 503}:
        return False
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        code = str(payload.get("error") or payload.get("code") or "").strip()
        detail = str(payload.get("detail") or payload.get("message") or "").strip()
        if code == "adaos_root_mcp_upstream_failed":
            return True
        if "fetch failed" in detail.lower():
            return True
    return "fetch failed" in str(exc).lower()


def _embedded_fallback_enabled() -> bool:
    return str(os.getenv("ADAOS_ROOT_MCP_EMBEDDED_FALLBACK", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _embedded_fallback_ttl_sec() -> float:
    raw = str(os.getenv("ADAOS_ROOT_MCP_EMBEDDED_FALLBACK_TTL_SEC") or "").strip()
    if not raw:
        return _EMBEDDED_FALLBACK_DEFAULT_TTL_SEC
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _EMBEDDED_FALLBACK_DEFAULT_TTL_SEC


def _embedded_fallback_key(context: dict[str, Any]) -> str:
    return str(context.get("root_url") or "").strip().rstrip("/") or "<default-root>"


def _should_use_embedded_fallback(context: dict[str, Any]) -> bool:
    if not _embedded_fallback_enabled():
        return False
    key = _embedded_fallback_key(context)
    until = float(_EMBEDDED_FALLBACK_UNTIL.get(key) or 0.0)
    if until <= time.monotonic():
        _EMBEDDED_FALLBACK_UNTIL.pop(key, None)
        return False
    return True


def _local_first_enabled() -> bool:
    return str(os.getenv("ADAOS_ROOT_MCP_LOCAL_FIRST", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _should_use_local_embedded(context: dict[str, Any]) -> bool:
    return _local_first_enabled() and bool(context.get("local_runtime"))


def _mark_embedded_fallback(context: dict[str, Any], *, operation: str, exc: BaseException) -> None:
    ttl = _embedded_fallback_ttl_sec()
    if _embedded_fallback_enabled() and ttl > 0:
        _EMBEDDED_FALLBACK_UNTIL[_embedded_fallback_key(context)] = time.monotonic() + ttl
    payload = getattr(exc, "payload", None)
    error_code = ""
    if isinstance(payload, dict):
        error_code = str(payload.get("error") or payload.get("code") or "").strip()
    _log.debug(
        "Root MCP bridge upstream unavailable; using embedded local Root MCP operation=%s ttl_s=%.1f error_type=%s status=%s code=%s",
        operation,
        ttl,
        type(exc).__name__,
        getattr(exc, "status_code", None),
        error_code,
    )


def _embedded_operational_surface(context: dict[str, Any]) -> dict[str, Any]:
    from adaos.services.root_mcp.registry import get_descriptor_set
    from adaos.services.root_mcp.service import foundation_snapshot

    foundation = foundation_snapshot()
    descriptor = get_descriptor_set("mcp_session_profile")
    descriptor_payload = descriptor.get("payload") if isinstance(descriptor.get("payload"), dict) else {}
    session_registry = (
        descriptor_payload.get("session_registry")
        if isinstance(descriptor_payload.get("session_registry"), dict)
        else {}
    )
    client_info = foundation.get("client") if isinstance(foundation.get("client"), dict) else {}
    return _result_response(
        {
            "operational_surface": {
                "token_management": {
                    "enabled": True,
                    "issuer_mode": "root_mcp",
                    "token_management_route": "root_mcp",
                    "preferred_bootstrap": str(
                        client_info.get("preferred_bootstrap")
                        or "root-issued MCP Session Lease"
                    ),
                    "session_capability_profiles": list(
                        session_registry.get("capability_profiles")
                        or session_registry.get("profiles")
                        or []
                    ),
                }
            }
        }
    )


def _embedded_sessions(context: dict[str, Any], *, limit: int, active_only: bool) -> dict[str, Any]:
    from adaos.services.root_mcp.sessions import list_mcp_session_leases

    return _result_response(
        {
            "sessions": list_mcp_session_leases(
                limit=int(limit),
                target_id=str(context.get("target_id") or ""),
                active_only=bool(active_only),
            )
        }
    )


def _embedded_access_tokens(context: dict[str, Any], *, limit: int, active_only: bool) -> dict[str, Any]:
    from adaos.services.root_mcp.tokens import list_access_tokens

    return _result_response(
        {
            "tokens": list_access_tokens(
                limit=int(limit),
                target_id=str(context.get("target_id") or ""),
                active_only=bool(active_only),
            )
        }
    )


def _embedded_activity_log(context: dict[str, Any], *, limit: int) -> dict[str, Any]:
    from adaos.services.root_mcp.audit import list_audit_events

    return _result_response(
        {
            "events": list_audit_events(
                limit=int(limit),
                target_id=str(context.get("target_id") or ""),
                subnet_id=str(context.get("subnet_id") or "") or None,
            )
        }
    )


def _embedded_issue_session(
    context: dict[str, Any],
    *,
    capability_profile: str,
    ttl_seconds: int,
    audience: str,
    note: str | None,
) -> dict[str, Any]:
    from adaos.services.root_mcp.sessions import issue_mcp_session_lease

    target_id = str(context.get("target_id") or "").strip()
    if not target_id:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    return _result_response(
        issue_mcp_session_lease(
            audience=str(audience or "codex-vscode"),
            actor="infra_access_skill",
            auth_method="local_sdk",
            ttl_seconds=int(ttl_seconds),
            capability_profile=str(capability_profile or "ProfileOpsRead"),
            target_id=target_id,
            subnet_id=str(context.get("subnet_id") or "") or None,
            zone=str(context.get("zone") or "") or None,
            note=note,
        )
    )


def get_local_target_context(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
) -> dict[str, Any]:
    ctx = require_ctx("sdk.data.root_mcp")
    cfg = _load_config(ctx)
    subnet_id = str(getattr(cfg, "subnet_id", None) or "").strip() or None
    effective_target_id = str(target_id or "").strip() or (f"hub:{subnet_id}" if subnet_id else None)
    zone = (
        str(getattr(cfg, "zone_id", None) or "").strip()
        or str(os.getenv("ADAOS_ZONE_ID") or os.getenv("ZONE_ID") or "").strip()
        or None
    )
    default_root_url = _default_root_url()
    resolved_root_url = _default_root_url(root_url=root_url) if root_url else default_root_url
    return {
        "root_url": resolved_root_url,
        "target_id": effective_target_id,
        "subnet_id": subnet_id,
        "zone": zone,
        "local_runtime": _norm_url(resolved_root_url) == _norm_url(default_root_url),
    }


def get_local_operational_surface(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    if _should_use_local_embedded(context):
        return _embedded_operational_surface(context)
    if _should_use_embedded_fallback(context):
        return _embedded_operational_surface(context)
    client = get_management_client(root_url=context["root_url"])
    try:
        foundation = client.foundation()
        session_profile = client.get_descriptor("mcp_session_profile")
    except Exception as exc:
        if _is_bridge_upstream_fetch_failure(exc):
            _mark_embedded_fallback(context, operation="surface", exc=exc)
            return _embedded_operational_surface(context)
        raise
    foundation_result = dict(foundation.get("result") or {}) if isinstance(foundation.get("result"), dict) else {}
    session_result = dict(session_profile.get("result") or {}) if isinstance(session_profile.get("result"), dict) else {}
    session_registry = dict(session_result.get("session_registry") or {}) if isinstance(session_result.get("session_registry"), dict) else {}
    return {
        "ok": True,
        "response": {
            "result": {
                "operational_surface": {
                    "token_management": {
                        "enabled": True,
                        "issuer_mode": "root_mcp",
                        "token_management_route": "root_mcp",
                        "preferred_bootstrap": str(
                            ((foundation_result.get("client") or {}) if isinstance(foundation_result.get("client"), dict) else {}).get("preferred_bootstrap")
                            or "root-issued MCP Session Lease"
                        ),
                        "session_capability_profiles": list(session_registry.get("capability_profiles") or []),
                    }
                }
            }
        },
    }


def list_local_access_tokens(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
    limit: int = 50,
    active_only: bool = False,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    if _should_use_local_embedded(context):
        return _embedded_access_tokens(context, limit=limit, active_only=active_only)
    if _should_use_embedded_fallback(context):
        return _embedded_access_tokens(context, limit=limit, active_only=active_only)
    client = get_management_client(root_url=context["root_url"])
    try:
        return dict(
            client.list_access_tokens(
                limit=int(limit),
                target_id=str(context["target_id"]),
                active_only=bool(active_only),
            )
        )
    except Exception as exc:
        if _is_bridge_upstream_fetch_failure(exc):
            _mark_embedded_fallback(context, operation="access_tokens", exc=exc)
            return _embedded_access_tokens(context, limit=limit, active_only=active_only)
        raise


def list_local_mcp_sessions(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
    limit: int = 50,
    active_only: bool = False,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    if _should_use_local_embedded(context):
        return _embedded_sessions(context, limit=limit, active_only=active_only)
    if _should_use_embedded_fallback(context):
        return _embedded_sessions(context, limit=limit, active_only=active_only)
    client = get_management_client(root_url=context["root_url"])
    try:
        return dict(
            client.list_session_leases(
                limit=int(limit),
                target_id=str(context["target_id"]),
                active_only=bool(active_only),
            )
        )
    except Exception as exc:
        if _is_bridge_upstream_fetch_failure(exc):
            _mark_embedded_fallback(context, operation="sessions", exc=exc)
            return _embedded_sessions(context, limit=limit, active_only=active_only)
        raise


def get_local_activity_log(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    if _should_use_local_embedded(context):
        return _embedded_activity_log(context, limit=limit)
    if _should_use_embedded_fallback(context):
        return _embedded_activity_log(context, limit=limit)
    client = get_management_client(root_url=context["root_url"])
    try:
        return dict(client.recent_audit(limit=int(limit), target_id=str(context["target_id"])))
    except Exception as exc:
        if _is_bridge_upstream_fetch_failure(exc):
            _mark_embedded_fallback(context, operation="activity_log", exc=exc)
            return _embedded_activity_log(context, limit=limit)
        raise


def issue_local_codex_mcp_session(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
    capability_profile: str = "ProfileOpsRead",
    ttl_seconds: int = 28_800,
    audience: str = "codex-vscode",
    note: str | None = None,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    if _should_use_local_embedded(context):
        return _embedded_issue_session(
            context,
            capability_profile=str(capability_profile),
            ttl_seconds=int(ttl_seconds),
            audience=str(audience),
            note=str(note or "infra_access_skill Codex bootstrap").strip(),
        )
    if _should_use_embedded_fallback(context):
        return _embedded_issue_session(
            context,
            capability_profile=str(capability_profile),
            ttl_seconds=int(ttl_seconds),
            audience=str(audience),
            note=str(note or "infra_access_skill Codex bootstrap").strip(),
        )
    client = get_management_client(root_url=context["root_url"])
    payload = {
        "target_id": str(context["target_id"]),
        "audience": str(audience),
        "ttl_seconds": int(ttl_seconds),
        "capability_profile": str(capability_profile),
        "note": str(note or "infra_access_skill Codex bootstrap").strip(),
    }
    try:
        return dict(client.issue_session_lease(payload))
    except Exception as exc:
        if _is_bridge_upstream_fetch_failure(exc):
            _mark_embedded_fallback(context, operation="issue_session", exc=exc)
            return _embedded_issue_session(
                context,
                capability_profile=str(capability_profile),
                ttl_seconds=int(ttl_seconds),
                audience=str(audience),
                note=str(note or "infra_access_skill Codex bootstrap").strip(),
            )
        raise


__all__ = [
    "get_local_activity_log",
    "get_local_operational_surface",
    "get_local_target_context",
    "get_management_client",
    "issue_local_codex_mcp_session",
    "list_local_access_tokens",
    "list_local_mcp_sessions",
]
