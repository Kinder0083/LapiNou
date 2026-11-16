# SPDX-License-Identifier: MIT
import asyncio
import datetime as dt

import pytest

from lapinou.db import Database
from lapinou.tools.base import ToolBox, ToolContext
from lapinou.tools.timers import TimerTools

CTX = ToolContext(rabbit_id="lapin-a")
T0 = dt.datetime(2026, 11, 16, 17, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self, now: dt.datetime) -> None:
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class FakeAnnouncer:
    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    async def say(self, rabbit_id: str, text: str) -> None:
        self.said.append((rabbit_id, text))


def open_db(path) -> Database:
    db = Database(path)
    db.migrate()
    return db


async def settle(condition, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("délai dépassé")
        await asyncio.sleep(0.01)


async def test_timer_survives_restart(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer()
    db = open_db(tmp_path / "l.db")
    timers = TimerTools(db, ann, clock=clock)
    await timers.start()
    box = ToolBox(timers.tools())
    result = await box.call("set_timer", {"seconds": 300, "label": "les pâtes"}, CTX)
    assert not result.is_error, result.content
    await timers.close()
    db.close()

    # Redémarrage : nouvelle base ouverte, nouveau planificateur.
    clock.now = T0 + dt.timedelta(seconds=120)
    db2 = open_db(tmp_path / "l.db")
    timers2 = TimerTools(db2, ann, clock=clock)
    await timers2.start()
    pending = timers2.list("lapin-a")
    assert len(pending) == 1
    assert pending[0].due_at == T0 + dt.timedelta(seconds=300)
    assert ann.said == []

    clock.now = T0 + dt.timedelta(seconds=301)
    timers2.poke()
    await settle(lambda: ann.said)
    assert ann.said[0][0] == "lapin-a" and "pâtes" in ann.said[0][1]
    assert timers2.list("lapin-a") == []
    await timers2.close()


async def test_missed_recently_is_announced_at_startup_else_dropped(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer()
    db = open_db(tmp_path / "l.db")
    timers = TimerTools(db, ann, clock=clock)
    await timers.set_timer("lapin-a", 60, "récent")
    await timers.set_timer("lapin-a", 30, "ancien")
    # Serveur arrêté jusqu'à 10 min 50 : « ancien » (dû à 0:30) a 10 min 20 de retard
    # (abandonné), « récent » (dû à 1:00) 9 min 50 (annoncé tout de suite).
    clock.now = T0 + dt.timedelta(minutes=10, seconds=50)
    await timers.start()
    await settle(lambda: ann.said)
    await asyncio.sleep(0.05)
    assert len(ann.said) == 1 and "récent" in ann.said[0][1]
    assert timers.list("lapin-a") == []
    await timers.close()


async def test_reminder_recurring_is_rescheduled(tmp_path):
    clock, ann = Clock(T0), FakeAnnouncer()
    db = open_db(tmp_path / "l.db")
    timers = TimerTools(db, ann, clock=clock)
    await timers.start()
    box = ToolBox(timers.tools())
    at = (T0 + dt.timedelta(hours=1)).astimezone().replace(tzinfo=None).isoformat(
        timespec="minutes"
    )
    result = await box.call(
        "set_reminder", {"at": at, "text": "brosser les dents", "recurrence": "daily"}, CTX
    )
    assert not result.is_error, result.content
    clock.now = T0 + dt.timedelta(hours=1, seconds=1)
    timers.poke()
    await settle(lambda: ann.said)
    assert "brosser les dents" in ann.said[0][1]
    [next_one] = timers.list("lapin-a")
    assert next_one.due_at == T0 + dt.timedelta(days=1, hours=1)
    await timers.close()


async def test_reminder_in_the_past_is_refused(tmp_path):
    timers = TimerTools(open_db(tmp_path / "l.db"), FakeAnnouncer(), clock=Clock(T0))
    box = ToolBox(timers.tools())
    at = (T0 - dt.timedelta(hours=1)).astimezone().replace(tzinfo=None).isoformat()
    result = await box.call("set_reminder", {"at": at, "text": "x", "recurrence": None}, CTX)
    assert result.is_error


async def test_list_and_cancel_are_per_rabbit(tmp_path):
    timers = TimerTools(open_db(tmp_path / "l.db"), FakeAnnouncer(), clock=Clock(T0))
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
    timers = TimerTools(open_db(tmp_path / "l.db"), FakeAnnouncer(), clock=Clock(T0))
    result = await ToolBox(timers.tools()).call(
        "set_timer", {"seconds": seconds, "label": None}, CTX
    )
    assert result.is_error
