from __future__ import annotations
import json
import hashlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from adaos.adapters.db import sqlite as sqlite_db
from adaos.apps.api.auth import require_owner_token
from adaos.services.id_gen import new_id
from adaos.services.root.service import RootAuthService

router = APIRouter()
root_router = APIRouter(prefix="/v1/root", tags=["root"])
subnet_router = APIRouter(prefix="/v1/subnets", tags=["subnets"])
router.include_router(root_router)
router.include_router(subnet_router)


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
    owner_id = RootAuthService.resolve_owner(payload.owner_token)
    canonical_source = payload.model_dump(mode="python")
    body_hash = hashlib.sha256(_canonical_body(canonical_source).encode("utf-8")).hexdigest()

    if idempotency_key:
        cached = sqlite_db.idem_get(idempotency_key, "POST", "/v1/subnets/register", owner_id, body_hash)
        if cached:
            return Response(content=cached["body_json"], status_code=cached["status_code"], media_type="application/json")

    result = RootAuthService.register_subnet(payload.owner_token, payload.csr_pem, payload.fingerprint, hints=payload.hints)
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
    owner_id = RootAuthService.resolve_owner(owner_token)
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
