from __future__ import annotations
import json
import hashlib
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from adaos.adapters.db import sqlite as sqlite_db
from adaos.apps.api.auth import require_owner_token
from adaos.services.agent_context import get_ctx
from adaos.services.id_gen import new_id
from adaos.services.join_codes import (
    JoinCodeConsumed,
    JoinCodeExpired,
    JoinCodeNotFound,
    consume_any as consume_join_code_any,
    create as create_join_code,
)
from adaos.services.root_mcp.service import (
    foundation_snapshot,
    get_descriptor,
    get_managed_target,
    invoke_tool,
    list_descriptor_registry,
    list_managed_targets,
    list_tool_contracts,
    recent_audit_events,
)
from adaos.services.root_mcp.audit import append_audit_event
from adaos.services.root_mcp.model import RootMcpAuditEvent, RootMcpSurface
from adaos.services.root_mcp.policy import evaluate_direct_access
from adaos.services.root_mcp.reports import ingest_control_report, list_control_reports
from adaos.services.root_mcp.memory_reports import (
    get_memory_profile_artifact,
    list_memory_profile_artifacts,
    get_memory_profile_report,
    ingest_memory_profile_report,
    list_memory_profile_reports,
)
from adaos.services.root_mcp.targets import upsert_managed_target
from adaos.services.root_mcp.tokens import issue_access_token, list_access_tokens, revoke_access_token, validate_access_token

router = APIRouter()
root_router = APIRouter(prefix="/v1/root", tags=["root"])
subnet_router = APIRouter(prefix="/v1/subnets", tags=["subnets"])


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_owner_id(owner_token: str) -> str:
    require_owner_token(owner_token)
    return os.getenv("ADAOS_ROOT_OWNER_ID") or "local-owner"


def _append_direct_root_mcp_audit(
    *,
    tool_id: str,
    actor: str,
    auth_method: str,
    capability: str,
    status: str,
    target_id: str | None = None,
    meta: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    redactions: list[str] | None = None,
) -> str:
    finished_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    event = RootMcpAuditEvent(
        event_id=new_id(),
        request_id=new_id(),
        trace_id=new_id(),
        tool_id=tool_id,
        surface=RootMcpSurface.OPERATIONS,
        actor=actor,
        auth_method=auth_method,
        capability=capability,
        target_id=target_id,
        policy_decision="allow",
        execution_adapter="root.direct_endpoint",
        dry_run=False,
        status=status,
        started_at=finished_at,
        finished_at=finished_at,
        result_summary=result_summary or {},
        error=error or {},
        redactions=list(redactions or []),
        meta=meta or {},
    )
    append_audit_event(event)
    return event.event_id


def _register_subnet_with_root_auth(owner_token: str, csr_pem: str, fingerprint: str, hints: dict[str, Any] | None = None) -> dict[str, Any]:
    from adaos.services.root.service import RootAuthService

    return RootAuthService.register_subnet(owner_token, csr_pem, fingerprint, hints=hints)


def _require_root_write_auth(*, authorization: str | None, owner_token: str | None) -> dict[str, Any]:
    """
    Root-side write auth (best-effort for dev/self-hosted root).

    Accepted methods:
    - `X-Owner-Token` (matches `ADAOS_ROOT_OWNER_TOKEN` via require_owner_token)
    - `Authorization: Bearer ...` (optionally checked against `ADAOS_ROOT_BEARER_TOKEN` if set)
    """
    if owner_token:
        require_owner_token(owner_token)
        owner_id = _resolve_owner_id(owner_token)
        return {"method": "owner_token", "owner_id": owner_id, "actor": f"owner:{owner_id}"}

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        expected = os.getenv("ADAOS_ROOT_BEARER_TOKEN") or ""
        if expected and token != expected:
            raise HTTPException(status_code=401, detail="Invalid bearer token")
        return {
            "method": "bearer",
            "verified": bool(expected),
            "actor": "bearer:root",
        }

    raise HTTPException(status_code=401, detail="Missing Authorization bearer token or X-Owner-Token")


def _require_root_access_auth(*, authorization: str | None, owner_token: str | None) -> dict[str, Any]:
    if owner_token:
        return _require_root_write_auth(authorization=authorization, owner_token=owner_token)

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail="Invalid bearer token")

        expected = os.getenv("ADAOS_ROOT_BEARER_TOKEN") or ""
        if expected and token == expected:
            return {
                "method": "bearer",
                "verified": True,
                "actor": "bearer:root",
            }

        record = validate_access_token(token)
        if record is not None:
            return {
                "method": "mcp_access_token",
                "verified": True,
                "actor": f"mcp_access_token:{record.get('token_id')}",
                "grant_source": "issued_access_token",
                "capabilities": list(record.get("capabilities") or []),
                "subnet_id": record.get("subnet_id"),
                "zone": record.get("zone"),
                "allowed_target_ids": list(record.get("target_ids") or []),
                "access_token_id": record.get("token_id"),
                "audience": record.get("audience"),
            }

    raise HTTPException(status_code=401, detail="Missing Authorization bearer token or X-Owner-Token")


def _legacy_root_token_auth(root_token: str | None) -> dict[str, Any] | None:
    token = str(root_token or "").strip()
    if not token:
        return None
    expected = os.getenv("ROOT_TOKEN") or os.getenv("ADAOS_ROOT_BEARER_TOKEN") or ""
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="Invalid X-Root-Token")
    return {
        "method": "root_token",
        "verified": bool(expected),
        "actor": "root_token:root",
        "capabilities": ["*"],
        "grant_source": "legacy_root_token",
    }


def _hub_report_token_auth(hub_report_token: str | None) -> dict[str, Any] | None:
    token = str(hub_report_token or "").strip()
    if not token:
        return None
    expected = os.getenv("ADAOS_ROOT_HUB_REPORT_TOKEN") or ""
    if not expected:
        return None
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid X-AdaOS-Hub-Report-Token")
    return {
        "method": "hub_report_token",
        "verified": True,
        "actor": "hub_report:verified",
        "grant_source": "hub_report_token",
    }


def _require_root_read_auth_or_legacy_root_token(
    *,
    authorization: str | None,
    owner_token: str | None,
    root_token: str | None,
) -> dict[str, Any]:
    legacy = _legacy_root_token_auth(root_token)
    if legacy is not None:
        return legacy
    return _require_root_access_auth(authorization=authorization, owner_token=owner_token)


def _resolve_hub_control_report_auth(
    *,
    authorization: str | None,
    owner_token: str | None,
    root_token: str | None,
    hub_report_token: str | None,
) -> dict[str, Any]:
    if owner_token or authorization:
        return _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    report_token_auth = _hub_report_token_auth(hub_report_token)
    if report_token_auth is not None:
        return report_token_auth
    legacy = _legacy_root_token_auth(root_token)
    if legacy is not None:
        return legacy
    return {
        "method": "hub_control_report_unverified",
        "verified": False,
        "actor": "hub_report:unverified",
        "grant_source": "transport_expected",
    }


def _mcp_scope(*, subnet_id: str | None, zone: str | None) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    token = str(subnet_id or "").strip()
    if token:
        scope["subnet_id"] = token
    token = str(zone or "").strip()
    if token:
        scope["zone"] = token
    return scope


def _effective_mcp_scope(*, auth: dict[str, Any], subnet_id: str | None, zone: str | None) -> dict[str, Any]:
    scope = _mcp_scope(subnet_id=subnet_id, zone=zone)
    auth_subnet = str(auth.get("subnet_id") or "").strip()
    if auth_subnet:
        if scope.get("subnet_id") and scope["subnet_id"] != auth_subnet:
            raise HTTPException(status_code=403, detail={"code": "scope_mismatch", "message": "Requested subnet scope does not match access token scope."})
        scope["subnet_id"] = auth_subnet
    auth_zone = str(auth.get("zone") or "").strip()
    if auth_zone:
        if scope.get("zone") and scope["zone"] != auth_zone:
            raise HTTPException(status_code=403, detail={"code": "zone_mismatch", "message": "Requested zone scope does not match access token scope."})
        scope["zone"] = auth_zone
    return scope


def _allowed_target_ids(auth: dict[str, Any]) -> list[str]:
    raw = auth.get("allowed_target_ids") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        token = str(item or "").strip()
        if token and token not in out:
            out.append(token)
    return out


def _enforce_mcp_capability(required_capability: str, *, auth: dict[str, Any]) -> None:
    decision = evaluate_direct_access(
        required_capability,
        actor=str(auth.get("actor") or "root:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        auth_context=auth,
    )
    if not decision.allowed:
        raise HTTPException(status_code=403, detail={"code": decision.code, "message": decision.message})


@root_router.post("/register")
def root_register() -> dict:
    """Legacy bootstrap endpoint has been replaced by owner-based flow."""
    raise RuntimeError("legacy endpoint removed; use owner login flow")


class SubnetRegisterRequest(BaseModel):
    csr_pem: str
    fingerprint: str
    owner_token: str
    hints: dict[str, Any] | None = None


def _canonical_body(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@subnet_router.post("/register")
async def subnet_register(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    payload_raw = await request.json()
    if not isinstance(payload_raw, dict):
        raise HTTPException(status_code=422, detail="Invalid request body")
    try:
        payload = SubnetRegisterRequest.model_validate(payload_raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    require_owner_token(payload.owner_token)
    owner_id = _resolve_owner_id(payload.owner_token)
    canonical_source = payload.model_dump(mode="python")
    body_hash = hashlib.sha256(_canonical_body(canonical_source).encode("utf-8")).hexdigest()

    if idempotency_key:
        cached = sqlite_db.idem_get(idempotency_key, "POST", "/v1/subnets/register", owner_id, body_hash)
        if cached:
            return Response(content=cached["body_json"], status_code=cached["status_code"], media_type="application/json")

    result = _register_subnet_with_root_auth(payload.owner_token, payload.csr_pem, payload.fingerprint, hints=payload.hints)
    response_body = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if idempotency_key:
        sqlite_db.idem_put(
            idempotency_key,
            "POST",
            "/v1/subnets/register",
            owner_id,
            body_hash,
            200,
            response_body,
            result["event_id"],
            result["server_time_utc"],
        )
    return Response(content=response_body, status_code=200, media_type="application/json")


@subnet_router.get("/register/status")
async def subnet_register_status(
    fingerprint: str,
    owner_token: str = Header(..., alias="X-Owner-Token"),
) -> dict:
    require_owner_token(owner_token)
    owner_id = _resolve_owner_id(owner_token)
    subnet = sqlite_db.subnet_get_or_create(owner_id)
    device = sqlite_db.device_get_by_fingerprint(subnet["subnet_id"], fingerprint)
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if device:
        data = {
            "subnet_id": subnet["subnet_id"],
            "hub_device_id": device["device_id"],
            "cert_pem": device["cert_pem"],
        }
    else:
        data = None
    return {"data": data, "event_id": new_id(), "server_time_utc": now_iso}


class RootJoinCodeCreateRequest(BaseModel):
    subnet_id: str = Field(..., min_length=1, max_length=128)
    # Deprecated: in Root-proxy routing the hub does not need to expose a public URL and
    # Root does not need a hub token to route traffic (hub uses its local token).
    hub_url: str | None = Field(
        default=None,
        min_length=3,
        max_length=2048,
        description="[deprecated] Hub base URL used as rendezvous for members",
    )
    token: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        description="[deprecated] Subnet token required by the hub",
    )
    ttl_minutes: int = Field(15, ge=1, le=60)
    length: int = Field(8, ge=8, le=12)


class RootJoinCodeCreateResponse(BaseModel):
    ok: bool
    code: str
    expires_at_utc: str


@subnet_router.post("/join-code", response_model=RootJoinCodeCreateResponse)
async def subnet_join_code_create(
    payload: RootJoinCodeCreateRequest,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
) -> RootJoinCodeCreateResponse:
    auth = _require_root_write_auth(authorization=authorization, owner_token=owner_token)
    deprecated_fields: list[str] = []
    if payload.hub_url:
        deprecated_fields.append("hub_url")
    if payload.token:
        deprecated_fields.append("token")
    info = create_join_code(
        subnet_id=str(payload.subnet_id).strip(),
        ttl_seconds=int(payload.ttl_minutes) * 60,
        length=int(payload.length),
        meta={
            "kind": "subnet.member.join",
            "issued_by": "root",
            "auth": auth,
            "deprecated_fields": deprecated_fields,
            **({"hub_url": str(payload.hub_url).strip()} if payload.hub_url else {}),
            **({"token": str(payload.token).strip()} if payload.token else {}),
        },
        ctx=get_ctx(),
    )
    return RootJoinCodeCreateResponse(ok=True, code=info.code, expires_at_utc=_iso_utc(info.expires_at))


class RootSubnetJoinRequest(BaseModel):
    code: str = Field(..., min_length=4, max_length=64)
    node_id: str | None = None
    hostname: str | None = None


class RootSubnetJoinResponse(BaseModel):
    ok: bool
    subnet_id: str
    token: str
    root_url: str
    hub_url: str
    diagnostics: dict[str, Any]


@subnet_router.post("/join", response_model=RootSubnetJoinResponse)
async def subnet_join_consume(req: Request, payload: RootSubnetJoinRequest) -> RootSubnetJoinResponse:
    """
    Root-mediated join:

    - member posts a short one-time join-code to Root
    - Root returns subnet_id + hub rendezvous URL + subnet token
    """
    code = str(payload.code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="code is required")

    try:
        rec = consume_join_code_any(code=code, ctx=get_ctx())
    except JoinCodeNotFound:
        raise HTTPException(status_code=404, detail="join-code not found") from None
    except JoinCodeExpired:
        raise HTTPException(status_code=410, detail="join-code expired") from None
    except JoinCodeConsumed:
        raise HTTPException(status_code=409, detail="join-code already used") from None

    subnet_id = str(rec.get("subnet_id") or "").strip()
    meta = rec.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    root_url = str(req.base_url).rstrip("/")
    hub_url = str(meta.get("hub_url") or "").strip()
    if not hub_url:
        hub_url = f"{root_url}/hubs/{subnet_id}"

    # For self-hosted dev Root we may not have a session-JWT signer; return a best-effort token.
    # In production Root this is a web-session JWT accepted by the Root proxy.
    token = str(meta.get("token") or "").strip() or f"dev-session:{new_id()}"

    if not subnet_id or not token or not hub_url:
        raise HTTPException(status_code=500, detail="invalid join-code record (missing subnet_id/token/hub_url)")

    diags = {
        "subnet_id": subnet_id,
        "node_id_hint": (payload.node_id or "").strip() or None,
        "hostname_hint": (payload.hostname or "").strip() or None,
        "code_created_at_utc": _iso_utc(float(rec.get("created_at") or 0.0)) if rec.get("created_at") else None,
        "code_expires_at_utc": _iso_utc(float(rec.get("expires_at") or 0.0)) if rec.get("expires_at") else None,
        "issued_by": meta.get("issued_by") or None,
        "deprecated_fields": meta.get("deprecated_fields") or [],
        "hub_url": hub_url,
        "root_url": root_url,
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return RootSubnetJoinResponse(ok=True, subnet_id=subnet_id, token=token, root_url=root_url, hub_url=hub_url, diagnostics=diags)


class RootMcpCallRequest(BaseModel):
    tool_id: str = Field(..., min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    dry_run: bool = False
    arguments: dict[str, Any] = Field(default_factory=dict)


class RootMcpTargetUpsertRequest(BaseModel):
    target_id: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    kind: str = Field(..., min_length=1, max_length=64)
    environment: str = Field(..., min_length=1, max_length=32)
    status: str = Field(default="unknown", min_length=1, max_length=64)
    zone: str | None = Field(default=None, max_length=128)
    subnet_id: str | None = Field(default=None, max_length=128)
    transport: dict[str, Any] = Field(default_factory=dict)
    operational_surface: dict[str, Any] = Field(default_factory=dict)
    access: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


class RootMcpAccessTokenIssueRequest(BaseModel):
    audience: str = Field(..., min_length=1, max_length=128)
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    capabilities: list[str] = Field(default_factory=list)
    subnet_id: str | None = Field(default=None, max_length=128)
    zone: str | None = Field(default=None, max_length=128)
    target_id: str | None = Field(default=None, max_length=128)
    target_ids: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=512)


class RootMcpAccessTokenRevokeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=512)


@router.post("/v1/hub/control/report")
async def hub_control_report_ingest(
    request: Request,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    root_token: str | None = Header(default=None, alias="X-Root-Token"),
    hub_report_token: str | None = Header(default=None, alias="X-AdaOS-Hub-Report-Token"),
) -> dict[str, Any]:
    auth = _resolve_hub_control_report_auth(
        authorization=authorization,
        owner_token=owner_token,
        root_token=root_token,
        hub_report_token=hub_report_token,
    )
    payload_raw = await request.json()
    if not isinstance(payload_raw, dict):
        raise HTTPException(status_code=422, detail="Invalid request body")
    result = ingest_control_report(payload_raw, ingest_auth=auth)
    protocol = payload_raw.get("_protocol") if isinstance(payload_raw.get("_protocol"), dict) else {}
    audit_event = RootMcpAuditEvent(
        event_id=new_id(),
        request_id=str(protocol.get("message_id") or result.get("event_id") or new_id()),
        trace_id=str(protocol.get("flow_id") or new_id()),
        tool_id="hub.control_report.ingest",
        surface=RootMcpSurface.OPERATIONS,
        actor=str(auth.get("actor") or "hub_report:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        capability="hub.control_report.ingest",
        target_id=str(result.get("target_id") or ""),
        policy_decision="allow",
        execution_adapter="hub_root_protocol",
        dry_run=False,
        status="duplicate" if bool(result.get("duplicate")) else "ok",
        started_at=str(payload_raw.get("reported_at") or result.get("server_time_utc") or ""),
        finished_at=str(result.get("server_time_utc") or ""),
        result_summary={"kind": "control_report", "duplicate": bool(result.get("duplicate"))},
        meta={
            "report_verified": bool(result.get("report_verified")),
            "report_auth_method": str(result.get("report_auth_method") or ""),
            "subnet_id": str(payload_raw.get("subnet_id") or ""),
            "zone": str(payload_raw.get("zone") or ""),
            "message_id": str(protocol.get("message_id") or ""),
        },
    )
    append_audit_event(audit_event)
    return {
        **result,
        "auth": {"method": auth.get("method")},
        "audit_event_id": audit_event.event_id,
    }


@router.get("/v1/hubs/control/reports")
async def hub_control_reports(
    hub_id: str | None = None,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    root_token: str | None = Header(default=None, alias="X-Root-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_read_auth_or_legacy_root_token(authorization=authorization, owner_token=owner_token, root_token=root_token)
    _enforce_mcp_capability("operations.read.targets", auth=auth)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    allowed_target_ids = _allowed_target_ids(auth)
    target_filter = str(hub_id or "").strip() or None
    if target_filter and allowed_target_ids and target_filter not in allowed_target_ids:
        raise HTTPException(status_code=403, detail={"code": "target_forbidden", "message": "Managed target is outside the token target allowlist."})

    items: list[dict[str, Any]] = []
    for item in list_control_reports(hub_id=target_filter):
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        target_id = str(item.get("hub_id") or "").strip()
        if allowed_target_ids and target_id not in allowed_target_ids:
            continue
        if scope.get("subnet_id") and target.get("subnet_id") and scope["subnet_id"] != target["subnet_id"]:
            if target_filter:
                raise HTTPException(status_code=403, detail={"code": "scope_mismatch", "message": "Managed target is outside the requested subnet scope."})
            continue
        if scope.get("zone") and target.get("zone") and scope["zone"] != target["zone"]:
            if target_filter:
                raise HTTPException(status_code=403, detail={"code": "zone_mismatch", "message": "Managed target is outside the requested zone scope."})
            continue
        items.append(item)
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "reports": items,
    }


@router.post("/v1/hub/memory_profile/report")
async def hub_memory_profile_report_ingest(
    request: Request,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    root_token: str | None = Header(default=None, alias="X-Root-Token"),
    hub_report_token: str | None = Header(default=None, alias="X-AdaOS-Hub-Report-Token"),
) -> dict[str, Any]:
    auth = _resolve_hub_control_report_auth(
        authorization=authorization,
        owner_token=owner_token,
        root_token=root_token,
        hub_report_token=hub_report_token,
    )
    payload_raw = await request.json()
    if not isinstance(payload_raw, dict):
        raise HTTPException(status_code=422, detail="Invalid request body")
    result = ingest_memory_profile_report(payload_raw, ingest_auth=auth)
    protocol = payload_raw.get("_protocol") if isinstance(payload_raw.get("_protocol"), dict) else {}
    audit_event = RootMcpAuditEvent(
        event_id=new_id(),
        request_id=str(protocol.get("message_id") or result.get("event_id") or new_id()),
        trace_id=str(protocol.get("flow_id") or new_id()),
        tool_id="hub.memory_profile_report.ingest",
        surface=RootMcpSurface.OPERATIONS,
        actor=str(auth.get("actor") or "hub_report:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        capability="hub.memory_profile_report.ingest",
        target_id=str(result.get("hub_id") or ""),
        policy_decision="allow",
        execution_adapter="hub_root_protocol",
        dry_run=False,
        status="duplicate" if bool(result.get("duplicate")) else "ok",
        started_at=str(payload_raw.get("reported_at") or result.get("server_time_utc") or ""),
        finished_at=str(result.get("server_time_utc") or ""),
        result_summary={
            "kind": "memory_profile_report",
            "duplicate": bool(result.get("duplicate")),
            "session_id": str(result.get("session_id") or ""),
        },
        meta={
            "report_verified": bool(result.get("report_verified")),
            "report_auth_method": str(result.get("report_auth_method") or ""),
            "subnet_id": str(payload_raw.get("subnet_id") or ""),
            "zone": str(payload_raw.get("zone") or ""),
            "session_id": str(result.get("session_id") or ""),
            "message_id": str(protocol.get("message_id") or ""),
        },
    )
    append_audit_event(audit_event)
    return {
        **result,
        "auth": {"method": auth.get("method")},
        "audit_event_id": audit_event.event_id,
    }


@router.get("/v1/hubs/memory_profile/reports")
async def hub_memory_profile_reports(
    hub_id: str | None = None,
    session_id: str | None = None,
    state: str | None = None,
    suspected_only: bool = False,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    root_token: str | None = Header(default=None, alias="X-Root-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_read_auth_or_legacy_root_token(
        authorization=authorization,
        owner_token=owner_token,
        root_token=root_token,
    )
    _enforce_mcp_capability("operations.read.targets", auth=auth)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    allowed_target_ids = _allowed_target_ids(auth)
    target_filter = str(hub_id or "").strip() or None
    if target_filter and allowed_target_ids and target_filter not in allowed_target_ids:
        raise HTTPException(status_code=403, detail={"code": "target_forbidden", "message": "Managed target is outside the token target allowlist."})

    items: list[dict[str, Any]] = []
    for item in list_memory_profile_reports(
        hub_id=target_filter,
        session_id=session_id,
        session_state=state,
        suspected_only=suspected_only,
        subnet_id=scope.get("subnet_id"),
        zone=scope.get("zone"),
    ):
        report = item.get("report") if isinstance(item.get("report"), dict) else {}
        target_subnet = str(report.get("subnet_id") or "").strip()
        target_zone = str(report.get("zone") or "").strip()
        target_id = str(item.get("hub_id") or "").strip()
        if allowed_target_ids and target_id not in allowed_target_ids:
            continue
        if scope.get("subnet_id") and target_subnet and scope["subnet_id"] != target_subnet:
            if target_filter:
                raise HTTPException(status_code=403, detail={"code": "scope_mismatch", "message": "Managed target is outside the requested subnet scope."})
            continue
        if scope.get("zone") and target_zone and scope["zone"] != target_zone:
            if target_filter:
                raise HTTPException(status_code=403, detail={"code": "zone_mismatch", "message": "Managed target is outside the requested zone scope."})
            continue
        items.append(item)
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "reports": items,
    }


@router.get("/v1/hubs/memory_profile/reports/{session_id}")
async def hub_memory_profile_report(
    session_id: str,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    root_token: str | None = Header(default=None, alias="X-Root-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_read_auth_or_legacy_root_token(
        authorization=authorization,
        owner_token=owner_token,
        root_token=root_token,
    )
    _enforce_mcp_capability("operations.read.targets", auth=auth)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    item = get_memory_profile_report(session_id)
    if item is None:
        raise HTTPException(status_code=404, detail="memory profile report was not found")
    report = item.get("report") if isinstance(item.get("report"), dict) else {}
    target_id = str(item.get("hub_id") or "").strip()
    allowed_target_ids = _allowed_target_ids(auth)
    if allowed_target_ids and target_id not in allowed_target_ids:
        raise HTTPException(status_code=403, detail={"code": "target_forbidden", "message": "Managed target is outside the token target allowlist."})
    target_subnet = str(report.get("subnet_id") or "").strip()
    target_zone = str(report.get("zone") or "").strip()
    if scope.get("subnet_id") and target_subnet and scope["subnet_id"] != target_subnet:
        raise HTTPException(status_code=403, detail={"code": "scope_mismatch", "message": "Managed target is outside the requested subnet scope."})
    if scope.get("zone") and target_zone and scope["zone"] != target_zone:
        raise HTTPException(status_code=403, detail={"code": "zone_mismatch", "message": "Managed target is outside the requested zone scope."})
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "report": item,
    }


@router.get("/v1/hubs/memory_profile/reports/{session_id}/artifacts/{artifact_id}")
async def hub_memory_profile_artifact(
    session_id: str,
    artifact_id: str,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    root_token: str | None = Header(default=None, alias="X-Root-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_read_auth_or_legacy_root_token(
        authorization=authorization,
        owner_token=owner_token,
        root_token=root_token,
    )
    _enforce_mcp_capability("operations.read.targets", auth=auth)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    report_item = get_memory_profile_report(session_id)
    if report_item is None:
        raise HTTPException(status_code=404, detail="memory profile report was not found")
    report = report_item.get("report") if isinstance(report_item.get("report"), dict) else {}
    target_id = str(report_item.get("hub_id") or "").strip()
    allowed_target_ids = _allowed_target_ids(auth)
    if allowed_target_ids and target_id not in allowed_target_ids:
        raise HTTPException(status_code=403, detail={"code": "target_forbidden", "message": "Managed target is outside the token target allowlist."})
    target_subnet = str(report.get("subnet_id") or "").strip()
    target_zone = str(report.get("zone") or "").strip()
    if scope.get("subnet_id") and target_subnet and scope["subnet_id"] != target_subnet:
        raise HTTPException(status_code=403, detail={"code": "scope_mismatch", "message": "Managed target is outside the requested subnet scope."})
    if scope.get("zone") and target_zone and scope["zone"] != target_zone:
        raise HTTPException(status_code=403, detail={"code": "zone_mismatch", "message": "Managed target is outside the requested zone scope."})
    artifact = get_memory_profile_artifact(session_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="memory profile artifact was not found")
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        **artifact,
    }


@router.get("/v1/hubs/memory_profile/reports/{session_id}/artifacts")
async def hub_memory_profile_artifacts(
    session_id: str,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    root_token: str | None = Header(default=None, alias="X-Root-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_read_auth_or_legacy_root_token(
        authorization=authorization,
        owner_token=owner_token,
        root_token=root_token,
    )
    _enforce_mcp_capability("operations.read.targets", auth=auth)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    report_item = get_memory_profile_report(session_id)
    if report_item is None:
        raise HTTPException(status_code=404, detail="memory profile report was not found")
    report = report_item.get("report") if isinstance(report_item.get("report"), dict) else {}
    target_id = str(report_item.get("hub_id") or "").strip()
    allowed_target_ids = _allowed_target_ids(auth)
    if allowed_target_ids and target_id not in allowed_target_ids:
        raise HTTPException(status_code=403, detail={"code": "target_forbidden", "message": "Managed target is outside the token target allowlist."})
    target_subnet = str(report.get("subnet_id") or "").strip()
    target_zone = str(report.get("zone") or "").strip()
    if scope.get("subnet_id") and target_subnet and scope["subnet_id"] != target_subnet:
        raise HTTPException(status_code=403, detail={"code": "scope_mismatch", "message": "Managed target is outside the requested subnet scope."})
    if scope.get("zone") and target_zone and scope["zone"] != target_zone:
        raise HTTPException(status_code=403, detail={"code": "zone_mismatch", "message": "Managed target is outside the requested zone scope."})
    artifacts = list_memory_profile_artifacts(session_id)
    if artifacts is None:
        raise HTTPException(status_code=404, detail="memory profile report was not found")
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        **artifacts,
    }


@root_router.get("/mcp/foundation")
async def root_mcp_foundation(
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    _enforce_mcp_capability("development.read.foundation", auth=auth)
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "foundation": foundation_snapshot(),
    }


@root_router.get("/mcp/contracts")
async def root_mcp_contracts(
    surface: str | None = None,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    _enforce_mcp_capability("development.read.contracts" if str(surface or "").strip().lower() != "operations" else "operations.read.contracts", auth=auth)
    contracts = [item.to_dict() for item in list_tool_contracts(surface=surface)]
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "contracts": contracts,
    }


@root_router.get("/mcp/descriptors")
async def root_mcp_descriptors(
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    _enforce_mcp_capability("development.read.descriptors", auth=auth)
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "descriptors": list_descriptor_registry(),
    }


@root_router.get("/mcp/descriptors/{descriptor_id}")
async def root_mcp_descriptor(
    descriptor_id: str,
    level: str = "std",
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    _enforce_mcp_capability("development.read.descriptors", auth=auth)
    try:
        descriptor = get_descriptor(descriptor_id, level=level)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"Descriptor '{descriptor_id}' was not found."}) from None
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "descriptor": descriptor,
    }


@root_router.get("/mcp/targets")
async def root_mcp_targets(
    environment: str | None = None,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    _enforce_mcp_capability("operations.read.targets", auth=auth)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    targets = list_managed_targets(environment=environment, subnet_id=scope.get("subnet_id"), zone=scope.get("zone"))
    allowed_target_ids = _allowed_target_ids(auth)
    if allowed_target_ids:
        targets = [item for item in targets if str(item.get("target_id") or "") in allowed_target_ids]
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "targets": targets,
    }


@root_router.post("/mcp/targets")
async def root_mcp_upsert_target(
    payload: RootMcpTargetUpsertRequest,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
) -> dict[str, Any]:
    auth = _require_root_write_auth(authorization=authorization, owner_token=owner_token)
    _enforce_mcp_capability("operations.write.targets", auth=auth)
    target = upsert_managed_target(payload.model_dump(mode="python"))
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "target": target.to_dict(),
    }


@root_router.get("/mcp/targets/{target_id}")
async def root_mcp_target(
    target_id: str,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    _enforce_mcp_capability("operations.read.targets", auth=auth)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    target = get_managed_target(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "target_not_found", "message": f"Managed target '{target_id}' was not found."})
    allowed_target_ids = _allowed_target_ids(auth)
    if allowed_target_ids and target_id not in allowed_target_ids:
        raise HTTPException(status_code=403, detail={"code": "target_forbidden", "message": "Managed target is outside the token target allowlist."})
    if scope.get("subnet_id") and target.get("subnet_id") and scope["subnet_id"] != target["subnet_id"]:
        raise HTTPException(status_code=403, detail={"code": "scope_mismatch", "message": "Managed target is outside the requested subnet scope."})
    if scope.get("zone") and target.get("zone") and scope["zone"] != target["zone"]:
        raise HTTPException(status_code=403, detail={"code": "zone_mismatch", "message": "Managed target is outside the requested zone scope."})
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "target": target,
    }


@root_router.get("/mcp/audit")
async def root_mcp_audit(
    limit: int = 50,
    tool_id: str | None = None,
    trace_id: str | None = None,
    actor: str | None = None,
    target_id: str | None = None,
    subnet_filter: str | None = None,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    _enforce_mcp_capability("audit.read", auth=auth)
    events = recent_audit_events(
        limit=max(1, min(int(limit), 200)),
        tool_id=tool_id,
        trace_id=trace_id,
        actor=actor,
        target_id=target_id,
        subnet_id=subnet_filter,
    )
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "events": events,
    }


@root_router.post("/mcp/access-tokens")
async def root_mcp_issue_access_token(
    payload: RootMcpAccessTokenIssueRequest,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
) -> dict[str, Any]:
    auth = _require_root_write_auth(authorization=authorization, owner_token=owner_token)
    _enforce_mcp_capability("operations.issue.tokens", auth=auth)
    issued = issue_access_token(
        audience=payload.audience,
        actor=str(auth.get("actor") or "root:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        ttl_seconds=payload.ttl_seconds,
        capabilities=list(payload.capabilities or []),
        subnet_id=payload.subnet_id,
        zone=payload.zone,
        target_id=payload.target_id,
        target_ids=list(payload.target_ids or []),
        note=payload.note,
    )
    audit_event_id = _append_direct_root_mcp_audit(
        tool_id="root.access_tokens.issue",
        actor=str(auth.get("actor") or "root:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        capability="operations.issue.tokens",
        status="ok",
        target_id=payload.target_id,
        meta={
            "subnet_id": issued.get("subnet_id"),
            "zone": issued.get("zone"),
            "audience": issued.get("audience"),
            "token_id": issued.get("token_id"),
        },
        result_summary={"kind": "access_token", "token_id": issued.get("token_id")},
        redactions=["result.access_token"],
    )
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "token": issued,
        "audit_event_id": audit_event_id,
    }


@root_router.get("/mcp/access-tokens")
async def root_mcp_list_access_tokens(
    limit: int = 100,
    status: str | None = None,
    audience: str | None = None,
    target_id: str | None = None,
    active_only: bool = False,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    _enforce_mcp_capability("operations.read.tokens", auth=auth)
    tokens = list_access_tokens(
        limit=max(1, min(int(limit), 200)),
        status=status,
        audience=audience,
        target_id=target_id,
        active_only=active_only,
    )
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "tokens": tokens,
    }


@root_router.post("/mcp/access-tokens/{token_id}/revoke")
async def root_mcp_revoke_access_token(
    token_id: str,
    payload: RootMcpAccessTokenRevokeRequest,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
) -> dict[str, Any]:
    auth = _require_root_write_auth(authorization=authorization, owner_token=owner_token)
    _enforce_mcp_capability("operations.revoke.tokens", auth=auth)
    try:
        record = revoke_access_token(
            token_id,
            actor=str(auth.get("actor") or "root:unknown"),
            auth_method=str(auth.get("method") or "unknown"),
            reason=payload.reason,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "token_not_found", "message": "Access token not found."}) from None
    audit_event_id = _append_direct_root_mcp_audit(
        tool_id="root.access_tokens.revoke",
        actor=str(auth.get("actor") or "root:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        capability="operations.revoke.tokens",
        status="ok",
        target_id=str(record.get("primary_target_id") or ""),
        meta={
            "subnet_id": record.get("subnet_id"),
            "zone": record.get("zone"),
            "audience": record.get("audience"),
            "token_id": record.get("token_id"),
        },
        result_summary={"kind": "access_token", "token_id": record.get("token_id"), "status": record.get("status")},
    )
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "token": record,
        "audit_event_id": audit_event_id,
    }


@root_router.post("/mcp/call")
async def root_mcp_call(
    payload: RootMcpCallRequest,
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    scope = _effective_mcp_scope(auth=auth, subnet_id=subnet_id, zone=zone)
    response = invoke_tool(
        payload.tool_id,
        arguments=payload.arguments,
        request_id=payload.request_id,
        trace_id=payload.trace_id,
        actor=str(auth.get("actor") or "root:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        dry_run=payload.dry_run,
        scope=scope,
        auth_context=auth,
    )
    return {"ok": response.ok, "scope": scope, "response": response.to_dict()}


router.include_router(root_router)
router.include_router(subnet_router)
