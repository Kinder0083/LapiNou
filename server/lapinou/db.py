# SPDX-License-Identifier: MIT
"""Base SQLite (stdlib) et migrations numérotées.

Une seule connexion, partagée par la boucle asyncio et les fils (routes de la page, budget,
`asyncio.to_thread`) : `Database.conn` la garde derrière un verrou. Chaque `execute` lit tout
son résultat sous le verrou (les tables sont petites) : aucun curseur n'est jamais parcouru par
deux fils à la fois.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Result:
    """Le résultat complet d'une requête (comme un curseur déjà lu)."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.rows = cursor.fetchall() if cursor.description is not None else []
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.lastrowid
        self.description = cursor.description
        self._next = 0

    def fetchone(self) -> Any:
        if self._next >= len(self.rows):
            return None
        row = self.rows[self._next]
        self._next += 1
        return row

    def fetchall(self) -> list[Any]:
        rest, self._next = self.rows[self._next:], len(self.rows)
        return rest

    def __iter__(self):
        return iter(self.fetchall())


class LockedConnection:
    """La connexion sqlite3, un appel à la fois (verrou réentrant)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.lock = threading.RLock()

    def execute(self, sql: str, params: Any = ()) -> Result:
        with self.lock:
            return Result(self._conn.execute(sql, params))

    def executescript(self, script: str) -> None:
        with self.lock:
            self._conn.executescript(script)

    def commit(self) -> None:
        with self.lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self.lock:
            self._conn.rollback()

    def close(self) -> None:
        with self.lock:
            self._conn.close()

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(path), check_same_thread=False)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA journal_mode = WAL")
        self.conn = LockedConnection(raw)

    def migrate(self) -> int:
        """Applique les migrations manquantes ; idempotent. Retourne la version."""
        with self.conn.lock:
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
