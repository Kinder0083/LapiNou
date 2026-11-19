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



async def test_schedule_config_error_reply_is_not_a_refusal(platform, caplog):
    """Le vrai contrôleur répond ERROR à scheduleConfig même quand il règle l'heure et le
    fuseau (tests/expected/controller_platform.txt) : pas d'avertissement « commande refusée »."""
    c = await platform.connect("lapin-s")
    schedule = await c.until(r"^scheduleConfig \{")
    sid = re.search(r'^id: "([^"]*)"', schedule, re.M).group(1)
    with caplog.at_level("DEBUG", logger="lapinou.rabbit.platform"):
        await c.send(f'id: "c-s" correlationId: "{sid}" response {{ code: ERROR }}')
        await c.send('id: "c-p" ping { }')
        await c.until(r'^correlationId: "c-p"')
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert platform.get("lapin-s").state == "ready"


async def test_refused_interactive_mode_is_retried_soon(monkeypatch, caplog):
    """Le contrôleur refuse tant qu'il n'est pas AVAILABLE : le serveur réessaie vite, pas
    au renouvellement (des heures plus tard). Le premier refus suit chaque connexion (le
    contrôleur se configure encore) : une information, pas un avertissement."""
    caplog.set_level("INFO", logger="lapinou.rabbit.platform")
    import lapinou.rabbit.platform as platform_mod

    monkeypatch.setattr(platform_mod, "INTERACTIVE_RETRY_S", 0.05)
    codec = build_codec()
    registry = RabbitRegistry(codec=codec)
    await registry.start("127.0.0.1", 0)
    c = FakeController(codec, "lapin-r")
    try:
        await c.connect(registry.port)
        seen = []
        refusals = 2

        async def loop():
            nonlocal refusals
            while True:
                text = await c.recv()
                seen.append(text.split(" ", 3)[:3])
                if re.search(r"^interactiveMode \{", text, re.M):
                    mid = re.search(r'^id: "([^"]*)"', text, re.M).group(1)
                    code = "ERROR" if refusals > 0 else "OK"
                    refusals -= 1
                    await c.send(f'id: "c-{mid}" correlationId: "{mid}" response {{ code: {code} }}')
                    if code == "OK":
                        return

        await asyncio.wait_for(loop(), 10)
        await asyncio.wait_for(registry.get("lapin-r").wait_ready(), 10)
        assert registry.get("lapin-r").state == "ready"
        refused = [r.levelname for r in caplog.records if "mode interactif refusé" in r.getMessage()]
        assert refused == ["INFO", "WARNING"]
    finally:
        await c.close()
        await registry.close()

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
    # Le contrôleur passe l'id du message à multimedia-daemon, qui marque le flux micro avec :
    # c'est le playback_id de l'annuleur d'écho.
    assert re.search(r'^id: "pb-1"', play, re.M)


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


async def test_config_without_apps_pushed_at_connection(platform):
    """Le vrai contrôleur attend `config` pour passer de CONNECTING à AVAILABLE (sans lui,
    il refuse le mode interactif) ; aucune application n'y est annoncée (R43)."""
    c = await platform.connect("lapin-1")
    config = await c.until(r"^config \{")
    assert "playsounds: true" in config and "configapp" not in config.lower()
    await c.until(r"^pendingmessage: false")


async def test_slow_handler_does_not_block_controller(platform):
    release = asyncio.Event()

    async def slow(rabbit, kind):
        await release.wait()

    platform.on_button = slow
    c = await platform.connect("lapin-1")
    await c.send('id: "b1" buttonCallback { type: SIMPLE }')
    await c.send('id: "p1" ping { }')
    assert 'correlationId: "p1"' in await c.until(r"correlationId", timeout=2)
    await asyncio.wait_for(platform.get("lapin-1").ears(1, 2), 2)
    assert "left: 1" in await c.until(r"^ears \{", timeout=2)
    release.set()


async def test_handler_exception_is_isolated(platform):
    def boom(rabbit, kind):
        raise RuntimeError("x")

    platform.on_button = boom
    c = await platform.connect("lapin-1")
    await c.send('id: "b1" buttonCallback { type: SIMPLE }')
    await c.send('id: "p1" ping { }')
    assert 'correlationId: "p1"' in await c.until(r"correlationId", timeout=2)


async def test_play_replacing_play_attributes_events(platform):
    c = await platform.connect("lapin-1")
    r = platform.get("lapin-1")
    await r.play("http://x/a.mp3", "A")
    await r.play("http://x/b.mp3", "B")
    await c.send('id: "m1" multimediaCallback { action: STOP }')  # fin de A
    await c.send('id: "m2" multimediaCallback { action: PLAY }')  # début de B
    for _ in range(100):
        if len(platform.events) >= 2:
            break
        await asyncio.sleep(0.05)
    assert platform.events == [
        ("playback", "lapin-1", "A", "stopped"),
        ("playback", "lapin-1", "B", "playing"),
    ]


async def test_failed_play_leaves_no_stale_id(platform):
    r = platform.get("lapin-1")
    with pytest.raises(ConnectionError):
        await r.play("http://x/a.mp3", "A")  # jamais connecté
    assert r._playback_id is None


async def test_interactive_mode_renewed_before_expiry():
    codec = build_codec()
    registry = RabbitRegistry(codec=codec, interactive_ttl=1)
    await registry.start("127.0.0.1", 0)
    c = FakeController(codec, "lapin-1")
    try:
        await c.connect(registry.port)
        first = await c.until(r"^interactiveMode \{")
        await asyncio.wait_for(registry.get("lapin-1").wait_ready(), 10)
        second = await c.until(r"^interactiveMode \{", timeout=3)  # renouvelé
        id1 = re.search(r'^\s*interactiveId: "([^"]+)"', first, re.M).group(1)
        id2 = re.search(r'^\s*interactiveId: "([^"]+)"', second, re.M).group(1)
        assert id1 != id2
        await asyncio.sleep(0.2)
        await registry.get("lapin-1").ears(1, 1)
        ears = await c.until(r"^ears \{")
        assert f'interactiveId: "{id2}"' in ears
    finally:
        await c.close()
        await registry.close()


async def test_bad_interactive_id_reenters_and_retries_once(platform):
    c = await platform.connect("lapin-1")
    await platform.get("lapin-1").ears(7, 8)
    ears = await c.until(r"^ears \{")
    eid = re.search(r'^id: "([^"]*)"', ears, re.M).group(1)
    await c.send(f'id: "e1" correlationId: "{eid}" response {{ code: BAD_INTERACTIVEID }}')
    await c.until(r"^interactiveMode \{")
    again = await c.until(r"^ears \{")
    assert "left: 7" in again


async def test_controller_reset_resyncs_and_retries_once(platform):
    """Vu sur le vrai lapin : « Karotz stop interactive mode, resetting », puis chaque commande
    refusée (ERROR) sans fin. Le serveur renvoie sa configuration, reprend la main et renvoie la
    commande ; une deuxième erreur aussitôt après ne relance pas tout (au plus une fois par
    RESYNC_MIN_S)."""
    c = await platform.connect("lapin-1")
    await c.until(r"^config \{")  # celui de la connexion
    await platform.get("lapin-1").ears(3, 4)
    ears = await c.until(r"^ears \{")
    eid = re.search(r'^id: "([^"]*)"', ears, re.M).group(1)
    await c.send(f'id: "e1" correlationId: "{eid}" response {{ code: ERROR }}')
    await c.until(r"^config \{")
    await c.until(r"^interactiveMode \{")
    again = await c.until(r"^ears \{")
    assert "left: 3" in again
    aid = re.search(r'^id: "([^"]*)"', again, re.M).group(1)
    await c.send(f'id: "e2" correlationId: "{aid}" response {{ code: ERROR }}')
    await platform.get("lapin-1").ears(5, 6)
    nxt = await c.until(r"^(ears|config|interactiveMode) \{")
    assert re.search(r"^ears \{", nxt, re.M) and "left: 5" in nxt


async def test_events_keep_order_per_rabbit_with_slow_handler(platform):
    seen = []

    async def slow(rabbit, kind):
        if not seen:
            await asyncio.sleep(0.2)
        seen.append(kind)

    platform.on_button = slow
    c = await platform.connect("lapin-1")
    await c.send('id: "b1" buttonCallback { type: LONG_START }')
    await c.send('id: "b2" buttonCallback { type: LONG_STOP }')
    for _ in range(100):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.05)
    assert seen == ["long_start", "long_stop"]


async def test_bad_interactive_id_on_play_keeps_playback_events(platform):
    c = await platform.connect("lapin-1")
    await platform.get("lapin-1").play("http://x/a.mp3", "A")
    play = await c.until(r"^multimedia \{")
    pid = re.search(r'^id: "([^"]*)"', play, re.M).group(1)
    await c.send(f'id: "e1" correlationId: "{pid}" response {{ code: BAD_INTERACTIVEID }}')
    await c.until(r"^interactiveMode \{")
    retry = await c.until(r"^multimedia \{")
    rid = re.search(r'^id: "([^"]*)"', retry, re.M).group(1)
    await c.send('id: "m1" multimediaCallback { action: PLAY }')
    await c.send('id: "m2" multimediaCallback { action: STOP }')
    for _ in range(100):
        if len(platform.events) >= 2:
            break
        await asyncio.sleep(0.05)
    assert rid == pid == "A"  # le renvoi garde l'id : les marques d'écho restent reconnues
    assert platform.events == [
        ("playback", "lapin-1", "A", "playing"),
        ("playback", "lapin-1", "A", "stopped"),
    ]
