from __future__ import annotations

from typing import Any, Optional

from adaos.sdk.core._ctx import require_ctx
from adaos.services.scenario import ProjectionService
from adaos.services.user.profile import UserProfileService


class _ScopeCtx:
    def __init__(self, scope: str) -> None:
        self._scope = scope

    async def set_async(
        self,
        slot: str,
        value: Any,
        *,
        user_id: Optional[str] = None,
        webspace_id: Optional[str] = None,
    ) -> None:
        """
        Async variant for use inside async skills/handlers.
        """
        ctx = require_ctx(f"sdk.data.ctx.{self._scope}.set")
        svc = ProjectionService.from_ctx(ctx)
        await svc.apply(self._scope, slot, value, user_id=user_id, webspace_id=webspace_id)

    def set(
        self,
        slot: str,
        value: Any,
        *,
        user_id: Optional[str] = None,
        webspace_id: Optional[str] = None,
    ) -> None:
        """
        Synchronous helper for ctx.<scope>.set(slot, value).

        For now this is a thin wrapper over the async variant; blocking
        is acceptable for MVP as writes are small and targeted.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.set_async(slot, value, user_id=user_id, webspace_id=webspace_id))
        else:
            loop.create_task(self.set_async(slot, value, user_id=user_id, webspace_id=webspace_id))


class _CurrentUserCtx(_ScopeCtx):
    def __init__(self) -> None:
        super().__init__("current_user")

    def get_profile_settings(self) -> dict:
        ctx = require_ctx("sdk.data.ctx.current_user.get_profile_settings")
        svc = UserProfileService(ctx)
        return svc.get_profile().settings


subnet = _ScopeCtx("subnet")
current_user = _CurrentUserCtx()
selected_user = _ScopeCtx("selected_user")

__all__ = ["subnet", "current_user", "selected_user"]
