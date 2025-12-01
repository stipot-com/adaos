# src\adaos\services\scenario\control.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from ..skill.state import _validate_key as _validate_binding_key  # reuse validation rules


@dataclass(slots=True)
class ScenarioControlService:
    """
    High-level control operations for scenarios, backed by the shared KV
    store. This service is intentionally light-weight and side-effect free
    beyond KV writes so that it can be used from CLI, skills and core
    runtimes.

    Keys layout (target state, aligned with docs/concepts/scenarios-target-state.md):

      - scenarios/<id>/enabled
      - scenarios/<id>/bindings/<key>
    """

    ctx: object

    def set_enabled(self, scenario_id: str, enabled: bool) -> None:
        """
        Explicitly enable or disable a scenario at the node/subnet level.

        Missing keys are treated as "enabled" by :meth:`is_enabled`, so this
        only needs to be called when toggling a scenario away from its
        default behaviour.
        """
        key = f"scenarios/{scenario_id}/enabled"
        self.ctx.kv.set(key, bool(enabled))

    def is_enabled(self, scenario_id: str) -> bool:
        """
        Check whether a scenario is enabled.

        If no explicit flag is stored, scenarios are considered enabled by
        default so that new scenarios become usable without extra wiring.
        """
        key = f"scenarios/{scenario_id}/enabled"
        value = self.ctx.kv.get(key, None)
        return bool(value) if value is not None else True

    def set_binding(self, scenario_id: str, key: str, value: str) -> bool:
        """
        Store a binding for a scenario, for example mapping a webspace,
        user profile or external channel to a specific scenario id.

        Returns ``False`` if the scenario does not exist in the configured
        scenarios repository (best-effort check), otherwise ``True``.
        """
        repo = getattr(self.ctx, "scenarios_repo", None)
        if repo is not None:
            try:
                repo.ensure()
            except Exception:
                pass
            if repo.get(scenario_id) is None:
                return False
        storage_key = f"scenarios/{scenario_id}/bindings/{_validate_binding_key(key)}"
        self.ctx.kv.set(storage_key, value)
        return True

    def get_binding(self, scenario_id: str, key: str, default: Optional[str] = None) -> Optional[str]:
        """
        Retrieve a single binding value for the given scenario id and key.
        """
        storage_key = f"scenarios/{scenario_id}/bindings/{_validate_binding_key(key)}"
        value = self.ctx.kv.get(storage_key, default)
        return value

    def list_bindings(self, scenario_id: str) -> Dict[str, str]:
        """
        List all bindings stored for a given scenario id.

        The returned mapping is ``binding_key -> value`` with the common
        ``scenarios/<id>/bindings/`` prefix stripped.
        """
        if not hasattr(self.ctx.kv, "list"):
            raise NotImplementedError("KV backend does not support list() operation for scenario bindings")
        prefix = f"scenarios/{scenario_id}/bindings/"
        keys = self.ctx.kv.list(prefix=prefix)  # type: ignore[arg-type]
        result: Dict[str, str] = {}
        for full_key in keys:
            if not isinstance(full_key, str):
                continue
            if not full_key.startswith(prefix):
                continue
            binding_key = full_key[len(prefix) :]
            value = self.ctx.kv.get(full_key, None)
            if isinstance(value, str):
                result[binding_key] = value
        return result
