from __future__ import annotations

import os
from typing import Any

from adaos.sdk.core._ctx import require_ctx
from adaos.services.root.client import RootHttpClient
from adaos.services.root_mcp.client import RootMcpClient, RootMcpClientConfig
from adaos.services.zone_hosts import canonical_zone_id, zone_public_base_url


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
        return str(root_url).strip()
    cfg = _load_config(ctx)
    configured = str(getattr(getattr(cfg, "root_settings", None), "base_url", None) or "").strip()
    if configured:
        return configured
    zone_id = canonical_zone_id(
        str(os.getenv("ADAOS_ZONE_ID") or getattr(cfg, "zone_id", None) or os.getenv("ZONE_ID") or "").strip().lower()
    )
    return zone_public_base_url(zone_id)


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
    return {
        "root_url": _default_root_url(root_url=root_url),
        "target_id": effective_target_id,
        "subnet_id": subnet_id,
        "zone": zone,
    }


def get_local_operational_surface(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    client = get_management_client(root_url=context["root_url"])
    foundation = client.foundation()
    session_profile = client.get_descriptor("mcp_session_profile")
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
    client = get_management_client(root_url=context["root_url"])
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    return dict(
        client.list_access_tokens(
            limit=int(limit),
            target_id=str(context["target_id"]),
            active_only=bool(active_only),
        )
    )


def list_local_mcp_sessions(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
    limit: int = 50,
    active_only: bool = False,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    client = get_management_client(root_url=context["root_url"])
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    return dict(
        client.list_session_leases(
            limit=int(limit),
            target_id=str(context["target_id"]),
            active_only=bool(active_only),
        )
    )


def get_local_activity_log(
    *,
    target_id: str | None = None,
    root_url: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    context = get_local_target_context(target_id=target_id, root_url=root_url)
    client = get_management_client(root_url=context["root_url"])
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    return dict(client.recent_audit(limit=int(limit), target_id=str(context["target_id"])))


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
    client = get_management_client(root_url=context["root_url"])
    if not context["target_id"]:
        raise RuntimeError("Unable to infer local target_id for Root MCP access.")
    return dict(
        client.issue_session_lease(
            {
                "target_id": str(context["target_id"]),
                "audience": str(audience),
                "ttl_seconds": int(ttl_seconds),
                "capability_profile": str(capability_profile),
                "note": str(note or "infra_access_skill Codex bootstrap").strip(),
            }
        )
    )


__all__ = [
    "get_local_activity_log",
    "get_local_operational_surface",
    "get_local_target_context",
    "get_management_client",
    "issue_local_codex_mcp_session",
    "list_local_access_tokens",
    "list_local_mcp_sessions",
]
