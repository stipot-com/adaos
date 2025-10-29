from __future__ import annotations
import asyncio
import json
import hashlib
import os
import base64
from datetime import datetime, timezone
from typing import Any, Literal, List

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from openai import OpenAI
from openai import OpenAIError

from adaos.adapters.db import sqlite as sqlite_db
from adaos.apps.api.auth import require_owner_token, require_owner_only
from adaos.services.id_gen import new_id
from adaos.services.root.service import RootAuthService
from adaos.services.crypto.pki import get_pki_service

router = APIRouter()
root_router = APIRouter(prefix="/v1/root", tags=["root"])
subnet_router = APIRouter(prefix="/v1/subnets", tags=["subnets"])
router.include_router(root_router)
router.include_router(subnet_router)


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model: str = Field(default="gpt-4o-mini",
                       description="LLM model identifier")
    messages: list[ChatMessage]
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0, le=1)


def _get_openai_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503, detail="OPENAI_API_KEY is not configured")
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
        raise HTTPException(
            status_code=502, detail=f"OpenAI request failed: {exc}") from exc

    return completion.model_dump()


class SubnetRegisterRequest(BaseModel):
    csr_pem: str
    fingerprint: str
    owner_token: str
    hints: dict[str, Any] | None = None


# ===== НОВЫЕ МОДЕЛИ ДЛЯ АУТЕНТИФИКАЦИИ =====
class HubRegistrationRequest(BaseModel):
    hub_id: str
    public_key: str
    hub_name: str


class AuthChallengeRequest(BaseModel):
    hub_id: str


class AuthVerifyRequest(BaseModel):
    hub_id: str
    challenge: str
    signature: str


class AuthSessionResponse(BaseModel):
    status: str
    session_token: str
    expires_at: int
    permissions: list[str]


def _canonical_body(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@subnet_router.post("/register")
async def subnet_register(
    request: Request,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"),
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
    body_hash = hashlib.sha256(_canonical_body(
        canonical_source).encode("utf-8")).hexdigest()

    if idempotency_key:
        cached = sqlite_db.idem_get(
            idempotency_key, "POST", "/v1/subnets/register", owner_id, body_hash)
        if cached:
            return Response(content=cached["body_json"], status_code=cached["status_code"], media_type="application/json")

    result = RootAuthService.register_subnet(
        payload.owner_token, payload.csr_pem, payload.fingerprint, hints=payload.hints)
    response_body = json.dumps(
        result, ensure_ascii=False, separators=(",", ":"))
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
    device = sqlite_db.device_get_by_fingerprint(
        subnet["subnet_id"], fingerprint)
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


# ===== НОВЫЕ ENDPOINTS ДЛЯ БЕСПАРОЛЬНОЙ АУТЕНТИФИКАЦИИ =====
@root_router.post("/auth/register")
async def hub_register(request: HubRegistrationRequest) -> dict:
    """
    Регистрация нового хаба с публичным ключом для беспарольной аутентификации
    """
    pki_service = get_pki_service()

    try:
        result = pki_service.register_hub(
            hub_id=request.hub_id,
            public_key_pem=request.public_key,
            hub_name=request.hub_name
        )
        return {
            "status": "success",
            "data": result,
            "event_id": new_id(),
            "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@root_router.post("/auth/challenge")
async def auth_challenge(request: AuthChallengeRequest) -> dict:
    """
    Создание cryptographic challenge для аутентификации хаба
    """
    pki_service = get_pki_service()

    try:
        challenge = pki_service.create_auth_challenge(request.hub_id)
        return {
            "status": "success",
            "challenge": challenge,
            "event_id": new_id(),
            "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@root_router.post("/auth/verify")
async def auth_verify(request: AuthVerifyRequest) -> AuthSessionResponse:
    """
    Верификация подписи challenge и создание сессии аутентификации
    """
    pki_service = get_pki_service()

    try:
        is_valid = pki_service.verify_challenge_signature(
            hub_id=request.hub_id,
            challenge=request.challenge,
            signature_b64=request.signature
        )

        if is_valid:
            session = pki_service.create_auth_session(request.hub_id)
            return AuthSessionResponse(
                status="authenticated",
                session_token=session["session_token"],
                expires_at=session.get("expires_at", int(
                    datetime.now(timezone.utc).timestamp()) + 86400),
                permissions=session["permissions"]
            )
        else:
            raise HTTPException(status_code=401, detail="Invalid signature")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@root_router.get("/auth/session/{session_token}")
async def get_session_info(session_token: str) -> dict:
    """
    Получение информации о сессии аутентификации
    """
    session = sqlite_db.get_auth_session(session_token)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Проверяем не истекла ли сессия
    if session.get("expires_at", 0) < int(datetime.now(timezone.utc).timestamp()):
        sqlite_db.delete_expired_sessions()
        raise HTTPException(status_code=401, detail="Session expired")

    return {
        "status": "success",
        "session": session,
        "event_id": new_id(),
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }


@root_router.post("/auth/cleanup")
async def cleanup_expired_sessions() -> dict:
    """Очистка просроченных сессий и challenges"""
    from adaos.adapters.db.sqlite import delete_expired_sessions, cleanup_expired_challenges

    # Эта функция теперь должна возвращать количество
    sessions_deleted = delete_expired_sessions()
    challenges_deleted = cleanup_expired_challenges()

    return {
        "status": "success",
        "message": f"Cleaned up {sessions_deleted} sessions and {challenges_deleted} challenges",
        "event_id": new_id(),
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }


@root_router.get("/auth/hubs")
async def list_registered_hubs() -> dict:
    """
    Список зарегистрированных хабов (административный endpoint)
    """
    hubs = sqlite_db.list_active_hubs()

    return {
        "status": "success",
        "hubs": hubs,
        "count": len(hubs),
        "event_id": new_id(),
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }


@root_router.post("/auth/hubs/{hub_id}/permissions")
async def update_hub_permissions(
    hub_id: str,
    permissions: List[str],
    auth=Depends(require_owner_only)  # Только owner может менять права
) -> dict:
    """Обновление прав доступа для хаба"""
    hub = sqlite_db.get_hub_registration(hub_id)
    if not hub:
        raise HTTPException(status_code=404, detail="Hub not found")

    # Обновляем сессии хаба с новыми правами
    from adaos.adapters.db.sqlite import update_hub_sessions_permissions
    updated_count = update_hub_sessions_permissions(hub_id, permissions)

    return {
        "status": "success",
        "message": f"Permissions updated for hub {hub_id}",
        "permissions": permissions,
        "sessions_updated": updated_count,
        "event_id": new_id(),
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }


@root_router.delete("/auth/session/{session_token}")
async def revoke_session(
    session_token: str,
    auth=Depends(require_owner_only)  # Только owner может отзывать сессии
) -> dict:
    """Принудительное завершение сессии"""
    session = sqlite_db.get_auth_session(session_token)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Удаляем сессию
    from adaos.adapters.db.sqlite import delete_auth_session
    delete_auth_session(session_token)

    return {
        "status": "success",
        "message": f"Session {session_token} revoked",
        "event_id": new_id(),
        "server_time_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    }
