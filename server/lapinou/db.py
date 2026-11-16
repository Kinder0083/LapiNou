# SPDX-License-Identifier: MIT
"""Base SQLite (stdlib) et migrations numérotées."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def migrate(self) -> int:
        """Applique les migrations manquantes ; idempotent. Retourne la version."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        self.conn.commit()
        done = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        for sql_file in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(sql_file.name[:3])
            if version in done:
                continue
            script = sql_file.read_text(encoding="utf-8")
            # Une migration est atomique : tout ou rien.
            try:
                self.conn.executescript(
                    "BEGIN;\n"
                    + script
                    + "\nINSERT INTO schema_migrations(version, name) "
                    + f"VALUES ({version}, '{sql_file.stem}'); COMMIT;"
                )
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.rollback()
                raise
        return self.version()

    def version(self) -> int:
        row = self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return row[0] or 0

    def close(self) -> None:
        self.conn.close()
