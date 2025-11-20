import sys
from pathlib import Path


def _ensure_slot_paths() -> None:
    current = Path(__file__).resolve()
    try:
        src_root = next(parent for parent in current.parents if parent.name == "src")
    except StopIteration as exc:  # pragma: no cover - defensive guard
        raise RuntimeError("unable to locate slot src directory") from exc

    slot_root = src_root.parent
    vendor_root = slot_root / "vendor"

    paths: list[Path] = []
    if vendor_root.is_dir():
        paths.append(vendor_root)
    paths.append(src_root)

    for path in reversed(paths):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


_ensure_slot_paths()

from skills.weather_skill.handlers.main import get_weather

if __name__ == "__main__":
    result = get_weather(city="Test City")
    if not isinstance(result, dict):
        raise SystemExit("result must be a dict")
    if "ok" not in result:
        raise SystemExit("result missing 'ok' field")
    raise SystemExit(0)
