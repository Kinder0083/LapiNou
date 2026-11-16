# SPDX-License-Identifier: MIT
"""Bouton « Mettre à jour » du serveur, comme FSAO : `git pull`, `installer/post-update.sh`
(dépendances, migrations, fichiers systemd : écrit par l'installateur, Tâche 25), puis
redémarrage du service.

Le clone est celui d'où tourne le serveur (sauf `[update] repo_dir`) ; pour un dépôt privé, son
remote porte déjà l'accès (installateur). Le redémarrage part une seconde après la réponse, pour
que la page la reçoive ; `systemctl --no-block` le confie à systemd, qui l'achève même quand il
arrête ce processus. Les identifiants d'URL sont masqués dans la sortie montrée.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parents[2]
POST_UPDATE = Path("installer") / "post-update.sh"
TIMEOUT_S = 900
MAX_OUTPUT = 6000
_CREDENTIALS = re.compile(r"(\w+://)[^/@\s]+@")


@dataclass
class UpdateResult:
    ok: bool
    before: str
    after: str
    output: str
    restarting: bool
    message: str


def _redact(text: str) -> str:
    return _CREDENTIALS.sub(r"\1***@", text)


class Updater:
    def __init__(
        self,
        repo_dir: Path | str,
        restart_command: list[str],
        *,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        restart_delay_s: float = 1.0,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.restart_command = list(restart_command)
        self._run = run
        self.restart_delay_s = restart_delay_s
        self._lock = asyncio.Lock()
        self._restart_task: asyncio.Task | None = None

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> Updater:
        u = settings.update
        return cls(u.repo_dir or REPO_DIR, u.restart_command, **kwargs)

    def _cmd(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return self._run(cmd, cwd=self.repo_dir, capture_output=True, text=True,
                             timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=str(e))

    def current(self) -> str:
        """Le commit en service (court), ou « inconnu »."""
        r = self._cmd(["git", "-C", str(self.repo_dir), "rev-parse", "--short", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "inconnu"

    async def update(self) -> UpdateResult:
        async with self._lock:
            result = await asyncio.to_thread(self._update)
        if result.restarting:
            await self.restart()
        return result

    def _update(self) -> UpdateResult:
        out: list[str] = []

        def step(cmd: list[str]) -> bool:
            r = self._cmd(cmd)
            out.append(f"$ {' '.join(cmd)}\n{r.stdout}{r.stderr}")
            return r.returncode == 0

        def result(ok: bool, after: str, restarting: bool, message: str) -> UpdateResult:
            text = _redact("".join(out))[-MAX_OUTPUT:]
            return UpdateResult(ok, before, after, text, restarting, message)

        before = self.current()
        if not step(["git", "-C", str(self.repo_dir), "pull", "--ff-only"]):
            log.warning("mise à jour : git pull a échoué")
            return result(False, before, False, "Échec de git pull : rien n'a changé.")
        after = self.current()
        if after == before:
            return result(True, after, False, "Déjà à jour.")
        script = self.repo_dir / POST_UPDATE
        if script.is_file() and not step(["bash", str(script)]):
            log.warning("mise à jour : post-update.sh a échoué")
            return result(False, after, False,
                          "Code mis à jour, mais post-update.sh a échoué : service non "
                          "redémarré (voir la sortie).")
        log.info("mise à jour %s → %s ; redémarrage", before, after)
        return result(True, after, True, f"Mis à jour ({before} → {after}) : redémarrage.")

    async def restart(self) -> None:
        """Redémarre le service après `restart_delay_s` (la réponse HTTP part d'abord)."""
        if self._restart_task is None or self._restart_task.done():
            self._restart_task = asyncio.create_task(self._restart_later())

    async def _restart_later(self) -> None:
        await asyncio.sleep(self.restart_delay_s)
        r = await asyncio.to_thread(self._cmd, self.restart_command)
        if r.returncode != 0:
            log.error("redémarrage impossible (%s) : %s", " ".join(self.restart_command),
                      (r.stderr or r.stdout).strip()[:300])

    async def wait_restart(self) -> None:
        if self._restart_task is not None:
            await self._restart_task
