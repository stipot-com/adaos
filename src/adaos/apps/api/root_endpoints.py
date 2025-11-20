from __future__ import annotations
import asyncio
import json
import hashlib
import os
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from openai import OpenAI
from openai import OpenAIError

from adaos.adapters.db import sqlite as sqlite_db
from adaos.apps.api.auth import require_owner_token
from adaos.services.id_gen import new_id
from adaos.services.root.service import RootAuthService

router = APIRouter()
root_router = APIRouter(prefix="/v1/root", tags=["root"])
subnet_router = APIRouter(prefix="/v1/subnets", tags=["subnets"])
router.include_router(root_router)
router.include_router(subnet_router)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model: str = Field(default="gpt-4o-mini", description="LLM model identifier")
    messages: list[ChatMessage]
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0, le=1)


def _get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is not configured")
    return OpenAI(api_key=api_key)


@root_router.post("/register")
def root_register() -> dict:
    """Legacy bootstrap endpoint has been replaced by owner-based flow."""
    raise RuntimeError("legacy endpoint removed; use owner login flow")


@root_router.post("/llm/chat")
async def llm_chat(payload: ChatRequest) -> dict:
    client = _get_openai_client()
    request_payload: dict = {
        "model": payload.model,
        "messages": [msg.model_dump() for msg in payload.messages],
    }
    if payload.temperature is not None:
        request_payload["temperature"] = payload.temperature
    if payload.max_tokens is not None:
        request_payload["max_tokens"] = payload.max_tokens
    if payload.top_p is not None:
        request_payload["top_p"] = payload.top_p

    try:
        completion = await asyncio.to_thread(client.chat.completions.create, **request_payload)
    except OpenAIError as exc:
        status = getattr(exc, "status_code", None) or 502
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - safety net for unexpected SDK errors
        raise HTTPException(status_code=502, detail=f"OpenAI request failed: {exc}") from exc

    return completion.model_dump()


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
