# SPDX-License-Identifier: MIT
"""Connexion à la page de réglages : un mot de passe admin (haché argon2, dans secrets.toml) et
des sessions (jeton aléatoire dans un cookie HttpOnly, SameSite=Strict, en mémoire : un
redémarrage déconnecte).

Le mot de passe est créé une seule fois par la page (`/api/setup`, tant qu'aucun n'existe) ou
par l'installateur : `python -m lapinou.web.auth < mot_de_passe` affiche son haché, à mettre en
`[admin] password_hash` de secrets.toml. Après 5 échecs en 5 minutes depuis une même adresse,
la connexion est bloquée une minute.
"""

from __future__ import annotations

import secrets
import sys
import threading
import time
from collections.abc import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import SettingsStore

COOKIE = "lapinou_session"
SESSION_TTL_S = 12 * 3600
MIN_PASSWORD = 8
MAX_PASSWORD = 256
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300
LOCKOUT_S = 60

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def password_problem(password: str) -> str | None:
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        return f"le mot de passe doit faire au moins {MIN_PASSWORD} caractères"
    return None


class Locked(Exception):
    """Trop d'échecs : réessayer dans `retry_after` secondes."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


class Auth:
    def __init__(self, store: SettingsStore, clock: Callable[[], float] = time.monotonic):
        self.store = store
        self._clock = clock
        self._sessions: dict[str, float] = {}  # jeton → expiration
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def password_set(self) -> bool:
        return bool(self.store.get().admin.password_hash)

    def _save_hash(self, new_hash: str) -> None:
        data = self.store.get().model_dump(mode="python")
        data["admin"]["password_hash"] = new_hash
        self.store.replace(data)

    def _verify(self, password: str) -> bool:
        stored = self.store.get().admin.password_hash
        if not stored:
            return False
        try:
            _hasher.verify(stored, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        if _hasher.check_needs_rehash(stored):
            self._save_hash(hash_password(password))
        return True

    # --- Sessions ----------------------------------------------------------------------

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self._clock()
            self._sessions = {t: exp for t, exp in self._sessions.items() if exp > now}
            self._sessions[token] = now + SESSION_TTL_S
        return token

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            exp = self._sessions.get(token)
            if exp is None or exp <= self._clock():
                self._sessions.pop(token, None)
                return False
            return True

    def logout(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def logout_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    # --- Mot de passe ------------------------------------------------------------------

    def setup(self, password: str) -> str:
        """Crée le mot de passe (s'il n'y en a pas) ; renvoie une session. PermissionError si un
        mot de passe existe, ValueError s'il est trop court."""
        if self.password_set():
            raise PermissionError("mot de passe déjà créé")
        problem = password_problem(password)
        if problem:
            raise ValueError(problem)
        self._save_hash(hash_password(password))
        return self.new_session()

    def _check_lock(self, client: str) -> None:
        now = self._clock()
        until = self._locked_until.get(client, 0.0)
        if until > now:
            raise Locked(int(until - now) + 1)

    def login(self, password: str, client: str) -> str | None:
        """Une session si le mot de passe est bon, None sinon ; Locked si bloqué."""
        with self._lock:
            self._check_lock(client)
        if self._verify(password):
            with self._lock:
                self._failures.pop(client, None)
            return self.new_session()
        with self._lock:
            now = self._clock()
            recent = [t for t in self._failures.get(client, []) if t > now - FAILURE_WINDOW_S]
            recent.append(now)
            self._failures[client] = recent
            if len(recent) >= MAX_FAILURES:
                self._locked_until[client] = now + LOCKOUT_S
                self._failures.pop(client, None)
        return None

    def change(self, old: str, new: str, client: str) -> bool:
        """Change le mot de passe (l'ancien est exigé) ; les autres sessions sont fermées."""
        with self._lock:
            self._check_lock(client)
        if not self._verify(old):
            return False
        problem = password_problem(new)
        if problem:
            raise ValueError(problem)
        self._save_hash(hash_password(new))
        self.logout_all()
        return True


def main() -> None:
    """Le haché argon2 du mot de passe lu sur l'entrée standard (pour l'installateur)."""
    password = sys.stdin.readline().rstrip("\n")
    problem = password_problem(password)
    if problem:
        sys.exit(problem)
    print(hash_password(password))


if __name__ == "__main__":
    main()
