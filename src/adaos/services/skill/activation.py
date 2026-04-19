from __future__ import annotations

from pathlib import Path
from typing import Literal

from adaos.domain.workspace_manifest import SkillActivationPolicy
from adaos.services.workspace_registry import find_workspace_registry_entry


InactiveSubscriptionStrategy = Literal["always_registered", "early_cheap_handlers"]


def load_skill_activation_policy(
    workspace_root: Path,
    skill_name: str,
    *,
    fallback_to_scan: bool = True,
) -> SkillActivationPolicy | None:
    token = str(skill_name or "").strip()
    if not token:
        return None
    entry = find_workspace_registry_entry(
        workspace_root,
        kind="skills",
        name_or_id=token,
        fallback_to_scan=fallback_to_scan,
    )
    if not isinstance(entry, dict):
        return None
    return SkillActivationPolicy.from_mapping(entry.get("activation"))


def subscription_strategy_for_policy(policy: SkillActivationPolicy | None) -> InactiveSubscriptionStrategy:
    if policy is None:
        return "always_registered"
    if policy.mode in {"lazy", "on_demand"}:
        return "early_cheap_handlers"
    return "always_registered"


def allows_background_refresh(
    policy: SkillActivationPolicy | None,
    *,
    startup: bool = False,
    scenario_active: bool | None = None,
    client_present: bool | None = None,
    webspace_is_target: bool | None = None,
) -> bool:
    if policy is None:
        return True
    if startup and policy.startup_allowed is False:
        return False
    if policy.background_refresh is False:
        return False

    when = policy.when
    if when.scenarios_active and scenario_active is False:
        return False
    if when.client_presence is True and client_present is False:
        return False
    if when.webspace_scope in {"active", "listed"} and webspace_is_target is False:
        return False
    return True


__all__ = [
    "InactiveSubscriptionStrategy",
    "allows_background_refresh",
    "load_skill_activation_policy",
    "subscription_strategy_for_policy",
]
