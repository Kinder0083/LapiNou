# SPDX-License-Identifier: MIT
import sqlite3

import pytest
from fastapi.testclient import TestClient

from lapinou.app import create_app
from lapinou.config import Settings
from lapinou.db import Database

TABLES = {"rabbits", "conversations", "turns", "memories", "timers", "usage"}


def _tables(db):
    rows = db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in rows}


def test_migrate_creates_tables(tmp_path):
    db = Database(tmp_path / "lapinou.db")
    assert db.migrate() == 1
    assert TABLES <= _tables(db)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(usage)")}
    assert cols == {"month", "provider", "model", "input_tokens", "output_tokens", "usd"}
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(timers)")}
    assert cols == {"id", "rabbit_id", "due_at", "text", "recurrence"}


def test_migrate_idempotent_keeps_data(tmp_path):
    path = tmp_path / "lapinou.db"
    db = Database(path)
    db.migrate()
    db.conn.execute(
        "INSERT INTO memories(text, created_at, source) VALUES ('a', 'now', 'voice')"
    )
    db.conn.commit()
    assert db.migrate() == 1
    db.close()
    db2 = Database(path)
    assert db2.migrate() == 1
    assert db2.conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    assert db2.conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_usage_primary_key(tmp_path):
    db = Database(tmp_path / "x.db")
    db.migrate()
    q = "INSERT INTO usage(month, provider, model) VALUES ('2026-11','a','m')"
    db.conn.execute(q)
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(q)


def test_health_endpoint():
    client = TestClient(create_app(Settings()))
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
