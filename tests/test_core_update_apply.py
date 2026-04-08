from __future__ import annotations

import json
import subprocess
from pathlib import Path


def test_checkout_target_version_ignores_non_sha(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(mod, "_run", lambda cmd, cwd=None: calls.append(list(cmd)))

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="2026.3.1")
    assert calls == []


def test_checkout_target_version_checks_out_sha(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")

    def _fake_run(cmd, *, cwd=None):
        calls.append(list(cmd))

    monkeypatch.setattr(mod, "_run", _fake_run)

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="a" * 12)
    assert calls == [["git", "checkout", "aaaaaaaaaaaa"]]


def test_checkout_target_version_fetches_then_retries(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    calls: list[list[str]] = []

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "git")

    state = {"attempt": 0}

    def _fake_run(cmd, *, cwd=None):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "checkout"] and state["attempt"] == 0:
            state["attempt"] += 1
            raise RuntimeError("missing commit in shallow clone")

    monkeypatch.setattr(mod, "_run", _fake_run)

    mod._checkout_target_version(tmp_path, target_rev="main", target_version="b" * 12)
    assert calls == [
        ["git", "checkout", "bbbbbbbbbbbb"],
        ["git", "fetch", "--depth", "50", "origin", "main"],
        ["git", "checkout", "bbbbbbbbbbbb"],
    ]


def test_repair_moved_venv_rewrites_script_paths(tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    original_venv = tmp_path / "tmp-build" / "B" / "venv"
    final_venv = tmp_path / "slots" / "B" / "venv"
    scripts_dir_name = "Scripts" if mod.os.name == "nt" else "bin"
    python_name = "python.exe" if mod.os.name == "nt" else "python"
    scripts = final_venv / scripts_dir_name
    scripts.mkdir(parents=True, exist_ok=True)
    old_python = original_venv / scripts_dir_name / python_name
    pip_script = scripts / "pip"
    activate_script = scripts / "activate"
    pip_script.write_text(f"#!{old_python}\nprint('ok')\n", encoding="utf-8")
    activate_script.write_text(f"VIRTUAL_ENV=\"{original_venv}\"\n", encoding="utf-8")

    result = mod._repair_moved_venv(final_venv, original_venv_dir=original_venv)

    assert result["ok"] is True
    assert str(pip_script) in result["repaired_files"]
    assert str(activate_script) in result["repaired_files"]
    assert pip_script.read_text(encoding="utf-8").startswith(f"#!{final_venv / scripts_dir_name / python_name}")
    assert str(final_venv) in activate_script.read_text(encoding="utf-8")


def test_migrate_installed_skill_runtimes_uses_target_python(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    captured: dict[str, object] = {}

    def _fake_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(env or {})
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "skills": []}), stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    payload = mod._migrate_installed_skill_runtimes(
        tmp_path / "venv" / "bin" / "python",
        base_dir="/tmp/adaos-base",
        shared_dotenv_path="/tmp/adaos.env",
        run_tests=True,
    )

    assert payload["ok"] is True
    assert captured["cmd"] == [
        str(tmp_path / "venv" / "bin" / "python"),
        "-m",
        "adaos.apps.skill_runtime_migrate",
        "--json",
    ]
    assert captured["env"]["ADAOS_BASE_DIR"] == "/tmp/adaos-base"
    assert captured["env"]["ADAOS_SHARED_DOTENV_PATH"] == "/tmp/adaos.env"


def test_migrate_installed_skill_runtimes_can_skip_tests(monkeypatch, tmp_path: Path) -> None:
    import adaos.apps.core_update_apply as mod

    captured: dict[str, object] = {}

    def _fake_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "skills": []}), stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    mod._migrate_installed_skill_runtimes(
        tmp_path / "venv" / "bin" / "python",
        run_tests=False,
    )

    assert captured["cmd"] == [
        str(tmp_path / "venv" / "bin" / "python"),
        "-m",
        "adaos.apps.skill_runtime_migrate",
        "--json",
        "--skip-tests",
    ]

