# src/adaos/adapters/db/sqlite_schema.py
from __future__ import annotations

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS skills (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        active_version TEXT,
        repo_url TEXT,
        installed BOOLEAN DEFAULT 1,
        last_updated TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_versions (
        id INTEGER PRIMARY KEY,
        skill_name TEXT,
        version TEXT,
        path TEXT,
        status TEXT,
        created_at TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS scenarios (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        active_version TEXT,
        repo_url TEXT,
        installed BOOLEAN DEFAULT 1,
        last_updated TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS scenario_versions (
        id INTEGER PRIMARY KEY,
        scenario_name TEXT,
        version TEXT,
        path TEXT,
        status TEXT,
        created_at TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS subnets (
        subnet_id TEXT PRIMARY KEY,
        owner_id TEXT,
        created_at INT
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        subnet_id TEXT NOT NULL,
        role TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        cert_pem TEXT NOT NULL,
        issued_at INT NOT NULL,
        expires_at INT NOT NULL,
        UNIQUE(subnet_id, fingerprint)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_cache (
        key TEXT,
        method TEXT,
        path TEXT,
        principal_id TEXT,
        body_hash TEXT,
        status_code INT,
        body_json TEXT,
        event_id TEXT,
        server_time_utc TEXT,
        created_at INT,
        expires_at INT,
        PRIMARY KEY(key, method, path, principal_id, body_hash)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ca_state (
        id INTEGER PRIMARY KEY CHECK(id=1),
        ca_key_pem TEXT NOT NULL,
        ca_cert_pem TEXT NOT NULL,
        next_serial INTEGER NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_devices_fpr ON devices(fingerprint);
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_idem_exp ON idempotency_cache(expires_at);
    """,
)


def ensure_schema(sql) -> None:
    with sql.connect() as con:
        cur = con.cursor()
        for stmt in _SCHEMA:
            cur.execute(stmt)
        con.commit()
