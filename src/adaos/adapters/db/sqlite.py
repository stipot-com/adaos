# src\adaos\adapters\db\sqlite.py
"""
Лёгкий слой совместимости со старым API:
add_or_update_entity, update_skill_version, list_entities, set_installed_flag.

Внутри использует текущее подключение SQLite из bootstrap (ctx.sql)
и ту же схему таблиц (skills/skill_versions, scenarios/scenario_versions).
"""
from __future__ import annotations

import json
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
            (name, active_version, repo_url, 1 if installed else 0,
             active_version, repo_url, 1 if installed else 0),
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
            INSERT INTO {table}( {'skill_name' if entity == 'skills' else 'scenario_name'}, version, path, status, created_at)
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
        cur = con.execute(
            "SELECT active_version FROM skills WHERE name=?", (name,))
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
        cur = con.execute(
            "SELECT ca_key_pem, ca_cert_pem, next_serial FROM ca_state WHERE id=1")
        row = cur.fetchone()
        if row:
            return {"ca_key_pem": row[0], "ca_cert_pem": row[1], "next_serial": int(row[2])}
        from datetime import datetime, timedelta

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "AdaOS Root CA")])
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
        cert_pem = cert.public_bytes(
            serialization.Encoding.PEM).decode("utf-8")
        con.execute(
            "INSERT INTO ca_state(id, ca_key_pem, ca_cert_pem, next_serial) VALUES(1, ?, ?, ?)",
            (key_pem, cert_pem, 1),
        )
        con.commit()
        return {"ca_key_pem": key_pem, "ca_cert_pem": cert_pem, "next_serial": 1}


def ca_update_serial(next_serial: int) -> None:
    sql = get_ctx().sql
    with sql.connect() as con:
        con.execute("UPDATE ca_state SET next_serial=? WHERE id=1",
                    (int(next_serial),))
        con.commit()


def subnet_get_or_create(owner_id: str) -> dict:
    sql = get_ctx().sql
    now = int(time.time())
    with sql.connect() as con:
        cur = con.execute(
            "SELECT subnet_id, owner_id, created_at FROM subnets WHERE owner_id=?", (owner_id,))
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
        existing.update(
            {"cert_pem": cert_pem, "issued_at": issued_at, "expires_at": expires_at})
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


# ---- Pairing (pair_codes) and bindings (chat_bindings) ---------------------------------

def pair_issue(bot_id: str, hub_id: str | None, *, ttl_sec: int = 600) -> dict:
    """Create one-time pair code with TTL. Returns {code, bot_id, hub_id, expires_at}."""
    sql = get_ctx().sql
    now = int(time.time())
    expires_at = now + max(1, int(ttl_sec))
    # generate simple base32-like uppercase code 8-10 chars using new_id
    raw = new_id().replace("-", "").upper()
    code = raw[:10]
    with sql.connect() as con:
        con.execute(
            """
            INSERT INTO pair_codes(code, bot_id, hub_id, expires_at, state, created_at, note)
            VALUES(?, ?, ?, ?, 'issued', ?, NULL)
            """,
            (code, bot_id, hub_id, expires_at, now),
        )
        con.commit()
    return {"code": code, "bot_id": bot_id, "hub_id": hub_id, "expires_at": expires_at, "state": "issued"}


def pair_get(code: str) -> dict | None:
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "SELECT code, bot_id, hub_id, expires_at, state, created_at FROM pair_codes WHERE code=?",
            (code,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "code": row[0],
        "bot_id": row[1],
        "hub_id": row[2],
        "expires_at": int(row[3]) if row[3] is not None else None,
        "state": row[4],
        "created_at": int(row[5]) if row[5] is not None else None,
    }


def pair_confirm(code: str) -> dict | None:
    sql = get_ctx().sql
    now = int(time.time())
    rec = pair_get(code)
    if not rec:
        return None
    if rec.get("expires_at") and rec["expires_at"] < now:
        # expire
        with sql.connect() as con:
            con.execute(
                "UPDATE pair_codes SET state='expired' WHERE code=?", (code,))
            con.commit()
        rec["state"] = "expired"
        return rec
    if rec.get("state") not in ("issued",):
        return rec
    with sql.connect() as con:
        con.execute(
            "UPDATE pair_codes SET state='confirmed' WHERE code=?", (code,))
        con.commit()
    rec["state"] = "confirmed"
    return rec


def pair_revoke(code: str) -> bool:
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "UPDATE pair_codes SET state='revoked' WHERE code=?", (code,))
        con.commit()
        return cur.rowcount > 0


def binding_upsert(platform: str, user_id: str, bot_id: str, *, hub_id: str | None, ada_user_id: str | None = None) -> dict:
    """Upsert chat binding and return the record."""
    sql = get_ctx().sql
    now = int(time.time())
    ada_user_id = ada_user_id or new_id()
    with sql.connect() as con:
        con.execute(
            """
            INSERT INTO chat_bindings(platform, user_id, bot_id, ada_user_id, hub_id, created_at, last_seen)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, user_id, bot_id) DO UPDATE SET
              hub_id=excluded.hub_id,
              ada_user_id=COALESCE(chat_bindings.ada_user_id, excluded.ada_user_id),
              last_seen=excluded.last_seen
            """,
            (platform, user_id, bot_id, ada_user_id, hub_id, now, now),
        )
        con.commit()
    return get_binding_by_user(platform, user_id, bot_id) or {
        "platform": platform,
        "user_id": user_id,
        "bot_id": bot_id,
        "ada_user_id": ada_user_id,
        "hub_id": hub_id,
        "created_at": now,
        "last_seen": now,
    }


def get_binding_by_user(platform: str, user_id: str, bot_id: str) -> dict | None:
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            """
            SELECT platform, user_id, bot_id, ada_user_id, hub_id, created_at, last_seen
            FROM chat_bindings WHERE platform=? AND user_id=? AND bot_id=?
            """,
            (platform, user_id, bot_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "platform": row[0],
        "user_id": row[1],
        "bot_id": row[2],
        "ada_user_id": row[3],
        "hub_id": row[4],
        "created_at": int(row[5]) if row[5] is not None else None,
        "last_seen": int(row[6]) if row[6] is not None else None,
    }


# ---- Hub Authentication (hub_registrations, auth_sessions) -----------------

def save_hub_registration(
    hub_id: str,
    public_key: str,
    hub_name: str,
    capabilities: List[str] = None,
    status: str = "active"
) -> None:
    """
    Сохранение регистрации хаба в базу данных.
    Совместимо с существующей структурой кода.
    """
    sql = get_ctx().sql
    now = int(time.time())
    capabilities_json = json.dumps(
        capabilities or ["basic", "skills", "scenarios"])

    with sql.connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO hub_registrations 
            (hub_id, public_key, hub_name, capabilities, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (hub_id, public_key, hub_name, capabilities_json, now, status)
        )
        con.commit()


def get_hub_registration(hub_id: str) -> Optional[Dict[str, Any]]:
    """
    Получение регистрации хаба по ID.
    Возвращает словарь в стиле существующего кода.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "SELECT hub_id, public_key, hub_name, capabilities, created_at, status FROM hub_registrations WHERE hub_id = ?",
            (hub_id,)
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "hub_id": row[0],
        "public_key": row[1],
        "hub_name": row[2],
        "capabilities": json.loads(row[3]) if row[3] else [],
        "created_at": int(row[4]) if row[4] else None,
        "status": row[5]
    }


def save_auth_session(
    session_token: str,
    hub_id: str,
    permissions: List[str] = None,
    ttl_hours: int = 24
) -> None:
    """
    Сохранение сессии аутентификации.
    """
    sql = get_ctx().sql
    now = int(time.time())
    expires_at = now + (ttl_hours * 3600)
    permissions_json = json.dumps(
        permissions or ["api:read", "api:write", "repo:access"])

    with sql.connect() as con:
        con.execute(
            """
            INSERT INTO auth_sessions 
            (session_token, hub_id, issued_at, expires_at, permissions)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_token, hub_id, now, expires_at, permissions_json)
        )
        con.commit()


def get_auth_session(session_token: str) -> Optional[Dict[str, Any]]:
    """
    Получение сессии по токену.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "SELECT session_token, hub_id, issued_at, expires_at, permissions FROM auth_sessions WHERE session_token = ?",
            (session_token,)
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "session_token": row[0],
        "hub_id": row[1],
        "issued_at": int(row[2]) if row[2] else None,
        "expires_at": int(row[3]) if row[3] else None,
        "permissions": json.loads(row[4]) if row[4] else []
    }


def delete_expired_sessions() -> int:
    """Удаление просроченных сессий."""
    sql = get_ctx().sql
    now = int(time.time())
    with sql.connect() as con:
        cur = con.execute(
            "DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
        con.commit()
        return cur.rowcount


def revoke_hub_registration(hub_id: str) -> bool:
    """
    Отзыв регистрации хаба.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "UPDATE hub_registrations SET status = 'revoked' WHERE hub_id = ?",
            (hub_id,)
        )
        con.commit()
        return cur.rowcount > 0


def list_active_hubs() -> List[Dict[str, Any]]:
    """
    Список активных хабов.
    """
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "SELECT hub_id, public_key, hub_name, capabilities, created_at FROM hub_registrations WHERE status = 'active' ORDER BY hub_id"
        )
        rows = cur.fetchall()

    return [
        {
            "hub_id": row[0],
            "public_key": row[1],
            "hub_name": row[2],
            "capabilities": json.loads(row[3]) if row[3] else [],
            "created_at": int(row[4]) if row[4] else None
        }
        for row in rows
    ]


def save_auth_challenge(
    hub_id: str,
    challenge: str,
    ttl_sec: int = 300
) -> None:
    """Сохранение challenge в БД"""
    sql = get_ctx().sql
    now = int(time.time())
    expires_at = now + ttl_sec

    with sql.connect() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO auth_challenges 
            (hub_id, challenge, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (hub_id, challenge, now, expires_at)
        )
        con.commit()


def get_auth_challenge(hub_id: str) -> Dict[str, Any] | None:
    """Получение challenge по hub_id"""
    sql = get_ctx().sql
    now = int(time.time())

    with sql.connect() as con:
        cur = con.execute(
            """
            SELECT hub_id, challenge, created_at, expires_at 
            FROM auth_challenges 
            WHERE hub_id = ? AND expires_at > ?
            """,
            (hub_id, now)
        )
        row = cur.fetchone()

    if not row:
        return None

    return {
        "hub_id": row[0],
        "challenge": row[1],
        "created_at": row[2],
        "expires_at": row[3]
    }


def delete_auth_challenge(hub_id: str) -> bool:
    """Удаление challenge (после использования)"""
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "DELETE FROM auth_challenges WHERE hub_id = ?",
            (hub_id,)
        )
        con.commit()
        return cur.rowcount > 0


def cleanup_expired_challenges() -> int:
    """Очистка просроченных challenges, возвращает количество удаленных"""
    sql = get_ctx().sql
    now = int(time.time())
    with sql.connect() as con:
        cur = con.execute(
            "DELETE FROM auth_challenges WHERE expires_at < ?",
            (now,)
        )
        con.commit()
        return cur.rowcount


def update_hub_sessions_permissions(hub_id: str, permissions: List[str]) -> int:
    """Обновление прав доступа во всех активных сессиях хаба"""
    sql = get_ctx().sql
    permissions_json = json.dumps(permissions)

    with sql.connect() as con:
        cur = con.execute(
            "UPDATE auth_sessions SET permissions = ? WHERE hub_id = ? AND expires_at > ?",
            (permissions_json, hub_id, int(time.time()))
        )
        con.commit()
        return cur.rowcount


def delete_auth_session(session_token: str) -> bool:
    """Удаление конкретной сессии"""
    sql = get_ctx().sql
    with sql.connect() as con:
        cur = con.execute(
            "DELETE FROM auth_sessions WHERE session_token = ?",
            (session_token,)
        )
        con.commit()
        return cur.rowcount > 0
