-- 1. Список всех пользователей с ролями
SELECT u.user_id, u.username, u.full_name, r.role_name
FROM users u
JOIN roles r ON u.role_id = r.role_id
ORDER BY u.username;

-- 2. Все активные сессии пользователей
SELECT s.session_id, u.username, s.started_at, s.expires_at, s.ip_address
FROM sessions s
JOIN users u ON s.user_id = u.user_id
JOIN session_statuses ss ON s.session_status_id = ss.session_status_id
WHERE ss.status_name = 'active'
ORDER BY s.started_at DESC;

-- 3. Все токены доступа конкретного пользователя
SELECT t.token_id, u.username, tt.type_name, t.issued_at, t.expires_at, t.is_revoked
FROM access_tokens t
JOIN users u ON t.user_id = u.user_id
JOIN token_types tt ON t.token_type_id = tt.token_type_id
WHERE u.username = 'user_010'
ORDER BY t.issued_at DESC;

-- 4. Журнал действий по конкретной сессии
SELECT l.log_id, at.action_name, l.action_description, l.created_at, l.is_success
FROM audit_logs l
JOIN sessions s ON l.session_id = s.session_id
JOIN action_types at ON l.action_type_id = at.action_type_id
WHERE s.session_id = 1
ORDER BY l.created_at DESC;

-- 5. Количество пользователей по ролям
SELECT r.role_name, COUNT(u.user_id) AS total_users
FROM roles r
LEFT JOIN users u ON r.role_id = u.role_id
GROUP BY r.role_name
ORDER BY total_users DESC;

-- 6. Количество сессий по каждому пользователю
SELECT u.username, COUNT(s.session_id) AS total_sessions
FROM users u
LEFT JOIN sessions s ON u.user_id = s.user_id
GROUP BY u.username
ORDER BY total_sessions DESC;

-- 7. Количество успешных и неуспешных действий
SELECT l.is_success, COUNT(l.log_id) AS total_actions
FROM audit_logs l
GROUP BY l.is_success;

-- 8. Все просроченные токены доступа
SELECT t.token_id, t.token_value, t.expires_at
FROM access_tokens t
WHERE t.expires_at < CURRENT_TIMESTAMP
  AND t.is_revoked = 0;
