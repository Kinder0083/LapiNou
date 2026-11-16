# SPDX-License-Identifier: MIT
"""Connexion à la page de réglages : un mot de passe admin (haché argon2id, dans secrets.toml)
et des sessions (jeton aléatoire dans un cookie HttpOnly, SameSite=Strict, en mémoire : un
redémarrage déconnecte).

Premier mot de passe (R40) : l'installateur le pose avec
`python -m lapinou.web.auth --config /etc/lapinou/config.toml < mot_de_passe` (sans
`--config`, le haché est seulement affiché). Sinon, au démarrage sans mot de passe, le serveur
écrit un **code d'installation** à usage unique dans un fichier 0600 du dossier de données
(`setup-token`, à côté de la base) et journalise seulement l'emplacement du fichier ; la page
n'accepte de créer le mot de passe qu'avec ce code, puis efface le fichier. Vérifier et
enregistrer se font sous un verrou : deux créations simultanées ne peuvent pas réussir toutes
les deux.

Essais limités par adresse : chaque essai est compté sous verrou **avant** le calcul argon2 ;
5 essais ratés en 5 minutes bloquent l'adresse 1 minute, puis 2, 4, 8… jusqu'à 15 minutes
(la série retombe à zéro après une connexion réussie ou une heure sans essai). Les ratés de la
connexion, du changement de mot de passe et du code d'installation comptent ensemble.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import SettingsStore, load_settings, save_settings

log = logging.getLogger(__name__)

COOKIE = "lapinou_session"
SESSION_TTL_S = 12 * 3600
MIN_PASSWORD = 8
MAX_PASSWORD = 256
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300
LOCKOUT_S = 60
MAX_LOCKOUT_S = 900
FORGET_S = 3600  # une adresse sans essai depuis une heure est oubliée (série remise à zéro)
MAX_CLIENTS = 1024
SETUP_TOKEN_NAME = "setup-token"

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


class SetupRefused(Exception):
    """Création du mot de passe par la page refusée (pas de code, ou code faux)."""


@dataclass
class _Client:
    attempts: list[float] = field(default_factory=list)
    locked_until: float = 0.0
    strikes: int = 0  # blocages déjà subis : la durée double à chaque fois
    last: float = 0.0


def _write_private(path: Path, text: str) -> None:
    """Écrit `text` dans un fichier 0600 (créé ainsi, jamais lisible par d'autres), atomiquement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Auth:
    def __init__(
        self,
        store: SettingsStore,
        setup_token_path: Path | str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.setup_token_path = Path(setup_token_path) if setup_token_path else None
        self._clock = clock
        self._sessions: dict[str, float] = {}  # jeton → expiration
        self._clients: dict[str, _Client] = {}
        self._lock = threading.Lock()  # sessions et essais
        self._setup_lock = threading.Lock()
        self._setup_token: str | None = None

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

    # --- Essais limités ----------------------------------------------------------------

    def _prune(self, now: float) -> None:
        stale = [k for k, c in self._clients.items()
                 if c.locked_until <= now and c.last < now - FORGET_S]
        for k in stale:
            del self._clients[k]
        if len(self._clients) >= MAX_CLIENTS:  # garde-fou mémoire : les plus anciennes partent
            for k in sorted(self._clients, key=lambda k: self._clients[k].last)[
                    : len(self._clients) - MAX_CLIENTS + 1]:
                del self._clients[k]

    def _attempt(self, client: str) -> None:
        """Compte un essai (avant toute vérification) ; Locked si l'adresse est bloquée."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            c = self._clients.setdefault(client, _Client())
            if c.locked_until > now:
                raise Locked(int(c.locked_until - now) + 1)
            c.attempts = [t for t in c.attempts if t > now - FAILURE_WINDOW_S] + [now]
            c.last = now
            if len(c.attempts) >= MAX_FAILURES:  # l'essai en cours passe, les suivants attendent
                c.locked_until = now + min(LOCKOUT_S * 2 ** c.strikes, MAX_LOCKOUT_S)
                c.strikes += 1
                c.attempts = []

    def _succeeded(self, client: str) -> None:
        with self._lock:
            self._clients.pop(client, None)

    def locked_for(self, client: str) -> float:
        """Secondes de blocage restantes pour cette adresse (0 : libre)."""
        with self._lock:
            c = self._clients.get(client)
            return max(0.0, c.locked_until - self._clock()) if c else 0.0

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

    # --- Premier mot de passe (R40) ----------------------------------------------------

    def prepare_setup(self) -> None:
        """Au démarrage : sans mot de passe, écrit un nouveau code d'installation (0600) ; avec
        un mot de passe, efface un code resté d'avant."""
        path = self.setup_token_path
        if path is None:
            return
        with self._setup_lock:
            if self.password_set():
                self._setup_token = None
                path.unlink(missing_ok=True)
                return
            token = secrets.token_urlsafe(24)
            _write_private(path, token + "\n")
            self._setup_token = token
        log.warning("aucun mot de passe admin : le code d'installation de la page de réglages "
                    "est dans %s (lisible par root seulement)", path)

    def setup(self, token: str, password: str, client: str) -> str:
        """Crée le mot de passe avec le code d'installation ; renvoie une session.
        PermissionError si un mot de passe existe, SetupRefused si le code manque ou est faux,
        ValueError si le mot de passe est trop court, Locked si l'adresse est bloquée."""
        self._attempt(client)
        with self._setup_lock:
            if self.password_set():
                raise PermissionError("mot de passe déjà créé")
            expected = self._setup_token
            if expected is None:
                raise SetupRefused("pas de code d'installation : utilisez "
                                   "python -m lapinou.web.auth sur le serveur")
            if not secrets.compare_digest(token.strip().encode(), expected.encode()):
                raise SetupRefused("code d'installation incorrect")
            problem = password_problem(password)
            if problem:
                raise ValueError(problem)
            self._save_hash(hash_password(password))
            self._setup_token = None
            if self.setup_token_path is not None:
                self.setup_token_path.unlink(missing_ok=True)
        self._succeeded(client)
        log.info("mot de passe admin créé par la page de réglages")
        return self.new_session()

    # --- Connexion et changement -------------------------------------------------------

    def login(self, password: str, client: str) -> str | None:
        """Une session si le mot de passe est bon, None sinon ; Locked si bloqué."""
        self._attempt(client)
        if not self._verify(password):
            return None
        self._succeeded(client)
        return self.new_session()

    def change(self, old: str, new: str, client: str) -> bool:
        """Change le mot de passe (l'ancien est exigé) ; les autres sessions sont fermées."""
        problem = password_problem(new)
        if problem:
            raise ValueError(problem)
        self._attempt(client)
        if not self._verify(old):
            return False
        self._succeeded(client)
        self._save_hash(hash_password(new))
        self.logout_all()
        return True


def main(argv: list[str] | None = None) -> None:
    """Le mot de passe lu sur l'entrée standard : avec --config, son haché est enregistré dans
    les secrets de ce config.toml (installateur, service arrêté) ; sinon il est affiché."""
    parser = argparse.ArgumentParser(prog="python -m lapinou.web.auth")
    parser.add_argument("--config", type=Path, help="config.toml dont les secrets reçoivent le haché")
    args = parser.parse_args(argv)
    password = sys.stdin.readline().rstrip("\r\n")
    problem = password_problem(password)
    if problem:
        sys.exit(problem)
    hashed = hash_password(password)
    if args.config is None:
        print(hashed)
        return
    settings = load_settings(args.config)
    settings.admin.password_hash = hashed
    save_settings(settings)
    print(f"mot de passe admin enregistré dans {args.config.with_name('secrets.toml')}")


if __name__ == "__main__":
    main()
