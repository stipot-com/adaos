import os
import random
import string
import hashlib
import json
import sqlite3
import datetime

DB_PATH = os.environ.get("DB_PATH", "/data/mcp.db")

def rand_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()

def rand_ip() -> str:
    return ".".join(str(random.randint(1, 254)) for _ in range(4))

def build_db() -> None:
    if os.path.exists(DB_PATH):
        print(f"Database already exists: {DB_PATH}")
        return

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON;")

    with open("/app/schema.sql", "r", encoding="utf-8") as f:
        cur.executescript(f.read())

    roles = [
        ("admin", "Полный административный доступ", 1, 1, 1),
        ("operator", "Сопровождение и контроль сессий", 0, 1, 1),
        ("auditor", "Просмотр журнала действий", 0, 1, 0),
        ("user", "Базовый пользовательский доступ", 0, 0, 0),
    ]
    cur.executemany(
        "INSERT INTO roles (role_name, role_description, can_admin, can_view_logs, can_manage_sessions) VALUES (?,?,?,?,?)",
        roles,
    )

    session_statuses = [
        ("active", "Сессия активна"),
        ("closed", "Сессия завершена штатно"),
        ("expired", "Сессия завершена по истечении срока"),
        ("blocked", "Сессия заблокирована"),
    ]
    cur.executemany(
        "INSERT INTO session_statuses (status_name, status_description) VALUES (?,?)",
        session_statuses,
    )

    token_types = [
        ("access", "Токен доступа"),
        ("refresh", "Токен обновления"),
        ("api", "API токен интеграции"),
    ]
    cur.executemany(
        "INSERT INTO token_types (type_name, type_description) VALUES (?,?)",
        token_types,
    )

    action_types = [
        ("login", "Успешный вход"),
        ("logout", "Выход из системы"),
        ("token_refresh", "Обновление токена"),
        ("resource_read", "Чтение системного ресурса"),
        ("resource_write", "Изменение системного ресурса"),
        ("permission_check", "Проверка прав доступа"),
        ("session_terminate", "Принудительное завершение сессии"),
        ("failed_login", "Неуспешная попытка входа"),
    ]
    cur.executemany(
        "INSERT INTO action_types (action_name, action_description) VALUES (?,?)",
        action_types,
    )

    first_names = ["Иван","Петр","Сергей","Алексей","Дмитрий","Андрей","Максим","Егор","Никита","Артем","Анна","Елена","Мария","Ольга","Дарья","Светлана","Татьяна","Юлия","Наталья","Ирина"]
    last_names = ["Иванов","Петров","Сидоров","Кузнецов","Смирнов","Волков","Федоров","Лебедев","Соколов","Морозов","Попова","Соколова","Михайлова","Козлова","Новикова"]
    domains = ["mcp.local", "example.com", "test.local"]
    now = datetime.datetime(2026, 3, 1, 12, 0, 0)

    users = []
    for i in range(1, 61):
        if i == 1:
            username = "admin_mcp"
            full_name = "Администратор MCP"
            role_id = 1
        else:
            username = f"user_{i:03d}"
            full_name = f"{random.choice(first_names)} {random.choice(last_names)}"
            if i in (2, 3, 4):
                role_id = 2
            elif i in (5, 6):
                role_id = 3
            else:
                role_id = 4

        email = f"{username}@{random.choice(domains)}"
        is_active = 1 if random.random() > 0.08 else 0
        created = now - datetime.timedelta(days=random.randint(30, 900))
        updated = created + datetime.timedelta(days=random.randint(0, 300))
        users.append(
            (
                username,
                full_name,
                email,
                rand_hash(username),
                is_active,
                created.isoformat(sep=" "),
                updated.isoformat(sep=" "),
                role_id,
            )
        )

    cur.executemany(
        """
        INSERT INTO users
        (username, full_name, email, password_hash, is_active, created_at, updated_at, role_id)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        users,
    )

    status_ids = {name: sid for sid, name in cur.execute("SELECT session_status_id, status_name FROM session_statuses")}
    type_ids = {name: tid for tid, name in cur.execute("SELECT token_type_id, type_name FROM token_types")}
    action_ids = {name: aid for aid, name in cur.execute("SELECT action_type_id, action_name FROM action_types")}
    user_rows = list(cur.execute("SELECT user_id, username FROM users"))
    agents = ["MCP Desktop Client", "Mozilla/5.0", "curl/8.5", "python-requests/2.32", "PostmanRuntime/7.39"]

    sessions = []
    for user_id, username in user_rows:
        count = 8 if username == "admin_mcp" else random.randint(1, 6)
        for _ in range(count):
            started = now - datetime.timedelta(days=random.randint(0, 180), hours=random.randint(0, 23), minutes=random.randint(0, 59))
            status_name = random.choices(["active", "closed", "expired", "blocked"], weights=[20, 55, 20, 5])[0]
            expires = started + datetime.timedelta(hours=random.randint(8, 168))

            if status_name == "active":
                ended = None
                last_activity = min(now, started + datetime.timedelta(hours=random.randint(1, 24)))
            elif status_name == "closed":
                ended = started + datetime.timedelta(minutes=random.randint(5, 600))
                last_activity = ended
            elif status_name == "expired":
                ended = expires
                last_activity = expires - datetime.timedelta(minutes=random.randint(1, 60))
            else:
                ended = started + datetime.timedelta(minutes=random.randint(1, 180))
                last_activity = ended

            sessions.append(
                (
                    user_id,
                    status_ids[status_name],
                    started.isoformat(sep=" "),
                    ended.isoformat(sep=" ") if ended else None,
                    rand_ip(),
                    random.choice(agents),
                    last_activity.isoformat(sep=" ") if last_activity else None,
                    expires.isoformat(sep=" "),
                )
            )

    cur.executemany(
        """
        INSERT INTO sessions
        (user_id, session_status_id, started_at, ended_at, ip_address, user_agent, last_activity_at, expires_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        sessions,
    )

    session_rows = list(cur.execute("SELECT session_id, user_id, started_at, expires_at FROM sessions"))
    tokens = []
    for session_id, user_id, started_at, expires_at in session_rows:
        started = datetime.datetime.fromisoformat(started_at)
        expires = datetime.datetime.fromisoformat(expires_at)
        token_names = ["access"] + (["refresh"] if random.random() > 0.25 else [])
        if random.random() < 0.05:
            token_names.append("api")

        for token_name in token_names:
            token_value = "".join(random.choices(string.ascii_letters + string.digits, k=40))
            issued = started + datetime.timedelta(minutes=random.randint(0, 10))
            token_expires = expires + (datetime.timedelta(days=30) if token_name == "refresh" else datetime.timedelta())
            is_revoked = 1 if random.random() < 0.08 else 0
            tokens.append(
                (
                    user_id,
                    session_id,
                    type_ids[token_name],
                    token_value,
                    issued.isoformat(sep=" "),
                    token_expires.isoformat(sep=" "),
                    is_revoked,
                )
            )

    cur.executemany(
        """
        INSERT INTO access_tokens
        (user_id, session_id, token_type_id, token_value, issued_at, expires_at, is_revoked)
        VALUES (?,?,?,?,?,?,?)
        """,
        tokens,
    )

    descriptions = {
        "login": "Успешный вход пользователя в систему",
        "logout": "Пользователь завершил работу",
        "token_refresh": "Выполнено обновление токена доступа",
        "resource_read": "Выполнено чтение системного ресурса",
        "resource_write": "Изменение параметров системного ресурса",
        "permission_check": "Выполнена проверка прав доступа",
        "session_terminate": "Сессия была завершена принудительно",
        "failed_login": "Неуспешная попытка входа в систему",
    }

    logs = []
    action_names = list(action_ids.keys())
    for session_id, user_id, started_at, _ in session_rows:
        current = datetime.datetime.fromisoformat(started_at)
        for _ in range(random.randint(3, 12)):
            action_name = random.choices(
                action_names,
                weights=[15, 8, 5, 25, 10, 20, 5, 3],
            )[0]
            current += datetime.timedelta(minutes=random.randint(1, 120))
            is_success = 0 if action_name == "failed_login" else (0 if random.random() < 0.03 else 1)
            details = json.dumps(
                {"source": "synthetic_seed", "session_id": session_id, "user_id": user_id},
                ensure_ascii=False,
            )
            logs.append(
                (
                    session_id,
                    user_id,
                    action_ids[action_name],
                    descriptions[action_name],
                    current.isoformat(sep=" "),
                    is_success,
                    details,
                )
            )

    cur.executemany(
        """
        INSERT INTO audit_logs
        (session_id, user_id, action_type_id, action_description, created_at, is_success, details)
        VALUES (?,?,?,?,?,?,?)
        """,
        logs,
    )

    conn.commit()
    conn.close()
    print(f"Database initialized: {DB_PATH}")

if __name__ == "__main__":
    random.seed(42)
    build_db()
