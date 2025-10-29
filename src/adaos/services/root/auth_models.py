# services/root/auth_models.py
"""
Модели данных для беспарольной аутентификации хабов.
"""
from typing import Dict, List, Optional, Any
import json
from datetime import datetime, timedelta


def create_hub_registration(
    hub_id: str,
    public_key: str,
    hub_name: str,
    capabilities: List[str] = None,
    status: str = "active"
) -> Dict[str, Any]:
    """Создание записи регистрации хаба"""
    return {
        "hub_id": hub_id,
        "public_key": public_key,
        "hub_name": hub_name,
        "capabilities": capabilities or ["basic", "skills", "scenarios"],
        "created_at": int(datetime.utcnow().timestamp()),
        "status": status
    }


def create_auth_session(
    hub_id: str,
    session_token: str,
    permissions: List[str] = None,
    ttl_hours: int = 24
) -> Dict[str, Any]:
    """Создание сессии аутентификации"""
    issued_at = int(datetime.utcnow().timestamp())
    expires_at = issued_at + (ttl_hours * 3600)

    return {
        "session_token": session_token,
        "hub_id": hub_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "permissions": permissions or ["api:read", "api:write", "repo:access"]
    }


def validate_hub_registration(data: Dict[str, Any]) -> bool:
    """Валидация данных регистрации хаба"""
    required_fields = ["hub_id", "public_key", "hub_name", "status"]
    return all(field in data for field in required_fields)
