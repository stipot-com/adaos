"""Process-wide registry of skill-provided NLU specifications."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterable, List

import yaml

from adaos.services.agent_context import get_ctx
from adaos.services.skill.resolver import SkillPathResolver

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IntentDisambiguation:
    inherit: List[str] = field(default_factory=list)
    defaults: Dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class IntentSpec:
    name: str
    utterances: List[str] = field(default_factory=list)
    slots: Dict[str, Any] = field(default_factory=dict)
    disambiguation: IntentDisambiguation = field(default_factory=IntentDisambiguation)


@dataclass(slots=True)
class SkillNLU:
    skill: str
    intents: List[IntentSpec] = field(default_factory=list)
    resolvers: Dict[str, Callable[..., Any]] = field(default_factory=dict)
    resources: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class _SkipSkill(Exception):
    """Control-flow error used to skip skills from the registry."""


class NLURegistry:
    """In-memory registry populated from skill manifests."""

    def __init__(self) -> None:
        self._skills: Dict[str, SkillNLU] = {}
        self._lock = RLock()

    # public API -----------------------------------------------------
    def load_all_active(self, skills: Iterable[str]) -> Dict[str, SkillNLU]:
        loaded: Dict[str, SkillNLU] = {}
        skipped: Dict[str, str] = {}
        for skill in skills:
            try:
                spec = self._load_skill(skill)
            except _SkipSkill as exc:
                skipped[skill] = str(exc)
                logger.warning("NLU skipped skill=%s reason=%s", skill, exc)
                continue
            except Exception as exc:  # pragma: no cover - defensive logging
                skipped[skill] = f"error: {exc}"
                logger.exception("NLU failed to load skill=%s", skill)
                continue
            loaded[skill] = spec
            self._log_skill(spec)
        with self._lock:
            self._skills = loaded
        if loaded:
            logger.info("NLU loaded for skills: %s", sorted(loaded))
        if skipped:
            for skill, reason in skipped.items():
                logger.warning("NLU skipped skill=%s reason=%s", skill, reason)
        return loaded

    def register(self, skill: str) -> None:
        try:
            spec = self._load_skill(skill)
        except _SkipSkill as exc:
            logger.warning("NLU skipped skill=%s reason=%s", skill, exc)
            with self._lock:
                self._skills.pop(skill, None)
            return
        except Exception:  # pragma: no cover - defensive logging
            logger.exception("NLU failed to register skill=%s", skill)
            return
        with self._lock:
            self._skills[skill] = spec
        self._log_skill(spec)

    def unregister(self, skill: str) -> None:
        with self._lock:
            self._skills.pop(skill, None)
        logger.info("NLU unregistered skill=%s", skill)

    def get(self) -> Dict[str, SkillNLU]:
        with self._lock:
            return dict(self._skills)

    # helpers --------------------------------------------------------
    def _resolver(self) -> SkillPathResolver:
        ctx = get_ctx()
        dev_root = getattr(ctx.paths, "dev_skills_dir", None)
        if callable(dev_root):
            dev_root = dev_root()
        if dev_root is None:
            dev_root = ctx.paths.skills_workspace_dir()
        workspace_root = ctx.paths.skills_workspace_dir()
        return SkillPathResolver(
            dev_root=Path(dev_root),
            workspace_root=Path(workspace_root),
        )

    def _load_skill(self, skill: str) -> SkillNLU:
        resolver = self._resolver()
        skill_dir = resolver.resolve(skill, space="workspace")
        manifest_path = skill_dir / "skill.yaml"
        if not manifest_path.exists():
            raise _SkipSkill("skill.yaml not found")
        try:
            manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise _SkipSkill(f"invalid skill.yaml: {exc}") from exc
        nlu_cfg = manifest_data.get("nlu")
        if not nlu_cfg:
            raise _SkipSkill("missing nlu section")
        intents_cfg = nlu_cfg.get("intents") or []
        if not intents_cfg:
            raise _SkipSkill("missing nlu.intents")
        resolvers_cfg = nlu_cfg.get("resolvers") or {}
        location_path = resolvers_cfg.get("location")
        if not location_path:
            raise _SkipSkill("missing resolvers.location")
        resolvers = {"location": self._import_resolver(location_path)}
        intents = self._parse_intents(intents_cfg)
        if not intents:
            raise _SkipSkill("no valid intents")
        resources = self._load_resources(skill_dir, nlu_cfg.get("locales") or {})
        return SkillNLU(skill=skill, intents=intents, resolvers=resolvers, resources=resources)

    def _parse_intents(self, intents_cfg: Iterable[Dict[str, Any]]) -> List[IntentSpec]:
        intents: List[IntentSpec] = []
        for item in intents_cfg:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            utterances_raw = item.get("utterances") or []
            utterances = [u.strip() for u in utterances_raw if isinstance(u, str) and u.strip()]
            if not utterances:
                continue
            slots = item.get("slots") or {}
            disamb_cfg = item.get("disambiguation") or {}
            disambiguation = IntentDisambiguation(
                inherit=list(disamb_cfg.get("inherit") or []),
                defaults=dict(disamb_cfg.get("defaults") or {}),
            )
            intents.append(IntentSpec(name=name.strip(), utterances=utterances, slots=dict(slots), disambiguation=disambiguation))
        return intents

    def _load_resources(self, skill_dir: Path, locales_cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        resources: Dict[str, Dict[str, Any]] = {}
        for locale, locale_cfg in locales_cfg.items():
            if not isinstance(locale_cfg, dict):
                continue
            res_cfg = locale_cfg.get("resources") or {}
            locale_resources: Dict[str, Any] = {}
            for key, relative in res_cfg.items():
                if not isinstance(relative, str):
                    continue
                path = (skill_dir / relative).resolve()
                if not path.exists():
                    raise _SkipSkill(f"resource not found: {relative}")
                locale_resources[key] = self._read_resource(path)
            if locale_resources:
                resources[str(locale)] = locale_resources
        return resources

    def _read_resource(self, path: Path) -> Any:
        if path.suffix.lower() == ".tsv":
            mapping: Dict[str, str] = {}
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text or text.startswith("#"):
                        continue
                    parts = text.split("\t")
                    if len(parts) < 2:
                        continue
                    key = parts[0].strip().lower()
                    value = parts[1].strip()
                    if key:
                        mapping[key] = value
            return mapping
        return path.read_text(encoding="utf-8")

    def _import_resolver(self, dotted: str) -> Callable[..., Any]:
        if ":" not in dotted:
            raise _SkipSkill(f"invalid resolver path: {dotted}")
        module_name, attr = dotted.split(":", 1)
        try:
            module = import_module(module_name)
        except Exception as exc:
            raise _SkipSkill(f"cannot import module {module_name}: {exc}") from exc
        try:
            resolver = getattr(module, attr)
        except AttributeError as exc:
            raise _SkipSkill(f"resolver attribute missing: {attr}") from exc
        if not callable(resolver):
            raise _SkipSkill(f"resolver {attr} is not callable")
        return resolver

    def _log_skill(self, spec: SkillNLU) -> None:
        locales = sorted(spec.resources.keys()) or ["-"]
        logger.info(
            "NLU: loaded skill=%s intents=%d resolvers=%s resources=%s",
            spec.skill,
            len(spec.intents),
            sorted(spec.resolvers.keys()),
            locales,
        )


registry = NLURegistry()

__all__ = [
    "IntentDisambiguation",
    "IntentSpec",
    "SkillNLU",
    "NLURegistry",
    "registry",
]
