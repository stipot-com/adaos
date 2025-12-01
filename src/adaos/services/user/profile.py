from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from adaos.services.agent_context import AgentContext, get_ctx


@dataclass(slots=True)
class UserProfile:
    user_id: str
    settings: Dict[str, object]


class UserProfileService:
    """
    Minimal user profile layer for MVP.

    - Uses Settings.owner_id as the single logical user id.
    - Persists profile settings in ctx.kv under:
        users/<user_id>/settings
    - Coordinates with ProjectionRegistry / Yjs via higher-level tools.
    """

    def __init__(self, ctx: Optional[AgentContext] = None) -> None:
        self.ctx: AgentContext = ctx or get_ctx()

    def current_user_id(self) -> str:
        """
        Return the logical user identifier for the current process.
        For MVP this is Settings.owner_id or 'local-owner' if not set.
        """
        owner = getattr(self.ctx.settings, "owner_id", None) or "local-owner"
        return str(owner)

    def _kv_key(self, user_id: str) -> str:
        return f"users/{user_id}/settings"

    def get_profile(self, user_id: Optional[str] = None) -> UserProfile:
        uid = user_id or self.current_user_id()
        raw = self.ctx.kv.get(self._kv_key(uid), {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        return UserProfile(user_id=uid, settings=dict(raw))

    def update_profile(self, settings: Dict[str, object], user_id: Optional[str] = None) -> UserProfile:
        uid = user_id or self.current_user_id()
        current = self.get_profile(uid).settings
        current.update(settings)
        self.ctx.kv.set(self._kv_key(uid), dict(current))
        return UserProfile(user_id=uid, settings=current)


__all__ = ["UserProfile", "UserProfileService"]

