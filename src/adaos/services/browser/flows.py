"""Simulated browser flows for device and owner registration using WebCrypto."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from adaos.services.root import ClientContext, DeviceRole, RootAuthorityBackend, Scope

__all__ = ["IndexedDBSimulator", "OwnerBrowserFlow", "BrowserIODeviceFlow", "thumbprint_for_key", "sign_assertion"]


def _b64url(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def thumbprint_for_key(private_key: ec.EllipticCurvePrivateKey) -> tuple[str, str]:
    public_key = private_key.public_key()
    numbers = public_key.public_numbers()
    jwk = {
        "crv": "P-256",
        "kty": "EC",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }
    serialized = json.dumps(jwk, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashes.Hash(hashes.SHA256())
    digest.update(serialized)
    thumb = _b64url(digest.finalize())
    pem = public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    return thumb, pem


def sign_assertion(private_key: ec.EllipticCurvePrivateKey, header: Mapping[str, object], payload: Mapping[str, object]) -> str:
    header_json = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    protected = _b64url(header_json)
    claims = _b64url(payload_json)
    message = f"{protected}.{claims}".encode("ascii")
    signature_der = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(signature_der)
    compact = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"{protected}.{claims}.{compact}"


class IndexedDBSimulator:
    """Lightweight key-value store used by flow simulations."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def put(self, key: str, value: str) -> None:
        self._data[key] = value

    def get(self, key: str) -> str | None:
        return self._data.get(key)


@dataclass
class OwnerBrowserFlow:
    backend: RootAuthorityBackend
    subnet_id: str
    owner_device_id: str
    context: ClientContext
    storage: IndexedDBSimulator = field(default_factory=IndexedDBSimulator)

    def __post_init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.thumbprint, self.public_pem = thumbprint_for_key(self._key)

    def _store_channel(self, payload: Mapping[str, object]) -> None:
        token = payload.get("channel_token")
        if isinstance(token, str):
            self.storage.put("channel_token", token)
        expires = payload.get("channel_expires_at")
        if isinstance(expires, str):
            self.storage.put("channel_expires_at", expires)
        issued = payload.get("server_time_utc")
        if isinstance(issued, str):
            self.storage.put("channel_issued_at", issued)
        else:
            self.storage.put("channel_issued_at", datetime.now(timezone.utc).isoformat())

    def start(self, scopes: Sequence[Scope], *, idempotency_key: str) -> Mapping[str, object]:
        response = self.backend.device_start(
            subnet_id=self.subnet_id,
            role=DeviceRole.OWNER,
            scopes=scopes,
            context=self.context,
            jwk_thumb=self.thumbprint,
            public_key_pem=self.public_pem,
            idempotency_key=idempotency_key,
        )
        self.storage.put("device_code", response["device_code"])
        self.storage.put("user_code", response["user_code"])
        return response

    def approve(
        self,
        *,
        granted_scopes: Sequence[Scope],
        aliases: Sequence[str],
        capabilities: Sequence[str],
        idempotency_key: str,
    ) -> Mapping[str, object]:
        response = self.backend.device_confirm(
            owner_device_id=self.owner_device_id,
            user_code=self.storage.get("user_code") or "",
            granted_scopes=granted_scopes,
            jwk_thumb=self.thumbprint,
            public_key_pem=self.public_pem,
            aliases=aliases,
            capabilities=capabilities,
            context=self.context,
            idempotency_key=idempotency_key,
        )
        self.storage.put("device_id", response["device_id"])
        self.storage.put("subnet_id", response["subnet_id"])
        return response

    def _timestamp(self, minutes: int = 1) -> int:
        return int(datetime.now(tz=timezone.utc).timestamp()) + minutes * 60

    def poll_tokens(self, *, idempotency_key: str) -> Mapping[str, object]:
        device_code = self.storage.get("device_code") or ""
        payload = {
            "nonce": device_code,
            "aud": f"subnet:{self.subnet_id}",
            "exp": self._timestamp(),
            "cnf": {"jkt": self.thumbprint},
        }
        assertion = sign_assertion(self._key, {"alg": "ES256", "kid": self.thumbprint}, payload)
        tokens = self.backend.device_token(device_code=device_code, assertion=assertion, idempotency_key=idempotency_key)
        self.storage.put("access_token", tokens["access_token"])
        self.storage.put("refresh_token", tokens["refresh_token"])
        self._store_channel(tokens)
        return tokens

    def auth_session(self, *, audience: str, idempotency_key: str) -> Mapping[str, object]:
        device_id = self.storage.get("device_id") or ""
        challenge = self.backend.auth_challenge(device_id=device_id, audience=audience, idempotency_key=idempotency_key)
        payload = {
            "nonce": challenge["nonce"],
            "aud": challenge["aud"],
            "exp": challenge["exp"],
            "cnf": {"jkt": self.thumbprint},
        }
        assertion = sign_assertion(self._key, {"alg": "ES256", "kid": self.thumbprint}, payload)
        tokens = self.backend.auth_complete(device_id=device_id, assertion=assertion, idempotency_key=f"{idempotency_key}-complete")
        self.storage.put("session_token", tokens["access_token"])
        self._store_channel(tokens)
        return tokens

    def refresh(self) -> Mapping[str, object]:
        refresh_token = self.storage.get("refresh_token") or ""
        payload = {
            "nonce": refresh_token,
            "aud": f"subnet:{self.subnet_id}",
            "exp": self._timestamp(),
            "cnf": {"jkt": self.thumbprint},
        }
        assertion = sign_assertion(self._key, {"alg": "ES256", "kid": self.thumbprint}, payload)
        response = self.backend.token_refresh(refresh_token=refresh_token, assertion=assertion)
        self.storage.put("access_token", response["access_token"])
        self.storage.put("refresh_token", response["refresh_token"])
        self._store_channel(response)
        return response

    def ensure_channel(self, *, idempotency_key: str, hub_node_id: str | None = None) -> Mapping[str, object]:
        channel_token = self.storage.get("channel_token") or ""
        if not channel_token:
            raise RuntimeError("channel token unavailable")
        issued_raw = self.storage.get("channel_issued_at")
        expires_raw = self.storage.get("channel_expires_at")

        def _parse(value: str | None, fallback: datetime) -> datetime:
            if not value:
                return fallback
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return fallback

        now = datetime.now(timezone.utc)
        issued_at = _parse(issued_raw, now - timedelta(minutes=10))
        expires_at = _parse(expires_raw, now)
        total = expires_at - issued_at
        ratio = 1.0 if total.total_seconds() <= 0 else (now - issued_at) / total
        if ratio < 0.8:
            return {
                "channel_token": channel_token,
                "channel_expires_at": expires_raw,
                "rotated": False,
            }

        audience = f"wss:subnet:{self.subnet_id}"
        if hub_node_id:
            audience = f"{audience}|hub:{hub_node_id}"
        payload = {
            "nonce": channel_token,
            "aud": audience,
            "exp": self._timestamp(),
            "cnf": {"jkt": self.thumbprint},
        }
        assertion = sign_assertion(self._key, {"alg": "ES256", "kid": self.thumbprint}, payload)
        response = self.backend.authorize_channel(
            device_id=self.storage.get("device_id") or "",
            channel_token=channel_token,
            assertion=assertion,
            hub_node_id=hub_node_id,
            idempotency_key=idempotency_key,
        )
        self._store_channel(response)
        return response


@dataclass
class BrowserIODeviceFlow:
    backend: RootAuthorityBackend
    subnet_id: str
    context: ClientContext
    storage: IndexedDBSimulator = field(default_factory=IndexedDBSimulator)

    def __post_init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.thumbprint, self.public_pem = thumbprint_for_key(self._key)

    def _store_channel(self, payload: Mapping[str, object]) -> None:
        token = payload.get("channel_token")
        if isinstance(token, str):
            self.storage.put("channel_token", token)
        expires = payload.get("channel_expires_at")
        if isinstance(expires, str):
            self.storage.put("channel_expires_at", expires)
        issued = payload.get("server_time_utc")
        if isinstance(issued, str):
            self.storage.put("channel_issued_at", issued)
        else:
            self.storage.put("channel_issued_at", datetime.now(timezone.utc).isoformat())

    def begin(self, scopes: Sequence[Scope], *, idempotency_key: str) -> Mapping[str, object]:
        response = self.backend.qr_start(
            subnet_id=self.subnet_id,
            scopes=scopes,
            context=self.context,
            init_thumb=self.thumbprint,
            idempotency_key=idempotency_key,
        )
        self.storage.put("session_id", response["session_id"])
        self.storage.put("nonce", response["nonce"])
        return response

    def approve(
        self,
        *,
        owner_device_id: str,
        scopes: Sequence[Scope],
        idempotency_key: str,
    ) -> Mapping[str, object]:
        approval = self.backend.qr_approve(
            owner_device_id=owner_device_id,
            session_id=self.storage.get("session_id") or "",
            jwk_thumb=self.thumbprint,
            public_key_pem=self.public_pem,
            scopes=scopes,
            context=self.context,
            idempotency_key=idempotency_key,
        )
        self.storage.put("device_id", approval["device_id"])
        return approval

    def complete_auth(self, *, idempotency_key: str, audience: str) -> Mapping[str, object]:
        device_id = self.storage.get("device_id") or ""
        challenge = self.backend.auth_challenge(device_id=device_id, audience=audience, idempotency_key=idempotency_key)
        payload = {
            "nonce": challenge["nonce"],
            "aud": challenge["aud"],
            "exp": challenge["exp"],
            "cnf": {"jkt": self.thumbprint},
        }
        assertion = sign_assertion(self._key, {"alg": "ES256", "kid": self.thumbprint}, payload)
        response = self.backend.auth_complete(
            device_id=device_id,
            assertion=assertion,
            idempotency_key=f"{idempotency_key}-complete",
        )
        self._store_channel(response)
        return response

    def ensure_channel(self, *, idempotency_key: str, hub_node_id: str | None = None) -> Mapping[str, object]:
        channel_token = self.storage.get("channel_token") or ""
        if not channel_token:
            raise RuntimeError("channel token unavailable")

        def _parse(value: str | None, fallback: datetime) -> datetime:
            if not value:
                return fallback
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return fallback

        now = datetime.now(timezone.utc)
        issued_at = _parse(self.storage.get("channel_issued_at"), now - timedelta(minutes=10))
        expires_at = _parse(self.storage.get("channel_expires_at"), now)
        total = expires_at - issued_at
        ratio = 1.0 if total.total_seconds() <= 0 else (now - issued_at) / total
        if ratio < 0.8:
            return {
                "channel_token": channel_token,
                "channel_expires_at": self.storage.get("channel_expires_at"),
                "rotated": False,
            }

        audience = f"wss:subnet:{self.subnet_id}"
        if hub_node_id:
            audience = f"{audience}|hub:{hub_node_id}"
        payload = {
            "nonce": channel_token,
            "aud": audience,
            "exp": int((now + timedelta(minutes=1)).timestamp()),
            "cnf": {"jkt": self.thumbprint},
        }
        assertion = sign_assertion(self._key, {"alg": "ES256", "kid": self.thumbprint}, payload)
        response = self.backend.authorize_channel(
            device_id=self.storage.get("device_id") or "",
            channel_token=channel_token,
            assertion=assertion,
            hub_node_id=hub_node_id,
            idempotency_key=idempotency_key,
        )
        self._store_channel(response)
        return response
