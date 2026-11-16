# SPDX-License-Identifier: MIT
"""Cerveau Claude : API Messages (bêta, repli côté serveur), en flux, avec outils."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic

from .base import (
    BrainError,
    BrainEvent,
    Conversation,
    Done,
    Refused,
    TextDelta,
    ToolCall,
    ToolSpec,
    Usage,
    context_text,
    is_strict_compatible,
    load_persona,
    validate_args,
)

log = logging.getLogger(__name__)

BETAS = ["server-side-fallback-2026-07-01"]
# Reprises au plus après un `pause_turn` (boucle de recherche trop longue).
MAX_PAUSES = 2
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
# Blocs gardés dans l'historique : les blocs de réflexion doivent revenir tels quels pendant
# une boucle d'outils ; les blocs des outils serveur (recherche) sont propres à la réponse.
_KEPT = ("thinking", "redacted_thinking", "text", "tool_use")


def tool_param(spec: ToolSpec) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
        "eager_input_streaming": True,
    }
    if is_strict_compatible(spec.input_schema):
        tool["strict"] = True
    return tool


def _history_block(block: Any) -> dict[str, Any] | None:
    kind = getattr(block, "type", None)
    if kind not in _KEPT:
        return None
    if kind == "text":
        return {"type": "text", "text": block.text}  # sans citations (blocs de recherche omis)
    if kind == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return block.model_dump(mode="json", exclude_none=True)


class AnthropicBrain:
    def __init__(
        self,
        model: str,
        api_key: str,
        web_search: bool,
        *,
        effort: str = "medium",
        max_tokens: int = 1024,
        persona: str | None = None,
        client: Any = None,
        clock: Callable[[], dt.datetime] = dt.datetime.now,
    ) -> None:
        self.name = model
        self.model = model
        self.web_search = web_search
        self.effort = effort
        self.max_tokens = max_tokens
        self.persona = persona if persona is not None else load_persona()
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, max_retries=1)
        self._clock = clock

    def _request(self, conv: Conversation, tools: list[ToolSpec]) -> dict[str, Any]:
        # Pas de `thinking` (adaptatif par défaut, non désactivable sur Opus 5.5), ni
        # température : la profondeur se règle par `output_config.effort`.
        req: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [
                {"type": "text", "text": self.persona, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": context_text(conv, self._clock())},
            ],
            "messages": conv.messages,
            "output_config": {"effort": self.effort},
            "betas": BETAS,
            "fallbacks": "default",
        }
        tool_params = [tool_param(t) for t in tools]
        if self.web_search:
            tool_params.append(dict(WEB_SEARCH_TOOL))
        if tool_params:
            req["tools"] = tool_params  # tool_choice auto (forcer un outil : 400 sur Opus 5.5)
        return req

    async def respond(
        self, conv: Conversation, tools: list[ToolSpec]
    ) -> AsyncIterator[BrainEvent]:
        specs = {t.name: t for t in tools}
        usage = Usage(provider="anthropic", model=self.model)
        paused: list[dict[str, Any]] = []
        for _ in range(MAX_PAUSES + 1):
            req = self._request(conv, tools)
            if paused:
                req["messages"] = conv.messages + paused
            try:
                async with self._client.beta.messages.stream(**req) as stream:
                    async for text in stream.text_stream:
                        if text:
                            yield TextDelta(text)
                    message = await stream.get_final_message()
            except (
                anthropic.RateLimitError,
                anthropic.APIStatusError,
                anthropic.APIConnectionError,
            ) as e:
                raise BrainError(f"{self.model} : {e}") from e
            usage = self._add_usage(usage, message)
            if message.stop_reason != "pause_turn":
                break
            # Boucle d'outil serveur (recherche) en pause : on renvoie le tour tel quel.
            paused.append({
                "role": "assistant",
                "content": [b.model_dump(mode="json", exclude_none=True) for b in message.content],
            })

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            yield Refused(getattr(details, "category", None), usage)
            return

        calls: list[ToolCall] = []
        for block in message.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else {}
            spec = specs.get(block.name)
            if spec is None:
                error = f"outil inconnu : {block.name}"
            else:
                error = validate_args(spec.input_schema, block.input)
            calls.append(ToolCall(block.id, block.name, args, error))
        history = [b for b in map(_history_block, message.content) if b is not None]
        if history:
            conv.messages.append({"role": "assistant", "content": history})
        if message.stop_reason == "max_tokens":
            log.warning("%s : réponse coupée (max_tokens)", self.model)
        for call in calls:
            yield call
        yield Done(usage)

    def _add_usage(self, total: Usage, message: Any) -> Usage:
        # Avec le repli côté serveur, `message.model` est le modèle qui a vraiment répondu.
        u = message.usage
        server = getattr(u, "server_tool_use", None)
        return Usage(
            provider="anthropic",
            model=getattr(message, "model", None) or self.model,
            input_tokens=total.input_tokens + (u.input_tokens or 0),
            output_tokens=total.output_tokens + (u.output_tokens or 0),
            cache_read_tokens=total.cache_read_tokens
            + (getattr(u, "cache_read_input_tokens", None) or 0),
            cache_write_tokens=total.cache_write_tokens
            + (getattr(u, "cache_creation_input_tokens", None) or 0),
            web_searches=total.web_searches
            + ((getattr(server, "web_search_requests", None) or 0) if server else 0),
        )
