# src\adaos\services\root\service.py
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import shutil
import ssl
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping
from fastapi import APIRouter, Depends
import yaml
from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.eventbus import emit

from adaos.services.crypto.pki import generate_rsa_key, make_csr, write_pem, write_private_key
from adaos.services.node_config import (
    NodeConfig,
    RootOwnerProfile,
    displayable_path,
    ensure_hub,
    load_node,
    save_node,
)
from adaos.services.skill.scaffold import create as scaffold_skill_create
from adaos.services.scenario.scaffold import create as scaffold_scenario_create

from .client import RootHttpClient, RootHttpError
from .keyring import KeyringUnavailableError, delete_refresh, load_refresh, save_refresh
from adaos.adapters.db import sqlite as sqlite_db
from adaos.apps.api.auth import require_owner_token
from adaos.services.id_gen import new_id
from adaos.adapters.scenarios.git_repo import GitScenarioRepository
from adaos.services.scenario.manager import ScenarioManager
from adaos.services.skill.manager import SkillManager
from adaos.adapters.db import SqliteScenarioRegistry
from adaos.adapters.db import SqliteSkillRegistry

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


logger = logging.getLogger(__name__)


class RootAuthError(RuntimeError):
    pass


def _get_scenario_manager(ctx: AgentContext = Depends(get_ctx)) -> ScenarioManager:
    repo = ctx.scenarios_repo
    reg = SqliteScenarioRegistry(ctx.sql)
    return ScenarioManager(repo=repo, registry=reg, git=ctx.git, paths=ctx.paths, bus=ctx.bus, caps=ctx.caps)


def _get_skill_manager(ctx: AgentContext = Depends(get_ctx)) -> SkillManager:
    # используем skills_repo, а не scenarios_repo
    repo = ctx.skills_repo
    registry = SqliteSkillRegistry(ctx.sql)
    return SkillManager(
        repo=repo,
        registry=registry,
        git=ctx.git,
        paths=ctx.paths,
        bus=getattr(ctx, "bus", None),
        caps=ctx.caps,
        settings=ctx.settings,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _current_timestamp() -> str:
    return _now().replace(microsecond=0).isoformat()


def _parse_timestamp(value: str | None) -> datetime:
    if not value:
        return _EPOCH
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH


def _parse_expiry(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - defensive fallback
        raise RootAuthError(f"invalid expiry timestamp: {value}") from exc


def _format_expiry(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _bump_version(current: str | None, index: int) -> str:
    parts = [0, 0, 0]
    if current:
        raw_parts = str(current).split(".")
        for idx in range(min(len(raw_parts), 3)):
            token = raw_parts[idx]
            digits = "".join(ch for ch in token if ch.isdigit())
            if digits:
                try:
                    parts[idx] = int(digits)
                except ValueError:
                    parts[idx] = 0
    index = max(0, min(index, 2))
    parts[index] += 1
    for reset in range(index + 1, 3):
        parts[reset] = 0
    return ".".join(str(value) for value in parts)


def _root_state(cfg: NodeConfig) -> dict:
    if cfg.root is None:
        cfg.root = {}
    return cfg.root


@dataclass
class RootAuthService:
    http: RootHttpClient
    clock_skew: timedelta = timedelta(seconds=30)

    @staticmethod
    def resolve_owner(token: str) -> str:
        require_owner_token(token)
        owner_id = os.getenv("ADAOS_ROOT_OWNER_ID") or "local-owner"
        return owner_id

    def login_owner(
        self,
        cfg: NodeConfig,
        *,
        on_start: Callable[[dict], None] | None = None,
    ) -> RootOwnerProfile:
        ensure_hub(cfg)
        owner_id = cfg.subnet_id
        start = self.http.owner_start(owner_id)
        if on_start:
            on_start(start)
        device_code = start.get("device_code")
        interval = max(int(start.get("interval", 5)), 1)
        expires_in = int(start.get("expires_in", 600))
        if not isinstance(device_code, str) or not device_code:
            raise RootAuthError("root did not return device_code")
        deadline = _now() + timedelta(seconds=expires_in)
        while _now() < deadline:
            try:
                result = self.http.owner_poll(device_code)
            except RootHttpError as exc:
                if exc.error_code == "authorization_pending":
                    time.sleep(interval)
                    continue
                if exc.error_code == "slow_down":
                    interval += 5
                    time.sleep(interval)
                    continue
                if exc.error_code in {"expired_token", "expired_device_code"}:
                    raise RootAuthError("device code expired before authorization completed") from exc
                raise
            break
        else:  # pragma: no cover - timing dependent
            raise RootAuthError("device authorization expired")

        if not isinstance(result, dict):
            raise RootAuthError("unexpected response from root")
        access_token = result.get("access_token")
        refresh_token = result.get("refresh_token")
        expires_at_str = result.get("expires_at")
        subject = result.get("subject")
        scopes = result.get("scopes") or []
        owner_returned = result.get("owner_id")
        hub_ids = result.get("hub_ids") or []
        if owner_returned and owner_returned != owner_id:
            raise RootAuthError("root returned mismatched owner_id")
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise RootAuthError("root response is missing tokens")
        if not isinstance(expires_at_str, str):
            raise RootAuthError("root response is missing expires_at")

        if not isinstance(scopes, Iterable):
            scopes = []
        scopes_list = [str(scope) for scope in scopes]
        if not isinstance(hub_ids, Iterable):
            hub_ids = []
        hub_list = [str(h) for h in hub_ids]

        expiry = _parse_expiry(expires_at_str)

        profile: RootOwnerProfile = {
            "owner_id": owner_id,
            "subject": subject if isinstance(subject, str) else None,
            "scopes": scopes_list,
            "access_expires_at": _format_expiry(expiry),
            "hub_ids": hub_list,
        }

        state = _root_state(cfg)
        state["profile"] = profile
        state["access_token_cached"] = access_token
        try:
            save_refresh(owner_id, refresh_token)
            state.pop("refresh_token_fallback", None)
        except KeyringUnavailableError:
            state["refresh_token_fallback"] = refresh_token
        save_node(cfg)
        return profile

    def _refresh_token(self, cfg: NodeConfig) -> tuple[str, datetime]:
        state = _root_state(cfg)
        profile = state.get("profile")
        if not profile:
            raise RootAuthError("root owner profile is not configured; run 'adaos dev root-login'")
        owner_id = profile["owner_id"]
        refresh_token: str | None = None
        try:
            refresh_token = load_refresh(owner_id)
        except KeyringUnavailableError:
            refresh_token = state.get("refresh_token_fallback")
        if not refresh_token:
            raise RootAuthError("refresh token is missing; login required")
        result = self.http.token_refresh(refresh_token)
        access_token = result.get("access_token")
        expires_at = result.get("expires_at")
        if not isinstance(access_token, str) or not isinstance(expires_at, str):
            raise RootAuthError("invalid refresh response from root")
        expiry = _parse_expiry(expires_at)
        state["access_token_cached"] = access_token
        profile["access_expires_at"] = _format_expiry(expiry)
        save_node(cfg)
        return access_token, expiry

    def get_access_token(self, cfg: NodeConfig) -> str:
        ensure_hub(cfg)
        state = _root_state(cfg)
        profile = state.get("profile")
        if not profile:
            raise RootAuthError("root owner profile is not configured; run 'adaos dev root-login'")
        cached = state.get("access_token_cached")
        if isinstance(cached, str) and cached:
            expiry = _parse_expiry(profile["access_expires_at"])
            if expiry - self.clock_skew > _now():
                return cached
        token, expiry = self._refresh_token(cfg)
        profile["access_expires_at"] = _format_expiry(expiry)
        save_node(cfg)
        return token

    def whoami(self, cfg: NodeConfig) -> dict:
        token = self.get_access_token(cfg)
        return self.http.whoami(token)

    def logout(self, cfg: NodeConfig) -> None:
        state = _root_state(cfg)
        profile = state.get("profile")
        if profile:
            owner_id = profile["owner_id"]
            try:
                delete_refresh(owner_id)
            except KeyringUnavailableError:
                pass
        cfg.root = None
        save_node(cfg)

    @staticmethod
    def register_subnet(owner_token: str, csr_pem: str, fingerprint: str, hints: Any | None = None) -> dict:
        owner_id = RootAuthService.resolve_owner(owner_token)
        ca_state = sqlite_db.ca_load()
        ca_key = serialization.load_pem_private_key(ca_state["ca_key_pem"].encode("utf-8"), password=None)
        ca_cert = x509.load_pem_x509_certificate(ca_state["ca_cert_pem"].encode("utf-8"))

        subnet = sqlite_db.subnet_get_or_create(owner_id)
        existing_device = sqlite_db.device_get_by_fingerprint(subnet["subnet_id"], fingerprint)
        now = datetime.now(timezone.utc)

        if existing_device:
            cert_pem = existing_device["cert_pem"]
            issued_at = existing_device["issued_at"]
            expires_at = existing_device["expires_at"]
            device_id = existing_device["device_id"]
        else:
            try:
                csr = x509.load_pem_x509_csr(csr_pem.encode("utf-8"))
            except ValueError as exc:  # pragma: no cover - invalid CSR handling
                raise RootAuthError("invalid CSR") from exc
            if not csr.is_signature_valid:
                raise RootAuthError("CSR signature invalid")

            serial = int(ca_state["next_serial"])
            not_before = now - timedelta(minutes=1)
            not_after = now + timedelta(days=365)
            builder = (
                x509.CertificateBuilder()
                .subject_name(csr.subject)
                .issuer_name(ca_cert.subject)
                .public_key(csr.public_key())
                .serial_number(serial)
                .not_valid_before(not_before)
                .not_valid_after(not_after)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            )
            for extension in csr.extensions:
                builder = builder.add_extension(extension.value, extension.critical)
            certificate = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())
            cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")
            issued_at = int(now.timestamp())
            expires_at = int(not_after.timestamp())
            device = sqlite_db.device_upsert_hub(subnet["subnet_id"], fingerprint, cert_pem, issued_at, expires_at)
            device_id = device["device_id"]
            sqlite_db.ca_update_serial(serial + 1)
        envelope_time = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        event_id = new_id()
        data = {
            "subnet_id": subnet["subnet_id"],
            "hub_device_id": device_id,
            "cert_pem": cert_pem,
        }
        return {"data": data, "event_id": event_id, "server_time_utc": envelope_time}


@dataclass
class OwnerHubsService:
    http: RootHttpClient
    auth: RootAuthService

    def sync(self, cfg: NodeConfig) -> list[str]:
        ensure_hub(cfg)
        token = self.auth.get_access_token(cfg)
        hubs = self.http.owner_hubs_list(token)
        hub_ids = [str(item.get("hub_id")) for item in hubs if isinstance(item, dict) and item.get("hub_id")]
        state = _root_state(cfg)
        profile = state.get("profile")
        if profile:
            profile["hub_ids"] = hub_ids
            save_node(cfg)
        return hub_ids

    def add_current_hub(self, cfg: NodeConfig, hub_id: str | None = None) -> None:
        ensure_hub(cfg)
        token = self.auth.get_access_token(cfg)
        effective_hub_id = hub_id or cfg.node_id
        if not effective_hub_id:
            raise RootAuthError("hub identifier is not configured")
        self.http.owner_hubs_add(token, effective_hub_id)
        state = _root_state(cfg)
        profile = state.get("profile")
        if profile:
            hubs = set(profile.get("hub_ids", []))
            hubs.add(effective_hub_id)
            profile["hub_ids"] = sorted(hubs)
            save_node(cfg)


@dataclass
class PkiService:
    http: RootHttpClient
    auth: RootAuthService

    def enroll(self, cfg: NodeConfig, hub_id: str, csr_pem: str, ttl: str | None = None) -> dict:
        ensure_hub(cfg)
        token = self.auth.get_access_token(cfg)
        return self.http.pki_enroll(token, hub_id, csr_pem, ttl)


# ---------------------------------------------------------------------------
# Developer workflow helpers
# ---------------------------------------------------------------------------


class RootServiceError(RuntimeError):
    """Raised when developer workflow operations fail."""


class TemplateResolutionError(RootServiceError):
    """Raised when a requested scaffold template cannot be resolved."""

    exit_code: int = 2

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class ArtifactNotFoundError(RootServiceError):
    """Raised when a requested artifact is missing from the dev workspace."""

    exit_code: int = 3

    def __init__(self, message: str, *, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


@dataclass(slots=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    interval: int
    expires_in: int


@dataclass(slots=True)
class RootInitResult:
    subnet_id: str
    reused: bool
    hub_key_path: Path
    hub_cert_path: Path
    ca_cert_path: Path | None
    workspace_path: Path


@dataclass(slots=True)
class RootLoginResult:
    owner_id: str
    workspace_path: Path
    subnet_id: str | None = None


@dataclass(slots=True)
class ArtifactCreateResult:
    kind: str
    name: str
    owner_id: str
    path: Path
    version: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class ArtifactPushResult:
    kind: str
    name: str
    stored_path: str
    sha256: str
    bytes_uploaded: int
    version: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class ArtifactDeleteResult:
    kind: str
    name: str
    owner_id: str
    path: Path
    version: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class ArtifactUpdateResult:
    kind: str
    name: str
    source_path: Path
    target_path: Path
    version: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class ArtifactListItem:
    name: str
    path: Path
    version: str | None
    updated_at: str | None


@dataclass(slots=True)
class ArtifactPublishResult:
    kind: str
    name: str
    source_path: Path
    target_path: Path
    version: str
    previous_version: str | None
    updated_at: str
    dry_run: bool = False
    warnings: tuple[str, ...] = ()


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


def assert_safe_name(name: str) -> None:
    if not name or not name.strip():
        raise RootServiceError("Name must not be empty")


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
    if not root.exists():
        raise RootServiceError(f"Path not found: {root}")
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RootServiceError(f"Expected directory at {root}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if _should_skip(relative):
                continue
            if path.is_dir():
                continue
            zf.write(path, arcname=str(relative))
    return buffer.getvalue()


def archive_bytes_to_b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def fingerprint_for_key(key: rsa.RSAPrivateKey) -> str:
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return "sha256:" + hashlib.sha256(public_bytes).hexdigest()


def _config_path_value(path: Path) -> str:
    rendered = displayable_path(path)
    return rendered if rendered is not None else str(path)


def _templates_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "templates"


def _template_path(name: str) -> Path:
    candidate = _templates_dir() / name
    if not candidate.exists():
        raise RootServiceError(f"Template '{name}' not found at {candidate}")
    return candidate


def _copy_template(src: Path, dst: Path) -> None:
    if dst.exists():
        raise RootServiceError(f"Target already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def _ensure_keep_file(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    keep = directory / ".keep"
    if not keep.exists():
        keep.write_text("", encoding="utf-8")


def _load_manifest(manifest_path: Path) -> dict[str, Any]:
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RootServiceError(f"Failed to read manifest at {manifest_path}: {exc}") from exc

    if manifest_path.suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RootServiceError(f"Invalid JSON manifest at {manifest_path}: {exc}") from exc
    else:
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise RootServiceError(f"Invalid YAML manifest at {manifest_path}: {exc}") from exc

    if not isinstance(data, dict):
        return {}
    return data


def _write_manifest(manifest_path: Path, data: Mapping[str, Any]) -> None:
    try:
        if manifest_path.suffix == ".json":
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=4) + "\n",
                encoding="utf-8",
            )
        else:
            manifest_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise RootServiceError(f"Failed to write manifest at {manifest_path}: {exc}") from exc


def _insecure_tls_enabled() -> bool:
    value = os.getenv("ADAOS_INSECURE_TLS", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


class RootDeveloperService:
    """High-level orchestration for Root developer workflows."""

    def __init__(
        self,
        *,
        config_loader: Callable[[], NodeConfig] | None = None,
        config_saver: Callable[[NodeConfig], None] | None = None,
        client_factory: Callable[[NodeConfig], RootHttpClient] | None = None,
    ) -> None:
        self._load_config = config_loader or load_node
        self._save_config = config_saver or save_node
        self._client_factory = client_factory
        self.ctx: AgentContext = get_ctx()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def init(self, *, root_token: str | None = None, metadata: Mapping[str, Any] | None = None) -> RootInitResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.init.start", {"node_id": node_id}, "root.dev")

        try:
            token = root_token or os.getenv("ROOT_TOKEN") or "dev-root-token"
            if not token:
                raise RootServiceError("ROOT_TOKEN is not configured; set ROOT_TOKEN or pass --token")

            existing_subnet = cfg.subnet_settings.id or cfg.subnet_id
            key_path = cfg.hub_key_path()
            cert_path = cfg.hub_cert_path()
            ca_path = cfg.ca_cert_path()

            if existing_subnet and key_path.exists() and cert_path.exists() and ca_path.exists():
                try:
                    cert_pem = cert_path.read_text(encoding="utf-8")
                except OSError:
                    cert_pem = ""
                if cert_pem and self._hub_certificate_is_acceptable(cert_pem, subnet_id=existing_subnet, owner_id=None):
                    workspace = self._prepare_workspace(cfg, owner="pending_owner")
                    cfg.subnet_settings.id = existing_subnet
                    cfg.subnet_id = existing_subnet
                    cfg.subnet_settings.hub.key = _config_path_value(key_path)
                    cfg.subnet_settings.hub.cert = _config_path_value(cert_path)
                    cfg.root_settings.ca_cert = _config_path_value(ca_path)
                    self._save_config(cfg)
                    emit(
                        self.ctx.bus,
                        "root.dev.init.done",
                        {
                            "node_id": node_id,
                            "subnet_id": existing_subnet,
                            "reused": True,
                            "workspace": displayable_path(workspace) or str(workspace),
                        },
                        "root.dev",
                    )
                    return RootInitResult(
                        subnet_id=existing_subnet,
                        reused=True,
                        hub_key_path=key_path,
                        hub_cert_path=cert_path,
                        ca_cert_path=ca_path,
                        workspace_path=workspace,
                    )

            key_path, private_key = self._ensure_hub_keypair(cfg)

            verify = self._plain_verify(cfg)
            client = self._client(cfg)

            previous_subnet = cfg.subnet_id
            registration = self._register_hub(
                client,
                token,
                verify=verify,
                private_key=private_key,
                metadata=metadata,
            )
            reused_flag = bool(registration.get("reused"))
            forge_info = registration.get("forge") if isinstance(registration, Mapping) else None
            forge_repo = forge_info.get("repo") if isinstance(forge_info, Mapping) else None
            forge_path = forge_info.get("path") if isinstance(forge_info, Mapping) else None
            if isinstance(forge_repo, str) and forge_repo:
                cfg.dev_settings.forge_repo = forge_repo
            if isinstance(forge_path, str) and forge_path:
                cfg.dev_settings.forge_path = forge_path

            subnet_id = registration.get("subnet_id")
            cert_pem = registration.get("cert_pem")
            ca_pem = registration.get("ca_pem")
            if not isinstance(subnet_id, str) or not subnet_id:
                raise RootServiceError("Root response missing subnet_id")
            if not isinstance(cert_pem, str) or not cert_pem.strip():
                raise RootServiceError("Root response missing hub certificate")

            cert_path = cfg.hub_cert_path()
            write_pem(cert_path, cert_pem)
            owner_id = cfg.root_settings.owner.owner_id if cfg.root_settings and cfg.root_settings.owner else None
            if not self._hub_certificate_is_acceptable(cert_pem, subnet_id=subnet_id, owner_id=owner_id):
                logger.warning(
                    "Root issued hub certificate without subnet binding; rotating hub credentials",
                )
                key_path, private_key = self._ensure_hub_keypair(cfg, force_new=True)
                rotation = self._register_hub(
                    client,
                    token,
                    verify=verify,
                    private_key=private_key,
                    metadata=metadata,
                    subnet_id=subnet_id,
                )
                rotated_cert = rotation.get("cert_pem")
                if not isinstance(rotated_cert, str) or not rotated_cert.strip():
                    raise RootServiceError("Root response missing hub certificate after rotation")
                write_pem(cert_path, rotated_cert)
                cert_pem = rotated_cert
                ca_candidate = rotation.get("ca_pem")
                if isinstance(ca_candidate, str) and ca_candidate.strip():
                    ca_pem = ca_candidate
                reused_flag = reused_flag or bool(rotation.get("reused"))
                if not self._hub_certificate_is_acceptable(cert_pem, subnet_id=subnet_id, owner_id=None):
                    raise RootServiceError("Root issued hub certificate without subnet binding; contact support")

            ca_path: Path | None = None
            if isinstance(ca_pem, str) and ca_pem.strip():
                ca_path = cfg.ca_cert_path()
                write_pem(ca_path, ca_pem)
                cfg.root_settings.ca_cert = _config_path_value(ca_path)

            cfg.subnet_settings.id = subnet_id
            cfg.subnet_id = subnet_id
            cfg.subnet_settings.hub.key = _config_path_value(key_path)
            cfg.subnet_settings.hub.cert = _config_path_value(cert_path)

            workspace = self._prepare_workspace(cfg, owner="pending_owner")

            self._save_config(cfg)

            reused = reused_flag or previous_subnet == subnet_id
            emit(
                self.ctx.bus,
                "root.dev.init.done",
                {
                    "node_id": node_id,
                    "subnet_id": subnet_id,
                    "reused": reused,
                    "workspace": displayable_path(workspace) or str(workspace),
                },
                "root.dev",
            )
            return RootInitResult(
                subnet_id=subnet_id,
                reused=reused,
                hub_key_path=key_path,
                hub_cert_path=cert_path,
                ca_cert_path=ca_path,
                workspace_path=workspace,
            )
        except Exception:
            emit(self.ctx.bus, "root.dev.init.error", {"node_id": node_id}, "root.dev")
            raise

    def login(
        self,
        *,
        on_authorize: Callable[[DeviceAuthorization], None] | None = None,
    ) -> RootLoginResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.login.start", {"node_id": node_id}, "root.dev")

        try:
            verify_plain = self._plain_verify(cfg)
            client = self._client(cfg)

            authorize_cert: tuple[str, str] | None = None
            authorize_verify: ssl.SSLContext | bool = verify_plain

            mtls_material = self._mtls_material_optional(cfg, verify_plain)
            if mtls_material:
                cert_path, key_path, mtls_verify = mtls_material
                authorize_cert = (cert_path, key_path)
                authorize_verify = mtls_verify
                owner_id_hint = cfg.subnet_id or cfg.subnet_settings.id or "local-owner"
                start = client.device_authorize(
                    verify=authorize_verify,
                    cert=authorize_cert,
                    payload={"owner_id": owner_id_hint},
                )
                try:
                    start = client.device_authorize(verify=authorize_verify, cert=authorize_cert, payload={"owner_id": owner_id_hint})
                except RootHttpError as exc:
                    if self._is_certificate_error(exc):
                        raise RootServiceError(
                            "Root rejected the hub client certificate; run 'adaos dev root init' to rotate credentials",
                        ) from exc
                    authorize_cert = None
                    authorize_verify = verify_plain
                    try:
                        start = client.device_authorize(verify=authorize_verify, payload={"owner_id": owner_id_hint})
                    except RootHttpError as retry_exc:
                        try:
                            fallback = self._maybe_retry_with_mtls(cfg, retry_exc)
                        except RootServiceError as mtls_exc:
                            raise mtls_exc from retry_exc
                        if not fallback:
                            raise RootServiceError(str(retry_exc)) from retry_exc
                        authorize_verify, authorize_cert = fallback
                        try:
                            start = client.device_authorize(verify=authorize_verify, cert=authorize_cert, payload={"owner_id": owner_id_hint})
                        except RootHttpError as second_exc:
                            raise RootServiceError(str(second_exc)) from second_exc
            else:
                owner_id_hint = cfg.subnet_id or cfg.subnet_settings.id or "local-owner"
                try:
                    start = client.device_authorize(verify=authorize_verify, payload={"owner_id": owner_id_hint})
                except RootHttpError as exc:
                    try:
                        fallback = self._maybe_retry_with_mtls(cfg, exc)
                    except RootServiceError as mtls_exc:
                        raise mtls_exc from exc
                    if not fallback:
                        raise RootServiceError(str(exc)) from exc
                    authorize_verify, authorize_cert = fallback
                    try:
                        start = client.device_authorize(verify=authorize_verify, cert=authorize_cert, payload={"owner_id": owner_id_hint})
                    except RootHttpError as retry_exc:
                        raise RootServiceError(str(retry_exc)) from retry_exc

            device_code = start.get("device_code")
            user_code = start.get("user_code") or start.get("user_code_short")
            verification_uri = start.get("verify_uri")
            verification_complete = start.get("verification_uri_complete")
            interval = max(int(start.get("interval", 5)), 1)
            expires_in = int(start.get("expires_in", 600))
            print("_log", device_code, user_code, verification_uri)
            if not isinstance(device_code, str) or not isinstance(user_code, str) or not isinstance(verification_uri, str):
                raise RootServiceError("Root did not return device authorization data")

            auth = DeviceAuthorization(
                device_code=device_code,
                user_code=user_code,
                verification_uri=verification_uri,
                verification_uri_complete=verification_complete if isinstance(verification_complete, str) else None,
                interval=interval,
                expires_in=expires_in,
            )
            if on_authorize:
                on_authorize(auth)

            deadline = time.monotonic() + auth.expires_in
            delay = 0
            while time.monotonic() < deadline:
                if delay:
                    time.sleep(delay)
                try:
                    result = client.device_poll(auth.device_code, verify=authorize_verify, cert=authorize_cert)
                except RootHttpError as exc:
                    code = exc.error_code or ""
                    if code == "authorization_pending":
                        delay = auth.interval
                        continue
                    if code == "slow_down":
                        auth.interval += 5
                        delay = auth.interval
                        continue
                    if code in {"expired_token", "expired_device_code"}:
                        raise RootServiceError("Device authorization expired before completion") from exc
                    raise RootServiceError(str(exc)) from exc
                else:
                    token = result
                    break
            else:
                raise RootServiceError("Device authorization expired before completion")

            owner_id = token.get("owner_id")
            subnet_id = token.get("subnet_id") if isinstance(token, Mapping) else None
            if not isinstance(owner_id, str) or not owner_id:
                raise RootServiceError("Root did not return owner_id")

            cfg.root_settings.owner.owner_id = owner_id
            if isinstance(subnet_id, str) and subnet_id:
                cfg.subnet_settings.id = subnet_id
                cfg.subnet_id = subnet_id

            workspace = self._activate_workspace(cfg, owner_id)
            self._save_config(cfg)
            result = RootLoginResult(owner_id=owner_id, workspace_path=workspace, subnet_id=subnet_id if isinstance(subnet_id, str) else None)
            emit(
                self.ctx.bus,
                "root.dev.login.done",
                {
                    "node_id": node_id,
                    "owner_id": owner_id,
                    "subnet_id": subnet_id,
                    "workspace": displayable_path(workspace) or str(workspace),
                },
                "root.dev",
            )
            return result
        except Exception:
            emit(self.ctx.bus, "root.dev.login.error", {"node_id": node_id}, "root.dev")
            raise

    def create_skill(self, name: str, template: str | None = None) -> ArtifactCreateResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.skill.create.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._create_artifact("skills", name, template=template)
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.skill.create.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        emit(
            self.ctx.bus,
            "root.dev.skill.create.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
            },
            "root.dev",
        )
        return result

    def create_scenario(self, name: str, template: str | None = None) -> ArtifactCreateResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.scenario.create.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._create_artifact("scenarios", name, template=template)
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.scenario.create.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        emit(
            self.ctx.bus,
            "root.dev.scenario.create.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
            },
            "root.dev",
        )
        return result

    def push_skill(self, name: str) -> ArtifactPushResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.skill.push.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._push_artifact("skills", name)
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.skill.push.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        emit(
            self.ctx.bus,
            "root.dev.skill.push.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
                "bytes": result.bytes_uploaded,
            },
            "root.dev",
        )
        return result

    def push_scenario(self, name: str) -> ArtifactPushResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.scenario.push.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._push_artifact("scenarios", name)
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.scenario.push.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        emit(
            self.ctx.bus,
            "root.dev.scenario.push.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
                "bytes": result.bytes_uploaded,
            },
            "root.dev",
        )
        return result

    def update_skill(self, name: str) -> ArtifactUpdateResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.skill.update.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._update_artifact(cfg, "skills", name)
        except ArtifactNotFoundError:
            emit(self.ctx.bus, "root.dev.skill.update.missing", {"name": name, "node_id": node_id}, "root.dev")
            raise
        except Exception:
            emit(self.ctx.bus, "root.dev.skill.update.error", {"name": name, "node_id": node_id}, "root.dev")
            raise
        emit(
            self.ctx.bus,
            "root.dev.skill.update.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
            },
            "root.dev",
        )
        return result

    def update_scenario(self, name: str) -> ArtifactUpdateResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.scenario.update.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._update_artifact(cfg, "scenarios", name)
        except ArtifactNotFoundError:
            emit(self.ctx.bus, "root.dev.scenario.update.missing", {"name": name, "node_id": node_id}, "root.dev")
            raise
        except Exception:
            emit(self.ctx.bus, "root.dev.scenario.update.error", {"name": name, "node_id": node_id}, "root.dev")
            raise
        emit(
            self.ctx.bus,
            "root.dev.scenario.update.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
            },
            "root.dev",
        )
        return result

    def publish_skill(
        self,
        name: str,
        *,
        bump: Literal["major", "minor", "patch"] = "patch",
        force: bool = False,
        dry_run: bool = False,
        signoff: bool = False,
    ) -> ArtifactPublishResult:
        # TODO self.caps.require("core", "skills.manage", "git.write", "net.git")
        # Проверяем именно skills workspace-репо
        root = self.ctx.paths.workspace_dir()
        if not (Path(root) / ".git").exists():
            raise RuntimeError("Skills repo is not initialized. Run `adaos skill sync` once.")
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(
            self.ctx.bus,
            "root.dev.skill.publish.start",
            {"name": name, "node_id": node_id, "bump": bump, "dry_run": dry_run},
            "root.dev",
        )
        try:
            result = self._publish_artifact(
                cfg,
                "skills",
                name,
                bump=bump,
                force=force,
                dry_run=dry_run,
            )
        except ArtifactNotFoundError:
            emit(
                self.ctx.bus,
                "root.dev.skill.publish.missing",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.skill.publish.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        emit(
            self.ctx.bus,
            "root.dev.skill.publish.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "previous_version": result.previous_version,
                "updated_at": result.updated_at,
                "dry_run": result.dry_run,
            },
            "root.dev",
        )
        if not result.dry_run:
            emit(
                self.ctx.bus,
                "registry.skills.published",
                {
                    "name": result.name,
                    "version": result.version,
                    "previous_version": result.previous_version,
                    "updated_at": result.updated_at,
                },
                "root.dev",
            )
        # гарантируем, что подпуть есть в sparse-checkout (на случай узкой sparse-конфигурации)
        try:
            self.ctx.git.sparse_add(str(self.ctx.paths.workspace_dir()), f"skills/{name}")
        except Exception:
            pass
        # Создаём менеджер навыков вручную и пушим подпуть
        mgr = SkillManager(
            repo=self.ctx.skills_repo,
            registry=SqliteSkillRegistry(self.ctx.sql),
            git=self.ctx.git,
            paths=self.ctx.paths,
            bus=getattr(self.ctx, "bus", None),
            caps=self.ctx.caps,
            settings=self.ctx.settings,
        )
        msg = f"publish(skill): {result.name} v{result.version}"
        sha = mgr.push(result.name, msg, signoff=signoff)
        # ничего не мешает вернуть sha в result через setattr/обновлённый датакласс — но это опционально
        return result

    def publish_scenario(
        self,
        name: str,
        *,
        bump: Literal["major", "minor", "patch"] = "patch",
        force: bool = False,
        dry_run: bool = False,
        signoff: bool = False,
    ) -> ArtifactPublishResult:
        root = self.ctx.paths.workspace_dir()
        if not (Path(root) / ".git").exists():
            raise RuntimeError("Skills repo is not initialized. Run `adaos skill sync` once.")
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(
            self.ctx.bus,
            "root.dev.scenario.publish.start",
            {"name": name, "node_id": node_id, "bump": bump, "dry_run": dry_run},
            "root.dev",
        )
        try:
            result = self._publish_artifact(
                cfg,
                "scenarios",
                name,
                bump=bump,
                force=force,
                dry_run=dry_run,
            )
        except ArtifactNotFoundError:
            emit(
                self.ctx.bus,
                "root.dev.scenario.publish.missing",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.scenario.publish.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        emit(
            self.ctx.bus,
            "root.dev.scenario.publish.done",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "previous_version": result.previous_version,
                "updated_at": result.updated_at,
                "dry_run": result.dry_run,
            },
            "root.dev",
        )
        if not result.dry_run:
            emit(
                self.ctx.bus,
                "registry.scenarios.published",
                {
                    "name": result.name,
                    "version": result.version,
                    "previous_version": result.previous_version,
                    "updated_at": result.updated_at,
                },
                "root.dev",
            )
        try:
            self.ctx.git.sparse_add(str(self.ctx.paths.workspace_dir()), f"scenarios/{name}")
        except Exception:
            pass
        # Создаём менеджер навыков вручную и пушим подпуть
        mgr = ScenarioManager(
            repo=self.ctx.scenarios_repo,
            registry=SqliteSkillRegistry(self.ctx.sql),
            git=self.ctx.git,
            paths=self.ctx.paths,
            bus=getattr(self.ctx, "bus", None),
            caps=self.ctx.caps,
        )
        msg = f"publish(scenario): {result.name} v{result.version}"
        sha = mgr.push(result.name, msg, signoff=signoff)
        # ничего не мешает вернуть sha в result через setattr/обновлённый датакласс — но это опционально
        return result

    def delete_skill(self, name: str) -> ArtifactDeleteResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.skill.delete.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._delete_artifact(cfg, "skills", name)
        except ArtifactNotFoundError:
            emit(
                self.ctx.bus,
                "root.dev.skill.delete.missing",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.skill.delete.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        # 1) удаляем драфт на root (он и был у тебя в репозитории)
        draft_audit = self._delete_draft_remote("skills", result.name, node_id=node_id, all_nodes=False)
        # 2) best-effort: удаляем из registry (может вернуться 404 — это ок)
        audit = self._delete_registry_remote("skills", result.name, version=None, all_versions=True, force=False)
        emit(
            self.ctx.bus,
            "dev.skills.deleted",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
                "draft_deleted": (draft_audit or {}).get("deleted", []),
                "registry_deleted_versions": (audit or {}).get("deleted", []),
                "registry_audit_id": (audit or {}).get("audit_id"),
            },
            "root.dev",
        )
        return result

    def delete_scenario(self, name: str) -> ArtifactDeleteResult:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        emit(self.ctx.bus, "root.dev.scenario.delete.start", {"name": name, "node_id": node_id}, "root.dev")
        try:
            result = self._delete_artifact(cfg, "scenarios", name)
        except ArtifactNotFoundError:
            emit(
                self.ctx.bus,
                "root.dev.scenario.delete.missing",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        except Exception:
            emit(
                self.ctx.bus,
                "root.dev.scenario.delete.error",
                {"name": name, "node_id": node_id},
                "root.dev",
            )
            raise
        draft_audit = self._delete_draft_remote("scenarios", result.name, node_id=node_id, all_nodes=False)
        audit = self._delete_registry_remote("scenarios", result.name, version=None, all_versions=True, force=False)
        emit(
            self.ctx.bus,
            "dev.scenarios.deleted",
            {
                "name": result.name,
                "node_id": node_id,
                "version": result.version,
                "updated_at": result.updated_at,
                "draft_deleted": (draft_audit or {}).get("deleted", []),
                "registry_deleted_versions": (audit or {}).get("deleted", []),
                "registry_audit_id": (audit or {}).get("audit_id"),
            },
            "root.dev",
        )
        return result

    def list_skills(self) -> list[ArtifactListItem]:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        items = self._list_artifacts(cfg, "skills")
        self._save_config(cfg)
        emit(
            self.ctx.bus,
            "root.dev.skill.list.done",
            {"node_id": node_id, "count": len(items)},
            "root.dev",
        )
        return items

    def list_scenarios(self) -> list[ArtifactListItem]:
        cfg = self._load_config()
        node_id = cfg.node_settings.id or cfg.node_id
        items = self._list_artifacts(cfg, "scenarios")
        self._save_config(cfg)
        emit(
            self.ctx.bus,
            "root.dev.scenario.list.done",
            {"node_id": node_id, "count": len(items)},
            "root.dev",
        )
        return items

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _client(self, cfg: NodeConfig) -> RootHttpClient:
        if self._client_factory:
            return self._client_factory(cfg)
        base_url = cfg.root_settings.base_url or "https://api.inimatic.com"
        return RootHttpClient(base_url=base_url)

    def _plain_verify(self, cfg: NodeConfig) -> ssl.SSLContext | bool:
        ca_setting = cfg.root_settings.ca_cert
        ca_path = cfg.ca_cert_path()
        if ca_setting:
            if ca_path.exists():
                return self._load_verify_context(ca_path)
            default_indicator = self.ctx.paths.base_dir() / "keys" / "ca.cert"
            try:
                configured = Path(str(ca_setting)).expanduser()
            except Exception:  # pragma: no cover - defensive
                configured = ca_path
            # TODO Привести к единому источнику домашнего каталога self.ctx.paths.base_dir()
            """ if configured != default_indicator:
                display = displayable_path(ca_path) or str(ca_path)
                raise RootServiceError(f"CA certificate not found at {display}") """
        if _insecure_tls_enabled():
            return False
        return True

    def _register_hub(
        self,
        client: RootHttpClient,
        token: str,
        *,
        verify: ssl.SSLContext | bool,
        private_key: rsa.RSAPrivateKey,
        metadata: Mapping[str, Any] | None = None,
        subnet_id: str | None = None,
    ) -> Mapping[str, Any]:
        fingerprint = fingerprint_for_key(private_key)
        meta_payload: dict[str, Any] = {"fingerprint": fingerprint}
        if metadata:
            meta_payload.update(metadata)
        bootstrap = client.request_bootstrap_token(token, meta=meta_payload, verify=verify)
        bootstrap_token = bootstrap.get("one_time_token") or bootstrap.get("token")
        if not isinstance(bootstrap_token, str) or not bootstrap_token:
            raise RootServiceError("Root did not return bootstrap token")
        csr_common_name = self._hub_common_name(subnet_id)
        csr_pem = make_csr(csr_common_name, None, private_key).replace("\r\n", "\n").strip() + "\n"
        return client.register_subnet(
            csr_pem,
            bootstrap_token=bootstrap_token,
            verify=verify,
        )

    def _maybe_retry_with_mtls(
        self,
        cfg: NodeConfig,
        exc: RootHttpError,
    ) -> tuple[ssl.SSLContext, tuple[str, str]] | None:
        if not self._should_retry_with_mtls(exc):
            return None
        try:
            cert_path, key_path, verify = self._mtls_material(cfg)
        except RootServiceError as exc:
            raise RootServiceError(f"{exc} (required for client certificate authentication)") from exc
        return verify, (cert_path, key_path)

    def _mtls_material_optional(
        self,
        cfg: NodeConfig,
        verify_hint: ssl.SSLContext | bool,
    ) -> tuple[str, str, ssl.SSLContext | bool] | None:
        cert_path = cfg.hub_cert_path()
        key_path = cfg.hub_key_path()
        if not cert_path.exists() or not key_path.exists():
            return None
        subnet_id = cfg.subnet_id or cfg.subnet_settings.id
        if subnet_id:
            try:
                cert_pem = cert_path.read_text(encoding="utf-8")
            except OSError:
                return None

            # Если уже известен owner_id — проверим и O, но мягко (только если O присутствует)
            owner_id = cfg.root_settings.owner.owner_id if cfg.root_settings and cfg.root_settings.owner else None
            if not self._hub_certificate_is_acceptable(cert_pem, subnet_id=subnet_id, owner_id=owner_id):
                logger.debug("Skipping hub mTLS credentials due to subject mismatch (CN/O) on certificate")
                return None

        ca_path = cfg.ca_cert_path()
        if ca_path.exists():
            verify = self._load_verify_context(ca_path)
        else:
            verify = verify_hint
        return str(cert_path), str(key_path), verify

    @staticmethod
    def _should_retry_with_mtls(exc: RootHttpError) -> bool:
        if exc.error_code in {
            "invalid_client_certificate",
            "client_certificate_required",
            "client_certificate_missing",
        }:
            return True
        if exc.status_code in {400, 401, 403} and "certificate" in str(exc).lower():
            return True
        return False

    @staticmethod
    def _is_certificate_error(exc: RootHttpError) -> bool:
        if exc.error_code in {
            "invalid_client_certificate",
            "client_certificate_required",
            "client_certificate_missing",
        }:
            return True
        return "certificate" in str(exc).lower()

    def _mtls_material(self, cfg: NodeConfig) -> tuple[str, str, ssl.SSLContext]:
        ca_path = cfg.ca_cert_path()
        cert_path = cfg.hub_cert_path()
        key_path = cfg.hub_key_path()
        for label, path in ("CA certificate", ca_path), ("hub certificate", cert_path), ("hub private key", key_path):
            if not path.exists():
                raise RootServiceError(f"{label} not found at {path}; run 'adaos dev root init' first")
        verify = self._load_verify_context(ca_path)
        return str(cert_path), str(key_path), verify

    @staticmethod
    def _load_verify_context(ca_path: Path) -> ssl.SSLContext:
        try:
            context = ssl.create_default_context()
        except ssl.SSLError as exc:  # pragma: no cover - unexpected SSL configuration issues
            raise RootServiceError(f"Failed to create TLS context: {exc}") from exc
        try:
            context.load_verify_locations(cafile=str(ca_path))
        except (FileNotFoundError, ssl.SSLError) as exc:
            raise RootServiceError(f"Failed to load CA certificate from {ca_path}: {exc}") from exc
        return context

    def _ensure_hub_keypair(
        self,
        cfg: NodeConfig,
        *,
        force_new: bool = False,
    ) -> tuple[Path, rsa.RSAPrivateKey]:
        key_path = cfg.hub_key_path()
        cert_path = cfg.hub_cert_path()

        if force_new:
            try:
                key_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                cert_path.unlink(missing_ok=True)
            except OSError:
                pass
        elif self._hub_certificate_requires_rotation(cert_path):
            try:
                key_path.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                cert_path.unlink(missing_ok=True)
            except OSError:
                pass

        key_path.parent.mkdir(parents=True, exist_ok=True)
        if key_path.exists():
            try:
                private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            except ValueError as exc:  # pragma: no cover - corrupted key
                raise RootServiceError(f"Invalid hub private key at {key_path}") from exc
        else:
            private_key = generate_rsa_key()
            write_private_key(key_path, private_key)
        cfg.subnet_settings.hub.key = _config_path_value(key_path)
        return key_path, private_key

    @staticmethod
    def _hub_certificate_requires_rotation(cert_path: Path) -> bool:
        if not cert_path.exists():
            return False
        try:
            x509.load_pem_x509_certificate(cert_path.read_bytes())
        except ValueError:
            return True
        return False

    @staticmethod
    def _hub_common_name(subnet_id: str | None) -> str:
        if subnet_id:
            return f"subnet:{subnet_id}"
        return "adaos-hub"

    @staticmethod
    def _hub_certificate_org(cert_pem: str) -> str | None:
        try:
            pem_clean = cert_pem.replace("\r\n", "\n").encode("utf-8")
            cert = x509.load_pem_x509_certificate(pem_clean)
        except Exception:
            return None
        try:
            attrs = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
            return attrs[0].value if attrs else None
        except Exception:
            return None

    @staticmethod
    def _hub_certificate_is_acceptable(
        cert_pem: str,
        *,
        subnet_id: str,
        owner_id: str | None = None,
    ) -> bool:
        """
        Допустимые варианты:
        - legacy: CN='subnet:<subnet_id>'
        - новый:  CN='hub:<hub_id>' (дополнительно, если указан owner_id, и присутствует O, то O должно быть 'owner:<owner_id>')
        """
        cn = RootDeveloperService._hub_certificate_common_name(cert_pem)
        if not cn:
            return False

        # Legacy-режим: CN=subnet:<subnet_id>
        if cn == f"subnet:{subnet_id}":
            return True

        # Новый формат: CN=hub:<hub_id>
        if cn.startswith("hub:"):
            if owner_id:
                org = RootDeveloperService._hub_certificate_org(cert_pem) or ""
                # Если поле O присутствует — оно должно совпадать. Если поля O нет, не заваливаем проверку.
                if org and org != f"owner:{owner_id}":
                    return False
            return True

        return False

    @staticmethod
    def _hub_certificate_common_name(cert_pem: str) -> str | None:
        try:
            # normalize line endings just in case
            pem_clean = cert_pem.replace("\r\n", "\n").encode("utf-8")
            cert = x509.load_pem_x509_certificate(pem_clean)
        except Exception as e:
            return None

        try:
            cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            if not cn_attrs:
                return None
            return cn_attrs[0].value
        except Exception as e:
            return None

    def _workspace_root(self, cfg: NodeConfig) -> Path:
        hub_id = cfg.subnet_id or "pending_hub"
        return (self.ctx.paths.base_dir() / "dev" / hub_id).resolve()

    def _prepare_workspace(self, cfg: NodeConfig, *, owner: str) -> Path:
        workspace_root = self._workspace_root(cfg)
        workspace_root.mkdir(parents=True, exist_ok=True)
        for sub in ("skills", "scenarios", "uploads"):
            _ensure_keep_file(workspace_root / sub)
        cfg.dev_settings.workspace = _config_path_value(workspace_root)
        return workspace_root

    def _activate_workspace(self, cfg: NodeConfig, owner_id: str) -> Path:
        return self._prepare_workspace(cfg, owner=owner_id)

    def _owner_workspace(self, cfg: NodeConfig) -> tuple[str, Path]:
        owner = cfg.owner_id or "pending_owner"
        path = self._prepare_workspace(cfg, owner=owner)
        self._save_config(cfg)
        return owner, path

    def _create_artifact(
        self,
        kind: Literal["skills", "scenarios"],
        name: str,
        *,
        template: str | None,
    ) -> ArtifactCreateResult:
        assert_safe_name(name)
        cfg = self._load_config()
        owner, workspace = self._owner_workspace(cfg)
        target = workspace / kind / name

        template_path, prototype_value = self._resolve_template(kind, template)
        _copy_template(template_path, target)
        manifest_meta = self._update_manifest(
            kind,
            target,
            name,
            prototype_value,
            version_bump_index=1,
            set_prototype=True,
        )

        return ArtifactCreateResult(
            kind=kind.rstrip("s"),
            name=name,
            owner_id=owner,
            path=target,
            version=(manifest_meta or {}).get("version"),
            updated_at=(manifest_meta or {}).get("updated_at"),
        )

    def _list_artifacts(self, cfg: NodeConfig, kind: Literal["skills", "scenarios"]) -> list[ArtifactListItem]:
        owner = cfg.owner_id or "pending_owner"
        workspace = self._prepare_workspace(cfg, owner=owner)
        artifacts_dir = workspace / kind
        items: list[ArtifactListItem] = []
        if artifacts_dir.exists():
            for entry in artifacts_dir.iterdir():
                if not entry.is_dir():
                    continue
                name, version, updated_at = self._artifact_manifest_info(entry, kind)
                items.append(
                    ArtifactListItem(
                        name=name,
                        path=entry,
                        version=version,
                        updated_at=updated_at,
                    )
                )
        items.sort(key=lambda item: (_parse_timestamp(item.updated_at), item.name.lower()), reverse=True)
        return items

    def _delete_artifact(
        self,
        cfg: NodeConfig,
        kind: Literal["skills", "scenarios"],
        name: str,
    ) -> ArtifactDeleteResult:
        owner = cfg.owner_id or "pending_owner"
        workspace = self._prepare_workspace(cfg, owner=owner)
        target = workspace / kind / name
        if not target.exists() or not target.is_dir():
            raise ArtifactNotFoundError(f"{kind[:-1].capitalize()} '{name}' not found at {target}")

        manifest_name, version, updated_at = self._artifact_manifest_info(target, kind)
        shutil.rmtree(target)

        result = ArtifactDeleteResult(
            kind=kind.rstrip("s"),
            name=manifest_name,
            owner_id=owner,
            path=target,
            version=version,
            updated_at=updated_at,
        )
        self._save_config(cfg)
        return result

    def _delete_registry_remote(
        self,
        kind: Literal["skills", "scenarios"],
        name: str,
        *,
        version: str | None,
        all_versions: bool,
        force: bool,
    ) -> dict | None:
        cfg = self._load_config()
        owner_id = cfg.owner_id
        if not owner_id:
            raise RootServiceError("Owner is not configured; run 'adaos dev root login' first")
        cert_path, key_path, verify = self._mtls_material_for_role(cfg, "hub")
        client = self._client(cfg)
        try:
            if kind == "skills":
                resp = client.delete_skill_registry(
                    name=name,
                    version=version,
                    all_versions=all_versions,
                    force=force,
                    verify=verify,
                    cert=(cert_path, key_path),
                )
            else:
                resp = client.delete_scenario_registry(
                    name=name,
                    version=version,
                    all_versions=all_versions,
                    force=force,
                    verify=verify,
                    cert=(cert_path, key_path),
                )
        except RootHttpError as exc:
            # 404 — артефакт не публиковался в registry: это не ошибка для delete
            if getattr(exc, "status_code", None) == 404:
                return None
            # 409 — используется: подскажи юзеру про --force (пока не реализуем)
            if getattr(exc, "status_code", None) == 409:
                raise RootServiceError(f"Cannot delete {kind[:-1]} '{name}' from registry: artifact is in use") from exc
            raise
        except Exception as exc:
            raise RootServiceError(f"Failed to delete {kind[:-1]} '{name}' from registry on root") from exc
        return resp or {}

    def _delete_draft_remote(
        self,
        kind: Literal["skills", "scenarios"],
        name: str,
        *,
        node_id: str | None,
        all_nodes: bool,
    ) -> dict | None:
        cfg = self._load_config()
        if not cfg.owner_id:
            raise RootServiceError("Owner is not configured; run 'adaos dev root login' first")
        cert_path, key_path, verify = self._mtls_material_for_role(cfg, "hub")
        client = self._client(cfg)
        try:
            if kind == "skills":
                resp = client.delete_skill_draft(
                    name=name,
                    node_id=node_id,
                    all_nodes=all_nodes,
                    verify=verify,
                    cert=(cert_path, key_path),
                )
            else:
                resp = client.delete_scenario_draft(
                    name=name,
                    node_id=node_id,
                    all_nodes=all_nodes,
                    verify=verify,
                    cert=(cert_path, key_path),
                )
        except RootHttpError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            raise
        except Exception as exc:
            raise RootServiceError(f"Failed to delete {kind[:-1]} draft '{name}' on root") from exc
        return resp or {}

    def _manifest_payload(
        self,
        directory: Path,
        kind: Literal["skills", "scenarios"],
    ) -> tuple[Path, dict[str, Any]] | None:
        for candidate in self._manifest_candidates(kind):
            manifest_path = directory / candidate
            if not manifest_path.exists():
                continue
            data = _load_manifest(manifest_path)
            return manifest_path, data
        return None

    def _manifest_warnings(
        self,
        source: dict[str, Any] | None,
        target: dict[str, Any] | None,
    ) -> list[str]:
        if not source or not target:
            return []
        ignore = {"version", "updated_at"}
        keys = sorted(set(source.keys()) | set(target.keys()))
        warnings: list[str] = []
        for key in keys:
            if key in ignore:
                continue
            if key not in source:
                warnings.append(f"Registry metadata contains '{key}' absent in dev copy")
                continue
            if key not in target:
                warnings.append(f"Dev metadata contains new field '{key}' not present in registry")
                continue
            if source[key] != target[key]:
                warnings.append(f"Field '{key}' differs between dev and registry metadata")
        return warnings

    def _workspace_templates_dir(self, kind: Literal["skills", "scenarios"]) -> Path:
        if kind == "scenarios":
            return self.ctx.paths.scenarios_workspace_dir()
        return self.ctx.paths.skills_workspace_dir()

    def _builtin_templates_dir(self, kind: Literal["skills", "scenarios"]) -> Path:
        if kind == "scenarios":
            return self.ctx.paths.scenario_templates_dir()
        return self.ctx.paths.skill_templates_dir()

    def _default_template_name(self, kind: Literal["skills", "scenarios"]) -> str:
        return "scenario_default" if kind == "scenarios" else "skill_default"

    def _collect_templates(self, directory: Path) -> list[str]:
        if not directory.exists():
            return []
        return sorted(entry.name for entry in directory.iterdir() if entry.is_dir())

    def _resolve_template(
        self,
        kind: Literal["skills", "scenarios"],
        template: str | None,
    ) -> tuple[Path, str]:
        workspace_dir = self._workspace_templates_dir(kind)
        builtin_dir = self._builtin_templates_dir(kind)
        default_name = self._default_template_name(kind)

        if isinstance(template, str):
            template = template.strip() or None

        template_name = template or default_name

        search_candidates: list[Path] = []
        if template:
            search_candidates.extend(
                [
                    workspace_dir / template_name,
                    builtin_dir / template_name,
                ]
            )
        else:
            search_candidates.append(builtin_dir / template_name)

        for candidate in search_candidates:
            if candidate.exists():
                prototype_value = template if template else "default"
                return candidate, prototype_value

        available_user = self._collect_templates(workspace_dir)
        available_builtin = self._collect_templates(builtin_dir)

        limit = 20
        lines = [f"Template '{template_name}' not found for {kind[:-1]}."]
        lines.append("Available templates (use --template <name>):")

        remaining = limit
        if available_user:
            lines.append("  Workspace templates:")
            for name in available_user[:remaining]:
                lines.append(f"    - {name}")
            remaining -= min(len(available_user), remaining)
        if available_builtin and remaining > 0:
            lines.append("  Built-in templates:")
            for name in available_builtin[:remaining]:
                lines.append(f"    - {name}")
            remaining -= min(len(available_builtin), remaining)
        if not available_user and not available_builtin:
            lines.append("  (no templates available)")
        elif remaining == 0 and (len(available_user) + len(available_builtin)) > limit:
            lines.append("  …")

        lines.append("Specify a template explicitly with --template <name>.")

        raise TemplateResolutionError("\n".join(lines))

    def _update_manifest(
        self,
        kind: Literal["skills", "scenarios"],
        target: Path,
        name: str,
        prototype: str | None,
        *,
        version_bump_index: int | None,
        set_prototype: bool,
        explicit_version: str | None = None,
    ) -> dict[str, str] | None:
        for candidate in self._manifest_candidates(kind):
            manifest_path = target / candidate
            if not manifest_path.exists():
                continue
            data = _load_manifest(manifest_path)
            data["name"] = name
            if set_prototype and prototype is not None:
                data["prototype"] = prototype

            existing_version = data.get("version") if isinstance(data.get("version"), str) else None
            if explicit_version is not None:
                data["version"] = explicit_version
            elif version_bump_index is not None:
                data["version"] = _bump_version(existing_version, version_bump_index)

            timestamp = _current_timestamp()
            data["updated_at"] = timestamp

            _write_manifest(manifest_path, data)
            return {
                "version": data.get("version") if isinstance(data.get("version"), str) else None,
                "updated_at": timestamp,
            }
        return None

    def _manifest_candidates(self, kind: Literal["skills", "scenarios"]) -> list[str]:
        if kind == "skills":
            return ["skill.yaml"]
        return ["scenario.yaml", "scenario.yml", "scenario.json"]

    def _artifact_manifest_info(
        self,
        entry: Path,
        kind: Literal["skills", "scenarios"],
    ) -> tuple[str, str | None, str | None]:
        for candidate in self._manifest_candidates(kind):
            manifest_path = entry / candidate
            if not manifest_path.exists():
                continue
            data = _load_manifest(manifest_path)
            name_raw = data.get("name")
            name = name_raw.strip() if isinstance(name_raw, str) and name_raw.strip() else entry.name
            version_raw = data.get("version")
            version = version_raw if isinstance(version_raw, str) and version_raw else None
            updated_raw = data.get("updated_at")
            updated_at = updated_raw if isinstance(updated_raw, str) and updated_raw else None
            return name, version, updated_at
        return entry.name, None, None

    def _mtls_material_for_role(self, cfg: NodeConfig, role: Literal["hub", "node"]) -> tuple[str, str, ssl.SSLContext]:
        ca_path = cfg.ca_cert_path()
        if role == "hub":
            cert_path = cfg.hub_cert_path()
            key_path = cfg.hub_key_path()
            role_name = "hub"
        else:
            cert_path = cfg.node_cert_path()
            key_path = cfg.node_key_path()
            role_name = "node"

        for label, path in (f"{role_name} certificate", cert_path), (f"{role_name} private key", key_path), ("CA certificate", ca_path):
            if not path.exists():
                raise RootServiceError(f"{label} not found at {path}; run 'adaos dev node register' (for node) or '... root init' (for hub) first")

        verify = self._load_verify_context(ca_path)
        return str(cert_path), str(key_path), verify

    def _push_artifact(self, kind: Literal["skills", "scenarios"], name: str) -> ArtifactPushResult:
        cfg = self._load_config()

        owner_id = cfg.owner_id
        if not owner_id:
            raise RootServiceError("Owner is not configured; run 'adaos dev root login' first")
        _, workspace = self._owner_workspace(cfg)
        source = workspace / kind / name
        if not source.exists():
            raise RootServiceError(f"{kind[:-1].capitalize()} '{name}' not found at {source}")
        manifest_meta = self._update_manifest(
            kind,
            source,
            name,
            None,
            version_bump_index=2,
            set_prototype=False,
        )
        archive_bytes = create_zip_bytes(source)
        archive_b64 = archive_bytes_to_b64(archive_bytes)
        digest = hashlib.sha256(archive_bytes).hexdigest()
        cert_path, key_path, verify = self._mtls_material_for_role(cfg, "hub")
        client = self._client(cfg)
        node_id = cfg.node_settings.id or cfg.node_id
        if kind == "skills":
            response = client.push_skill_draft(
                name=name,
                archive_b64=archive_b64,
                node_id=node_id,
                verify=verify,
                cert=(cert_path, key_path),
                sha256=digest,
            )
        else:
            response = client.push_scenario_draft(
                name=name,
                archive_b64=archive_b64,
                node_id=node_id,
                verify=verify,
                cert=(cert_path, key_path),
                sha256=digest,
            )
        stored = response.get("stored_path")
        if not isinstance(stored, str) or not stored:
            raise RootServiceError("Root did not return stored_path")
        return ArtifactPushResult(
            kind=kind.rstrip("s"),
            name=name,
            stored_path=stored,
            sha256=digest,
            bytes_uploaded=len(archive_bytes),
            version=(manifest_meta or {}).get("version"),
            updated_at=(manifest_meta or {}).get("updated_at"),
        )

    def _update_artifact(self, cfg: NodeConfig, kind: Literal["skills", "scenarios"], name: str) -> ArtifactUpdateResult:
        assert_safe_name(name)
        forge_repo = getattr(cfg.dev_settings, "forge_repo", None)
        forge_path = getattr(cfg.dev_settings, "forge_path", None)
        if not forge_repo or not forge_path:
            raise RootServiceError("Forge repository is not configured; re-run 'adaos dev root init' with a Root that provides forge repo info")

        repo_dir = Path(self.ctx.paths.dev_dir()) / ".forge"
        repo_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.ctx.git.ensure_repo(str(repo_dir), forge_repo)
            self.ctx.git.pull(str(repo_dir))
        except Exception as exc:  # pragma: no cover - git failures depend on environment
            raise RootServiceError(f"Failed to update forge repository at {repo_dir}: {exc}") from exc

        node_id = cfg.node_settings.id or cfg.node_id
        relative = Path(forge_path) / "nodes" / node_id / kind / name
        source = repo_dir / relative
        if not source.exists():
            raise ArtifactNotFoundError(f"{kind[:-1].capitalize()} '{name}' not found in forge repository (expected at {relative})")

        owner = cfg.owner_id or "pending_owner"
        workspace = self._prepare_workspace(cfg, owner=owner)
        target = workspace / kind / name

        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)

        manifest_name, version, updated_at = self._artifact_manifest_info(target, kind)
        return ArtifactUpdateResult(
            kind=kind.rstrip("s"),
            name=manifest_name,
            source_path=source,
            target_path=target,
            version=version,
            updated_at=updated_at,
        )

    def _publish_artifact(
        self,
        cfg: NodeConfig,
        kind: Literal["skills", "scenarios"],
        name: str,
        *,
        bump: Literal["major", "minor", "patch"],
        force: bool,
        dry_run: bool,
    ) -> ArtifactPublishResult:
        bump_map = {"major": 0, "minor": 1, "patch": 2}
        if bump not in bump_map:
            raise RootServiceError(f"Unsupported version bump '{bump}'")
        bump_index = bump_map[bump]

        workspace_root = self._workspace_root(cfg)
        source = workspace_root / kind / name
        if not source.exists() or not source.is_dir():
            raise ArtifactNotFoundError(f"{kind[:-1].capitalize()} '{name}' not found at {source}")

        target = (self.ctx.paths.scenarios_dir() if kind == "scenarios" else self.ctx.paths.skills_dir()) / name

        source_payload = self._manifest_payload(source, kind)
        source_data = source_payload[1] if source_payload else {}
        target_payload = self._manifest_payload(target, kind) if target.exists() else None
        target_data = target_payload[1] if target_payload else {}
        warnings = self._manifest_warnings(source_data, target_data)

        previous_version: str | None = None
        if target.exists():
            _, previous_version, _ = self._artifact_manifest_info(target, kind)

        new_version = _bump_version(previous_version, bump_index)

        if warnings and not force and not dry_run:
            warning_text = "; ".join(warnings)
            raise RootServiceError(
                "; ".join(
                    [
                        "Manifest metadata differences detected",
                        warning_text,
                        "Re-run with --force to publish anyway.",
                    ]
                )
            )

        if dry_run:
            timestamp = _current_timestamp()
            return ArtifactPublishResult(
                kind=kind.rstrip("s"),
                name=name,
                source_path=source,
                target_path=target,
                version=new_version,
                previous_version=previous_version,
                updated_at=timestamp,
                dry_run=True,
                warnings=tuple(warnings),
            )

        backup: Path | None = None
        if target.exists():
            backup = target.parent / f".{target.name}.publish-backup"
            if backup.exists():
                shutil.rmtree(backup)
            target.rename(backup)

        scaffold = scaffold_skill_create if kind == "skills" else scaffold_scenario_create
        created = False
        manifest_meta: dict[str, str] | None = None
        try:
            scaffold(name, template=str(source), version=new_version, register=True, push=False)
            created = True
            manifest_meta = self._update_manifest(
                kind,
                target,
                name,
                None,
                version_bump_index=None,
                set_prototype=False,
                explicit_version=new_version,
            )
        except Exception:
            if created and target.exists():
                try:
                    shutil.rmtree(target)
                except OSError:
                    pass
            if backup and backup.exists():
                try:
                    backup.rename(target)
                except OSError:
                    pass
            raise
        else:
            if backup and backup.exists():
                shutil.rmtree(backup)

        updated_at = (manifest_meta or {}).get("updated_at") or _current_timestamp()

        return ArtifactPublishResult(
            kind=kind.rstrip("s"),
            name=name,
            source_path=source,
            target_path=target,
            version=new_version,
            previous_version=previous_version,
            updated_at=updated_at,
            dry_run=False,
            warnings=tuple(warnings),
        )


__all__ = [
    "RootAuthService",
    "OwnerHubsService",
    "PkiService",
    "RootAuthError",
    "RootDeveloperService",
    "RootServiceError",
    "TemplateResolutionError",
    "ArtifactNotFoundError",
    "DeviceAuthorization",
    "RootInitResult",
    "RootLoginResult",
    "ArtifactCreateResult",
    "ArtifactPushResult",
    "ArtifactDeleteResult",
    "ArtifactListItem",
    "ArtifactPublishResult",
    "ArtifactUpdateResult",
    "assert_safe_name",
    "create_zip_bytes",
    "archive_bytes_to_b64",
    "fingerprint_for_key",
]
