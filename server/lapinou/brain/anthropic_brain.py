# SPDX-License-Identifier: MIT
"""Cerveau Claude : API Messages (bêta, repli côté serveur), en flux, avec outils."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
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
# Modèles qui acceptent le repli côté serveur (en-tête bêta + `fallbacks`).
SERVER_FALLBACK_MODELS = ("claude-opus-5-5",)


def supports_server_fallback(model: str) -> bool:
    return any(model == m or model.startswith(m + "-") for m in SERVER_FALLBACK_MODELS)


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


def without_fallback_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copie de l'historique sans blocs `fallback` (pour un modèle sans la bêta de repli)."""
    out = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "fallback" for b in content
        ):
            message = {**message, "content": [b for b in content if b.get("type") != "fallback"]}
        out.append(message)
    return out


def _history_invalid(error: anthropic.BadRequestError) -> bool:
    """Vrai si la 400 vise l'historique (invalid_request sur messages, réflexion, tool_use)."""
    text = f"{error} {getattr(error, 'body', '')}".lower()
    return "invalid_request" in text and any(
        word in text for word in ("messages", "thinking", "tool_use")
    )


def _drop_unpaired_server_tools(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retire les server_tool_use sans résultat (boucle coupée après trop de pauses)."""
    answered = {b.get("tool_use_id") for b in content if b.get("type", "").endswith("_result")}
    return [b for b in content
            if not (b.get("type") == "server_tool_use" and b.get("id") not in answered)]


def _dump(block: Any) -> dict[str, Any]:
    """Un bloc tel que rendu par l'API (alias JSON : `from` du bloc `fallback`)."""
    if isinstance(block, dict):
        return block
    return block.model_dump(mode="json", by_alias=True, exclude_none=True)


class AnthropicBrain:
    kind = "anthropic"
    paid = True
    def __init__(
        self,
        model: str,
        api_key: str,
        web_search: bool,
        *,
        provider: str = "anthropic",
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
        self.provider = provider  # nom du fournisseur dans les réglages (dédoublonnage)
        self.server_fallback = supports_server_fallback(model)
        # Clé vide : le SDK lit ANTHROPIC_API_KEY.
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key or None, max_retries=1)
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
            # Sans la bêta de repli, un bloc `fallback` de l'historique serait refusé.
            "messages": conv.messages if self.server_fallback
            else without_fallback_blocks(conv.messages),
            "output_config": {"effort": self.effort},
        }
        if self.server_fallback:
            req["betas"] = BETAS
            req["fallbacks"] = "default"
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
        usage = Usage(provider="anthropic", model=self.model, requested_model=self.model)
        # Contenu du tour assistant, tel que rendu ; après un `pause_turn`, la reprise continue
        # ce même tour : les contenus successifs sont mis bout à bout.
        content: list[dict[str, Any]] = []
        for _ in range(MAX_PAUSES + 1):
            req = self._request(conv, tools)
            if content:
                req["messages"] = req["messages"] + [{"role": "assistant", "content": content}]
            try:
                async with self._client.beta.messages.stream(**req) as stream:
                    async for text in stream.text_stream:
                        if text:
                            yield TextDelta(text)
                    message = await stream.get_final_message()
            except anthropic.BadRequestError as e:
                # Requête refusée telle quelle (historique ou outils invalides) : visible.
                log.error("%s : requête refusée (400) : %s", self.model, e)
                raise BrainError(f"{self.model} : 400 {e}", retryable=False,
                                 history_invalid=_history_invalid(e)) from e
            except (
                anthropic.RateLimitError,
                anthropic.APIStatusError,
                anthropic.APIConnectionError,
            ) as e:
                raise BrainError(f"{self.model} : {e}") from e
            usage = self._add_usage(usage, message)
            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                yield Refused(getattr(details, "category", None), usage)
                return
            content += [_dump(b) for b in message.content]
            if message.stop_reason != "pause_turn":
                break

        if message.stop_reason == "pause_turn":
            log.warning("%s : encore en pause_turn après %d reprises ; tour coupé",
                        self.model, MAX_PAUSES)
            content = _drop_unpaired_server_tools(content)

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
        if content:
            conv.messages.append({"role": "assistant", "content": content})
        if message.stop_reason == "max_tokens":
            log.warning("%s : réponse coupée (max_tokens)", self.model)
        for call in calls:
            yield call
        yield Done(usage)

    def _add_usage(self, total: Usage, message: Any) -> Usage:
        # Avec le repli côté serveur, `message.model` est le modèle qui a vraiment répondu, et
        # `usage.iterations` détaille chaque tentative avec son modèle.
        u = message.usage
        server = getattr(u, "server_tool_use", None)
        # Chaque réponse (une par reprise de `pause_turn`) ajoute ses tentatives ; une réponse
        # sans détail compte pour une tentative. Une seule tentative simple : pas de détail.
        parsed = [
            Usage(
                provider="anthropic",
                model=getattr(it, "model", None) or self.model,
                input_tokens=getattr(it, "input_tokens", 0) or 0,
                output_tokens=getattr(it, "output_tokens", 0) or 0,
                cache_read_tokens=getattr(it, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(it, "cache_creation_input_tokens", 0) or 0,
                requested_model=self.model,
            )
            for it in (getattr(u, "iterations", None) or [])
        ]
        previous = total.iterations or ([replace(total, iterations=[])] if total.input_tokens
                                        or total.output_tokens else [])
        iterations = previous + (parsed or [self._flat(message)])
        if len(iterations) == 1 and not parsed:
            iterations = []
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
            requested_model=self.model,
            iterations=iterations,
        )

    def _flat(self, message: Any) -> Usage:
        u = message.usage
        return Usage(
            provider="anthropic",
            model=getattr(message, "model", None) or self.model,
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
            requested_model=self.model,
        )
