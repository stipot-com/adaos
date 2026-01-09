from __future__ import annotations

import logging
from pathlib import Path
import os
from typing import Any, Dict, List

import yaml

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.interpreter.workspace import InterpreterWorkspace, IntentMapping
from adaos.services.interpreter.trainer import RasaTrainer
from adaos.services.scenarios import loader as scenarios_loader
from adaos.sdk.core.decorators import subscribe

_log = logging.getLogger("adaos.interpreter.registry")


def _sync_skill_nlu_metadata(ctx: AgentContext) -> int:
    """
    Project skill-level ``skill.yaml["nlu"]`` sections into interpreter
    metadata files (``<skill>/interpreter/intents.yml``) so that
    InterpreterWorkspace.collect_skill_intents() can see them.
    """
    skills_dir = Path(ctx.paths.skills_dir())
    count = 0
    try:
        skills = ctx.skills_repo.list()
    except Exception:  # pragma: no cover - defensive logging
        _log.warning("failed to list skills for interpreter registry", exc_info=True)
        return 0

    for meta in skills:
        skill_id = getattr(meta, "id", None)
        skill_name = getattr(skill_id, "value", None) if skill_id is not None else None
        if not skill_name:
            skill_name = getattr(meta, "name", None)
        if not skill_name:
            continue
        root = skills_dir / str(skill_name)
        skill_yaml = root / "skill.yaml"
        if not skill_yaml.exists():
            continue
        try:
            payload = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
        except Exception:
            _log.debug("failed to read skill.yaml for %s", skill_name, exc_info=True)
            continue
        nlu_section = payload.get("nlu") or {}
        intents_raw = nlu_section.get("intents") or []
        if not isinstance(intents_raw, list) or not intents_raw:
            continue

        intents_doc: List[Dict[str, Any]] = []
        for entry in intents_raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or entry.get("intent")
            if not isinstance(name, str) or not name.strip():
                continue
            utterances = entry.get("utterances") or entry.get("examples") or []
            if not isinstance(utterances, list) or not utterances:
                continue
            intents_doc.append(
                {
                    "intent": str(name).strip(),
                    "description": entry.get("description"),
                    "skill": str(skill_name),
                    "examples": [str(u) for u in utterances if isinstance(u, str) and u.strip()],
                }
            )

        if not intents_doc:
            continue

        meta_dir = root / InterpreterWorkspace.SKILL_METADATA_DIR
        meta_dir.mkdir(parents=True, exist_ok=True)
        target = meta_dir / InterpreterWorkspace.SKILL_METADATA_FILE
        try:
            target.write_text(
                yaml.safe_dump({"intents": intents_doc}, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            count += len(intents_doc)
        except Exception:
            _log.warning("failed to write interpreter metadata for skill=%s", skill_name, exc_info=True)
    return count


def _collect_scenario_intents(ctx: AgentContext) -> List[IntentMapping]:
    """
    Read scenario-level NLU configuration (scenario.json["nlu"].intents)
    and convert it to InterpreterWorkspace IntentMapping entries.
    """
    mappings: List[IntentMapping] = []
    scenarios_root = Path(ctx.paths.scenarios_dir())
    try:
        children = [p for p in scenarios_root.iterdir() if p.is_dir()]
    except Exception:  # pragma: no cover - defensive logging
        _log.warning("failed to list scenarios_dir=%s", scenarios_root, exc_info=True)
        return mappings

    for child in children:
        scenario_id = child.name
        try:
            content = scenarios_loader.read_content(scenario_id)
        except FileNotFoundError:
            continue
        except Exception:
            _log.warning("failed to read scenario.json for %s", scenario_id, exc_info=True)
            continue
        if not isinstance(content, dict):
            continue
        nlu_section = content.get("nlu") or {}
        intents = nlu_section.get("intents") or {}
        if not isinstance(intents, dict):
            continue
        for intent_id, spec in intents.items():
            if not isinstance(intent_id, str) or not isinstance(spec, dict):
                continue
            examples = spec.get("examples") or []
            if not isinstance(examples, list):
                examples = []
            # We allow intents без примеров, но они не попадут в тренировочный набор.
            mappings.append(
                IntentMapping(
                    intent=intent_id,
                    description=spec.get("description"),
                    skill=None,
                    tool=None,
                    scenario=scenario_id,
                    examples=[str(e) for e in examples if isinstance(e, str) and e.strip()],
                )
            )
    return mappings


def sync_from_scenarios_and_skills(ctx: AgentContext) -> Dict[str, Any]:
    """
    High-level entry point used by CLI and tooling:

      1) проектирует skill.yaml[nlu] в interpreter/intents.yml по каждому навыку;
      2) считывает scenario.json["nlu"] и добавляет/обновляет intents в
         InterpreterWorkspace.config.yaml.
    """
    ws = InterpreterWorkspace(ctx)
    skill_count = _sync_skill_nlu_metadata(ctx)
    scenario_mappings = _collect_scenario_intents(ctx)

    for mapping in scenario_mappings:
        # overwrite existing config intents with the same name
        ws.upsert_intent(mapping)

    _log.info(
        "interpreter registry sync: skills_intents=%d scenario_intents=%d",
        skill_count,
        len(scenario_mappings),
    )
    return {
        "skills_intents": skill_count,
        "scenario_intents": len(scenario_mappings),
    }


def _maybe_autotrain(ctx: AgentContext) -> None:
    """
    Optionally trigger Rasa training after NLU data changes.

    Controlled by the ADAOS_INTERPRETER_AUTOTRAIN=1 env flag so that
    heavy training is opt-in and can be enabled in dev/prod as needed.
    """
    if os.getenv("ADAOS_INTERPRETER_AUTOTRAIN") != "1":
        return
    try:
        ws = InterpreterWorkspace(ctx)
        trainer = RasaTrainer(ws)
        meta = trainer.train(note="auto-train")
        _log.info("interpreter auto-train completed at %s", meta.get("trained_at"))
    except Exception:
        _log.warning("interpreter auto-train failed", exc_info=True)


def _handle_nlu_refresh(reason: str, webspace_id: str | None = None) -> None:
    """
    Shared helper for event-driven NLU refresh.
    """
    ctx = get_ctx()
    summary = sync_from_scenarios_and_skills(ctx)
    _log.info(
        "nlu.refresh reason=%s webspace=%s skills_intents=%d scenario_intents=%d",
        reason,
        webspace_id or "",
        summary.get("skills_intents", 0),
        summary.get("scenario_intents", 0),
    )
    _maybe_autotrain(ctx)


@subscribe("scenarios.synced")
async def _on_scenarios_synced(evt: Dict[str, Any]) -> None:
    """
    When a scenario payload is projected into YDoc, refresh interpreter NLU
    registry so that Rasa sees updated intents/examples.
    """
    webspace_id = str(evt.get("webspace_id") or "")
    _handle_nlu_refresh("scenarios.synced", webspace_id=webspace_id)


@subscribe("scenario.installed")
async def _on_scenario_installed(evt: Dict[str, Any]) -> None:
    """
    After a scenario is installed into the workspace repo, refresh NLU
    registry so that new scenario-level intents become visible.
    """
    scenario_id = str(evt.get("id") or evt.get("scenario_id") or "")
    _handle_nlu_refresh(f"scenario.installed:{scenario_id}")


@subscribe("skills.activated")
async def _on_skills_activated(evt: Dict[str, Any]) -> None:
    """
    When skills are activated for a webspace/subnet, refresh interpreter
    NLU registry so that new skill-level intents become part of the bundle.
    """
    webspace_id = str(evt.get("webspace_id") or "")
    _handle_nlu_refresh("skills.activated", webspace_id=webspace_id)


@subscribe("skills.rolledback")
async def _on_skills_rolledback(evt: Dict[str, Any]) -> None:
    """
    When skills are rolled back, refresh interpreter NLU registry to drop
    obsolete intents/examples from the bundle.
    """
    webspace_id = str(evt.get("webspace_id") or "")
    _handle_nlu_refresh("skills.rolledback", webspace_id=webspace_id)


@subscribe("desktop.webspace.reload")
async def _on_webspace_reload(evt: Dict[str, Any]) -> None:
    """
    A YJS webspace reload often follows scenario/skill changes in dev.
    Use it as an additional safety net to refresh NLU registry.
    """
    webspace_id = str(evt.get("webspace_id") or "")
    _handle_nlu_refresh("desktop.webspace.reload", webspace_id=webspace_id)

