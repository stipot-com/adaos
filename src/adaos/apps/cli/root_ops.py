from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Callable, Optional

import httpx

from adaos.apps.bootstrap import get_ctx

DEFAULT_ROOT_BASE = "http://127.0.0.1:3030"
DEFAULT_ROOT_TOKEN = "dev-root-token"
DEFAULT_ADAOS_BASE = "http://127.0.0.1:8777"
DEFAULT_ADAOS_TOKEN = "dev-local-token"
CONFIG_FILENAME = "root-cli.json"
FORGE_ROOT = Path("/tmp/forge")
DRY_RUN_PLACEHOLDER_TS = "<ts>"


class RootCliError(RuntimeError):
    """Raised for CLI integration failures with the Root service."""


@dataclass
class RootCliConfig:
    root_base: str = DEFAULT_ROOT_BASE
    root_token: str = DEFAULT_ROOT_TOKEN
    subnet_id: Optional[str] = None
    node_id: Optional[str] = None
    adaos_base: str = DEFAULT_ADAOS_BASE
    adaos_token: str = DEFAULT_ADAOS_TOKEN


def _config_path() -> Path:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / CONFIG_FILENAME


def load_root_cli_config() -> RootCliConfig:
    path = _config_path()
    defaults_instance = RootCliConfig()
    defaults = {field.name: getattr(defaults_instance, field.name) for field in fields(RootCliConfig)}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise RootCliError(f"Failed to parse {path}: {exc}") from exc
        for key in defaults:
            if key in data:
                defaults[key] = data[key]
    config = RootCliConfig(**defaults)
    if not path.exists():
        save_root_cli_config(config)
    return config


def save_root_cli_config(config: RootCliConfig) -> None:
    path = _config_path()
    path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _root_url(config: RootCliConfig, suffix: str) -> str:
    base = (config.root_base or DEFAULT_ROOT_BASE).rstrip("/")
    return f"{base}{suffix}"


def _request(
    method: str,
    url: str,
    *,
    config: RootCliConfig,
    json_body: Optional[dict] = None,
    timeout: float = 10.0,
    extra_headers: Optional[dict] = None,
) -> httpx.Response:
    headers = {"X-AdaOS-Token": config.root_token or DEFAULT_ROOT_TOKEN}
    if extra_headers:
        headers.update(extra_headers)
    try:
        response = httpx.request(method, url, json=json_body, headers=headers, timeout=timeout)
    except httpx.RequestError as exc:
        raise RootCliError(f"{method} {url} failed: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text.strip()
        raise RootCliError(
            f"{method} {url} failed with status {response.status_code}: {detail or 'no body'}"
        )
    return response


def run_preflight_checks(
    config: RootCliConfig,
    *,
    dry_run: bool,
    echo: Callable[[str], None],
) -> None:
    root_health = _root_url(config, "/healthz")
    bridge_health = _root_url(config, "/adaos/healthz")
    if dry_run:
        echo(f"[dry-run] GET {root_health}")
        echo(
            f"[dry-run] GET {bridge_health} with headers "
            f"X-AdaOS-Base={config.adaos_base or DEFAULT_ADAOS_BASE}, "
            "X-AdaOS-Token=<masked>"
        )
        return

    _request("GET", root_health, config=config)
    echo(f"Preflight OK: {root_health}")
    _request(
        "GET",
        bridge_health,
        config=config,
        json_body=None,
        timeout=10.0,
        extra_headers={
            "X-AdaOS-Base": config.adaos_base or DEFAULT_ADAOS_BASE,
            "X-AdaOS-Token": config.adaos_token or DEFAULT_ADAOS_TOKEN,
        },
    )
    echo(
        "Preflight OK: {url}".format(
            url=f"{bridge_health}?base={config.adaos_base or DEFAULT_ADAOS_BASE}"
        )
    )


def ensure_registration(
    config: RootCliConfig,
    *,
    dry_run: bool,
    subnet_name: Optional[str],
    echo: Callable[[str], None],
) -> RootCliConfig:
    updated = replace(config)
    changed = False

    if not updated.subnet_id:
        payload = {"subnet_name": subnet_name} if subnet_name else None
        if dry_run:
            echo(
                f"[dry-run] POST {_root_url(config, '/v1/subnets/register')} "
                f"payload={payload or '{}'}"
            )
            updated.subnet_id = updated.subnet_id or "<subnet-id>"
        else:
            response = _request(
                "POST",
                _root_url(config, "/v1/subnets/register"),
                config=config,
                json_body=payload,
            )
            data = response.json()
            subnet_id = data.get("subnet_id")
            if not subnet_id:
                raise RootCliError("Root did not return subnet_id")
            updated.subnet_id = subnet_id
            changed = True
            echo(f"Registered subnet: {subnet_id}")

    if not updated.node_id:
        if dry_run:
            echo(
                f"[dry-run] POST {_root_url(config, '/v1/nodes/register')} "
                f"payload={{'subnet_id': {updated.subnet_id or '<subnet-id>'}}}"
            )
            updated.node_id = updated.node_id or "<node-id>"
        else:
            if not updated.subnet_id:
                raise RootCliError("Subnet registration failed: subnet_id is missing")
            response = _request(
                "POST",
                _root_url(config, "/v1/nodes/register"),
                config=config,
                json_body={"subnet_id": updated.subnet_id},
            )
            data = response.json()
            node_id = data.get("node_id")
            if not node_id:
                raise RootCliError("Root did not return node_id")
            updated.node_id = node_id
            updated.subnet_id = data.get("subnet_id") or updated.subnet_id
            changed = True
            echo(f"Registered node: {node_id} (subnet {updated.subnet_id})")

    if changed and not dry_run:
        save_root_cli_config(updated)

    return updated


def assert_safe_name(name: str) -> None:
    if not name or name.strip() == "":
        raise RootCliError("Name must not be empty")
    if ".." in name or "/" in name or "\\" in name:
        raise RootCliError(f"Name '{name}' contains forbidden characters")
    if Path(name).name != name:
        raise RootCliError(f"Name '{name}' is not a safe basename")


_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "node_modules",
    "dist",
    "build",
    ".idea",
}
_SKIP_FILES = {".DS_Store"}


def _should_skip(path: Path) -> bool:
    for part in path.parts[:-1]:
        if part in _SKIP_DIRS:
            return True
    if path.name in _SKIP_FILES:
        return True
    if path.suffix in {".pyc", ".pyo"}:
        return True
    return False


def create_zip_bytes(root: Path) -> bytes:
    root = root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise RootCliError(f"Path '{root}' is not a directory")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(root)
            if _should_skip(rel):
                continue
            zf.write(path, arcname=str(rel))
    buffer.seek(0)
    return buffer.read()


def archive_bytes_to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def push_skill_draft(
    config: RootCliConfig,
    *,
    node_id: Optional[str],
    name: str,
    archive_b64: str,
    dry_run: bool,
    echo: Callable[[str], None],
) -> str:
    url = _root_url(config, "/v1/skills/draft")
    target_node = node_id or "<node-id>"
    if dry_run:
        forge_path = FORGE_ROOT / target_node / "skills" / name / f"{DRY_RUN_PLACEHOLDER_TS}_{name}.zip"
        echo(
            f"[dry-run] POST {url} payload={{'node_id': '{target_node}', 'name': '{name}', 'archive_b64': '<omitted>'}}"
        )
        return str(forge_path)

    if not node_id:
        raise RootCliError("Node ID is not configured; run registration first")

    response = _request(
        "POST",
        url,
        config=config,
        json_body={"node_id": node_id, "name": name, "archive_b64": archive_b64},
        timeout=30.0,
    )
    data = response.json()
    stored = data.get("stored")
    if not stored:
        raise RootCliError("Root did not return stored path")
    return stored


def store_scenario_draft(
    *,
    node_id: Optional[str],
    name: str,
    archive_bytes: bytes,
    dry_run: bool,
    echo: Callable[[str], None],
) -> str:
    target_node = node_id or "<node-id>"
    base_dir = FORGE_ROOT / target_node / "scenarios" / name
    if dry_run:
        predicted = base_dir / f"{DRY_RUN_PLACEHOLDER_TS}_{name}.zip"
        echo(f"[dry-run] Would store scenario archive at {predicted}")
        return str(predicted)

    if not node_id:
        raise RootCliError("Node ID is not configured; run registration first")

    base_dir.mkdir(parents=True, exist_ok=True)
    filename = base_dir / f"{int(time.time())}_{name}.zip"
    filename.write_bytes(archive_bytes)
    return str(filename)


__all__ = [
    "RootCliConfig",
    "RootCliError",
    "archive_bytes_to_b64",
    "assert_safe_name",
    "create_zip_bytes",
    "ensure_registration",
    "load_root_cli_config",
    "push_skill_draft",
    "run_preflight_checks",
    "store_scenario_draft",
]
