from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict
import os
import sys
import uuid
import yaml
from adaos.services.agent_context import get_ctx, AgentContext  # type: ignore


def _base_dir(ctx: AgentContext | None = None) -> Path:
    return get_ctx().paths.base_dir()


def _config_path(ctx: AgentContext | None = None) -> Path:
    p = get_ctx().paths.base_dir() / "node.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class RootOwnerProfile(TypedDict):
    owner_id: str
    subject: str | None
    scopes: list[str]
    access_expires_at: str
    hub_ids: list[str]


class RootState(TypedDict, total=False):
    profile: RootOwnerProfile
    access_token_cached: str | None
    refresh_token_fallback: str | None


@dataclass
class RootOwnerSettings:
    owner_id: str | None = None


@dataclass
class RootSettings:
    base_url: str = "https://api.inimatic.com"
    # Store relative/default-friendly path; resolve via _expand_path
    ca_cert: str | None = "keys/ca.cert"
    owner: RootOwnerSettings = field(default_factory=RootOwnerSettings)


@dataclass
class HubKeypair:
    # Store relative/default-friendly paths; resolve via _expand_path
    key: str | None = "keys/hub_private.pem"
    cert: str | None = "keys/hub_cert.pem"


@dataclass
class SubnetSettings:
    id: str | None = None
    hub: HubKeypair = field(default_factory=HubKeypair)


@dataclass
class NodeSettings:
    id: str | None = None


@dataclass
class DevSettings:
    # Store relative path under base_dir; resolve via _expand_path
    workspace: str = "dev"
    forge_repo: str | None = None
    forge_path: str | None = None


@dataclass
class NodeConfig:
    # TODO refactor to node.id
    node_id: str
    # TODO refactor to subnet.id
    subnet_id: str
    role: str
    hub_url: str | None = None
    token: str | None = None
    root_state: RootState | None = None
    root_settings: RootSettings = field(default_factory=RootSettings)
    subnet_settings: SubnetSettings = field(default_factory=SubnetSettings)
    node_settings: NodeSettings = field(default_factory=NodeSettings)
    dev_settings: DevSettings = field(default_factory=DevSettings)

    @property
    def root(self) -> RootState | None:
        return self.root_state

    @root.setter
    def root(self, value: RootState | None) -> None:
        self.root_state = value

    @property
    def node_id_value(self) -> str:
        return self.node_settings.id or self.node_id

    @property
    def subnet_id_value(self) -> str:
        return self.subnet_settings.id or self.subnet_id

    @property
    def owner_id(self) -> str | None:
        return self.root_settings.owner.owner_id

    def ensure_defaults(self) -> bool:
        changed = False
        if not self.role:
            self.role = "hub"
            changed = True
        if not self.token:
            self.token = os.environ.get("ADAOS_TOKEN", "dev-root-token")
            changed = True
        if not self.node_id:
            self.node_id = str(uuid.uuid4())
            changed = True
        if not self.subnet_id:
            self.subnet_id = str(uuid.uuid4())
            changed = True
        if not self.node_settings.id:
            self.node_settings.id = self.node_id
            changed = True
        if not self.subnet_settings.id:
            self.subnet_settings.id = self.subnet_id
            changed = True
        if not self.root_settings.base_url:
            self.root_settings.base_url = "https://api.inimatic.com"
            changed = True
        if not self.root_settings.ca_cert:
            self.root_settings.ca_cert = "keys/ca.cert"
            changed = True
        if not self.dev_settings.workspace:
            self.dev_settings.workspace = "dev"
            changed = True
        return changed

    def sync_sections(self) -> None:
        if self.node_id:
            if self.node_settings.id != self.node_id:
                self.node_settings.id = self.node_id
        elif self.node_settings.id:
            self.node_id = self.node_settings.id
        if self.subnet_id:
            if self.subnet_settings.id != self.subnet_id:
                self.subnet_settings.id = self.subnet_id
        elif self.subnet_settings.id:
            self.subnet_id = self.subnet_settings.id

    def to_dict(self) -> dict[str, Any]:
        data = {
            "node_id": self.node_id,
            "subnet_id": self.subnet_id,
            "role": self.role,
            "hub_url": self.hub_url,
            "token": self.token,
            "root_state": self.root_state,
            "root": _settings_to_dict(self.root_settings),
            "subnet": _settings_to_dict(self.subnet_settings),
            "node": _settings_to_dict(self.node_settings),
            "dev": _settings_to_dict(self.dev_settings),
        }
        return data

    def hub_key_path(self) -> Path:
        return _expand_path(self.subnet_settings.hub.key, "keys/hub_private.pem")

    def hub_cert_path(self) -> Path:
        return _expand_path(self.subnet_settings.hub.cert, "keys/hub_cert.pem")

    def ca_cert_path(self) -> Path:
        return _expand_path(self.root_settings.ca_cert, "keys/ca.cert")

    def workspace_path(self) -> Path:
        return _expand_path(self.dev_settings.workspace, "dev")

    def owner_workspace(self) -> Path | None:
        owner = self.owner_id
        if not owner:
            return None
        return self.workspace_path() / owner


def _expand_path(value: str | None, fallback: str) -> Path:
    target = value or fallback
    candidate = Path(str(target))
    base = _base_dir()

    if candidate.is_absolute():
        return candidate

    text = str(candidate).replace("\\", "/")
    if text.startswith("~"):
        # Backward-compatibility: map legacy ~/.adaos paths into current base_dir
        if text.startswith("~/.adaos"):
            suffix = text.split("~/.adaos", 1)[1].lstrip("/\\")
            if suffix:
                return base / Path(suffix)
            return base
        return candidate.expanduser()

    return base / candidate


def _stringify_path(value: str | None) -> str | None:
    if not value:
        return None
    candidate = Path(str(value)).expanduser()
    try:
        resolved = candidate.resolve(strict=False)  # type: ignore[arg-type]
    except TypeError:  # pragma: no cover - compatibility with Python <3.12
        resolved = candidate

    base = _base_dir()
    try:
        base_resolved = base.resolve(strict=False)
    except TypeError:  # pragma: no cover - compatibility with Python <3.12
        base_resolved = base

    try:
        relative_base = resolved.relative_to(base_resolved)
    except ValueError:
        pass
    else:
        # Always stringify under the active base_dir; avoid emitting legacy ~/.adaos
        if not relative_base.parts:
            return str(base_resolved)
        return str(base_resolved / relative_base)

    home = Path.home().resolve()
    try:
        relative_home = resolved.relative_to(home)
    except ValueError:
        return str(candidate)
    if not relative_home.parts:
        return "~"
    return str(Path("~") / relative_home)


def displayable_path(value: Path | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Path):
        return _stringify_path(str(value))
    return _stringify_path(value)


def _settings_to_dict(settings: Any) -> dict[str, Any]:
    data = asdict(settings)
    if isinstance(settings, RootSettings):
        data["base_url"] = data.get("base_url") or "https://api.inimatic.com"
        data["ca_cert"] = _stringify_path(data.get("ca_cert"))
        owner = data.get("owner") or {}
        owner_id = owner.get("owner_id") if isinstance(owner, dict) else None
        data["owner"] = {"owner_id": owner_id} if owner_id else {}
    if isinstance(settings, SubnetSettings):
        hub = data.get("hub") or {}
        if isinstance(hub, dict):
            key_path = _stringify_path(hub.get("key"))
            cert_path = _stringify_path(hub.get("cert"))
            if key_path:
                hub["key"] = key_path
            else:
                hub.pop("key", None)
            if cert_path:
                hub["cert"] = cert_path
            else:
                hub.pop("cert", None)
            data["hub"] = hub
    if isinstance(settings, DevSettings):
        data["workspace"] = _stringify_path(data.get("workspace"))
        forge_repo = data.get("forge_repo")
        data["forge_repo"] = forge_repo if forge_repo else None
        forge_path = data.get("forge_path")
        data["forge_path"] = str(forge_path) if forge_path else None
    return data


def _settings_from_dict(settings_cls: type, payload: Any):
    payload = payload or {}
    if settings_cls is RootSettings:
        base_url = payload.get("base_url") if isinstance(payload, dict) else None
        ca_cert = payload.get("ca_cert") if isinstance(payload, dict) else None
        owner_raw = payload.get("owner") if isinstance(payload, dict) else None
        owner_id = owner_raw.get("owner_id") if isinstance(owner_raw, dict) else None
        return RootSettings(
            base_url=base_url or "https://api.inimatic.com",
            ca_cert=ca_cert or "keys/ca.cert",
            owner=RootOwnerSettings(owner_id=owner_id),
        )
    if settings_cls is SubnetSettings:
        hub_raw = payload.get("hub") if isinstance(payload, dict) else None
        hub_key = hub_raw.get("key") if isinstance(hub_raw, dict) else None
        hub_cert = hub_raw.get("cert") if isinstance(hub_raw, dict) else None
        return SubnetSettings(
            id=payload.get("id") if isinstance(payload, dict) else None,
            hub=HubKeypair(key=hub_key, cert=hub_cert),
        )
    if settings_cls is NodeSettings:
        return NodeSettings(id=payload.get("id") if isinstance(payload, dict) else None)
    if settings_cls is DevSettings:
        workspace = payload.get("workspace") if isinstance(payload, dict) else None
        forge_repo = payload.get("forge_repo") if isinstance(payload, dict) else None
        forge_path = payload.get("forge_path") if isinstance(payload, dict) else None
        return DevSettings(workspace=workspace or "dev", forge_repo=forge_repo, forge_path=forge_path)
    raise TypeError(f"Unsupported settings class: {settings_cls!r}")


def _default_conf() -> NodeConfig:
    conf = NodeConfig(
        node_id=str(uuid.uuid4()),
        subnet_id=str(uuid.uuid4()),
        role="hub",
        hub_url=None,
        token=os.environ.get("ADAOS_TOKEN", "dev-local-token"),
        root_state=None,
    )
    conf.ensure_defaults()
    return conf


def _looks_like_root_state(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    return any(key in raw for key in ("profile", "access_token_cached", "refresh_token_fallback"))


def _normalize_root_state(raw: Any) -> RootState | None:
    if not isinstance(raw, dict):
        return None
    profile_data = raw.get("profile")
    profile: RootOwnerProfile | None = None
    if isinstance(profile_data, dict):
        owner_id = profile_data.get("owner_id")
        if isinstance(owner_id, str) and owner_id:
            subject = profile_data.get("subject")
            scopes = profile_data.get("scopes")
            expires_at = profile_data.get("access_expires_at")
            hub_ids = profile_data.get("hub_ids")
            if not isinstance(scopes, list):
                scopes = []
            else:
                scopes = [s for s in scopes if isinstance(s, str)]
            if not isinstance(hub_ids, list):
                hub_ids = []
            else:
                hub_ids = [h for h in hub_ids if isinstance(h, str)]
            if isinstance(expires_at, str):
                try:
                    datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except ValueError:
                    expires_at = datetime.now(timezone.utc).isoformat()
            else:
                expires_at = datetime.now(timezone.utc).isoformat()
            profile = RootOwnerProfile(
                owner_id=owner_id,
                subject=subject if isinstance(subject, str) else None,
                scopes=scopes,
                access_expires_at=expires_at,
                hub_ids=hub_ids,
            )
    state: RootState = {}
    if profile:
        state["profile"] = profile
    access_cached = raw.get("access_token_cached")
    if isinstance(access_cached, str) and access_cached:
        state["access_token_cached"] = access_cached
    refresh_fallback = raw.get("refresh_token_fallback")
    if isinstance(refresh_fallback, str) and refresh_fallback:
        state["refresh_token_fallback"] = refresh_fallback
    return state or None


def load_node(ctx: AgentContext | None = None) -> NodeConfig:
    path = _config_path()
    if not path.exists():
        conf = _default_conf()
        save_node(conf, ctx=ctx)
        return conf
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    raw_root_settings = data.get("root")
    raw_root_state = data.get("root_state")
    if raw_root_state is None and _looks_like_root_state(raw_root_settings):
        raw_root_state = raw_root_settings
        raw_root_settings = {}

    root_settings = _settings_from_dict(RootSettings, raw_root_settings)
    subnet_settings = _settings_from_dict(SubnetSettings, data.get("subnet"))
    node_settings = _settings_from_dict(NodeSettings, data.get("node"))
    dev_settings = _settings_from_dict(DevSettings, data.get("dev"))

    node_id = data.get("node_id") or node_settings.id or str(uuid.uuid4())
    node_settings.id = node_id
    subnet_id = data.get("subnet_id") or subnet_settings.id or str(uuid.uuid4())
    subnet_settings.id = subnet_id
    role = (data.get("role") or "hub").strip().lower()
    hub_url = data.get("hub_url")
    token = data.get("token") or os.environ.get("ADAOS_TOKEN", "dev-local-token")
    root_state = _normalize_root_state(raw_root_state)

    conf = NodeConfig(
        node_id=node_id,
        subnet_id=subnet_id,
        role=role,
        hub_url=hub_url,
        token=token,
        root_state=root_state,
        root_settings=root_settings,
        subnet_settings=subnet_settings,
        node_settings=node_settings,
        dev_settings=dev_settings,
    )
    changed = conf.ensure_defaults()
    conf.sync_sections()
    if changed:
        save_node(conf, ctx=ctx)
    return conf


def save_node(conf: NodeConfig, *, ctx: AgentContext | None = None) -> None:
    conf.sync_sections()
    data = conf.to_dict()
    _config_path(ctx).write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def ensure_hub(conf: NodeConfig) -> None:
    if conf.role.strip().lower() != "hub":
        raise ValueError("root features are available only for nodes with role 'hub'")


def node_base_dir(ctx: AgentContext | None = None) -> Path:
    return _base_dir(ctx)


def set_role(role: str, *, hub_url: str | None = None, subnet_id: str | None = None, ctx: AgentContext | None = None) -> NodeConfig:
    role = role.lower().strip()
    if role not in ("hub", "member"):
        raise ValueError("role must be 'hub' or 'member'")
    conf = load_config(ctx=ctx)
    conf.role = role
    if subnet_id:
        conf.subnet_id = subnet_id
    conf.hub_url = hub_url if role == "member" else None
    save_config(conf, ctx=ctx)
    return conf


def load_config(ctx: AgentContext | None = None) -> NodeConfig:
    return load_node(ctx=ctx)


def save_config(conf: NodeConfig, *, ctx: AgentContext | None = None) -> None:
    save_node(conf, ctx=ctx)
