"""SQLite persistence helpers for the AdaOS root service."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final

__all__ = ["SQLitePersistence"]


class SQLitePersistence:
    """Lightweight wrapper that initialises the required SQLite schema."""

    _SCHEMA: Final[str] = """
    PRAGMA journal_mode=WAL;
    PRAGMA foreign_keys=ON;

    CREATE TABLE IF NOT EXISTS subnets (
        id TEXT PRIMARY KEY,
        owner_device_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        settings_json TEXT
    );

    CREATE TABLE IF NOT EXISTS nodes (
        id TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        subnet_id TEXT NOT NULL,
        pub_fingerprint TEXT,
        status TEXT NOT NULL,
        last_seen TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY,
        role TEXT NOT NULL,
        subnet_id TEXT NOT NULL,
        node_id TEXT,
        jwk_thumb TEXT,
        public_key_pem TEXT,
        aliases_json TEXT NOT NULL,
        capabilities_json TEXT NOT NULL,
        scopes_json TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT
    );

    CREATE TABLE IF NOT EXISTS device_aliases (
        alias_slug TEXT NOT NULL,
        display TEXT NOT NULL,
        subnet_id TEXT NOT NULL,
        device_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (alias_slug, subnet_id)
    );

    CREATE TABLE IF NOT EXISTS consents (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        requester_id TEXT NOT NULL,
        subnet_id TEXT NOT NULL,
        scopes TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT,
        resolved_at TEXT,
        owner_id TEXT
    );

    CREATE TABLE IF NOT EXISTS pending_csrs (
        consent_id TEXT PRIMARY KEY,
        csr_pem TEXT NOT NULL,
        node_id TEXT NOT NULL,
        role TEXT NOT NULL,
        scopes TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS idempotency_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT NOT NULL,
        method TEXT NOT NULL,
        path TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        body_hash TEXT NOT NULL,
        response_json TEXT,
        status_code INTEGER,
        headers_json TEXT,
        event_id TEXT,
        server_time_utc TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        committed INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS denylist (
        id TEXT PRIMARY KEY,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        subnet_id TEXT NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT
    );

    CREATE TABLE IF NOT EXISTS audit_records (
        id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        signature TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS issued_certificates (
        consent_id TEXT PRIMARY KEY,
        cert_pem TEXT NOT NULL,
        chain_pem TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS certificate_authorities (
        name TEXT PRIMARY KEY,
        key_pem TEXT NOT NULL,
        cert_pem TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS subnet_ca_delegations (
        subnet_id TEXT NOT NULL,
        hub_node_id TEXT NOT NULL,
        cert_pem TEXT NOT NULL,
        chain_pem TEXT NOT NULL,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        offline INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (subnet_id, hub_node_id)
    );

    CREATE TABLE IF NOT EXISTS qr_sessions (
        session_id TEXT PRIMARY KEY,
        subnet_id TEXT NOT NULL,
        nonce TEXT NOT NULL,
        scopes TEXT NOT NULL,
        origin TEXT,
        ip_hash TEXT,
        ua_hash TEXT,
        init_thumb TEXT,
        requested_thumb TEXT,
        status TEXT NOT NULL,
        approved_scopes TEXT,
        device_id TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );

    CREATE TABLE IF NOT EXISTS device_codes (
        device_code TEXT PRIMARY KEY,
        user_code TEXT NOT NULL,
        subnet_id TEXT NOT NULL,
        role TEXT NOT NULL,
        scopes TEXT NOT NULL,
        origin TEXT,
        ip_hash TEXT,
        ua_hash TEXT,
        init_thumb TEXT,
        public_key_pem TEXT,
        status TEXT NOT NULL,
        approved_scopes TEXT,
        device_id TEXT,
        owner_id TEXT,
        response_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at TEXT
    );

    CREATE TABLE IF NOT EXISTS tokens (
        token TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        cnf_thumb TEXT,
        subnet_id TEXT NOT NULL,
        payload_json TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS hub_channels (
        node_id TEXT PRIMARY KEY,
        token TEXT NOT NULL,
        subnet_id TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        rotated_at TEXT,
        revoked INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS browser_challenges (
        device_id TEXT PRIMARY KEY,
        nonce TEXT NOT NULL,
        audience TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS rate_counters (
        bucket TEXT PRIMARY KEY,
        count INTEGER NOT NULL,
        window_start TEXT NOT NULL
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idempotency_cache_unique
        ON idempotency_cache(idempotency_key, method, path, principal_id, body_hash);

    CREATE INDEX IF NOT EXISTS idx_idempotency_expires
        ON idempotency_cache(expires_at);

    CREATE INDEX IF NOT EXISTS idx_device_codes_expires
        ON device_codes(expires_at);

    CREATE INDEX IF NOT EXISTS idx_tokens_expires
        ON tokens(expires_at);

    CREATE INDEX IF NOT EXISTS idx_hub_channels_expires
        ON hub_channels(expires_at);

    CREATE INDEX IF NOT EXISTS idx_devices_subnet_role
        ON devices(subnet_id, role);

    CREATE INDEX IF NOT EXISTS idx_devices_thumb
        ON devices(jwk_thumb);

    CREATE INDEX IF NOT EXISTS idx_consents_status
        ON consents(status, expires_at);
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        if not self._path.parent.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def _ensure_schema(self) -> None:
        self._conn.executescript(self._SCHEMA)

