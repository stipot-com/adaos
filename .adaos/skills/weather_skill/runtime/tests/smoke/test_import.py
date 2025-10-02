import importlib
import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    """Insert the slot src directory so namespace imports work standalone."""

    current = Path(__file__).resolve()
    try:
        src_root = next(parent for parent in current.parents if parent.name == "src")
    except StopIteration as exc:  # pragma: no cover - defensive guard
        raise RuntimeError("unable to locate slot src directory") from exc

    src_str = str(src_root)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)

if __name__ == "__main__":
    _ensure_src_on_path()
    importlib.import_module("skills.weather_skill.handlers.main")
