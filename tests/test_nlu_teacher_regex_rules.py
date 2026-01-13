# tests/test_nlu_teacher_regex_rules.py
import json
from pathlib import Path

import pytest


@pytest.mark.anyio
async def test_teacher_regex_rule_applies_to_scenario_and_pipeline_picks_it_up():
    from adaos.services.agent_context import get_ctx
    from adaos.services.nlu.pipeline import _try_regex_intent
    from adaos.services.nlu.regex_rules_runtime import _on_regex_rule_apply
    from adaos.services.yjs.doc import async_get_ydoc

    ctx = get_ctx()
    scenario_id = "web_desktop"
    webspace_id = "ws-test"

    # Minimal scenario that owns the intent mapping (scope=scenario).
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
                            "scope": "scenario",
                            "actions": [{"type": "callSkill", "target": "nlp.intent.weather.get", "params": {"city": "$slot.city"}}],
                            "examples": ["погода в Москве"],
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

    # Bind webspace -> scenario.
    async with async_get_ydoc(webspace_id) as ydoc:
        ui_map = ydoc.get_map("ui")
        with ydoc.begin_transaction() as txn:
            ui_map.set(txn, "current_scenario", scenario_id)

    # Baseline: built-in regex stage should not match "температура" (it only matches "погода/weather").
    intent, slots, via, _raw = await _try_regex_intent("Покажи температуру в Берлине", webspace_id=webspace_id)
    assert intent is None
    assert via == "regex"
    assert slots == {}

    # Apply a teacher regex-rule into scenario (NLU Teacher flow).
    pattern = r"\b(?:температур\w*|градус\w*)\b(?:\s+(?:в|во)\s+(?P<city>[^?.!,;:]+))?"
    await _on_regex_rule_apply(
        {
            "webspace_id": webspace_id,
            "intent": "desktop.open_weather",
            "pattern": pattern,
        }
    )

    # Now the same utterance must be recognized via dynamic regex.
    intent, slots, via, _raw = await _try_regex_intent("Покажи температуру в Берлине", webspace_id=webspace_id)
    assert intent == "desktop.open_weather"
    assert via == "regex.dynamic"
    assert slots.get("city") == "Берлине"

    # Verify it was persisted into scenario.json (workspace scope).
    saved = json.loads(scenario_json.read_text(encoding="utf-8"))
    rules = (saved.get("nlu") or {}).get("regex_rules") or []
    assert any(r.get("intent") == "desktop.open_weather" and r.get("pattern") == pattern for r in rules if isinstance(r, dict))
