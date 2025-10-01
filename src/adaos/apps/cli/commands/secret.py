from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from adaos.services.agent_context import get_ctx
from adaos.services.secrets.service import SecretsService
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment
from adaos.services.skill.secrets_backend import SkillSecretsBackend

app = typer.Typer(help="Управление секретами")


def _svc(skill: str | None = None) -> SecretsService:
    ctx = get_ctx()
    if skill:
        skills_root = Path(ctx.paths.skills_dir())
        env = SkillRuntimeEnvironment(skills_root=skills_root, skill_name=skill)
        env.ensure_base()
        return SecretsService(SkillSecretsBackend(env.data_root() / "files" / "secrets.json"), ctx.caps)
    if not hasattr(ctx, "secrets") or not hasattr(ctx.secrets, "put"):
        raise typer.BadParameter("Secrets backend не инициализирован. Проверь bootstrap: ctx.secrets должен быть SecretsService.")
    return ctx.secrets


def _redact(v: str) -> str:
    if v is None:
        return "-"
    if len(v) <= 8:
        return "*" * len(v)
    return v[:2] + "*" * (len(v) - 6) + v[-4:]


@app.command("set")
def cmd_set(
    key: str,
    value: str,
    scope: str = typer.Option("profile", "--scope", help="profile|global"),
    skill: str | None = typer.Option(None, "--skill", "-s", help="Работать с секретами конкретного навыка"),
):
    _svc(skill).put(key, value, scope=scope)  # значений в stdout не печатаем
    typer.echo(f"Saved: {key} [{scope}]")


@app.command("get")
def cmd_get(
    key: str,
    show: bool = typer.Option(False, "--show", help="Показать значение"),
    scope: str = typer.Option("profile", "--scope"),
    skill: str | None = typer.Option(None, "--skill", "-s", help="Работать с секретами конкретного навыка"),
):
    v = _svc(skill).get(key, scope=scope)
    if v is None:
        typer.echo("Not found")
        raise typer.Exit(1)
    typer.echo(v if show else _redact(v))


@app.command("list")
def cmd_list(
    scope: str = typer.Option("profile", "--scope"),
    skill: str | None = typer.Option(None, "--skill", "-s", help="Работать с секретами конкретного навыка"),
):
    items = _svc(skill).list(scope=scope)
    for it in items:
        typer.echo(f"- {it['key']} ({json.dumps(it.get('meta') or {})})")


@app.command("delete")
def cmd_delete(
    key: str,
    scope: str = typer.Option("profile", "--scope"),
    skill: str | None = typer.Option(None, "--skill", "-s", help="Работать с секретами конкретного навыка"),
):
    _svc(skill).delete(key, scope=scope)
    typer.echo(f"Deleted: {key} [{scope}]")


@app.command("export")
def cmd_export(
    scope: str = typer.Option("profile", "--scope"),
    show: bool = typer.Option(False, "--show"),
    skill: str | None = typer.Option(None, "--skill", "-s", help="Работать с секретами конкретного навыка"),
):
    data = _svc(skill).export_items(scope=scope)
    if not show:
        # по умолчанию редактируем значения
        for d in data:
            if "value" in d and d["value"] is not None:
                d["value"] = _redact(d["value"])
    typer.echo(json.dumps({"items": data}, ensure_ascii=False, indent=2))


@app.command("import")
def cmd_import(
    file: str,
    scope: str = typer.Option("profile", "--scope"),
    skill: str | None = typer.Option(None, "--skill", "-s", help="Работать с секретами конкретного навыка"),
):
    if file == "-":
        payload = json.loads(sys.stdin.read())
    else:
        payload = json.loads(Path(file).read_text(encoding="utf-8"))
    n = _svc(skill).import_items(payload.get("items", []), scope=scope)
    typer.echo(f"Imported: {n}")
