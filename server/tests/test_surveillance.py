# SPDX-License-Identifier: MIT
"""Surveillance sonore : détection (bruit, voix), enregistrements de 10 s, entités MQTT."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
import wave

import numpy as np
import pytest

from lapinou.config import Settings, SurveillanceConfig
from lapinou.surveillance import (ClipStore, Detector, Surveillance, SurveillanceMqtt, discovery,
                                  level_db)

RATE = 16000
BLOCK = 2048  # comme lapinou-mic
RID = "r1"
T0 = dt.datetime(2026, 11, 21, 22, 0, 0, tzinfo=dt.UTC)


def tone(db: float, n: int = BLOCK) -> np.ndarray:
    """Un bloc de sinus dont le RMS vaut `db` dBFS."""
    amp = 32768 * 10 ** (db / 20) * np.sqrt(2)
    return (amp * np.sin(np.arange(n) * 2 * np.pi * 440 / RATE)).astype(np.int16)


def quiet(n: int = BLOCK) -> np.ndarray:
    return tone(-80, n)


def seconds(fn, s: float) -> list[np.ndarray]:
    return [fn() for _ in range(int(s * RATE / BLOCK))]


def feed_all(det: Detector, blocks, muted=False):
    events = []
    for b in blocks:
        events += det.feed(b, muted)
    return events


def kinds(events, kind):
    return [v for k, v in events if k == kind]


def test_level_db_is_the_rms_in_dbfs():
    assert level_db(tone(-20)) == pytest.approx(-20, abs=0.2)
    assert level_db(np.zeros(BLOCK, np.int16)) == -90.0


def test_loud_for_300_ms_is_noise_then_calm_for_5_s_ends_it():
    det = Detector(SurveillanceConfig(), clock=lambda: T0)
    assert kinds(feed_all(det, seconds(quiet, 2)), "noise") == []
    ev = feed_all(det, [tone(-20)] * 2)  # 256 ms : pas encore
    assert kinds(ev, "noise") == []
    ev = feed_all(det, [tone(-20)])  # 384 ms d'affilée
    assert kinds(ev, "noise") == [True]
    ev = feed_all(det, seconds(quiet, 4.5))
    assert kinds(ev, "noise") == []
    ev = feed_all(det, seconds(quiet, 1))
    assert kinds(ev, "noise") == [False]


def test_a_short_click_is_not_noise():
    det = Detector(SurveillanceConfig(), clock=lambda: T0)
    ev = feed_all(det, [tone(-10), quiet(), tone(-10), quiet()] * 10)
    assert kinds(ev, "noise") == []


def test_the_threshold_is_the_detectors():
    det = Detector(SurveillanceConfig(), clock=lambda: T0)
    det.threshold_db = -25
    assert kinds(feed_all(det, [tone(-30)] * 10), "noise") == []
    assert kinds(feed_all(det, [tone(-20)] * 10), "noise") == [True]


def test_what_the_rabbit_plays_itself_never_triggers():
    det = Detector(SurveillanceConfig(), vad=lambda pcm: True, clock=lambda: T0)
    ev = feed_all(det, seconds(lambda: tone(-10), 3), muted=True)
    assert kinds(ev, "noise") == [] and kinds(ev, "voice") == [] and kinds(ev, "clip") == []


def test_a_clip_is_10_s_with_the_3_s_before():
    det = Detector(SurveillanceConfig(), clock=lambda: T0)
    before = seconds(lambda: tone(-60), 4)
    ev = feed_all(det, before + [tone(-20)] * 3 + seconds(quiet, 12))
    clips = kinds(ev, "clip")
    assert len(clips) == 1
    clip = clips[0]
    assert len(clip.samples) == 10 * RATE
    assert clip.kinds == {"bruit"}
    assert clip.peak_db == pytest.approx(-20, abs=0.5)
    assert clip.started == T0
    # Les 3 s d'avant : du -60 dBFS, puis le bruit, avant le calme.
    head = clip.samples[: 2 * RATE]
    assert level_db(head) == pytest.approx(-60, abs=0.5)
    onset = 3 * RATE - 3 * BLOCK  # le bruit commence trois blocs avant la détection
    assert level_db(clip.samples[onset + BLOCK : onset + 2 * BLOCK]) == pytest.approx(-20, abs=0.5)


def test_a_noise_that_lasts_gives_clips_one_after_the_other():
    det = Detector(SurveillanceConfig(), clock=lambda: T0)
    ev = feed_all(det, seconds(lambda: tone(-20), 25) + seconds(quiet, 12))
    clips = kinds(ev, "clip")
    assert len(clips) == 3
    assert all(len(c.samples) == 10 * RATE for c in clips)


def test_a_voice_is_detected_and_marks_the_clip():
    speaking = {"on": False}
    det = Detector(SurveillanceConfig(), vad=lambda pcm: speaking["on"], clock=lambda: T0)
    feed_all(det, seconds(quiet, 1))
    speaking["on"] = True
    ev = feed_all(det, seconds(lambda: tone(-50), 1))  # parole faible : pas de bruit
    assert kinds(ev, "voice") == [True]
    assert kinds(ev, "noise") == []
    speaking["on"] = False
    ev = feed_all(det, seconds(quiet, 12))
    assert kinds(ev, "voice") == [False]
    (clip,) = kinds(ev, "clip")
    assert clip.kinds == {"voix"}


def test_level_is_reported_each_second_when_it_changes_or_every_30_s():
    det = Detector(SurveillanceConfig(), clock=lambda: T0)
    ev = feed_all(det, seconds(lambda: tone(-50), 3))
    levels = kinds(ev, "level")
    assert len(levels) == 1 and levels[0] == pytest.approx(-50, abs=0.5)  # une fois : stable
    ev = feed_all(det, seconds(lambda: tone(-44), 2))
    assert len(kinds(ev, "level")) == 1  # 6 dB de plus
    ev = feed_all(det, seconds(lambda: tone(-44), 31))
    assert len(kinds(ev, "level")) == 1  # la piqûre de rappel des 30 s


def test_reset_forgets_everything():
    det = Detector(SurveillanceConfig(), clock=lambda: T0)
    feed_all(det, [tone(-20)] * 5)
    det.reset()
    ev = feed_all(det, seconds(quiet, 12))
    assert kinds(ev, "noise") == [] and kinds(ev, "clip") == []


# --- Enregistrements ------------------------------------------------------------------------


def test_clip_store_saves_a_wav_found_by_its_token_only(tmp_path):
    store = ClipStore(tmp_path / "clips", keep_days=7, keep_max=10)
    samples = tone(-20, RATE)
    token = store.save(samples)
    assert len(token) == 22
    path = store.path(token)
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2)
        assert np.array_equal(np.frombuffer(w.readframes(RATE), np.int16), samples)
    for bad in ("../state", "x" * 22 + "/", "", token[:-1], token + "a"):
        assert store.path(bad) is None


def test_clip_store_keeps_the_newest_and_drops_old_ones(tmp_path):
    store = ClipStore(tmp_path / "clips", keep_days=7, keep_max=3)
    tokens = []
    for i in range(5):
        tokens.append(store.save(quiet(10)))
        os.utime(store.path(tokens[-1]), (time.time() - 100 + i, time.time() - 100 + i))
        store.purge()
    assert [store.path(t) is not None for t in tokens] == [False, False, True, True, True]
    old = store.path(tokens[2])
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    store.purge()
    assert store.path(tokens[2]) is None


# --- MQTT -------------------------------------------------------------------------------------


def test_discovery_puts_the_entities_in_the_rabbits_device():
    msgs = dict(discovery("lapinou", "homeassistant", RID))
    objects = {topic.split("/")[1] + ":" + topic.split("/")[3] for topic in msgs}
    assert objects == {"switch:surv_armed", "number:surv_threshold", "sensor:surv_level",
                       "binary_sensor:surv_noise", "binary_sensor:surv_voice",
                       "event:surv_event"}
    for topic, payload in msgs.items():
        assert topic.startswith(f"homeassistant/") and topic.endswith(f"/{RID}/" +
                                                                       topic.split("/")[3] + "/config")
        cfg = json.loads(payload)
        assert cfg["device"]["identifiers"] == [f"lapinou_{RID}"]
        assert cfg["unique_id"] == f"lapinou_{RID}_{topic.split('/')[3]}"
        assert cfg["availability"] == [{"topic": f"lapinou/{RID}/availability"},
                                       {"topic": "lapinou/server/availability"}]
        assert cfg["availability_mode"] == "all"
    switch = json.loads(msgs[f"homeassistant/switch/{RID}/surv_armed/config"])
    assert switch["command_topic"] == f"lapinou/{RID}/surveillance/armed/set"
    assert switch["state_topic"] == f"lapinou/{RID}/surveillance/armed"
    number = json.loads(msgs[f"homeassistant/number/{RID}/surv_threshold/config"])
    assert (number["min"], number["max"], number["step"]) == (-70, -10, 1)
    event = json.loads(msgs[f"homeassistant/event/{RID}/surv_event/config"])
    assert event["event_types"] == ["bruit", "voix"]
    assert json.loads(msgs[f"homeassistant/binary_sensor/{RID}/surv_noise/config"])[
        "device_class"] == "sound"


class FakeClient:
    def __init__(self):
        self.published: list[tuple[str, str, bool]] = []
        self.subscribed: list[str] = []
        self.will = None
        self.on_connect = self.on_message = None

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def username_pw_set(self, u, p):
        pass

    def reconnect_delay_set(self, a, b):
        pass

    def connect_async(self, host, port, keepalive):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def is_connected(self):
        return True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

        class Info:
            def wait_for_publish(self, t):
                pass

        return Info()

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def connect(self):
        self.on_connect(self, None, None, 0, None)

    def message(self, topic, payload):
        class Msg:
            pass

        m = Msg()
        m.topic, m.payload = topic, payload.encode()
        self.on_message(self, None, m)

    def last(self, topic):
        values = [p for t, p, _ in self.published if t == topic]
        return values[-1] if values else None


def make_hub(tmp_path, client, *, vad=None, transcriber=None, settings=None):
    settings = settings or Settings.model_validate({"mqtt": {"host": "broker.test"}})
    mqtt = SurveillanceMqtt.from_settings(settings, client_factory=lambda: client)
    return Surveillance(settings.surveillance, mqtt, tmp_path / "surveillance",
                        "http://srv.test:8080", make_vad=vad, transcriber=transcriber,
                        clock=lambda: T0)


async def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("délai dépassé")
        await asyncio.sleep(0.01)


async def test_hub_announces_the_rabbit_and_follows_the_switch(tmp_path):
    client = FakeClient()
    hub = make_hub(tmp_path, client)
    await hub.start()
    assert client.will == ("lapinou/server/availability", "offline", True)
    client.connect()
    assert client.last("lapinou/server/availability") == "online"
    assert "lapinou/+/surveillance/+/set" in client.subscribed
    assert "homeassistant/status" in client.subscribed
    hub.add(RID)
    await until(lambda: client.last(f"homeassistant/switch/{RID}/surv_armed/config") is not None)
    assert client.last(f"lapinou/{RID}/surveillance/armed") == "OFF"
    assert client.last(f"lapinou/{RID}/surveillance/threshold") == "-40"
    # Désarmé : le micro n'est pas examiné.
    for b in [tone(-10)] * 10:
        hub.feed(RID, b, False)
    await asyncio.sleep(0.1)
    assert client.last(f"lapinou/{RID}/surveillance/noise") in (None, "OFF")
    client.message(f"lapinou/{RID}/surveillance/armed/set", "ON")
    await until(lambda: client.last(f"lapinou/{RID}/surveillance/armed") == "ON")
    for b in [tone(-10)] * 10:
        hub.feed(RID, b, False)
    await until(lambda: client.last(f"lapinou/{RID}/surveillance/noise") == "ON")
    client.message(f"lapinou/{RID}/surveillance/armed/set", "OFF")
    await until(lambda: client.last(f"lapinou/{RID}/surveillance/armed") == "OFF")
    assert client.last(f"lapinou/{RID}/surveillance/noise") == "OFF"
    assert client.last(f"lapinou/{RID}/surveillance/level") == "None"
    await hub.close()


async def test_hub_keeps_armed_and_threshold_across_a_restart(tmp_path):
    client = FakeClient()
    hub = make_hub(tmp_path, client)
    await hub.start()
    client.connect()
    hub.add(RID)
    client.message(f"lapinou/{RID}/surveillance/armed/set", "ON")
    client.message(f"lapinou/{RID}/surveillance/threshold/set", "-5")  # hors plage : -10
    client.message(f"lapinou/{RID}/surveillance/threshold/set", "abc")  # ignoré
    await until(lambda: client.last(f"lapinou/{RID}/surveillance/threshold") == "-10")
    await hub.close()
    client2 = FakeClient()
    hub2 = make_hub(tmp_path, client2)
    await hub2.start()
    client2.connect()
    hub2.add(RID)
    await until(lambda: client2.last(f"lapinou/{RID}/surveillance/armed") == "ON")
    assert client2.last(f"lapinou/{RID}/surveillance/threshold") == "-10"
    await hub2.close()


async def test_home_assistant_back_online_gets_the_entities_again(tmp_path):
    client = FakeClient()
    hub = make_hub(tmp_path, client)
    await hub.start()
    client.connect()
    hub.add(RID)
    topic = f"homeassistant/switch/{RID}/surv_armed/config"
    await until(lambda: client.last(topic) is not None)
    n = sum(1 for t, _, _ in client.published if t == topic)
    client.message("homeassistant/status", "online")
    await until(lambda: sum(1 for t, _, _ in client.published if t == topic) == n + 1)
    await hub.close()


class FakeTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, samples):
        self.calls += 1
        return "ouvre la porte"


async def test_a_clip_with_a_voice_is_saved_transcribed_and_announced(tmp_path):
    client = FakeClient()
    speaking = {"on": False}
    transcriber = FakeTranscriber()
    hub = make_hub(tmp_path, client, vad=lambda: (lambda pcm: speaking["on"]),
                   transcriber=transcriber)
    await hub.start()
    client.connect()
    hub.add(RID)
    client.message(f"lapinou/{RID}/surveillance/armed/set", "ON")
    await until(lambda: client.last(f"lapinou/{RID}/surveillance/armed") == "ON")
    speaking["on"] = True
    for b in seconds(lambda: tone(-30), 2):
        hub.feed(RID, b, False)
    await until(lambda: client.last(f"lapinou/{RID}/surveillance/voice") == "ON")
    speaking["on"] = False
    for b in seconds(quiet, 12):
        hub.feed(RID, b, False)
    await until(lambda: client.last(f"lapinou/{RID}/surveillance/event") is not None)
    event = json.loads(client.last(f"lapinou/{RID}/surveillance/event"))
    assert event["event_type"] == "voix"
    assert event["texte"] == "ouvre la porte"
    assert event["bruit"] is True and event["voix"] is True
    assert event["debut"] == T0.isoformat()
    url = event["enregistrement"]
    assert url.startswith("http://srv.test:8080/surveillance/clips/") and url.endswith(".wav")
    token = url.rsplit("/", 1)[1][:-4]
    assert hub.store.path(token) is not None
    assert transcriber.calls == 1
    await hub.close()


async def test_without_mqtt_nothing_runs(tmp_path):
    hub = Surveillance(SurveillanceConfig(), None, tmp_path / "s", "http://x")
    assert not hub.enabled
    await hub.start()
    hub.add(RID)
    hub.feed(RID, tone(-10), False)
    await hub.close()


def test_the_clip_route_serves_the_wav_by_its_token(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from lapinou import surveillance

    hub = Surveillance(SurveillanceConfig(), None, tmp_path / "s", "http://x")
    token = hub.store.save(tone(-20, RATE))
    app = FastAPI()
    app.state.surveillance = hub
    app.include_router(surveillance.router)
    http = TestClient(app)
    r = http.get(f"/surveillance/clips/{token}.wav")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    assert http.get(f"/surveillance/clips/{'A' * 22}.wav").status_code == 404
    assert http.get("/surveillance/clips/..%2Fstate.json.wav").status_code == 404
