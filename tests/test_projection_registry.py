from __future__ import annotations

from adaos.services.scenario.projection_registry import ProjectionRegistry


def test_projection_registry_active_scenario_overrides_skill_defaults(monkeypatch) -> None:
    registry = ProjectionRegistry()
    registry.load_entries(
        [
            {
                "scope": "subnet",
                "slot": "weather.snapshot",
                "targets": [{"backend": "yjs", "path": "data/weather/default"}],
            }
        ]
    )

    monkeypatch.setattr(
        "adaos.services.scenario.projection_registry.read_manifest",
        lambda scenario_id, *, space="workspace": {
            "data_projections": [
                {
                    "scope": "subnet",
                    "slot": "weather.snapshot",
                    "targets": [{"backend": "yjs", "path": f"data/weather/{scenario_id}"}],
                }
            ]
        },
    )

    loaded = registry.load_from_scenario("storm_lab", space="dev")

    resolved = registry.resolve("subnet", "weather.snapshot")
    assert loaded == 1
    assert registry.active_scenario_id() == "storm_lab"
    assert registry.active_space() == "dev"
    assert len(resolved) == 1
    assert resolved[0].path == "data/weather/storm_lab"


def test_projection_registry_clears_stale_scenario_overrides(monkeypatch) -> None:
    registry = ProjectionRegistry()
    registry.load_entries(
        [
            {
                "scope": "subnet",
                "slot": "infrastate.snapshot",
                "targets": [{"backend": "yjs", "path": "data/infrastate/base"}],
            }
        ]
    )

    def _read_manifest(scenario_id: str, *, space: str = "workspace") -> dict[str, object]:
        if scenario_id == "with_override":
            return {
                "data_projections": [
                    {
                        "scope": "subnet",
                        "slot": "infrastate.snapshot",
                        "targets": [{"backend": "yjs", "path": "data/infrastate/override"}],
                    }
                ]
            }
        return {"data_projections": []}

    monkeypatch.setattr("adaos.services.scenario.projection_registry.read_manifest", _read_manifest)

    registry.load_from_scenario("with_override", space="workspace")
    overridden = registry.resolve("subnet", "infrastate.snapshot")
    registry.load_from_scenario("without_override", space="dev")
    restored = registry.resolve("subnet", "infrastate.snapshot")

    assert overridden[0].path == "data/infrastate/override"
    assert registry.active_scenario_id() == "without_override"
    assert registry.active_space() == "dev"
    assert restored[0].path == "data/infrastate/base"
