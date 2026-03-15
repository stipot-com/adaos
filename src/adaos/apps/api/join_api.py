from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from adaos.apps.api.auth import require_token
from adaos.services.agent_context import get_ctx
from adaos.services.join_codes import (
    JoinCodeConsumed,
    JoinCodeExpired,
    JoinCodeNotFound,
    create as create_join_code,
    consume as consume_join_code,
)

router = APIRouter(tags=["join"])


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat().replace("+00:00", "Z")


class JoinCodeCreateRequest(BaseModel):
    ttl_minutes: int = Field(15, ge=1, le=60)
    length: int = Field(8, ge=8, le=12)


class JoinCodeCreateResponse(BaseModel):
    ok: bool
    code: str
    expires_at_utc: str


@router.post("/node/join-code", response_model=JoinCodeCreateResponse, dependencies=[Depends(require_token)])
async def join_code_create(payload: JoinCodeCreateRequest):
    conf = get_ctx().config
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node can create join-codes")
    info = create_join_code(
        subnet_id=conf.subnet_id,
        ttl_seconds=int(payload.ttl_minutes) * 60,
        length=int(payload.length),
        meta={"kind": "subnet.member.join"},
        ctx=get_ctx(),
    )
    return JoinCodeCreateResponse(ok=True, code=info.code, expires_at_utc=_iso_utc(info.expires_at))


class JoinConsumeRequest(BaseModel):
    code: str
    node_id: str | None = None
    hostname: str | None = None


class JoinConsumeResponse(BaseModel):
    ok: bool
    subnet_id: str
    token: str
    root_url: str
    hub_url: str
    diagnostics: dict[str, Any]


@router.post("/node/join", response_model=JoinConsumeResponse)
async def join_consume(req: Request, payload: JoinConsumeRequest):
    """
    Exchange short join-code for subnet connection parameters.

    Auth: join-code only (one-time, TTL).
    """
    conf = get_ctx().config
    if conf.role != "hub":
        raise HTTPException(status_code=403, detail="only hub node accepts joins")

    code = str(payload.code or "").strip()
    if not code:
        raise HTTPException(status_code=422, detail="code is required")

    try:
        rec = consume_join_code(code=code, subnet_id=conf.subnet_id, ctx=get_ctx())
    except JoinCodeNotFound:
        raise HTTPException(status_code=404, detail="join-code not found") from None
    except JoinCodeExpired:
        raise HTTPException(status_code=410, detail="join-code expired") from None
    except JoinCodeConsumed:
        raise HTTPException(status_code=409, detail="join-code already used") from None

    token = conf.token or "dev-local-token"
    root_url = str(req.base_url).rstrip("/")
    hub_url = root_url
    diags = {
        "subnet_id": conf.subnet_id,
        "hub_node_id": conf.node_id,
        "node_id_hint": (payload.node_id or "").strip() or None,
        "root_url": root_url,
        "rendezvous_url": hub_url,
        "code_created_at_utc": _iso_utc(float(rec.get("created_at") or 0.0)) if rec.get("created_at") else None,
        "code_expires_at_utc": _iso_utc(float(rec.get("expires_at") or 0.0)) if rec.get("expires_at") else None,
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return JoinConsumeResponse(ok=True, subnet_id=conf.subnet_id, token=token, root_url=root_url, hub_url=hub_url, diagnostics=diags)
