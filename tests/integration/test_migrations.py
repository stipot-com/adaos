from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from adaos.services.root.persistence.sqlite import SQLitePersistence


@pytest.fixture()
def connection(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "root.sqlite"
    persistence = SQLitePersistence(db_path)
    try:
        yield persistence.connection
    finally:
        persistence.close()


def test_idempotency_cache_schema(connection: sqlite3.Connection) -> None:
    cursor = connection.execute("PRAGMA table_info(idempotency_cache)")
    columns = {row[1] for row in cursor.fetchall()}
    expected = {
        "idempotency_key",
        "method",
        "path",
        "principal_id",
        "body_hash",
        "response_json",
        "status_code",
        "headers_json",
        "created_at",
        "expires_at",
        "event_id",
        "server_time_utc",
    }
    assert expected.issubset(columns)

    cursor = connection.execute("PRAGMA index_list(idempotency_cache)")
    indexes = {row[1] for row in cursor.fetchall()}
    assert any("expires" in name for name in indexes)

    cursor = connection.execute("PRAGMA index_info(idempotency_cache_unique)")
    uniq_cols = [row[2] for row in cursor.fetchall()]
    assert uniq_cols == ["idempotency_key", "method", "path", "principal_id", "body_hash"]


def test_hub_channel_schema(connection: sqlite3.Connection) -> None:
    cursor = connection.execute("PRAGMA table_info(hub_channels)")
    columns = {row[1] for row in cursor.fetchall()}
    assert {
        "node_id",
        "token",
        "subnet_id",
        "expires_at",
        "created_at",
        "rotated_at",
        "revoked",
    } <= columns

    cursor = connection.execute("PRAGMA index_list(hub_channels)")
    indexes = {row[1] for row in cursor.fetchall()}
    assert any("expires" in name for name in indexes)

