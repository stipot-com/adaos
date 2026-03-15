from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, MutableMapping

try:
    from dotenv import dotenv_values, find_dotenv  # type: ignore
except Exception:  # pragma: no cover
    dotenv_values = None
    find_dotenv = None


_RUNTIME_ENV_PREFIXES = (
    "HUB_NATS_",
    "WS_NATS_PROXY_",
)
_RUNTIME_ENV_KEYS = {
    "ADAOS_WIN_SELECTOR_LOOP",
}


def _is_runtime_key(name: str) -> bool:
    key = str(name or "").strip()
    if not key:
        return False
    if key in _RUNTIME_ENV_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in _RUNTIME_ENV_PREFIXES)


def runtime_dotenv_overrides(dotenv_path: str | os.PathLike[str] | None = None) -> dict[str, str]:
    if dotenv_values is None:
        return {}
    path_value = str(dotenv_path or "").strip()
    if not path_value:
        try:
            path_value = str(find_dotenv() or "").strip() if callable(find_dotenv) else ""
        except Exception:
            path_value = ""
    if not path_value:
        return {}
    path = Path(path_value)
    if not path.exists():
        return {}
    try:
        raw = dotenv_values(path)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for key, value in (raw or {}).items():
        if not _is_runtime_key(str(key or "")):
            continue
        if value is None:
            continue
        out[str(key)] = str(value)
    return out


def apply_runtime_dotenv_overrides(
    env: MutableMapping[str, str] | None = None,
    *,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    target = os.environ if env is None else env
    overrides = runtime_dotenv_overrides(dotenv_path)
    for key, value in overrides.items():
        target[str(key)] = str(value)
    return overrides


def merged_runtime_dotenv_env(
    base_env: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    merged = dict(base_env or os.environ)
    merged.update(runtime_dotenv_overrides(dotenv_path))
    return merged
