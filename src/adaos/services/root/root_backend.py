"""Persistent, security-hardened backend for AdaOS root authentication flows."""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import hashlib
import hmac
import re
import threading

import yaml
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID, ObjectIdentifier

from .enums import ConsentStatus, ConsentType, DeviceRole, Scope
from .ids import (
    DeviceId,
    NodeId,
    SubnetId,
    generate_device_id,
    generate_event_id,
    generate_node_id,
    generate_trace_id,
)
from .persistence.sqlite import SQLitePersistence
from .schemas import AuditExport, ErrorEnvelope, ResponseEnvelope, TokenResponse

__all__ = ["ClientContext", "RootAuthorityBackend", "RootBackendError"]


class RootBackendError(RuntimeError):
    """Exception raised when the backend emits a structured error."""

    def __init__(self, envelope: ErrorEnvelope, *, status_code: int = 400) -> None:
        super().__init__(envelope.message)
        self.envelope = envelope
        self.status_code = status_code


@dataclass(slots=True)
class ClientContext:
    """Metadata captured from the caller to prevent relay attacks."""

    origin: str
    client_ip: str
    user_agent: str


class RootCertificateAuthority:
    """Represents the offline AdaOS root certificate authority."""

    def __init__(self, *, private_key: ec.EllipticCurvePrivateKey, certificate: x509.Certificate) -> None:
        self._key = private_key
        self._cert = certificate

    @classmethod
    def from_config(
        cls,
        *,
        private_key_pem: str | None,
        certificate_pem: str | None,
        common_name: str,
    ) -> "RootCertificateAuthority":
        key_pem = (private_key_pem or "").strip()
        cert_pem = (certificate_pem or "").strip()
        if key_pem and cert_pem:
            private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
            certificate = x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
            return cls(private_key=private_key, certificate=certificate)

        private_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(tz=timezone.utc)
        subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=2), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()), critical=False)
        ).sign(private_key=private_key, algorithm=hashes.SHA256())
        return cls(private_key=private_key, certificate=certificate)

    @property
    def certificate(self) -> x509.Certificate:
        return self._cert

    @property
    def certificate_pem(self) -> str:
        return self._cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    @property
    def private_key_pem(self) -> str:
        return (
            self._key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ).decode("ascii")
        )

    def issue_intermediate(
        self,
        *,
        common_name: str,
        lifetime: timedelta,
    ) -> tuple[str, str, datetime]:
        """Create an intermediate CA certificate signed by the offline root."""

        private_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(tz=timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._cert.subject)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + lifetime)
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(private_key.public_key()),
                critical=False,
            )
        )
        certificate = builder.sign(private_key=self._key, algorithm=hashes.SHA256())
        expires_at = certificate.not_valid_after
        key_pem = (
            private_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ).decode("ascii")
        )
        cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        return key_pem, cert_pem, expires_at


class IntermediateCertificateAuthority:
    """Intermediate CA anchored to the offline root certificate."""

    def __init__(
        self,
        *,
        private_key_pem: str,
        certificate_pem: str,
        root_certificate: x509.Certificate,
    ) -> None:
        self._private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
        self._certificate = x509.load_pem_x509_certificate(certificate_pem.encode("utf-8"))
        self._root_certificate = root_certificate

    @property
    def certificate(self) -> x509.Certificate:
        return self._certificate

    @property
    def chain_pem(self) -> str:
        return (
            self._certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
            + self._root_certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
        )

    def needs_rotation(self, *, now: datetime, margin: timedelta) -> bool:
        not_after = self._certificate.not_valid_after
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=timezone.utc)
        return now + margin >= not_after

    def _build_certificate(
        self,
        csr: x509.CertificateSigningRequest,
        *,
        not_after: datetime,
        role: DeviceRole,
        subnet_id: str,
        node_id: str,
        scopes: Sequence[Scope],
        ca: bool,
        path_length: int | None,
    ) -> x509.Certificate:
        now = datetime.now(tz=timezone.utc)
        builder = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self._certificate.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=ca, path_length=path_length), critical=True)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._certificate.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(csr.public_key()),
                critical=False,
            )
        )

        san_values = [
            x509.UniformResourceIdentifier(f"urn:adaos:role={role.value}"),
            x509.UniformResourceIdentifier(f"urn:adaos:subnet={subnet_id}"),
            x509.UniformResourceIdentifier(f"urn:adaos:node={node_id}"),
            x509.UniformResourceIdentifier(
                "urn:adaos:scopes=" + ",".join(sorted(scope.value for scope in scopes))
            ),
        ]
        builder = builder.add_extension(x509.SubjectAlternativeName(san_values), critical=False)
        scopes_payload = json.dumps({"scopes": [scope.value for scope in scopes]}).encode("utf-8")
        adaos_scope_oid = ObjectIdentifier("1.3.6.1.4.1.57264.1")
        builder = builder.add_extension(x509.UnrecognizedExtension(adaos_scope_oid, scopes_payload), critical=False)

        if ca:
            key_usage = x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            )
        else:
            key_usage = x509.KeyUsage(
                digital_signature=True,
                content_commitment=True,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            )
        builder = builder.add_extension(key_usage, critical=True)

        eku = x509.ExtendedKeyUsage(
            [
                x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
                x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
            ]
        )
        builder = builder.add_extension(eku, critical=False)
        return builder.sign(private_key=self._private_key, algorithm=hashes.SHA256())

    def sign_end_entity(
        self,
        csr_pem: str,
        *,
        subnet_id: str,
        node_id: str,
        role: DeviceRole,
        scopes: Sequence[Scope],
        lifetime: timedelta,
    ) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode("utf-8"))
        not_after = datetime.now(tz=timezone.utc) + lifetime
        certificate = self._build_certificate(
            csr,
            not_after=not_after,
            role=role,
            subnet_id=subnet_id,
            node_id=node_id,
            scopes=scopes,
            ca=False,
            path_length=None,
        )
        return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def sign_subnet_ca(
        self,
        csr_pem: str,
        *,
        subnet_id: str,
        hub_node_id: str,
        scopes: Sequence[Scope],
        lifetime: timedelta,
    ) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode("utf-8"))
        not_after = datetime.now(tz=timezone.utc) + lifetime
        certificate = self._build_certificate(
            csr,
            not_after=not_after,
            role=DeviceRole.SERVICE,
            subnet_id=subnet_id,
            node_id=hub_node_id,
            scopes=scopes,
            ca=True,
            path_length=0,
        )
        return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")

    @staticmethod
    def certificate_not_after(certificate_pem: str) -> datetime:
        cert = x509.load_pem_x509_certificate(certificate_pem.encode("utf-8"))
        not_after = cert.not_valid_after
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=timezone.utc)
        return not_after


class RootAuthorityBackend:
    """SQLite-backed backend implementing the MVP backlog requirements."""

    _ALIAS_PATTERN = re.compile(r"[^a-z0-9]+")

    def __init__(
        self,
        *,
        db_path: str | Path,
        config_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self._config = self._load_config(config_path)
        self._persistence = SQLitePersistence(self._db_path)
        self._conn = self._persistence.connection
        self._idem_lock = threading.Lock()
        ttl = self._config.get("idempotency_ttl_seconds")
        if ttl is None:
            ttl = self._config.get("idempotency_ttl", 600)
        self._idempotency_ttl = int(ttl)
        self._lifetimes = self._config.get("lifetimes", {})
        self._rate_limits = self._config.get("rate_limits", {})
        self._rate_window = int(self._config.get("rate_limit_window", 60))
        self._audit_key = self._config.get("hmac_audit_key", "").encode("utf-8")
        context_key = self._config.get("context_hmac_key")
        self._context_key = (str(context_key).encode("utf-8") if context_key else self._audit_key)
        stored_root = self._conn.execute(
            "SELECT key_pem, cert_pem, expires_at FROM certificate_authorities WHERE name = ?",
            ("root",),
        ).fetchone()
        if self._config.get("root_ca_private_key_pem") and self._config.get("root_ca_certificate_pem"):
            self._root_ca = RootCertificateAuthority.from_config(
                private_key_pem=self._config.get("root_ca_private_key_pem"),
                certificate_pem=self._config.get("root_ca_certificate_pem"),
                common_name=self._config.get("ca_common_name", "AdaOS Root CA"),
            )
        elif stored_root is not None:
            self._root_ca = RootCertificateAuthority.from_config(
                private_key_pem=stored_root["key_pem"],
                certificate_pem=stored_root["cert_pem"],
                common_name=self._config.get("ca_common_name", "AdaOS Root CA"),
            )
        else:
            self._root_ca = RootCertificateAuthority.from_config(
                private_key_pem=None,
                certificate_pem=None,
                common_name=self._config.get("ca_common_name", "AdaOS Root CA"),
            )
            expires_at = self._root_ca.certificate.not_valid_after
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO certificate_authorities(name, key_pem, cert_pem, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
                    (
                        "root",
                        self._root_ca.private_key_pem,
                        self._root_ca.certificate_pem,
                        self._now().isoformat(),
                        expires_at.isoformat(),
                    ),
                )
        self._intermediate_margin = timedelta(
            days=int(self._config.get("intermediate_rotation_margin_days", 30))
        )
        self._intermediate_ca = self._load_or_create_intermediate()

    # ------------------------------------------------------------------
    # lifecycle helpers
    # ------------------------------------------------------------------
    def close(self) -> None:
        self._persistence.close()

    # ------------------------------------------------------------------
    # configuration helpers
    # ------------------------------------------------------------------
    def _load_config(self, config_path: str | Path | None) -> Mapping[str, Any]:
        path = Path(config_path) if config_path else Path(__file__).with_name("config.yaml")
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def _load_or_create_intermediate(self) -> IntermediateCertificateAuthority:
        now = self._now()
        lifetime_days = int(self._lifetimes.get("intermediate_certificate_days", 180))
        row = self._conn.execute(
            "SELECT key_pem, cert_pem, expires_at FROM certificate_authorities WHERE name = ?",
            ("intermediate",),
        ).fetchone()
        if row is not None:
            cert = x509.load_pem_x509_certificate(row["cert_pem"].encode("utf-8"))
            expires_at = cert.not_valid_after
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now + self._intermediate_margin < expires_at:
                return IntermediateCertificateAuthority(
                    private_key_pem=row["key_pem"],
                    certificate_pem=row["cert_pem"],
                    root_certificate=self._root_ca.certificate,
                )
        key_pem, cert_pem, expires_at = self._root_ca.issue_intermediate(
            common_name=f"AdaOS Intermediate {now.strftime('%Y%m%d')}",
            lifetime=timedelta(days=lifetime_days),
        )
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO certificate_authorities(name, key_pem, cert_pem, created_at, expires_at) "
                "VALUES(?, ?, ?, ?, ?)",
                ("intermediate", key_pem, cert_pem, now.isoformat(), expires_at.isoformat()),
            )
        return IntermediateCertificateAuthority(
            private_key_pem=key_pem,
            certificate_pem=cert_pem,
            root_certificate=self._root_ca.certificate,
        )

    def _intermediate(self) -> IntermediateCertificateAuthority:
        now = self._now()
        if self._intermediate_ca.needs_rotation(now=now, margin=self._intermediate_margin):
            self._intermediate_ca = self._load_or_create_intermediate()
        return self._intermediate_ca

    # ------------------------------------------------------------------
    # time helpers
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _format_time(self, moment: datetime | None = None) -> str:
        instant = (moment or self._now()).astimezone(timezone.utc)
        return instant.replace(microsecond=instant.microsecond // 1000 * 1000).isoformat().replace("+00:00", "Z")

    def _certificate_lifetime(self, role: DeviceRole) -> timedelta:
        if role is DeviceRole.HUB:
            days = int(self._lifetimes.get("hub_certificate_days", 30))
        elif role is DeviceRole.MEMBER:
            days = int(self._lifetimes.get("member_certificate_days", 21))
        elif role is DeviceRole.SERVICE:
            days = int(self._lifetimes.get("subnet_ca_days", 7))
        else:
            days = 30
        return timedelta(days=days)

    def _envelope(
        self,
        payload: Mapping[str, Any],
        *,
        event_id: str | None = None,
        server_time: datetime | None = None,
    ) -> dict[str, Any]:
        event_id = event_id or generate_event_id()
        server_time = server_time or self._now()
        body = dict(payload)
        body.setdefault("event_id", event_id)
        body.setdefault("server_time_utc", self._format_time(server_time))
        return body

    def _raise_error(
        self,
        *,
        code: str,
        message: str,
        hint: str | None = None,
        retry_after: int | None = None,
        status_code: int = 400,
    ) -> None:
        server_time = self._now()
        envelope = ErrorEnvelope(
            code=code,
            message=message,
            hint=hint,
            retry_after=retry_after,
            event_id=generate_event_id(),
            server_time_utc=server_time,
        )
        raise RootBackendError(envelope, status_code=status_code)

    # ------------------------------------------------------------------
    # generic helpers
    # ------------------------------------------------------------------
    def _context_hmac(self, value: str) -> str:
        return hmac.new(self._context_key or b"", value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _slugify_alias(self, alias: str) -> str:
        slug = self._ALIAS_PATTERN.sub("-", alias.strip().lower())
        slug = slug.strip("-")
        if not slug:
            self._raise_error(code="invalid_alias", message="Alias must contain alphanumeric characters.")
        return slug

    def _rate_limit(self, category: str, key: str) -> None:
        limit = int(self._rate_limits.get(category, 0))
        if limit <= 0:
            return
        now = self._now()
        bucket = f"{category}:{key}"
        window_start = now - timedelta(seconds=self._rate_window)
        with self._conn:
            row = self._conn.execute(
                "SELECT count, window_start FROM rate_counters WHERE bucket = ?",
                (bucket,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO rate_counters(bucket, count, window_start) VALUES(?, ?, ?)",
                    (bucket, 1, now.isoformat()),
                )
                return
            current_start = datetime.fromisoformat(row["window_start"])
            if current_start < window_start:
                self._conn.execute(
                    "UPDATE rate_counters SET count = ?, window_start = ? WHERE bucket = ?",
                    (1, now.isoformat(), bucket),
                )
                return
            count = int(row["count"])
            if count >= limit:
                retry = max(1, int((current_start + timedelta(seconds=self._rate_window) - now).total_seconds()))
                self._raise_error(
                    code="rate_limited",
                    message="Request rate exceeded for this endpoint.",
                    retry_after=retry,
                    status_code=429,
                )
            self._conn.execute(
                "UPDATE rate_counters SET count = count + 1 WHERE bucket = ?",
                (bucket,),
            )

    def _with_idempotency(
        self,
        *,
        idempotency_key: str | None,
        method: str,
        path: str,
        principal_id: str,
        body: Mapping[str, Any],
        handler: Callable[[], tuple[Mapping[str, Any], int]],
    ) -> dict[str, Any]:
        if not idempotency_key:
            self._raise_error(code="missing_idempotency_key", message="Idempotency-Key header is required.")
        with self._idem_lock:
            now = self._now()
            body_json = json.dumps(body, sort_keys=True, separators=(",", ":"))
            body_hash = hashlib.sha256(body_json.encode("utf-8")).hexdigest()

            params = {
                "idempotency_key": idempotency_key,
                "method": method,
                "path": path,
                "principal_id": principal_id,
                "body_hash": body_hash,
            }

            while True:
                now = self._now()
                expires_at = now + timedelta(seconds=self._idempotency_ttl)
                row = self._conn.execute(
                    "SELECT response_json, status_code, headers_json, expires_at, committed FROM idempotency_cache "
                    "WHERE idempotency_key = :idempotency_key AND method = :method "
                    "AND path = :path AND principal_id = :principal_id AND body_hash = :body_hash",
                    params,
                ).fetchone()
                if row is not None:
                    record_expires = datetime.fromisoformat(row["expires_at"])
                    if now > record_expires:
                        with self._conn:
                            self._conn.execute(
                                "DELETE FROM idempotency_cache WHERE idempotency_key = :idempotency_key "
                                "AND method = :method AND path = :path AND principal_id = :principal_id AND body_hash = :body_hash",
                                params,
                            )
                        self._raise_error(
                            code="idempotency_key_expired",
                            message="Idempotency key is older than the configured TTL.",
                            status_code=409,
                        )
                    if int(row["committed"]) == 0:
                        time.sleep(0.01)
                        continue
                    payload = json.loads(row["response_json"])
                    return payload

                try:
                    with self._conn:
                        self._conn.execute(
                            "INSERT INTO idempotency_cache (idempotency_key, method, path, principal_id, body_hash, "
                            "created_at, expires_at, committed) VALUES (:idempotency_key, :method, :path, :principal_id, :body_hash,"
                            " :created_at, :expires_at, 0)",
                            {**params, "created_at": now.isoformat(), "expires_at": expires_at.isoformat()},
                        )
                    break
                except (sqlite3.IntegrityError, sqlite3.OperationalError):
                    time.sleep(0.01)

            try:
                payload, status_code = handler()
            except Exception:
                with self._conn:
                    self._conn.execute(
                        "DELETE FROM idempotency_cache WHERE idempotency_key = :idempotency_key AND method = :method AND path = :path "
                        "AND principal_id = :principal_id AND body_hash = :body_hash",
                        params,
                    )
                raise
            envelope = self._envelope(payload, server_time=now)
            with self._conn:
                self._conn.execute(
                    "UPDATE idempotency_cache SET response_json = :response_json, status_code = :status_code, headers_json = :headers, "
                    "event_id = :event_id, server_time_utc = :server_time, committed = 1 WHERE idempotency_key = :idempotency_key "
                    "AND method = :method AND path = :path AND principal_id = :principal_id AND body_hash = :body_hash",
                    {
                        **params,
                        "response_json": json.dumps(envelope, sort_keys=True),
                        "status_code": int(status_code),
                        "headers": json.dumps({}, sort_keys=True),
                        "event_id": envelope["event_id"],
                        "server_time": envelope["server_time_utc"],
                    },
                )
            return envelope

    # ------------------------------------------------------------------
    # entity helpers
    # ------------------------------------------------------------------
    def _ensure_subnet(self, *, subnet_id: str, owner_device_id: str) -> None:
        row = self._conn.execute(
            "SELECT owner_device_id FROM subnets WHERE id = ?",
            (subnet_id,),
        ).fetchone()
        now_iso = self._now().isoformat()
        if row is None:
            with self._conn:
                existing = self._conn.execute(
                    "SELECT id FROM subnets WHERE owner_device_id = ?",
                    (owner_device_id,),
                ).fetchone()
                if existing is not None and existing["id"] != subnet_id:
                    self._raise_error(
                        code="owner_conflict",
                        message="Owner already controls a different subnet.",
                        status_code=409,
                    )
                self._conn.execute(
                    "INSERT INTO subnets(id, owner_device_id, created_at, settings_json) VALUES(?, ?, ?, ?)",
                    (subnet_id, owner_device_id, now_iso, json.dumps({})),
                )
        elif row["owner_device_id"] != owner_device_id:
            self._raise_error(code="forbidden", message="Owner does not control this subnet.", status_code=403)
        with self._conn:
            owner_row = self._conn.execute(
                "SELECT id FROM devices WHERE id = ?",
                (owner_device_id,),
            ).fetchone()
            if owner_row is None:
                self._conn.execute(
                    "INSERT INTO devices(id, role, subnet_id, node_id, jwk_thumb, public_key_pem, aliases_json, capabilities_json, "
                    "scopes_json, revoked, created_at, updated_at, deleted_at) "
                    "VALUES(?, 'OWNER', ?, NULL, NULL, NULL, ?, ?, ?, 0, ?, ?, NULL)",
                    (
                        owner_device_id,
                        subnet_id,
                        json.dumps([]),
                        json.dumps([]),
                        json.dumps([scope.value for scope in Scope]),
                        now_iso,
                        now_iso,
                    ),
                )

    def _fetch_device(self, device_id: str) -> Mapping[str, Any]:
        row = self._conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            self._raise_error(code="unknown_device", message="Device not found.", status_code=404)
        if int(row["revoked"]) == 1:
            self._raise_error(code="device_revoked", message="Device has been revoked.", status_code=403)
        return dict(row)

    def _fetch_node(self, node_id: str) -> Mapping[str, Any]:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            self._raise_error(code="unknown_node", message="Node not found.", status_code=404)
        return dict(row)

    def _check_scope(self, *, actor_id: str, required: Scope | None, subnet_id: str) -> Mapping[str, Any]:
        device = self._fetch_device(actor_id)
        if device["subnet_id"] != subnet_id:
            self._raise_error(code="forbidden", message="Device is not part of this subnet.", status_code=403)
        if required is not None:
            scopes = set(json.loads(device["scopes_json"]))
            if required.value not in scopes:
                self._raise_error(code="forbidden_scope", message="Required scope missing.", status_code=403)
        return device

    def _record_audit(
        self,
        *,
        subnet_id: str,
        actor_id: str | None,
        subject_id: str | None,
        action: str,
        scopes: Iterable[Scope],
        payload: Mapping[str, Any],
    ) -> None:
        event_id = str(generate_event_id())
        trace_id = str(generate_trace_id())
        record = {
            "event_id": event_id,
            "trace_id": trace_id,
            "subnet_id": subnet_id,
            "actor_id": actor_id,
            "subject_id": subject_id,
            "action": action,
            "acl": [scope.value for scope in scopes],
            "ttl": 90 * 24 * 3600,
            "payload": payload,
            "timestamp": self._format_time(),
        }
        serialized = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._audit_key, serialized, hashlib.sha256).hexdigest()
        with self._conn:
            self._conn.execute(
                "INSERT INTO audit_records(id, event_id, payload_json, signature, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    record["event_id"],
                    record["event_id"],
                    json.dumps(record, sort_keys=True),
                    signature,
                    self._now().isoformat(),
                ),
            )

    def _revoke_tokens_for_device(self, device_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE tokens SET revoked = 1 WHERE device_id = ? AND revoked = 0",
                (device_id,),
            )

    def _revoke_hub_channels(self, node_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE hub_channels SET revoked = 1 WHERE node_id = ?",
                (node_id,),
            )

    def _issue_token_bundle(self, *, device_row: Mapping[str, Any], cnf_thumb: str) -> TokenResponse:
        access_lifetime = int(self._lifetimes.get("access_token_seconds", 600))
        refresh_lifetime = int(self._lifetimes.get("refresh_token_seconds", 4 * 3600))
        channel_lifetime = int(self._lifetimes.get("channel_token_seconds", 300))
        now = self._now()
        access_token = secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(64)
        channel_token = secrets.token_urlsafe(40)
        payload = {
            "device_id": device_row["id"],
            "subnet_id": device_row["subnet_id"],
            "scopes": json.loads(device_row["scopes_json"]),
        }
        with self._conn:
            self._conn.execute(
                "INSERT INTO tokens(token, device_id, kind, cnf_thumb, subnet_id, payload_json, created_at, expires_at, revoked) "
                "VALUES(?, ?, 'access', ?, ?, ?, ?, ?, 0)",
                (
                    access_token,
                    device_row["id"],
                    cnf_thumb,
                    device_row["subnet_id"],
                    json.dumps(payload, sort_keys=True),
                    now.isoformat(),
                    (now + timedelta(seconds=access_lifetime)).isoformat(),
                ),
            )
            self._conn.execute(
                "INSERT INTO tokens(token, device_id, kind, cnf_thumb, subnet_id, payload_json, created_at, expires_at, revoked) "
                "VALUES(?, ?, 'refresh', ?, ?, ?, ?, ?, 0)",
                (
                    refresh_token,
                    device_row["id"],
                    cnf_thumb,
                    device_row["subnet_id"],
                    json.dumps(payload, sort_keys=True),
                    now.isoformat(),
                    (now + timedelta(seconds=refresh_lifetime)).isoformat(),
                ),
            )
            self._conn.execute(
                "INSERT INTO tokens(token, device_id, kind, cnf_thumb, subnet_id, payload_json, created_at, expires_at, revoked) "
                "VALUES(?, ?, 'channel', ?, ?, ?, ?, ?, 0)",
                (
                    channel_token,
                    device_row["id"],
                    cnf_thumb,
                    device_row["subnet_id"],
                    json.dumps(payload, sort_keys=True),
                    now.isoformat(),
                    (now + timedelta(seconds=channel_lifetime)).isoformat(),
                ),
            )
        scopes = [Scope(scope) for scope in payload["scopes"]]
        return TokenResponse(
            device_id=device_row["id"],
            subnet_id=device_row["subnet_id"],
            scopes=scopes,
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=now + timedelta(seconds=access_lifetime),
            refresh_expires_at=now + timedelta(seconds=refresh_lifetime),
            channel_token=channel_token,
            channel_expires_at=now + timedelta(seconds=channel_lifetime),
        )

    def _rotate_channel_token(self, *, device_row: Mapping[str, Any]) -> tuple[str, datetime]:
        channel_lifetime = int(self._lifetimes.get("channel_token_seconds", 300))
        now = self._now()
        token = secrets.token_urlsafe(40)
        scopes = json.loads(device_row["scopes_json"])
        payload = {
            "device_id": device_row["id"],
            "subnet_id": device_row["subnet_id"],
            "scopes": scopes,
        }
        with self._conn:
            self._conn.execute(
                "UPDATE tokens SET revoked = 1 WHERE device_id = ? AND kind = 'channel'",
                (device_row["id"],),
            )
            self._conn.execute(
                "INSERT INTO tokens(token, device_id, kind, cnf_thumb, subnet_id, payload_json, created_at, expires_at, revoked) "
                "VALUES(?, ?, 'channel', ?, ?, ?, ?, ?, 0)",
                (
                    token,
                    device_row["id"],
                    device_row.get("jwk_thumb"),
                    device_row["subnet_id"],
                    json.dumps(payload, sort_keys=True),
                    now.isoformat(),
                    (now + timedelta(seconds=channel_lifetime)).isoformat(),
                ),
            )
        self._record_audit(
            subnet_id=device_row["subnet_id"],
            actor_id=device_row["id"],
            subject_id=device_row["id"],
            action="device.channel_rotated",
            scopes=[Scope(scope) for scope in scopes],
            payload={"token_suffix": token[-6:]},
        )
        return token, now + timedelta(seconds=channel_lifetime)

    def _verify_jws(self, assertion: str, *, public_key_pem: str, expected_nonce: str, expected_aud: str, expected_thumb: str) -> Mapping[str, Any]:
        try:
            header_b64, payload_b64, signature_b64 = assertion.split(".")
        except ValueError as exc:
            self._raise_error(code="invalid_assertion", message="Invalid JWS format.")
        header = json.loads(self._b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(self._b64url_decode(payload_b64).decode("utf-8"))
        if header.get("alg") != "ES256":
            self._raise_error(code="invalid_algorithm", message="Unsupported algorithm.")
        if header.get("kid") != expected_thumb:
            self._raise_error(code="hok_mismatch", message="Key identifier mismatch.")
        cnf = payload.get("cnf", {})
        if cnf.get("jkt") != expected_thumb:
            self._raise_error(code="hok_mismatch", message="Holder-of-key thumbprint mismatch.")
        if payload.get("nonce") != expected_nonce or payload.get("aud") != expected_aud:
            self._raise_error(code="challenge_mismatch", message="Nonce or audience mismatch.")
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            self._raise_error(code="invalid_assertion", message="Missing expiration.")
        if self._now() >= datetime.fromtimestamp(exp, tz=timezone.utc):
            self._raise_error(code="assertion_expired", message="Assertion expired.")
        signature = self._b64url_decode(signature_b64)
        public_key = serialization.load_pem_public_key(public_key_pem.encode("ascii"))
        try:
            if len(signature) == 64:
                r = int.from_bytes(signature[:32], "big")
                s = int.from_bytes(signature[32:], "big")
                signature_to_verify = encode_dss_signature(r, s)
            else:
                signature_to_verify = signature
            public_key.verify(
                signature_to_verify,
                f"{header_b64}.{payload_b64}".encode("ascii"),
                ec.ECDSA(hashes.SHA256()),
            )
        except InvalidSignature:
            self._raise_error(code="invalid_assertion", message="Signature verification failed.")
        return payload

    @staticmethod
    def _b64url_decode(data: str) -> bytes:
        padding = "=" * ((4 - len(data) % 4) % 4)
        return base64.urlsafe_b64decode(data + padding)

    # ------------------------------------------------------------------
    # Device registration flow
    # ------------------------------------------------------------------
    def device_start(
        self,
        *,
        subnet_id: str,
        role: DeviceRole,
        scopes: Sequence[Scope],
        context: ClientContext,
        jwk_thumb: str,
        idempotency_key: str | None,
        public_key_pem: str | None = None,
    ) -> dict[str, Any]:
        self._rate_limit("device", context.client_ip)

        def handler() -> tuple[Mapping[str, Any], int]:
            now = self._now()
            expires_at = now + timedelta(seconds=int(self._lifetimes.get("device_code_seconds", 600)))
            device_code = secrets.token_urlsafe(24)
            user_code = secrets.token_urlsafe(8)
            record = {
                "device_code": device_code,
                "user_code": user_code,
                "subnet_id": subnet_id,
                "role": role.value,
                "scopes": json.dumps([scope.value for scope in scopes]),
                "origin": self._context_hmac(context.origin),
                "ip_hash": self._context_hmac(context.client_ip),
                "ua_hash": self._context_hmac(context.user_agent),
                "init_thumb": jwk_thumb,
                "public_key_pem": public_key_pem,
                "status": "pending",
                "approved_scopes": None,
                "device_id": None,
                "owner_id": None,
                "response_json": None,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "used_at": None,
            }
            with self._conn:
                self._conn.execute(
                    "INSERT INTO device_codes(device_code, user_code, subnet_id, role, scopes, origin, ip_hash, ua_hash, "
                    "init_thumb, public_key_pem, status, approved_scopes, device_id, owner_id, response_json, created_at, updated_at, "
                    "expires_at, used_at) "
                    "VALUES(:device_code, :user_code, :subnet_id, :role, :scopes, :origin, :ip_hash, :ua_hash, :init_thumb, :public_key_pem, "
                    ":status, :approved_scopes, :device_id, :owner_id, :response_json, :created_at, :updated_at, :expires_at, :used_at)",
                    record,
                )
            payload = {
                "device_code": device_code,
                "user_code": user_code,
                "expires_in": int((expires_at - now).total_seconds()),
                "expires_at": expires_at.isoformat(),
                "subnet_id": subnet_id,
            }
            return payload, 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/device/start",
            principal_id=context.origin,
            body={
                "subnet_id": subnet_id,
                "role": role.value,
                "scopes": [scope.value for scope in scopes],
                "origin": context.origin,
                "client_ip": context.client_ip,
                "user_agent": context.user_agent,
                "jwk_thumb": jwk_thumb,
            },
            handler=handler,
        )

    def device_confirm(
        self,
        *,
        owner_device_id: str,
        user_code: str,
        granted_scopes: Sequence[Scope],
        jwk_thumb: str,
        public_key_pem: str,
        aliases: Sequence[str] | None,
        capabilities: Sequence[str] | None,
        context: ClientContext,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        def handler() -> tuple[Mapping[str, Any], int]:
            row = self._conn.execute(
                "SELECT * FROM device_codes WHERE user_code = ?",
                (user_code,),
            ).fetchone()
            if row is None:
                self._raise_error(code="unknown_user_code", message="User code not recognised.", status_code=404)
            record = dict(row)
            now = self._now()
            if datetime.fromisoformat(record["expires_at"]) < now:
                self._raise_error(code="expired_device_code", message="Device code expired.", status_code=409)
            if record["status"] == "approved" and record["response_json"]:
                return json.loads(record["response_json"]), 200
            if record["status"] == "denied":
                self._raise_error(code="already_denied", message="Device request already denied.", status_code=409)
            if record["init_thumb"] != jwk_thumb:
                self._raise_error(code="anti_relay_blocked", message="Thumbprint mismatch.", status_code=403)
            if record["origin"] != self._context_hmac(context.origin):
                self._raise_error(code="anti_relay_blocked", message="Origin mismatch.", status_code=403)
            if record["ip_hash"] != self._context_hmac(context.client_ip):
                self._raise_error(code="anti_relay_blocked", message="Client IP mismatch.", status_code=403)
            if record["ua_hash"] != self._context_hmac(context.user_agent):
                self._raise_error(code="anti_relay_blocked", message="User agent mismatch.", status_code=403)
            subnet_id = record["subnet_id"]
            self._ensure_subnet(subnet_id=subnet_id, owner_device_id=owner_device_id)
            device_id = record["device_id"] or str(generate_device_id())
            now_iso = now.isoformat()
            scopes_json = json.dumps(sorted({scope.value for scope in granted_scopes}))
            aliases_json = json.dumps([])
            capabilities_json = json.dumps(sorted(set(capabilities or [])))
            if record["device_id"] is None:
                with self._conn:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO devices(id, role, subnet_id, node_id, jwk_thumb, public_key_pem, aliases_json, "
                        "capabilities_json, scopes_json, revoked, created_at, updated_at, deleted_at) "
                        "VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?, 0, ?, ?, NULL)",
                        (
                            device_id,
                            record["role"],
                            subnet_id,
                            jwk_thumb,
                            public_key_pem,
                            aliases_json,
                            capabilities_json,
                            scopes_json,
                            now_iso,
                            now_iso,
                        ),
                    )
            else:
                with self._conn:
                    self._conn.execute(
                        "UPDATE devices SET jwk_thumb = ?, public_key_pem = ?, scopes_json = ?, capabilities_json = ?, updated_at = ? "
                        "WHERE id = ?",
                        (jwk_thumb, public_key_pem, scopes_json, capabilities_json, now_iso, device_id),
                    )
            if aliases:
                self._store_aliases(subnet_id=subnet_id, device_id=device_id, aliases=aliases)
            response = self._envelope(
                {
                    "device_id": device_id,
                    "subnet_id": subnet_id,
                    "scopes": json.loads(scopes_json),
                }
            )
            with self._conn:
                self._conn.execute(
                    "UPDATE device_codes SET status = 'approved', approved_scopes = ?, device_id = ?, owner_id = ?, used_at = ?, "
                    "public_key_pem = ?, response_json = ?, updated_at = ? WHERE user_code = ?",
                    (
                        scopes_json,
                        device_id,
                        owner_device_id,
                        now_iso,
                        public_key_pem,
                        json.dumps(response, sort_keys=True),
                        now_iso,
                        user_code,
                    ),
                )
            self._record_audit(
                subnet_id=subnet_id,
                actor_id=owner_device_id,
                subject_id=device_id,
                action="device.approved",
                scopes=granted_scopes,
                payload={"user_code": user_code, "role": record["role"]},
            )
            return response, 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/device/confirm",
            principal_id=owner_device_id,
            body={"user_code": user_code, "owner": owner_device_id},
            handler=handler,
        )

    def _store_aliases(self, *, subnet_id: str, device_id: str, aliases: Sequence[str]) -> None:
        now_iso = self._now().isoformat()
        prepared: list[tuple[str, str]] = []
        for alias in aliases:
            slug = self._slugify_alias(alias)
            row = self._conn.execute(
                "SELECT device_id FROM device_aliases WHERE alias_slug = ? AND subnet_id = ?",
                (slug, subnet_id),
            ).fetchone()
            if row and row["device_id"] != device_id:
                self._raise_error(code="alias_conflict", message="Alias already in use within subnet.", status_code=409)
            prepared.append((slug, alias))
        with self._conn:
            self._conn.execute("DELETE FROM device_aliases WHERE device_id = ?", (device_id,))
            for slug, alias in prepared:
                self._conn.execute(
                    "INSERT INTO device_aliases(alias_slug, display, subnet_id, device_id, created_at) VALUES(?, ?, ?, ?, ?)",
                    (slug, alias, subnet_id, device_id, now_iso),
                )

    def device_token(
        self,
        *,
        device_code: str,
        assertion: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        def handler() -> tuple[Mapping[str, Any], int]:
            row = self._conn.execute(
                "SELECT * FROM device_codes WHERE device_code = ?",
                (device_code,),
            ).fetchone()
            if row is None:
                self._raise_error(code="expired_device_code", message="Device code invalid.", status_code=404)
            record = dict(row)
            now = self._now()
            if datetime.fromisoformat(record["expires_at"]) < now:
                self._raise_error(code="expired_device_code", message="Device code expired.", status_code=409)
            if record["status"] == "pending":
                self._raise_error(code="authorization_pending", message="Waiting for owner approval.", status_code=409)
            if record["status"] == "denied":
                self._raise_error(code="access_denied", message="Owner denied the request.", status_code=403)
            if record["status"] == "issued" and record["response_json"]:
                return json.loads(record["response_json"]), 200
            device_row = self._fetch_device(record["device_id"])
            payload = self._verify_jws(
                assertion,
                public_key_pem=device_row["public_key_pem"],
                expected_nonce=device_code,
                expected_aud=f"subnet:{device_row['subnet_id']}",
                expected_thumb=device_row["jwk_thumb"],
            )
            issued = self._issue_token_bundle(device_row=device_row, cnf_thumb=device_row["jwk_thumb"])
            response = self._envelope(issued.as_payload())
            with self._conn:
                self._conn.execute(
                    "UPDATE device_codes SET status = 'issued', response_json = ?, used_at = ?, updated_at = ? WHERE device_code = ?",
                    (json.dumps(response, sort_keys=True), now.isoformat(), now.isoformat(), device_code),
                )
            self._record_audit(
                subnet_id=device_row["subnet_id"],
                actor_id=device_row["id"],
                subject_id=device_row["id"],
                action="device.tokens_issued",
                scopes=[Scope(scope) for scope in json.loads(device_row["scopes_json"])],
                payload={"nonce": payload.get("nonce")},
            )
            return response, 200

        self._rate_limit("device", device_code)
        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/device/token",
            principal_id=device_code,
            body={"device_code": device_code},
            handler=handler,
        )

    # ------------------------------------------------------------------
    # QR session flow
    # ------------------------------------------------------------------
    def qr_start(
        self,
        *,
        subnet_id: str,
        scopes: Sequence[Scope],
        context: ClientContext,
        init_thumb: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        self._rate_limit("qr", context.client_ip)

        def handler() -> tuple[Mapping[str, Any], int]:
            now = self._now()
            ttl = int(self._lifetimes.get("qr_session_seconds", 300))
            expires_at = now + timedelta(seconds=ttl)
            session_id = secrets.token_urlsafe(20)
            nonce = secrets.token_urlsafe(12)
            record = {
                "session_id": session_id,
                "subnet_id": subnet_id,
                "nonce": nonce,
                "scopes": json.dumps([scope.value for scope in scopes]),
                "origin": self._context_hmac(context.origin),
                "ip_hash": self._context_hmac(context.client_ip),
                "ua_hash": self._context_hmac(context.user_agent),
                "init_thumb": init_thumb,
                "requested_thumb": init_thumb,
                "status": "pending",
                "approved_scopes": None,
                "device_id": None,
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
                "used_at": None,
            }
            with self._conn:
                self._conn.execute(
                    "INSERT INTO qr_sessions(session_id, subnet_id, nonce, scopes, origin, ip_hash, ua_hash, init_thumb, requested_thumb, "
                    "status, approved_scopes, device_id, created_at, expires_at, used_at) "
                    "VALUES(:session_id, :subnet_id, :nonce, :scopes, :origin, :ip_hash, :ua_hash, :init_thumb, :requested_thumb, :status, :approved_scopes, :device_id, :created_at, :expires_at, :used_at)",
                    record,
                )
            payload = {
                "session_id": session_id,
                "nonce": nonce,
                "ttl": ttl,
                "init_device_thumb": init_thumb,
                "origin": context.origin,
            }
            return payload, 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/qr/start",
            principal_id=context.origin,
            body={"subnet_id": subnet_id},
            handler=handler,
        )

    def qr_approve(
        self,
        *,
        owner_device_id: str,
        session_id: str,
        jwk_thumb: str,
        public_key_pem: str,
        scopes: Sequence[Scope],
        context: ClientContext,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        def handler() -> tuple[Mapping[str, Any], int]:
            row = self._conn.execute(
                "SELECT * FROM qr_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                self._raise_error(code="unknown_session", message="QR session not found.", status_code=404)
            record = dict(row)
            now = self._now()
            if datetime.fromisoformat(record["expires_at"]) < now:
                self._raise_error(code="expired_session", message="QR session expired.", status_code=409)
            if record["status"] == "approved" and record["device_id"] and record["approved_scopes"]:
                return json.loads(record["approved_scopes"]), 200
            if record["init_thumb"] != jwk_thumb:
                self._raise_error(code="anti_relay_blocked", message="Thumbprint mismatch.", status_code=403)
            if record["origin"] != self._context_hmac(context.origin):
                self._raise_error(code="anti_relay_blocked", message="Origin mismatch.")
            if record["ip_hash"] != self._context_hmac(context.client_ip):
                self._raise_error(code="anti_relay_blocked", message="IP mismatch.")
            if record["ua_hash"] != self._context_hmac(context.user_agent):
                self._raise_error(code="anti_relay_blocked", message="User agent mismatch.")
            subnet_id = record["subnet_id"]
            self._ensure_subnet(subnet_id=subnet_id, owner_device_id=owner_device_id)
            device_id = record.get("device_id") or str(generate_device_id())
            now_iso = now.isoformat()
            scopes_json = json.dumps(sorted({scope.value for scope in scopes}))
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO devices(id, role, subnet_id, node_id, jwk_thumb, public_key_pem, aliases_json, capabilities_json, "
                    "scopes_json, revoked, created_at, updated_at, deleted_at) "
                    "VALUES(?, 'BROWSER_IO', ?, NULL, ?, ?, ?, ?, ?, 0, ?, ?, NULL)",
                    (device_id, subnet_id, jwk_thumb, public_key_pem, json.dumps([]), json.dumps([]), scopes_json, now_iso, now_iso),
                )
                self._conn.execute(
                    "UPDATE qr_sessions SET status = 'approved', approved_scopes = ?, device_id = ?, used_at = ? WHERE session_id = ?",
                    (json.dumps({"device_id": device_id, "scopes": json.loads(scopes_json)}), device_id, now_iso, session_id),
                )
            response = self._envelope({"device_id": device_id, "subnet_id": subnet_id, "scopes": json.loads(scopes_json)})
            self._record_audit(
                subnet_id=subnet_id,
                actor_id=owner_device_id,
                subject_id=device_id,
                action="qr.approved",
                scopes=scopes,
                payload={"session_id": session_id},
            )
            return response, 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/qr/approve",
            principal_id=owner_device_id,
            body={"session_id": session_id},
            handler=handler,
        )

    # ------------------------------------------------------------------
    # Browser HoK authentication
    # ------------------------------------------------------------------
    def auth_challenge(
        self,
        *,
        device_id: str,
        audience: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        self._rate_limit("auth", device_id)
        device = self._fetch_device(device_id)

        def handler() -> tuple[Mapping[str, Any], int]:
            nonce = secrets.token_urlsafe(24)
            exp_seconds = int(self._lifetimes.get("challenge_seconds", 120))
            expires_at = self._now() + timedelta(seconds=exp_seconds)
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO browser_challenges(device_id, nonce, audience, expires_at, created_at) VALUES(?, ?, ?, ?, ?)",
                    (device_id, nonce, audience, expires_at.isoformat(), self._now().isoformat()),
                )
            payload = {"nonce": nonce, "aud": audience, "exp": int(expires_at.timestamp())}
            return payload, 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/auth/challenge",
            principal_id=device_id,
            body={"device_id": device_id, "aud": audience},
            handler=handler,
        )

    def auth_complete(
        self,
        *,
        device_id: str,
        assertion: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        device = self._fetch_device(device_id)

        def handler() -> tuple[Mapping[str, Any], int]:
            challenge = self._conn.execute(
                "SELECT * FROM browser_challenges WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if challenge is None:
                self._raise_error(code="challenge_expired", message="Challenge missing or expired.", status_code=409)
            if datetime.fromisoformat(challenge["expires_at"]) < self._now():
                self._raise_error(code="challenge_expired", message="Challenge expired.", status_code=409)
            payload = self._verify_jws(
                assertion,
                public_key_pem=device["public_key_pem"],
                expected_nonce=challenge["nonce"],
                expected_aud=challenge["audience"],
                expected_thumb=device["jwk_thumb"],
            )
            issued = self._issue_token_bundle(device_row=device, cnf_thumb=device["jwk_thumb"])
            response = self._envelope(issued.as_payload())
            with self._conn:
                self._conn.execute("DELETE FROM browser_challenges WHERE device_id = ?", (device_id,))
            self._record_audit(
                subnet_id=device["subnet_id"],
                actor_id=device_id,
                subject_id=device_id,
                action="browser.authenticated",
                scopes=[Scope(scope) for scope in json.loads(device["scopes_json"])],
                payload={"aud": payload.get("aud")},
            )
            return response, 200

        self._rate_limit("auth", device_id)
        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/auth/complete",
            principal_id=device_id,
            body={"device_id": device_id},
            handler=handler,
        )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------
    def token_refresh(self, *, refresh_token: str, assertion: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM tokens WHERE token = ? AND kind = 'refresh'",
            (refresh_token,),
        ).fetchone()
        if row is None or int(row["revoked"]) == 1:
            self._raise_error(code="invalid_refresh", message="Refresh token unknown.", status_code=401)
        if datetime.fromisoformat(row["expires_at"]) < self._now():
            self._raise_error(code="invalid_refresh", message="Refresh token expired.", status_code=401)
        device = self._fetch_device(row["device_id"])
        self._verify_jws(
            assertion,
            public_key_pem=device["public_key_pem"],
            expected_nonce=refresh_token,
            expected_aud=f"subnet:{device['subnet_id']}",
            expected_thumb=device["jwk_thumb"],
        )
        self._revoke_tokens_for_device(device["id"])
        issued = self._issue_token_bundle(device_row=device, cnf_thumb=device["jwk_thumb"])
        self._record_audit(
            subnet_id=device["subnet_id"],
            actor_id=device["id"],
            subject_id=device["id"],
            action="device.tokens_rotated",
            scopes=[Scope(scope) for scope in json.loads(device["scopes_json"])],
            payload={"refresh": refresh_token},
        )
        return self._envelope(issued.as_payload())

    def introspect_token(self, *, token: str, assertion: str) -> Mapping[str, Any]:
        row = self._conn.execute("SELECT * FROM tokens WHERE token = ?", (token,)).fetchone()
        if row is None:
            self._raise_error(code="invalid_token", message="Token unknown.", status_code=401)
        if int(row["revoked"]) == 1:
            self._raise_error(code="device_revoked", message="Token revoked.", status_code=401)
        if datetime.fromisoformat(row["expires_at"]) < self._now():
            self._raise_error(code="invalid_token", message="Token expired.", status_code=401)
        device = self._fetch_device(row["device_id"])
        self._verify_jws(
            assertion,
            public_key_pem=device["public_key_pem"],
            expected_nonce=token,
            expected_aud=f"subnet:{device['subnet_id']}",
            expected_thumb=device["jwk_thumb"],
        )
        payload = json.loads(row["payload_json"])
        payload.update({"token": token, "kind": row["kind"], "expires_at": row["expires_at"]})
        return self._envelope(payload)

    def authorize_channel(
        self,
        *,
        device_id: str,
        channel_token: str,
        assertion: str,
        hub_node_id: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        def handler() -> tuple[Mapping[str, Any], int]:
            row = self._conn.execute(
                "SELECT * FROM tokens WHERE token = ? AND kind = 'channel'",
                (channel_token,),
            ).fetchone()
            if row is None:
                self._raise_error(code="channel_unknown", message="Channel token invalid.", status_code=401)
            if int(row["revoked"]) == 1:
                self._raise_error(code="channel_revoked", message="Channel revoked.", status_code=403)
            if row["device_id"] != device_id:
                self._raise_error(code="channel_forbidden", message="Channel token not bound to device.", status_code=403)
            now = self._now()
            created_at = datetime.fromisoformat(row["created_at"])
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at < now:
                self._raise_error(code="channel_expired", message="Channel expired.", status_code=401)
            device = self._fetch_device(device_id)
            expected_aud = f"wss:subnet:{device['subnet_id']}"
            if hub_node_id:
                hub = self._fetch_node(hub_node_id)
                if hub["subnet_id"] != device["subnet_id"]:
                    self._raise_error(code="forbidden", message="Hub belongs to another subnet.", status_code=403)
                expected_aud = f"{expected_aud}|hub:{hub_node_id}"
            self._verify_jws(
                assertion,
                public_key_pem=device["public_key_pem"],
                expected_nonce=channel_token,
                expected_aud=expected_aud,
                expected_thumb=device["jwk_thumb"],
            )
            scopes = json.loads(device["scopes_json"])
            rotated = False
            new_token = channel_token
            new_expiry = expires_at
            total = expires_at - created_at
            if total.total_seconds() > 0:
                elapsed = now - created_at
                if elapsed / total >= 0.8:
                    new_token, new_expiry = self._rotate_channel_token(device_row=device)
                    rotated = True
            payload = {
                "device_id": device_id,
                "subnet_id": device["subnet_id"],
                "scopes": scopes,
                "channel_token": new_token,
                "channel_expires_at": new_expiry.isoformat(),
                "hub_node_id": hub_node_id,
                "rotated": rotated,
                "message_id": str(generate_event_id()),
            }
            return payload, 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/channel/authorize",
            principal_id=device_id,
            body={
                "device_id": device_id,
                "channel_token": channel_token,
                "hub_node_id": hub_node_id,
            },
            handler=handler,
        )

    def revoke_device(self, *, actor_device_id: str, device_id: str, reason: str | None, idempotency_key: str | None) -> dict[str, Any]:
        actor = self._fetch_device(actor_device_id)
        target = self._fetch_device(device_id)
        if actor["subnet_id"] != target["subnet_id"]:
            self._raise_error(code="forbidden", message="Devices belong to different subnets.", status_code=403)
        scopes = [Scope(scope) for scope in json.loads(actor["scopes_json"])]
        if Scope.MANAGE_IO_DEVICES.value not in [scope.value for scope in scopes] and actor_device_id != target["id"]:
            self._raise_error(code="forbidden_scope", message="Missing manage scope to revoke others.", status_code=403)

        def handler() -> tuple[Mapping[str, Any], int]:
            now_iso = self._now().isoformat()
            with self._conn:
                self._conn.execute("UPDATE devices SET revoked = 1, updated_at = ? WHERE id = ?", (now_iso, device_id))
                self._conn.execute(
                    "INSERT INTO denylist(id, entity_type, entity_id, subnet_id, reason, created_at, expires_at) "
                    "VALUES(?, 'device', ?, ?, ?, ?, NULL)",
                    (
                        str(generate_event_id()),
                        device_id,
                        target["subnet_id"],
                        reason or "unspecified",
                        now_iso,
                    ),
                )
            self._revoke_tokens_for_device(device_id)
            self._record_audit(
                subnet_id=target["subnet_id"],
                actor_id=actor_device_id,
                subject_id=device_id,
                action="device.revoked",
                scopes=[Scope(scope) for scope in json.loads(target["scopes_json"])],
                payload={"reason": reason or "unspecified"},
            )
            return self._envelope({"device_id": device_id, "revoked": True}), 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path=f"/v1/devices/{device_id}/revoke",
            principal_id=actor_device_id,
            body={"reason": reason or "unspecified"},
            handler=handler,
        )

    def rotate_device(
        self,
        *,
        device_id: str,
        jwk_thumb: str,
        public_key_pem: str,
        assertion: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        device = self._fetch_device(device_id)
        self._verify_jws(
            assertion,
            public_key_pem=device["public_key_pem"],
            expected_nonce=device_id,
            expected_aud=f"subnet:{device['subnet_id']}",
            expected_thumb=device["jwk_thumb"],
        )

        def handler() -> tuple[Mapping[str, Any], int]:
            now_iso = self._now().isoformat()
            with self._conn:
                self._conn.execute(
                    "UPDATE devices SET jwk_thumb = ?, public_key_pem = ?, updated_at = ? WHERE id = ?",
                    (jwk_thumb, public_key_pem, now_iso, device_id),
                )
            self._record_audit(
                subnet_id=device["subnet_id"],
                actor_id=device_id,
                subject_id=device_id,
                action="device.rotated",
                scopes=[Scope(scope) for scope in json.loads(device["scopes_json"])],
                payload={"thumbprint": jwk_thumb},
            )
            return self._envelope({"device_id": device_id, "rotated": True}), 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path=f"/v1/devices/{device_id}/rotate",
            principal_id=device_id,
            body={"jwk_thumb": jwk_thumb},
            handler=handler,
        )

    # ------------------------------------------------------------------
    # Directory and alias management
    # ------------------------------------------------------------------
    def get_device(self, *, actor_device_id: str, device_id: str) -> dict[str, Any]:
        device = self._check_scope(actor_id=actor_device_id, required=None, subnet_id=self._fetch_device(device_id)["subnet_id"])
        target = self._fetch_device(device_id)
        payload = {
            "device_id": target["id"],
            "role": target["role"],
            "aliases": json.loads(target["aliases_json"]),
            "capabilities": json.loads(target["capabilities_json"]),
            "scopes": json.loads(target["scopes_json"]),
            "subnet_id": target["subnet_id"],
        }
        return self._envelope(payload)

    def update_device(
        self,
        *,
        actor_device_id: str,
        device_id: str,
        aliases: Sequence[str] | None = None,
        capabilities: Sequence[str] | None = None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        target = self._fetch_device(device_id)
        actor = self._check_scope(actor_id=actor_device_id, required=None, subnet_id=target["subnet_id"])
        require_manage = bool(capabilities and set(capabilities) != set(json.loads(target["capabilities_json"])))
        if require_manage:
            self._check_scope(actor_id=actor_device_id, required=Scope.MANAGE_IO_DEVICES, subnet_id=target["subnet_id"])

        def handler() -> tuple[Mapping[str, Any], int]:
            now_iso = self._now().isoformat()
            new_aliases = json.loads(target["aliases_json"])
            if aliases is not None:
                new_aliases = sorted(set(aliases))
                self._store_aliases(subnet_id=target["subnet_id"], device_id=device_id, aliases=new_aliases)
            new_capabilities = json.loads(target["capabilities_json"])
            if capabilities is not None:
                new_capabilities = sorted(set(capabilities))
            with self._conn:
                self._conn.execute(
                    "UPDATE devices SET aliases_json = ?, capabilities_json = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(new_aliases),
                        json.dumps(new_capabilities),
                        now_iso,
                        device_id,
                    ),
                )
            self._record_audit(
                subnet_id=target["subnet_id"],
                actor_id=actor_device_id,
                subject_id=device_id,
                action="device.updated",
                scopes=[Scope(scope) for scope in json.loads(target["scopes_json"])],
                payload={"aliases": new_aliases, "capabilities": new_capabilities},
            )
            return self._envelope({"device_id": device_id, "aliases": new_aliases, "capabilities": new_capabilities}), 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="PUT",
            path=f"/v1/devices/{device_id}",
            principal_id=actor_device_id,
            body={"device_id": device_id},
            handler=handler,
        )

    def list_devices(self, *, actor_device_id: str, role: DeviceRole | None = None) -> dict[str, Any]:
        actor = self._fetch_device(actor_device_id)
        query = "SELECT * FROM devices WHERE subnet_id = ?"
        params: list[Any] = [actor["subnet_id"]]
        if role is not None:
            query += " AND role = ?"
            params.append(role.value)
        rows = self._conn.execute(query, params).fetchall()
        payload = [
            {
                "device_id": row["id"],
                "role": row["role"],
                "aliases": json.loads(row["aliases_json"]),
                "capabilities": json.loads(row["capabilities_json"]),
                "scopes": json.loads(row["scopes_json"]),
            }
            for row in rows
        ]
        return self._envelope({"devices": payload})

    # ------------------------------------------------------------------
    # Consent and CSR management
    # ------------------------------------------------------------------
    def submit_csr(
        self,
        *,
        csr_pem: str,
        role: DeviceRole,
        subnet_id: str,
        node_id: str | None = None,
        scopes: Sequence[Scope],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        if role not in {DeviceRole.HUB, DeviceRole.MEMBER, DeviceRole.SERVICE}:
            self._raise_error(
                code="invalid_role",
                message="CSR supported only for hub/member/service.",
                status_code=400,
            )

        def handler() -> tuple[Mapping[str, Any], int]:
            consent_id = str(generate_event_id())
            requested_node_id = node_id or str(generate_node_id())
            if role is DeviceRole.SERVICE:
                if node_id is None:
                    self._raise_error(
                        code="missing_node_id",
                        message="Subnet CA delegation requires an existing hub node id.",
                        status_code=400,
                    )
                hub_row = self._conn.execute(
                    "SELECT role, subnet_id FROM nodes WHERE id = ?",
                    (node_id,),
                ).fetchone()
                if hub_row is None or hub_row["role"] != DeviceRole.HUB.value or hub_row["subnet_id"] != subnet_id:
                    self._raise_error(
                        code="invalid_node",
                        message="Subnet CA delegation requires an active hub in the subnet.",
                        status_code=404,
                    )
                consent_type = ConsentType.DEVICE
            else:
                consent_type = ConsentType.MEMBER

            now_iso = self._now().isoformat()
            scopes_json = json.dumps([scope.value for scope in scopes])
            with self._conn:
                self._conn.execute(
                    "INSERT INTO consents(id, type, requester_id, subnet_id, scopes, status, created_at, updated_at, expires_at, owner_id) "
                    "VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL)",
                    (consent_id, consent_type.value, requested_node_id, subnet_id, scopes_json, now_iso, now_iso, now_iso),
                )
                self._conn.execute(
                    "INSERT INTO pending_csrs(consent_id, csr_pem, node_id, role, scopes, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (consent_id, csr_pem, requested_node_id, role.value, scopes_json, now_iso),
                )
                if role is not DeviceRole.SERVICE:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO nodes(id, role, subnet_id, pub_fingerprint, status, last_seen, created_at, updated_at) "
                        "VALUES(?, ?, ?, 'pending', 'pending', NULL, ?, ?)",
                        (requested_node_id, role.value, subnet_id, now_iso, now_iso),
                    )
            payload = {"consent_id": consent_id, "node_id": requested_node_id, "status": "pending"}
            return payload, 202

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path="/v1/hub/csr",
            principal_id=subnet_id,
            body={"role": role.value, "subnet_id": subnet_id, "node_id": node_id},
            handler=handler,
        )

    def resolve_consent(
        self,
        *,
        owner_device_id: str,
        consent_id: str,
        approve: bool,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        def handler() -> tuple[Mapping[str, Any], int]:
            consent = self._conn.execute("SELECT * FROM consents WHERE id = ?", (consent_id,)).fetchone()
            if consent is None:
                self._raise_error(code="unknown_consent", message="Consent not found.", status_code=404)
            if consent["status"] != "pending":
                return self._envelope({"consent_id": consent_id, "status": consent["status"]}), 200
            subnet_id = consent["subnet_id"]
            consent_type = ConsentType(consent["type"])
            required_scope = (
                Scope.MANAGE_MEMBERS if consent_type is ConsentType.MEMBER else Scope.MANAGE_IO_DEVICES
            )
            self._check_scope(actor_id=owner_device_id, required=required_scope, subnet_id=subnet_id)
            new_status = "approved" if approve else "denied"
            now_iso = self._now().isoformat()
            with self._conn:
                self._conn.execute(
                    "UPDATE consents SET status = ?, updated_at = ?, resolved_at = ?, owner_id = ? WHERE id = ?",
                    (new_status, now_iso, now_iso, owner_device_id, consent_id),
                )
            payload = {"consent_id": consent_id, "status": new_status}
            self._record_audit(
                subnet_id=subnet_id,
                actor_id=owner_device_id,
                subject_id=consent["requester_id"],
                action=f"consent.{new_status}",
                scopes=[Scope(scope) for scope in json.loads(consent["scopes"])],
                payload=payload,
            )
            if new_status == "approved":
                csr_row = self._conn.execute(
                    "SELECT * FROM pending_csrs WHERE consent_id = ?",
                    (consent_id,),
                ).fetchone()
                if csr_row:
                    ca = self._intermediate()
                    csr_role = DeviceRole(csr_row["role"])
                    csr_scopes = [Scope(scope) for scope in json.loads(csr_row["scopes"])]
                    lifetime = self._certificate_lifetime(csr_role)
                    if csr_role is DeviceRole.SERVICE:
                        cert_pem = ca.sign_subnet_ca(
                            csr_row["csr_pem"],
                            subnet_id=subnet_id,
                            hub_node_id=csr_row["node_id"],
                            scopes=csr_scopes,
                            lifetime=lifetime,
                        )
                    else:
                        cert_pem = ca.sign_end_entity(
                            csr_row["csr_pem"],
                            subnet_id=subnet_id,
                            node_id=csr_row["node_id"],
                            role=csr_role,
                            scopes=csr_scopes,
                            lifetime=lifetime,
                        )
                    chain = ca.chain_pem
                    not_after = IntermediateCertificateAuthority.certificate_not_after(cert_pem)
                    with self._conn:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO issued_certificates(consent_id, cert_pem, chain_pem, created_at) VALUES(?, ?, ?, ?)",
                            (consent_id, cert_pem, chain, now_iso),
                        )
                        if csr_role is DeviceRole.SERVICE:
                            self._conn.execute(
                                "INSERT OR REPLACE INTO subnet_ca_delegations(subnet_id, hub_node_id, cert_pem, chain_pem, issued_at, expires_at, offline) "
                                "VALUES(?, ?, ?, ?, ?, ?, 0)",
                                (
                                    subnet_id,
                                    csr_row["node_id"],
                                    cert_pem,
                                    chain,
                                    now_iso,
                                    not_after.isoformat(),
                                ),
                            )
                        else:
                            self._conn.execute(
                                "UPDATE nodes SET status = 'active', pub_fingerprint = ? WHERE id = ?",
                                (
                                    base64.urlsafe_b64encode(
                                        x509.load_pem_x509_certificate(cert_pem.encode("ascii")).fingerprint(hashes.SHA256())
                                    )
                                    .rstrip(b"=")
                                    .decode("ascii"),
                                    csr_row["node_id"],
                                ),
                            )
                    if csr_role is DeviceRole.HUB:
                        self._revoke_hub_channels(csr_row["node_id"])
            return self._envelope(payload), 200

        return self._with_idempotency(
            idempotency_key=idempotency_key,
            method="POST",
            path=f"/v1/consents/{consent_id}/resolve",
            principal_id=owner_device_id,
            body={"approve": approve},
            handler=handler,
        )

    def list_consents(
        self,
        *,
        owner_device_id: str,
        status: ConsentStatus = ConsentStatus.PENDING,
        consent_type: ConsentType | None = None,
    ) -> dict[str, Any]:
        device = self._fetch_device(owner_device_id)
        scopes = set(json.loads(device["scopes_json"]))
        allowed_types: set[str] = set()
        if Scope.MANAGE_MEMBERS.value in scopes:
            allowed_types.add(ConsentType.MEMBER.value)
        if Scope.MANAGE_IO_DEVICES.value in scopes:
            allowed_types.add(ConsentType.DEVICE.value)
        if consent_type is not None:
            if consent_type.value not in allowed_types:
                self._raise_error(
                    code="forbidden_scope",
                    message="Scope does not permit viewing this consent type.",
                    status_code=403,
                )
            types_filter = {consent_type.value}
        else:
            types_filter = allowed_types
        if not types_filter:
            self._raise_error(
                code="forbidden_scope",
                message="Device lacks required scopes for consent listing.",
                status_code=403,
            )
        placeholders = ",".join("?" for _ in types_filter)
        query = (
            f"SELECT * FROM consents WHERE subnet_id = ? AND status = ? AND type IN ({placeholders})"
        )
        params: list[Any] = [device["subnet_id"], status.value, *types_filter]
        query += " ORDER BY created_at"
        result_rows = self._conn.execute(query, params).fetchall()
        payload = [
            {
                "consent_id": row["id"],
                "type": row["type"],
                "requester_id": row["requester_id"],
                "subnet_id": row["subnet_id"],
                "scopes": json.loads(row["scopes"]),
                "status": row["status"],
                "created_at": row["created_at"],
            }
            for row in result_rows
        ]
        return self._envelope({"consents": payload})

    def collect_certificate(self, *, consent_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT cert_pem, chain_pem FROM issued_certificates WHERE consent_id = ?",
            (consent_id,),
        ).fetchone()
        if row is None:
            self._raise_error(code="csr_pending", message="Certificate not ready.", status_code=404)
        return self._envelope({"cert": row["cert_pem"], "chain": row["chain_pem"]})

    # ------------------------------------------------------------------
    # Hub channels and denylist helpers
    # ------------------------------------------------------------------
    def open_hub_channel(self, *, node_id: str, certificate_pem: str) -> dict[str, Any]:
        node = self._fetch_node(node_id)
        if node["status"] != "active":
            self._raise_error(code="node_inactive", message="Node is not active.", status_code=403)
        cert = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
        fingerprint = (
            base64.urlsafe_b64encode(cert.fingerprint(hashes.SHA256())).rstrip(b"=").decode("ascii")
        )
        if node.get("pub_fingerprint") != fingerprint:
            self._raise_error(code="certificate_mismatch", message="Certificate fingerprint mismatch.", status_code=403)
        channel_lifetime = int(self._lifetimes.get("channel_token_seconds", 300))
        token = secrets.token_urlsafe(48)
        now = self._now()
        expires_at = now + timedelta(seconds=channel_lifetime)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO hub_channels(node_id, token, subnet_id, expires_at, created_at, rotated_at, revoked) "
                "VALUES(?, ?, ?, ?, ?, ?, 0)",
                (
                    node_id,
                    token,
                    node["subnet_id"],
                    expires_at.isoformat(),
                    now.isoformat(),
                    None,
                ),
            )
        return self._envelope(
            {
                "node_id": node_id,
                "subnet_id": node["subnet_id"],
                "channel_token": token,
                "expires_at": expires_at.isoformat(),
            }
        )

    def rotate_hub_channel(self, *, node_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT token, subnet_id, expires_at, revoked FROM hub_channels WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            self._raise_error(code="channel_unknown", message="Channel not initialised.", status_code=404)
        if int(row["revoked"]) == 1:
            self._raise_error(code="channel_revoked", message="Channel revoked.", status_code=403)
        channel_lifetime = int(self._lifetimes.get("channel_token_seconds", 300))
        now = self._now()
        if datetime.fromisoformat(row["expires_at"]) < now:
            self._raise_error(code="channel_expired", message="Channel expired.", status_code=401)
        token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(seconds=channel_lifetime)
        with self._conn:
            self._conn.execute(
                "UPDATE hub_channels SET token = ?, expires_at = ?, rotated_at = ?, revoked = 0 WHERE node_id = ?",
                (token, expires_at.isoformat(), now.isoformat(), node_id),
            )
        return self._envelope(
            {
                "node_id": node_id,
                "subnet_id": row["subnet_id"],
                "channel_token": token,
                "expires_at": expires_at.isoformat(),
            }
        )

    def verify_hub_channel(self, *, node_id: str, token: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT token, subnet_id, expires_at, revoked FROM hub_channels WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None or row["token"] != token:
            self._raise_error(code="channel_unknown", message="Channel token invalid.", status_code=401)
        if int(row["revoked"]) == 1:
            self._raise_error(code="channel_revoked", message="Channel revoked.", status_code=403)
        if datetime.fromisoformat(row["expires_at"]) < self._now():
            self._raise_error(code="channel_expired", message="Channel expired.", status_code=401)
        return self._envelope(
            {
                "node_id": node_id,
                "subnet_id": row["subnet_id"],
                "expires_at": row["expires_at"],
            }
        )

    def fetch_denylist(self, *, subnet_id: str) -> dict[str, Any]:
        now_iso = self._now().isoformat()
        rows = self._conn.execute(
            "SELECT entity_type, entity_id, reason, created_at, expires_at FROM denylist WHERE subnet_id = ?",
            (subnet_id,),
        ).fetchall()
        payload = [
            {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "reason": row["reason"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "active": row["expires_at"] is None or row["expires_at"] > now_iso,
            }
            for row in rows
        ]
        return self._envelope({"entries": payload})

    # ------------------------------------------------------------------
    # Audit export
    # ------------------------------------------------------------------
    def export_audit(self, *, subnet_id: str | None = None) -> AuditExport:
        query = "SELECT payload_json, signature FROM audit_records"
        params: list[Any] = []
        if subnet_id:
            query += " WHERE json_extract(payload_json, '$.subnet_id') = ?"
            params.append(subnet_id)
        rows = self._conn.execute(query, params).fetchall()
        records = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["signature"] = row["signature"]
            records.append(payload)
        export = AuditExport(records=records)
        if not export.verify(self._audit_key):
            self._raise_error(code="audit_corrupted", message="Audit signature verification failed.", status_code=500)
        return export

    # ------------------------------------------------------------------
    # Misc helpers exposed for tests
    # ------------------------------------------------------------------
    def fetch_device_code(self, device_code: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT device_code, user_code, subnet_id, origin, ip_hash, ua_hash FROM device_codes WHERE device_code = ?",
            (device_code,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    @property
    def audit_key(self) -> bytes:
        return self._audit_key

