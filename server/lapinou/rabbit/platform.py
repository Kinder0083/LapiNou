# SPDX-License-Identifier: MIT
"""Un lapin vu de la plateforme : commandes et événements du contrôleur Karotz.

Le protocole (PlatformMessage, longueur sur 4 octets puis protobuf) et le
codec sont ceux de LapinLibre (tools/app-server/app_server.py), importés tels
quels : seule la gestion de plusieurs connexions simultanées est ici.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import struct
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

REPO = Path(__file__).resolve().parents[3]
_APP_SERVER_DIR = REPO / "tools" / "app-server"
if str(_APP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_SERVER_DIR))

import app_server  # noqa: E402  (LapinLibre : Codec, quote)

if TYPE_CHECKING:
    from .registry import RabbitRegistry

log = logging.getLogger(__name__)

PROTO = REPO / "userland" / "proto" / "platform-message.proto"
# Le mode interactif est renouvelé à cette fraction de sa durée.
RENEW_FRACTION = 0.8
INTERACTIVE_RETRY_S = 2.0  # premier nouvel essai après un refus, doublé ensuite
INTERACTIVE_RETRY_MAX_S = 60.0
READY_TIMEOUT_S = 10
# Une commande refusée (ERROR) alors que l'on se croit en mode interactif : le contrôleur l'a
# quitté (vu sur le vrai lapin : « Karotz stop interactive mode, resetting » après une action de
# la page LapinLibre). On refait la poignée de main, au plus une fois par RESYNC_MIN_S.
RESYNC_MIN_S = 15.0

quote = app_server.quote


def build_codec(protoc: str | None = None) -> app_server.Codec:
    """Le Codec de LapinLibre : paquet protobuf s'il est là, sinon la commande protoc."""
    protoc = protoc or os.environ.get("LAPINOU_PROTOC") or shutil.which("protoc") or "protoc"
    return app_server.Codec(protoc, PROTO, "auto")


@dataclass
class Step:
    """Une étape de chorégraphie : oreilles, LED (fixe, ou animée si `pulse`), puis attente."""

    ears: tuple[int, int] | None = None
    led: str | None = None
    pulse: str | None = None
    wait_ms: int = 0


class _Link:
    """Une connexion au contrôleur : écritures sérialisées, codec hors de la boucle."""

    def __init__(self, codec, writer: asyncio.StreamWriter):
        self.codec, self.writer = codec, writer
        self._lock = asyncio.Lock()

    async def send(self, text: str, msg_id: str | None = None) -> str:
        msg_id = msg_id or str(uuid.uuid4())
        async with self._lock:
            payload = await asyncio.to_thread(self.codec.encode, f"id: {quote(msg_id)} {text}")
            self.writer.write(struct.pack(">I", len(payload)) + payload)
            await self.writer.drain()
        return msg_id

    def close(self) -> None:
        self.writer.close()


class Rabbit:
    """Un lapin : identifiant (login du contrôleur), nom, connexion, état.

    L'objet survit aux reconnexions ; `state` vaut `disconnected`, `connected`
    (authentifié) ou `ready` (mode interactif pris : les commandes passent).
    """

    def __init__(self, rabbit_id: str, registry: RabbitRegistry, name: str = ""):
        self.id = rabbit_id
        self.name = name or rabbit_id
        self.state = "disconnected"
        self._registry = registry
        self._link: _Link | None = None
        self._interactive_id: str | None = None
        self._pending_interactive: dict[str, str] = {}  # id de requête -> interactiveId demandé
        self._interactive_tries = 0  # refus de suite (délai du prochain essai)
        self._sent: dict[str, str] = {}  # id de commande -> corps (pour un renvoi)
        self._renew_task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._playback_id: str | None = None
        self._stale_stops = 0  # STOP attendus pour des lectures déjà remplacées
        self._last_resync = float("-inf")  # horloge de la boucle au dernier _resync
        self._play_req: str | None = None
        # scheduleConfig envoyés : le contrôleur y répond ERROR même quand il règle l'heure et
        # le fuseau (tests/expected/controller_platform.txt), ce n'est pas un refus.
        self._schedule_reqs: set[str] = set()
        self._events: asyncio.Queue | None = None  # file des événements (créée par le registre)

    @property
    def connected(self) -> bool:
        return self._link is not None

    async def wait_ready(self) -> None:
        await self._ready.wait()

    # ---- connexion (appelé par le registre) ----

    async def _attach(self, link: _Link) -> None:
        old, self._link = self._link, link
        if old is not None:
            old.close()
        self.state = "connected"
        self._ready.clear()
        self._playback_id, self._stale_stops = None, 0
        self._interactive_id = None
        self._pending_interactive.clear()
        self._schedule_reqs.clear()
        # Comme le serveur d'applications de LapinLibre : `config` d'abord. Sans lui, le
        # contrôleur reste CONNECTING et refuse le mode interactif ; il passe par
        # UPDATING_CONF puis AVAILABLE. Puis l'horloge (scheduleConfig) et le drapeau des
        # messages (nez vert seul).
        await self._push_config(link)
        self._interactive_tries = 0
        await self._start_interactive(link)
        if self._renew_task is not None:
            self._renew_task.cancel()
        self._renew_task = self._registry._spawn(self._renew_loop(link))

    async def _push_config(self, link: _Link) -> None:
        """config, puis l'horloge (scheduleConfig) et le drapeau des messages (nez vert seul)."""
        await link.send(self._registry.config_text())
        self._schedule_reqs.add(await link.send(self._registry.schedule_text()))
        await link.send("pendingmessage: false")

    async def _start_interactive(self, link: _Link) -> None:
        """Prend (ou reprend) la main ; l'identifiant n'est adopté qu'à la réponse OK du contrôleur."""
        new_id = str(uuid.uuid4())
        req = await link.send(
            f"interactiveMode {{ action: START interactiveId: {quote(new_id)} "
            f"ttl: {self._registry.interactive_ttl} }}"
        )
        self._pending_interactive[req] = new_id

    async def _retry_interactive(self, link: _Link, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._link is link and self._interactive_id is None:
            try:
                await self._start_interactive(link)
            except OSError:
                pass

    async def _renew_loop(self, link: _Link) -> None:
        while True:
            await asyncio.sleep(max(0.1, self._registry.interactive_ttl * RENEW_FRACTION))
            if self._link is not link:
                return
            try:
                await self._start_interactive(link)
            except OSError:
                return

    def _detach(self, link: _Link) -> None:
        if self._link is link:
            self._link, self.state = None, "disconnected"
            if self._renew_task is not None:
                self._renew_task.cancel()
                self._renew_task = None
            self._ready.clear()
            self._interactive_id = None

    # ---- commandes ----

    async def _command(self, body: str, msg_id: str | None = None) -> str:
        """Envoie une commande (id de message `msg_id`, sinon un uuid) ; rend cet id."""
        if self._link is None:
            raise ConnectionError(f"lapin {self.id} déconnecté")
        try:
            await asyncio.wait_for(self._ready.wait(), READY_TIMEOUT_S)
        except TimeoutError:
            raise ConnectionError(f"lapin {self.id} : mode interactif non obtenu") from None
        link = self._link
        if link is None:
            raise ConnectionError(f"lapin {self.id} déconnecté")
        req = await link.send(f"interactiveId: {quote(self._interactive_id)} {body}", msg_id)
        self._sent[req] = body
        for old in list(self._sent)[:-50]:
            del self._sent[old]
        return req

    async def play(self, url: str, playback_id: str) -> None:
        """Joue `url`. L'id du message est `playback_id` : le contrôleur le passe à
        multimedia-daemon, qui marque avec lui le début et la fin de la lecture dans le flux
        micro (marques de l'annuleur d'écho, `RabbitStream.playback_marks`)."""
        previous, self._playback_id = self._playback_id, playback_id
        try:
            self._play_req = await self._command(
                f"multimedia {{ play {{ url: {quote(url)} }} }}", msg_id=playback_id
            )
        except BaseException:
            self._playback_id = previous
            raise
        if previous is not None:
            # La lecture remplacée est finie ; son STOP, s'il vient, ne concerne plus la nouvelle.
            self._stale_stops += 1
            self._registry._emit("playback", self, previous, "stopped")

    async def stop_playback(self) -> None:
        await self._command("multimedia { stop { } }")

    async def ears(self, left: int, right: int) -> None:
        await self._command(f"ears {{ left: {int(left)} right: {int(right)} }}")

    async def led(self, color: str, pulse: str | None = None, period_ms: int | None = None) -> None:
        """LED fixe ; `pulse` = "pulse" (clignote) ou "fade" (fondu), avec sa période."""
        if pulse is None:
            body = f"light {{ color: {quote(color)} }}"
        elif pulse == "pulse":
            body = f"pulse {{ color: {quote(color)} period: {int(period_ms or 500)} pulse: -1 }}"
        elif pulse == "fade":
            body = f"fade {{ color: {quote(color)} period: {int(period_ms or 1000)} }}"
        else:
            raise ValueError(f"animation de LED inconnue : {pulse!r} (pulse, fade)")
        await self._command(f"led {{ {body} }}")

    async def choreography(self, steps: list[Step]) -> None:
        for step in steps:
            if step.pulse is not None and step.led is None:
                raise ValueError("Step.pulse demande une couleur (Step.led)")
        for step in steps:
            if step.ears is not None:
                await self.ears(*step.ears)
            if step.led is not None:
                await self.led(step.led, step.pulse)
            if step.wait_ms > 0:
                await asyncio.sleep(step.wait_ms / 1000)

    # ---- messages du contrôleur ----

    async def _handle(self, link: _Link, text: str) -> None:
        top = set(re.findall(r"^(\w+) \{", text, re.M))
        msg_id = re.search(r'^id: "([^"]*)"', text, re.M)
        corr = re.search(r'^correlationId: "([^"]*)"', text, re.M)
        if "ping" in top and corr is None and "interactiveId" not in text:
            await link.send(
                f"correlationId: {quote(msg_id.group(1) if msg_id else '')} response {{ code: OK }}"
            )
            return
        if corr and "response" in top:
            await self._handle_response(corr.group(1), text)
        if "buttonCallback" in top:
            kind = re.search(r"^\s+type: (\w+)", text, re.M)
            if kind:
                self._registry._emit("button", self, kind.group(1).lower())
        if "rfidCallback" in top:
            tag = re.search(r'^\s+id: "([^"]*)"', text, re.M)
            direction = re.search(r"^\s+direction: (\w+)", text, re.M)
            if tag:
                action = direction.group(1).lower() if direction else "in"
                self._registry._emit("rfid", self, tag.group(1), action)
        if "multimediaCallback" in top:
            action = re.search(r"^\s+action: (\w+)", text, re.M)
            status = {"PLAY": "playing", "RESUME": "playing", "PAUSE": "paused", "STOP": "stopped"}.get(
                action.group(1) if action else ""
            )
            if status == "playing":
                self._stale_stops = 0
            if status == "stopped" and self._stale_stops > 0:
                self._stale_stops -= 1
            elif status and self._playback_id is not None:
                pid = self._playback_id
                if status == "stopped":
                    self._playback_id = None
                self._registry._emit("playback", self, pid, status)

    async def _handle_response(self, corr: str, text: str) -> None:
        code = re.search(r"^\s+code: (\w+)", text, re.M)
        code = code.group(1) if code else "?"
        if corr in self._pending_interactive:
            new_id = self._pending_interactive.pop(corr)
            if code == "OK":
                self._interactive_id = new_id
                self.state = "ready"
                self._ready.set()
            else:
                # Refusé tant que le contrôleur n'est pas AVAILABLE (configuration en cours,
                # lapin endormi) : nouvel essai bientôt, pas au prochain renouvellement.
                self._interactive_tries += 1
                delay = min(INTERACTIVE_RETRY_MAX_S, INTERACTIVE_RETRY_S * 2 ** min(self._interactive_tries - 1, 6))
                # Le premier refus suit chaque connexion (le contrôleur se configure encore).
                level = logging.INFO if self._interactive_tries == 1 else logging.WARNING
                log.log(level, "lapin %s : mode interactif refusé (%s), nouvel essai dans %.0f s",
                        self.id, code, delay)
                link = self._link
                if link is not None:
                    self._registry._spawn(self._retry_interactive(link, delay))
            return
        if corr in self._schedule_reqs:
            self._schedule_reqs.discard(corr)
            log.debug("lapin %s : scheduleConfig, réponse %s (heure réglée)", self.id, code)
            return
        if code == "OK":
            self._sent.pop(corr, None)
            return
        body = self._sent.pop(corr, None)
        log.warning("lapin %s : commande refusée (%s)%s", self.id, code,
                    f" : {body.split(None, 1)[0]}" if body else "")
        if code == "BAD_INTERACTIVEID" and body is not None and self._link is not None:
            self._registry._spawn(self._reenter_and_retry(body, corr, corr == self._play_req))
        elif code == "ERROR" and body is not None and self._link is not None and self._resync_due():
            # Le contrôleur a quitté le mode interactif (remis à zéro) : config, puis la main.
            self._registry._spawn(self._reenter_and_retry(body, corr, corr == self._play_req,
                                                          resync=True))
        elif corr == self._play_req and self._playback_id is not None:
            pid, self._playback_id = self._playback_id, None
            self._registry._emit("playback", self, pid, "error")

    def _resync_due(self) -> bool:
        now = asyncio.get_running_loop().time()
        if now - self._last_resync < RESYNC_MIN_S:
            return False
        self._last_resync = now
        return True

    async def _reenter_and_retry(self, body: str, req_id: str, is_play: bool = False,
                                 resync: bool = False) -> None:
        """Le contrôleur ne reconnaît plus notre prise de main : la reprendre, renvoyer une fois.
        Un `play` renvoyé garde son id (le playback_id des marques d'écho). `resync` : il a été
        remis à zéro ; on lui renvoie d'abord sa configuration, comme à la connexion."""
        link = self._link
        if link is None:
            return
        self._ready.clear()
        self.state = "connected"
        self._interactive_id = None
        try:
            if resync:
                log.warning("lapin %s : mode interactif perdu, reprise de la main", self.id)
                await self._push_config(link)
                self._interactive_tries = 0
            await self._start_interactive(link)
            req = await self._command(body, msg_id=req_id if is_play else None)
            if is_play:
                self._play_req = req
            self._sent.pop(req, None)  # pas de second renvoi
        except (ConnectionError, OSError):
            log.warning("lapin %s : renvoi impossible", self.id)
