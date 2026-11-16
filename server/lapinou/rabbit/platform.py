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
# Durée de la prise de main (mode interactif) : le contrôleur la rend à l'expiration.
INTERACTIVE_TTL_S = 86400
READY_TIMEOUT_S = 10

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
        self._interactive_req: str | None = None
        self._ready = asyncio.Event()
        self._playback_id: str | None = None
        self._play_req: str | None = None

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
        self._playback_id = None
        await link.send("config { playsounds: true }")
        await link.send(self._registry.schedule_text())
        self._interactive_id = str(uuid.uuid4())
        self._interactive_req = await link.send(
            f"interactiveMode {{ action: START interactiveId: {quote(self._interactive_id)} "
            f"ttl: {INTERACTIVE_TTL_S} }}"
        )

    def _detach(self, link: _Link) -> None:
        if self._link is link:
            self._link, self.state = None, "disconnected"
            self._ready.clear()
            self._interactive_id = None

    # ---- commandes ----

    async def _command(self, body: str) -> str:
        if self._link is None:
            raise ConnectionError(f"lapin {self.id} déconnecté")
        try:
            await asyncio.wait_for(self._ready.wait(), READY_TIMEOUT_S)
        except TimeoutError:
            raise ConnectionError(f"lapin {self.id} : mode interactif non obtenu") from None
        link = self._link
        if link is None:
            raise ConnectionError(f"lapin {self.id} déconnecté")
        return await link.send(f"interactiveId: {quote(self._interactive_id)} {body}")

    async def play(self, url: str, playback_id: str) -> None:
        self._playback_id = playback_id
        self._play_req = await self._command(f"multimedia {{ play {{ url: {quote(url)} }} }}")

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
                await self._registry._emit("button", self, kind.group(1).lower())
        if "rfidCallback" in top:
            tag = re.search(r'^\s+id: "([^"]*)"', text, re.M)
            direction = re.search(r"^\s+direction: (\w+)", text, re.M)
            if tag:
                action = direction.group(1).lower() if direction else "in"
                await self._registry._emit("rfid", self, tag.group(1), action)
        if "multimediaCallback" in top:
            action = re.search(r"^\s+action: (\w+)", text, re.M)
            status = {"PLAY": "playing", "RESUME": "playing", "PAUSE": "paused", "STOP": "stopped"}.get(
                action.group(1) if action else ""
            )
            if status and self._playback_id is not None:
                pid = self._playback_id
                if status == "stopped":
                    self._playback_id = None
                await self._registry._emit("playback", self, pid, status)

    async def _handle_response(self, corr: str, text: str) -> None:
        ok = re.search(r"^\s+code: OK", text, re.M) is not None
        if corr == self._interactive_req:
            if ok:
                self.state = "ready"
                self._ready.set()
            else:
                log.warning("lapin %s : mode interactif refusé", self.id)
        elif corr == self._play_req and not ok and self._playback_id is not None:
            pid, self._playback_id = self._playback_id, None
            await self._registry._emit("playback", self, pid, "error")
