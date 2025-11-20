from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from adaos.apps.cli.i18n import _
from adaos.services.agent_context import get_ctx

app = typer.Typer(help=_("cli.repo.help"))


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
