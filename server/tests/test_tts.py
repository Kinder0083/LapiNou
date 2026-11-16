# SPDX-License-Identifier: MIT
"""Synthèse vocale : découpage en phrases, MP3 + PCM 16 kHz cohérents, stockage 10 min, serveur
de compatibilité (protocole de voice-daemon). Moteur factice pour la logique ; les vrais moteurs
(Kokoro, Piper) sont ignorés tant que leurs modèles manquent."""

from __future__ import annotations

import hashlib
import io
import socket
import sys
import textwrap
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


# --- store partagé, ffmpeg, Kokoro vide, en-têtes -----------------------------------------------


async def test_attach_une_seule_boutique_get_sert_le_mp3(voice):
    app = create_app(Settings())
    voice.attach(app)
    speech = await voice.speak("Bonjour.")
    path = speech.mp3_url.removeprefix("http://10.0.0.5:8080")
    r = TestClient(app).get(path)
    assert r.status_code == 200 and r.content == voice.store.get(speech.id)
    assert is_mp3(r.content)


def test_ffmpeg_absent_refuse_clairement(voice, monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        voice.load()


def test_kokoro_sortie_vide_est_une_erreur_explicite(monkeypatch, tmp_path):
    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeKokoro:
        def get_voices(self):
            return ["ff_siwis"]

        def create(self, *a, **k):
            return np.zeros(0, dtype=np.float32), 24000

    class FakeEngine:
        def __init__(self, models):
            self.kokoro, self.lock = FakeKokoro(), Lock()

    monkeypatch.setattr(tts.tts_server, "KokoroEngine", FakeEngine)
    v = tts.Voice("kokoro", "ff_siwis")
    with pytest.raises(RuntimeError, match="rien"):
        v.render_mp3("Bonjour.")


def raw_post(port: int, headers: str, body: bytes = b"") -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(f"POST /fr HTTP/1.1\r\nHost: x\r\n{headers}\r\n".encode() + body)
        return int(s.recv(200).split()[1])


@pytest.mark.parametrize("length", ["abc", "-5", "1e3"])
def test_compat_content_length_invalide_400(compat, length):
    port = int(compat.rsplit(":", 1)[1])
    assert raw_post(port, f"Content-Length: {length}\r\nConnection: close\r\n") == 400


# --- Piper : processus séparé (GPL-3 : le serveur MIT n'importe rien de piper) -----------------

FAKE_WORKER = textwrap.dedent(
    r"""
    import json, os, struct, sys
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    out.write(b'{"ready": true}\n')
    for line in sys.stdin.buffer:
        text = json.loads(line)["text"]
        if text == "boom":
            out.write(b'{"error": "boom"}\n')
            continue
        if text == "die":
            os._exit(3)
        pcm = struct.pack("<h", 1000) * 2205 * (1 + len(text) % 3)
        out.write(json.dumps({"rate": 22050, "bytes": len(pcm)}).encode() + b"\n" + pcm)
    """
)


@pytest.fixture
def piper_proc(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(FAKE_WORKER)
    proc = tts.PiperProcess(Path(sys.executable), tmp_path / "m.onnx", worker=worker)
    yield proc
    proc.close()


def test_piper_process_synthetise(piper_proc):
    pcm, rate = piper_proc.synthesize("a")
    assert rate == 22050 and len(pcm) == 2205 * 2 * 2 and pcm[:2] == bytes([0xE8, 0x03])


def test_piper_process_erreur_du_worker(piper_proc):
    with pytest.raises(RuntimeError, match="boom"):
        piper_proc.synthesize("boom")
    assert piper_proc.synthesize("a")[1] == 22050  # le worker vit toujours


def test_piper_process_redemarre_si_mort(piper_proc):
    piper_proc.synthesize("a")
    first = piper_proc.proc.pid
    piper_proc.proc.kill()
    piper_proc.proc.wait()
    assert piper_proc.synthesize("a")[1] == 22050  # redémarré
    assert piper_proc.proc.pid != first
    with pytest.raises(RuntimeError):  # meurt à chaque fois : une seule reprise
        piper_proc.synthesize("die")


def test_le_serveur_n_importe_pas_piper():
    code = "import sys, lapinou.voice.tts, lapinou.web.tts_compat, lapinou.app; "            "sys.exit(any(m == 'piper' or m.startswith('piper.') for m in sys.modules))"
    import subprocess

    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
    assert "import piper" not in Path(tts.__file__).read_text().replace("`", "")
    assert "from piper" not in Path(tts.__file__).read_text()


def test_voix_piper_verifiee_par_sha256(tmp_path, monkeypatch):
    (tmp_path / "v.onnx").write_bytes(b"modele")
    (tmp_path / "v.onnx.json").write_bytes(b"{}")
    py = tmp_path / "py"
    py.write_text("")
    with pytest.raises(RuntimeError, match="inconnue"):
        tts.Voice("piper", "v", models=tmp_path, piper_python=py).load()
    pins = {"v.onnx": hashlib.sha256(b"modele").hexdigest(), "v.onnx.json": "0" * 64}
    monkeypatch.setitem(tts.PIPER_VOICES, "v", pins)
    with pytest.raises(RuntimeError, match="SHA-256"):
        tts.Voice("piper", "v", models=tmp_path, piper_python=py).load()


def test_piper_sans_venv_indique_la_commande(tmp_path, monkeypatch):
    (tmp_path / "v.onnx").write_bytes(b"m")
    (tmp_path / "v.onnx.json").write_bytes(b"{}")
    monkeypatch.setitem(
        tts.PIPER_VOICES,
        "v",
        {"v.onnx": hashlib.sha256(b"m").hexdigest(), "v.onnx.json": hashlib.sha256(b"{}").hexdigest()},
    )
    with pytest.raises(RuntimeError, match="piper-setup"):
        tts.Voice("piper", "v", models=tmp_path, piper_python=tmp_path / "absent").load()


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


@pytest.mark.skipif(
    not ((PIPER_DIR / "fr_FR-siwis-medium.onnx").is_file() and tts.PIPER_PYTHON.is_file()),
    reason="voix Piper ou venv Piper absents (python -m lapinou.voice.models piper-setup)",
)
async def test_piper_reel_par_sous_processus():
    v = tts.Voice("piper", "fr_FR-siwis-medium")
    try:
        speech = await v.speak("Bonjour, je suis Lapinou.")
        assert is_mp3(v.store.get(speech.id))
        assert 1.0 < speech.duration_s < 4.0
        assert np.abs(speech.pcm16k).max() > 3000
    finally:
        v.close()
