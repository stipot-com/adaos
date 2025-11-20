"""Secret management CLI commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Iterable

import typer

from adaos.sdk.data.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.crypto.secrets_service import SecretsService
from adaos.services.skill.runtime_env import SkillRuntimeEnvironment
from adaos.services.skill.secrets_backend import SkillSecretsBackend


app = typer.Typer(help=_("cli.secret.help"))


def _skills_root() -> Path:
    ctx = get_ctx()
    base = getattr(ctx.paths, "skills_dir")
    root = base() if callable(base) else base
    return Path(root).expanduser().resolve()


def _known_skills() -> list[str]:
    runtime_root = _skills_root() / ".runtime"
    if not runtime_root.exists():
        return []
    return sorted([p.name for p in runtime_root.iterdir() if p.is_dir()])


def _service_for(skill: str | None) -> SecretsService:
    ctx = get_ctx()
    if skill:
        env = SkillRuntimeEnvironment(skills_root=_skills_root(), skill_name=skill)
        env.ensure_base()
        backend = SkillSecretsBackend(env.data_root() / "files" / "secrets.json")
        return SecretsService(backend, ctx.caps)
    if not hasattr(ctx, "secrets"):
        raise typer.BadParameter(_("cli.secret.backend.missing"))
    return ctx.secrets


def _redact(value: str | None) -> str:
    if value is None:
        return "-"
    if len(value) <= 8:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 6) + value[-4:]


def _print_skill_secrets(skill: str, items: Iterable[Dict[str, object]]) -> None:
    typer.echo(_("cli.secret.list.skill_header", name=skill))
    empty = True
    for item in items:
        key = item.get("key")
        meta = item.get("meta") or {}
        typer.echo(_("cli.secret.list.item", key=key, meta=json.dumps(meta, ensure_ascii=False)))
        empty = False
    if empty:
        typer.echo(_("cli.secret.list.empty_skill"))


@app.command("set", help=_("cli.secret.set.help"))
def cmd_set(
    key: str,
    value: str,
    scope: str = typer.Option("profile", "--scope", help=_("cli.secret.option.scope")),
    skill: str | None = typer.Option(None, "--skill", "-s", help=_("cli.secret.option.skill")),
):
    _service_for(skill).put(key, value, scope=scope)
    typer.echo(_("cli.secret.set.saved", key=key, scope=scope))


@app.command("get", help=_("cli.secret.get.help"))
def cmd_get(
    key: str,
    show: bool = typer.Option(False, "--show", help=_("cli.secret.get.show")),
    scope: str = typer.Option("profile", "--scope", help=_("cli.secret.option.scope")),
    skill: str | None = typer.Option(None, "--skill", "-s", help=_("cli.secret.option.skill")),
):
    value = _service_for(skill).get(key, scope=scope)
    if value is None:
        typer.echo(_("cli.secret.get.missing", key=key))
        raise typer.Exit(1)
    typer.echo(value if show else _redact(value))


@app.command("list", help=_("cli.secret.list.help"))
def cmd_list(
    scope: str = typer.Option("profile", "--scope", help=_("cli.secret.option.scope")),
    skill: str | None = typer.Option(None, "--skill", "-s", help=_("cli.secret.option.skill")),
):
    if skill:
        items = _service_for(skill).list(scope=scope)
        _print_skill_secrets(skill, items)
        return

    names = _known_skills()
    if not names:
        typer.echo(_("cli.secret.list.none"))
        return
    for name in names:
        items = _service_for(name).list(scope=scope)
        _print_skill_secrets(name, items)


@app.command("delete", help=_("cli.secret.delete.help"))
def cmd_delete(
    key: str,
    scope: str = typer.Option("profile", "--scope", help=_("cli.secret.option.scope")),
    skill: str | None = typer.Option(None, "--skill", "-s", help=_("cli.secret.option.skill")),
):
    _service_for(skill).delete(key, scope=scope)
    typer.echo(_("cli.secret.delete.done", key=key, scope=scope))


@app.command("export", help=_("cli.secret.export.help"))
def cmd_export(
    scope: str = typer.Option("profile", "--scope", help=_("cli.secret.option.scope")),
    show: bool = typer.Option(False, "--show", help=_("cli.secret.export.show")),
    skill: str | None = typer.Option(None, "--skill", "-s", help=_("cli.secret.option.skill")),
):
    if skill:
        data = _service_for(skill).export_items(scope=scope)
        if not show:
            for entry in data:
                if "value" in entry and entry["value"] is not None:
                    entry["value"] = _redact(str(entry["value"]))
        typer.echo(json.dumps({"skill": skill, "scope": scope, "items": data}, ensure_ascii=False, indent=2))
        return

    payload: Dict[str, Dict[str, object]] = {}
    names = _known_skills()
    for name in names:
        items = _service_for(name).export_items(scope=scope)
        if not show:
            for entry in items:
                if "value" in entry and entry["value"] is not None:
                    entry["value"] = _redact(str(entry["value"]))
        payload[name] = {"items": items}
    typer.echo(json.dumps({"scope": scope, "skills": payload}, ensure_ascii=False, indent=2))


@app.command("import", help=_("cli.secret.import.help"))
def cmd_import(
    file: str,
    scope: str = typer.Option("profile", "--scope", help=_("cli.secret.option.scope")),
    skill: str | None = typer.Option(None, "--skill", "-s", help=_("cli.secret.option.skill")),
):
    if file == "-":
        payload = json.loads(sys.stdin.read())
    else:
        payload = json.loads(Path(file).read_text(encoding="utf-8"))

    if skill:
        items = payload.get("items") if isinstance(payload, dict) else None
        if items is None:
            items = payload if isinstance(payload, list) else []
        count = _service_for(skill).import_items(items, scope=scope)
        typer.echo(_("cli.secret.import.count", count=count, skill=skill))
        return

    scope_override = payload.get("scope", scope)
    skills_payload = payload.get("skills", {}) if isinstance(payload, dict) else {}
    if not isinstance(skills_payload, dict):
        raise typer.BadParameter(_("cli.secret.import.invalid"))

    available = set(_known_skills())
    total = 0
    for skill_name, content in skills_payload.items():
        if skill_name not in available:
            typer.secho(_("cli.secret.import.missing_skill", name=skill_name), fg=typer.colors.YELLOW)
            continue
        items = []
        if isinstance(content, dict):
            maybe_items = content.get("items")
            if isinstance(maybe_items, list):
                items = maybe_items
        if not items:
            continue
        total += _service_for(skill_name).import_items(items, scope=scope_override)

    typer.echo(_("cli.secret.import.total", count=total))


__all__ = ["app"]

