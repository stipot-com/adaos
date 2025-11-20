"""Utilities for exposing AdaOS build metadata.

The project keeps a static semantic version in :mod:`pyproject.toml`, but for
internal deployments we want automatically increasing versions on every push
without having to edit the sources manually.  To achieve that we derive a
monotonic build identifier from the Git history (commit count + short SHA) and
expose it together with the commit timestamp.  Both values can be overridden by
environment variables so CI pipelines or packaged builds may inject canonical
information.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from typing import Final


_BASE_VERSION: Final[str] = os.getenv("ADAOS_BASE_VERSION", "0.1.0")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=_repo_root(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return completed.stdout.strip()
    except Exception:
        return None


def _compute_version() -> str:
    explicit = os.getenv("ADAOS_BUILD_VERSION")
    if explicit:
        return explicit

    rev_count = _git("rev-list", "--count", "HEAD")
    short_sha = _git("rev-parse", "--short", "HEAD")
    if rev_count:
        suffix = f"+{rev_count}"
        if short_sha:
            suffix += f".{short_sha}"
        return f"{_BASE_VERSION}{suffix}"

    return _BASE_VERSION


def _compute_build_date() -> str:
    explicit = os.getenv("ADAOS_BUILD_DATE")
    if explicit:
        return explicit

    commit_ts = _git("show", "-s", "--format=%cI", "HEAD")
    if commit_ts:
        return commit_ts

    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class BuildInfo:
    version: str
    build_date: str


def _load_build_info() -> BuildInfo:
    return BuildInfo(version=_compute_version(), build_date=_compute_build_date())


BUILD_INFO: Final[BuildInfo] = _load_build_info()

