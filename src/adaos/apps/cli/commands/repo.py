from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

import typer

from adaos.apps.cli.i18n import _
from adaos.services.agent_context import get_ctx
from adaos.services.workspace_registry import list_workspace_registry_entries, workspace_registry_path

app = typer.Typer(help=_("cli.repo.help"))
registry_app = typer.Typer(help="Read workspace registry metadata.")
app.add_typer(registry_app, name="registry")


def _resolve_cache(kind: str) -> Path:
    ctx = get_ctx()
    attr_name = f"{kind}_cache_dir"
    attr = getattr(ctx.paths, attr_name, None)
    if attr is not None:
        value = attr() if callable(attr) else attr
    else:
        fallback = getattr(ctx.paths, f"{kind}_dir")
        value = fallback() if callable(fallback) else fallback
    return Path(value).expanduser().resolve()


def _run_git(repo: Path, args: list[str]) -> None:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(detail)


def _normalize_registry_kind(value: str | None) -> Literal["skills", "scenarios"] | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized or normalized == "all":
        return None
    if normalized in {"skill", "skills"}:
        return "skills"
    if normalized in {"scenario", "scenarios"}:
        return "scenarios"
    raise typer.BadParameter("kind must be 'skills' or 'scenarios'")


@app.command("reset")
def reset(
    kind: str = typer.Argument(..., help=_("cli.repo.reset.help")),
):
    normalized = kind.strip().lower()
    if normalized not in {"skills", "scenarios"}:
        raise typer.BadParameter("kind must be 'skills' or 'scenarios'")

    repo_path = _resolve_cache(normalized)
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        typer.secho(_("cli.repo.reset.not_initialized", kind=normalized, path=repo_path), fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.echo(_("cli.repo.reset.running", kind=normalized, path=repo_path))
    try:
        _run_git(repo_path, ["fetch", "origin"])
        _run_git(repo_path, ["reset", "--hard", "origin/main"])
        _run_git(repo_path, ["clean", "-fdx"])
    except RuntimeError as err:
        typer.secho(_("cli.repo.reset.failed", reason=str(err)), fg=typer.colors.RED)
        raise typer.Exit(1)

    typer.secho(_("cli.repo.reset.done", path=repo_path), fg=typer.colors.GREEN)


@registry_app.command("list")
def registry_list(
    kind: str | None = typer.Option(None, "--kind", help="skills | scenarios"),
    name: str | None = typer.Option(None, "--name", help="Filter by canonical artifact name."),
    json_output: bool = typer.Option(False, "--json", help=_("cli.option.json")),
    fallback_scan: bool = typer.Option(
        True,
        "--fallback-scan/--no-fallback-scan",
        help="Rebuild from local workspace if registry.json is missing.",
    ),
):
    ctx = get_ctx()
    workspace_root = Path(ctx.paths.workspace_dir()).expanduser().resolve()
    registry_path = workspace_registry_path(workspace_root)
    normalized_kind = _normalize_registry_kind(kind)
    items = list_workspace_registry_entries(
        workspace_root,
        kind=normalized_kind,
        name=name,
        fallback_to_scan=fallback_scan,
    )

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "registry_path": str(registry_path),
                    "kind": normalized_kind or "all",
                    "items": items,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not items:
        typer.echo(f"no registry entries found at {registry_path}")
        return

    for item in items:
        artifact_kind = str(item.get("kind") or "?")
        artifact_name = str(item.get("name") or "?")
        version = str(item.get("version") or "unknown")
        title = str(item.get("title") or "").strip()
        suffix = f" ({title})" if title else ""
        typer.echo(f"{artifact_kind}: {artifact_name} v{version}{suffix}")
