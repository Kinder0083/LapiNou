# SPDX-License-Identifier: MIT
"""Cerveau compatible OpenAI (Chat Completions en flux, outils) : serveur local ou distant."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import openai

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
    content_blocks,
    context_text,
    load_persona,
    validate_args,
)


def _tool_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or [] if b.get("type") == "text")


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Traduit l'historique commun (format Anthropic) en messages Chat Completions."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        blocks = content_blocks(message)
        text = " ".join(b["text"] for b in blocks if b.get("type") == "text").strip()
        if message.get("role") == "assistant":
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            if text or calls:
                out.append(entry)
            continue
        for b in blocks:
            if b.get("type") == "tool_result":
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"],
                            "content": _tool_content(b.get("content"))})
        if text:
            out.append({"role": "user", "content": text})
    return out


def _tool_param(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


class OpenAIBrain:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        max_tokens: int = 1024,
        persona: str | None = None,
        client: Any = None,
        clock: Callable[[], dt.datetime] = dt.datetime.now,
    ) -> None:
        self.name = model
        self.model = model
        self.max_tokens = max_tokens
        self.persona = persona if persona is not None else load_persona()
        # Un serveur local n'exige pas de clé, mais le SDK en veut une.
        self._client = client or openai.AsyncOpenAI(
            base_url=base_url or None, api_key=api_key or "sans-clé", max_retries=1
        )
        self._clock = clock

    async def respond(
        self, conv: Conversation, tools: list[ToolSpec]
    ) -> AsyncIterator[BrainEvent]:
        system = f"{self.persona}\n\n{context_text(conv, self._clock())}"
        req: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(system, conv.messages),
            "max_tokens": self.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            req["tools"] = [_tool_param(t) for t in tools]
        text = ""
        pending: dict[int, dict[str, str]] = {}
        finish = None
        usage = Usage(provider="openai", model=self.model)
        try:
            stream = await self._client.chat.completions.create(**req)
            async for chunk in stream:
                if chunk.usage is not None:
                    usage.input_tokens = chunk.usage.prompt_tokens or 0
                    usage.output_tokens = chunk.usage.completion_tokens or 0
                for choice in chunk.choices:
                    delta = choice.delta
                    if delta.content:
                        text += delta.content
                        yield TextDelta(delta.content)
                    for tc in delta.tool_calls or []:
                        slot = pending.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments
                    if choice.finish_reason:
                        finish = choice.finish_reason
        except (openai.RateLimitError, openai.APIStatusError, openai.APIConnectionError) as e:
            raise BrainError(f"{self.model} : {e}") from e

        if finish == "content_filter":
            yield Refused("content_filter", usage)
            return
        specs = {t.name: t for t in tools}
        calls: list[ToolCall] = []
        for _, slot in sorted(pending.items()):
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args, error = {}, "arguments : JSON invalide"
            else:
                spec = specs.get(slot["name"])
                error = (validate_args(spec.input_schema, args) if spec
                         else f"outil inconnu : {slot['name']}")
                if not isinstance(args, dict):
                    args = {}
            calls.append(ToolCall(slot["id"], slot["name"], args, error))
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                   for c in calls]
        if blocks:
            conv.messages.append({"role": "assistant", "content": blocks})
        for call in calls:
            yield call
        yield Done(usage)
