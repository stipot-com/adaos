from __future__ import annotations

import base64
import hashlib
import io
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

from adaos.services.crypto.pki import generate_rsa_key, make_csr, write_pem, write_private_key
from adaos.services.node_config import NodeConfig, RootOwnerProfile, ensure_hub, load_node, save_node

from .client import RootHttpClient, RootHttpError
from .keyring import KeyringUnavailableError, delete_refresh, load_refresh, save_refresh
from adaos.adapters.db import sqlite as sqlite_db
from adaos.apps.api.auth import require_owner_token
from adaos.services.id_gen import new_id

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


logger = logging.getLogger(__name__)



class RootAuthError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:  # pragma: no cover - defensive fallback
        raise RootAuthError(f"invalid expiry timestamp: {value}") from exc


def _format_expiry(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


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


@dataclass(slots=True)
class ArtifactPushResult:
    kind: str
    name: str
    stored_path: str
    sha256: str
    bytes_uploaded: int


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


def _home_relative(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        return str(path)
    return str(Path("~") / rel)


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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def init(self, *, root_token: str | None = None, metadata: Mapping[str, Any] | None = None) -> RootInitResult:
        cfg = self._load_config()
        token = root_token or os.getenv("ROOT_TOKEN") or os.getenv("ADAOS_ROOT_TOKEN")
        if not token:
            raise RootServiceError("ROOT_TOKEN is not configured; set ROOT_TOKEN or pass --token")

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

        subnet_id = registration.get("subnet_id")
        cert_pem = registration.get("cert_pem")
        ca_pem = registration.get("ca_pem")
        if not isinstance(subnet_id, str) or not subnet_id:
            raise RootServiceError("Root response missing subnet_id")
        if not isinstance(cert_pem, str) or not cert_pem.strip():
            raise RootServiceError("Root response missing hub certificate")

        cert_path = cfg.hub_cert_path()
        write_pem(cert_path, cert_pem)

        if not self._hub_certificate_matches_subnet(cert_pem, subnet_id):
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
            if not self._hub_certificate_matches_subnet(cert_pem, subnet_id):
                raise RootServiceError(
                    "Root issued hub certificate without subnet binding; contact support",
                )

        ca_path: Path | None = None
        if isinstance(ca_pem, str) and ca_pem.strip():
            ca_path = cfg.ca_cert_path()
            write_pem(ca_path, ca_pem)
            cfg.root_settings.ca_cert = _home_relative(ca_path)

        cfg.subnet_settings.id = subnet_id
        cfg.subnet_id = subnet_id
        cfg.subnet_settings.hub.key = _home_relative(key_path)
        cfg.subnet_settings.hub.cert = _home_relative(cert_path)

        self._save_config(cfg)

        workspace = self._prepare_workspace(cfg, owner="pending_owner")

        reused = reused_flag or previous_subnet == subnet_id
        return RootInitResult(
            subnet_id=subnet_id,
            reused=reused,
            hub_key_path=key_path,
            hub_cert_path=cert_path,
            ca_cert_path=ca_path,
            workspace_path=workspace,
        )

    def login(
        self,
        *,
        on_authorize: Callable[[DeviceAuthorization], None] | None = None,
    ) -> RootLoginResult:
        cfg = self._load_config()
        verify_plain = self._plain_verify(cfg)
        client = self._client(cfg)

        authorize_cert: tuple[str, str] | None = None
        authorize_verify: ssl.SSLContext | bool = verify_plain

        mtls_material = self._mtls_material_optional(cfg, verify_plain)
        if mtls_material:
            cert_path, key_path, mtls_verify = mtls_material
            authorize_cert = (cert_path, key_path)
            authorize_verify = mtls_verify
            try:
                start = client.device_authorize(verify=authorize_verify, cert=authorize_cert)
            except RootHttpError as exc:
                if self._is_certificate_error(exc):
                    raise RootServiceError(
                        "Root rejected the hub client certificate; run 'adaos dev root init' to rotate credentials",
                    ) from exc
                authorize_cert = None
                authorize_verify = verify_plain
                try:
                    start = client.device_authorize(verify=authorize_verify)
                except RootHttpError as retry_exc:
                    try:
                        fallback = self._maybe_retry_with_mtls(cfg, retry_exc)
                    except RootServiceError as mtls_exc:
                        raise mtls_exc from retry_exc
                    if not fallback:
                        raise RootServiceError(str(retry_exc)) from retry_exc
                    authorize_verify, authorize_cert = fallback
                    try:
                        start = client.device_authorize(verify=authorize_verify, cert=authorize_cert)
                    except RootHttpError as second_exc:
                        raise RootServiceError(str(second_exc)) from second_exc
        else:
            try:
                start = client.device_authorize(verify=authorize_verify)
            except RootHttpError as exc:
                try:
                    fallback = self._maybe_retry_with_mtls(cfg, exc)
                except RootServiceError as mtls_exc:
                    raise mtls_exc from exc
                if not fallback:
                    raise RootServiceError(str(exc)) from exc
                authorize_verify, authorize_cert = fallback
                try:
                    start = client.device_authorize(verify=authorize_verify, cert=authorize_cert)
                except RootHttpError as retry_exc:
                    raise RootServiceError(str(retry_exc)) from retry_exc
        device_code = start.get("device_code")
        user_code = start.get("user_code") or start.get("user_code_short")
        verification_uri = start.get("verification_uri") or start.get("verification_uri_complete")
        verification_complete = start.get("verification_uri_complete")
        interval = max(int(start.get("interval", 5)), 1)
        expires_in = int(start.get("expires_in", 600))
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
        return RootLoginResult(owner_id=owner_id, workspace_path=workspace, subnet_id=subnet_id if isinstance(subnet_id, str) else None)

    def create_skill(self, name: str) -> ArtifactCreateResult:
        return self._create_artifact("skills", name, template="skill_default")

    def create_scenario(self, name: str) -> ArtifactCreateResult:
        return self._create_artifact("scenarios", name, template="scenario_default")

    def push_skill(self, name: str) -> ArtifactPushResult:
        return self._push_artifact("skills", name)

    def push_scenario(self, name: str) -> ArtifactPushResult:
        return self._push_artifact("scenarios", name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _client(self, cfg: NodeConfig) -> RootHttpClient:
        if self._client_factory:
            return self._client_factory(cfg)
        base_url = cfg.root_settings.base_url or "https://api.inimatic.com"
        return RootHttpClient(base_url=base_url)

    def _plain_verify(self, cfg: NodeConfig) -> ssl.SSLContext | bool:
        ca = cfg.root_settings.ca_cert
        if ca:
            path = Path(ca).expanduser()
            default_ca_path = Path("~/.adaos/keys/ca.cert").expanduser()
            if path.exists():
                return self._load_verify_context(path)
            if path != default_ca_path:
                raise RootServiceError(f"CA certificate not found at {path}")
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
            raise RootServiceError(
                f"{exc} (required for client certificate authentication)"
            ) from exc
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
            if not self._hub_certificate_matches_subnet(cert_pem, subnet_id):
                logger.debug(
                    "Skipping hub mTLS credentials due to missing subnet binding on certificate",
                )
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
        cfg.subnet_settings.hub.key = _home_relative(key_path)
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
    def _hub_certificate_matches_subnet(cert_pem: str, subnet_id: str) -> bool:
        common_name = RootDeveloperService._hub_certificate_common_name(cert_pem)
        if not common_name:
            return False
        return common_name == f"subnet:{subnet_id}"

    @staticmethod
    def _hub_certificate_common_name(cert_pem: str) -> str | None:
        try:
            cert = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
        except ValueError:
            return None
        try:
            common_names = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        except Exception:  # pragma: no cover - defensive
            return None
        if not common_names:
            return None
        value = common_names[0].value
        if not isinstance(value, str):
            return None
        return value

    def _prepare_workspace(self, cfg: NodeConfig, *, owner: str) -> Path:
        workspace_root = cfg.workspace_path()
        workspace_root.mkdir(parents=True, exist_ok=True)
        target = workspace_root / owner
        for sub in ("skills", "scenarios", "uploads"):
            _ensure_keep_file(target / sub)
        return target

    def _activate_workspace(self, cfg: NodeConfig, owner_id: str) -> Path:
        workspace_root = cfg.workspace_path()
        pending = workspace_root / "pending_owner"
        target = workspace_root / owner_id
        if pending.exists():
            if target.exists():
                for child in pending.iterdir():
                    destination = target / child.name
                    if destination.exists():
                        continue
                    child.rename(destination)
                try:
                    pending.rmdir()
                except OSError:
                    pass
            else:
                pending.rename(target)
        target.mkdir(parents=True, exist_ok=True)
        for sub in ("skills", "scenarios", "uploads"):
            _ensure_keep_file(target / sub)
        return target

    def _owner_workspace(self, cfg: NodeConfig) -> tuple[str, Path]:
        owner = cfg.owner_id or "pending_owner"
        path = cfg.workspace_path() / owner
        if not path.exists():
            path = self._prepare_workspace(cfg, owner=owner)
        else:
            for sub in ("skills", "scenarios", "uploads"):
                _ensure_keep_file(path / sub)
        return owner, path

    def _create_artifact(self, kind: Literal["skills", "scenarios"], name: str, *, template: str) -> ArtifactCreateResult:
        assert_safe_name(name)
        cfg = self._load_config()
        owner, workspace = self._owner_workspace(cfg)
        target = workspace / kind / name
        template_path = _template_path(template)
        _copy_template(template_path, target)
        return ArtifactCreateResult(kind=kind.rstrip("s"), name=name, owner_id=owner, path=target)

    def _push_artifact(self, kind: Literal["skills", "scenarios"], name: str) -> ArtifactPushResult:
        cfg = self._load_config()
        owner_id = cfg.owner_id
        if not owner_id:
            raise RootServiceError("Owner is not configured; run 'adaos dev root login' first")
        _, workspace = self._owner_workspace(cfg)
        source = workspace / kind / name
        if not source.exists():
            raise RootServiceError(f"{kind[:-1].capitalize()} '{name}' not found at {source}")
        archive_bytes = create_zip_bytes(source)
        archive_b64 = archive_bytes_to_b64(archive_bytes)
        digest = hashlib.sha256(archive_bytes).hexdigest()
        cert_path, key_path, verify = self._mtls_material(cfg)
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
        )


__all__ = [
    "RootAuthService",
    "OwnerHubsService",
    "PkiService",
    "RootAuthError",
    "RootDeveloperService",
    "RootServiceError",
    "DeviceAuthorization",
    "RootInitResult",
    "RootLoginResult",
    "ArtifactCreateResult",
    "ArtifactPushResult",
    "assert_safe_name",
    "create_zip_bytes",
    "archive_bytes_to_b64",
    "fingerprint_for_key",
]
