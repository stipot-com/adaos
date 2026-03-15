# tests/test_nlu_teacher_target_resolution.py
import json
from pathlib import Path

import pytest
import yaml


@pytest.mark.anyio
async def test_regex_rule_candidate_defaults_to_skill_target_when_intent_calls_skill():
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.candidates_runtime import _on_candidate_apply
    from adaos.services.nlu.regex_rules_runtime import _on_regex_rule_apply
    from adaos.services.yjs.doc import async_get_ydoc

    ctx = get_ctx()
    webspace_id = "ws-test-target"

    # Scenario owns intent, but delegates to a skill event.
    scenario_id = "web_desktop"
    scenario_root = Path(ctx.paths.scenarios_dir()) / scenario_id
    scenario_root.mkdir(parents=True, exist_ok=True)
    scenario_json = scenario_root / "scenario.json"
    scenario_json.write_text(
        json.dumps(
            {
                "id": scenario_id,
                "version": "0.0.1",
                "nlu": {
                    "intents": {
                        "desktop.open_weather": {
                            "actions": [{"type": "callSkill", "target": "nlp.intent.weather.get", "params": {"city": "$slot.city"}}]
                        }
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # Skill subscribes to that event.
    skill_name = "weather_skill"
    skill_root = Path(ctx.paths.skills_dir()) / skill_name
    skill_root.mkdir(parents=True, exist_ok=True)
    skill_yaml = skill_root / "skill.yaml"
    skill_yaml.write_text(
        yaml.safe_dump(
            {
                "name": skill_name,
                "version": "0.1.0",
                "events": {"subscribe": ["nlp.intent.weather.get"]},
                "nlu": {},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    # Wire apply handler to the local bus.
    ctx.bus.subscribe("nlp.teacher.regex_rule.apply", _on_regex_rule_apply)

    candidate_id = "cand.test"
    pattern = r"\bтемператур\w*\b\s+(?:в|во)\s+(?P<city>[^?.!,;:]+)"
    async with async_get_ydoc(webspace_id) as ydoc:
        ui_map = ydoc.get_map("ui")
        data_map = ydoc.get_map("data")
        with ydoc.begin_transaction() as txn:
            ui_map.set(txn, "current_scenario", scenario_id)
            data_map.set(
                txn,
                "nlu_teacher",
                {
                    "candidates": [
                        {
                            "id": candidate_id,
                            "kind": "regex_rule",
                            "text": "скажи температуру в Москве",
                            "request_id": "nlu.test",
                            "regex_rule": {"intent": "desktop.open_weather", "pattern": pattern},
                            "status": "pending",
                        }
                    ]
                },
            )

    await _on_candidate_apply({"webspace_id": webspace_id, "candidate_id": candidate_id})

    # Must be stored in the skill (preferred), not in the scenario.
    rr = []
    for _ in range(50):
        saved_skill = yaml.safe_load(skill_yaml.read_text(encoding="utf-8")) or {}
        rr = ((saved_skill.get("nlu") or {}).get("regex_rules")) or []
        if any(isinstance(r, dict) and r.get("intent") == "desktop.open_weather" for r in rr):
            break
        import asyncio

        await asyncio.sleep(0.01)
    assert any(isinstance(r, dict) and r.get("intent") == "desktop.open_weather" and r.get("pattern") == pattern for r in rr)

    saved_scenario = json.loads(scenario_json.read_text(encoding="utf-8"))
    rr_sc = ((saved_scenario.get("nlu") or {}).get("regex_rules")) or []
    assert not any(isinstance(r, dict) and r.get("pattern") == pattern for r in rr_sc)
