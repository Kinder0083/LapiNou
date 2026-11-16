# SPDX-License-Identifier: MIT
"""Gestes du lapin selon l'état de la conversation : LED et oreilles, par le protocole plateforme.

Écoute : bleu pulsé ; réflexion : violet, oreilles qui bougent doucement ; parole : vert ;
erreur : orange ; repos : vert fixe (couleur de repos de LapinLibre), oreilles droites.

Les commandes partent d'une tâche à part : l'orchestrateur n'attend jamais le lapin (une
commande peut attendre le mode interactif jusqu'à 10 s). Seul le dernier état demandé compte ;
une panne (lapin déconnecté, commande refusée) est notée et oubliée.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .rabbit.platform import Step

log = logging.getLogger(__name__)

EARS_REST = (0, 0)
# Réflexion : les oreilles se penchent doucement l'une après l'autre (pas de 0 à 16).
THINK_WIGGLE: tuple[tuple[tuple[int, int], int], ...] = (((3, 0), 700), ((0, 3), 700))


@dataclass(frozen=True)
class Look:
    led: str
    pulse: str | None = None
    period_ms: int | None = None
    ears: tuple[int, int] | None = EARS_REST
    wiggle: bool = False


LOOKS: dict[str, Look] = {
    "idle": Look("00FF00"),
    "listening": Look("0000FF", "pulse", 700),
    "followup": Look("0000FF", "pulse", 700),
    "thinking": Look("8000FF", ears=None, wiggle=True),
    "speaking": Look("00FF00"),
    "error": Look("FF8000"),
}


class Gestures:
    """Les gestes d'un lapin (objet `Rabbit` : `led`, `ears`, `choreography`)."""

    def __init__(self, rabbit, *, wiggle=THINK_WIGGLE) -> None:
        self.rabbit = rabbit
        self.wiggle = wiggle
        self.current: str | None = None  # dernier aspect demandé
        self._wanted: str | None = None
        self._changed = asyncio.Event()
        self._ears: tuple[int, int] | None = None  # dernière position envoyée
        self._task: asyncio.Task | None = None
        self._dance: asyncio.Task | None = None

    def show(self, look: str) -> None:
        """Demande l'aspect `look` (clé de LOOKS) ; ne bloque pas."""
        if look not in LOOKS:
            raise ValueError(f"aspect inconnu : {look!r}")
        self.current = self._wanted = look
        self._changed.set()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())

    def dance(self, steps: list[Step]) -> None:
        """Une chorégraphie d'outil (histoire, jeu), jouée à côté ; remplace la précédente."""
        if self._dance is not None:
            self._dance.cancel()
        self._dance = asyncio.create_task(self._guard(self.rabbit.choreography(steps)))
        self._ears = None  # position inconnue après la danse

    async def close(self) -> None:
        tasks = [t for t in (self._task, self._dance) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = self._dance = None

    async def _guard(self, coro) -> bool:
        try:
            await coro
            return True
        except (ConnectionError, OSError, ValueError) as e:
            log.debug("lapin %s : geste impossible (%s)", self.rabbit.id, type(e).__name__)
        except Exception:  # inattendu : noté, mais les gestes continuent
            log.exception("lapin %s : geste en échec", self.rabbit.id)
        self._ears = None
        return False

    async def _ears_to(self, ears: tuple[int, int]) -> None:
        if ears != self._ears and await self._guard(self.rabbit.ears(*ears)):
            self._ears = ears

    async def _worker(self) -> None:
        while True:
            await self._changed.wait()
            self._changed.clear()
            look = LOOKS[self._wanted]  # type: ignore[index]
            await self._guard(self.rabbit.led(look.led, look.pulse, look.period_ms))
            if look.ears is not None:
                await self._ears_to(look.ears)
            if look.wiggle:
                await self._wiggle()
            elif not self._changed.is_set():
                return

    async def _wiggle(self) -> None:
        """Bouge les oreilles jusqu'au prochain changement d'aspect."""
        while not self._changed.is_set():
            for ears, wait_ms in self.wiggle:
                await self._ears_to(ears)
                try:
                    await asyncio.wait_for(self._changed.wait(), wait_ms / 1000)
                    return
                except TimeoutError:
                    pass
