from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from adaos.services.capacity import get_io_capacity_entry, is_io_available, set_io_state


@dataclass(frozen=True, slots=True)
class GitAvailability:
    enabled: bool
    git_path: str | None = None
    reason: str | None = None
    mode: str | None = None  # e.g. "disabled", "auto"
    source: str | None = None  # "node.yaml" | "which" | "env"


def _which_git() -> str | None:
    try:
        return shutil.which("git")
    except Exception:
        return None


def get_git_availability(*, base_dir: Path | None = None) -> GitAvailability:
    # Explicit env override always wins.
    env = str(os.getenv("ADAOS_GIT_MODE", "") or "").strip().lower()
    if env in {"0", "off", "false", "disabled", "disable", "no"}:
        return GitAvailability(enabled=False, git_path=_which_git(), reason="disabled by ADAOS_GIT_MODE", mode="disabled", source="env")

    entry = get_io_capacity_entry("git", base_dir=base_dir)
    if entry:
        caps = [str(x) for x in (entry.get("capabilities") or [])]
        mode = None
        for c in caps:
            if c.startswith("mode:"):
                mode = c.split(":", 1)[1].strip() or None
        enabled = is_io_available("git", base_dir=base_dir, default=True)
        if not enabled:
            return GitAvailability(enabled=False, git_path=_which_git(), reason="disabled in node.yaml capacity", mode=(mode or "disabled"), source="node.yaml")
        return GitAvailability(enabled=True, git_path=_which_git(), reason=None, mode=(mode or "available"), source="node.yaml")

    git_path = _which_git()
    if not git_path:
        return GitAvailability(enabled=False, git_path=None, reason="git not found in PATH", mode="auto", source="which")
    return GitAvailability(enabled=True, git_path=git_path, reason=None, mode="auto", source="which")


def autodetect_git(*, base_dir: Path | None = None, reason_hint: str | None = None) -> GitAvailability:
    """
    Detect whether git can be used and persist the result into node.yaml capacity io:git.
    """
    av = get_git_availability(base_dir=base_dir)
    # If env forced, still persist for observability.
    enabled = bool(av.enabled and av.git_path)
    if enabled:
        set_io_state(
            "git",
            available=True,
            base_capabilities=["git"],
            reason=None,
            mode="available",
            priority=45,
            base_dir=base_dir,
        )
        return GitAvailability(enabled=True, git_path=av.git_path, reason=None, mode="available", source=av.source)

    reason = av.reason or reason_hint or "git is disabled/unavailable"
    set_io_state(
        "git",
        available=False,
        base_capabilities=["git"],
        reason=reason,
        mode="disabled",
        priority=45,
        base_dir=base_dir,
    )
    return GitAvailability(enabled=False, git_path=av.git_path, reason=reason, mode="disabled", source=av.source)


def set_git_enabled(*, base_dir: Path | None = None) -> GitAvailability:
    git_path = _which_git()
    if not git_path:
        set_io_state("git", available=False, base_capabilities=["git"], reason="git not found in PATH", mode="disabled", priority=45, base_dir=base_dir)
        return GitAvailability(enabled=False, git_path=None, reason="git not found in PATH", mode="disabled", source="which")
    set_io_state("git", available=True, base_capabilities=["git"], reason=None, mode="available", priority=45, base_dir=base_dir)
    return GitAvailability(enabled=True, git_path=git_path, reason=None, mode="available", source="which")


def set_git_disabled(*, base_dir: Path | None = None, reason: str | None = None) -> GitAvailability:
    set_io_state("git", available=False, base_capabilities=["git"], reason=(reason or "disabled"), mode="disabled", priority=45, base_dir=base_dir)
    return GitAvailability(enabled=False, git_path=_which_git(), reason=(reason or "disabled"), mode="disabled", source="node.yaml")

