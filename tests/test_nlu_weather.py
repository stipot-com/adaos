from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from adaos.services.agent_context import get_ctx
from adaos.services.nlu.context import DialogContext
from adaos.services.nlu.arbiter import arbitrate
from adaos.services.nlu.registry import registry as nlu_registry
from adaos.sdk.data import memory as sdk_memory
from adaos.integrations.ovos.padatious_adapter import parse_candidates


def _setup_weather_skill(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_workspace_dir())
    skill_dir = skills_root / "weather_skill"
    (skill_dir / "locales" / "ru").mkdir(parents=True, exist_ok=True)
    (skill_dir / "__init__.py").write_text("", encoding="utf-8")
    (skill_dir / "resolvers.py").write_text(
        textwrap.dedent(
            """
            import re
            from typing import Any, Dict


            _WORD_RE = re.compile(r"[\\wёЁ-]+", re.IGNORECASE)


            def resolve_location(*, text: str, lang: str, slots: Dict[str, Any], resources: Dict[str, Any]):
                mapping = resources.get("location_map", {})
                for token in _WORD_RE.findall(text.lower()):
                    if token in mapping:
                        return mapping[token], 0.9
                return None
            """
        ),
        encoding="utf-8",
    )
    (skill_dir / "locales" / "ru" / "location_map.tsv").write_text(
        "\n".join([
            "берлин\tBerlin, DE",
            "берлине\tBerlin, DE",
            "лондон\tLondon, GB",
            "лондоне\tLondon, GB",
        ]),
        encoding="utf-8",
    )
    (skill_dir / "skill.yaml").write_text(
        textwrap.dedent(
            """
            name: weather_skill
            version: 1.0.0
            nlu:
              intents:
                - name: weather.show
                  utterances:
                    - "какая погода {date?} {place?}"
                    - "погода {date?} {place?}"
                  slots:
                    date: { type: datetime, required: false }
                    place: { type: location, required: false }
                  disambiguation:
                    inherit: ["date"]
                    defaults:
                      place: "@home_city"
              locales:
                ru:
                  resources:
                    location_map: "locales/ru/location_map.tsv"
              resolvers:
                location: "weather_skill.resolvers:resolve_location"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(skills_root))


def test_registry_loads_skill(monkeypatch):
    nlu_registry.load_all_active([])
    _setup_weather_skill(monkeypatch)
    loaded = nlu_registry.load_all_active(["weather_skill"])
    assert "weather_skill" in loaded
    spec = loaded["weather_skill"]
    assert any(intent.name == "weather.show" for intent in spec.intents)
    assert "location" in spec.resolvers
    assert spec.resources["ru"]["location_map"]["лондон"] == "London, GB"


def test_weather_today_then_london(monkeypatch):
    nlu_registry.load_all_active([])
    _setup_weather_skill(monkeypatch)
    nlu_registry.load_all_active(["weather_skill"])

    def fake_today(text, today=None):
        return "2025-10-18" if "сегодня" in text else None

    monkeypatch.setattr("adaos.services.nlu.normalizer.normalize_date_ru", fake_today)
    sdk_memory.put("home_city", "Berlin, DE")

    ctx = DialogContext()

    r1 = arbitrate("какая погода на сегодня?", "ru", ctx)
    chosen1 = r1["payload"]["chosen"]
    assert chosen1["intent"] == "weather.show"
    assert chosen1["slots"]["date"] == "2025-10-18"
    assert chosen1["slots"]["place"] == "Berlin, DE"

    r2 = arbitrate("а в лондоне?", "ru", ctx)
    chosen2 = r2["payload"]["chosen"]
    assert chosen2["intent"] == "weather.show"
    assert chosen2["slots"]["date"] == "2025-10-18"
    assert chosen2["slots"]["place"] == "London, GB"
    sdk_memory.delete("home_city")


def test_skill_without_resolver_is_skipped(monkeypatch):
    nlu_registry.load_all_active([])
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_workspace_dir())
    skill_dir = skills_root / "smalltalk"
    (skill_dir / "locales").mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        textwrap.dedent(
            """
            name: smalltalk
            version: 0.1.0
            nlu:
              intents:
                - name: chat.welcome
                  utterances:
                    - "привет"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(skills_root))
    loaded = nlu_registry.load_all_active(["smalltalk"])
    assert "smalltalk" not in loaded
    ctx_dialog = DialogContext()
    event = arbitrate("привет", "ru", ctx_dialog)
    assert event["payload"]["candidates"] == []
    assert event["payload"]["chosen"] == {}


def test_parse_candidates_uses_registry(monkeypatch):
    nlu_registry.load_all_active([])
    _setup_weather_skill(monkeypatch)
    nlu_registry.load_all_active(["weather_skill"])
    candidates = parse_candidates("какая погода на сегодня?", "ru")
    assert candidates
    assert any(c.get("skill") == "weather_skill" for c in candidates)


def test_weather_without_registered_skill():
    nlu_registry.load_all_active([])
    ctx = DialogContext()
    event = arbitrate("какая погода?", "ru", ctx)
    assert event["payload"]["candidates"] == []
    assert event["payload"]["chosen"] == {}
