# SPDX-License-Identifier: MIT
import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from lapinou.db import Database
from lapinou.tools.base import ToolBox, ToolContext
from lapinou.tools.timers import TimerTools, next_occurrence

CTX = ToolContext(rabbit_id="lapin-a")
T0 = dt.datetime(2026, 11, 16, 17, 0, tzinfo=dt.UTC)  # 18 h à Paris
PARIS = ZoneInfo("Europe/Paris")
UTC = dt.UTC


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class FakeAnnouncer:
    def __init__(self, failures: int = 0) -> None:
        self.said: list[tuple[str, str]] = []
        self.attempts = 0
        self.failures = failures  # nombre d'échecs avant de réussir (-1 : toujours)

    async def say(self, rabbit_id: str, text: str) -> None:
        self.attempts += 1
        if self.failures < 0 or self.attempts <= self.failures:
            raise ConnectionError("lapin déconnecté")
        self.said.append((rabbit_id, text))


class StuckAnnouncer:
    """Ne répond jamais pour lapin-a ; normal pour les autres."""

    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    async def say(self, rabbit_id: str, text: str) -> None:
        if rabbit_id == "lapin-a":
            await asyncio.Event().wait()
        self.said.append((rabbit_id, text))


def open_db(path) -> Database:
    db = Database(path)
    db.migrate()
    return db


def make(db, ann, clock, **kw) -> TimerTools:
    return TimerTools(db, ann, clock=clock, tz=PARIS, **kw)


async def settle(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("délai dépassé")
        await asyncio.sleep(0.01)


async def test_timer_survives_restart(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer()
    db = open_db(tmp_path / "l.db")
    timers = make(db, ann, clock)
    await timers.start()
    box = ToolBox(timers.tools())
    result = await box.call("set_timer", {"seconds": 300, "label": "les pâtes"}, CTX)
    assert not result.is_error, result.content
    await timers.close()
    db.close()

    # Redémarrage : nouvelle base ouverte, nouveau planificateur.
    clock.now = T0 + dt.timedelta(seconds=120)
    db2 = open_db(tmp_path / "l.db")
    timers2 = make(db2, ann, clock)
    await timers2.start()
    pending = timers2.list("lapin-a")
    assert len(pending) == 1
    assert pending[0].due_at == T0 + dt.timedelta(seconds=300)
    assert ann.said == []

    clock.now = T0 + dt.timedelta(seconds=301)
    timers2.poke()
    await settle(lambda: ann.said and not timers2.list("lapin-a"))
    assert ann.said[0][0] == "lapin-a" and "pâtes" in ann.said[0][1]
    await timers2.close()


async def test_missed_recently_is_announced_at_startup_else_dropped(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer()
    db = open_db(tmp_path / "l.db")
    timers = make(db, ann, clock)
    await timers.set_timer("lapin-a", 60, "récent")
    await timers.set_timer("lapin-a", 30, "ancien")
    # Serveur arrêté jusqu'à 10 min 50 : « ancien » (dû à 0:30) a 10 min 20 de retard
    # (abandonné), « récent » (dû à 1:00) 9 min 50 (annoncé tout de suite).
    clock.now = T0 + dt.timedelta(minutes=10, seconds=50)
    await timers.start()
    await settle(lambda: ann.said and not timers.list("lapin-a"))
    await asyncio.sleep(0.05)
    assert len(ann.said) == 1 and "récent" in ann.said[0][1]
    await timers.close()


async def test_failed_announcement_is_retried_then_deleted(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer(failures=2)
    timers = make(open_db(tmp_path / "l.db"), ann, clock, retry_s=0.05)
    await timers.start()
    await timers.set_timer("lapin-a", 60, "goûter")
    clock.now = T0 + dt.timedelta(seconds=61)
    timers.poke()
    await settle(lambda: ann.said and not timers.list("lapin-a"))
    assert ann.attempts == 3 and "goûter" in ann.said[0][1]
    await timers.close()


async def test_failed_announcement_kept_until_too_late_then_dropped(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer(failures=-1)
    timers = make(open_db(tmp_path / "l.db"), ann, clock, retry_s=0.05)
    await timers.start()
    await timers.set_timer("lapin-a", 60, "goûter")
    clock.now = T0 + dt.timedelta(seconds=61)
    timers.poke()
    await settle(lambda: ann.attempts >= 2)
    assert len(timers.list("lapin-a")) == 1  # pas encore supprimé : le lapin peut revenir
    clock.now = T0 + dt.timedelta(minutes=11, seconds=1)
    timers.poke()
    await settle(lambda: not timers.list("lapin-a"))
    attempts = ann.attempts
    await asyncio.sleep(0.2)
    assert ann.attempts == attempts and ann.said == []
    await timers.close()


async def test_stuck_announcement_does_not_block_others(tmp_path):
    clock, ann = Clock(T0), StuckAnnouncer()
    timers = make(open_db(tmp_path / "l.db"), ann, clock, announce_timeout_s=5.0)
    await timers.start()
    await timers.set_timer("lapin-a", 60, "bloqué")
    await timers.set_timer("lapin-b", 61, "libre")
    clock.now = T0 + dt.timedelta(seconds=62)
    timers.poke()
    await settle(lambda: ann.said, timeout=1.0)
    assert ann.said == [("lapin-b", "Le minuteur libre est terminé !")]
    assert [t.text for t in timers.list("lapin-a")] == ["Le minuteur bloqué est terminé !"]
    await timers.close()


async def test_stuck_announcement_times_out(tmp_path):
    clock, ann = Clock(T0), StuckAnnouncer()
    timers = make(open_db(tmp_path / "l.db"), ann, clock, announce_timeout_s=0.05,
                  retry_s=0.05)
    await timers.start()
    await timers.set_timer("lapin-a", 60, "bloqué")
    clock.now = T0 + dt.timedelta(seconds=61)
    timers.poke()
    await asyncio.sleep(0.3)
    assert len(timers.list("lapin-a")) == 1  # échec (délai) : gardé pour réessayer
    await timers.close()


async def test_reminder_recurring_is_rescheduled(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer()
    timers = make(open_db(tmp_path / "l.db"), ann, clock)
    await timers.start()
    box = ToolBox(timers.tools())
    result = await box.call(
        "set_reminder", {"at": "2026-11-16T19:00", "text": "brosser les dents",
                         "recurrence": "daily"}, CTX
    )
    assert not result.is_error, result.content
    [first] = timers.list("lapin-a")
    assert first.due_at == T0 + dt.timedelta(hours=1)  # 19 h à Paris = 18 h UTC
    clock.now = T0 + dt.timedelta(hours=1, seconds=1)
    timers.poke()
    await settle(lambda: ann.said and timers.list("lapin-a")[0].due_at != first.due_at)
    assert "brosser les dents" in ann.said[0][1]
    [next_one] = timers.list("lapin-a")
    assert next_one.due_at == T0 + dt.timedelta(days=1, hours=1)
    await timers.close()


async def test_reminder_in_the_past_is_refused(tmp_path):
    timers = make(open_db(tmp_path / "l.db"), FakeAnnouncer(), Clock(T0))
    box = ToolBox(timers.tools())
    result = await box.call("set_reminder", {"at": "2026-11-16T17:30", "text": "x",
                                             "recurrence": None}, CTX)
    assert result.is_error  # 17 h 30 à Paris = 16 h 30 UTC, avant T0


def paris(*a) -> dt.datetime:
    return dt.datetime(*a, tzinfo=PARIS).astimezone(UTC)


def test_daily_across_spring_forward_including_missing_0230():
    # Passage à l'heure d'été : dimanche 29 mars 2026, 2 h → 3 h (02:30 n'existe pas).
    first = paris(2026, 3, 28, 2, 30)
    assert first == dt.datetime(2026, 3, 28, 1, 30, tzinfo=UTC)
    gap = next_occurrence(first, "daily@02:30", PARIS)
    assert gap == dt.datetime(2026, 3, 29, 1, 30, tzinfo=UTC)  # 03:30 heure d'été
    after = next_occurrence(gap, "daily@02:30", PARIS)
    assert after.astimezone(PARIS).time() == dt.time(2, 30)  # retour à 02:30
    assert after == dt.datetime(2026, 3, 30, 0, 30, tzinfo=UTC)
    morning = next_occurrence(paris(2026, 3, 28, 8, 0), "daily", PARIS)
    assert morning == dt.datetime(2026, 3, 29, 6, 0, tzinfo=UTC)  # 08:00 heure d'été


def test_daily_across_fall_back():
    # Retour à l'heure d'hiver : dimanche 25 octobre 2026, 3 h → 2 h (02:30 existe deux fois).
    first = paris(2026, 10, 24, 2, 30)
    assert first == dt.datetime(2026, 10, 24, 0, 30, tzinfo=UTC)
    twice = next_occurrence(first, "daily@02:30", PARIS)
    assert twice == dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # la première fois
    after = next_occurrence(twice, "daily@02:30", PARIS)
    assert after == dt.datetime(2026, 10, 26, 1, 30, tzinfo=UTC)
    morning = next_occurrence(paris(2026, 10, 24, 8, 0), "daily", PARIS)
    assert morning == dt.datetime(2026, 10, 25, 7, 0, tzinfo=UTC)  # 08:00 heure d'hiver


async def test_reminder_at_missing_time_keeps_its_wall_clock(tmp_path):
    clock = Clock(dt.datetime(2026, 3, 28, 0, 0, tzinfo=UTC))
    ann = FakeAnnouncer()
    timers = make(open_db(tmp_path / "l.db"), ann, clock)
    await timers.set_reminder("lapin-a", dt.datetime(2026, 3, 28, 2, 30), "pilule", "daily")
    for expected in [(2026, 3, 28, 1, 30), (2026, 3, 29, 1, 30), (2026, 3, 30, 0, 30)]:
        [t] = timers.list("lapin-a")
        assert t.due_at == dt.datetime(*expected, tzinfo=UTC)
        clock.now = t.due_at + dt.timedelta(seconds=1)
        assert await timers.fire_due() == 1
        await settle(lambda: timers.list("lapin-a")[0].due_at != t.due_at)
    await timers.close()


async def test_list_and_cancel_are_per_rabbit(tmp_path):
    timers = make(open_db(tmp_path / "l.db"), FakeAnnouncer(), Clock(T0))
    box = ToolBox(timers.tools())
    await box.call("set_timer", {"seconds": 60, "label": None}, CTX)
    other = await timers.set_timer("lapin-b", 60, "autre")
    listing = await box.call("list_timers", {}, CTX)
    assert "autre" not in listing.content
    refused = await box.call("cancel_timer", {"id": other}, CTX)
    assert refused.is_error
    [mine] = timers.list("lapin-a")
    done = await box.call("cancel_timer", {"id": mine.id}, CTX)
    assert not done.is_error
    assert timers.list("lapin-a") == [] and len(timers.list("lapin-b")) == 1


@pytest.mark.parametrize("seconds", [0, -5, 10 * 86400])
async def test_timer_bounds(tmp_path, seconds):
    timers = make(open_db(tmp_path / "l.db"), FakeAnnouncer(), Clock(T0))
    result = await ToolBox(timers.tools()).call(
        "set_timer", {"seconds": seconds, "label": None}, CTX
    )
    assert result.is_error
