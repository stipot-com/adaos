import os

from adaos.apps.cli import app as cli_app


def test_should_reexec_windows_wrapper_for_adaos_exe(monkeypatch):
    monkeypatch.setattr(cli_app.os, "name", "nt", raising=False)
    monkeypatch.delenv("ADAOS_CLI_REEXECED", raising=False)
    assert cli_app._should_reexec_windows_wrapper(r"D:\git\adaos\.venv\Scripts\adaos.exe")


def test_should_not_reexec_after_flag(monkeypatch):
    monkeypatch.setattr(cli_app.os, "name", "nt", raising=False)
    monkeypatch.setenv("ADAOS_CLI_REEXECED", "1")
    assert not cli_app._should_reexec_windows_wrapper(r"D:\git\adaos\.venv\Scripts\adaos.exe")


def test_preferred_cli_python_uses_sibling_python(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python_exe = scripts / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    src = tmp_path / "src" / "adaos" / "apps" / "cli"
    src.mkdir(parents=True)
    monkeypatch.setattr(cli_app, "__file__", os.fspath(src / "app.py"))
    monkeypatch.setattr(cli_app.sys, "argv", [str(scripts / "adaos.exe")])
    monkeypatch.setattr(cli_app.sys, "executable", os.fspath(tmp_path / "fallback.exe"))
    assert cli_app._preferred_cli_python() == os.fspath(python_exe)


def test_preferred_cli_python_resolves_via_path(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python_exe = scripts / "python.exe"
    python_exe.write_text("", encoding="utf-8")
    src = tmp_path / "src" / "adaos" / "apps" / "cli"
    src.mkdir(parents=True)
    monkeypatch.setattr(cli_app, "__file__", os.fspath(src / "app.py"))
    monkeypatch.setattr(cli_app.sys, "argv", ["adaos.exe"])
    monkeypatch.setattr(cli_app.shutil, "which", lambda _: os.fspath(scripts / "adaos.exe"))
    monkeypatch.setattr(cli_app.sys, "executable", os.fspath(tmp_path / "fallback.exe"))
    assert cli_app._preferred_cli_python() == os.fspath(python_exe)


def test_repo_venv_python_detected(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    src = tmp_path / "src" / "adaos" / "apps" / "cli"
    src.mkdir(parents=True)
    monkeypatch.setattr(cli_app, "__file__", os.fspath(src / "app.py"))
    assert cli_app._repo_venv_python() == os.fspath(venv_python)


def test_should_reexec_repo_venv_when_current_python_differs(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    src = tmp_path / "src" / "adaos" / "apps" / "cli"
    src.mkdir(parents=True)
    monkeypatch.setattr(cli_app, "__file__", os.fspath(src / "app.py"))
    monkeypatch.setattr(cli_app.sys, "executable", os.fspath(tmp_path / "Python311" / "python.exe"))
    monkeypatch.delenv("ADAOS_CLI_REEXECED", raising=False)
    monkeypatch.delenv("ADAOS_DISABLE_PREFERRED_PYTHON_REEXEC", raising=False)
    assert cli_app._should_reexec_repo_venv()


def test_should_not_reexec_repo_venv_when_already_using_it(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    src = tmp_path / "src" / "adaos" / "apps" / "cli"
    src.mkdir(parents=True)
    monkeypatch.setattr(cli_app, "__file__", os.fspath(src / "app.py"))
    monkeypatch.setattr(cli_app.sys, "executable", os.fspath(venv_python))
    monkeypatch.delenv("ADAOS_CLI_REEXECED", raising=False)
    monkeypatch.delenv("ADAOS_DISABLE_PREFERRED_PYTHON_REEXEC", raising=False)
    assert not cli_app._should_reexec_repo_venv()


def test_apply_cli_log_noise_defaults_sets_eventbus_rule(monkeypatch):
    monkeypatch.delenv("ADAOS_CLI_DEBUG", raising=False)
    monkeypatch.delenv("ADAOS_LOG_HIDE", raising=False)

    cli_app._apply_cli_log_noise_defaults()

    assert os.environ["ADAOS_LOG_HIDE"] == "adaos.eventbus=INFO"


def test_apply_cli_log_noise_defaults_appends_without_overwriting_existing_rules(monkeypatch):
    monkeypatch.delenv("ADAOS_CLI_DEBUG", raising=False)
    monkeypatch.setenv("ADAOS_LOG_HIDE", "adaos.router=WARNING")

    cli_app._apply_cli_log_noise_defaults()

    assert os.environ["ADAOS_LOG_HIDE"] == "adaos.router=WARNING,adaos.eventbus=INFO"


def test_apply_cli_log_noise_defaults_respects_cli_debug(monkeypatch):
    monkeypatch.setenv("ADAOS_CLI_DEBUG", "1")
    monkeypatch.delenv("ADAOS_LOG_HIDE", raising=False)

    cli_app._apply_cli_log_noise_defaults()

    assert "ADAOS_LOG_HIDE" not in os.environ


def test_apply_cli_log_noise_defaults_keeps_explicit_eventbus_rule(monkeypatch):
    monkeypatch.delenv("ADAOS_CLI_DEBUG", raising=False)
    monkeypatch.setenv("ADAOS_LOG_HIDE", "adaos.eventbus=DEBUG,adaos.router=WARNING")

    cli_app._apply_cli_log_noise_defaults()

    assert os.environ["ADAOS_LOG_HIDE"] == "adaos.eventbus=DEBUG,adaos.router=WARNING"
