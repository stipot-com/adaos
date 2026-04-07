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
from adaos.services.root_mcp.policy import evaluate_direct_access

router = APIRouter()
root_router = APIRouter(prefix="/v1/root", tags=["root"])
subnet_router = APIRouter(prefix="/v1/subnets", tags=["subnets"])


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_owner_id(owner_token: str) -> str:
    require_owner_token(owner_token)
    return os.getenv("ADAOS_ROOT_OWNER_ID") or "local-owner"


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
    return _require_root_write_auth(authorization=authorization, owner_token=owner_token)


def _mcp_scope(*, subnet_id: str | None, zone: str | None) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    token = str(subnet_id or "").strip()
    if token:
        scope["subnet_id"] = token
    token = str(zone or "").strip()
    if token:
        scope["zone"] = token
    return scope


def _enforce_mcp_capability(required_capability: str, *, auth: dict[str, Any]) -> None:
    decision = evaluate_direct_access(
        required_capability,
        actor=str(auth.get("actor") or "root:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
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


@root_router.get("/mcp/foundation")
async def root_mcp_foundation(
    authorization: str | None = Header(default=None),
    owner_token: str | None = Header(default=None, alias="X-Owner-Token"),
    subnet_id: str | None = Header(default=None, alias="X-AdaOS-Subnet-Id"),
    zone: str | None = Header(default=None, alias="X-AdaOS-Zone"),
) -> dict[str, Any]:
    auth = _require_root_access_auth(authorization=authorization, owner_token=owner_token)
    _enforce_mcp_capability("development.read.foundation", auth=auth)
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": _mcp_scope(subnet_id=subnet_id, zone=zone),
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
    _enforce_mcp_capability("development.read.contracts" if str(surface or "").strip().lower() != "operations" else "operations.read.contracts", auth=auth)
    contracts = [item.to_dict() for item in list_tool_contracts(surface=surface)]
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": _mcp_scope(subnet_id=subnet_id, zone=zone),
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
    _enforce_mcp_capability("development.read.descriptors", auth=auth)
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": _mcp_scope(subnet_id=subnet_id, zone=zone),
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
    _enforce_mcp_capability("development.read.descriptors", auth=auth)
    try:
        descriptor = get_descriptor(descriptor_id, level=level)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": f"Descriptor '{descriptor_id}' was not found."}) from None
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": _mcp_scope(subnet_id=subnet_id, zone=zone),
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
    scope = _mcp_scope(subnet_id=subnet_id, zone=zone)
    return {
        "ok": True,
        "auth": {"method": auth.get("method")},
        "scope": scope,
        "targets": list_managed_targets(environment=environment, subnet_id=scope.get("subnet_id"), zone=scope.get("zone")),
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
    scope = _mcp_scope(subnet_id=subnet_id, zone=zone)
    target = get_managed_target(target_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "target_not_found", "message": f"Managed target '{target_id}' was not found."})
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
        "scope": _mcp_scope(subnet_id=subnet_id, zone=zone),
        "events": events,
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
    scope = _mcp_scope(subnet_id=subnet_id, zone=zone)
    response = invoke_tool(
        payload.tool_id,
        arguments=payload.arguments,
        request_id=payload.request_id,
        trace_id=payload.trace_id,
        actor=str(auth.get("actor") or "root:unknown"),
        auth_method=str(auth.get("method") or "unknown"),
        dry_run=payload.dry_run,
        scope=scope,
    )
    return {"ok": response.ok, "scope": scope, "response": response.to_dict()}


router.include_router(root_router)
router.include_router(subnet_router)
