# services/root/auth_middleware.py
from fastapi import Request, HTTPException, Depends
import time
from typing import Dict, Any

from adaos.adapters.db import sqlite as sqlite_db


async def verify_hub_session(request: Request) -> Dict[str, Any]:
    """Верификация сессии хаба"""
    # Получаем Authorization header
    authorization = request.headers.get("Authorization")
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Authorization header required"
        )

    # Парсим Bearer token
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Invalid authentication scheme. Use: Bearer <token>"
        )

    session_token = parts[1]
    session = sqlite_db.get_auth_session(session_token)

    if not session:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired session"
        )

    # Проверяем срок действия сессии
    current_time = int(time.time())
    if session["expires_at"] < current_time:
        sqlite_db.delete_expired_sessions()
        raise HTTPException(
            status_code=401,
            detail="Session expired"
        )

    # Проверяем, что хаб все еще активен
    hub_reg = sqlite_db.get_hub_registration(session["hub_id"])
    if not hub_reg or hub_reg.get("status") != "active":
        raise HTTPException(
            status_code=401,
            detail="Hub registration revoked"
        )

    # Добавляем информацию в request state
    request.state.hub_session = session
    request.state.hub_registration = hub_reg

    return session

# Зависимости для использования в endpoints


def require_hub_auth():
    """Зависимость для защиты endpoints"""
    return Depends(verify_hub_session)


def get_current_hub(request: Request) -> Dict[str, Any]:
    """Получение текущего аутентифицированного хаба"""
    return getattr(request.state, "hub_session", {})


def get_hub_permissions(request: Request) -> list:
    """Получение permissions текущего хаба"""
    session = getattr(request.state, "hub_session", {})
    return session.get("permissions", [])


def check_permission(required_permission: str):
    """Декоратор для проверки конкретного permission"""
    async def permission_checker(session: Dict[str, Any] = Depends(verify_hub_session)):
        permissions = session.get("permissions", [])
        if required_permission not in permissions:
            raise HTTPException(
                status_code=403,
                detail=f"Permission denied: {required_permission} required"
            )
        return True
    return Depends(permission_checker)
