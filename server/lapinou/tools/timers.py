# SPDX-License-Identifier: MIT
"""Minuteurs et rappels : en base (table `timers`), annoncés par le lapin qui les a reçus.

Ils survivent au redémarrage : au démarrage, la base est relue ; un minuteur manqué de moins de
10 minutes est annoncé tout de suite, au-delà il est abandonné (journalisé), sauf un rappel
récurrent, reporté à sa prochaine occurrence. Un minuteur n'est supprimé (ou reporté) qu'une
fois annoncé ; si l'annonce échoue (lapin en train de se reconnecter), elle est retentée
jusqu'à 10 minutes de retard. Chaque annonce a son délai et sa tâche : une annonce bloquée ne
retient pas les autres.

`due_at` est stocké en UTC ISO 8601 ; les heures dites et les récurrences suivent le fuseau
réglé (Europe/Paris par défaut), pas celui du système. Une récurrence garde son heure murale
(`daily@02:30`) : une heure qui n'existe pas (passage à l'heure d'été) est décalée ce jour-là
seulement. La colonne `text` contient la phrase à annoncer.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from ..brain.base import ToolResult
from ..db import Database
from .base import Announcer, Tool, ToolContext, error, nullable, strict_schema

log = logging.getLogger(__name__)

MISSED_GRACE = dt.timedelta(minutes=10)
MAX_TIMER_S = 24 * 3600
MAX_REMINDER_AHEAD = dt.timedelta(days=366)
MAX_PER_RABBIT = 30
MAX_WAIT_S = 60.0  # le planificateur revérifie au moins chaque minute
ANNOUNCE_TIMEOUT_S = 15.0
RETRY_S = 15.0
RECURRENCES = ("daily", "weekdays", "weekly")
DEFAULT_TZ = "Europe/Paris"


@dataclass(frozen=True)
class Timer:
    id: int
    rabbit_id: str
    due_at: dt.datetime
    text: str
    recurrence: str | None  # « daily@08:00 » (ou « daily » : heure de due_at)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _store(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).isoformat(timespec="seconds")


def next_occurrence(due: dt.datetime, recurrence: str, tz: ZoneInfo) -> dt.datetime:
    """L'occurrence suivante, à la même heure murale dans `tz` (changements d'heure compris).

    Une heure inexistante ce jour-là (02:30 au passage à l'heure d'été) devient l'instant
    correspondant juste après le saut (03:30) ; une heure en double prend la première.
    """
    kind, _, wall = recurrence.partition("@")
    local = due.astimezone(tz)
    if wall:
        hour, minute = (int(x) for x in wall.split(":"))
        at = dt.time(hour, minute)
    else:
        at = local.time().replace(microsecond=0)
    day = local.date() + dt.timedelta(days=7 if kind == "weekly" else 1)
    if kind == "weekdays":
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
    return dt.datetime.combine(day, at, tzinfo=tz).astimezone(dt.UTC)


def _kind(recurrence: str | None) -> str | None:
    return recurrence.partition("@")[0] if recurrence else None


class TimerTools:
    def __init__(
        self,
        db: Database,
        announcer: Announcer,
        *,
        clock: Callable[[], dt.datetime] = _utc_now,
        tz: ZoneInfo | str = DEFAULT_TZ,
        announce_timeout_s: float = ANNOUNCE_TIMEOUT_S,
        retry_s: float = RETRY_S,
    ) -> None:
        self.db = db
        self.announcer = announcer
        self._now = clock
        self.tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self.announce_timeout_s = announce_timeout_s
        self.retry_s = retry_s
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._inflight: dict[int, asyncio.Task] = {}
        self._retry_at: dict[int, float] = {}  # id → loop.time() de la prochaine tentative
        # Annoncés mais pas encore rangés (base en échec) : id → due_at, jamais redits.
        self._said: dict[int, str] = {}

    def _spoken_time(self, when: dt.datetime) -> str:
        local = when.astimezone(self.tz)
        return f"{local.day:02d}/{local.month:02d} à {local.hour} h {local.minute:02d}"

    def _advance(self, timer: Timer, now: dt.datetime) -> dt.datetime:
        assert timer.recurrence
        due = timer.due_at
        while due <= now:
            due = next_occurrence(due, timer.recurrence, self.tz)
        return due

    # --- Planificateur ------------------------------------------------------------------

    async def start(self) -> None:
        """Relit la base, règle le sort des minuteurs manqués, puis lance le planificateur."""
        now = self._now()
        for timer in self._rows("SELECT * FROM timers WHERE due_at <= ?", (_store(now),)):
            if now - timer.due_at > MISSED_GRACE:
                self._give_up(timer, now, "serveur arrêté")
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        tasks = [t for t in (self._task, *self._inflight.values()) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._inflight.clear()

    def poke(self) -> None:
        """Réveille le planificateur (nouveau minuteur, annonce finie, horloge déplacée)."""
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.fire_due()
                wait = self._next_wait()
            except Exception:
                log.exception("planificateur des minuteurs")
                wait = 5.0  # jamais de boucle folle
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except TimeoutError:
                pass

    def _next_wait(self) -> float:
        now, mono = self._now(), asyncio.get_running_loop().time()
        wait = MAX_WAIT_S
        for timer in self._rows("SELECT * FROM timers"):
            if timer.id in self._inflight:
                continue  # sa fin réveillera le planificateur
            if timer.id in self._said:
                wait = min(wait, self.retry_s)  # rangement à retenter
                continue
            delay = (timer.due_at - now).total_seconds()
            if delay <= 0:
                delay = self._retry_at.get(timer.id, mono) - mono
            wait = min(wait, delay)
        return max(0.01, wait)

    async def fire_due(self) -> int:
        """Lance l'annonce des minuteurs échus (une tâche chacun). Retourne leur nombre."""
        now, mono = self._now(), asyncio.get_running_loop().time()
        launched = 0
        for timer in self._rows("SELECT * FROM timers WHERE due_at <= ? ORDER BY due_at",
                                (_store(now),)):
            if timer.id in self._inflight:
                continue
            if self._said.get(timer.id) == _store(timer.due_at):
                self._settle(timer)  # déjà dit : seulement le ranger
                continue
            if now - timer.due_at > MISSED_GRACE:
                self._give_up(timer, now, "annonce impossible")
                continue
            if mono < self._retry_at.get(timer.id, mono):
                continue
            self._inflight[timer.id] = asyncio.create_task(self._announce(timer))
            launched += 1
        return launched

    async def _announce(self, timer: Timer) -> None:
        try:
            try:
                await asyncio.wait_for(self.announcer.say(timer.rabbit_id, timer.text),
                                       self.announce_timeout_s)
            except Exception as e:  # noqa: BLE001 - le lapin peut se reconnecter : on retente
                log.warning("annonce du minuteur %d à %s en échec (%s), nouvel essai dans %g s",
                            timer.id, timer.rabbit_id, type(e).__name__, self.retry_s)
                self._retry_at[timer.id] = asyncio.get_running_loop().time() + self.retry_s
                return
            self._retry_at.pop(timer.id, None)
            self._said[timer.id] = _store(timer.due_at)
            self._settle(timer)
        finally:
            self._inflight.pop(timer.id, None)
            self.poke()

    def _settle(self, timer: Timer) -> None:
        """Range un minuteur annoncé ; si la base échoue, il est gardé comme « dit » (jamais
        annoncé une deuxième fois) et le rangement est retenté."""
        try:
            self._done(timer)
        except Exception:  # noqa: BLE001 - base indisponible : on retentera
            log.exception("minuteur %d annoncé mais pas encore rangé (il ne sera pas redit)",
                          timer.id)
            return
        self._said.pop(timer.id, None)

    def _still_there(self, timer: Timer) -> bool:
        row = self.db.conn.execute("SELECT due_at FROM timers WHERE id = ?",
                                   (timer.id,)).fetchone()
        return row is not None and row[0] == _store(timer.due_at)

    def _done(self, timer: Timer) -> None:
        """Après l'annonce : supprimé, ou reporté s'il est récurrent (sauf s'il a changé)."""
        if not self._still_there(timer):
            return  # annulé pendant l'annonce
        if timer.recurrence:
            self._update(timer.id, self._advance(timer, self._now()))
        else:
            self._delete(timer.id)

    def _give_up(self, timer: Timer, now: dt.datetime, why: str) -> None:
        self._retry_at.pop(timer.id, None)
        late = now - timer.due_at
        if timer.recurrence:
            due = self._advance(timer, now)
            self._update(timer.id, due)
            log.warning("rappel %d manqué de %s (%s), reporté au %s", timer.id, late, why,
                        _store(due))
        else:
            self._delete(timer.id)
            log.warning("minuteur %d manqué de %s (%s), abandonné : %s", timer.id, late, why,
                        timer.text)

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
        """`at` sans fuseau : heure murale dans le fuseau réglé."""
        if recurrence is not None and recurrence not in RECURRENCES:
            raise ValueError(f"récurrence inconnue : {recurrence}")
        now = self._now()
        # L'heure murale demandée, même si elle n'existe pas ce jour-là (02:30 en mars).
        local = at if at.tzinfo is None else at.astimezone(self.tz)
        if at.tzinfo is None:
            at = at.replace(tzinfo=self.tz)
        stored = f"{recurrence}@{local.hour:02d}:{local.minute:02d}" if recurrence else None
        due = at.astimezone(dt.UTC)
        if due <= now and stored is None:
            raise ValueError("cette heure est déjà passée")
        # « En semaine » demandé un samedi ou un dimanche : pas ce jour-là, le lundi suivant.
        while due <= now or (recurrence == "weekdays" and due.astimezone(self.tz).weekday() >= 5):
            due = next_occurrence(due, stored, self.tz)
        if due - now > MAX_REMINDER_AHEAD:
            raise ValueError("un rappel se règle au plus un an à l'avance")
        if self._count(rabbit_id) >= MAX_PER_RABBIT:
            raise ValueError("trop de rappels en cours")
        text = text.strip()[:200]
        if not text:
            raise ValueError("de quoi faut-il se souvenir ?")
        return self._insert(rabbit_id, due, f"C'est l'heure : {text}", stored)

    def list(self, rabbit_id: str) -> list[Timer]:
        return self._rows("SELECT * FROM timers WHERE rabbit_id = ? ORDER BY due_at",
                          (rabbit_id,))

    def all(self) -> list[Timer]:
        """Les minuteurs et rappels de tous les lapins (page de réglages)."""
        return self._rows("SELECT * FROM timers ORDER BY due_at")

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
        when = self._spoken_time(timer.due_at)
        return ToolResult(f"Rappel n°{timer_id} programmé le {when}.")

    async def _tool_list(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        timers = self.list(ctx.rabbit_id)
        if not timers:
            return ToolResult("Aucun minuteur ni rappel en cours.")
        lines = [
            f"n°{t.id} le {self._spoken_time(t.due_at)} : {t.text}"
            + (f" (répété : {_kind(t.recurrence)})" if t.recurrence else "")
            for t in timers
        ]
        return ToolResult("\n".join(lines))

    async def _tool_cancel(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if self.cancel(ctx.rabbit_id, args["id"]):
            return ToolResult(f"Minuteur n°{args['id']} annulé.")
        return error(f"Pas de minuteur n°{args['id']} pour ce lapin.")
