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


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    import shutil

    from lapinou import db as dbmod

    mig = tmp_path / "mig"
    mig.mkdir()
    shutil.copy(dbmod.MIGRATIONS_DIR / "001_init.sql", mig / "001_init.sql")
    (mig / "002_broken.sql").write_text(
        "-- SPDX-License-Identifier: MIT\n"
        "CREATE TABLE half (id INTEGER);\n"
        "INSERT INTO nonexistent VALUES (1);\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", mig)
    db = Database(tmp_path / "m.db")
    with pytest.raises(sqlite3.Error):
        db.migrate()
    assert not db.conn.in_transaction
    assert db.version() == 1
    assert TABLES <= _tables(db)
    assert "half" not in _tables(db)
    # La connexion reste utilisable.
    db.conn.execute("INSERT INTO memories(text, created_at) VALUES ('ok', 'now')")
    db.conn.commit()
    assert db.version() == 1
