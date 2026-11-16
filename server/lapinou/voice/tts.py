# SPDX-License-Identifier: MIT
"""Synthèse vocale : phrase -> MP3 servi au lapin + PCM 16 kHz pour l'annulateur d'écho.

Moteurs : Kokoro (`KokoroEngine` de LapinLibre, R4, importé de tools/app-server) ou Piper
(paquet `piper-tts`, GPL-3.0-or-later : extra optionnel `piper`, importé à la demande, R24).
L'encodage MP3 est `tts_server.to_mp3` (lameenc, 64 kbit/s) ; la fréquence est celle du moteur
(Kokoro 24 kHz, Piper 22,05 kHz), sans rééchantillonnage : le Karotz (ARM9 sans FPU) lit le
MP3 tel que LapinLibre le sert déjà.

Alignement du PCM (Tâche 17 : référence de l'annulateur d'écho) : `Speech.pcm16k` est le
**décodage du MP3 servi** (ffmpeg, mono, 16 kHz, int16), pas la synthèse d'origine. Le retard
de l'encodeur (~1105 échantillons, lameenc n'écrit pas d'en-tête LAME qui le déclarerait) et le
bourrage de la dernière trame y sont donc, comme dans ce que lit karotz-player ; la durée
`duration_s` en est celle.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
from fastapi import APIRouter, HTTPException, Request, Response

if TYPE_CHECKING:
    from lapinou.config import Settings

_REPO = Path(__file__).resolve().parents[3]
_APP_SERVER_DIR = _REPO / "tools" / "app-server"
if str(_APP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_SERVER_DIR))

import tts_server  # noqa: E402  (LapinLibre : KokoroEngine, to_mp3, wav_of)

PCM_RATE = 16000
MAX_TEXT_CHARS = 1000  # par requête (phrase ou notification) ; au-delà : refusé
MP3_TTL_S = 600  # les MP3 servis sont conservés 10 minutes
MAX_MP3 = 200  # garde-fou mémoire si les phrases s'enchaînent plus vite que le TTL

Engine = Literal["kokoro", "piper"]
ENGINES = ("kokoro", "piper")
PIPER_MODELS = Path.home() / ".cache" / "lapinou" / "piper"
# Code de langue de phonémisation de Kokoro selon le préfixe de la voix (ff_siwis -> fr-fr).
_KOKORO_LANG = {"ff": "fr-fr", "ef": "es", "if": "it", "pf": "pt-br", "af": "en-us", "bf": "en-gb"}

_SENTENCE = re.compile(
    r"""\S.*?(?:[.!?…]+(?:\s*[»”"')\]])*(?=\s|$)|$)""",
    re.DOTALL,
)


def split_sentences(text: str) -> list[str]:
    """Phrases à synthétiser séparément (la première part dès qu'elle est prête). Coupe après
    . ! ? … (et un guillemet fermant), si une espace ou la fin suit (« 3.5 » reste entier), et
    à chaque saut de ligne."""
    out: list[str] = []
    for line in text.splitlines():
        out.extend(m.group().strip() for m in _SENTENCE.finditer(line) if m.group().strip())
    return out


@dataclass(frozen=True)
class Speech:
    id: str
    mp3_url: str
    pcm16k: np.ndarray  # int16, 16 kHz, mono : le MP3 servi décodé
    duration_s: float


class Mp3Store:
    """MP3 en mémoire, par identifiant, conservés `ttl_s` secondes."""

    def __init__(
        self,
        ttl_s: float = MP3_TTL_S,
        max_items: int = MAX_MP3,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_s, self.max_items, self._clock = ttl_s, max_items, clock
        self._items: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._lock = threading.Lock()

    def _purge(self) -> None:
        limit = self._clock() - self.ttl_s
        while self._items:
            key, (stamp, _) = next(iter(self._items.items()))
            if stamp > limit and len(self._items) <= self.max_items:
                break
            del self._items[key]

    def put(self, key: str, mp3: bytes) -> None:
        with self._lock:
            self._items[key] = (self._clock(), mp3)
            self._purge()

    def get(self, key: str) -> bytes | None:
        with self._lock:
            self._purge()
            item = self._items.get(key)
            return item[1] if item else None

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._items)


def decode_mp3_16k(mp3: bytes) -> np.ndarray:
    """MP3 -> PCM int16 mono 16 kHz (ffmpeg, qui garde le retard de l'encodeur sans en-tête LAME)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", "-", "-f", "s16le", "-ac", "1",
             "-ar", str(PCM_RATE), "-"],
            input=mp3, capture_output=True, check=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"décodage du MP3 impossible (ffmpeg requis) : {e}") from e
    return np.frombuffer(r.stdout, dtype="<i2").astype(np.int16)


def _kokoro_synth(voice: str, models: Path | None) -> Callable[[str], bytes]:
    engine = tts_server.KokoroEngine(models or tts_server.KOKORO_MODELS)  # RuntimeError si absent
    if voice not in engine.kokoro.get_voices():
        raise ValueError(f"voix Kokoro inconnue : {voice!r}")
    lang = _KOKORO_LANG.get(voice[:2], "fr-fr")

    def synth(text: str) -> bytes:
        # La voix est réglable : on appelle Kokoro directement, comme KokoroEngine.__call__
        # (qui fixe la voix par langue), avec la même normalisation (crête à -1 dB).
        with engine.lock:
            samples, rate = engine.kokoro.create(text, voice=voice, speed=1.0, lang=lang)
        samples = np.asarray(samples, dtype=np.float32)
        samples = samples / max(1e-6, float(np.abs(samples).max())) * 0.89
        return tts_server.wav_of((samples * 32767).astype("<i2").tobytes(), rate)

    return synth


def _piper_synth(voice: str, models: Path | None) -> Callable[[str], bytes]:
    try:
        from piper import PiperVoice
    except ImportError:
        raise RuntimeError("piper-tts n'est pas installé (uv sync --extra piper)") from None
    path = (models or PIPER_MODELS) / f"{voice}.onnx"
    if not path.is_file():
        raise RuntimeError(f"voix Piper absente : {path}")
    piper = PiperVoice.load(path)

    def synth(text: str) -> bytes:
        chunks = list(piper.synthesize(text))
        if not chunks:
            raise RuntimeError("Piper n'a rien synthétisé")
        pcm = np.concatenate([c.audio_int16_array for c in chunks]).astype("<i2")
        return tts_server.wav_of(pcm.tobytes(), chunks[0].sample_rate)

    return synth


class Voice:
    """Un moteur et une voix. `synth` (tests) remplace le moteur : texte -> WAV 16 bits mono.
    `base_url` : début de l'URL que le lapin utilise pour joindre ce serveur (port 8080)."""

    def __init__(
        self,
        engine: Engine,
        voice: str,
        *,
        models: Path | None = None,
        base_url: str = "",
        store: Mp3Store | None = None,
        synth: Callable[[str], bytes] | None = None,
    ):
        if engine not in ENGINES:
            raise ValueError(f"moteur de synthèse inconnu : {engine!r} (kokoro ou piper)")
        self.engine, self.voice, self.models = engine, voice, models
        self.base_url = base_url.rstrip("/")
        self.store = store if store is not None else Mp3Store()
        self._synth = synth
        self._lock = threading.Lock()  # un seul calcul à la fois (les moteurs ne sont pas sûrs en parallèle)
        self._cache: OrderedDict[str, bytes] = OrderedDict()  # texte -> MP3 (serveur de compatibilité)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> Voice:
        return cls(settings.voice.tts_engine, settings.voice.voice, **kwargs)  # type: ignore[arg-type]

    def load(self) -> None:
        """Charge le moteur (long : quelques secondes) ; sinon fait à la première phrase.
        RuntimeError si les modèles manquent, ValueError pour une voix inconnue."""
        with self._lock:
            self._load()

    def _load(self) -> Callable[[str], bytes]:
        if self._synth is None:
            make = _kokoro_synth if self.engine == "kokoro" else _piper_synth
            self._synth = make(self.voice, self.models)
        return self._synth

    @staticmethod
    def check_text(text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("texte vide")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"texte trop long ({len(text)} > {MAX_TEXT_CHARS} caractères)")
        return text

    def render_mp3(self, text: str) -> bytes:
        """Texte -> MP3 (bloquant). ValueError (texte), RuntimeError (moteur)."""
        text = self.check_text(text)
        with self._lock:
            synth = self._load()
            try:
                wav = synth(text)
                return tts_server.to_mp3(wav)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"encodage MP3 impossible : {e}") from e

    def cached_mp3(self, text: str) -> bytes:
        """Comme `render_mp3`, avec les 32 derniers textes conservés (notifications répétées)."""
        text = self.check_text(text)
        with self._lock:
            hit = self._cache.get(text)
            if hit is not None:
                self._cache.move_to_end(text)
                return hit
        mp3 = self.render_mp3(text)
        with self._lock:
            self._cache[text] = mp3
            while len(self._cache) > 32:
                self._cache.popitem(last=False)
        return mp3

    def speak_sync(self, text: str) -> Speech:
        mp3 = self.render_mp3(text)
        pcm = decode_mp3_16k(mp3)
        speech_id = uuid.uuid4().hex
        self.store.put(speech_id, mp3)
        return Speech(speech_id, f"{self.base_url}/tts/{speech_id}.mp3", pcm, len(pcm) / PCM_RATE)

    async def speak(self, text: str) -> Speech:
        """Une phrase (voir `split_sentences`) -> MP3 prêt à jouer et PCM de référence."""
        return await asyncio.to_thread(self.speak_sync, text)


router = APIRouter()


@router.get("/tts/{name}")
def get_tts(name: str, request: Request) -> Response:
    """MP3 d'une phrase (`/tts/<id>.mp3`), tant qu'il est conservé."""
    key, dot, ext = name.rpartition(".")
    mp3 = request.app.state.tts_store.get(key) if dot and ext == "mp3" else None
    if mp3 is None:
        raise HTTPException(status_code=404)
    return Response(mp3, media_type="audio/mpeg")
