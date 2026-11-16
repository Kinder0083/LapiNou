# SPDX-License-Identifier: MIT
"""Serveur de plateforme : accepte les contrôleurs de plusieurs lapins à la fois."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import secrets
import struct
import time
from collections.abc import Callable
from typing import Any

from .platform import Rabbit, _Link, build_codec, quote

log = logging.getLogger(__name__)

DEFAULT_TIMEZONE = "Europe/Paris"
MAX_FRAME = 1 << 20


class RabbitRegistry:
    """Les lapins connus, par identifiant (le login de leur contrôleur).

    Événements vers l'orchestrateur (fonctions ou coroutines, réglables après
    coup) : on_button(rabbit, kind), on_rfid(rabbit, tag, action),
    on_playback(rabbit, playback_id, status).
    """

    def __init__(
        self,
        *,
        codec=None,
        timezone: str = DEFAULT_TIMEZONE,
        on_button: Callable[..., Any] | None = None,
        on_rfid: Callable[..., Any] | None = None,
        on_playback: Callable[..., Any] | None = None,
        interactive_ttl: int = 86400,
    ):
        self.codec = codec or build_codec()
        self.timezone = timezone
        self.interactive_ttl = interactive_ttl
        self.on_button, self.on_rfid, self.on_playback = on_button, on_rfid, on_playback
        self._rabbits: dict[str, Rabbit] = {}
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- lapins ----

    def get(self, rabbit_id: str) -> Rabbit:
        """Le lapin `rabbit_id` (créé déconnecté s'il n'est jamais venu)."""
        if rabbit_id not in self._rabbits:
            self._rabbits[rabbit_id] = Rabbit(rabbit_id, self)
        return self._rabbits[rabbit_id]

    def all(self) -> list[Rabbit]:
        return list(self._rabbits.values())

    def schedule_text(self) -> str:
        """scheduleConfig : règle l'horloge et le fuseau du lapin."""
        return f"scheduleConfig {{ time: {int(time.time() * 1000)} gmt: {quote(self.timezone)} }}"

    def _spawn(self, coro) -> asyncio.Task:
        """Une tâche suivie : annulée à la fermeture, jamais dans la boucle de lecture."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _emit(self, event: str, *args) -> None:
        """Appelle on_<event> dans sa propre tâche (un gestionnaire lent ne bloque pas le contrôleur)."""
        if getattr(self, f"on_{event}") is not None:
            self._spawn(self._run_handler(event, args))

    async def _run_handler(self, event: str, args: tuple) -> None:
        try:
            result = getattr(self, f"on_{event}")(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("gestionnaire on_%s en échec", event)

    # ---- serveur ----

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str = "0.0.0.0", port: int = 9123) -> None:
        self._server = await asyncio.start_server(self._serve, host, port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for rabbit in self._rabbits.values():
            if rabbit._link is not None:
                rabbit._link.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _read(self, reader: asyncio.StreamReader) -> str:
        (size,) = struct.unpack(">I", await reader.readexactly(4))
        if size > MAX_FRAME:
            raise ValueError(f"message trop grand ({size} octets)")
        return await asyncio.to_thread(self.codec.decode, await reader.readexactly(size))

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        link = _Link(self.codec, writer)
        rabbit: Rabbit | None = None
        try:
            await link.send(f"authenticationToken {{ token: {quote(secrets.token_hex(16))} }}")
            while True:
                text = await self._read(reader)
                if rabbit is None:
                    login = re.search(r'^\s+login: "([^"]+)"', text, re.M)
                    if login and "authenticationResponse" in text:
                        rabbit = self.get(login.group(1))
                        log.info("lapin %s connecté", rabbit.id)
                        await rabbit._attach(link)
                    continue
                await rabbit._handle(link, text)
        except (OSError, ValueError, asyncio.IncompleteReadError) as e:
            log.info("connexion du contrôleur fermée (%s)", e or "fin de flux")
        finally:
            if rabbit is not None:
                rabbit._detach(link)
            link.close()
