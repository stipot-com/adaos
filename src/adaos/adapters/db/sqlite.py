# src\adaos\adapters\db\sqlite.py
"""
Лёгкий слой совместимости со старым API:
add_or_update_entity, update_skill_version, list_entities, set_installed_flag.

Внутри использует текущее подключение SQLite из bootstrap (ctx.sql)
и ту же схему таблиц (skills/skill_versions, scenarios/scenario_versions).
"""
from __future__ import annotations
import time
from typing import Optional, Iterable, Literal, List, Dict, Any

from adaos.services.agent_context import get_ctx
from adaos.services.id_gen import new_id

Entity = Literal["skills", "scenarios"]

_SKILL_VERS = "skill_versions"
_SCEN_VERS = "scenario_versions"


def _vers_table(entity: Entity) -> str:
    return _SCEN_VERS if entity == "scenarios" else _SKILL_VERS


def add_or_update_entity(
    entity: Entity,
    name: str,
    active_version: Optional[str] = None,
    repo_url: Optional[str] = None,
    installed: bool = True,
) -> None:
    """
    Upsert записи в таблицы skills/scenarios (совместимо со старым кодом).
    """
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute(
            f"""
            INSERT INTO {entity}(name, active_version, repo_url, installed, last_updated)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                active_version = COALESCE(?, {entity}.active_version),
                repo_url       = COALESCE(?, {entity}.repo_url),
                installed      = ?,
                last_updated   = CURRENT_TIMESTAMP
            """,
            (name, active_version, repo_url, 1 if installed else 0, active_version, repo_url, 1 if installed else 0),
        )
        con.commit()


def set_installed_flag(entity: Entity, name: str, installed: bool) -> None:
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute(
            f"UPDATE {entity} SET installed=?, last_updated=CURRENT_TIMESTAMP WHERE name=?",
            (1 if installed else 0, name),
        )
        con.commit()


def list_entities(entity: Entity, installed_only: bool = True) -> List[Dict[str, Any]]:
    """
    Возвращает список словарей (совместимо с прежним стилем).
    Ключи: name, active_version, repo_url, installed, last_updated (unix).
    """
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    sql = get_ctx().sql
    where = "WHERE installed=1" if installed_only else ""
    with sql.connect() as con:
        cur = con.execute(
            f"""
            SELECT name, active_version, repo_url, installed,
                   strftime('%s', COALESCE(last_updated, CURRENT_TIMESTAMP))
            FROM {entity} {where}
            ORDER BY name
            """
        )
        rows = cur.fetchall()
    return [
        {
            "name": r[0],
            "active_version": r[1],
            "repo_url": r[2],
            "installed": bool(r[3]),
            "last_updated": float(r[4]) if r[4] is not None else None,
        }
        for r in rows
    ]


def update_skill_version(
    entity: Entity,
    name: str,
    version: str,
    path: str,
    status: str = "available",  # например: available/active/disabled
) -> None:
    """
    Добавляет запись о версии в skill_versions/scenario_versions.
    """
    if entity not in ("skills", "scenarios"):
        raise ValueError("entity must be 'skills' or 'scenarios'")
    table = _vers_table(entity)
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute(
            f"""
            INSERT INTO {table}( { 'skill_name' if entity=='skills' else 'scenario_name' }, version, path, status, created_at)
            VALUES( ?, ?, ?, ?, CURRENT_TIMESTAMP )
            """,
            (name, version, path, status),
        )
        con.commit()


def get_skill_versions(name: str) -> List[Dict[str, Any]]:
    """
    Вернуть версии навыка из таблицы skill_versions.
    Формат элементов: {'version': str, 'path': str, 'status': str, 'created_at': str}
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "SELECT version, path, status, COALESCE(created_at, CURRENT_TIMESTAMP) " "FROM skill_versions WHERE skill_name=? ORDER BY created_at DESC, version DESC",
            (name,),
        )
        rows = cur.fetchall()
    return [{"version": r[0], "path": r[1], "status": r[2], "created_at": r[3]} for r in rows]


def list_versions(name: str) -> Optional[str]:
    """
    Совместимый помощник: вернуть active_version навыка из skills.
    Старый CLI ожидает одиночное значение.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute("SELECT active_version FROM skills WHERE name=?", (name,))
        row = cur.fetchone()
    return row[0] if row and row[0] else None


def idem_get(key: str, method: str, path: str, principal_id: str, body_hash: str) -> dict | None:
    if not key:
        return None
    sql = get_ctx().sql
    now = int(time.time())
    with sql.connect() as con:
        cur = con.execute(
            """
            SELECT status_code, body_json, event_id, server_time_utc
            FROM idempotency_cache
            WHERE key=? AND method=? AND path=? AND principal_id=? AND body_hash=? AND expires_at>=?
            """,
            (key, method, path, principal_id, body_hash, now),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "status_code": row[0],
        "body_json": row[1],
        "event_id": row[2],
        "server_time_utc": row[3],
    }


def idem_put(
    key: str,
    method: str,
    path: str,
    principal_id: str,
    body_hash: str,
    status_code: int,
    body_json: str,
    event_id: str,
    server_time_utc: str,
    *,
    ttl: int = 600,
) -> None:
    if not key:
        return
    sql = get_ctx().sql
    now = int(time.time())
    expires_at = now + max(ttl, 1)
    with sql.connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO idempotency_cache(
                key, method, path, principal_id, body_hash,
                status_code, body_json, event_id, server_time_utc,
                created_at, expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                key,
                method,
                path,
                principal_id,
                body_hash,
                status_code,
                body_json,
                event_id,
                server_time_utc,
                now,
                expires_at,
            ),
        )
        con.commit()


def ca_load() -> dict:
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute("SELECT ca_key_pem, ca_cert_pem, next_serial FROM ca_state WHERE id=1")
        row = cur.fetchone()
        if row:
            return {"ca_key_pem": row[0], "ca_cert_pem": row[1], "next_serial": int(row[2])}
        from datetime import datetime, timedelta

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AdaOS Root CA")])
        now = datetime.utcnow()
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        )
        cert = builder.sign(private_key=key, algorithm=hashes.SHA256())
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        con.execute(
            "INSERT INTO ca_state(id, ca_key_pem, ca_cert_pem, next_serial) VALUES(1, ?, ?, ?)",
            (key_pem, cert_pem, 1),
        )
        con.commit()
        return {"ca_key_pem": key_pem, "ca_cert_pem": cert_pem, "next_serial": 1}


def ca_update_serial(next_serial: int) -> None:
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute("UPDATE ca_state SET next_serial=? WHERE id=1", (int(next_serial),))
        con.commit()


def subnet_get_or_create(owner_id: str) -> dict:
    sql = get_ctx().sql
    now = int(time.time())
    with sql.connect() as con:
        cur = con.execute("SELECT subnet_id, owner_id, created_at FROM subnets WHERE owner_id=?", (owner_id,))
        row = cur.fetchone()
        if row:
            return {"subnet_id": row[0], "owner_id": row[1], "created_at": row[2]}
        subnet_id = new_id()
        con.execute(
            "INSERT INTO subnets(subnet_id, owner_id, created_at) VALUES(?, ?, ?)",
            (subnet_id, owner_id, now),
        )
        con.commit()
        return {"subnet_id": subnet_id, "owner_id": owner_id, "created_at": now}


def device_get_by_fingerprint(subnet_id: str, fingerprint: str) -> dict | None:
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            """
            SELECT device_id, subnet_id, role, fingerprint, cert_pem, issued_at, expires_at
            FROM devices
            WHERE subnet_id=? AND fingerprint=?
            """,
            (subnet_id, fingerprint),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "device_id": row[0],
        "subnet_id": row[1],
        "role": row[2],
        "fingerprint": row[3],
        "cert_pem": row[4],
        "issued_at": int(row[5]),
        "expires_at": int(row[6]),
    }


def device_upsert_hub(
    subnet_id: str,
    fingerprint: str,
    cert_pem: str,
    issued_at: int,
    expires_at: int,
) -> dict:
    existing = device_get_by_fingerprint(subnet_id, fingerprint)
    sql = get_ctx().sql
    if existing:
        with sql.connect() as con:
            con.execute(
                "UPDATE devices SET cert_pem=?, issued_at=?, expires_at=? WHERE device_id=?",
                (cert_pem, issued_at, expires_at, existing["device_id"]),
            )
            con.commit()
        existing.update({"cert_pem": cert_pem, "issued_at": issued_at, "expires_at": expires_at})
        return existing
    device_id = new_id()
    with sql.connect() as con:
        con.execute(
            """
            INSERT INTO devices(device_id, subnet_id, role, fingerprint, cert_pem, issued_at, expires_at)
            VALUES(?, ?, 'hub', ?, ?, ?, ?)
            """,
            (device_id, subnet_id, fingerprint, cert_pem, issued_at, expires_at),
        )
        con.commit()
    return {
        "device_id": device_id,
        "subnet_id": subnet_id,
        "role": "hub",
        "fingerprint": fingerprint,
        "cert_pem": cert_pem,
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
