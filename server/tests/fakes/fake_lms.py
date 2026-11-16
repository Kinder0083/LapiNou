# SPDX-License-Identifier: MIT
"""Faux Lyrion Music Server : répond au JSON-RPC `slim.request` sans réseau (MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx2


class FakeLms:
    def __init__(self) -> None:
        self.players = [
            {"playerid": "aa:aa:aa:aa:aa:01", "name": "Cuisine", "connected": 1},
            {"playerid": "aa:aa:aa:aa:aa:02", "name": "Salon", "connected": 1},
            {"playerid": "aa:aa:aa:aa:aa:03", "name": "Chambre des enfants", "connected": 1},
            {"playerid": "aa:aa:aa:aa:aa:04", "name": "Chambre parents", "connected": 1},
        ]
        self.favorites = [
            {"id": "f1.0", "name": "France Inter", "isaudio": 1},
            {"id": "f1.1", "name": "Radio Classique", "isaudio": 1},
            {"id": "f1.2", "name": "Comptines", "isaudio": 1},
        ]
        self.commands: list[tuple[str, list[Any]]] = []
        self.down = False
        self.rpc_error = False  # répond {"error": …} au lieu de {"result": …}
        self.status = {
            "mode": "play",
            "remote_title": "France Inter",
            "playlist_loop": [{"title": "Le journal", "artist": "France Inter"}],
        }

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("connexion refusée")
        assert request.url.path == "/jsonrpc.js"
        body = json.loads(request.content)
        assert body["method"] == "slim.request"
        player, cmd = body["params"]
        self.commands.append((player, cmd))
        if self.rpc_error:
            return httpx2.Response(200, json={"id": body["id"],
                                              "error": {"code": -32601, "message": "boom"}})
        result: dict[str, Any] = {}
        if cmd[:1] == ["players"]:
            result = {"count": len(self.players), "players_loop": self.players}
        elif cmd[:2] == ["favorites", "items"]:
            result = {"count": len(self.favorites), "loop_loop": self.favorites}
        elif cmd[:1] == ["status"]:
            result = self.status
        return httpx2.Response(200, json={"id": body["id"], "method": "slim.request",
                                          "params": body["params"], "result": result})

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))

    def sent(self) -> list[tuple[str, list[Any]]]:
        """Les commandes autres que les lectures (players, favoris, status)."""
        return [(p, c) for p, c in self.commands
                if c[:1] not in (["players"], ["status"]) and c[:2] != ["favorites", "items"]]
