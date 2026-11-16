# SPDX-License-Identifier: MIT
"""Cerveau de dernier recours : Assist de Home Assistant (gratuit, local, sans outils).

Assist agit directement sur les entités qui lui sont exposées dans Home Assistant, sans passer
par la politique d'actions sensibles de LapiNou. Deux garde-fous :
- toute phrase qui cite un mot de `safety.assist_blocked_keywords` est refusée sans appeler HA ;
- dans Home Assistant (Paramètres > Assistants vocaux > Exposer), n'exposez à Assist aucune
  entité sensible : serrures, alarme, portes, garage, portail, chauffage, four, plaques.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from .base import (
    BrainError,
    BrainEvent,
    Conversation,
    Done,
    Refused,
    TextDelta,
    ToolSpec,
    Usage,
    add_assistant_text,
    last_user_text,
)

TIMEOUT_S = 15.0


class AssistBrain:
    name = "assist"
    kind = "homeassistant"
    model = "assist"
    paid = False

    def __init__(
        self,
        ha_url: str,
        token: str,
        *,
        blocked_keywords: list[str] | None = None,
        client: httpx2.AsyncClient | None = None,
    ):
        self.blocked_keywords = list(blocked_keywords or [])
        words = "|".join(re.escape(k.lower()) for k in self.blocked_keywords)
        self._blocked = re.compile(rf"\b(?:{words})[sx]?\b") if words else None
        self.url = ha_url.rstrip("/") + "/api/conversation/process"
        self._token = token
        self._client = client
        # Assist garde son propre contexte : un identifiant de conversation par lapin.
        self._conversation_ids: dict[str, str] = {}

    async def _post(self, payload: dict[str, Any]) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {self._token}"}
        if self._client is not None:
            return await self._client.post(self.url, json=payload, headers=headers)
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as client:
            return await client.post(self.url, json=payload, headers=headers)

    async def respond(
        self, conv: Conversation, tools: list[ToolSpec]
    ) -> AsyncIterator[BrainEvent]:
        text = last_user_text(conv)
        if self._blocked is not None and self._blocked.search(text.lower()):
            yield Refused("sensitive")
            return
        payload: dict[str, Any] = {"text": text, "language": "fr"}
        if conv.rabbit_id in self._conversation_ids:
            payload["conversation_id"] = self._conversation_ids[conv.rabbit_id]
        try:
            response = await self._post(payload)
            response.raise_for_status()
            data = response.json()
            speech = data["response"]["speech"]["plain"]["speech"]
        except (httpx2.HTTPError, ValueError, KeyError, TypeError) as e:
            raise BrainError(f"Assist : {e!r}") from e
        if data.get("conversation_id"):
            self._conversation_ids[conv.rabbit_id] = data["conversation_id"]
        speech = str(speech).strip()
        if speech:
            add_assistant_text(conv, speech)
            yield TextDelta(speech)
        yield Done(Usage(provider="homeassistant", model="assist"))
