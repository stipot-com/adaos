from __future__ import annotations
from typing import Callable, Dict, List, Tuple, Optional
import inspect
import logging
from pathlib import Path
from adaos.sdk.data.bus import on, emit
from adaos.sdk.data.context import set_current_skill, clear_current_skill
from adaos.sdk.core._ctx import require_ctx
from adaos.sdk.core.errors import SdkRuntimeNotInitialized

# публичные реестры (стабильные имена)
subscriptions: List[Tuple[str, Callable]] = []
tools_registry: Dict[str, Dict[str, Callable]] = {}
tools_meta: Dict[str, dict] = {}  # по qualname функции
event_payloads: Dict[str, dict] = {}  # topic -> schema
emits_map: Dict[str, set[str]] = {}  # qualname -> {topics}
_registered: bool = False  # внутренняя защита от двойной регистрации
_SUBSCRIPTIONS = subscriptions
_TOOLS = tools_registry
_LOG = logging.getLogger("adaos.sdk.subscriptions")


def subscribe(topic: str):
    """Регистрирует обработчик; фактическая подписка делает register_subscriptions()."""

    def deco(fn: Callable):
        subscriptions.append((topic, fn))
        return fn

    return deco


async def register_subscriptions():
    """Подписать все функции, помеченные @subscribe, на bus (однократно)."""
    global _registered
    if _registered:
        return
    skill_topic_handlers: Dict[str, Dict[str, str]] = {}
    skill_summaries: Dict[str, list[tuple[str, str]]] = {}

    for topic, fn in subscriptions:
        skill_name = _infer_skill_name(fn)

        if skill_name:
            handlers_for_skill = skill_topic_handlers.setdefault(skill_name, {})
            if topic in handlers_for_skill:
                _LOG.warning(
                    "duplicate subscription skipped skill=%s topic=%s handler=%s existing=%s",
                    skill_name,
                    topic,
                    f"{fn.__module__}.{fn.__name__}",
                    handlers_for_skill[topic],
                )
                continue
            handlers_for_skill[topic] = f"{fn.__module__}.{fn.__name__}"

        if inspect.iscoroutinefunction(fn):

            async def _wrap(evt, _fn=fn, _skill=skill_name):
                pushed = _maybe_push_skill(_fn, _skill)
                try:
                    return await _fn(evt)
                finally:
                    if pushed:
                        clear_current_skill()

        else:

            async def _wrap(evt, _fn=fn, _skill=skill_name):
                pushed = _maybe_push_skill(_fn, _skill)
                try:
                    _fn(evt)
                finally:
                    if pushed:
                        clear_current_skill()

        handler_name = f"{fn.__module__}.{fn.__name__}"
        skill_key = skill_name or "<unknown>"
        skill_summaries.setdefault(skill_key, []).append((topic, fn.__name__))
        await on(topic, _wrap)
        try:
            await emit(
                "skill.subscription.registered",
                {"topic": topic, "handler": handler_name},
                source="sdk.core.decorators",
            )
        except Exception:
            _LOG.warning(
                "failed to emit subscription event handler=%s topic=%s",
                handler_name,
                topic,
                exc_info=True,
            )
    for skill, entries in sorted(skill_summaries.items()):
        summary = ", ".join(f"{topic}: {handler}" for topic, handler in entries)
        _LOG.info("skill=%s subscriptions=[%s]", skill, summary)

    _registered = True


def tool(
    public_name: Optional[str] = None,
    *,
    summary: str = "",
    stability: str = "experimental",
    idempotent: Optional[bool] = None,
    side_effects: Optional[str] = None,
    examples: Optional[list[str]] = None,
    since: Optional[str] = None,
    version: Optional[str] = None,
    input_schema: Optional[dict] = None,
    output_schema: Optional[dict] = None,
):
    """Маркер инструмента с публичным именем и метаданными."""

    def deco(fn: Callable):
        name = public_name or fn.__name__
        mod = fn.__module__
        tools_registry.setdefault(mod, {})[name] = fn
        qn = f"{mod}.{fn.__name__}"
        tools_meta[qn] = {
            "public_name": name,
            "summary": summary,
            "stability": stability,
            "idempotent": idempotent,
            "side_effects": side_effects,
            "examples": (examples or []),
            "since": since,
            "version": version,
            "input_schema": input_schema,
            "output_schema": output_schema,
        }
        return fn

    return deco


def event_payload(topic: str, schema: dict):
    """Опишите форму payload для события (для экспорта)."""
    event_payloads[topic] = schema
    return lambda fn: fn


def emits(*topics: str):
    """Пометьте функцию как публикующую события (для карты событий)."""

    def _wrap(fn: Callable):
        qn = f"{fn.__module__}.{fn.__name__}"
        emits_map.setdefault(qn, set()).update(topics)
        return fn

    return _wrap


def resolve_tool(module_name: str, public_name: str) -> Callable | None:
    """Вернуть callable по публичному имени инструмента из модуля."""
    return (tools_registry.get(module_name) or {}).get(public_name)



def _infer_skill_name(fn: Callable) -> Optional[str]:
    """���஡����� ������ ��� ���몠 �� ��� � 䠩�� handlers/main.py."""
    try:
        path = Path(inspect.getfile(fn)).resolve()
    except Exception:
        return None
    parts = list(path.parts)
    for idx in range(len(parts) - 1, -1, -1):
        part = parts[idx]
        if part == "skills" and idx + 1 < len(parts):
            nxt = parts[idx + 1]
            if nxt == ".runtime" and idx + 2 < len(parts):
                return parts[idx + 2]
            return nxt
    return None


def _maybe_push_skill(fn: Callable, skill_name: Optional[str]) -> bool:
    """
    ��⠭����� CurrentSkill �� �६� �맮�� ��ࠡ��稪�.

    1) ��⠥��� �१ SkillContextService (set_current_skill).
    2) �᫨ ���뮪 �� ������ � registry, ����塞 ���� � skill root
       �� 䠩�� ������� � ��뢠�� ctx.skill_ctx.set(...) �������.
    """
    if not skill_name:
        return False

    try:
        if set_current_skill(skill_name):
            return True
    except SdkRuntimeNotInitialized:
        _LOG.debug("AgentContext not available when setting skill=%s", skill_name)
    except Exception:
        _LOG.warning("set_current_skill failed for %s", skill_name, exc_info=True)

    try:
        ctx = require_ctx("sdk.data.skill_memory")
        handler_path = Path(inspect.getfile(fn)).resolve()
        parts = list(handler_path.parts)
        skill_root: Path | None = None
        for idx in range(len(parts) - 1, -1, -1):
            part = parts[idx]
            if part == "skills" and idx + 1 < len(parts):
                skill_root = Path(*parts[: idx + 2])  # .../skills/<skill_name>
                break
        if skill_root is None:
            return False
        skill_ctx = getattr(ctx, "skill_ctx", None)
        if not skill_ctx:
            return False
        return bool(skill_ctx.set(skill_name, skill_root))
    except Exception:
        _LOG.debug("fallback skill_ctx.set failed for %s", skill_name, exc_info=True)
        return False
