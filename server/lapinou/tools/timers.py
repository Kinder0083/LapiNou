# SPDX-License-Identifier: MIT
"""Minuteurs et rappels : en base (table `timers`), annoncés par le lapin qui les a reçus.

Ils survivent au redémarrage : au démarrage, la base est relue ; un minuteur manqué de moins de
10 minutes est annoncé tout de suite, au-delà il est abandonné (journalisé), sauf un rappel
récurrent, reporté à sa prochaine occurrence. `due_at` est stocké en UTC ISO 8601, et la
colonne `text` contient la phrase à annoncer.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..brain.base import ToolResult
from ..db import Database
from .base import Announcer, Tool, ToolContext, error, nullable, strict_schema

log = logging.getLogger(__name__)

MISSED_GRACE = dt.timedelta(minutes=10)
MAX_TIMER_S = 24 * 3600
MAX_REMINDER_AHEAD = dt.timedelta(days=366)
MAX_PER_RABBIT = 30
MAX_WAIT_S = 60.0  # le planificateur revérifie au moins chaque minute
RECURRENCES = ("daily", "weekdays", "weekly")


@dataclass(frozen=True)
class Timer:
    id: int
    rabbit_id: str
    due_at: dt.datetime
    text: str
    recurrence: str | None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _store(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).isoformat(timespec="seconds")


def next_occurrence(due: dt.datetime, recurrence: str) -> dt.datetime:
    """L'occurrence suivante, à la même heure locale (changements d'heure compris)."""
    local = due.astimezone().replace(tzinfo=None)
    step = dt.timedelta(days=7 if recurrence == "weekly" else 1)
    local += step
    if recurrence == "weekdays":
        while local.weekday() >= 5:
            local += step
    return local.astimezone().astimezone(dt.UTC)


def _spoken_time(when: dt.datetime) -> str:
    local = when.astimezone()
    return f"{local.day:02d}/{local.month:02d} à {local.hour} h {local.minute:02d}"


class TimerTools:
    def __init__(
        self,
        db: Database,
        announcer: Announcer,
        *,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self.db = db
        self.announcer = announcer
        self._now = clock
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # --- Planificateur ------------------------------------------------------------------

    async def start(self) -> None:
        """Relit la base, règle le sort des minuteurs manqués, puis lance le planificateur."""
        now = self._now()
        for timer in self._rows("SELECT * FROM timers WHERE due_at <= ?", (_store(now),)):
            late = now - timer.due_at
            if late <= MISSED_GRACE:
                continue  # annoncé par le premier passage du planificateur
            if timer.recurrence:
                due = timer.due_at
                while due <= now:
                    due = next_occurrence(due, timer.recurrence)
                self._update(timer.id, due)
                log.info("rappel %d manqué de %s, reporté au %s", timer.id, late, _store(due))
            else:
                self._delete(timer.id)
                log.warning("minuteur %d manqué de %s (serveur arrêté), abandonné : %s",
                            timer.id, late, timer.text)
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def poke(self) -> None:
        """Réveille le planificateur (nouveau minuteur, ou horloge déplacée en test)."""
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await self.fire_due()
            except Exception:
                log.exception("planificateur des minuteurs")
            row = self.db.conn.execute("SELECT MIN(due_at) FROM timers").fetchone()
            wait = MAX_WAIT_S
            if row[0]:
                delay = (dt.datetime.fromisoformat(row[0]) - self._now()).total_seconds()
                wait = max(0.0, min(wait, delay))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except TimeoutError:
                pass
            self._wake.clear()

    async def fire_due(self) -> int:
        """Annonce les minuteurs échus ; supprime ou reporte chacun. Retourne leur nombre."""
        async with self._lock:
            now = self._now()
            due = self._rows("SELECT * FROM timers WHERE due_at <= ? ORDER BY due_at",
                             (_store(now),))
            for timer in due:
                if timer.recurrence:
                    when = timer.due_at
                    while when <= now:
                        when = next_occurrence(when, timer.recurrence)
                    self._update(timer.id, when)
                else:
                    self._delete(timer.id)
                try:
                    await self.announcer.say(timer.rabbit_id, timer.text)
                except Exception:
                    log.exception("annonce du minuteur %d à %s", timer.id, timer.rabbit_id)
            return len(due)

    # --- Base ---------------------------------------------------------------------------

    def _rows(self, sql: str, params: tuple = ()) -> list[Timer]:
        return [
            Timer(r["id"], r["rabbit_id"], dt.datetime.fromisoformat(r["due_at"]), r["text"],
                  r["recurrence"])
            for r in self.db.conn.execute(sql, params)
        ]

    def _insert(self, rabbit_id: str, due: dt.datetime, text: str, recurrence: str | None) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO timers(rabbit_id, due_at, text, recurrence) VALUES (?, ?, ?, ?)",
            (rabbit_id, _store(due), text, recurrence),
        )
        self.db.conn.commit()
        self.poke()
        return int(cur.lastrowid)

    def _update(self, timer_id: int, due: dt.datetime) -> None:
        self.db.conn.execute("UPDATE timers SET due_at = ? WHERE id = ?", (_store(due), timer_id))
        self.db.conn.commit()

    def _delete(self, timer_id: int) -> None:
        self.db.conn.execute("DELETE FROM timers WHERE id = ?", (timer_id,))
        self.db.conn.commit()

    def _count(self, rabbit_id: str) -> int:
        row = self.db.conn.execute("SELECT COUNT(*) FROM timers WHERE rabbit_id = ?",
                                   (rabbit_id,)).fetchone()
        return int(row[0])

    # --- API ----------------------------------------------------------------------------

    async def set_timer(self, rabbit_id: str, seconds: int, label: str | None) -> int:
        if not 1 <= seconds <= MAX_TIMER_S:
            raise ValueError("un minuteur dure de 1 seconde à 24 heures")
        if self._count(rabbit_id) >= MAX_PER_RABBIT:
            raise ValueError("trop de minuteurs en cours")
        label = (label or "").strip()[:80]
        text = f"Le minuteur {label} est terminé !" if label else "Le minuteur est terminé !"
        return self._insert(rabbit_id, self._now() + dt.timedelta(seconds=seconds), text, None)

    async def set_reminder(
        self, rabbit_id: str, at: dt.datetime, text: str, recurrence: str | None
    ) -> int:
        if recurrence is not None and recurrence not in RECURRENCES:
            raise ValueError(f"récurrence inconnue : {recurrence}")
        now = self._now()
        if at.tzinfo is None:
            at = at.astimezone()  # heure locale du serveur
        if at <= now:
            if recurrence is None:
                raise ValueError("cette heure est déjà passée")
            while at <= now:
                at = next_occurrence(at, recurrence)
        if at - now > MAX_REMINDER_AHEAD:
            raise ValueError("un rappel se règle au plus un an à l'avance")
        if self._count(rabbit_id) >= MAX_PER_RABBIT:
            raise ValueError("trop de rappels en cours")
        text = text.strip()[:200]
        if not text:
            raise ValueError("de quoi faut-il se souvenir ?")
        return self._insert(rabbit_id, at, f"C'est l'heure : {text}", recurrence)

    def list(self, rabbit_id: str) -> list[Timer]:
        return self._rows("SELECT * FROM timers WHERE rabbit_id = ? ORDER BY due_at",
                          (rabbit_id,))

    def cancel(self, rabbit_id: str, timer_id: int) -> bool:
        cur = self.db.conn.execute("DELETE FROM timers WHERE id = ? AND rabbit_id = ?",
                                   (timer_id, rabbit_id))
        self.db.conn.commit()
        return cur.rowcount > 0

    # --- Outils -------------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "set_timer",
                "Lance un minuteur (compte à rebours) de quelques secondes à 24 heures : "
                "« dans 10 minutes », « minuteur pour les pâtes ». Le lapin l'annoncera à la "
                "fin. Pour une heure précise de l'horloge, utilise set_reminder.",
                strict_schema({
                    "seconds": {"type": "integer", "description": "Durée en secondes."},
                    "label": nullable({"type": "string", "description": "Ex. « les pâtes »."}),
                }),
                self._tool_set_timer,
            ),
            Tool(
                "set_reminder",
                "Programme un rappel ou un réveil à une date et heure précises (heure locale, "
                "calculée d'après la date du contexte), éventuellement répété. Le lapin dira "
                "« C'est l'heure : <text> ». Pour une durée (« dans 5 minutes »), utilise "
                "set_timer.",
                strict_schema({
                    "at": {"type": "string",
                           "description": "Date et heure locales ISO 8601, ex. 2026-11-16T18:30."},
                    "text": {"type": "string", "description": "Ce qu'il faudra rappeler."},
                    "recurrence": nullable({"type": "string", "enum": list(RECURRENCES),
                                            "description": "daily, weekdays (lundi-vendredi), "
                                                           "weekly, ou null."}),
                }),
                self._tool_set_reminder,
            ),
            Tool(
                "list_timers",
                "Liste les minuteurs et rappels en cours de ce lapin, avec leur numéro.",
                strict_schema({}),
                self._tool_list,
            ),
            Tool(
                "cancel_timer",
                "Annule un minuteur ou un rappel de ce lapin par son numéro (voir list_timers).",
                strict_schema({"id": {"type": "integer"}}),
                self._tool_cancel,
            ),
        ]

    async def _tool_set_timer(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            timer_id = await self.set_timer(ctx.rabbit_id, args["seconds"], args["label"])
        except ValueError as e:
            return error(str(e))
        return ToolResult(f"Minuteur n°{timer_id} lancé pour {args['seconds']} secondes.")

    async def _tool_set_reminder(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            at = dt.datetime.fromisoformat(args["at"])
        except ValueError:
            return error("date invalide : il faut AAAA-MM-JJTHH:MM")
        try:
            timer_id = await self.set_reminder(ctx.rabbit_id, at, args["text"],
                                               args["recurrence"])
        except ValueError as e:
            return error(str(e))
        [timer] = [t for t in self.list(ctx.rabbit_id) if t.id == timer_id]
        return ToolResult(f"Rappel n°{timer_id} programmé le {_spoken_time(timer.due_at)}.")

    async def _tool_list(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        timers = self.list(ctx.rabbit_id)
        if not timers:
            return ToolResult("Aucun minuteur ni rappel en cours.")
        lines = [
            f"n°{t.id} le {_spoken_time(t.due_at)} : {t.text}"
            + (f" (répété : {t.recurrence})" if t.recurrence else "")
            for t in timers
        ]
        return ToolResult("\n".join(lines))

    async def _tool_cancel(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self.cancel(ctx.rabbit_id, args["id"]):
            return ToolResult(f"Minuteur n°{args['id']} annulé.")
        return error(f"Pas de minuteur n°{args['id']} pour ce lapin.")
