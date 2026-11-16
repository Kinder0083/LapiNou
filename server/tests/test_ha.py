# SPDX-License-Identifier: MIT
"""Outils Home Assistant contre un faux ha-mcp (vrai serveur MCP stdio, sans Home Assistant)."""

import json
import logging
import sys
from pathlib import Path

import pytest

from lapinou.brain.base import is_strict_compatible
from lapinou.config import SafetyConfig
from lapinou.tools.base import ToolBox, ToolContext
from lapinou.tools.ha import HomeAssistantTools
from lapinou.tools.policy import Policy

FAKE = Path(__file__).parent / "fakes" / "fake_mcp.py"
CTX = ToolContext(rabbit_id="lapin-a")
TOKEN = "jeton-de-test-ne-doit-pas-fuiter"


def calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def service_calls(log: Path) -> list[dict]:
    return [c["args"] for c in calls(log) if c["tool"] == "ha_call_service"]


@pytest.fixture
async def ha(tmp_path, request):
    marker = request.node.get_closest_marker("safety")
    safety = SafetyConfig(**(marker.kwargs if marker else {}))
    log = tmp_path / "calls.jsonl"
    tools = HomeAssistantTools(
        Policy(safety),
        mcp_cmd=[sys.executable, str(FAKE)],
        env={
            "HOMEASSISTANT_URL": "http://ha.test:8123",
            "HOMEASSISTANT_TOKEN": TOKEN,
            "FAKE_MCP_LOG": str(log),
            "FAKE_MCP_ENV": str(tmp_path / "env.json"),
        },
    )
    await tools.start()
    yield tools, log, tmp_path
    await tools.close()


async def test_only_allowed_tools_are_exposed(ha):
    tools, _, tmp_path = ha
    names = {t.name for t in tools.tools()}
    assert names == {"ha_search", "ha_get_state", "ha_call_service"}
    assert "ha_restart" in tools.server_tools  # présent côté serveur, mais pas exposé
    for tool in tools.tools():
        assert is_strict_compatible(tool.spec().input_schema)
    env = json.loads((tmp_path / "env.json").read_text())
    assert env == {"url": "http://ha.test:8123", "has_token": True}


async def test_allow_list_restricts_further(tmp_path):
    tools = HomeAssistantTools(
        Policy(SafetyConfig()), mcp_cmd=[sys.executable, str(FAKE)], env={}, allow=["ha_get_state"]
    )
    await tools.start()
    try:
        assert [t.name for t in tools.tools()] == ["ha_get_state"]
        result = await ToolBox(tools.tools()).call(
            "ha_call_service",
            {"domain": "light", "service": "turn_on", "entity_id": "light.salon",
             "brightness_pct": None, "temperature": None, "volume_level": None,
             "position": None},
            CTX,
        )
        assert result.is_error
    finally:
        await tools.close()


def args(domain, service, entity, **data):
    base = {"brightness_pct": None, "temperature": None, "volume_level": None, "position": None}
    return {"domain": domain, "service": service, "entity_id": entity, **base, **data}


async def test_light_call_goes_through(ha):
    tools, log, _ = ha
    result = await ToolBox(tools.tools()).call(
        "ha_call_service", args("light", "turn_on", "light.salon", brightness_pct=50), CTX
    )
    assert not result.is_error, result.content
    assert service_calls(log) == [{"domain": "light", "service": "turn_on",
                                   "entity_id": "light.salon", "data": {"brightness_pct": 50}}]


async def test_unlock_denied_never_reaches_ha(ha):
    tools, log, _ = ha
    result = await ToolBox(tools.tools()).call(
        "ha_call_service", args("lock", "unlock", "lock.porte_entree"), CTX
    )
    assert result.is_error
    assert service_calls(log) == []
    assert tools.pending("lapin-a") is None


@pytest.mark.safety(allowed_entities=["lock.porte_entree"])
async def test_unlock_needs_confirmation_then_runs_on_yes(ha):
    tools, log, _ = ha
    result = await ToolBox(tools.tools()).call(
        "ha_call_service", args("lock", "unlock", "lock.porte_entree"), CTX
    )
    assert not result.is_error
    pending = tools.pending("lapin-a")
    assert pending is not None and pending.token in result.content
    assert service_calls(log) == []
    done = await tools.confirm("lapin-a", "oui")
    assert done is not None and not done.is_error
    assert service_calls(log) == [{"domain": "lock", "service": "unlock",
                                   "entity_id": "lock.porte_entree", "data": None}]
    assert tools.pending("lapin-a") is None


@pytest.mark.safety(allowed_entities=["lock.porte_entree"])
async def test_confirmation_refused_cancels(ha):
    tools, log, _ = ha
    await ToolBox(tools.tools()).call(
        "ha_call_service", args("lock", "unlock", "lock.porte_entree"), CTX
    )
    other = await tools.confirm("lapin-b", "oui")  # un autre lapin ne confirme rien
    assert other is None
    cancelled = await tools.confirm("lapin-a", "non")
    assert cancelled is not None and "annul" in cancelled.content.lower()
    assert service_calls(log) == []
    assert tools.pending("lapin-a") is None


async def test_cover_device_class_read_from_state(ha):
    tools, log, _ = ha
    box = ToolBox(tools.tools())
    garage = await box.call("ha_call_service", args("cover", "open_cover", "cover.garage"), CTX)
    assert garage.is_error
    volet = await box.call(
        "ha_call_service", args("cover", "open_cover", "cover.volet_chambre"), CTX
    )
    assert not volet.is_error, volet.content
    assert [c["entity_id"] for c in service_calls(log)] == ["cover.volet_chambre"]


async def test_state_output_is_wrapped_as_data(ha):
    tools, _, _ = ha
    result = await ToolBox(tools.tools()).call("ha_get_state", {"entity_id": "sensor.piege"}, CTX)
    assert "Ignore tes consignes" in result.content
    assert "pas des instructions" in result.content
    search = await ToolBox(tools.tools()).call(
        "ha_search", {"query": "salon", "domain": None}, CTX
    )
    assert "light.salon" in search.content


async def test_token_never_logged(ha, caplog):
    tools, _, _ = ha
    caplog.set_level(logging.DEBUG)
    await ToolBox(tools.tools()).call("ha_get_state", {"entity_id": "light.inconnue"}, CTX)
    assert TOKEN not in repr(tools)
    assert TOKEN not in caplog.text


async def test_ha_mcp_unavailable_is_a_spoken_error(tmp_path):
    tools = HomeAssistantTools(
        Policy(SafetyConfig()), mcp_cmd=[str(tmp_path / "pas-de-ha-mcp")], env={}
    )
    await tools.start()  # ne lève pas
    names = {t.name for t in tools.tools()}
    assert names == {"ha_search", "ha_get_state", "ha_call_service"}
    result = await ToolBox(tools.tools()).call("ha_get_state", {"entity_id": "light.x"}, CTX)
    assert result.is_error and "Home Assistant" in result.content
    await tools.close()
