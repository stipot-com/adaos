import sys
from pathlib import Path


def _ensure_src_on_path() -> None:
    current = Path(__file__).resolve()
    try:
        src_root = next(parent for parent in current.parents if parent.name == "src")
    except StopIteration as exc:  # pragma: no cover - defensive guard
        raise RuntimeError("unable to locate slot src directory") from exc

    src_str = str(src_root)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


_ensure_src_on_path()

from skills.weather_skill.handlers.main import get_weather

if __name__ == "__main__":
    result = get_weather(city="Test City")
    if not isinstance(result, dict):
        raise SystemExit("result must be a dict")
    if "ok" not in result:
        raise SystemExit("result missing 'ok' field")
    raise SystemExit(0)
