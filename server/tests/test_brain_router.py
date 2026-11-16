# SPDX-License-Identifier: MIT
import datetime as dt
import json

import httpx2
import pytest
from fakes.fake_llm import (
    FakeAnthropicClient,
    FakeBrain,
    FakeOpenAIClient,
    anthropic_message,
    connection_error,
    failing,
    openai_chunk,
)

from lapinou.brain.anthropic_brain import BETAS, AnthropicBrain
from lapinou.brain.assist_brain import AssistBrain
from lapinou.brain.base import (
    BrainError,
    Conversation,
    Done,
    Refused,
    TextDelta,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    add_tool_results,
    add_user_text,
    load_persona,
)
from lapinou.brain.budget import Budget
from lapinou.brain.openai_brain import OpenAIBrain
from lapinou.brain.router import REFUSAL_FR, BrainRouter, is_simple
from lapinou.config import BudgetConfig, ProviderConfig, Settings
from lapinou.db import Database

NOW = dt.datetime(2026, 11, 16, 14, 5)

LIGHT = ToolSpec(
    name="allumer",
    description="Allume une lumière.",
    input_schema={
        "type": "object",
        "properties": {"piece": {"type": "string"}},
        "required": ["piece"],
        "additionalProperties": False,
    },
)


def conv_with(text: str) -> Conversation:
    conv = Conversation(rabbit_id="r1", memories=["Le chat s'appelle Caramel."])
    add_user_text(conv, text)
    return conv


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "lapinou.db")
    d.migrate()
    yield d
    d.close()


def budget_at(db, ratio: float) -> Budget:
    """Un budget de 10 $ déjà consommé à `ratio`."""
    b = Budget(db, BudgetConfig(monthly_usd=10.0), clock=lambda: NOW)
    if ratio:
        b.record(Usage("anthropic", "claude-opus-5-5", input_tokens=int(ratio * 10 / 4 * 1e6)))
    return b


def make_router(**brains) -> BrainRouter:
    return BrainRouter(
        primary=brains.get("primary", FakeBrain("principal")),
        fast=brains.get("fast", FakeBrain("rapide")),
        fallbacks=brains.get("fallbacks", []),
        assist=brains.get("assist", FakeBrain("assist")),
    )


async def collect(agen) -> list:
    return [e async for e in agen]


def spoken(events) -> str:
    return "".join(e.text for e in events if isinstance(e, TextDelta))


# --- Choix du cerveau -------------------------------------------------------------------------


def test_simple_house_commands():
    assert is_simple(conv_with("Allume la lumière du salon"))
    assert is_simple(conv_with("éteins la cuisine s'il te plaît"))
    assert is_simple(conv_with("Mets la radio"))
    assert not is_simple(conv_with("Pourquoi le ciel est bleu ?"))
    assert not is_simple(conv_with("Raconte-moi une histoire de dragon"))
    long = "allume la lumière puis raconte moi pourquoi les étoiles brillent la nuit dans le ciel"
    assert not is_simple(conv_with(long))


def test_pick_by_request_and_budget(db):
    router = make_router()
    question = conv_with("Pourquoi les lapins ont de grandes oreilles ?")
    assert router.pick(question, budget_at(db, 0.0)).name == "principal"
    assert router.pick(conv_with("Allume le salon"), budget_at(db, 0.0)).name == "rapide"


def test_budget_85_percent_picks_fast(db):
    b = budget_at(db, 0.85)
    assert b.ratio() == pytest.approx(0.85)
    assert make_router().pick(conv_with("Pourquoi la mer est salée ?"), b).name == "rapide"


def test_budget_100_percent_picks_assist(db):
    b = budget_at(db, 1.0)
    assert make_router().pick(conv_with("Pourquoi la mer est salée ?"), b).name == "assist"


async def test_budget_stop_answers_with_assist_and_spends_nothing(db):
    b = budget_at(db, 1.0)
    primary = FakeBrain("principal")
    router = make_router(primary=primary)
    events = await collect(router.respond(conv_with("Raconte une blague"), [LIGHT], b))
    assert spoken(events) == "réponse de assist"
    assert primary.calls == []


# --- Secours ------------------------------------------------------------------------------------


async def test_primary_failure_falls_back(db):
    secours = FakeBrain("secours")
    router = make_router(primary=failing("principal"), fallbacks=[secours])
    conv = conv_with("Pourquoi la lune change de forme ?")
    events = await collect(router.respond(conv, [LIGHT], budget_at(db, 0.0)))
    assert spoken(events) == "réponse de secours"
    assert isinstance(events[-1], Done)
    assert conv.messages[-1]["role"] == "assistant"


async def test_everything_down_ends_with_assist_then_gentle_phrase(db):
    router = make_router(
        primary=failing("principal"), fallbacks=[failing("secours")], assist=failing("assist")
    )
    events = await collect(router.respond(conv_with("Pourquoi ?"), [], budget_at(db, 0.0)))
    assert spoken(events)  # une phrase douce, jamais une exception
    assert isinstance(events[-1], Done)


async def test_assist_tried_after_chain(db):
    assist = FakeBrain("assist")
    router = make_router(primary=failing("principal"), fallbacks=[failing("secours")],
                         assist=assist)
    events = await collect(router.respond(conv_with("Pourquoi ?"), [], budget_at(db, 0.0)))
    assert spoken(events) == "réponse de assist"


async def test_failing_fast_falls_back_to_primary_when_budget_allows(db):
    router = make_router(fast=failing("rapide"))
    events = await collect(router.respond(conv_with("Allume le salon"), [], budget_at(db, 0.0)))
    assert spoken(events) == "réponse de principal"


# --- Refus et usage -------------------------------------------------------------------------------


async def test_refused_becomes_gentle_phrase(db):
    refusing = FakeBrain("principal", events=[
        Refused("general_harms", Usage("anthropic", "claude-opus-5-5", input_tokens=250_000)),
    ])
    b = budget_at(db, 0.0)
    conv = conv_with("Une question pas pour les enfants")
    events = await collect(make_router(primary=refusing).respond(conv, [], b))
    assert spoken(events) == REFUSAL_FR
    assert not any(isinstance(e, Refused) for e in events)
    assert isinstance(events[-1], Done)
    assert conv.messages[-1] == {"role": "assistant",
                                 "content": [{"type": "text", "text": REFUSAL_FR}]}
    assert b.spent() == pytest.approx(1.0)  # un refus est facturé


async def test_router_records_usage(db):
    b = budget_at(db, 0.0)
    primary = FakeBrain("principal", events=[
        TextDelta("Bonjour"),
        Done(Usage("anthropic", "claude-opus-5-5", input_tokens=500_000, output_tokens=50_000)),
    ])
    await collect(make_router(primary=primary).respond(conv_with("Pourquoi ?"), [], b))
    assert b.spent() == pytest.approx(3.0)


async def test_tool_continuation_keeps_the_same_brain(db):
    b = budget_at(db, 0.0)
    fast = FakeBrain("rapide", events=[
        ToolCall("t1", "allumer", {"piece": "salon"}),
        Done(Usage("anthropic", "claude-haiku-5-5")),
    ])
    primary = FakeBrain("principal")
    router = make_router(fast=fast, primary=primary)
    conv = conv_with("Allume le salon")
    await collect(router.respond(conv, [LIGHT], b))
    conv.messages.append({"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "allumer", "input": {"piece": "salon"}}]})
    add_tool_results(conv, [("t1", ToolResult("Lumière du salon allumée."))])
    await collect(router.respond(conv, [LIGHT], b))
    assert len(fast.calls) == 2 and primary.calls == []


# --- AnthropicBrain -------------------------------------------------------------------------------


def make_anthropic(client, **kw) -> AnthropicBrain:
    return AnthropicBrain("claude-opus-5-5", "test-key", kw.pop("web_search", True),
                          client=client, clock=lambda: NOW, **kw)


async def test_anthropic_request_shape():
    client = FakeAnthropicClient(deltas=["Coucou", " toi !"])
    brain = make_anthropic(client)
    conv = conv_with("Bonjour Lapinou")
    events = await collect(brain.respond(conv, [LIGHT]))
    assert spoken(events) == "Coucou toi !"
    (req,) = client.requests
    assert req["model"] == "claude-opus-5-5"
    assert "thinking" not in req
    for knob in ("temperature", "top_p", "top_k", "tool_choice"):
        assert knob not in req
    assert req["output_config"] == {"effort": "medium"}
    assert req["betas"] == BETAS == ["server-side-fallback-2026-07-01"]
    assert req["fallbacks"] == "default"
    persona, context = req["system"]
    assert persona == {"type": "text", "text": load_persona(),
                       "cache_control": {"type": "ephemeral"}}
    assert "lundi 16 novembre 2026" in context["text"]
    assert "Caramel" in context["text"] and "cache_control" not in context
    light, search = req["tools"]
    assert light["name"] == "allumer" and light["eager_input_streaming"] is True
    assert light["strict"] is True
    assert search["type"] == "web_search_20260209" and search["name"] == "web_search"
    assert not any(t.get("type", "").startswith("code_execution") for t in req["tools"])
    assert req["messages"] == conv_with("Bonjour Lapinou").messages
    # Le tour de l'assistant est ajouté à la conversation.
    assert conv.messages[-1] == {"role": "assistant",
                                 "content": [{"type": "text", "text": "Coucou toi !"}]}


async def test_anthropic_fast_effort_and_no_search():
    client = FakeAnthropicClient(deltas=["Ok"])
    brain = AnthropicBrain("claude-haiku-5-5", "test-key", False, effort="low", client=client,
                           clock=lambda: NOW)
    await collect(brain.respond(conv_with("Allume"), []))
    (req,) = client.requests
    assert req["output_config"] == {"effort": "low"}
    assert "tools" not in req


async def test_anthropic_tool_calls_usage_and_history():
    final = anthropic_message(
        [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Je regarde."},
            {"type": "tool_use", "id": "t1", "name": "allumer", "input": {"piece": "salon"}},
            {"type": "tool_use", "id": "t2", "name": "allumer", "input": {"pièce": 3}},
            {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {"query": "x"}},
        ],
        stop_reason="tool_use",
        model="claude-opus-5-5",
        usage={"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900,
               "cache_creation_input_tokens": 50,
               "server_tool_use": {"web_search_requests": 1}},
    )
    client = FakeAnthropicClient(deltas=["Je regarde."], final=final)
    conv = conv_with("Allume le salon")
    events = await collect(make_anthropic(client).respond(conv, [LIGHT]))
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert calls[0] == ToolCall("t1", "allumer", {"piece": "salon"})
    assert calls[1].id == "t2" and calls[1].error  # arguments invalides : erreur d'outil
    assert len(calls) == 2  # l'outil serveur n'est pas pour l'appelant
    (done,) = [e for e in events if isinstance(e, Done)]
    assert done.usage == Usage("anthropic", "claude-opus-5-5", input_tokens=100,
                               output_tokens=20, cache_read_tokens=900,
                               cache_write_tokens=50, web_searches=1)
    blocks = conv.messages[-1]["content"]
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use", "tool_use"]


async def test_anthropic_refusal():
    final = anthropic_message([{"type": "text", "text": "Je"}], stop_reason="refusal",
                              stop_details={"type": "refusal", "category": "general_harms"})
    client = FakeAnthropicClient(deltas=["Je"], final=final)
    conv = conv_with("…")
    events = await collect(make_anthropic(client).respond(conv, []))
    assert isinstance(events[-1], Refused) and events[-1].category == "general_harms"
    assert events[-1].usage.input_tokens == 12
    assert conv.messages[-1]["role"] == "user"  # rien d'ajouté


async def test_anthropic_sdk_errors_become_brain_errors():
    client = FakeAnthropicClient(error=connection_error())
    with pytest.raises(BrainError):
        await collect(make_anthropic(client).respond(conv_with("Salut"), []))


# --- OpenAIBrain ----------------------------------------------------------------------------------


async def test_openai_stream_tools_and_translation():
    chunks = [
        openai_chunk({"role": "assistant", "content": "Je "}),
        openai_chunk({"content": "allume."}),
        openai_chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                      "function": {"name": "allumer", "arguments": '{"pie'}}]}),
        openai_chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ce": "salon"}'}}]}),
        openai_chunk({}, finish="tool_calls"),
        openai_chunk(None, usage={"prompt_tokens": 30, "completion_tokens": 8,
                                  "total_tokens": 38}),
    ]
    client = FakeOpenAIClient(chunks)
    brain = OpenAIBrain("http://llm.local/v1", "local-model", "", client=client,
                        clock=lambda: NOW)
    conv = conv_with("Allume le salon")
    conv.messages.insert(0, {"role": "user", "content": [{"type": "text", "text": "Avant"}]})
    conv.messages.insert(1, {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "tool_use", "id": "old", "name": "allumer", "input": {"piece": "cuisine"}}]})
    conv.messages.insert(2, {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "old", "content": "ok", "is_error": False}]})
    events = await collect(brain.respond(conv, [LIGHT]))
    assert spoken(events) == "Je allume."
    assert [e for e in events if isinstance(e, ToolCall)] == [
        ToolCall("c1", "allumer", {"piece": "salon"})]
    assert events[-1] == Done(Usage("openai", "local-model", input_tokens=30, output_tokens=8))
    (req,) = client.requests
    assert req["stream"] is True and req["model"] == "local-model"
    msgs = req["messages"]
    assert msgs[0]["role"] == "system" and load_persona() in msgs[0]["content"]
    assert msgs[2] == {"role": "assistant", "content": None, "tool_calls": [
        {"id": "old", "type": "function",
         "function": {"name": "allumer", "arguments": json.dumps({"piece": "cuisine"})}}]}
    assert msgs[3] == {"role": "tool", "tool_call_id": "old", "content": "ok"}
    assert msgs[4] == {"role": "user", "content": "Allume le salon"}
    assert req["tools"][0]["function"]["name"] == "allumer"
    assert conv.messages[-1]["content"][-1]["type"] == "tool_use"


# --- AssistBrain ----------------------------------------------------------------------------------


async def test_assist_brain_posts_to_home_assistant():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={
            "conversation_id": "conv-42",
            "response": {"speech": {"plain": {"speech": "Lumière allumée."}}},
        })

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    brain = AssistBrain("http://ha.local:8123/", "jeton-test", client=client)
    conv = conv_with("Allume le salon")
    events = await collect(brain.respond(conv, [LIGHT]))
    assert spoken(events) == "Lumière allumée."
    assert events[-1] == Done(Usage("homeassistant", "assist"))
    add_user_text(conv, "Et la cuisine")
    await collect(brain.respond(conv, []))
    first, second = seen
    assert str(first.url) == "http://ha.local:8123/api/conversation/process"
    assert first.headers["authorization"] == "Bearer jeton-test"
    assert json.loads(first.content) == {"text": "Allume le salon", "language": "fr"}
    assert json.loads(second.content) == {"text": "Et la cuisine", "language": "fr",
                                          "conversation_id": "conv-42"}
    assert conv.messages[-1]["role"] == "assistant"


async def test_assist_brain_http_error():
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda r: httpx2.Response(401, json={"message": "nope"})))
    brain = AssistBrain("http://ha.local:8123", "jeton-test", client=client)
    with pytest.raises(BrainError):
        await collect(brain.respond(conv_with("Salut"), []))


# --- Depuis les réglages --------------------------------------------------------------------------


def test_router_from_settings():
    s = Settings()
    s.brain.providers["anthropic"] = ProviderConfig(kind="anthropic", api_key="test-key")
    s.brain.providers["local"] = ProviderConfig(kind="openai", base_url="http://llm.local/v1")
    s.brain.fallback_chain = ["local:petit-modele", "homeassistant"]
    s.ha.url, s.ha.token = "http://ha.local:8123", "jeton-test"
    router = BrainRouter.from_settings(s)
    assert isinstance(router.primary, AnthropicBrain)
    assert (router.primary.model, router.primary.effort, router.primary.web_search) == (
        "claude-opus-5-5", "medium", True)
    assert (router.fast.model, router.fast.effort, router.fast.web_search) == (
        "claude-haiku-5-5", "low", False)
    (local,) = router.fallbacks  # « homeassistant » est l'assistant de dernier recours
    assert isinstance(local, OpenAIBrain) and local.model == "petit-modele"
    assert isinstance(router.assist, AssistBrain)


def test_persona_mentions_the_rules():
    persona = load_persona()
    assert "SPDX" not in persona
    for word in ("enfant", "phrases courtes", "outil", "Markdown"):
        assert word.lower() in persona.lower()
