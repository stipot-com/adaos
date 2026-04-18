# \src\adaos\services\scenario\projection_service.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional
import json
import logging

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.yjs.doc import mutate_live_room, async_get_ydoc
from adaos.services.user.profile import UserProfileService
from .projection_registry import ProjectionRegistry, ProjectionTarget

_log = logging.getLogger("adaos.scenario.projection")


def _clone_json_like(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except Exception:
        if isinstance(value, dict):
            return {str(k): _clone_json_like(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_clone_json_like(v) for v in value]
        items = getattr(value, "items", None)
        if callable(items):
            try:
                return {str(k): _clone_json_like(v) for k, v in items()}
            except Exception:
                return value
        return value


def _mapping_items(value: Any) -> list[tuple[str, Any]] | None:
    if isinstance(value, dict):
        return [(str(key), item) for key, item in value.items() if str(key)]
    items = getattr(value, "items", None)
    if callable(items):
        try:
            return [(str(key), item) for key, item in items() if str(key)]
        except Exception:
            return None
    return None


def _json_like_equal(current: Any, next_value: Any) -> bool:
    if current is next_value:
        return True

    current_items = _mapping_items(current)
    next_items = _mapping_items(next_value)
    if current_items is not None or next_items is not None:
        if current_items is None or next_items is None:
            return False
        if len(current_items) != len(next_items):
            return False
        next_lookup = {key: item for key, item in next_items}
        if len(next_lookup) != len(next_items):
            return False
        for key, current_item in current_items:
            if key not in next_lookup:
                return False
            if not _json_like_equal(current_item, next_lookup[key]):
                return False
        return True

    if isinstance(current, (list, tuple)) or isinstance(next_value, (list, tuple)):
        if not isinstance(current, (list, tuple)) or not isinstance(next_value, (list, tuple)):
            return False
        if len(current) != len(next_value):
            return False
        return all(_json_like_equal(left, right) for left, right in zip(current, next_value))

    try:
        return current == next_value
    except Exception:
        return _clone_json_like(current) == _clone_json_like(next_value)


def _merge_nested_path(existing: Any, segments: List[str], payload: Any) -> tuple[bool, Any]:
    if not segments:
        if _json_like_equal(existing, payload):
            return False, existing
        return True, _clone_json_like(payload)

    key = str(segments[0] or "")
    if not key:
        return False, _clone_json_like(existing)

    child_existing = None
    if isinstance(existing, dict):
        child_existing = existing.get(key)
    else:
        items = _mapping_items(existing)
        if items is not None:
            for item_key, item_value in items:
                if item_key == key:
                    child_existing = item_value
                    break

    changed, merged_child = _merge_nested_path(child_existing, segments[1:], payload)
    if not changed:
        return False, existing

    base = _clone_json_like(existing)
    if not isinstance(base, dict):
        base = {}
    merged = dict(base)
    merged[key] = merged_child
    return True, merged


@dataclass(slots=True)
class ProjectionService:
    """
    Apply logical ctx.* writes to physical backends using ProjectionRegistry.

    For MVP supports:
      - backend="yjs": writes to YDoc paths (data/...),
      - backend="kv":  profile settings via UserProfileService (current_user).
    """

    ctx: AgentContext
    registry: ProjectionRegistry

    @classmethod
    def from_ctx(cls, ctx: Optional[AgentContext] = None) -> "ProjectionService":
        c = ctx or get_ctx()
        return cls(ctx=c, registry=c.projections)

    async def apply(
        self,
        scope: str,
        slot: str,
        value: Any,
        *,
        user_id: Optional[str] = None,
        webspace_id: Optional[str] = None,
    ) -> None:
        targets = self.registry.resolve(scope, slot)
        if not targets:
            _log.debug("no projections configured for scope=%s slot=%s", scope, slot)
            return
        for t in targets:
            if t.backend == "yjs":
                await self._apply_yjs(t, value, user_id=user_id, webspace_id=webspace_id)
            elif t.backend == "kv":
                self._apply_kv(scope, slot, value, user_id=user_id)
            else:
                # sql/other backends are reserved for future use
                _log.debug("backend %s is not implemented yet for scope=%s slot=%s", t.backend, scope, slot)

    async def _apply_yjs(
        self,
        target: ProjectionTarget,
        value: Any,
        *,
        user_id: Optional[str],
        webspace_id: Optional[str],
    ) -> None:
        # For projections we trust the calling context (events_ws, ctx.* helpers)
        # to pass the actual webspace id used by the Y websocket room. Fall back
        # to a literal "default" when nothing is provided so that the same id is
        # used consistently across YDoc, events and projections.
        token = (webspace_id or target.webspace_id or "default").strip()
        ws_id = token or "default"
        path = target.path or ""
        if not path:
            return

        # Allow simple {user_id} templating inside Yjs paths.
        if "{user_id}" in path:
            uid = user_id or UserProfileService(self.ctx).current_user_id()
            path = path.replace("{user_id}", uid)

        segments = [s for s in path.split("/") if s]
        if len(segments) < 2:
            return
        root_name = segments[0]

        def _mutator(doc, txn) -> None:
            root = doc.get_map(root_name)

            # For simple two-segment paths like ``data/weather`` keep the
            # legacy flat ``data["weather"]`` behaviour so existing widgets
            # continue to work. For longer paths such as ``data/infra/status``
            # merge into the existing top-level subtree so sibling branches
            # like other user ids are preserved.
            if len(segments) == 2:
                key = segments[1]
                current = root.get(key)
                if _json_like_equal(current, value):
                    return
                root.set(txn, key, _clone_json_like(value))
                return

            top_key = segments[1]
            current_top = root.get(top_key)
            changed, merged = _merge_nested_path(current_top, segments[2:], value)
            if not changed:
                return
            root.set(txn, top_key, merged)

        if not mutate_live_room(ws_id, _mutator):
            try:
                async with async_get_ydoc(ws_id) as ydoc:
                    with ydoc.begin_transaction() as txn:
                        _mutator(ydoc, txn)
            except Exception:
                _log.warning("failed to apply yjs projection webspace=%s path=%s", ws_id, path, exc_info=True)

    def _apply_kv(self, scope: str, slot: str, value: Any, *, user_id: Optional[str]) -> None:
        # For MVP treat (current_user, "profile.settings") specially and
        # route it through the UserProfileService, so profile can be
        # managed via ctx.current_user.set("profile.settings", ...).
        if scope == "current_user" and slot == "profile.settings":
            svc = UserProfileService(self.ctx)
            if isinstance(value, dict):
                svc.update_profile(value, user_id=user_id)
            else:
                _log.debug("profile.settings expects a mapping, got %r", type(value))
        else:
            _log.debug("kv projection ignored for scope=%s slot=%s (no handler)", scope, slot)


__all__ = ["ProjectionService"]
