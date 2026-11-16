# SPDX-License-Identifier: MIT
"""Synthèse vocale : découpage en phrases, MP3 + PCM 16 kHz cohérents, stockage 10 min, serveur
de compatibilité (protocole de voice-daemon). Moteur factice pour la logique ; les vrais moteurs
(Kokoro, Piper) sont ignorés tant que leurs modèles manquent."""

from __future__ import annotations

import io
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from lapinou.app import create_app
from lapinou.config import Settings
from lapinou.voice import tts
from lapinou.web import tts_compat

RATE = 24000


def sine_wav(seconds: float = 0.6, rate: int = RATE) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    pcm = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype("<i2")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


class FakeSynth:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, text: str) -> bytes:
        self.calls.append(text)
        return sine_wav()


@pytest.fixture
def synth():
    return FakeSynth()


@pytest.fixture
def voice(synth):
    return tts.Voice("kokoro", "ff_siwis", synth=synth, base_url="http://10.0.0.5:8080")


# --- split_sentences ---------------------------------------------------------------------------


def test_split_sentences_trois_phrases():
    assert tts.split_sentences("Bonjour. Il fait 12 °C ! Et toi ?") == [
        "Bonjour.",
        "Il fait 12 °C !",
        "Et toi ?",
    ]


@pytest.mark.parametrize(
    "text, want",
    [
        ("", []),
        ("   \n ", []),
        ("Sans point final", ["Sans point final"]),
        ("Il fait 3.5 degrés. Super", ["Il fait 3.5 degrés.", "Super"]),
        ("Quoi ?! Non… Si.", ["Quoi ?!", "Non…", "Si."]),
        ("Ligne un\nLigne deux", ["Ligne un", "Ligne deux"]),
        ("Dit-il : « Oui. » Puis part.", ["Dit-il : « Oui. »", "Puis part."]),
    ],
)
def test_split_sentences_cas(text, want):
    assert tts.split_sentences(text) == want


# --- speak -------------------------------------------------------------------------------------


def is_mp3(data: bytes) -> bool:
    return len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0


async def test_speak_mp3_valide_et_pcm_16k_coherent(voice):
    speech = await voice.speak("Bonjour le monde.")
    assert speech.pcm16k.dtype == np.int16 and speech.pcm16k.ndim == 1
    assert speech.duration_s == pytest.approx(len(speech.pcm16k) / 16000)
    assert speech.duration_s == pytest.approx(0.6, abs=0.12)  # 0,6 s de synthèse + délai MP3
    assert np.abs(speech.pcm16k).max() > 3000  # pas du silence
    assert speech.mp3_url == f"http://10.0.0.5:8080/tts/{speech.id}.mp3"
    mp3 = voice.store.get(speech.id)
    assert mp3 is not None and is_mp3(mp3)


async def test_pcm_aligne_sur_le_mp3_servi(voice):
    """Le PCM est le décodage du MP3 servi : le retard de l'encodeur y est inclus."""
    speech = await voice.speak("Test d'alignement.")
    assert np.array_equal(speech.pcm16k, tts.decode_mp3_16k(voice.store.get(speech.id)))


async def test_speak_refuse_vide_et_trop_long(voice, synth):
    for bad in ("", "  \n\t "):
        with pytest.raises(ValueError):
            await voice.speak(bad)
    with pytest.raises(ValueError):
        await voice.speak("a" * (tts.MAX_TEXT_CHARS + 1))
    assert synth.calls == []


async def test_speak_ids_distincts(voice):
    a, b = await voice.speak("Un."), await voice.speak("Un.")
    assert a.id != b.id


def test_store_conserve_10_minutes():
    now = [1000.0]
    store = tts.Mp3Store(ttl_s=600, clock=lambda: now[0])
    store.put("a", b"\xff\xfb1")
    now[0] += 599
    assert store.get("a") == b"\xff\xfb1"
    now[0] += 602
    assert store.get("a") is None
    store.put("b", b"x")
    assert len(store) == 1  # « a » purgé


def test_route_get_tts_mp3():
    app = create_app(Settings())
    app.state.tts_store.put("abc123", b"\xff\xfbDATA")
    client = TestClient(app)
    r = client.get("/tts/abc123.mp3")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"\xff\xfbDATA"
    assert client.get("/tts/inconnu.mp3").status_code == 404
    assert client.get("/tts/abc123.wav").status_code == 404
    assert client.get("/tts/..%2Fetc.mp3").status_code == 404


def test_from_settings():
    s = Settings()
    s.voice.tts_engine, s.voice.voice = "kokoro", "ff_siwis"
    v = tts.Voice.from_settings(s, base_url="http://x:8080")
    assert (v.engine, v.voice) == ("kokoro", "ff_siwis")
    s.voice.tts_engine = "inconnu"
    with pytest.raises(ValueError):
        tts.Voice.from_settings(s)


# --- serveur de compatibilité (port 8081, voice-daemon) ----------------------------------------


@pytest.fixture
def compat(voice):
    httpd = tts_compat.make_server(voice, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def post(url: str, body: str):
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    return urllib.request.urlopen(req, timeout=20)


SSML = '<s><break time="200ms" />Il fait &lt;beau&gt; aujourd\'hui.<break time="150ms" /></s>'


def test_compat_repond_un_mp3_comme_tts_server(compat, synth):
    r = post(compat + "/fr", SSML)
    data = r.read()
    assert r.status == 200
    assert r.headers["Content-Type"] == "audio/mpeg"
    assert int(r.headers["Content-Length"]) == len(data)
    assert is_mp3(data)
    assert synth.calls == ["Il fait <beau> aujourd'hui."]  # balises retirées, entités décodées


def test_compat_chemin_vide_et_cache(compat, synth):
    post(compat + "/", SSML).read()
    post(compat + "/en", SSML).read()
    assert len(synth.calls) == 1  # même texte : MP3 conservé


def test_compat_texte_vide_400(compat):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(compat + "/fr", '<s><break time="200ms" /></s>')
    assert e.value.code == 400


def test_compat_texte_trop_long_413(compat):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(compat + "/fr", "<s>" + "a" * (tts.MAX_TEXT_CHARS + 1) + "</s>")
    assert e.value.code == 413


def test_compat_echec_de_synthese_500(voice, compat):
    def boom(text):
        raise RuntimeError("moteur absent")

    voice._synth = boom
    with pytest.raises(urllib.error.HTTPError) as e:
        post(compat + "/fr", "<s>Bonjour</s>")
    assert e.value.code == 500


# --- vrais moteurs (ignorés sans modèles) ------------------------------------------------------

KOKORO_DIR = Path.home() / ".cache" / "lapinlibre" / "kokoro"
PIPER_DIR = Path.home() / ".cache" / "lapinou" / "piper"


@pytest.mark.skipif(not (KOKORO_DIR / "kokoro-v1.0.onnx").is_file(), reason="modèles Kokoro absents")
async def test_kokoro_reel():
    v = tts.Voice("kokoro", "ff_siwis")
    speech = await v.speak("Bonjour, je suis Lapinou.")
    assert is_mp3(v.store.get(speech.id))
    assert 1.0 < speech.duration_s < 4.0
    assert np.abs(speech.pcm16k).max() > 3000


@pytest.mark.skipif(not (PIPER_DIR / "fr_FR-siwis-medium.onnx").is_file(), reason="voix Piper absente")
async def test_piper_reel():
    pytest.importorskip("piper")
    v = tts.Voice("piper", "fr_FR-siwis-medium")
    speech = await v.speak("Bonjour, je suis Lapinou.")
    assert is_mp3(v.store.get(speech.id))
    assert 1.0 < speech.duration_s < 4.0
    assert np.abs(speech.pcm16k).max() > 3000
