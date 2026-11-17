# SPDX-License-Identifier: MIT
"""Orchestrateur de conversation : un `RabbitSession` par lapin, un `Orchestrator` pour tous.

États : IDLE → LISTENING → THINKING → SPEAKING → FOLLOWUP (fenêtre de suite, 5 s) → IDLE.

- Déclencheurs : le mot d'activation (détecteur par transcription, R23), l'appui long
  (`lclick_start`), une annonce (`say` : minuteurs, rappels, notifications passées au serveur).
- Interruption : le mot d'activation ou un clic pendant SPEAKING arrête la lecture
  (`Rabbit.stop_playback`) et repart en LISTENING (ou directement sur la commande entendue).
- Flux micro : un seul consommateur par lapin (`RabbitStream.frames()`), qui passe chaque
  bloc dans l'annuleur d'écho (dans la boucle, comme le veut `EchoCanceller.process`) puis
  le donne soit au détecteur (repos, parole), soit à la transcription (écoute, suite).
- Le détecteur est appelé dans un fil (`asyncio.to_thread`), un instantané `WakeHit` est
  traité une fois par `at_sample` : on agit sur la première commande non vide, ou, sans
  commande, sur un instantané complet ou après `wake_grow_s`. Ni `WakeHit.text` ni
  `WakeHit.command` ne sont journalisés.
- Réponse : les phrases partent une à une dès qu'elles sont complètes (synthèse de la suivante
  pendant la lecture) ; `Retract` (portée : l'appel `respond()` en cours) arrête la lecture et
  oublie les phrases en file de cet appel.
- Actions sensibles : la question officielle (`PendingAction.phrase`) est dite telle quelle,
  et la phrase suivante de l'enfant va à `confirm()` sans passer par le cerveau ; ni
  l'historique ni `turns` ne la gardent en clair (« [confirmation] » ou « [code] », R48).
- Identité (R47) : un flux micro sans plateforme et une plateforme sans flux en même temps
  (login de account.conf différent de uuid.conf) sont signalés par un avertissement clair.
- Pannes : Home Assistant injoignable → « Je n'arrive pas à joindre la maison. » et retour au
  repos ; lapin perdu (flux ou plateforme) → conversation abandonnée sans rien dire ; toute
  autre panne → LED orange, une phrase, retour au repos. La session survit.
- Journal : tables `conversations` / `turns`, purgées au-delà de `logs.retention_days`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import re
import sqlite3
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import numpy as np

from .brain.base import (
    Conversation,
    Retract,
    TextDelta,
    ToolCall,
    ToolResult,
    add_assistant_text,
    add_tool_results,
    add_user_text,
)
from .db import Database
from .gestures import Gestures
from .tools.base import ToolBox, ToolContext
from .tools.ha import UNAVAILABLE as HA_UNAVAILABLE
from .voice.asr import NO_SPEECH_S
from .voice.tts import split_sentences
from .voice.wake_asr import WakeHit, match_wake_word

log = logging.getLogger(__name__)

HOME_DOWN_FR = "Je n'arrive pas à joindre la maison."
ERROR_FR = "Oups, j'ai un petit souci. On réessaie dans un moment ?"
LOST_FR = "Oups, je me suis perdu. On réessaie ?"
CONFIRMED_FR = "C'est fait."
CANCELLED_FR = "D'accord, je ne fais rien."
FAILED_FR = "Je n'ai pas réussi à le faire."
# Ce que l'historique et le journal gardent d'une réponse à une confirmation (R48).
CONFIRM_MARK = "[confirmation]"
CODE_MARK = "[code]"

MAX_TOOL_ROUNDS = 5  # tours d'outils par phrase de l'enfant
PLAY_MARGIN_S = 5.0  # fin de lecture attendue au plus durée + marge
REFERENCES_KEPT = 16  # PCM des dernières phrases, pour l'annuleur d'écho
WAKE_QUEUE_MAX = 300  # blocs en attente du détecteur (~40 s) ; au-delà, le plus ancien part
_SENTENCE_DONE = re.compile(r"[.!?…]+[»”\"')\]]*\s*$")
CLICKS = ("simple", "click")  # appui bref (ButtonCallback.SIMPLE)
LONG_PRESSES = ("long_start", "lclick_start")  # début d'un appui long (LONG_START)
_RESET = object()  # dans la file du détecteur : oublier la parole en cours
_END = object()  # un tour qui clôt la conversation


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    FOLLOWUP = "followup"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class ConversationLog:
    """Tables `conversations` et `turns` ; ce qui a plus de `retention_days` est effacé."""

    def __init__(
        self, db: Database, retention_days: int = 7,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self.db = db
        self.retention_days = retention_days
        self._clock = clock

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")

    def purge(self) -> int:
        limit = self._clock() - dt.timedelta(days=self.retention_days)
        cur = self.db.conn.execute(
            "DELETE FROM conversations WHERE started_at < ?",
            (limit.isoformat(timespec="seconds"),),
        )
        self.db.conn.commit()
        return cur.rowcount

    def start(self, rabbit_id: str) -> int:
        self.purge()
        cur = self.db.conn.execute(
            "INSERT INTO conversations(rabbit_id, started_at) VALUES (?, ?)",
            (rabbit_id, self._now()),
        )
        self.db.conn.commit()
        return int(cur.lastrowid)

    def turn(self, conversation_id: int, role: str, text: str) -> None:
        self.db.conn.execute(
            "INSERT INTO turns(conversation_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, text, self._now()),
        )
        self.db.conn.commit()

    def end(self, conversation_id: int) -> None:
        self.db.conn.execute(
            "UPDATE conversations SET ended_at = ? WHERE id = ?", (self._now(), conversation_id)
        )
        self.db.conn.commit()


@dataclass
class SessionDeps:
    """Ce dont une session a besoin (partagé par tous les lapins, sauf les fabriques).

    `brain` : `respond(conv, tools, budget)` (le `BrainRouter`) ; `voice` : `speak(text)` →
    `Speech` ; `transcriber` : `transcribe_utterance(...)` ; `make_detector()` : un détecteur
    de mot d'activation par lapin (`feed`, `reset`) ; `make_aec(reference, marks)` : un
    annuleur d'écho par lapin ; `ha` : `pending` / `confirm` des actions sensibles ;
    `memory` : `relevant(phrase)` ; `state` : `publish(rabbit_id, état)` (MQTT).
    """

    brain: Any
    tools: ToolBox
    voice: Any
    transcriber: Any
    make_detector: Callable[[], Any] | None = None
    make_aec: Callable[[Callable[[str], np.ndarray | None], Mapping], Any] | None = None
    budget: Any = None
    ha: Any = None
    memory: Any = None
    log: ConversationLog | None = None
    state: Any = None
    names: tuple[str, ...] = ("lapinou",)
    followup_s: float = 5.0
    silence_ms: int = 700
    max_listen_s: float = 15.0
    wake_grow_s: float = 1.5  # attente d'une commande après un « Lapinou » encore ouvert
    watch_s: float = 0.25  # période de vérification de la connexion du lapin


class _Speaker:
    """Les phrases d'un tour : synthétisées dans l'ordre (une d'avance), jouées une à une.

    Chaque phrase porte l'époque de l'appel `respond()` qui l'a produite ; `retract()` tue
    l'époque en cours (file vidée, lecture arrêtée) et en ouvre une nouvelle."""

    def __init__(self, session: RabbitSession, look: str | None = None,
                 on_start: Callable[[], None] | None = None) -> None:
        self._s = session
        self._look = look
        self._on_start = on_start  # appelé quand le lapin a accepté la première lecture
        self._epoch = 0
        self._dead: set[int] = set()
        self._texts: deque[tuple[int, str]] = deque()
        self._wake = asyncio.Event()
        self._closed = False
        self._playing: tuple[int, str, str] | None = None  # (époque, playback_id, texte)
        self._synth: set[asyncio.Task] = set()
        self.spoken: list[str] = []  # phrases jouées en entier et non retirées
        self._task = asyncio.create_task(self._run())

    @property
    def idle(self) -> bool:
        return self._playing is None and not self._texts and not self._synth

    @property
    def heard(self) -> list[str]:
        """Ce que l'enfant a entendu, la phrase en cours comprise."""
        playing = self._playing
        if playing is not None and playing[0] not in self._dead:
            return [*self.spoken, playing[2]]
        return list(self.spoken)

    def new_call(self) -> None:
        self._epoch += 1

    def push(self, text: str) -> None:
        self._texts.append((self._epoch, text))
        self._wake.set()

    async def retract(self) -> None:
        dead = self._epoch
        self._dead.add(dead)
        self._epoch += 1
        self._texts = deque(item for item in self._texts if item[0] != dead)
        playing = self._playing
        if playing is not None and playing[0] == dead:
            await self._s._stop(playing[1])

    async def close(self) -> None:
        """Attend la fin de la dernière phrase (lève l'erreur de synthèse ou de lecture)."""
        self._closed = True
        self._wake.set()
        await self._task

    def cancel(self) -> None:
        self._task.cancel()
        for task in self._synth:
            task.cancel()

    def _started(self) -> None:
        on_start, self._on_start = self._on_start, None
        if on_start is not None:
            on_start()

    def _synthesize(self, item: tuple[int, str]) -> tuple[int, str, asyncio.Task]:
        task = asyncio.create_task(self._s.deps.voice.speak(item[1]))
        self._synth.add(task)
        task.add_done_callback(self._synth.discard)
        return item[0], item[1], task

    async def _next(self) -> tuple[int, str] | None:
        while not self._texts:
            if self._closed:
                return None
            self._wake.clear()
            await self._wake.wait()
        return self._texts.popleft()

    async def _run(self) -> None:
        upcoming = None
        try:
            while True:
                if upcoming is None:
                    item = await self._next()
                    if item is None:
                        return
                    upcoming = self._synthesize(item)
                epoch, text, task = upcoming
                speech = await task
                upcoming = None
                if epoch in self._dead:
                    continue
                if self._texts:  # une phrase d'avance pendant la lecture
                    upcoming = self._synthesize(self._texts.popleft())
                self._playing = (epoch, speech.id, text)
                try:
                    self._s._remember(speech)
                    self._s._set_state(State.SPEAKING, look=self._look)
                    await self._s._play(speech, self._started)
                finally:
                    self._playing = None
                if epoch not in self._dead:
                    self.spoken.append(text)
        finally:
            for task in self._synth:
                task.cancel()


class RabbitSession:
    """La conversation d'un lapin (`Rabbit` de la plateforme + son `RabbitStream`)."""

    def __init__(self, rabbit, stream, deps: SessionDeps) -> None:
        self.rabbit, self.stream, self.deps = rabbit, stream, deps
        self.id: str = rabbit.id
        self.state = State.IDLE
        self.gestures = Gestures(rabbit)
        self._look: str | None = None
        self._refs: OrderedDict[str, np.ndarray] = OrderedDict()
        self._aec = None
        if deps.make_aec is not None and stream is not None:
            self._aec = deps.make_aec(self._reference, stream.playback_marks)
        self._detector = None
        if deps.make_detector is not None:
            try:
                self._detector = deps.make_detector()
            except Exception:  # la session existe quand même (bouton, annonces)
                log.exception("lapin %s : détecteur du mot d'activation impossible", self.id)
        self._wake_q: asyncio.Queue = asyncio.Queue()
        self._listen_q: asyncio.Queue | None = None
        self._seen: deque[int] = deque(maxlen=32)
        self._grow: tuple[int, asyncio.Task] | None = None
        self._task: asyncio.Task | None = None  # la conversation en cours
        self._idle = asyncio.Event()
        self._idle.set()
        self._playback: dict[str, asyncio.Event] = {}
        self._conv: Conversation | None = None
        self._conv_id: int | None = None
        self._bg: set[asyncio.Task] = set()
        self._closing = False
        self._aec_failed = False

    @property
    def is_idle(self) -> bool:
        return self._idle.is_set()

    # --- Cycle de vie -------------------------------------------------------------------

    def start(self) -> None:
        if self.stream is not None:
            self._spawn(self._pump())
        if self._detector is not None:
            self._spawn(self._wake_loop())
        reachable = self._reachable()
        self._spawn(self._watch(reachable))
        self._publish(State.IDLE if reachable else "offline")

    async def close(self) -> None:
        self._closing = True
        if self._grow is not None:
            self._grow[1].cancel()
            self._grow = None
        await self._abandon(offline=True)
        tasks = list(self._bg)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.gestures.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg_done)
        return task

    def _bg_done(self, task: asyncio.Task) -> None:
        self._bg.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("lapin %s : tâche en échec", self.id, exc_info=task.exception())

    def _reachable(self) -> bool:
        return bool(self.rabbit.connected) and (self.stream is None or self.stream.connected)

    # --- Événements -----------------------------------------------------------------------

    def on_button(self, kind: str) -> None:
        """Bouton de la tête, depuis le registre (types du contrôleur en minuscules)."""
        interrupt = kind in CLICKS + LONG_PRESSES and self.state == State.SPEAKING
        listen = kind in LONG_PRESSES and self._task is None
        if interrupt or listen:
            self._trigger(None)

    def on_playback(self, playback_id: str, status: str) -> None:
        if status in ("stopped", "error"):
            done = self._playback.get(playback_id)
            if done is not None:
                done.set()

    async def say(self, text: str) -> None:
        """Annonce (minuteur, rappel, notification) : attend la fin de la conversation en
        cours, puis dit `text` et ouvre la fenêtre de suite. Rend la main dès que le lapin a
        commencé à jouer l'annonce (un délai de l'appelant ne la fait donc jamais répéter).
        ConnectionError si le lapin est injoignable ou que l'annonce n'a pas pu commencer."""
        while True:
            if not self._reachable() or self._closing:
                raise ConnectionError(f"lapin {self.id} injoignable")
            if self._task is None:
                break
            await self._idle.wait()
        said = asyncio.get_running_loop().create_future()
        self._begin(self._run(announce=text, announced=said))
        task = self._task
        try:
            await asyncio.shield(said)  # annuler l'appelant n'annule pas `said`
        except asyncio.CancelledError:
            # L'appelant (délai du minuteur) renonce avant que le lapin ne parle : l'annonce
            # n'est pas dite, il la redira. Une fois commencée, elle n'est jamais répétée.
            if not said.done() and self._task is task:
                self._spawn(self._abandon(offline=not self._reachable()))
            raise

    # --- Flux micro -----------------------------------------------------------------------

    async def _pump(self) -> None:
        """Seul lecteur du flux : écho retiré, puis détecteur ou transcription."""
        async for chunk in self.stream.frames():
            pcm = chunk.pcm
            if self._aec is not None:
                try:
                    pcm = self._aec.process(pcm, chunk.first_sample)
                except Exception:
                    if not self._aec_failed:
                        log.exception("lapin %s : annulation d'écho en échec", self.id)
                        self._aec_failed = True
            if not len(pcm):
                continue
            if self._listen_q is not None:
                self._listen_q.put_nowait(pcm)
            elif self._detector is not None and (
                self.state in (State.IDLE, State.SPEAKING) or self._grow is not None
            ):
                if self._wake_q.qsize() >= WAKE_QUEUE_MAX:
                    self._wake_q.get_nowait()
                self._wake_q.put_nowait(pcm)
        log.info("lapin %s : flux micro fermé", self.id)
        await self._abandon(offline=True)

    async def _wake_loop(self) -> None:
        while True:
            item = await self._wake_q.get()
            try:
                if item is _RESET:
                    # reset() peut rendre la clôture d'une détection restée ouverte : on
                    # l'ignore, car elle date d'avant l'écoute ou la parole qui commence
                    # (sa commande, s'il y en avait une, a déjà été servie).
                    await asyncio.to_thread(self._detector.reset)
                    continue
                hits = await asyncio.to_thread(self._detector.feed, item)
            except Exception:
                log.exception("lapin %s : détecteur du mot d'activation en échec", self.id)
                continue
            for hit in hits:
                self._on_hit(hit)

    def _wake_reset(self) -> None:
        if self._detector is not None:
            self._wake_q.put_nowait(_RESET)

    def _on_hit(self, hit: WakeHit) -> None:
        """Un instantané du détecteur (jamais journalisé : c'est la parole de l'enfant)."""
        if self._grow is not None and self._grow[0] == hit.at_sample:
            if hit.command or hit.complete:
                self._grow[1].cancel()
                self._grow = None
                self._trigger(hit.command or None)
            return
        if hit.at_sample in self._seen:
            return
        self._seen.append(hit.at_sample)
        if self.state not in (State.IDLE, State.SPEAKING) or self._grow is not None:
            return
        if hit.command or hit.complete or self.state == State.SPEAKING:
            self._trigger(hit.command or None)
        else:  # « Lapinou » d'une parole encore ouverte : la commande peut suivre
            self._grow = (hit.at_sample, self._spawn(self._grow_timeout()))

    async def _grow_timeout(self) -> None:
        await asyncio.sleep(self.deps.wake_grow_s)
        self._grow = None
        self._trigger(None)

    async def _watch(self, reachable: bool) -> None:
        """Suit la connexion du lapin (plateforme et flux micro) : une conversation en cours
        est abandonnée à la coupure ; au repos, « hors ligne » puis « repos » au retour."""
        while True:
            await asyncio.sleep(self.deps.watch_s)
            now = self._reachable()
            if self._task is not None and not now:
                log.info("lapin %s perdu en pleine conversation : abandon", self.id)
                await self._abandon(offline=True)
            elif now != reachable and self._task is None:
                self._publish(State.IDLE if now else "offline")
            reachable = now

    # --- Conversation ---------------------------------------------------------------------

    def _trigger(self, first: str | None) -> None:
        if self._closing:
            return
        if self._task is None:
            self._begin(self._run(first))
        else:
            self._spawn(self._interrupt(first))

    def _begin(self, coro) -> None:
        self._idle.clear()
        self._task = asyncio.create_task(coro)

    async def _interrupt(self, first: str | None) -> None:
        if not self._reachable():
            return  # la surveillance abandonne la conversation
        old = self._task
        if old is not None:
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        with contextlib.suppress(ConnectionError, OSError):
            await self.rabbit.stop_playback()
        if self._task not in (old, None):
            return  # une autre conversation a pris la main entre-temps
        if self._closing or not self._reachable():
            if old is not None and self._task is old:
                self._finish(True, old)  # coupé pendant l'interruption : abandon
            return
        self._begin(self._run(first))

    async def _abandon(self, offline: bool) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._task is task:
            self._finish(offline, task)

    def _finish(self, offline: bool = False, task: asyncio.Task | None = None) -> None:
        offline = offline or not self._reachable()
        if self.deps.ha is not None:
            # Une confirmation ne vaut que dans la conversation qui l'a demandée : un « oui »
            # dit plus tard (« Lapinou, oui ») ne doit jamais valider une vieille action.
            self.deps.ha.cancel_pending(self.id)
        if self._conv_id is not None:
            self._db(self.deps.log.end, self._conv_id)
        self._conv = self._conv_id = None
        self._listen_q = None
        self.state = State.IDLE
        self._publish("offline" if offline else State.IDLE)
        self._show("idle")  # hors ligne aussi : arrête les oreilles de la réflexion
        self._wake_reset()
        if self._task is (task or asyncio.current_task()):
            self._task = None
        self._idle.set()

    async def _run(self, first: str | None = None, *, announce: str | None = None,
                   announced: asyncio.Future | None = None) -> None:
        offline = False
        try:
            if self._conv is None:
                self._conv = Conversation(self.id)
                if self.deps.log is not None:
                    self._conv_id = self._db(self.deps.log.start, self.id)
            await self._converse(first, announce, announced)
        except ConnectionError:
            log.info("lapin %s injoignable : conversation abandonnée", self.id)
            offline = True
        except Exception:
            log.exception("lapin %s : conversation en échec", self.id)
            await self._say_error(ERROR_FR)
        finally:
            if announced is not None and not announced.done():
                announced.set_exception(ConnectionError(f"annonce non dite à {self.id}"))
        self._finish(offline)

    async def _converse(self, first, announce, announced) -> None:
        text, followup = first, False
        if announce is not None:

            def started() -> None:
                if not announced.done():
                    announced.set_result(None)

            speaker = _Speaker(self, on_start=started)
            speaker.push(announce)
            try:
                await speaker.close()
            except BaseException:
                speaker.cancel()
                raise
            self._log_turn("assistant", announce)
            followup = True
        while True:
            if text is None:
                text = await self._listen(followup)
                if text is None:
                    return
                if followup:  # « Lapinou, … » redit dans la fenêtre de suite
                    found = match_wake_word(text, self.deps.names)
                    if found is not None:
                        text = found[1] or None
                        if text is None:
                            followup = False
                            continue
            if await self._turn(text) is _END:
                return
            text, followup = None, True

    async def _listen(self, followup: bool) -> str | None:
        transcriber = self.deps.transcriber
        if transcriber is None:
            log.warning("lapin %s : pas de transcription (modèles absents)", self.id)
            return None
        self._set_state(State.FOLLOWUP if followup else State.LISTENING)
        queue: asyncio.Queue = asyncio.Queue()
        # L'audio arrivé après le mot d'activation, que le détecteur n'a pas encore lu,
        # est le début de la demande. En suite de réponse, c'est l'écho : jeté.
        while not self._wake_q.empty():
            item = self._wake_q.get_nowait()
            if item is not _RESET and not followup:
                queue.put_nowait(item)
        self._listen_q = queue
        self._wake_reset()

        async def chunks():
            try:
                while True:
                    yield await queue.get()
            finally:
                if self._listen_q is queue:
                    self._listen_q = None

        try:
            text = await transcriber.transcribe_utterance(
                chunks(),
                max_s=self.deps.max_listen_s,
                silence_ms=self.deps.silence_ms,
                no_speech_s=self.deps.followup_s if followup else NO_SPEECH_S,
            )
        finally:
            if self._listen_q is queue:
                self._listen_q = None
        return (text or "").strip() or None

    async def _turn(self, text: str):
        conv = self._conv
        assert conv is not None
        self._set_state(State.THINKING)
        ha = self.deps.ha
        pending = ha.pending(self.id) if ha is not None else None
        if pending is not None:
            # R48 : la réponse (« oui », le code) ne va ni au cerveau, ni au journal, ni dans
            # `turns` : seul un repère y figure.
            marker = CODE_MARK if pending.needs_pin else CONFIRM_MARK
            self._log_turn("user", marker)
            result = await ha.confirm(self.id, text)
            if result is None:  # expirée à l'instant : rien n'est fait
                result = ToolResult("Action annulée : la confirmation a expiré.")
            return await self._confirmed(marker, result)
        self._log_turn("user", text)
        mark = len(conv.messages)
        if self.deps.memory is not None:
            conv.memories = list(self.deps.memory.relevant(text))
        add_user_text(conv, text)
        speaker = _Speaker(self)
        try:
            outcome = await self._respond(conv, speaker)
            pending = ha.pending(self.id) if ha is not None else None
            if outcome is None and pending is not None:
                # La question officielle, dite telle quelle (jamais reformulée par le modèle).
                speaker.new_call()
                speaker.push(pending.phrase)
                _append_assistant_text(conv, pending.phrase)
            await speaker.close()
        except asyncio.CancelledError:
            speaker.cancel()
            heard = speaker.heard
            del conv.messages[mark:]
            if heard:  # interrompu : l'historique garde ce qui a été entendu
                add_user_text(conv, text)
                add_assistant_text(conv, " ".join(heard))
                self._log_turn("assistant", " ".join(heard))
            raise
        except BaseException:
            speaker.cancel()
            raise
        if speaker.spoken:
            self._log_turn("assistant", " ".join(speaker.spoken))
        if outcome is _END:
            await self._say_error(HOME_DOWN_FR)
            self._log_turn("assistant", HOME_DOWN_FR)
            return _END
        return None

    async def _respond(self, conv: Conversation, speaker: _Speaker):
        """Les tours du cerveau et des outils ; _END si Home Assistant est injoignable."""
        deps = self.deps
        ctx = ToolContext(self.id)
        specs = deps.tools.specs()
        for _ in range(MAX_TOOL_ROUNDS):
            speaker.new_call()
            calls: list[ToolCall] = []
            pending_text = ""
            events = deps.brain.respond(conv, specs, deps.budget)
            async with contextlib.aclosing(events):
                async for event in events:
                    if isinstance(event, TextDelta):
                        pending_text = _push_sentences(pending_text + event.text, speaker)
                    elif isinstance(event, Retract):
                        pending_text = ""
                        await speaker.retract()
                    elif isinstance(event, ToolCall):
                        calls.append(event)
            if pending_text.strip():
                speaker.push(pending_text.strip())
            if not calls:
                return None
            if speaker.idle:
                self._set_state(State.THINKING)
            results: list[tuple[str, ToolResult]] = []
            for call in calls:
                if call.error is not None:
                    result = ToolResult(call.error, is_error=True)
                else:
                    result = await deps.tools.call(call.name, call.args, ctx)
                if result.choreography:
                    self.gestures.dance(result.choreography)
                results.append((call.id, result))
            add_tool_results(conv, results)
            if any(r.is_error and r.content == HA_UNAVAILABLE for _, r in results):
                add_assistant_text(conv, HOME_DOWN_FR)
                return _END
        add_assistant_text(conv, LOST_FR)
        speaker.push(LOST_FR)
        return None

    async def _confirmed(self, text: str, result: ToolResult):
        """Réponse de l'enfant à une confirmation : une phrase fixe, sans le cerveau."""
        conv = self._conv
        home_down = result.is_error and result.content == HA_UNAVAILABLE
        if home_down:
            phrase = HOME_DOWN_FR
        elif result.is_error:
            phrase = FAILED_FR
        elif result.content.startswith("Fait"):
            phrase = CONFIRMED_FR
        else:
            phrase = CANCELLED_FR
        add_user_text(conv, text)
        add_assistant_text(conv, phrase)
        if home_down:
            await self._say_error(phrase)
        else:
            speaker = _Speaker(self)
            speaker.push(phrase)
            try:
                await speaker.close()
            except BaseException:
                speaker.cancel()
                raise
        self._log_turn("assistant", phrase)
        return _END if home_down else None

    async def _say_error(self, text: str) -> None:
        """LED orange et une phrase ; ne lève pas (sauf annulation)."""
        self._show("error")
        speaker = _Speaker(self, look="error")
        speaker.push(text)
        try:
            await speaker.close()
        except asyncio.CancelledError:
            speaker.cancel()
            raise
        except Exception as e:  # noqa: BLE001 - la session doit survivre
            log.warning("lapin %s : phrase d'erreur impossible (%s)", self.id, type(e).__name__)

    # --- Lecture --------------------------------------------------------------------------

    def _remember(self, speech) -> None:
        self._refs[speech.id] = speech.pcm16k
        while len(self._refs) > REFERENCES_KEPT:
            self._refs.popitem(last=False)

    def _reference(self, playback_id: str) -> np.ndarray | None:
        """Le PCM joué sous `playback_id` (référence de l'annuleur d'écho) ; ne bloque pas."""
        return self._refs.get(playback_id)

    async def _play(self, speech, started: Callable[[], None] | None = None) -> None:
        done = asyncio.Event()
        self._playback[speech.id] = done
        try:
            # speech.id est l'id du message `play` : celui des marques d'écho du lapin.
            await self.rabbit.play(speech.mp3_url, speech.id)
            if started is not None:
                started()
            try:
                await asyncio.wait_for(done.wait(), speech.duration_s + PLAY_MARGIN_S)
            except TimeoutError:
                log.info("lapin %s : fin de lecture non reçue", self.id)
        finally:
            self._playback.pop(speech.id, None)

    async def _stop(self, playback_id: str) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            await self.rabbit.stop_playback()
        done = self._playback.get(playback_id)
        if done is not None:
            done.set()

    # --- État, gestes, journal ------------------------------------------------------------

    def _set_state(self, state: State, look: str | None = None) -> None:
        self.state = state
        self._publish(state)
        self._show(look or state)

    def _show(self, look: str) -> None:
        if look != self._look:
            self._look = look
            self.gestures.show(look)

    def _publish(self, state: str) -> None:
        if self.deps.state is None:
            return
        try:
            self.deps.state.publish(self.id, str(state))
        except Exception as e:  # noqa: BLE001 - MQTT absent : tout le reste fonctionne
            log.warning("état MQTT non publié (%s)", type(e).__name__)

    def _log_turn(self, role: str, text: str) -> None:
        if self._conv_id is not None:
            self._db(self.deps.log.turn, self._conv_id, role, text)

    def _db(self, fn, *args):
        try:
            return fn(*args)
        except sqlite3.Error as e:
            log.warning("journal des conversations : %s", e)
            return None


def _push_sentences(text: str, speaker: _Speaker) -> str:
    """Envoie les phrases complètes de `text` ; rend le reste (phrase pas encore finie)."""
    parts = split_sentences(text)
    if not parts:
        return text
    if _SENTENCE_DONE.search(text):
        complete, rest = parts, ""
    else:
        complete, rest = parts[:-1], text[text.rfind(parts[-1]):]
    for part in complete:
        speaker.push(part)
    return rest


def _append_assistant_text(conv: Conversation, text: str) -> None:
    """Ajoute `text` au dernier message de l'assistant (sinon en nouveau message)."""
    if conv.messages and conv.messages[-1].get("role") == "assistant":
        content = conv.messages[-1].get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        conv.messages[-1]["content"] = [*(content or []), {"type": "text", "text": text}]
    else:
        add_assistant_text(conv, text)


class Orchestrator:
    """Les sessions de tous les lapins ; `Announcer` des minuteurs et rappels (`say`)."""

    def __init__(self, deps: SessionDeps | None = None) -> None:
        self.deps = deps
        self.sessions: dict[str, RabbitSession] = {}
        self._registry = None
        self._gateway = None
        self._mismatch_seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()

    def attach(self, registry, gateway) -> None:
        """Branche les événements du registre (boutons, lectures, connexions) et les flux."""
        self._registry, self._gateway = registry, gateway
        registry.on_button = self._on_button
        registry.on_playback = self._on_playback
        registry.on_connect = self._on_connect
        gateway.on_stream = self._on_stream
        for stream in list(gateway.streams.values()):
            self._on_stream(stream)

    def _on_stream(self, stream) -> None:
        if stream.rabbit_id in self.sessions:
            return
        rabbit = self._registry.get(stream.rabbit_id)
        session = RabbitSession(rabbit, stream, self.deps)
        self.sessions[stream.rabbit_id] = session
        session.start()
        self.check_identity(stream.rabbit_id)

    def _on_connect(self, rabbit) -> None:
        self.check_identity()

    def check_identity(self, new_stream: str | None = None) -> None:
        """R47 : un lapin a un seul identifiant (uuid.conf), login plateforme et flux micro
        compris. Un flux sans plateforme et une plateforme sans flux en même temps : le login
        de account.conf ne vaut pas l'uuid ; c'est dit clairement (une fois par cas), sinon la
        session resterait muette sans explication."""
        rabbits = getattr(self._registry, "all", None)
        if rabbits is None or self._gateway is None:
            return
        logins = {r.id for r in rabbits() if r.connected}
        streams = {rid for rid, s in self._gateway.streams.items()
                   if s.connected or rid == new_stream}
        alone_streams = tuple(sorted(streams - logins))
        alone_logins = tuple(sorted(logins - streams))
        if not alone_streams or not alone_logins:
            return
        key = (alone_streams, alone_logins)
        if key in self._mismatch_seen:
            return
        self._mismatch_seen.add(key)
        log.warning(
            "identité incohérente : flux micro de %s sans plateforme, plateforme de %s sans "
            "flux micro ; le login (account.conf [account] id) doit valoir l'uuid du lapin "
            "(uuid.conf) : relancer le lapin (S40 le corrige) ou réinstaller",
            ", ".join(alone_streams), ", ".join(alone_logins))

    def _on_button(self, rabbit, kind: str) -> None:
        session = self.sessions.get(rabbit.id)
        if session is not None:
            session.on_button(kind)

    def _on_playback(self, rabbit, playback_id: str, status: str) -> None:
        session = self.sessions.get(rabbit.id)
        if session is not None:
            session.on_playback(playback_id, status)

    async def say(self, rabbit_id: str, text: str) -> None:
        session = self.sessions.get(rabbit_id)
        if session is None:
            raise ConnectionError(f"lapin {rabbit_id} jamais connecté")
        await session.say(text)

    async def close(self) -> None:
        await asyncio.gather(*(s.close() for s in self.sessions.values()),
                             return_exceptions=True)
