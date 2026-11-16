# SPDX-License-Identifier: MIT
"""Plateforme multi-lapins, face à de faux contrôleurs Karotz (asyncio)."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct

import pytest

from lapinou.rabbit.platform import Step, build_codec
from lapinou.rabbit.registry import RabbitRegistry

pytestmark = pytest.mark.skipif(
    shutil.which("protoc") is None and "LAPINOU_PROTOC" not in os.environ,
    reason="protoc introuvable",
)


class FakeController:
    """Le contrôleur d'un lapin : se connecte, s'authentifie, accepte le mode interactif."""

    def __init__(self, codec, login):
        self.codec, self.login = codec, login
        self.inbox: list[str] = []
        self.reader = self.writer = None

    async def connect(self, port):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        token = await self.until(r"^authenticationToken \{")
        tid = re.search(r'^id: "([^"]*)"', token, re.M).group(1)
        await self.send(
            f'id: "c-auth" correlationId: "{tid}" '
            f'authenticationResponse {{ login: "{self.login}" authentication: "x" }}'
        )

    async def send(self, text):
        payload = await asyncio.to_thread(self.codec.encode, text)
        self.writer.write(struct.pack(">I", len(payload)) + payload)
        await self.writer.drain()

    async def recv(self):
        head = await self.reader.readexactly(4)
        payload = await self.reader.readexactly(struct.unpack(">I", head)[0])
        return await asyncio.to_thread(self.codec.decode, payload)

    async def until(self, pattern, timeout=10):
        """Le premier message reçu (ou déjà reçu) qui correspond ; accepte le mode interactif."""

        async def loop():
            while True:
                for i, text in enumerate(self.inbox):
                    if re.search(pattern, text, re.S | re.M):
                        return self.inbox.pop(i)
                text = await self.recv()
                if re.search(r"^interactiveMode \{", text, re.M):
                    mid = re.search(r'^id: "([^"]*)"', text, re.M).group(1)
                    await self.send(f'id: "c-{mid}" correlationId: "{mid}" response {{ code: OK }}')
                self.inbox.append(text)

        return await asyncio.wait_for(loop(), timeout)

    async def close(self):
        self.writer.close()


@pytest.fixture
async def platform():
    codec = build_codec()
    events: list[tuple] = []

    async def on_button(rabbit, kind):
        events.append(("button", rabbit.id, kind))

    async def on_rfid(rabbit, tag, action):
        events.append(("rfid", rabbit.id, tag, action))

    def on_playback(rabbit, pid, status):  # un rappel synchrone est accepté aussi
        events.append(("playback", rabbit.id, pid, status))

    registry = RabbitRegistry(
        codec=codec, on_button=on_button, on_rfid=on_rfid, on_playback=on_playback
    )
    await registry.start("127.0.0.1", 0)
    controllers: list[FakeController] = []

    async def connect(login):
        c = FakeController(codec, login)
        controllers.append(c)
        await c.connect(registry.port)
        # Le serveur prend la main (mode interactif) une fois le contrôleur authentifié.
        await c.until(r"^interactiveMode \{")
        await asyncio.wait_for(registry.get(login).wait_ready(), 10)
        return c

    registry.connect = connect
    registry.events = events
    yield registry
    for c in controllers:
        await c.close()
    await registry.close()


async def test_schedule_config_sent_with_timezone(platform):
    c = await platform.connect("lapin-1")
    schedule = await c.until(r"^scheduleConfig \{")
    assert 'gmt: "Europe/Paris"' in schedule
    rabbit = platform.get("lapin-1")
    assert rabbit.connected and rabbit.state == "ready"


async def test_two_rabbits_get_their_own_commands(platform):
    a = await platform.connect("lapin-a")
    b = await platform.connect("lapin-b")
    await platform.get("lapin-a").ears(3, 4)
    await platform.get("lapin-b").led("FF0000", None, None)
    ears = await a.until(r"^ears \{")
    assert "left: 3" in ears and "right: 4" in ears
    led = await b.until(r"^led \{")
    assert 'color: "FF0000"' in led
    assert not any(re.search(r"^led \{", t, re.M) for t in a.inbox)
    assert not any(re.search(r"^ears \{", t, re.M) for t in b.inbox)


async def test_play_carries_interactive_id(platform):
    c = await platform.connect("lapin-1")
    await platform.get("lapin-1").play("http://x/a.mp3", "pb-1")
    play = await c.until(r"^multimedia \{")
    assert 'url: "http://x/a.mp3"' in play
    assert re.search(r'^interactiveId: ".+"', play, re.M)


async def test_led_pulse_and_fade(platform):
    c = await platform.connect("lapin-1")
    r = platform.get("lapin-1")
    await r.led("00FF00", "pulse", 300)
    assert "period: 300" in await c.until(r"^led \{")
    await r.led("0000FF", "fade", None)
    assert "fade {" in await c.until(r"^led \{")
    with pytest.raises(ValueError):
        await r.led("0000FF", "nope", None)


async def test_choreography_runs_steps_in_order(platform):
    c = await platform.connect("lapin-1")
    r = platform.get("lapin-1")
    await r.choreography(
        [
            Step(ears=(1, 2), led="FF0000"),
            Step(pulse="pulse", led="00FF00", wait_ms=10),
            Step(ears=(0, 0)),
        ]
    )
    kinds = []
    for _ in range(4):
        text = await c.until(r"^(ears|led) \{")
        kinds.append(re.search(r"^(ears|led) \{", text, re.M).group(1))
    assert kinds == ["ears", "led", "led", "ears"]


async def test_stop_playback(platform):
    c = await platform.connect("lapin-1")
    await platform.get("lapin-1").stop_playback()
    assert "stop {" in await c.until(r"^multimedia \{")


async def test_button_rfid_playback_events(platform):
    c = await platform.connect("lapin-1")
    await c.send('id: "b1" buttonCallback { type: DOUBLE }')
    await c.send('id: "r1" rfidCallback { id: "04AABB" direction: IN }')
    await platform.get("lapin-1").play("http://x/a.mp3", "pb-9")
    await c.until(r"^multimedia \{")
    await c.send('id: "m1" multimediaCallback { action: PLAY }')
    await c.send('id: "m2" multimediaCallback { action: STOP }')
    for _ in range(100):
        if len(platform.events) >= 4:
            break
        await asyncio.sleep(0.05)
    assert platform.events == [
        ("button", "lapin-1", "double"),
        ("rfid", "lapin-1", "04AABB", "in"),
        ("playback", "lapin-1", "pb-9", "playing"),
        ("playback", "lapin-1", "pb-9", "stopped"),
    ]


async def test_ping_answered_and_reconnect_keeps_rabbit(platform):
    c = await platform.connect("lapin-1")
    first = platform.get("lapin-1")
    await c.send('id: "p1" ping { }')
    assert 'correlationId: "p1"' in await c.until(r"correlationId")
    await c.close()
    for _ in range(100):
        if not first.connected:
            break
        await asyncio.sleep(0.05)
    assert not first.connected and first.state == "disconnected"
    with pytest.raises(ConnectionError):
        await first.ears(1, 1)
    c2 = await platform.connect("lapin-1")
    assert platform.get("lapin-1") is first and first.connected
    await first.ears(5, 6)
    assert "left: 5" in await c2.until(r"^ears \{")
