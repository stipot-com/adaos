from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from adaos.services.node_config import NodeConfig, RootOwnerProfile, ensure_hub, save_node

from .client import RootHttpClient, RootHttpError
from .keyring import KeyringUnavailableError, delete_refresh, load_refresh, save_refresh
from adaos.adapters.db import sqlite as sqlite_db
from adaos.apps.api.auth import require_owner_token
from adaos.services.id_gen import new_id

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization



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


__all__ = ["RootAuthService", "OwnerHubsService", "PkiService", "RootAuthError"]
