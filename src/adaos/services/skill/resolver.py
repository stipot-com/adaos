from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

SkillSpace = Literal["dev", "workspace"]


@dataclass(frozen=True)
class SkillPathResolver:
    dev_root: Path
    workspace_root: Path

    def resolve(self, name: str, *, space: SkillSpace = "workspace") -> Path:
        """
        Return absolute path to skill folder by name within selected space.
        Raises FileNotFoundError if not found.
        """
        if space == "dev":
            root = self.dev_root
        elif space == "workspace":
            root = self.workspace_root
        else:
            raise ValueError(f"unknown skill space: {space}")

        path = (root / name).resolve()
        if not path.exists():
            raise FileNotFoundError(f"skill '{name}' not found in {space} at {path}")
        if not path.is_dir():
            raise FileNotFoundError(f"skill '{name}' in {space} exists but is not a directory: {path}")
        return path
