# SPDX-License-Identifier: MIT
"""Faux fournisseurs de langage : aucun appel réseau, aucune dépense."""

from __future__ import annotations

import copy
from typing import Any

import anthropic
import httpx2
from anthropic.types.beta import BetaMessage
from openai.types.chat import ChatCompletionChunk

from lapinou.brain.base import (
    BrainError,
    Conversation,
    Done,
    TextDelta,
    ToolSpec,
    Usage,
    add_assistant_text,
)


class FakeBrain:
    """Un cerveau scripté : rejoue `events`, ou lève `error` avant toute réponse."""

    def __init__(self, name: str, events: list | None = None, error: Exception | None = None,
                 kind: str = "fake", paid: bool = True):
        self.name = name
        self.kind = kind
        self.model = name
        self.paid = paid
        self.events = events if events is not None else [
            TextDelta(f"réponse de {name}"),
            Done(Usage(provider="fake", model=name, input_tokens=10, output_tokens=5)),
        ]
        self.error = error
        self.error_once = False  # True : seul le premier appel échoue
        self.calls: list[tuple[Conversation, list[ToolSpec]]] = []

    async def respond(self, conv: Conversation, tools: list[ToolSpec]):
        self.calls.append((conv, tools))
        if self.error is not None:
            error, self.error = self.error, (None if self.error_once else self.error)
            raise error
        text = ""
        for event in self.events:
            if isinstance(event, TextDelta):
                text += event.text
            yield event
        if text:
            add_assistant_text(conv, text)


def failing(name: str) -> FakeBrain:
    return FakeBrain(name, error=BrainError(f"{name} en panne"))


# --- Faux client Anthropic ------------------------------------------------------------------


def anthropic_message(
    content: list[dict[str, Any]],
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    usage: dict[str, Any] | None = None,
    stop_details: dict[str, Any] | None = None,
) -> BetaMessage:
    return BetaMessage.construct(
        id="msg_fake",
        type="message",
        role="assistant",
        model=model,
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        stop_sequence=None,
        usage=usage or {"input_tokens": 12, "output_tokens": 7},
    )


class _FakeAnthropicStream:
    def __init__(self, deltas: list[str], final: BetaMessage) -> None:
        self._deltas = deltas
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def gen():
            for d in self._deltas:
                yield d

        return gen()

    async def get_final_message(self) -> BetaMessage:
        return self._final


class FakeAnthropicClient:
    """Imite `AsyncAnthropic` : `client.beta.messages.stream(**kwargs)`.

    `finals` : un message final par appel successif (reprise après `pause_turn`).
    """

    def __init__(self, deltas=None, final: BetaMessage | None = None, error=None,
                 finals: list[BetaMessage] | None = None) -> None:
        self.deltas = deltas or []
        final = final or anthropic_message([{"type": "text", "text": "".join(self.deltas)}])
        self.finals = list(finals or [final])
        self.error = error
        self.requests: list[dict[str, Any]] = []
        self.beta = self
        self.messages = self

    def stream(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if self.error is not None:
            raise self.error
        final = self.finals.pop(0) if len(self.finals) > 1 else self.finals[0]
        return _FakeAnthropicStream(self.deltas, final)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")
    )


def bad_request() -> anthropic.BadRequestError:
    request = httpx2.Request("POST", "https://api.anthropic.invalid/v1/messages")
    return anthropic.BadRequestError(
        "messages: thinking blocks cannot be modified",
        response=httpx2.Response(400, request=request),
        body=None,
    )


# --- Faux client OpenAI ----------------------------------------------------------------------


def openai_chunk(delta: dict[str, Any] | None = None, finish: str | None = None,
                 usage: dict[str, Any] | None = None) -> ChatCompletionChunk:
    choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    data: dict[str, Any] = {
        "id": "c", "object": "chat.completion.chunk", "created": 0, "model": "local",
        "choices": choices,
    }
    if usage is not None:
        data["usage"] = usage
    return ChatCompletionChunk.model_validate(data)


class FakeOpenAIClient:
    """Imite `AsyncOpenAI` : `await client.chat.completions.create(stream=True, ...)`."""

    def __init__(self, chunks: list[ChatCompletionChunk], error=None) -> None:
        self.chunks = chunks
        self.error = error
        self.requests: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if self.error is not None:
            raise self.error

        async def gen():
            for c in self.chunks:
                yield c

        return gen()
