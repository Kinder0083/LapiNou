# SPDX-License-Identifier: MIT
"""Orchestrateur de conversation de bout en bout : faux lapin, faux cerveau, faux outils.

Le flux micro est un vrai `RabbitStream` nourri de trames ; Home Assistant est le faux
ha-mcp (vrai serveur MCP stdio, tests/fakes/fake_mcp.py) derrière les vrais
`HomeAssistantTools` et leur politique. Aucun appel réseau réel.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

from lapinou import conversation as conv_mod
from lapinou.brain.base import Done, Retract, TextDelta, ToolCall, ToolResult, Usage
from lapinou.config import SafetyConfig, Settings
from lapinou.conversation import (
    HOME_DOWN_FR,
    ConversationLog,
    Orchestrator,
    RabbitSession,
    SessionDeps,
)
from lapinou.db import Database
from lapinou.gestures import LOOKS, Gestures
from lapinou.mqtt_state import AssistantState
from lapinou.rabbit.audio_gateway import Frame, RabbitStream
from lapinou.rabbit.platform import Step
from lapinou.tools.base import Tool, ToolBox, strict_schema
from lapinou.tools.ha import HomeAssistantTools
from lapinou.tools.policy import Policy
from lapinou.voice.tts import Speech
from lapinou.voice.wake_asr import WakeHit

FAKE_MCP = Path(__file__).parent / "fakes" / "fake_mcp.py"
RID = "lapin-a"
CHUNK = 1600  # 100 ms à 16 kHz


# --- Faux ---------------------------------------------------------------------------------


class FakeRabbit:
    """Un lapin de la plateforme : enregistre les commandes ; une lecture finit seule après
    `play_s`, sauf avec `hold` (jusqu'à `stop_playback`)."""

    def __init__(self, rabbit_id: str = RID, play_s: float = 0.03) -> None:
        self.id = rabbit_id
        self.connected = True
        self.play_s = play_s
        self.hold = False
        self.played: list[tuple[str, str]] = []
        self.stops = 0
        self.leds: list[tuple[str, str | None]] = []
        self.ears_moves: list[tuple[int, int]] = []
        self.dances: list[list[Step]] = []
        self.on_playback = None
        self.started = 0
        self._held: list[str] = []

    async def play(self, url: str, playback_id: str) -> None:
        if not self.connected:
            raise ConnectionError("lapin déconnecté")
        self.played.append((url, playback_id))
        self.started += 1
        if self.hold:
            self._held.append(playback_id)
        else:
            asyncio.get_running_loop().call_later(
                self.play_s, self.on_playback, playback_id, "stopped"
            )

    async def stop_playback(self) -> None:
        self.stops += 1
        held, self._held = self._held, []
        for pid in held:
            self.on_playback(pid, "stopped")

    async def led(self, color, pulse=None, period_ms=None) -> None:
        if not self.connected:
            raise ConnectionError("lapin déconnecté")
        self.leds.append((color, pulse))

    async def ears(self, left, right) -> None:
        if not self.connected:
            raise ConnectionError("lapin déconnecté")
        self.ears_moves.append((left, right))

    async def choreography(self, steps) -> None:
        self.dances.append(steps)


class FakeVoice:
    def __init__(self) -> None:
        self.texts: dict[str, str] = {}  # playback_id -> texte
        self.gate: asyncio.Event | None = None
        self.fail = False

    async def speak(self, text: str) -> Speech:
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("synthèse en panne")
        sid = uuid.uuid4().hex
        self.texts[sid] = text
        return Speech(sid, f"http://srv:8080/tts/{sid}.mp3", np.full(1600, 7, np.int16), 0.1)


class FakeTranscriber:
    """Rend les réponses dans l'ordre (None : personne n'a parlé ; BLOCK : attend toujours),
    après avoir lu au moins un bloc du flux."""

    BLOCK = object()

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.starved = 0

    async def transcribe_utterance(self, chunks, max_s=15, silence_ms=700, idle_timeout_s=7.0,
                                   no_speech_s=5.0):
        self.calls.append({"max_s": max_s, "silence_ms": silence_ms, "no_speech_s": no_speech_s})
        try:
            reply = self.replies.pop(0) if self.replies else None
            try:
                first = await asyncio.wait_for(chunks.__anext__(), 0.5)
                assert first.dtype == np.int16
            except TimeoutError:
                self.starved += 1  # aucun audio (lapin sans flux)
            if reply is self.BLOCK:
                await asyncio.Event().wait()
            return reply
        finally:
            await chunks.aclose()


class FakeDetector:
    def __init__(self) -> None:
        self.pending: list[WakeHit] = []
        self.fed = 0
        self.resets = 0

    def feed(self, pcm):
        assert pcm.dtype == np.int16
        self.fed += len(pcm)
        out, self.pending = self.pending, []
        return out

    def reset(self):
        self.resets += 1
        return []

    def hear(self, *hits: WakeHit) -> None:
        self.pending.extend(hits)


class Block:
    """Événement de cerveau scripté : attend `event` (cerveau qui réfléchit longtemps)."""

    def __init__(self) -> None:
        self.event = asyncio.Event()


class ScriptedBrain:
    """Rejoue un tour scripté par appel de `respond` ; ajoute le tour de l'assistant comme les
    vrais cerveaux."""

    def __init__(self, *rounds) -> None:
        self.rounds = list(rounds)
        self.calls: list[list[dict]] = []
        self.closed = 0

    async def respond(self, conv, tools, budget):
        self.calls.append(copy.deepcopy(conv.messages))
        self.memories = list(conv.memories)
        events = self.rounds.pop(0) if self.rounds else [TextDelta("D'accord."), done()]
        text, uses = "", []
        try:
            for ev in events:
                if isinstance(ev, Block):
                    await ev.event.wait()
                    continue
                if isinstance(ev, TextDelta):
                    text += ev.text
                if isinstance(ev, ToolCall):
                    uses.append({"type": "tool_use", "id": ev.id, "name": ev.name,
                                 "input": ev.args})
                yield ev
        finally:
            self.closed += 1
        content = ([{"type": "text", "text": text}] if text else []) + uses
        if content:
            conv.messages.append({"role": "assistant", "content": content})


class FakeState:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    def publish(self, rabbit_id: str, state: str) -> None:
        self.published.append((rabbit_id, state))

    def states(self) -> list[str]:
        out: list[str] = []
        for _, s in self.published:
            if not out or out[-1] != s:
                out.append(s)
        return out


class FakeMemory:
    def __init__(self, facts) -> None:
        self.facts = facts
        self.queries: list[str] = []

    def relevant(self, query: str) -> list[str]:
        self.queries.append(query)
        return list(self.facts)


def done() -> Done:
    return Done(Usage(provider="fake", model="fake"))


def call_service(domain, service, entity, call_id="t1") -> ToolCall:
    args = {"domain": domain, "service": service, "entity_id": entity, "brightness_pct": None,
            "temperature": None, "volume_level": None, "position": None}
    return ToolCall(call_id, "ha_call_service", args)


def hit(at=1000, command="", complete=True, text="Lapinou") -> WakeHit:
    return WakeHit(at_sample=at, text=text, command=command, complete=complete)


async def until(cond, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition non atteinte")
        await asyncio.sleep(0.005)


def service_calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [c["args"] for c in map(json.loads, lines) if c["tool"] == "ha_call_service"]


# --- Monde de test ------------------------------------------------------------------------


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.rabbit = FakeRabbit()
        self.stream = RabbitStream(RID)
        self.stream.connected = True
        self.voice = FakeVoice()
        self.detector = FakeDetector()
        self.state = FakeState()
        self.db = Database(tmp_path / "lapinou.db")
        self.db.migrate()
        self.log = ConversationLog(self.db, retention_days=7)
        self.ha: HomeAssistantTools | None = None
        self.mcp_log = tmp_path / "calls.jsonl"
        self.session: RabbitSession | None = None
        self._seq = 0
        self._sample = 0
        self._feeder: asyncio.Task | None = None

    async def start_ha(self, mcp_cmd=None, **safety) -> HomeAssistantTools:
        self.ha = HomeAssistantTools(
            Policy(SafetyConfig(**safety)),
            mcp_cmd=mcp_cmd or [sys.executable, str(FAKE_MCP)],
            env={"FAKE_MCP_LOG": str(self.mcp_log)},
        )
        await self.ha.start()
        return self.ha

    def feed(self, samples: int = CHUNK) -> None:
        self.stream._feed(Frame(RID, self._seq, mulaw=b"\xff" * samples,
                                first_sample=self._sample))
        self._seq += 1
        self._sample += samples

    async def _feed_forever(self) -> None:
        while True:
            if self.stream.connected:
                self.feed()
            await asyncio.sleep(0.01)

    def start(self, brain, transcriber, *, tools=None, aec=False, **kw) -> RabbitSession:
        self.brain, self.transcriber = brain, transcriber
        if tools is None:
            tools = ToolBox(self.ha.tools() if self.ha else [])
        make_aec = None
        if aec:
            from lapinou.voice.aec import EchoCanceller

            make_aec = EchoCanceller
        deps = SessionDeps(
            brain=brain,
            tools=tools,
            voice=self.voice,
            transcriber=transcriber,
            make_detector=lambda: self.detector,
            make_aec=make_aec,
            ha=self.ha,
            log=self.log,
            state=self.state,
            watch_s=0.02,
            wake_grow_s=kw.pop("wake_grow_s", 0.3),
            **kw,
        )
        self.session = RabbitSession(self.rabbit, self.stream, deps)
        self.rabbit.on_playback = self.session.on_playback
        self.session.start()
        self._feeder = asyncio.create_task(self._feed_forever())
        return self.session

    def spoken(self) -> list[str]:
        return [self.voice.texts[pid] for _, pid in self.rabbit.played]

    async def idle(self, timeout: float = 3.0) -> None:
        await until(lambda: self.session.state == "idle" and self.session.is_idle, timeout)

    def turns(self) -> list[tuple[str, str]]:
        rows = self.db.conn.execute("SELECT role, text FROM turns ORDER BY id")
        return [(r["role"], r["text"]) for r in rows]

    async def close(self) -> None:
        if self._feeder is not None:
            self._feeder.cancel()
            await asyncio.gather(self._feeder, return_exceptions=True)
        if self.session is not None:
            await self.session.close()
        if self.ha is not None:
            await self.ha.close()
        self.db.close()


@pytest.fixture
async def world(tmp_path):
    w = World(tmp_path)
    yield w
    await w.close()


# --- Bout en bout -------------------------------------------------------------------------


async def test_allume_le_salon_calls_the_tool_then_speaks(world):
    await world.start_ha()
    brain = ScriptedBrain(
        [call_service("light", "turn_on", "light.salon"), done()],
        [TextDelta("Voilà, le salon est allumé !"), done()],
    )
    session = world.start(brain, FakeTranscriber(None), aec=True)
    world.detector.hear(hit(command="allume le salon", text="Lapinou, allume le salon"))
    await until(lambda: world.spoken())
    await world.idle()

    assert service_calls(world.mcp_log) == [
        {"domain": "light", "service": "turn_on", "entity_id": "light.salon", "data": None}
    ]
    assert world.spoken() == ["Voilà, le salon est allumé !"]
    assert brain.calls[0][-1]["content"][0]["text"] == "allume le salon"
    tool_result = brain.calls[1][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and not tool_result["is_error"]
    assert world.state.states() == ["idle", "thinking", "speaking", "followup", "idle"]
    assert world.transcriber.calls[0]["no_speech_s"] == 5  # fenêtre de suite
    # Référence de l'annuleur d'écho : le PCM exact de la phrase jouée.
    pid = world.rabbit.played[0][1]
    assert session._reference(pid) is not None and len(session._reference(pid)) == 1600
    assert world.turns() == [("user", "allume le salon"),
                             ("assistant", "Voilà, le salon est allumé !")]
    row = world.db.conn.execute("SELECT rabbit_id, ended_at FROM conversations").fetchone()
    assert row["rabbit_id"] == RID and row["ended_at"] is not None


async def test_wake_word_alone_listens_then_transcribes(world):
    brain = ScriptedBrain([TextDelta("Il est midi."), done()])
    world.start(brain, FakeTranscriber("quelle heure est-il", None))
    world.detector.hear(hit())
    await until(lambda: world.spoken())
    await world.idle()
    assert brain.calls[0][-1]["content"][0]["text"] == "quelle heure est-il"
    assert world.state.states()[:3] == ["idle", "listening", "thinking"]
    assert ("0000FF", "pulse") in world.rabbit.leds  # écoute : bleu pulsé
    assert world.transcriber.calls[0]["silence_ms"] == 700


async def test_false_wake_without_speech_returns_to_idle_without_the_brain(world):
    brain = ScriptedBrain()
    world.start(brain, FakeTranscriber(None))
    world.detector.hear(hit())
    await until(lambda: world.transcriber.calls)
    await world.idle()
    assert brain.calls == []
    assert world.rabbit.played == []
    assert world.state.states() == ["idle", "listening", "idle"]
    assert world.turns() == []
    # La détection reprend après le retour au repos.
    fed = world.detector.fed
    await until(lambda: world.detector.fed > fed)


async def test_click_while_speaking_stops_playback_and_listens(world):
    world.rabbit.hold = True
    brain = ScriptedBrain([TextDelta("Il était une fois un lapin. Il vivait dans un pré. "
                                     "Fin."), done()])
    session = world.start(brain, FakeTranscriber(FakeTranscriber.BLOCK, FakeTranscriber.BLOCK))
    world.detector.hear(hit(command="raconte une histoire"))
    await until(lambda: session.state == "speaking")
    session.on_button("simple")
    await until(lambda: session.state == "listening")
    assert world.rabbit.stops == 1
    await asyncio.sleep(0.1)
    assert world.spoken() == ["Il était une fois un lapin."]  # la suite n'est pas jouée


async def test_wake_word_while_speaking_interrupts_with_its_command(world):
    world.rabbit.hold = True
    brain = ScriptedBrain([TextDelta("Une très longue phrase. Et encore une."), done()],
                          [TextDelta("D'accord, j'arrête."), done()])
    session = world.start(brain, FakeTranscriber(FakeTranscriber.BLOCK))
    world.detector.hear(hit(at=1000, command="raconte"))
    await until(lambda: session.state == "speaking")
    world.rabbit.hold = False
    world.detector.hear(hit(at=90000, command="arrête"))
    await until(lambda: len(brain.calls) == 2)
    assert world.rabbit.stops == 1
    await until(lambda: "D'accord, j'arrête." in world.spoken())
    assert "Et encore une." not in world.spoken()
    # Même conversation : l'historique du tour interrompu est gardé sans casser l'alternance.
    texts = [m["content"][0].get("text") for m in brain.calls[1] if m["role"] == "user"]
    assert texts[-1] == "arrête"
    assert world.db.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1


async def test_long_press_starts_listening(world):
    brain = ScriptedBrain([TextDelta("Coucou !"), done()])
    session = world.start(brain, FakeTranscriber("bonjour", None))
    session.on_button("long_start")
    await until(lambda: world.spoken() == ["Coucou !"])
    await world.idle()
    assert world.state.states()[1] == "listening"


async def test_home_assistant_down_speaks_one_sentence_and_session_lives(world):
    await world.start_ha(mcp_cmd=[sys.executable, "-c", "import sys; sys.exit(3)"])
    brain = ScriptedBrain([call_service("light", "turn_on", "light.salon"), done()],
                          [TextDelta("Coucou !"), done()])
    session = world.start(brain, FakeTranscriber(None, None))
    world.detector.hear(hit(command="allume le salon"))
    await until(lambda: world.spoken())
    await world.idle()
    assert world.spoken() == [HOME_DOWN_FR]
    assert len(brain.calls) == 1  # pas de second tour : la phrase est dite par Lapinou
    assert ("FF8000", None) in world.rabbit.leds  # erreur : orange
    assert world.state.states()[-1] == "idle"
    # Toujours vivante : un nouveau mot d'activation est servi.
    world.detector.hear(hit(at=500000, command="dis bonjour"))
    await until(lambda: world.spoken()[-1:] == ["Coucou !"])
    assert session.is_idle or session.state != "idle"


async def test_open_the_door_is_gently_refused(world):
    await world.start_ha()
    brain = ScriptedBrain(
        [call_service("lock", "unlock", "lock.porte_entree"), done()],
        [TextDelta("Ça, je n'ai pas le droit. Il faut demander à un grand."), done()],
    )
    world.start(brain, FakeTranscriber(None))
    world.detector.hear(hit(command="ouvre la porte"))
    await until(lambda: len(world.spoken()) == 2)
    await world.idle()
    assert service_calls(world.mcp_log) == []
    result = brain.calls[1][-1]["content"][0]
    assert result["is_error"] and result["content"].startswith("REFUSÉ")
    assert world.spoken() == ["Ça, je n'ai pas le droit.", "Il faut demander à un grand."]
    assert ("FF8000", None) not in world.rabbit.leds


async def test_sensitive_action_is_confirmed_by_the_next_utterance(world):
    ha = await world.start_ha(allowed_entities=["lock.porte_entree"])
    brain = ScriptedBrain([call_service("lock", "unlock", "lock.porte_entree"), done()],
                          [done()])
    world.start(brain, FakeTranscriber("oui", None))
    world.detector.hear(hit(command="ouvre la porte"))
    await until(lambda: len(world.spoken()) == 2)
    await world.idle()
    phrase = world.spoken()[0]
    assert phrase.startswith("Action sensible : déverrouiller « Porte d'entrée »")
    assert phrase.endswith("Réponds oui ou non.")
    assert world.spoken()[1] == conv_mod.CONFIRMED_FR
    assert service_calls(world.mcp_log) == [
        {"domain": "lock", "service": "unlock", "entity_id": "lock.porte_entree", "data": None}
    ]
    assert len(brain.calls) == 2  # « oui » ne passe pas par le cerveau
    assert ha.pending(RID) is None
    assert ("user", "oui") in world.turns()


async def test_sensitive_action_with_wrong_pin_is_not_done(world):
    await world.start_ha(allowed_entities=["lock.porte_entree"], pin="1234")
    brain = ScriptedBrain([call_service("lock", "unlock", "lock.porte_entree"), done()],
                          [done()])
    world.start(brain, FakeTranscriber("un deux trois cinq", None))
    world.detector.hear(hit(command="ouvre la porte"))
    await until(lambda: len(world.spoken()) == 2)
    await world.idle()
    assert world.spoken()[0].endswith("dis le code secret.")
    assert world.spoken()[1] == conv_mod.CANCELLED_FR
    assert service_calls(world.mcp_log) == []


async def test_wifi_cut_while_thinking_abandons_cleanly(world, caplog):
    block = Block()
    brain = ScriptedBrain([block, TextDelta("Jamais dit."), done()],
                          [TextDelta("Me revoilà."), done()])
    session = world.start(brain, FakeTranscriber(None))
    world.detector.hear(hit(command="raconte une blague"))
    await until(lambda: session.state == "thinking")
    with caplog.at_level(logging.WARNING):
        world.stream.connected = False  # la passerelle a perdu le flux
        world.rabbit.connected = False
        await world.idle()
    assert brain.closed == 1  # le cerveau a été arrêté
    assert world.rabbit.played == []
    assert world.state.published[-1] == (RID, "offline")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    row = world.db.conn.execute("SELECT ended_at FROM conversations").fetchone()
    assert row["ended_at"] is not None
    # Le lapin revient : la session repart.
    world.stream.connected = world.rabbit.connected = True
    world.detector.hear(hit(at=999999, command="coucou"))
    await until(lambda: world.spoken() == ["Me revoilà."])


async def test_stream_closed_ends_the_session_quietly(world, caplog):
    block = Block()
    brain = ScriptedBrain([block, done()])
    session = world.start(brain, FakeTranscriber(None))
    world.detector.hear(hit(command="raconte"))
    await until(lambda: session.state == "thinking")
    with caplog.at_level(logging.WARNING):
        world.stream._close()
        await world.idle()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_retract_drops_the_queued_sentences_of_that_call(world):
    world.voice.gate = asyncio.Event()
    brain = ScriptedBrain([TextDelta("Phrase un. Phrase deux. "), Retract(),
                           TextDelta("Refus doux."), done()])
    world.start(brain, FakeTranscriber(None))
    world.detector.hear(hit(command="dis un truc"))
    await until(lambda: len(brain.calls) == 1 and brain.closed == 1)
    world.voice.gate.set()
    await world.idle()
    assert world.spoken() == ["Refus doux."]
    assert world.turns()[-1] == ("assistant", "Refus doux.")


async def test_retract_stops_the_sentence_being_played(world):
    world.rabbit.hold = True
    gate = Block()
    brain = ScriptedBrain([TextDelta("Phrase un. "), gate, Retract(), TextDelta("Refus doux."),
                           done()])
    session = world.start(brain, FakeTranscriber(None))
    world.detector.hear(hit(command="dis un truc"))
    await until(lambda: session.state == "speaking")
    world.rabbit.hold = False
    gate.event.set()
    await world.idle()
    assert world.rabbit.stops == 1
    assert world.spoken() == ["Phrase un.", "Refus doux."]


async def test_wake_hits_are_deduplicated_and_a_grown_command_is_used(world):
    brain = ScriptedBrain([TextDelta("Allumé."), done()])
    world.start(brain, FakeTranscriber(None), wake_grow_s=2.0)
    world.detector.hear(hit(at=5000, command="", complete=False))
    await asyncio.sleep(0.05)
    world.detector.hear(hit(at=5000, command="allume le salon", complete=False),
                        hit(at=5000, command="allume le salon s'il te plaît", complete=True))
    await until(lambda: world.spoken())
    await world.idle()
    assert len(brain.calls) == 1
    assert brain.calls[0][-1]["content"][0]["text"] == "allume le salon"
    assert world.transcriber.calls[0]["no_speech_s"] == 5  # seule la fenêtre de suite a écouté


async def test_incomplete_wake_without_command_listens_after_a_short_wait(world):
    brain = ScriptedBrain([TextDelta("Oui ?"), done()])
    world.start(brain, FakeTranscriber("tu es là", None), wake_grow_s=0.1)
    world.detector.hear(hit(at=5000, command="", complete=False))
    await until(lambda: world.spoken())
    assert brain.calls[0][-1]["content"][0]["text"] == "tu es là"


async def test_followup_strips_a_repeated_wake_word(world):
    brain = ScriptedBrain([TextDelta("Bonjour."), done()], [TextDelta("Allumé."), done()])
    world.start(brain, FakeTranscriber("Lapinou, allume le salon", None))
    world.detector.hear(hit(command="bonjour"))
    await until(lambda: len(world.spoken()) == 2)
    assert brain.calls[1][-1]["content"][0]["text"] == "allume le salon"


async def test_memories_are_injected_for_the_utterance(world):
    brain = ScriptedBrain([TextDelta("Bien sûr."), done()])
    world.start(brain, FakeTranscriber(None), memory=FakeMemory(["Léa aime les chats"]))
    world.detector.hear(hit(command="qu'est-ce que j'aime"))
    await until(lambda: world.spoken())
    assert brain.memories == ["Léa aime les chats"]


async def test_tool_choreography_is_played(world):
    steps = [Step(ears=(5, 5), wait_ms=0)]

    async def story(args, ctx):
        return ToolResult("Une histoire.", choreography=steps)

    tools = ToolBox([Tool("histoire", "Une histoire", strict_schema({}), story)])
    brain = ScriptedBrain([ToolCall("s1", "histoire", {}), done()],
                          [TextDelta("Il était une fois."), done()])
    world.start(brain, FakeTranscriber(None), tools=tools)
    world.detector.hear(hit(command="une histoire"))
    await until(lambda: world.rabbit.dances == [steps])


async def test_thinking_shows_violet_and_moves_the_ears(world):
    block = Block()
    brain = ScriptedBrain([block, TextDelta("Fini."), done()])
    session = world.start(brain, FakeTranscriber(None))
    session.gestures.wiggle = (((3, 0), 10), ((0, 3), 10))
    world.detector.hear(hit(command="réfléchis"))
    await until(lambda: ("8000FF", None) in world.rabbit.leds)
    await until(lambda: {(3, 0), (0, 3)} <= set(world.rabbit.ears_moves))
    block.event.set()
    await until(lambda: ("00FF00", None) in world.rabbit.leds)
    await world.idle()
    assert world.rabbit.ears_moves[-1] == (0, 0)


async def test_say_speaks_then_opens_the_followup_window(world):
    brain = ScriptedBrain([TextDelta("Oui, c'est l'heure."), done()])
    session = world.start(brain, FakeTranscriber("déjà ?", None))
    await session.say("Le minuteur est fini.")
    assert world.spoken() == ["Le minuteur est fini."]
    await until(lambda: len(world.spoken()) == 2)
    await world.idle()
    assert brain.calls[0][-1]["content"][0]["text"] == "déjà ?"


async def test_say_waits_for_the_current_conversation(world):
    block = Block()
    brain = ScriptedBrain([block, TextDelta("Réponse."), done()])
    session = world.start(brain, FakeTranscriber(None, None))
    world.detector.hear(hit(command="question"))
    await until(lambda: session.state == "thinking")
    announce = asyncio.create_task(session.say("Annonce."))
    await asyncio.sleep(0.05)
    assert not announce.done()
    block.event.set()
    await asyncio.wait_for(announce, 3)
    assert world.spoken() == ["Réponse.", "Annonce."]


async def test_say_to_a_disconnected_rabbit_raises(world):
    session = world.start(ScriptedBrain(), FakeTranscriber())
    world.rabbit.connected = False
    with pytest.raises(ConnectionError):
        await session.say("Coucou.")


async def test_tts_failure_shows_error_and_returns_to_idle(world, caplog):
    world.voice.fail = True
    brain = ScriptedBrain([TextDelta("Bonjour."), done()])
    world.start(brain, FakeTranscriber(None))
    world.detector.hear(hit(command="bonjour"))
    await until(lambda: ("FF8000", None) in world.rabbit.leds)
    await world.idle()
    world.voice.fail = False


# --- Journal et rétention -----------------------------------------------------------------


def test_conversation_log_purges_after_retention(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    now = [dt.datetime(2026, 11, 1, tzinfo=dt.UTC)]
    log = ConversationLog(db, retention_days=7, clock=lambda: now[0])
    old = log.start(RID)
    log.turn(old, "user", "ancien")
    log.end(old)
    now[0] += dt.timedelta(days=8)
    new = log.start(RID)  # purge à chaque nouvelle conversation
    log.turn(new, "user", "récent")
    assert [r[0] for r in db.conn.execute("SELECT id FROM conversations")] == [new]
    assert [r[0] for r in db.conn.execute("SELECT text FROM turns")] == ["récent"]
    db.close()


# --- Gestes -------------------------------------------------------------------------------


async def test_gestures_survive_a_disconnected_rabbit():
    rabbit = FakeRabbit()
    rabbit.connected = False
    gestures = Gestures(rabbit)
    gestures.show("listening")
    await asyncio.sleep(0.02)
    rabbit.connected = True
    gestures.show("error")
    await until(lambda: rabbit.leds == [("FF8000", None)])
    await gestures.close()
    assert set(LOOKS) >= {"idle", "listening", "thinking", "speaking", "error"}


# --- MQTT ---------------------------------------------------------------------------------


class FakeMqtt:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.auth = None
        self.connected_to = None
        self.on_connect = None
        self.stopped = False

    def username_pw_set(self, user, password):
        self.auth = (user, password)

    def reconnect_delay_set(self, a, b):
        pass

    def connect_async(self, host, port, keepalive):
        self.connected_to = (host, port)

    def loop_start(self):
        pass

    def loop_stop(self):
        self.stopped = True

    def is_connected(self):
        return False

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

    def disconnect(self):
        pass


def test_assistant_state_is_published_retained_and_republished_on_connect():
    fake = FakeMqtt()
    state = AssistantState("broker", 1884, "lapin", "p$a;ss \"é", client_factory=lambda: fake)
    state.publish(RID, "listening")  # avant la connexion : gardé
    state.start()
    assert fake.auth == ("lapin", "p$a;ss \"é") and fake.connected_to == ("broker", 1884)
    fake.on_connect(fake, None, None, 0, None)
    state.publish(RID, "thinking")
    state.publish(RID, "thinking")  # inchangé : pas republié
    state.publish("x/#", "speaking")  # identifiant hors topic : ignoré
    state.publish("lapin-b", "speaking")
    assert fake.published == [
        ("lapinou/lapin-a/assistant", "écoute", 1, True),
        ("lapinou/lapin-a/assistant", "réfléchit", 1, True),
        ("lapinou/lapin-b/assistant", "parle", 1, True),
    ]
    state.close()
    assert fake.published[-2:] == [("lapinou/lapin-a/assistant", "hors ligne", 1, True),
                                   ("lapinou/lapin-b/assistant", "hors ligne", 1, True)]
    assert fake.stopped


def test_assistant_state_without_broker_does_nothing():
    state = AssistantState("", client_factory=lambda: pytest.fail("pas de client"))
    state.start()
    state.publish(RID, "speaking")
    state.close()
    assert not state.enabled


# --- Orchestrateur (plusieurs lapins) -----------------------------------------------------


class FakeRegistry:
    def __init__(self) -> None:
        self.rabbits: dict[str, FakeRabbit] = {}
        self.on_button = self.on_playback = None

    def get(self, rabbit_id):
        return self.rabbits.setdefault(rabbit_id, FakeRabbit(rabbit_id))


class FakeGateway:
    def __init__(self) -> None:
        self.streams: dict[str, RabbitStream] = {}
        self.on_stream = None


async def test_orchestrator_creates_a_session_per_stream_and_routes_events(tmp_path):
    registry, gateway = FakeRegistry(), FakeGateway()
    gateway.streams["lapin-a"] = RabbitStream("lapin-a")  # déjà connecté avant l'attache
    voice = FakeVoice()
    deps = SessionDeps(brain=ScriptedBrain(), tools=ToolBox([]), voice=voice,
                       transcriber=FakeTranscriber(), make_detector=FakeDetector)
    orch = Orchestrator(deps)
    orch.attach(registry, gateway)
    stream_b = RabbitStream("lapin-b")
    stream_b.connected = True
    gateway.streams["lapin-b"] = stream_b
    gateway.on_stream(stream_b)
    assert set(orch.sessions) == {"lapin-a", "lapin-b"}
    with pytest.raises(ConnectionError):
        await orch.say("lapin-z", "Coucou.")
    rabbit_b = registry.get("lapin-b")
    rabbit_b.on_playback = lambda pid, status: registry.on_playback(rabbit_b, pid, status)
    await orch.say("lapin-b", "Coucou.")
    assert [voice.texts[pid] for _, pid in rabbit_b.played] == ["Coucou."]
    assert registry.rabbits["lapin-a"].played == []
    await until(lambda: orch.sessions["lapin-b"].is_idle)
    registry.on_button(rabbit_b, "long_start")
    await until(lambda: orch.sessions["lapin-b"].state == "listening")
    await orch.close()


# --- Câblage de production ----------------------------------------------------------------


def test_production_services_wire_voice_tools_and_tts_compat(tmp_path):
    from fastapi.testclient import TestClient

    from lapinou import app as app_mod

    settings = Settings()
    settings.server.platform_port = settings.server.mic_port = 0
    settings.server.tts_port = 0
    settings.server.timezone = "America/Montreal"
    settings.safety.pin = "4321"
    settings.lms.host = "lms.test"
    settings.lms.allowed_url_hosts = ["radio.test"]

    closed = []

    class Voice(FakeVoice):
        store = object()

        def attach(self, app):
            app.state.tts_store = self.store

        def close(self):
            closed.append(True)

    services = app_mod.build_services(
        settings, db_path=tmp_path / "db.sqlite", voice=Voice(), recogniser=None,
        brain=ScriptedBrain(),
    )
    memory = next(t for t in services.tool_sources if type(t).__name__ == "MemoryTools")
    timers = next(t for t in services.tool_sources if type(t).__name__ == "TimerTools")
    lms = next(t for t in services.tool_sources if type(t).__name__ == "LmsTools")
    assert memory._pin == "4321"
    assert str(timers.tz) == "America/Montreal"
    assert lms.allowed_url_hosts == {"radio.test"}
    assert timers.announcer is services.orchestrator
    app = app_mod.create_app(settings, services=services)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert app.state.tts_store is Voice.store
        assert services.tts_server is not None
        port = services.tts_server.server_address[1]
        assert port > 0
    assert closed == [True]
    assert services.tts_server is None


# --- Rapport de correction 1 --------------------------------------------------------------


async def test_offline_then_idle_follow_the_rabbit_while_idle(world):
    world.start(ScriptedBrain(), FakeTranscriber())
    world.rabbit.connected = False
    await until(lambda: world.state.published[-1] == (RID, "offline"))
    world.rabbit.connected = True
    await until(lambda: world.state.published[-1] == (RID, "idle"))
    world.stream.connected = False  # le flux micro seul suffit aussi
    await until(lambda: world.state.published[-1] == (RID, "offline"))
    world.stream.connected = True
    await until(lambda: world.state.published[-1] == (RID, "idle"))


async def test_interrupt_on_an_unreachable_rabbit_does_not_listen(world):
    world.rabbit.hold = True
    brain = ScriptedBrain([TextDelta("Une longue histoire. La suite."), done()])
    session = world.start(brain, FakeTranscriber(FakeTranscriber.BLOCK))
    world.detector.hear(hit(command="raconte"))
    await until(lambda: session.state == "speaking")
    world.rabbit.connected = False
    session.on_button("simple")  # avant que la surveillance n'ait vu la coupure
    await world.idle()
    await asyncio.sleep(0.1)
    assert world.transcriber.calls == []
    assert "listening" not in world.state.states()
    assert world.state.published[-1] == (RID, "offline")
    assert session.is_idle


async def test_pending_action_expires_when_the_conversation_ends(world):
    ha = await world.start_ha(allowed_entities=["lock.porte_entree"])
    brain = ScriptedBrain([call_service("lock", "unlock", "lock.porte_entree"), done()],
                          [done()], [TextDelta("Oui quoi ?"), done()])
    world.start(brain, FakeTranscriber(None, None))
    world.detector.hear(hit(command="ouvre la porte"))
    await until(lambda: len(world.spoken()) == 1)
    await world.idle()  # fenêtre de suite passée sans réponse
    assert ha.pending(RID) is None
    world.detector.hear(hit(at=900000, command="oui"))  # « Lapinou, oui » bien plus tard
    await until(lambda: world.spoken()[-1:] == ["Oui quoi ?"])
    assert service_calls(world.mcp_log) == []
    assert brain.calls[-1][-1]["content"][0]["text"] == "oui"  # tour normal du cerveau


async def test_announcement_returns_once_playing_and_is_not_repeated(world):
    world.rabbit.hold = True  # lecture plus longue que le délai de l'appelant (minuteur)
    session = world.start(ScriptedBrain(), FakeTranscriber(None))
    await asyncio.wait_for(session.say("Le minuteur est fini."), 1)
    assert world.spoken() == ["Le minuteur est fini."]
    world.rabbit.hold = False
    await world.rabbit.stop_playback()
    await world.idle()
    assert world.rabbit.started == 1


async def test_announcement_abandoned_by_its_caller_is_not_spoken(world):
    world.voice.gate = asyncio.Event()  # synthèse lente
    session = world.start(ScriptedBrain(), FakeTranscriber(None))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(session.say("Annonce."), 0.1)
    world.voice.gate.set()
    await world.idle()
    await asyncio.sleep(0.05)
    assert world.rabbit.played == []  # le minuteur la redira : pas de double annonce


def test_services_started_partly_are_closed_on_failure(tmp_path):
    from fastapi.testclient import TestClient

    from lapinou import app as app_mod

    settings = Settings()
    settings.server.platform_port = settings.server.mic_port = settings.server.tts_port = 0
    closed = []

    class Voice(FakeVoice):
        def attach(self, app):
            pass

        def close(self):
            closed.append(True)

    services = app_mod.build_services(settings, db_path=tmp_path / "db.sqlite", voice=Voice(),
                                      recogniser=None, brain=ScriptedBrain())

    async def boom():
        raise RuntimeError("minuteurs en panne")

    services.timers.start = boom
    with pytest.raises(RuntimeError), TestClient(app_mod.create_app(settings, services=services)):
        pass
    assert closed == [True]
    assert not services.registry._server.is_serving()


@pytest.mark.skipif(shutil.which("protoc") is None and "LAPINOU_PROTOC" not in os.environ,
                    reason="protoc introuvable")
async def test_echo_marks_from_a_real_controller_find_their_reference():
    """De bout en bout avec la vraie plateforme : le contrôleur marque la lecture avec l'id du
    message `multimedia play` (comme multimedia-daemon) ; l'annuleur doit y trouver le PCM."""
    from test_platform import FakeController

    from lapinou.rabbit.platform import build_codec
    from lapinou.rabbit.registry import RabbitRegistry
    from lapinou.voice.aec import EchoCanceller

    codec = build_codec()
    registry = RabbitRegistry(codec=codec)
    await registry.start("127.0.0.1", 0)
    gateway = FakeGateway()
    stream = RabbitStream(RID)
    stream.connected = True
    gateway.streams[RID] = stream
    looked_up: list[tuple[str, bool]] = []

    def make_aec(reference, marks):
        def spy(playback_id):
            pcm = reference(playback_id)
            looked_up.append((playback_id, pcm is not None))
            return pcm

        return EchoCanceller(spy, marks)

    voice = FakeVoice()
    orch = Orchestrator(SessionDeps(brain=ScriptedBrain(), tools=ToolBox([]), voice=voice,
                                    transcriber=FakeTranscriber(None), make_aec=make_aec))
    controller = FakeController(codec, RID)
    seq = [0, 0]  # numéro de trame, premier échantillon

    def feed(mark=None):
        stream._feed(Frame(RID, seq[0], mulaw=b"\xff" * CHUNK, first_sample=seq[1], mark=mark))
        seq[0] += 1
        seq[1] += CHUNK

    async def controller_side():
        play = await controller.until(r"^multimedia \{\s*play")
        pid = re.search(r'^id: "([^"]*)"', play, re.MULTILINE).group(1)
        feed(mark=(pid, seq[1], True))
        for _ in range(5):
            feed()
            await asyncio.sleep(0.01)
        feed(mark=(pid, seq[1], False))
        await controller.send('id: "m1" multimediaCallback { action: PLAY }')
        await controller.send('id: "m2" multimediaCallback { action: STOP }')
        return pid

    side = None
    try:
        await controller.connect(registry.port)
        await controller.until(r"^interactiveMode \{")
        await asyncio.wait_for(registry.get(RID).wait_ready(), 10)
        orch.attach(registry, gateway)
        side = asyncio.create_task(controller_side())
        await asyncio.wait_for(orch.say(RID, "Bonjour."), 5)
        pid = await asyncio.wait_for(side, 5)
        await until(lambda: looked_up)
        assert pid in voice.texts  # l'id du message `play` est le Speech.id
        assert looked_up == [(pid, True)]
    finally:
        if side is not None:
            side.cancel()
        await orch.close()
        await controller.close()
        await registry.close()


async def test_gestures_survive_an_unexpected_error():
    rabbit = FakeRabbit()
    calls = []

    async def led(color, pulse=None, period_ms=None):
        calls.append(color)
        if len(calls) == 1:
            raise RuntimeError("inattendu")

    rabbit.led = led
    gestures = Gestures(rabbit)
    gestures.show("listening")
    await until(lambda: calls == ["0000FF"])
    gestures.show("error")
    await until(lambda: calls == ["0000FF", "FF8000"])
    await gestures.close()
