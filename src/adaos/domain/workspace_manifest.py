from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ActivationMode = Literal["eager", "lazy", "on_demand"]
ActivationWebspaceScope = Literal["active", "any", "listed"]


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    return text or None


def _clean_name_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    merged: list[str] = []
    for item in value:
        token = _clean_text(item)
        if token and token not in merged:
            merged.append(token)
    return tuple(merged)


@dataclass(frozen=True, slots=True)
class SkillActivationWhen:
    scenarios_active: tuple[str, ...] = ()
    webspaces: tuple[str, ...] = ()
    client_presence: bool | None = None
    webspace_scope: ActivationWebspaceScope | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.scenarios_active:
            payload["scenarios_active"] = list(self.scenarios_active)
        if self.webspaces:
            payload["webspaces"] = list(self.webspaces)
        if self.client_presence is not None:
            payload["client_presence"] = bool(self.client_presence)
        if self.webspace_scope:
            payload["webspace_scope"] = self.webspace_scope
        return payload


@dataclass(frozen=True, slots=True)
class SkillActivationPolicy:
    mode: ActivationMode = "eager"
    when: SkillActivationWhen = field(default_factory=SkillActivationWhen)
    startup_allowed: bool | None = None
    background_refresh: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"mode": self.mode}
        when_payload = self.when.to_dict()
        if when_payload:
            payload["when"] = when_payload
        if self.startup_allowed is not None:
            payload["startup_allowed"] = bool(self.startup_allowed)
        if self.background_refresh is not None:
            payload["background_refresh"] = bool(self.background_refresh)
        return payload

    @classmethod
    def from_mapping(cls, value: Any) -> "SkillActivationPolicy | None":
        if not isinstance(value, dict):
            return None

        raw_mode = _clean_text(value.get("mode")) or "eager"
        mode: ActivationMode = "eager"
        if raw_mode in {"eager", "lazy", "on_demand"}:
            mode = raw_mode

        when_raw = value.get("when")
        when_dict = when_raw if isinstance(when_raw, dict) else {}

        webspace_scope_raw = _clean_text(when_dict.get("webspace_scope"))
        webspace_scope: ActivationWebspaceScope | None = None
        if webspace_scope_raw in {"active", "any", "listed"}:
            webspace_scope = webspace_scope_raw

        startup_allowed = value.get("startup_allowed")
        if not isinstance(startup_allowed, bool):
            startup_allowed = None
        background_refresh = value.get("background_refresh")
        if not isinstance(background_refresh, bool):
            background_refresh = None

        return cls(
            mode=mode,
            when=SkillActivationWhen(
                scenarios_active=_clean_name_list(when_dict.get("scenarios_active")),
                webspaces=_clean_name_list(when_dict.get("webspaces")),
                client_presence=when_dict.get("client_presence")
                if isinstance(when_dict.get("client_presence"), bool)
                else None,
                webspace_scope=webspace_scope,
            ),
            startup_allowed=startup_allowed,
            background_refresh=background_refresh,
        )


@dataclass(frozen=True, slots=True)
class ScenarioSkillBindings:
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.required:
            payload["required"] = list(self.required)
        if self.optional:
            payload["optional"] = list(self.optional)
        return payload


def parse_skill_activation_policy(manifest: dict[str, Any]) -> SkillActivationPolicy | None:
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict):
        return None
    return SkillActivationPolicy.from_mapping(runtime.get("activation"))


def parse_scenario_skill_bindings(manifest: dict[str, Any]) -> ScenarioSkillBindings:
    required = list(_clean_name_list(manifest.get("depends")))
    optional: list[str] = []

    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        skills = runtime.get("skills")
        if isinstance(skills, dict):
            for item in _clean_name_list(skills.get("required")):
                if item not in required:
                    required.append(item)
            for item in _clean_name_list(skills.get("optional")):
                if item not in optional and item not in required:
                    optional.append(item)

    return ScenarioSkillBindings(required=tuple(required), optional=tuple(optional))


__all__ = [
    "ActivationMode",
    "ActivationWebspaceScope",
    "ScenarioSkillBindings",
    "SkillActivationPolicy",
    "SkillActivationWhen",
    "parse_scenario_skill_bindings",
    "parse_skill_activation_policy",
]
