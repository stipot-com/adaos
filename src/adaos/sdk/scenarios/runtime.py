# src\adaos\sdk\scenarios\runtime.py
"""Scenario runtime built on top of the SDK primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional
import contextlib
import io
import logging
import os
import re
import json

import yaml

from adaos.sdk.core.logging import setup_scenario_logger
from adaos.sdk.data import memory


ActionHandler = Callable[[Mapping[str, Any]], Any]


class ActionRegistry:
    def __init__(self) -> None:
        self._actions: Dict[str, ActionHandler] = {}

    def register(self, route: str, handler: ActionHandler) -> None:
        self._actions[route] = handler

    def call(self, route: str, args: Mapping[str, Any]) -> Any:
        if route not in self._actions:
            raise RuntimeError(f"unknown route: {route}")
        return self._actions[route](args)

    def has(self, route: str) -> bool:
        return route in self._actions

    def routes(self) -> Iterable[str]:
        return tuple(self._actions)


_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]+)\}")


@dataclass(slots=True)
class ScenarioStep:
    name: str
    when: Any | None = None
    call: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    save_as: Optional[str] = None
    set_values: Dict[str, Any] = field(default_factory=dict)
    children: List["ScenarioStep"] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ScenarioStep":
        name = str(data.get("name") or "step")
        args = data.get("args") or {}
        set_values = data.get("set") or {}
        children_raw = data.get("do") or []
        children = [cls.from_mapping(item) for item in children_raw if isinstance(item, Mapping)]
        return cls(
            name=name,
            when=data.get("when"),
            call=data.get("call"),
            args=dict(args) if isinstance(args, Mapping) else {},
            save_as=data.get("save_as"),
            set_values=dict(set_values) if isinstance(set_values, Mapping) else {},
            children=children,
        )


@dataclass(slots=True)
class ScenarioModel:
    id: str
    version: Optional[str]
    trigger: Optional[str]
    vars: Dict[str, Any]
    steps: List[ScenarioStep]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, fallback_id: str) -> "ScenarioModel":
        vars_section = payload.get("vars") or {}
        steps_section = payload.get("steps") or []
        steps = [ScenarioStep.from_mapping(step) for step in steps_section if isinstance(step, Mapping)]
        return cls(
            id=str(payload.get("id") or fallback_id),
            version=payload.get("version"),
            trigger=payload.get("trigger"),
            vars=dict(vars_section) if isinstance(vars_section, Mapping) else {},
            steps=steps,
        )


from adaos.services.agent_context import get_ctx, AgentContext


class ScenarioRuntime:
    def __init__(self, registry: Optional[ActionRegistry] = None) -> None:
        self._registry = registry or default_registry()
        self._logger: Optional[logging.Logger] = None
        self._log_path: Optional[Path] = None
        self.ctx: AgentContext = get_ctx()

    def _log(self, level: int, message: str, *, extra: Optional[Mapping[str, Any]] = None) -> None:
        if not self._logger:
            return
        payload = {"extra": dict(extra) if extra else {}}
        self._logger.log(level, message, extra=payload)

    def run(self, scenario: ScenarioModel, *, bag: Optional[MutableMapping[str, Any]] = None) -> Dict[str, Any]:
        state: Dict[str, Any] = bag if bag is not None else {}
        state.setdefault("vars", dict(scenario.vars))
        state.setdefault("steps", {})
        if self._log_path is not None:
            state.setdefault("meta", {})["log_file"] = str(self._log_path)
        self._log(logging.INFO, "scenario.start", extra={"scenario_id": scenario.id})
        for step in scenario.steps:
            self._execute_step(step, state)
        self._log(logging.INFO, "scenario.finish", extra={"scenario_id": scenario.id})
        return state

    def validate(self, scenario: ScenarioModel) -> List[str]:
        errors: List[str] = []
        seen: set[str] = set()

        def _check(step: ScenarioStep) -> None:
            if step.name in seen:
                errors.append(f"duplicate step name '{step.name}'")
            else:
                seen.add(step.name)
            if step.call and not self._registry.has(step.call):
                errors.append(f"unknown route '{step.call}' in step '{step.name}'")
            for child in step.children:
                _check(child)

        for step in scenario.steps:
            _check(step)
        return errors

    def _execute_step(self, step: ScenarioStep, bag: MutableMapping[str, Any]) -> None:
        if not _evaluate_condition(step.when, bag):
            return
        self._log(logging.INFO, "step.start", extra={"step": step.name})
        if step.set_values:
            for key, value in step.set_values.items():
                bag[key] = _resolve_value(value, bag)
            self._log(logging.INFO, "step.set", extra={"step": step.name, "keys": list(step.set_values.keys())})
        if step.call:
            args = _resolve_value(step.args, bag)
            if not isinstance(args, Mapping):
                args = {}
            try:
                result = self._registry.call(step.call, args)
                if step.save_as:
                    bag[step.save_as] = result
                bag.setdefault("steps", {})[step.name] = {
                    "route": step.call,
                    "args": args,
                    "result": result,
                }
                self._log(
                    logging.INFO,
                    "step.call.success",
                    extra={"step": step.name, "route": step.call},
                )
            except Exception as exc:
                bag.setdefault("steps", {})[step.name] = {
                    "route": step.call,
                    "args": args,
                    "error": str(exc),
                }
                self._log(
                    logging.ERROR,
                    "step.call.error",
                    extra={"step": step.name, "route": step.call, "error": str(exc)},
                )
                raise
        for child in step.children:
            self._execute_step(child, bag)
        self._log(logging.INFO, "step.finish", extra={"step": step.name})

    def run_from_file(self, path: str | Path) -> Dict[str, Any]:
        model = load_scenario(path)
        base_dir = self.ctx.paths.base_dir()
        ctx = ensure_runtime_context(base_dir)
        self._logger, self._log_path = setup_scenario_logger(model.id)
        self._log(
            logging.INFO,
            "scenario.loaded",
            extra={"scenario_id": model.id, "path": str(path)},
        )
        state: Dict[str, Any] = {"vars": dict(model.vars)}
        state.setdefault("meta", {})["log_file"] = str(self._log_path)
        result = self.run(model, bag=state)
        return result


def load_scenario(path: Path | str) -> ScenarioModel:
    """load scenario.yaml"""
    scenario_path = Path(path).expanduser().resolve() / "scenario.yaml"
    payload = yaml.safe_load(scenario_path.read_text(encoding="utf-8")) or {}
    return ScenarioModel.from_payload(payload, fallback_id=scenario_path.stem)


def _is_placeholder(value: str) -> bool:
    return value.startswith("${") and value.endswith("}")


def _resolve_reference(expr: str, bag: Mapping[str, Any]) -> Any:
    current: Any = bag
    for part in expr.split("."):
        key = part.strip()
        if not key:
            return None
        if isinstance(current, Mapping):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
        if current is None:
            return None
    return current


def _substitute_string(template: str, bag: Mapping[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        resolved = _resolve_reference(inner, bag)
        return "" if resolved is None else str(resolved)

    return _PLACEHOLDER_RE.sub(repl, template)


def _resolve_value(value: Any, bag: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        return {k: _resolve_value(v, bag) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_value(v, bag) for v in value]
    if isinstance(value, str):
        if _is_placeholder(value):
            inner = value[2:-1].strip()
            if inner.startswith("not "):
                return not bool(_resolve_reference(inner[4:], bag))
            return _resolve_reference(inner, bag)
        return _substitute_string(value, bag)
    return value


def _evaluate_condition(value: Any, bag: Mapping[str, Any]) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and _is_placeholder(value):
        inner = value[2:-1].strip()
        if inner.startswith("not "):
            return not bool(_resolve_reference(inner[4:], bag))
        return bool(_resolve_reference(inner, bag))
    if isinstance(value, str):
        return bool(value)
    return bool(value)


class _InMemoryKV:
    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}

    def get(self, key: str, default: Any | None = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any, ttl: Any | None = None) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def list(self, prefix: str = "") -> List[str]:
        return [k for k in self._store if k.startswith(prefix)]


class _InMemorySecrets:
    def __init__(self) -> None:
        self._store: Dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._store.get(name)

    def put(self, name: str, value: str) -> None:
        self._store[name] = value


def ensure_runtime_context(base_dir: Path):
    from adaos.services.agent_context import AgentContext, clear_ctx, get_ctx, set_ctx
    from adaos.services.eventbus import LocalEventBus
    from adaos.services.settings import Settings
    from adaos.adapters.fs.path_provider import PathProvider
    from adaos.services.policy.capabilities import InMemoryCapabilities

    try:
        ctx = get_ctx()
    except RuntimeError:
        ctx = None
    else:
        current_base = None
        if hasattr(ctx.paths, "base_dir"):
            try:
                current_base = ctx.paths.base_dir()
            except Exception:
                current_base = None
        if current_base == base_dir.resolve():
            return ctx
        clear_ctx()

    settings = Settings(base_dir=base_dir.resolve(), profile="sandbox")
    paths = PathProvider(settings)
    paths.ensure_tree()

    ctx = AgentContext(
        settings=settings,
        paths=paths,
        bus=LocalEventBus(),
        proc=SimpleNamespace(),
        caps=InMemoryCapabilities(),
        devices=SimpleNamespace(),
        kv=_InMemoryKV(),
        sql=SimpleNamespace(),
        secrets=_InMemorySecrets(),
        net=SimpleNamespace(),
        updates=SimpleNamespace(),
        git=SimpleNamespace(),
        fs=SimpleNamespace(),
        sandbox=SimpleNamespace(),
    )
    set_ctx(ctx)
    return ctx


def default_registry() -> ActionRegistry:
    registry = ActionRegistry()

    from adaos.services.io_console import print as console_print
    from adaos.services.io_voice_mock import stt_listen, tts_speak
    from adaos.services.skill.runtime import run_skill_handler_sync

    def _console(args: Mapping[str, Any]) -> Mapping[str, bool]:
        text = args.get("text")
        return console_print(text if isinstance(text, str) else None)

    def _tts(args: Mapping[str, Any]) -> Mapping[str, bool]:
        text = args.get("text")
        return tts_speak(text if isinstance(text, str) else None)

    def _stt(args: Mapping[str, Any]) -> Mapping[str, Any]:
        timeout = args.get("timeout", "20s")
        return stt_listen(timeout if isinstance(timeout, str) else "20s")

    def _profile_get(args: Mapping[str, Any]) -> Any:
        key = str(args.get("key") or "user.name")
        return memory.get(key)

    def _profile_set(args: Mapping[str, Any]) -> Any:
        key = str(args.get("key") or "user.name")
        raw = args.get("name")
        value = str(raw).strip() if raw is not None else ""
        if not value:
            memory.delete(key)
            return None
        memory.put(key, value)
        return value

    def _skills_run(args: Mapping[str, Any]) -> Mapping[str, Any]:
        skill = args.get("skill") or args.get("name") or args.get("skill_id")
        if not isinstance(skill, str) or not skill:
            raise ValueError("skill identifier is required")
        topic = args.get("topic") or ""
        if not isinstance(topic, str):
            topic = str(topic)
        payload = args.get("payload") or {}
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = run_skill_handler_sync(skill, topic, dict(payload))
        stdout = buf.getvalue().strip()
        return {"result": result, "stdout": stdout}

    registry.register("io.console.print", _console)
    registry.register("io.voice.tts.speak", _tts)
    registry.register("io.voice.stt.listen", _stt)
    registry.register("profile.get_name", _profile_get)
    registry.register("profile.set_name", _profile_set)
    registry.register("skills.run", _skills_run)
    registry.register("skills.handle", _skills_run)

    return registry


__all__ = [
    "ActionRegistry",
    "ScenarioModel",
    "ScenarioRuntime",
    "default_registry",
    "ensure_runtime_context",
    "load_scenario",
]
