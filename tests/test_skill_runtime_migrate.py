from __future__ import annotations


def test_migrate_installed_skills_prepares_and_activates(monkeypatch) -> None:
    import adaos.apps.skill_runtime_migrate as mod

    class _Row:
        def __init__(self, name: str, installed: bool = True) -> None:
            self.name = name
            self.installed = installed

    class _Registry:
        def __init__(self, _sql) -> None:
            pass

        def list(self):
            return [_Row("weather_skill"), _Row("service_skill"), _Row("draft_skill", installed=False)]

    class _Runtime:
        version = "1.2.3"
        slot = "B"

    class _Manager:
        def prepare_runtime(self, name: str, run_tests: bool = False):
            assert run_tests is False
            if name == "service_skill":
                raise RuntimeError("service env broken")
            return _Runtime()

        def activate_runtime(self, name: str, version=None, slot=None):
            assert version == "1.2.3"
            assert slot == "B"
            return "B"

    class _Ctx:
        sql = object()
        skills_repo = object()
        git = object()
        paths = object()
        bus = None
        caps = object()

    monkeypatch.setattr(mod, "init_ctx", lambda: None)
    monkeypatch.setattr(mod, "get_ctx", lambda: _Ctx())
    monkeypatch.setattr(mod, "SqliteSkillRegistry", _Registry)
    monkeypatch.setattr(mod, "_manager", lambda: _Manager())

    payload = mod.migrate_installed_skills()

    assert payload["ok"] is False
    assert payload["skills"][0]["skill"] == "weather_skill"
    assert payload["skills"][0]["ok"] is True
    assert payload["skills"][0]["prepared_version"] == "1.2.3"
    assert payload["skills"][1]["skill"] == "service_skill"
    assert payload["skills"][1]["ok"] is False
    assert "service env broken" in payload["skills"][1]["error"]
