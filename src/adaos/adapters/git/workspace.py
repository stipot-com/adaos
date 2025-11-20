from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from adaos.ports.git import GitClient


@dataclass(slots=True)
class SparseWorkspace:
    """Helper that manages git sparse-checkout patterns for a monorepo workspace."""

    git: GitClient
    root: Path

    def ensure_initialized(self, *, cone: bool = False) -> None:
        """Ensure sparse-checkout is initialised; ignore if already configured."""
        try:
            self.git.sparse_init(str(self.root), cone=cone)
        except Exception:
            # git returns a non-zero code if sparse-checkout is already initialised.
            pass

    # Patterns -----------------------------------------------------------------
    @property
    def patterns_file(self) -> Path:
        return self.root / ".git" / "info" / "sparse-checkout"

    def read_patterns(self) -> list[str]:
        sp = self.patterns_file
        if not sp.exists():
            return []
        lines: list[str] = []
        for raw in sp.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line not in lines:
                lines.append(line)
        return lines

    def write_patterns(self, patterns: Sequence[str]) -> None:
        sp = self.patterns_file
        sp.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(patterns)
        if content:
            content += "\n"
        sp.write_text(content, encoding="utf-8")

    def update(self, *, add: Iterable[str] = (), remove: Iterable[str] = ()) -> list[str]:
        """Update sparse-checkout patterns and apply them."""

        current = self.read_patterns()
        updated = [p for p in current if p not in remove]
        for item in add:
            if item not in updated:
                updated.append(item)

        self.ensure_initialized(cone=False)
        if updated:
            self.git.sparse_set(str(self.root), updated, no_cone=True)
        else:
            self.write_patterns([])
            try:
                self.git.sparse_reapply(str(self.root))
            except AttributeError:
                pass
            except Exception:
                # If sparse-checkout is already effectively disabled, ignore errors.
                pass
        return updated


def wait_for_materialized(
    path: Path,
    *,
    files: Sequence[str] = (),
    attempts: int = 10,
    delay: float = 0.1,
) -> None:
    """Wait until *path* exists and (optionally) at least one *files* item materialises."""

    last_error: FileNotFoundError | None = None
    for attempt in range(1, attempts + 1):
        if path.exists():
            if files:
                if any((path / name).exists() for name in files):
                    return
                last_error = FileNotFoundError(
                    f"none of the expected files present: {', '.join(files)}"
                )
            else:
                return
        else:
            last_error = FileNotFoundError(f"path '{path}' is absent")
        time.sleep(delay * attempt)
    if last_error is None:
        last_error = FileNotFoundError(f"path '{path}' is absent")
    raise last_error
