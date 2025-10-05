"""Hub agent helpers that integrate with the persistent root backend."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, MutableMapping, Sequence

import os
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from adaos.services.root import DeviceRole, RootAuthorityBackend, Scope

__all__ = ["HubAgent", "HubChannel", "HubKeystore", "AliasResolver"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class HubKeystore:
    """File-system backed storage for the hub key material."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)
        self._key_path = self._base / "hub.key"
        self._cert_path = self._base / "hub.crt"
        self._chain_path = self._base / "root.chain"
        self._node_path = self._base / "node.id"

    def has_material(self) -> bool:
        return (
            self._key_path.exists()
            and self._cert_path.exists()
            and self._chain_path.exists()
            and self._node_path.exists()
        )

    def load(self) -> Mapping[str, str]:
        return {
            "private_key": self._key_path.read_text(encoding="utf-8"),
            "certificate": self._cert_path.read_text(encoding="utf-8"),
            "chain": self._chain_path.read_text(encoding="utf-8"),
            "node_id": self._node_path.read_text(encoding="utf-8").strip(),
        }

    def save(self, *, private_key_pem: str, certificate_pem: str, chain_pem: str, node_id: str) -> None:
        self._key_path.write_text(private_key_pem, encoding="utf-8")
        os.chmod(self._key_path, 0o600)
        self._cert_path.write_text(certificate_pem, encoding="utf-8")
        self._chain_path.write_text(chain_pem, encoding="utf-8")
        self._node_path.write_text(node_id, encoding="utf-8")


class AliasResolver:
    """Maintains a cache of alias→device_id mappings for a subnet."""

    _PATTERN = re.compile(r"[^a-z0-9]+")

    def __init__(self) -> None:
        self._aliases: MutableMapping[str, str] = {}

    @staticmethod
    def _slug(value: str) -> str:
        slug = AliasResolver._PATTERN.sub("-", value.strip().lower()).strip("-")
        return slug

    def update(self, devices: Iterable[Mapping[str, object]]) -> None:
        aliases: MutableMapping[str, str] = {}
        for device in devices:
            device_id = str(device.get("device_id"))
            for alias in device.get("aliases", []) or []:
                slug = self._slug(str(alias))
                if slug:
                    aliases[slug] = device_id
        self._aliases = aliases

    def resolve(self, alias: str) -> str | None:
        slug = self._slug(alias)
        return self._aliases.get(slug)

    def snapshot(self) -> Mapping[str, str]:
        return dict(self._aliases)


@dataclass(slots=True)
class HubChannel:
    node_id: str
    token: str
    expires_at: datetime
    issued_at: datetime

    def is_valid(self, *, margin: timedelta | None = None) -> bool:
        margin = margin or timedelta(seconds=0)
        return self.expires_at - margin > _now()

    def rotation_ratio(self, *, now: datetime | None = None) -> float:
        now = now or _now()
        total = self.expires_at - self.issued_at
        if total.total_seconds() <= 0:
            return 1.0
        elapsed = now - self.issued_at
        return max(0.0, min(1.0, elapsed / total))


@dataclass
class HubAgent:
    """Implements bootstrap, alias resolution and channel rotation for hubs."""

    backend: RootAuthorityBackend
    storage_dir: Path
    subnet_id: str
    keystore: HubKeystore | None = None
    clock: Callable[[], datetime] = _now
    _channel: HubChannel | None = field(default=None, init=False)
    _resolver: AliasResolver = field(default_factory=AliasResolver, init=False)
    _denylist: set[str] = field(default_factory=set, init=False)
    _node_id: str | None = field(default=None, init=False)
    _scopes: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if self.keystore is None:
            self.keystore = HubKeystore(self.storage_dir)
        if self.keystore.has_material():
            material = self.keystore.load()
            self._node_id = material["node_id"]
            self._scopes = self._extract_scopes(material["certificate"])

    @staticmethod
    def _extract_scopes(certificate_pem: str) -> set[str]:
        try:
            cert = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        except ValueError:
            return set()
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        except x509.ExtensionNotFound:
            return set()
        scopes: set[str] = set()
        for uri in san:
            if isinstance(uri, x509.UniformResourceIdentifier) and uri.value.startswith("urn:adaos:scopes="):
                scope_list = uri.value.split("=", 1)[1]
                scopes.update(filter(None, (item.strip() for item in scope_list.split(","))))
        return scopes

    # ------------------------------------------------------------------
    # Bootstrap & certificate lifecycle
    # ------------------------------------------------------------------
    def bootstrap(
        self,
        *,
        scopes: Sequence[Scope],
        display_name: str,
        approve: Callable[[str], None],
        idempotency_key: str,
    ) -> Mapping[str, str]:
        if self.keystore and self.keystore.has_material():
            return self.keystore.load()

        private_key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(
                x509.Name(
                    [
                        x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, display_name),
                    ]
                )
            )
            .sign(private_key, hashes.SHA256())
        )
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("ascii")
        submission = self.backend.submit_csr(
            csr_pem=csr_pem,
            role=DeviceRole.HUB,
            subnet_id=self.subnet_id,
            scopes=scopes,
            idempotency_key=idempotency_key,
        )
        consent_id = submission["consent_id"]
        node_id = submission["node_id"]
        approve(consent_id)
        bundle = self.backend.collect_certificate(consent_id=consent_id)
        cert_pem = bundle["cert"]
        chain_pem = bundle["chain"]
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode("ascii")
        assert self.keystore is not None
        self.keystore.save(
            private_key_pem=private_pem,
            certificate_pem=cert_pem,
            chain_pem=chain_pem,
            node_id=node_id,
        )
        self._node_id = node_id
        self._scopes = self._extract_scopes(cert_pem)
        return self.keystore.load()

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------
    def connect(self) -> HubChannel:
        material = self.keystore.load() if self.keystore else None
        if material is None:
            raise RuntimeError("hub keystore not initialised")
        node_id = material["node_id"]
        self._node_id = node_id
        response = self.backend.open_hub_channel(node_id=node_id, certificate_pem=material["certificate"])
        expires_at = datetime.fromisoformat(response["expires_at"])
        channel = HubChannel(
            node_id=node_id,
            token=response["channel_token"],
            expires_at=expires_at,
            issued_at=self.clock(),
        )
        self._channel = channel
        return channel

    def ensure_channel(self, *, margin_seconds: int = 30) -> HubChannel:
        if self._channel is None or not self._channel.is_valid(margin=timedelta(seconds=margin_seconds)):
            if self._node_id is None:
                raise RuntimeError("hub node identifier unavailable")
            response = self.backend.rotate_hub_channel(node_id=self._node_id)
            expires_at = datetime.fromisoformat(response["expires_at"])
            self._channel = HubChannel(
                node_id=self._node_id,
                token=response["channel_token"],
                expires_at=expires_at,
                issued_at=self.clock(),
            )
        elif self._channel.rotation_ratio(now=self.clock()) >= 0.8:
            response = self.backend.rotate_hub_channel(node_id=self._channel.node_id)
            expires_at = datetime.fromisoformat(response["expires_at"])
            self._channel = HubChannel(
                node_id=self._channel.node_id,
                token=response["channel_token"],
                expires_at=expires_at,
                issued_at=self.clock(),
            )
        return self._channel

    def verify_channel(self) -> Mapping[str, str]:
        if self._channel is None:
            raise RuntimeError("channel not initialised")
        return self.backend.verify_hub_channel(node_id=self._channel.node_id, token=self._channel.token)

    # ------------------------------------------------------------------
    # Alias & denylist helpers
    # ------------------------------------------------------------------
    def refresh_directory(self, *, actor_device_id: str, role: DeviceRole | None = None) -> Mapping[str, str]:
        response = self.backend.list_devices(actor_device_id=actor_device_id, role=role)
        devices = response.get("devices", [])
        self._resolver.update(devices)
        return self._resolver.snapshot()

    def resolve_alias(self, alias: str) -> str | None:
        return self._resolver.resolve(alias)

    def refresh_denylist(self) -> set[str]:
        entries = self.backend.fetch_denylist(subnet_id=self.subnet_id)["entries"]
        self._denylist = {entry["entity_id"] for entry in entries if entry["active"]}
        return set(self._denylist)

    def send_skill_payload(self, *, target_device_id: str, required_scopes: Iterable[Scope]) -> None:
        if target_device_id in self._denylist:
            raise PermissionError("target device is denylisted")
        missing = [scope.value for scope in required_scopes if scope.value not in self._scopes]
        if missing:
            raise PermissionError(f"missing scopes: {', '.join(missing)}")

