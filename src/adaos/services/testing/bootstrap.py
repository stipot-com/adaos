"""Utilities for running skill tests without a full AdaOS runtime bootstrap."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping

from adaos.adapters.db.sqlite_store import SQLite, SQLiteKV
from adaos.adapters.fs.path_provider import PathProvider
from adaos.services.agent_context import AgentContext, clear_ctx, get_ctx, set_ctx
from adaos.services.eventbus import LocalEventBus
from adaos.services.settings import Settings

__all__ = [
    "MockSecrets",
    "MockCapabilities",
    "TestContextHandle",
    "bootstrap_test_ctx",
    "load_test_secrets",
    "mount_skill_paths_for_testing",
    "unmount_skill_paths",
    "skill_tests_root",
]


class MockSecrets:
    """In-memory implementation of the :class:`~adaos.ports.secrets.Secrets` port."""

    def __init__(self, data: Mapping[str, str] | None = None) -> None:
        self._data: dict[str, str] = dict(data or {})

    def get(self, key: str, default: str | None = None, *, scope: str = "profile") -> str | None:
        return self._data.get(key, default)

    def put(
        self,
        key: str,
        value: str,
        *,
        scope: str = "profile",
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._data[key] = value

    def delete(self, key: str, *, scope: str = "profile") -> None:
        self._data.pop(key, None)

    def list(self, *, scope: str = "profile") -> list[dict[str, str]]:
        return [
            {"key": key, "value": value, "scope": scope}
            for key, value in sorted(self._data.items())
        ]

    def import_items(self, items: Iterable[Mapping[str, Any]], *, scope: str = "profile") -> int:
        count = 0
        for item in items:
            key = item.get("key")
            value = item.get("value")
            if key is None or value is None:
                continue
            self._data[str(key)] = str(value)
            count += 1
        return count

    def export_items(self, *, scope: str = "profile") -> list[dict[str, str]]:
        return [
            {"key": key, "value": value, "scope": scope}
            for key, value in sorted(self._data.items())
        ]


class MockCapabilities:
    """Capability facade that approves every request by default."""

    def __init__(self, *, allow_all: bool = True) -> None:
        self._allow_all = allow_all
        self._grants: set[str] = set()

    def grant(self, *capabilities: str) -> None:
        self._grants.update(capabilities)

    def allows(self, subject: str, capability: str) -> bool:
        if self._allow_all:
            return True
        return capability in self._grants

    def require(self, subject: str, capability: str) -> None:
        if self._allow_all:
            return
        if capability not in self._grants:
            raise PermissionError(f"capability '{capability}' not granted for {subject}")


class _Noop:
    """Return ``None`` for any attribute access to keep mocks tolerant."""

    def __getattr__(self, name: str):  # pragma: no cover - trivial behaviour
        def _noop(*_: Any, **__: Any) -> None:
            return None

        return _noop


class _NoopProcess(_Noop):
    async def start(self, *_: Any, **__: Any) -> None:  # pragma: no cover - async noop
        return None

    async def status(self, *_: Any, **__: Any) -> str:  # pragma: no cover - async noop
        return "stopped"


@dataclass(slots=True)
class TestContextHandle:
    """Handle returned by :func:`bootstrap_test_ctx` for deterministic teardown."""

    ctx: AgentContext
    previous: AgentContext | None

    def teardown(self) -> None:
        if self.previous is None:
            clear_ctx()
        else:
            set_ctx(self.previous)


def _infer_base_dir(slot_dir: Path) -> Path:
    for parent in slot_dir.resolve().parents:
        if parent.name == ".adaos":
            return parent
    try:
        ctx = get_ctx()
    except RuntimeError:
        ctx = None
    else:
        paths = getattr(ctx, "paths", None)
        if paths is not None and hasattr(paths, "base_dir"):
            base_dir = paths.base_dir()
            return base_dir if isinstance(base_dir, Path) else Path(base_dir).resolve()
    override = os.getenv("ADAOS_BASE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(Settings.from_sources().base_dir).expanduser().resolve()


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip("\"\'")
    return data


def skill_tests_root(skill_slot_dir: Path, skill_name: str) -> Path:
    return skill_slot_dir / "src" / "skills" / skill_name / "tests"


def load_test_secrets(
    skill_slot_dir: Path,
    *,
    skill_name: str,
    env_prefix: str | None = None,
) -> dict[str, str]:
    """Collect secrets for skill tests from the environment and config files."""

    tests_root = skill_tests_root(skill_slot_dir, skill_name)
    prefix = env_prefix if env_prefix is not None else os.getenv("ADAOS_SKILL_TEST_ENV_PREFIX")
    secrets: dict[str, str] = {}

    if prefix == "*":
        secrets.update({k: v for k, v in os.environ.items() if isinstance(v, str)})
    elif prefix:
        secrets.update({k: v for k, v in os.environ.items() if k.startswith(prefix)})

    dotenv = _parse_env_file(tests_root / ".env")
    secrets.update(dotenv)

    config_path = tests_root / "test.config.json"
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, MutableMapping):
            secrets.update({str(k): str(v) for k, v in payload.items()})

    return secrets


def mount_skill_paths_for_testing(
    skill_name: str,
    skill_ver: str,
    slots_current_dir: Path,
) -> list[str]:
    """Place the active slot vendor/src directories at the front of ``sys.path``."""

    import sys

    slot_root = slots_current_dir.resolve()
    vendor = slot_root / "vendor"
    src = slot_root / "src"

    suffixes = {
        f"/{skill_name}/slots/current/src",
        f"/{skill_name}/slots/current/vendor",
        f"/{skill_name}/{skill_ver}/slots/A/src",
        f"/{skill_name}/{skill_ver}/slots/B/src",
        f"/{skill_name}/{skill_ver}/slots/A/vendor",
        f"/{skill_name}/{skill_ver}/slots/B/vendor",
    }

    def _is_outdated(entry: str) -> bool:
        normalised = entry.replace("\\", "/")
        return any(normalised.endswith(suffix) for suffix in suffixes)

    sys.path[:] = [entry for entry in sys.path if not _is_outdated(entry)]

    candidates: list[str] = []
    for candidate in (vendor, src):
        if candidate.is_dir():
            candidate_str = str(candidate)
            if candidate_str not in sys.path:
                candidates.append(candidate_str)

    inserted: list[str] = []
    for candidate_str in reversed(candidates):
        sys.path.insert(0, candidate_str)
        inserted.insert(0, candidate_str)
    return inserted


def unmount_skill_paths(paths: Iterable[str]) -> None:
    """Remove ``paths`` from ``sys.path`` preserving the order of other entries."""

    import sys

    removable = set(paths)
    if not removable:
        return
    sys.path[:] = [entry for entry in sys.path if entry not in removable]


def bootstrap_test_ctx(
    *,
    skill_name: str,
    skill_slot_dir: Path,
    secrets: Mapping[str, str],
    extra_caps: Mapping[str, Any] | None = None,
) -> TestContextHandle:
    """Create and install a minimal :class:`AgentContext` for skill tests."""

    base_dir = _infer_base_dir(skill_slot_dir)
    settings = Settings.from_sources().with_overrides(base_dir=base_dir, profile="test")
    paths = PathProvider(settings)
    paths.ensure_tree()

    bus = LocalEventBus()
    sql = SQLite(paths)
    kv = SQLiteKV(sql)
    proc = _NoopProcess()
    caps = MockCapabilities()
    caps.grant("secrets.read", "logger", "storage.temp")
    if extra_caps:
        caps.grant(*extra_caps.keys())

    secrets_backend = MockSecrets(secrets)
    noop = _Noop()

    ctx = AgentContext(
        settings=settings,
        paths=paths,
        bus=bus,
        proc=proc,
        caps=caps,
        devices=noop,
        kv=kv,
        sql=sql,
        secrets=secrets_backend,
        net=noop,
        updates=noop,
        git=noop,
        fs=noop,
        sandbox=noop,
    )

    try:
        previous = get_ctx()
    except RuntimeError:
        previous = None

    set_ctx(ctx)
    ctx.skill_ctx.set(skill_name, skill_slot_dir)

    return TestContextHandle(ctx=ctx, previous=previous)
