"""In-memory implementation of the AdaOS root authorization flows.

The :class:`RootAuthorityBackend` class implements the major flows outlined in
the MVP backlog.  It is intentionally lightweight and designed for integration
tests as well as early development.  The backend keeps all state in memory
while enforcing TTLs, device revocation, short lived credentials and
certificate issuance for hubs and members.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import json
import secrets
from typing import Dict, Iterable, List, MutableMapping, Sequence

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from .enums import ConsentStatus, ConsentType, DeviceRole, Scope
from .ids import (
    DeviceId,
    NodeId,
    SubnetId,
    generate_device_id,
    generate_event_id,
    generate_node_id,
    generate_subnet_id,
    generate_trace_id,
)
from .models import AuditEvent, ConsentRequest, DeviceRecord, NodeRecord, SubnetRecord

__all__ = ["RootAuthorityBackend", "RootBackendError", "TokenBundle"]


class RootBackendError(RuntimeError):
    """Exception raised when a root backend flow fails."""

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code or "backend_error"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _generate_code(length: int = 32) -> str:
    return secrets.token_urlsafe(length)[:length]


@dataclass(slots=True)
class TokenBundle:
    device_id: DeviceId
    subnet_id: SubnetId
    scopes: Sequence[Scope]
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "subnet_id": self.subnet_id,
            "scopes": [scope.value for scope in self.scopes],
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.access_expires_at.isoformat(),
        }


@dataclass(slots=True)
class DeviceFlow:
    user_code: str
    device_code: str
    expires_at: datetime
    role: DeviceRole
    subnet_id: SubnetId
    scopes_requested: Sequence[Scope]
    status: ConsentStatus = ConsentStatus.PENDING
    approved_scopes: Sequence[Scope] = field(default_factory=tuple)
    device_id: DeviceId | None = None
    jwk_thumbprint: str | None = None
    public_key_pem: str | None = None
    nonce: str | None = None

    def is_expired(self, *, moment: datetime | None = None) -> bool:
        if moment is None:
            moment = _utcnow()
        return moment >= self.expires_at


@dataclass(slots=True)
class BrowserChallenge:
    nonce: str
    aud: str
    exp: datetime


@dataclass(slots=True)
class PendingCsr:
    consent_id: str
    csr_pem: str
    subnet_id: SubnetId
    role: DeviceRole
    node_id: NodeId
    scopes: Sequence[Scope]


class CertificateAuthority:
    """Minimal CA able to sign hub and member certificates."""

    def __init__(self, *, common_name: str = "AdaOS Root CA", lifetime: timedelta | None = None) -> None:
        lifetime = lifetime or timedelta(days=365)
        self._key = ec.generate_private_key(ec.SECP256R1())
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        now = _utcnow()
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + lifetime)
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        )
        self._cert = builder.sign(private_key=self._key, algorithm=hashes.SHA256())

    @property
    def certificate_pem(self) -> str:
        return self._cert.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def sign_csr(
        self,
        csr_pem: str,
        *,
        subnet_id: SubnetId,
        node_id: NodeId,
        role: DeviceRole,
        scopes: Sequence[Scope],
        lifetime: timedelta | None = None,
    ) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode("ascii"))
        lifetime = lifetime or timedelta(days=90)
        now = _utcnow()
        builder = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self._cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + lifetime)
        )
        san_values = [
            x509.UniformResourceIdentifier(f"urn:adaos:role={role.value}"),
            x509.UniformResourceIdentifier(f"urn:adaos:subnet={subnet_id}"),
            x509.UniformResourceIdentifier(f"urn:adaos:node={node_id}"),
        ]
        for scope in scopes:
            san_values.append(x509.UniformResourceIdentifier(f"urn:adaos:scope={scope.value}"))
        builder = builder.add_extension(x509.SubjectAlternativeName(san_values), critical=False)
        adaos_scope_oid = ObjectIdentifier("1.3.6.1.4.1.57264.1")
        builder = builder.add_extension(
            x509.UnrecognizedExtension(
                adaos_scope_oid,
                json.dumps({"scopes": [scope.value for scope in scopes]}).encode("utf-8"),
            ),
            critical=False,
        )
        cert = builder.sign(private_key=self._key, algorithm=hashes.SHA256())
        return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


class RootAuthorityBackend:
    """Implements device registration, consent, and token issuance flows."""

    def __init__(
        self,
        *,
        access_ttl: timedelta | None = None,
        refresh_ttl: timedelta | None = None,
        device_flow_ttl: timedelta | None = None,
        qr_ttl: timedelta | None = None,
    ) -> None:
        self.access_ttl = access_ttl or timedelta(minutes=10)
        self.refresh_ttl = refresh_ttl or timedelta(hours=12)
        self.device_flow_ttl = device_flow_ttl or timedelta(minutes=10)
        self.qr_ttl = qr_ttl or timedelta(minutes=5)
        self._subnets: Dict[SubnetId, SubnetRecord] = {}
        self._devices: Dict[DeviceId, DeviceRecord] = {}
        self._nodes: Dict[NodeId, NodeRecord] = {}
        self._consents: Dict[str, ConsentRequest] = {}
        self._device_flows: Dict[str, DeviceFlow] = {}
        self._device_by_user_code: Dict[str, DeviceFlow] = {}
        self._browser_challenges: Dict[DeviceId, BrowserChallenge] = {}
        self._qr_sessions: Dict[str, DeviceFlow] = {}
        self._tokens: Dict[str, TokenBundle] = {}
        self._refresh_index: Dict[str, TokenBundle] = {}
        self._denylisted_devices: set[DeviceId] = set()
        self._revoked_tokens: Dict[str, DeviceId] = {}
        self._audit_log: List[AuditEvent] = []
        self._pending_csrs: Dict[str, PendingCsr] = {}
        self._issued_certificates: Dict[str, tuple[str, str]] = {}
        self._ca = CertificateAuthority()

    # ------------------------------------------------------------------ helpers
    def _record_audit(
        self,
        *,
        subnet_id: SubnetId,
        actor_id: DeviceId | None,
        subject_id: DeviceId | NodeId | SubnetId | None,
        action: str,
        scopes: Sequence[Scope],
        payload: dict[str, object],
    ) -> None:
        event = AuditEvent(
            id=generate_event_id(),
            trace_id=generate_trace_id(),
            subnet_id=subnet_id,
            actor_id=actor_id,
            subject_id=subject_id,
            action=action,
            acl=scopes,
            ttl=timedelta(days=90),
            payload=payload,
        )
        self._audit_log.append(event)

    def _issue_tokens(self, device: DeviceRecord) -> TokenBundle:
        now = _utcnow()
        access_expires = now + self.access_ttl
        refresh_expires = now + self.refresh_ttl
        bundle = TokenBundle(
            device_id=device.id,
            subnet_id=device.subnet_id,
            scopes=tuple(device.scopes),
            access_token=_generate_code(48),
            refresh_token=_generate_code(64),
            access_expires_at=access_expires,
            refresh_expires_at=refresh_expires,
        )
        self._tokens[bundle.access_token] = bundle
        self._refresh_index[bundle.refresh_token] = bundle
        return bundle

    def _ensure_subnet(self, subnet_id: SubnetId, owner_id: DeviceId) -> SubnetRecord:
        subnet = self._subnets.get(subnet_id)
        if subnet is None:
            subnet = SubnetRecord(id=subnet_id, owner_id=owner_id)
            self._subnets[subnet_id] = subnet
        return subnet

    def _require_device(self, device_id: DeviceId) -> DeviceRecord:
        try:
            device = self._devices[device_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise RootBackendError(f"device {device_id} not found", error_code="unknown_device") from exc
        if device.revoked:
            raise RootBackendError("device revoked", error_code="device_revoked")
        return device

    # ----------------------------------------------------------------- ownership
    def register_owner(self, *, subnet_id: SubnetId | None = None, owner_id: DeviceId | None = None) -> SubnetRecord:
        subnet_id = subnet_id or generate_subnet_id()
        owner_id = owner_id or generate_device_id()
        subnet = self._ensure_subnet(subnet_id, owner_id)
        if owner_id not in self._devices:
            owner_device = DeviceRecord(id=owner_id, role=DeviceRole.OWNER, subnet_id=subnet_id)
            owner_device.grant_scopes([scope for scope in Scope])
            self._devices[owner_id] = owner_device
        return subnet

    # ------------------------------------------------------------- device flows
    def start_device_flow(
        self,
        *,
        requester_role: DeviceRole,
        subnet_id: SubnetId,
        scopes_requested: Iterable[Scope],
    ) -> dict[str, object]:
        user_code = _generate_code(8).upper()
        device_code = _generate_code(40)
        flow = DeviceFlow(
            user_code=user_code,
            device_code=device_code,
            expires_at=_utcnow() + self.device_flow_ttl,
            role=requester_role,
            subnet_id=subnet_id,
            scopes_requested=tuple(scopes_requested),
        )
        self._device_flows[device_code] = flow
        self._device_by_user_code[user_code] = flow
        return {
            "user_code": user_code,
            "device_code": device_code,
            "interval": 5,
            "expires_in": int(self.device_flow_ttl.total_seconds()),
        }

    def confirm_device_flow(
        self,
        *,
        owner_id: DeviceId,
        user_code: str,
        scopes_granted: Iterable[Scope],
        jwk_thumbprint: str,
        public_key_pem: str | None,
        aliases: Iterable[str] | None = None,
        capabilities: Iterable[str] | None = None,
    ) -> DeviceRecord:
        flow = self._device_by_user_code.get(user_code)
        if flow is None or flow.is_expired():
            raise RootBackendError("invalid or expired user_code", error_code="expired_device_code")
        subnet = self._ensure_subnet(flow.subnet_id, owner_id)
        if subnet.owner_id != owner_id:
            raise RootBackendError("owner does not control subnet", error_code="forbidden")
        if flow.status is not ConsentStatus.PENDING:
            raise RootBackendError("device flow already resolved", error_code="already_approved")
        device_id = flow.device_id or generate_device_id()
        device = DeviceRecord(
            id=device_id,
            role=flow.role,
            subnet_id=subnet.id,
            jwk_thumbprint=jwk_thumbprint,
            public_key_pem=public_key_pem,
        )
        device.grant_scopes(scopes_granted)
        if aliases:
            for alias in aliases:
                device.add_alias(alias)
        if capabilities:
            device.capabilities.extend(capabilities)
        self._devices[device_id] = device
        flow.device_id = device_id
        flow.status = ConsentStatus.APPROVED
        flow.approved_scopes = tuple(device.scopes)
        flow.jwk_thumbprint = jwk_thumbprint
        flow.public_key_pem = public_key_pem
        self._record_audit(
            subnet_id=subnet.id,
            actor_id=owner_id,
            subject_id=device_id,
            action="device.approved",
            scopes=device.scopes,
            payload={"user_code": user_code, "role": device.role.value},
        )
        return device

    def deny_device_flow(self, *, owner_id: DeviceId, user_code: str) -> None:
        flow = self._device_by_user_code.get(user_code)
        if flow is None:
            raise RootBackendError("unknown user_code", error_code="unknown_request")
        subnet = self._ensure_subnet(flow.subnet_id, owner_id)
        if subnet.owner_id != owner_id:
            raise RootBackendError("owner does not control subnet", error_code="forbidden")
        flow.status = ConsentStatus.DENIED
        self._record_audit(
            subnet_id=subnet.id,
            actor_id=owner_id,
            subject_id=None,
            action="device.denied",
            scopes=tuple(flow.scopes_requested),
            payload={"user_code": user_code},
        )

    def poll_device_tokens(self, *, device_code: str) -> dict[str, object]:
        flow = self._device_flows.get(device_code)
        if flow is None:
            raise RootBackendError("device_code not found", error_code="expired_device_code")
        if flow.is_expired():
            del self._device_flows[device_code]
            self._device_by_user_code.pop(flow.user_code, None)
            raise RootBackendError("device_code expired", error_code="expired_device_code")
        if flow.status is ConsentStatus.PENDING:
            raise RootBackendError("authorization pending", error_code="authorization_pending")
        if flow.status is ConsentStatus.DENIED:
            raise RootBackendError("authorization denied", error_code="access_denied")
        device = self._require_device(flow.device_id)
        bundle = self._issue_tokens(device)
        self._device_flows.pop(device_code, None)
        self._device_by_user_code.pop(flow.user_code, None)
        return {
            **bundle.as_dict(),
            "owner_id": self._subnets[device.subnet_id].owner_id,
        }

    # --------------------------------------------------------------- QR sessions
    def start_qr_session(
        self,
        *,
        subnet_id: SubnetId,
        scopes_requested: Iterable[Scope],
    ) -> dict[str, object]:
        session_id = _generate_code(24)
        nonce = _generate_code(12)
        flow = DeviceFlow(
            user_code=session_id,
            device_code=session_id,
            expires_at=_utcnow() + self.qr_ttl,
            role=DeviceRole.BROWSER_IO,
            subnet_id=subnet_id,
            scopes_requested=tuple(scopes_requested),
            nonce=nonce,
        )
        self._qr_sessions[session_id] = flow
        return {
            "session_id": session_id,
            "nonce": nonce,
            "ttl": int(self.qr_ttl.total_seconds()),
        }

    def approve_qr_session(
        self,
        *,
        owner_id: DeviceId,
        session_id: str,
        jwk_thumbprint: str,
        public_key_pem: str | None,
        scopes_granted: Iterable[Scope],
    ) -> DeviceRecord:
        flow = self._qr_sessions.get(session_id)
        if flow is None or flow.is_expired():
            raise RootBackendError("invalid qr session", error_code="expired_session")
        subnet = self._ensure_subnet(flow.subnet_id, owner_id)
        if subnet.owner_id != owner_id:
            raise RootBackendError("owner mismatch", error_code="forbidden")
        device = DeviceRecord(
            id=generate_device_id(),
            role=DeviceRole.BROWSER_IO,
            subnet_id=subnet.id,
            jwk_thumbprint=jwk_thumbprint,
            public_key_pem=public_key_pem,
        )
        device.grant_scopes(scopes_granted)
        self._devices[device.id] = device
        flow.device_id = device.id
        flow.status = ConsentStatus.APPROVED
        flow.approved_scopes = tuple(device.scopes)
        self._record_audit(
            subnet_id=subnet.id,
            actor_id=owner_id,
            subject_id=device.id,
            action="qr.approved",
            scopes=device.scopes,
            payload={"session_id": session_id},
        )
        self._qr_sessions.pop(session_id, None)
        return device

    # --------------------------------------------------------- browser challenge
    def start_browser_challenge(self, *, device_id: DeviceId, audience: str) -> dict[str, object]:
        device = self._require_device(device_id)
        nonce = _generate_code(24)
        expiry = _utcnow() + timedelta(minutes=2)
        challenge = BrowserChallenge(nonce=nonce, aud=audience, exp=expiry)
        self._browser_challenges[device_id] = challenge
        return {"nonce": nonce, "aud": audience, "exp": expiry.isoformat()}

    def complete_browser_challenge(self, *, device_id: DeviceId, assertion: str) -> dict[str, object]:
        device = self._require_device(device_id)
        challenge = self._browser_challenges.get(device_id)
        if challenge is None or challenge.exp < _utcnow():
            raise RootBackendError("challenge expired", error_code="challenge_expired")
        if not device.public_key_pem:
            raise RootBackendError("device has no registered key", error_code="missing_key")
        try:
            header_b64, payload_b64, signature_b64 = assertion.split(".")
        except ValueError as exc:  # pragma: no cover - defensive
            raise RootBackendError("invalid assertion", error_code="invalid_assertion") from exc
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if header.get("alg") != "ES256":
            raise RootBackendError("unsupported algorithm", error_code="invalid_algorithm")
        if payload.get("nonce") != challenge.nonce or payload.get("aud") != challenge.aud:
            raise RootBackendError("challenge mismatch", error_code="challenge_mismatch")
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            raise RootBackendError("missing exp", error_code="invalid_assertion")
        if _utcnow() >= datetime.fromtimestamp(exp, tz=timezone.utc):
            raise RootBackendError("assertion expired", error_code="assertion_expired")
        signature = _b64url_decode(signature_b64)
        public_key = serialization.load_pem_public_key(device.public_key_pem.encode("ascii"))
        try:
            public_key.verify(
                signature,
                f"{header_b64}.{payload_b64}".encode("ascii"),
                ec.ECDSA(hashes.SHA256()),
            )
        except InvalidSignature as exc:
            raise RootBackendError("invalid signature", error_code="invalid_assertion") from exc
        bundle = self._issue_tokens(device)
        self._browser_challenges.pop(device_id, None)
        self._record_audit(
            subnet_id=device.subnet_id,
            actor_id=device.id,
            subject_id=device.id,
            action="browser.authenticated",
            scopes=device.scopes,
            payload={"aud": challenge.aud},
        )
        return bundle.as_dict()

    # --------------------------------------------------------- token management
    def refresh_token(self, *, refresh_token: str) -> dict[str, object]:
        bundle = self._refresh_index.get(refresh_token)
        if bundle is None:
            raise RootBackendError("refresh token invalid", error_code="invalid_refresh")
        if bundle.refresh_expires_at < _utcnow():
            raise RootBackendError("refresh token expired", error_code="invalid_refresh")
        device = self._require_device(bundle.device_id)
        # revoke old tokens
        self._tokens.pop(bundle.access_token, None)
        self._refresh_index.pop(bundle.refresh_token, None)
        new_bundle = self._issue_tokens(device)
        return new_bundle.as_dict()

    def revoke_device(self, *, device_id: DeviceId, reason: str | None = None) -> None:
        device = self._require_device(device_id)
        device.revoked = True
        self._denylisted_devices.add(device_id)
        to_remove = [token for token, bundle in self._tokens.items() if bundle.device_id == device_id]
        for token in to_remove:
            bundle = self._tokens.pop(token)
            self._refresh_index.pop(bundle.refresh_token, None)
            self._revoked_tokens[token] = device_id
        self._record_audit(
            subnet_id=device.subnet_id,
            actor_id=None,
            subject_id=device_id,
            action="device.revoked",
            scopes=device.scopes,
            payload={"reason": reason or "unspecified"},
        )

    def rotate_device_key(
        self,
        *,
        device_id: DeviceId,
        jwk_thumbprint: str,
        public_key_pem: str | None = None,
    ) -> DeviceRecord:
        device = self._require_device(device_id)
        device.rotate_key(jwk_thumbprint=jwk_thumbprint, public_key_pem=public_key_pem)
        self._record_audit(
            subnet_id=device.subnet_id,
            actor_id=device_id,
            subject_id=device_id,
            action="device.rotated",
            scopes=device.scopes,
            payload={"thumbprint": jwk_thumbprint},
        )
        return device

    def introspect_token(self, *, token: str) -> TokenBundle:
        bundle = self._tokens.get(token)
        if bundle is None:
            if token in self._revoked_tokens:
                raise RootBackendError("device revoked", error_code="device_revoked")
            raise RootBackendError("token unknown", error_code="invalid_token")
        if bundle.access_expires_at < _utcnow():
            self._tokens.pop(token, None)
            raise RootBackendError("token expired", error_code="invalid_token")
        if bundle.device_id in self._denylisted_devices:
            raise RootBackendError("device revoked", error_code="device_revoked")
        return bundle

    # ------------------------------------------------------------- consent flows
    def create_consent(
        self,
        *,
        consent_type: ConsentType,
        requester_id: DeviceId,
        subnet_id: SubnetId,
        scopes_requested: Iterable[Scope],
        ttl: timedelta | None = None,
    ) -> ConsentRequest:
        consent = ConsentRequest(
            id=str(generate_event_id()),
            consent_type=consent_type,
            requester_id=requester_id,
            subnet_id=subnet_id,
            scopes_requested=list(scopes_requested),
            ttl=ttl or timedelta(minutes=20),
        )
        self._consents[consent.id] = consent
        return consent

    def list_consents(self, *, subnet_id: SubnetId | None = None) -> List[ConsentRequest]:
        if subnet_id is None:
            return list(self._consents.values())
        return [c for c in self._consents.values() if c.subnet_id == subnet_id]

    def resolve_consent(self, *, consent_id: str, owner_id: DeviceId, approve: bool) -> ConsentRequest:
        consent = self._consents.get(consent_id)
        if consent is None:
            raise RootBackendError("consent not found", error_code="unknown_consent")
        subnet = self._ensure_subnet(consent.subnet_id, owner_id)
        if subnet.owner_id != owner_id:
            raise RootBackendError("owner mismatch", error_code="forbidden")
        consent.status = ConsentStatus.APPROVED if approve else ConsentStatus.DENIED
        self._record_audit(
            subnet_id=subnet.id,
            actor_id=owner_id,
            subject_id=consent.requester_id,
            action=f"consent.{consent.status.value}",
            scopes=tuple(consent.scopes_requested),
            payload={"consent_id": consent.id, "type": consent.consent_type.value},
        )
        return consent

    # --------------------------------------------------------------- CSR handling
    def submit_csr(
        self,
        *,
        csr_pem: str,
        role: DeviceRole,
        subnet_id: SubnetId,
        node_id: NodeId | None = None,
        scopes: Iterable[Scope],
    ) -> ConsentRequest:
        if role not in {DeviceRole.HUB, DeviceRole.MEMBER}:
            raise RootBackendError("csr only supported for hub/member", error_code="invalid_role")
        node_id = node_id or generate_node_id()
        requestor = generate_device_id()
        consent = self.create_consent(
            consent_type=ConsentType.MEMBER,
            requester_id=requestor,
            subnet_id=subnet_id,
            scopes_requested=scopes,
        )
        self._pending_csrs[consent.id] = PendingCsr(
            consent_id=consent.id,
            csr_pem=csr_pem,
            subnet_id=subnet_id,
            role=role,
            node_id=node_id,
            scopes=tuple(scopes),
        )
        node = NodeRecord(
            id=node_id,
            role=role,
            subnet_id=subnet_id,
            pub_fingerprint="pending",
            status="pending",
        )
        self._nodes[node_id] = node
        return consent

    def finalize_csr(self, *, consent_id: str) -> dict[str, str]:
        csr = self._pending_csrs.get(consent_id)
        if csr is None:
            raise RootBackendError("csr not found", error_code="unknown_csr")
        consent = self._consents.get(consent_id)
        if consent is None or consent.status is not ConsentStatus.APPROVED:
            raise RootBackendError("csr not approved", error_code="csr_pending")
        cert_pem = self._ca.sign_csr(
            csr.csr_pem,
            subnet_id=csr.subnet_id,
            node_id=csr.node_id,
            role=csr.role,
            scopes=csr.scopes,
        )
        chain = self._ca.certificate_pem
        self._issued_certificates[consent_id] = (cert_pem, chain)
        node = self._nodes[csr.node_id]
        node.status = "active"
        cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
        node.pub_fingerprint = _b64url(cert.fingerprint(hashes.SHA256()))
        return {"cert": cert_pem, "chain": chain}

    def collect_certificate(self, *, consent_id: str) -> dict[str, str]:
        try:
            cert, chain = self._issued_certificates[consent_id]
        except KeyError as exc:
            raise RootBackendError("certificate not ready", error_code="csr_pending") from exc
        return {"cert": cert, "chain": chain}

    # -------------------------------------------------------------- misc helpers
    @property
    def audit_log(self) -> Sequence[AuditEvent]:
        return tuple(self._audit_log)

    @property
    def devices(self) -> MutableMapping[DeviceId, DeviceRecord]:
        return self._devices

    @property
    def tokens(self) -> MutableMapping[str, TokenBundle]:
        return self._tokens

