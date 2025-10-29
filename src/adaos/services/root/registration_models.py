# services/root/registration_models.py
from pydantic import BaseModel
from typing import Optional, Dict, Any


class HubRegistrationRequest(BaseModel):
    """Запрос на регистрацию нового хаба"""
    hub_id: str
    hub_name: str
    public_key: str
    capabilities: Optional[list[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class HubRegistrationResponse(BaseModel):
    """Ответ на регистрацию хаба"""
    status: str
    hub_id: str
    session_token: str
    expires_at: int
    permissions: list[str]
