from __future__ import annotations

import base64
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

import httpx

from adaos.apps.bootstrap import get_ctx

DEFAULT_ROOT_BASE = "https://127.0.0.1:3030"
DEFAULT_ADAOS_BASE = "http://127.0.0.1:8777"
DEFAULT_ADAOS_TOKEN = "dev-local-token"
CONFIG_FILENAME = "root-cli.json"
KEYS_DIRNAME = "keys"


class RootCliError(RuntimeError):
    """Raised for CLI integration failures with the Root service."""


@dataclass
class KeyPairPaths:
    key: Optional[str] = None
    cert: Optional[str] = None


@dataclass
class RootCliKeys:
    hub: KeyPairPaths = field(default_factory=KeyPairPaths)
    node: KeyPairPaths = field(default_factory=KeyPairPaths)
    ca: Optional[str] = None


@dataclass
class RootCliConfig:
    root_base: str = DEFAULT_ROOT_BASE
    subnet_id: Optional[str] = None
    node_id: Optional[str] = None
    keys: RootCliKeys = field(default_factory=RootCliKeys)
    adaos_base: str = DEFAULT_ADAOS_BASE
    adaos_token: str = DEFAULT_ADAOS_TOKEN


def _config_path() -> Path:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / CONFIG_FILENAME


def load_root_cli_config() -> RootCliConfig:
    path = _config_path()
    config = RootCliConfig()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise RootCliError(f"Failed to parse {path}: {exc}") from exc
        config.root_base = data.get("root_base", config.root_base)
        config.subnet_id = data.get("subnet_id") or None
        config.node_id = data.get("node_id") or None
        config.adaos_base = data.get("adaos_base", config.adaos_base)
        config.adaos_token = data.get("adaos_token", config.adaos_token)
        keys_data = data.get("keys") or {}
        config.keys = RootCliKeys(
            hub=_parse_key_pair(keys_data.get("hub")),
            node=_parse_key_pair(keys_data.get("node")),
            ca=keys_data.get("ca") or None,
        )
    else:
        save_root_cli_config(config)
    return config


def save_root_cli_config(config: RootCliConfig) -> None:
    path = _config_path()
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_key_pair(data: Optional[dict]) -> KeyPairPaths:
    if not data:
        return KeyPairPaths()
    return KeyPairPaths(key=data.get("key") or None, cert=data.get("cert") or None)


def _root_url(config: RootCliConfig, suffix: str) -> str:
    base = (config.root_base or DEFAULT_ROOT_BASE).rstrip("/")
    return f"{base}{suffix}"


def _resolve_verify(config: RootCliConfig, *, allow_insecure: bool) -> str | bool:
    if config.keys.ca:
        ca_path = Path(config.keys.ca).expanduser()
        if not ca_path.exists():
            raise RootCliError(f"CA certificate not found at {ca_path}")
        return str(ca_path)
    if allow_insecure:
        return False
    raise RootCliError(
        "CA certificate is not configured; run 'adaos dev hub init' before using mTLS endpoints"
    )


def _mtls_cert_paths(config: RootCliConfig, role: Literal["hub", "node"]) -> tuple[str, str]:
    pair = config.keys.hub if role == "hub" else config.keys.node
    if not pair.cert or not pair.key:
        raise RootCliError(
            "{} certificate is not configured; run 'adaos dev {} init/register' first".format(
                role, "hub" if role == "hub" else "node"
            )
        )
    cert_path = Path(pair.cert).expanduser()
    key_path = Path(pair.key).expanduser()
    if not cert_path.exists():
        raise RootCliError(f"Certificate file not found: {cert_path}")
    if not key_path.exists():
        raise RootCliError(f"Private key file not found: {key_path}")
    return str(cert_path), str(key_path)


def _do_request(
    method: str,
    url: str,
    *,
    json_body: Optional[dict] = None,
    timeout: float = 10.0,
    headers: Optional[dict] = None,
    cert: Optional[tuple[str, str]] = None,
    verify: str | bool | None = None,
) -> httpx.Response:
    try:
        response = httpx.request(
            method,
            url,
            json=json_body,
            headers=headers,
            timeout=timeout,
            cert=cert,
            verify=verify,
        )
    except httpx.RequestError as exc:
        raise RootCliError(f"{method} {url} failed: {exc}") from exc
    if response.status_code >= 400:
        detail = response.text.strip()
        raise RootCliError(
            f"{method} {url} failed with status {response.status_code}: {detail or 'no body'}"
        )
    return response


def _plain_request(
    method: str,
    url: str,
    *,
    config: RootCliConfig,
    json_body: Optional[dict] = None,
    timeout: float = 10.0,
    headers: Optional[dict] = None,
) -> httpx.Response:
    verify = _resolve_verify(config, allow_insecure=True)
    return _do_request(method, url, json_body=json_body, timeout=timeout, headers=headers, verify=verify)


def _mtls_request(
    method: str,
    url: str,
    *,
    config: RootCliConfig,
    role: Literal["hub", "node"],
    json_body: Optional[dict] = None,
    timeout: float = 10.0,
) -> httpx.Response:
    cert = _mtls_cert_paths(config, role)
    verify = _resolve_verify(config, allow_insecure=False)
    return _do_request(method, url, json_body=json_body, timeout=timeout, cert=cert, verify=verify)


def run_preflight_checks(
    config: RootCliConfig,
    *,
    dry_run: bool,
    echo: Callable[[str], None],
) -> None:
    root_health = _root_url(config, "/healthz")
    bridge_health = _root_url(config, "/adaos/healthz")
    policy_url = _root_url(config, "/v1/policy")
    if dry_run:
        echo(f"[dry-run] GET {root_health}")
        echo(
            f"[dry-run] GET {bridge_health} with headers "
            f"X-AdaOS-Base={config.adaos_base or DEFAULT_ADAOS_BASE}, "
            "X-AdaOS-Token=<masked>"
        )
        echo(f"[dry-run] GET {policy_url} using node mTLS credentials")
        return

    _plain_request("GET", root_health, config=config)
    echo(f"Preflight OK: {root_health}")

    _plain_request(
        "GET",
        bridge_health,
        config=config,
        json_body=None,
        timeout=10.0,
        headers={
            "X-AdaOS-Base": config.adaos_base or DEFAULT_ADAOS_BASE,
            "X-AdaOS-Token": config.adaos_token or DEFAULT_ADAOS_TOKEN,
        },
    )
    echo(
        "Preflight OK: {url}".format(
            url=f"{bridge_health}?base={config.adaos_base or DEFAULT_ADAOS_BASE}"
        )
    )

    _mtls_request("GET", policy_url, config=config, role="node", timeout=10.0)
    echo(f"Preflight OK: {policy_url}")


def ensure_registration(
    config: RootCliConfig,
    *,
    dry_run: bool,
    subnet_name: Optional[str],  # unused but kept for API compatibility
    echo: Callable[[str], None],
) -> RootCliConfig:
    if dry_run:
        if not config.subnet_id:
            echo("[dry-run] subnet is not registered (run 'adaos dev hub init --token …')")
        if not config.node_id:
            echo("[dry-run] node is not registered (run 'adaos dev node register')")
        return config

    if not config.subnet_id or not config.keys.hub.cert or not config.keys.hub.key:
        raise RootCliError("Subnet is not registered. Run 'adaos dev hub init --token …'.")
    if not config.node_id or not config.keys.node.cert or not config.keys.node.key:
        raise RootCliError("Node is not registered. Run 'adaos dev node register'.")
    if not config.keys.ca:
        raise RootCliError("CA certificate is missing; re-run 'adaos dev hub init'.")
    return config


def assert_safe_name(name: str) -> None:
    if not name or name.strip() == "":
        raise RootCliError("Name must not be empty")


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


def _draft_url(kind: Literal["skills", "scenarios"]) -> str:
    return f"/v1/{kind}/draft"


def _dry_run_path(config: RootCliConfig, node_id: str, kind: str, name: str) -> str:
    subnet = config.subnet_id or "<subnet-id>"
    return str(Path("subnets") / subnet / "nodes" / node_id / kind / name)


def _push_draft(
    config: RootCliConfig,
    *,
    node_id: Optional[str],
    name: str,
    archive_b64: str,
    sha256: Optional[str],
    dry_run: bool,
    echo: Callable[[str], None],
    kind: Literal["skills", "scenarios"],
) -> str:
    url = _root_url(config, _draft_url(kind))
    target_node = node_id or "<node-id>"
    if dry_run:
        forge_path = _dry_run_path(config, target_node, kind, name)
        echo(
            f"[dry-run] POST {url} payload={{'node_id': '{target_node}', 'name': '{name}', 'archive_b64': '<omitted>'}}"
        )
        return forge_path

    if not node_id:
        raise RootCliError("Node ID is not configured; run registration first")

    payload = {
        "node_id": node_id,
        "name": name,
        "archive_b64": archive_b64,
    }
    if sha256:
        payload["sha256"] = sha256

    response = _mtls_request(
        "POST",
        url,
        config=config,
        role="node",
        json_body=payload,
        timeout=60.0,
    )
    data = response.json()
    stored = data.get("stored_path")
    if not stored:
        raise RootCliError("Root did not return stored_path")
    return stored


def push_skill_draft(
    config: RootCliConfig,
    *,
    node_id: Optional[str],
    name: str,
    archive_b64: str,
    sha256: Optional[str],
    dry_run: bool,
    echo: Callable[[str], None],
) -> str:
    return _push_draft(
        config,
        node_id=node_id,
        name=name,
        archive_b64=archive_b64,
        sha256=sha256,
        dry_run=dry_run,
        echo=echo,
        kind="skills",
    )


def push_scenario_draft(
    config: RootCliConfig,
    *,
    node_id: Optional[str],
    name: str,
    archive_b64: str,
    sha256: Optional[str],
    dry_run: bool,
    echo: Callable[[str], None],
) -> str:
    return _push_draft(
        config,
        node_id=node_id,
        name=name,
        archive_b64=archive_b64,
        sha256=sha256,
        dry_run=dry_run,
        echo=echo,
        kind="scenarios",
    )


def fetch_policy(config: RootCliConfig) -> dict:
    response = _mtls_request("GET", _root_url(config, "/v1/policy"), config=config, role="node")
    return response.json()


def keys_dir() -> Path:
    ctx = get_ctx()
    base_dir = Path(ctx.paths.base_dir())
    target = base_dir / KEYS_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    return target


_KEY_FILENAMES: dict[Literal['hub_key', 'hub_cert', 'node_key', 'node_cert', 'ca_cert'], str] = {
    'hub_key': 'hub.key',
    'hub_cert': 'hub.cert',
    'node_key': 'node.key',
    'node_cert': 'node.cert',
    'ca_cert': 'ca.cert',
}


def key_path(kind: Literal['hub_key', 'hub_cert', 'node_key', 'node_cert', 'ca_cert']) -> Path:
    return keys_dir() / _KEY_FILENAMES[kind]


__all__ = [
    "RootCliConfig",
    "RootCliError",
    "archive_bytes_to_b64",
    "assert_safe_name",
    "create_zip_bytes",
    "ensure_registration",
    "fetch_policy",
    "keys_dir",
    "key_path",
    "load_root_cli_config",
    "push_skill_draft",
    "push_scenario_draft",
    "run_preflight_checks",
    "save_root_cli_config",
]
