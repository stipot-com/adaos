"""Skill lifecycle CLI commands for the runtime environment."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional

import typer

from adaos.sdk.data.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.skill.enrich import load_manifest
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment
from adaos.services.skill.runtime_service import InstallResult, SkillRuntimeService
from adaos.services.skill.tests_runner import TestResult


app = typer.Typer(help=_("cli.help_skill"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service() -> SkillRuntimeService:
    return SkillRuntimeService(get_ctx())


def _parse_name_version(name_version: str) -> tuple[str, Optional[str]]:
    if "@" in name_version:
        name, version = name_version.split("@", 1)
        return name.strip(), version.strip() or None
    return name_version.strip(), None


def _echo_install_result(result: InstallResult) -> None:
    typer.secho(
        f"installed {result.skill} v{result.version} into slot {result.slot}",
        fg=typer.colors.GREEN,
    )
    if result.tests:
        typer.echo(_format_tests(result.tests))
    typer.echo(f"resolved manifest: {result.resolved_manifest}")


def _format_tests(tests: dict[str, TestResult]) -> str:
    parts = []
    for name, outcome in tests.items():
        parts.append(f"{name}={outcome.status}")
    return "tests: " + ", ".join(parts)


def _load_resolved(manifest_path: Path) -> dict:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _deprecated(name: str):
    def handler(*_args, **_kwargs):
        typer.secho(
            f"Command '{name}' is deprecated. Please use 'adaos skill install' and 'adaos skill activate'.",
            fg=typer.colors.YELLOW,
        )

    return handler


# ---------------------------------------------------------------------------
# Lifecycle commands
# ---------------------------------------------------------------------------


@app.command("install")
def install_cmd(
    name: str = typer.Argument(..., help="skill name, optionally with @version"),
    test: bool = typer.Option(False, "--test", help="run runtime tests during install"),
    slot: Optional[str] = typer.Option(None, "--slot", help="target slot A or B"),
):
    service = _service()
    skill, version = _parse_name_version(name)
    try:
        result = service.install(skill, version_override=version, run_tests=test, preferred_slot=slot)
    except Exception as exc:  # pragma: no cover - CLI surface
        typer.secho(f"install failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    _echo_install_result(result)


@app.command("activate")
def activate_cmd(
    name: str,
    slot: Optional[str] = typer.Option(None, "--slot", help="slot to activate"),
    version: Optional[str] = typer.Option(None, "--version", help="explicit version"),
):
    service = _service()
    try:
        active = service.activate(name, version=version, slot=slot)
    except Exception as exc:  # pragma: no cover - CLI surface
        typer.secho(f"activate failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"skill {name} now active on slot {active}", fg=typer.colors.GREEN)


@app.command("rollback")
def rollback_cmd(name: str):
    service = _service()
    try:
        slot = service.rollback(name)
    except Exception as exc:  # pragma: no cover
        typer.secho(f"rollback failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"rolled back {name} to slot {slot}", fg=typer.colors.YELLOW)


@app.command("status")
def status_cmd(name: str, json_output: bool = typer.Option(False, "--json", help="machine readable output")):
    service = _service()
    try:
        status = service.status(name)
    except Exception as exc:
        typer.secho(f"status failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        typer.echo(f"skill: {status['name']}")
        typer.echo(f"version: {status['version']}")
        typer.echo(f"active slot: {status['active_slot']}")
        typer.echo(f"resolved manifest: {status['resolved_manifest']}")
        if status.get("tests"):
            typer.echo("tests: " + ", ".join(f"{k}={v}" for k, v in status["tests"].items()))


@app.command("run")
def run_cmd(
    name: str,
    tool: str,
    payload: str = typer.Option("{}", "--json", help="JSON payload for the tool"),
    timeout: Optional[float] = typer.Option(None, "--timeout", help="override timeout in seconds"),
):
    service = _service()
    try:
        status = service.status(name)
    except Exception as exc:
        typer.secho(f"run failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    manifest = _load_resolved(Path(status["resolved_manifest"]))
    tool_spec = manifest.get("tools", {}).get(tool)
    if not tool_spec:
        typer.secho(f"tool '{tool}' not found in resolved manifest", fg=typer.colors.RED)
        raise typer.Exit(1)
    command = tool_spec.get("command")
    if not command:
        typer.secho("resolved manifest missing command", fg=typer.colors.RED)
        raise typer.Exit(1)
    try:
        input_payload = json.dumps(json.loads(payload or "{}"))
    except json.JSONDecodeError as exc:
        typer.secho(f"invalid payload: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1)
    proc = subprocess.run(
        command,
        input=input_payload,
        text=True,
        capture_output=True,
        timeout=timeout or tool_spec.get("timeout_seconds"),
    )
    if proc.returncode != 0:
        typer.secho(proc.stdout, fg=typer.colors.RED)
        typer.secho(proc.stderr, fg=typer.colors.RED)
        raise typer.Exit(proc.returncode)
    typer.echo(proc.stdout.strip())


@app.command("uninstall")
def uninstall_cmd(name: str, purge_data: bool = typer.Option(False, "--purge-data", help="remove durable data")):
    service = _service()
    try:
        service.uninstall(name, purge_data=purge_data)
    except Exception as exc:
        typer.secho(f"uninstall failed: {exc}", fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    typer.secho(f"skill {name} uninstalled", fg=typer.colors.GREEN)


@app.command("gc")
def gc_cmd(name: Optional[str] = typer.Option(None, "--name", help="skill to clean")):
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    targets = [name] if name else [p.name for p in skills_root.glob("*") if p.is_dir()]
    for skill in targets:
        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill)
        active_version = env.resolve_active_version()
        for version in env.list_versions():
            if version == active_version:
                continue
            for slot in ("A", "B"):
                env.cleanup_slot(version, slot)
            version_root = env.version_root(version)
            if version_root.exists():
                for child in version_root.iterdir():
                    if child.is_file():
                        child.unlink()
                try:
                    version_root.rmdir()
                except OSError:
                    pass
        typer.echo(f"gc {skill}: kept version {active_version or 'none'}")


@app.command("doctor")
def doctor_cmd(name: str):
    ctx = get_ctx()
    env = SkillRuntimeEnvironment(skills_root=Path(ctx.paths.skills_dir()), skill_name=name)
    version = env.resolve_active_version()
    if not version:
        typer.echo("no installed versions")
        return
    slot = env.read_active_slot(version)
    slot_paths = env.build_slot_paths(version, slot)
    items = {
        "skill_root": str((Path(ctx.paths.skills_dir()) / name).resolve()),
        "runtime_root": str(env.runtime_root.resolve()),
        "active_slot": slot,
        "resolved_manifest": str(slot_paths.resolved_manifest),
    }
    typer.echo(json.dumps(items, ensure_ascii=False, indent=2))


@app.command("migrate")
def migrate_cmd(name: Optional[str] = typer.Option(None, "--name", help="specific skill to migrate")):
    typer.secho("Legacy skill installs are deprecated. Re-run 'adaos skill install' to migrate.", fg=typer.colors.YELLOW)
    if name:
        install_cmd(name)


@app.command("lint")
def lint_cmd(path: str):
    target = Path(path).resolve()
    try:
        manifest = load_manifest(target)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    required = {"name", "version", "tools"}
    missing = [key for key in required if key not in manifest]
    if missing:
        typer.secho(f"manifest missing keys: {', '.join(missing)}", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("lint passed", fg=typer.colors.GREEN)


@app.command("scaffold")
def scaffold_cmd(name: str):
    ctx = get_ctx()
    target = Path(ctx.paths.skills_dir()) / name
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": name,
        "version": "0.1.0",
        "runtime": {"type": "python", "module": "handlers.main"},
        "tools": [
            {
                "name": "echo",
                "entry": "handlers.main:echo",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
                "output_schema": {"type": "object"},
            }
        ],
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    handlers = target / "handlers"
    handlers.mkdir(exist_ok=True)
    (handlers / "__init__.py").write_text("", encoding="utf-8")
    (handlers / "main.py").write_text(
        """def echo(text: str | None = None) -> dict:\n    return {\"ok\": True, \"text\": text or \"\"}\n""",
        encoding="utf-8",
    )
    tests_dir = target / "runtime" / "tests" / "smoke"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_import.py").write_text(
        """import importlib; importlib.import_module('handlers.main')\n""",
        encoding="utf-8",
    )
    typer.secho(f"scaffold created at {target}", fg=typer.colors.GREEN)


# legacy commands kept for compatibility


@app.command("list")
def list_cmd(json_output: bool = typer.Option(False, "--json", help="output machine readable format")):
    ctx = get_ctx()
    skills_root = Path(ctx.paths.skills_dir())
    runtime_root = skills_root / ".runtime"
    items = []
    if runtime_root.exists():
        for skill_dir in runtime_root.iterdir():
            if not skill_dir.is_dir():
                continue
            env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill_dir.name)
            versions = env.list_versions()
            if not versions:
                continue
            items.append({"name": skill_dir.name, "version": env.resolve_active_version() or versions[-1]})
    payload = {"skills": items}
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        for item in items:
            typer.echo(f"{item['name']} ({item['version']})")


@app.command("create")
def create_cmd(name: str, template: str = typer.Option("demo_skill", "--template", help="legacy template name")):
    typer.secho("'skill create' is deprecated; use 'skill scaffold' instead", fg=typer.colors.YELLOW)
    scaffold_cmd(name)


app.command("sync")(_deprecated("sync"))
app.command("push")(_deprecated("push"))
app.command("prep")(_deprecated("prep"))

