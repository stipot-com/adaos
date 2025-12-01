from __future__ import annotations

from typing import Any, Dict, Optional

from adaos.sdk.core._ctx import require_ctx
from adaos.services.user.profile import UserProfileService


def _svc() -> UserProfileService:
    ctx = require_ctx("sdk.data.profile")
    return UserProfileService(ctx)


def get_settings(user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Return profile settings for the given user_id or for the current
    logical user (owner_id) when user_id is omitted.
    """
    return _svc().get_profile(user_id).settings


def update_settings(patch: Dict[str, Any], user_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Merge the given patch into the user's profile settings and return
    the updated settings mapping.
    """
    prof = _svc().update_profile(patch, user_id)
    return prof.settings


__all__ = ["get_settings", "update_settings"]

