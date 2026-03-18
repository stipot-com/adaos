
CREATE TABLE roles (
    role_id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_name VARCHAR(50) NOT NULL UNIQUE,
    role_description VARCHAR(255),
    can_admin BOOLEAN NOT NULL DEFAULT 0,
    can_view_logs BOOLEAN NOT NULL DEFAULT 0,
    can_manage_sessions BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) NOT NULL UNIQUE,
    full_name VARCHAR(128) NOT NULL,
    email VARCHAR(128) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    role_id INTEGER NOT NULL,
    FOREIGN KEY (role_id) REFERENCES roles(role_id)
);

CREATE TABLE session_statuses (
    session_status_id INTEGER PRIMARY KEY AUTOINCREMENT,
    status_name VARCHAR(30) NOT NULL UNIQUE,
    status_description VARCHAR(255)
);

CREATE TABLE sessions (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_status_id INTEGER NOT NULL,
    started_at DATETIME NOT NULL,
    ended_at DATETIME,
    ip_address VARCHAR(45),
    user_agent VARCHAR(255),
    last_activity_at DATETIME,
    expires_at DATETIME NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (session_status_id) REFERENCES session_statuses(session_status_id)
);

CREATE TABLE token_types (
    token_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    type_name VARCHAR(30) NOT NULL UNIQUE,
    type_description VARCHAR(255)
);

CREATE TABLE access_tokens (
    token_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    token_type_id INTEGER NOT NULL,
    token_value VARCHAR(255) NOT NULL UNIQUE,
    issued_at DATETIME NOT NULL,
    expires_at DATETIME NOT NULL,
    is_revoked BOOLEAN NOT NULL DEFAULT 0,
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (token_type_id) REFERENCES token_types(token_type_id)
);

CREATE TABLE action_types (
    action_type_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_name VARCHAR(50) NOT NULL UNIQUE,
    action_description VARCHAR(255)
);

CREATE TABLE audit_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    action_type_id INTEGER NOT NULL,
    action_description VARCHAR(255) NOT NULL,
    created_at DATETIME NOT NULL,
    is_success BOOLEAN NOT NULL DEFAULT 1,
    details TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    FOREIGN KEY (action_type_id) REFERENCES action_types(action_type_id)
);

CREATE INDEX idx_roles_role_name ON roles(role_name);
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_role_id ON users(role_id);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_status_id ON sessions(session_status_id);
CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);
CREATE INDEX idx_access_tokens_user_id ON access_tokens(user_id);
CREATE INDEX idx_access_tokens_session_id ON access_tokens(session_id);
CREATE INDEX idx_access_tokens_type_id ON access_tokens(token_type_id);
CREATE INDEX idx_access_tokens_expires_at ON access_tokens(expires_at);
CREATE INDEX idx_audit_logs_session_id ON audit_logs(session_id);
CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX idx_audit_logs_action_type_id ON audit_logs(action_type_id);
CREATE INDEX idx_audit_logs_created_at ON audit_logs(created_at);
