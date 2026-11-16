# SPDX-License-Identifier: MIT
"""Musique et radio sur les Squeezebox de la maison, par Lyrion Music Server (JSON-RPC).

`POST http://hôte:9000/jsonrpc.js` avec `{"id": 1, "method": "slim.request",
"params": [lecteur, [commande…]]}`. La pièce dite (« dans la cuisine ») est rapprochée des noms
des lecteurs (sans accents ni casse, avec tolérance) ; sans pièce, c'est le lecteur par défaut
du lapin. Une radio se lance par son nom parmi les favoris de LMS, ou par une URL.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2

from ..brain.base import ToolResult
from ..config import RabbitConfig, Settings
from . import base
from .base import Tool, ToolContext, error, match_candidates, nullable, strict_schema

log = logging.getLogger(__name__)

UNAVAILABLE = "Lyrion (la musique de la maison) ne répond pas ; dis-le en une phrase simple."
MAX_NAME = 60  # longueur gardée d'un nom venu de LMS (lecteur, favori, titre)


def as_data(text: str, limit: int = 400) -> str:
    """Un texte venu de LMS, tronqué et cadré comme des données (pas des consignes)."""
    return base.as_data("Lyrion", text, "donnees_lms", limit)


def _short(text: object) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_NAME else text[:MAX_NAME] + "…"


class LmsError(Exception):
    pass


class UnknownRoom(LookupError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Player:
    id: str  # adresse MAC
    name: str


ROOM = nullable({
    "type": "string",
    "description": "La pièce dite par l'enfant (« cuisine »), ou null pour la pièce du lapin.",
})


class LmsTools:
    def __init__(
        self,
        host: str,
        port: int = 9000,
        rabbits: dict[str, RabbitConfig] | None = None,
        *,
        client: httpx2.AsyncClient | None = None,
        timeout_s: float = 5.0,
        allowed_url_hosts: Sequence[str] = (),
    ) -> None:
        self.url = f"http://{host}:{port}/jsonrpc.js"
        self.rabbits = rabbits or {}
        self._client = client
        self.timeout_s = timeout_s
        self.allowed_url_hosts = {h.lower() for h in allowed_url_hosts}

    @classmethod
    def from_settings(cls, settings: Settings) -> LmsTools:
        return cls(settings.lms.host, settings.lms.port, settings.rabbits,
                   allowed_url_hosts=settings.lms.allowed_url_hosts)

    async def request(self, player: str, cmd: list[Any]) -> dict[str, Any]:
        payload = {"id": 1, "method": "slim.request", "params": [player, cmd]}
        try:
            if self._client is not None:
                response = await self._client.post(self.url, json=payload,
                                                   timeout=self.timeout_s)
            else:
                async with httpx2.AsyncClient(timeout=self.timeout_s) as client:
                    response = await client.post(self.url, json=payload)
            response.raise_for_status()
            body = response.json()
            failed = body.get("error") is not None
            result = body.get("result")
        except (httpx2.HTTPError, ValueError, AttributeError) as e:
            log.warning("LMS %s : %s", cmd[:1], type(e).__name__)
            raise LmsError(str(e)) from e
        if failed:
            log.warning("LMS %s : erreur JSON-RPC", cmd[:1])
            raise LmsError("erreur JSON-RPC")
        return result if isinstance(result, dict) else {}

    # --- API ---------------------------------------------------------------------------

    async def players(self) -> list[Player]:
        result = await self.request("", ["players", 0, 99])
        return [
            Player(str(p.get("playerid", "")), str(p.get("name", "")))
            for p in result.get("players_loop", [])
            if p.get("playerid")
        ]

    async def resolve(self, room: str | None, rabbit_id: str) -> Player:
        players = await self.players()
        if not players:
            raise UnknownRoom("Lyrion ne voit aucun lecteur allumé.")
        names = [p.name for p in players]
        listing = as_data(", ".join(_short(n) for n in names))
        if room and room.strip():
            found = match_candidates(room, names)
            if not found:
                raise UnknownRoom(f"Je ne connais pas cette pièce. Pièces : {listing}")
            if len(found) > 1:
                which = as_data(" ou ".join(_short(names[i]) for i in found))
                raise UnknownRoom(f"Plusieurs pièces possibles : demande laquelle. {which}")
            return players[found[0]]
        default = self.rabbits.get(rabbit_id, RabbitConfig()).default_lms_player
        if default:
            for p in players:
                if p.id.lower() == default.lower():
                    return p
            found = match_candidates(default, names)
            if len(found) == 1:
                return players[found[0]]
        if len(players) == 1:
            return players[0]
        raise UnknownRoom(f"Dans quelle pièce ? Pièces : {listing}")

    async def play(self, player: Player, query: str) -> str:
        """Lance le favori LMS le plus proche de `query`, ou une URL d'un hôte permis."""
        if "://" in query:
            parts = urlsplit(query)
            host = (parts.hostname or "").lower()
            if parts.scheme not in ("http", "https") or host not in self.allowed_url_hosts:
                raise UnknownRoom("Je ne peux lancer que les radios des favoris de Lyrion.")
            await self.request(player.id, ["playlist", "play", query])
            return host
        result = await self.request("", ["favorites", "items", 0, 200])
        favorites = [f for f in result.get("loop_loop", []) if f.get("id")]
        names = [str(f.get("name", "")) for f in favorites]
        found = match_candidates(query, names, cutoff=0.6)
        if not found:
            listing = as_data(", ".join(_short(n) for n in names[:15]))
            raise UnknownRoom(f"Je ne trouve pas ça dans les favoris de Lyrion. Favoris : {listing}")
        if len(found) > 1:
            which = as_data(" ou ".join(_short(names[i]) for i in found[:5]))
            raise UnknownRoom(f"Plusieurs favoris possibles : demande lequel. {which}")
        fav = favorites[found[0]]
        await self.request(player.id, ["favorites", "playlist", "play", f"item_id:{fav['id']}"])
        return _short(fav.get("name", query))

    async def pause(self, player: Player, resume: bool = False) -> None:
        await self.request(player.id, ["pause", "0" if resume else "1"])

    async def volume(self, player: Player, level: int) -> None:
        await self.request(player.id, ["mixer", "volume", str(max(0, min(100, level)))])

    async def next(self, player: Player) -> None:
        await self.request(player.id, ["playlist", "index", "+1"])

    async def now_playing(self, player: Player) -> str:
        status = await self.request(player.id, ["status", "-", 1, "tags:al"])
        mode = status.get("mode")
        if mode != "play":
            state = "en pause" if mode == "pause" else "arrêté"
            return f"{_short(player.name)} : rien ne joue ({state})."
        track = (status.get("playlist_loop") or [{}])[0]
        title = _short(track.get("title") or status.get("current_title") or "")
        who = _short(track.get("artist") or status.get("remote_title") or "")
        return f"{_short(player.name)} : en lecture, « {title} »" + (f" ({who})." if who else ".")

    async def sync(self, players: list[Player]) -> None:
        """Groupe les lecteurs sur le premier ; un seul lecteur : il quitte son groupe."""
        if len(players) == 1:
            await self.request(players[0].id, ["sync", "-"])
            return
        master = players[0]
        for other in players[1:]:
            if other.id != master.id:
                await self.request(master.id, ["sync", other.id])

    # --- Outils ------------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "lms_play",
                "Lance une radio ou une musique sur les enceintes Squeezebox de la maison "
                "(Lyrion), dans la pièce demandée, par son nom parmi les favoris de Lyrion "
                "(« France Inter », « Comptines »). N'invente pas d'URL : elles sont refusées "
                "sauf réglage contraire. Pas pour les lumières ni la télé.",
                strict_schema({"room": ROOM, "query": {
                    "type": "string", "description": "Nom du favori."}}),
                self._tool_play,
            ),
            Tool(
                "lms_pause",
                "Met en pause (resume=false) ou relance (resume=true) la musique d'une pièce.",
                strict_schema({"room": ROOM, "resume": {"type": "boolean"}}),
                self._simple(lambda p, a: self.pause(p, a["resume"]),
                             lambda p, a: "Relancé" if a["resume"] else "En pause"),
            ),
            Tool(
                "lms_volume",
                "Règle le volume de la musique d'une pièce (0 à 100). Pas pour la voix du "
                "lapin.",
                strict_schema({"room": ROOM, "level": {"type": "integer",
                                                       "description": "0 à 100."}}),
                self._simple(lambda p, a: self.volume(p, a["level"]),
                             lambda p, a: f"Volume à {a['level']}"),
            ),
            Tool(
                "lms_next",
                "Passe au morceau suivant dans une pièce.",
                strict_schema({"room": ROOM}),
                self._simple(lambda p, a: self.next(p), lambda p, a: "Morceau suivant"),
            ),
            Tool(
                "lms_now_playing",
                "Dit ce qui passe en ce moment sur l'enceinte d'une pièce (« qu'est-ce qui "
                "passe ? »).",
                strict_schema({"room": ROOM}),
                self._tool_now_playing,
            ),
            Tool(
                "lms_group",
                "Groupe des pièces pour qu'elles jouent la même musique (la première pièce "
                "mène). Avec une seule pièce : elle quitte son groupe.",
                strict_schema({"rooms": {"type": "array", "items": {"type": "string"}}}),
                self._tool_group,
            ),
        ]

    async def _guard(self, coro_factory) -> ToolResult:
        try:
            return await coro_factory()
        except UnknownRoom as e:
            return error(e.message)
        except LmsError:
            return error(UNAVAILABLE)

    async def _tool_play(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        async def run() -> ToolResult:
            player = await self.resolve(args["room"], ctx.rabbit_id)
            name = await self.play(player, args["query"].strip())
            return ToolResult("C'est parti. " + as_data(f"{name} sur {_short(player.name)}"))

        return await self._guard(run)

    def _simple(self, action, describe):
        async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            async def run() -> ToolResult:
                player = await self.resolve(args["room"], ctx.rabbit_id)
                await action(player, args)
                return ToolResult(f"{describe(player, args)}. " + as_data(_short(player.name)))

            return await self._guard(run)

        return handler

    async def _tool_now_playing(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        async def run() -> ToolResult:
            player = await self.resolve(args["room"], ctx.rabbit_id)
            text = await self.now_playing(player)
            return ToolResult(as_data(text))

        return await self._guard(run)

    async def _tool_group(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        async def run() -> ToolResult:
            rooms = [r for r in args["rooms"] if r.strip()]
            if not rooms:
                return error("Quelles pièces ?")
            players = [await self.resolve(r, ctx.rabbit_id) for r in rooms]
            await self.sync(players)
            names = ", ".join(dict.fromkeys(_short(p.name) for p in players))
            return ToolResult(("Pièces groupées. " if len(players) > 1 else "Pièce seule. ")
                              + as_data(names))

        return await self._guard(run)

