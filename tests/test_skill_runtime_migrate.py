from __future__ import annotations


def test_migrate_installed_skills_runs_tests_and_rolls_back_on_failure(monkeypatch) -> None:
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

    class _TestResult:
        def __init__(self, status: str) -> None:
            self.status = status

    class _Manager:
        def runtime_status(self, name: str):
            if name == "service_skill":
                return {"version": "1.0.0", "active_slot": "A", "lifecycle": {"rehydrate": {"ok": True}}}
            return {"lifecycle": {"rehydrate": {"ok": True}}}

        def prepare_runtime(self, name: str, run_tests: bool = False):
            assert run_tests is False
            return _Runtime()

        def activate_runtime(self, name: str, version=None, slot=None):
            assert version == "1.2.3"
            assert slot == "B"
            return "B"

        def run_skill_tests(self, name: str, source: str = "installed"):
            assert source == "installed"
            if name == "service_skill":
                return {"suite": _TestResult("failed")}
            return {"suite": _TestResult("passed")}

        def rollback_runtime(self, name: str):
            assert name == "service_skill"
            return "A"

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

    payload = mod.migrate_installed_skills(run_tests=True)

    assert payload["ok"] is False
    assert payload["failed_total"] == 1
    assert payload["rollback_total"] == 1
    assert payload["tests_failed_total"] == 1
    assert payload["lifecycle_failed_total"] == 0
    assert payload["run_tests"] is True
    assert payload["skills"][0]["skill"] == "weather_skill"
    assert payload["skills"][0]["ok"] is True
    assert payload["skills"][0]["tests"] == {"suite": "passed"}
    assert payload["skills"][0]["lifecycle"] == {"rehydrate": {"ok": True}}
    assert payload["skills"][1]["skill"] == "service_skill"
    assert payload["skills"][1]["ok"] is False
    assert payload["skills"][1]["failed_stage"] == "tests"
    assert payload["skills"][1]["rollback_performed"] is True
    assert payload["skills"][1]["rollback_slot"] == "A"
    assert payload["skills"][1]["tests"] == {"suite": "failed"}


def test_migrate_installed_skills_can_skip_tests(monkeypatch) -> None:
    import adaos.apps.skill_runtime_migrate as mod

    class _Row:
        name = "weather_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql) -> None:
            pass

        def list(self):
            return [_Row()]

    class _Runtime:
        version = "1.2.3"
        slot = "B"

    class _Manager:
        def runtime_status(self, name: str):
            return {"lifecycle": {}}

        def prepare_runtime(self, name: str, run_tests: bool = False):
            return _Runtime()

        def activate_runtime(self, name: str, version=None, slot=None):
            return "B"

        def run_skill_tests(self, name: str, source: str = "installed"):
            raise AssertionError("tests should be skipped")

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

    payload = mod.migrate_installed_skills(run_tests=False)

    assert payload["ok"] is True
    assert payload["failed_total"] == 0
    assert payload["tests_failed_total"] == 0
    assert payload["lifecycle_failed_total"] == 0
    assert payload["run_tests"] is False
    assert payload["safe_for_core_update"] is True
    assert payload["skills"][0]["tests"] == {}


def test_migrate_installed_skills_marks_prepare_failures_safe_for_core_update(monkeypatch) -> None:
    import adaos.apps.skill_runtime_migrate as mod

    class _Row:
        name = "broken_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql) -> None:
            pass

        def list(self):
            return [_Row()]

    class _Manager:
        def runtime_status(self, name: str):
            return {"version": "1.0.0", "active_slot": "A", "lifecycle": {}}

        def prepare_runtime(self, name: str, run_tests: bool = False):
            raise RuntimeError("import failed during prepare")

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

    payload = mod.migrate_installed_skills(run_tests=True)

    assert payload["ok"] is False
    assert payload["failed_total"] == 1
    assert payload["safe_for_core_update"] is True
    assert payload["skills"][0]["failure_kind"] == "prepare"
    assert payload["skills"][0]["failed_stage"] == "prepare"


def test_post_commit_checks_deactivate_failing_skills(monkeypatch) -> None:
    import adaos.apps.skill_runtime_migrate as mod

    class _Row:
        def __init__(self, name: str, installed: bool = True) -> None:
            self.name = name
            self.installed = installed

    class _Registry:
        def __init__(self, _sql) -> None:
            pass

        def list(self):
            return [_Row("weather_skill"), _Row("service_skill")]

    class _TestResult:
        def __init__(self, status: str) -> None:
            self.status = status

    class _Manager:
        def runtime_status(self, name: str):
            return {"version": "1.2.3", "active_slot": "B", "deactivated": False, "lifecycle": {"persist": {"ok": True}}}

        def run_skill_tests(self, name: str, source: str = "installed"):
            assert source == "installed"
            if name == "service_skill":
                return {"suite": _TestResult("failed")}
            return {"suite": _TestResult("passed")}

        def deactivate_runtime(self, name: str, reason: str = "", failure_kind: str = "", failed_stage: str = "", source: str = "", committed_core_switch=None):
            assert name == "service_skill"
            assert reason == "post_commit_checks_failed"
            assert failure_kind == "tests"
            assert failed_stage == "tests"
            assert source == "post_commit_check_installed_skills"
            assert committed_core_switch is True
            return {"name": name, "deactivated": True, "reason": reason, "failure_kind": failure_kind, "failed_stage": failed_stage}

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

    payload = mod.post_commit_check_installed_skills(deactivate_on_failure=True)

    assert payload["ok"] is False
    assert payload["failed_total"] == 1
    assert payload["deactivated_total"] == 1
    assert payload["tests_failed_total"] == 1
    assert payload["lifecycle_failed_total"] == 0
    assert payload["skills"][1]["skill"] == "service_skill"
    assert payload["skills"][1]["deactivated"] is True
    assert payload["skills"][1]["failure_kind"] == "tests"
    assert payload["skills"][1]["failed_stage"] == "tests"
    assert payload["skills"][0]["lifecycle"] == {"persist": {"ok": True}}


def test_post_commit_checks_fail_on_lifecycle_health_before_tests(monkeypatch) -> None:
    import adaos.apps.skill_runtime_migrate as mod

    class _Row:
        def __init__(self, name: str, installed: bool = True) -> None:
            self.name = name
            self.installed = installed

    class _Registry:
        def __init__(self, _sql) -> None:
            pass

        def list(self):
            return [_Row("broken_skill")]

    class _Manager:
        def runtime_status(self, name: str):
            return {
                "version": "1.2.3",
                "active_slot": "B",
                "deactivated": False,
                "lifecycle": {
                    "rehydrate": {"ok": False, "skipped": False, "error": "projection rebuild failed"},
                    "healthcheck": {"ok": False, "skipped": False, "error": "service unhealthy"},
                },
            }

        def run_skill_tests(self, name: str, source: str = "installed"):
            raise AssertionError("tests should not run when lifecycle already failed")

        def deactivate_runtime(self, name: str, reason: str = "", failure_kind: str = "", failed_stage: str = "", source: str = "", committed_core_switch=None):
            assert name == "broken_skill"
            assert reason == "post_commit_checks_failed"
            assert failure_kind == "lifecycle"
            assert failed_stage == "rehydrate"
            assert source == "post_commit_check_installed_skills"
            assert committed_core_switch is True
            return {"name": name, "deactivated": True, "reason": reason, "failure_kind": failure_kind, "failed_stage": failed_stage}

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

    payload = mod.post_commit_check_installed_skills(deactivate_on_failure=True)

    assert payload["ok"] is False
    assert payload["failed_total"] == 1
    assert payload["deactivated_total"] == 1
    assert payload["lifecycle_failed_total"] == 1
    assert payload["tests_failed_total"] == 0
    assert payload["skills"][0]["failed_stage"] == "rehydrate"
    assert payload["skills"][0]["failure_kind"] == "lifecycle"
    assert payload["skills"][0]["deactivated"] is True


def test_post_commit_checks_persist_deactivation_metadata(monkeypatch) -> None:
    import adaos.apps.skill_runtime_migrate as mod

    class _Row:
        name = "broken_skill"
        installed = True

    class _Registry:
        def __init__(self, _sql) -> None:
            pass

        def list(self):
            return [_Row()]

    class _Manager:
        def runtime_status(self, name: str):
            return {
                "version": "2.0.0",
                "active_slot": "B",
                "deactivated": False,
                "lifecycle": {
                    "rehydrate": {"ok": False, "skipped": False, "error": "projection rebuild failed"},
                },
            }

        def run_skill_tests(self, name: str, source: str = "installed"):
            raise AssertionError("tests should not run when lifecycle already failed")

        def deactivate_runtime(self, name: str, reason: str = "", failure_kind: str = "", failed_stage: str = "", source: str = "", committed_core_switch=None):
            return {
                "name": name,
                "deactivated": True,
                "reason": reason,
                "failure_kind": failure_kind,
                "failed_stage": failed_stage,
                "source": source,
                "committed_core_switch": committed_core_switch,
            }

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

    payload = mod.post_commit_check_installed_skills(deactivate_on_failure=True)

    deactivation = payload["skills"][0]["deactivation"]
    assert deactivation["failure_kind"] == "lifecycle"
    assert deactivation["failed_stage"] == "rehydrate"
    assert deactivation["source"] == "post_commit_check_installed_skills"
    assert deactivation["committed_core_switch"] is True
