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
