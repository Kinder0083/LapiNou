# SPDX-License-Identifier: MIT
import datetime as dt

from lapinou.db import Database
from lapinou.tools.base import ToolBox, ToolContext
from lapinou.tools.memory import MemoryTools

CTX = ToolContext(rabbit_id="lapin-a")


def memory(tmp_path, **kw) -> MemoryTools:
    db = Database(tmp_path / "l.db")
    db.migrate()
    return MemoryTools(db, **kw)


async def test_remember_then_recall(tmp_path):
    mem = memory(tmp_path)
    box = ToolBox(mem.tools())
    result = await box.call("remember", {"text": "Léa adore les dinosaures"}, CTX)
    assert not result.is_error
    await box.call("remember", {"text": "Le chat s'appelle Moustache"}, CTX)
    found = await box.call("recall", {"query": "dinosaure"}, CTX)
    assert "Léa adore les dinosaures" in found.content
    assert "Moustache" not in found.content


async def test_relevant_ranks_and_limits_to_20(tmp_path):
    mem = memory(tmp_path, max_items=100)
    for i in range(30):
        mem.remember(f"souvenir numéro {i}")
    mem.remember("Papa aime le football")
    top = mem.relevant("on regarde le football avec papa ?")
    assert len(top) == 20
    assert top[0] == "Papa aime le football"
    # Sans requête : les plus récents.
    assert mem.relevant("")[0] == "Papa aime le football"


async def test_forget_by_id_and_by_query(tmp_path):
    mem = memory(tmp_path)
    box = ToolBox(mem.tools())
    a = mem.remember("Léa adore les dinosaures")
    mem.remember("Le chat s'appelle Moustache")
    await box.call("forget", {"id": a, "query": None}, CTX)
    assert [m.text for m in mem.all()] == ["Le chat s'appelle Moustache"]
    result = await box.call("forget", {"id": None, "query": "le chat"}, CTX)
    assert not result.is_error and mem.all() == []
    nothing = await box.call("forget", {"id": None, "query": "licorne"}, CTX)
    assert nothing.is_error


async def test_duplicates_and_cap(tmp_path):
    mem = memory(tmp_path, max_items=2)
    first = mem.remember("Léa a 6 ans")
    assert mem.remember("léa a 6 ans") == first
    mem.remember("Tom a 9 ans")
    box = ToolBox(mem.tools())
    full = await box.call("remember", {"text": "un de trop"}, CTX)
    assert full.is_error and len(mem.all()) == 2


async def test_disabled_memory_exposes_nothing(tmp_path):
    mem = memory(tmp_path, enabled=False)
    assert mem.tools() == []
    assert mem.relevant("x") == []


async def test_recency_breaks_ties(tmp_path):
    t = [dt.datetime(2026, 1, 1, tzinfo=dt.UTC)]
    mem = memory(tmp_path, clock=lambda: t[0])
    mem.remember("Léa aime le vélo")
    t[0] += dt.timedelta(days=3)
    mem.remember("Tom aime le vélo")
    assert mem.relevant("vélo")[:2] == ["Tom aime le vélo", "Léa aime le vélo"]
