import os
import time
from typing import Dict, Any

from fastapi import Depends, Header, HTTPException, status

from adaos.adapters.db import sqlite as sqlite_db
from adaos.services.node_config import load_config


def _expected_token() -> str:
    try:
        return load_config().token or "dev-local-token"
    except Exception:
        return "dev-local-token"


async def require_token(
    x_adaos_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """
    Принимаем либо X-AdaOS-Token, либо Authorization: Bearer <token>.
    """
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_adaos_token:
        token = x_adaos_token

    if token != _expected_token():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-AdaOS-Token",
        )


def require_owner_token(token: str) -> None:
    expected = os.getenv("ADAOS_ROOT_OWNER_TOKEN") or ""
    if not expected or token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid owner token"
        )


async def require_hub_session(
    authorization: str = Header(...)
) -> Dict[str, Any]:
    """Middleware для проверки сессии хаба"""
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Invalid authorization header")

    session_token = authorization[7:]  # Remove "Bearer " prefix

    # Получаем сессию из БД
    session = sqlite_db.get_auth_session(session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Invalid session token")

    # Проверяем срок действия
    if session.get("expires_at", 0) < int(time.time()):
        sqlite_db.delete_expired_sessions()
        raise HTTPException(status_code=401, detail="Session expired")

    # Получаем информацию о хабе
    hub = sqlite_db.get_hub_registration(session["hub_id"])
    if not hub or hub.get("status") != "active":
        raise HTTPException(status_code=401, detail="Hub not active")

    return {
        "session": session,
        "hub": hub
    }


async def require_authentication(
    authorization: str = Header(None),
    x_owner_token: str = Header(None),
    x_adaos_token: str = Header(None)
) -> Dict[str, Any]:
    """
    Универсальная аутентификация - поддерживает все типы:
    - Hub session (Bearer token)
    - Owner token
    - Local node token
    """
    # 1. Проверяем hub session
    if authorization and authorization.startswith("Bearer "):
        return await require_hub_session(authorization)

    # 2. Проверяем owner token
    elif x_owner_token:
        require_owner_token(x_owner_token)
        return {
            "auth_type": "owner",
            "owner_id": os.getenv("ADAOS_ROOT_OWNER_ID") or "local-owner"
        }

    # 3. Проверяем local node token (обратная совместимость)
    elif x_adaos_token:
        token = x_adaos_token
        if token != _expected_token():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-AdaOS-Token",
            )
        return {
            "auth_type": "node",
            "node_id": "local-node"  # Можно добавить получение node_id из конфига
        }

    else:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Use: Authorization: Bearer <session_token>, X-Owner-Token, or X-AdaOS-Token"
        )


async def require_hub_permission(
    permission: str,
    auth_info: Dict[str, Any] = Depends(require_authentication)
) -> Dict[str, Any]:
    """
    Проверка конкретных прав доступа для хаба
    """
    if auth_info.get("auth_type") != "hub":
        # Owner и node имеют полные права
        return auth_info

    session = auth_info.get("session", {})
    permissions = session.get("permissions", [])

    if permission not in permissions:
        raise HTTPException(
            status_code=403,
            detail=f"Permission denied: {permission}"
        )

    return auth_info


# Специализированные зависимости для разных типов доступа
async def require_api_read(
    auth_info: Dict[str, Any] = Depends(require_authentication)
) -> Dict[str, Any]:
    """Требует права на чтение API"""
    return await require_hub_permission("api:read", auth_info)


async def require_api_write(
    auth_info: Dict[str, Any] = Depends(require_authentication)
) -> Dict[str, Any]:
    """Требует права на запись в API"""
    return await require_hub_permission("api:write", auth_info)


async def require_repo_access(
    auth_info: Dict[str, Any] = Depends(require_authentication)
) -> Dict[str, Any]:
    """Требует права доступа к репозиториям"""
    return await require_hub_permission("repo:access", auth_info)


async def require_owner_only(
    auth_info: Dict[str, Any] = Depends(require_authentication)
) -> Dict[str, Any]:
    """Только для owner аутентификации"""
    if auth_info.get("auth_type") != "owner":
        raise HTTPException(
            status_code=403,
            detail="Owner authentication required"
        )
    return auth_info


async def require_hub_only(
    auth_info: Dict[str, Any] = Depends(require_authentication)
) -> Dict[str, Any]:
    """Только для hub аутентификации"""
    if auth_info.get("auth_type") != "hub":
        raise HTTPException(
            status_code=403,
            detail="Hub authentication required"
        )
    return auth_info
