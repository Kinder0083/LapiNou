# SPDX-License-Identifier: MIT
"""Synthèse vocale : phrase -> MP3 servi au lapin + PCM 16 kHz pour l'annulateur d'écho.

Moteurs : Kokoro (`KokoroEngine` de LapinLibre, R4, importé de tools/app-server) ou Piper.
Piper (`piper-tts`, GPL-3.0-or-later) n'est jamais importé par le serveur MIT (R30) : il tourne
dans un processus séparé et durable (`piper_worker.py`, exécuté par le Python d'un venv à part,
`PIPER_PYTHON`, créé par `python -m lapinou.voice.models piper-setup`), texte en entrée et PCM en
sortie sur stdin/stdout ; il est relancé s'il meurt. Les fichiers de la voix sont vérifiés par
SHA-256 au chargement (R31).
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
import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response

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
log = logging.getLogger(__name__)
ENGINES = ("kokoro", "piper")
PIPER_MODELS = Path.home() / ".cache" / "lapinou" / "piper"
PIPER_VENV = Path.home() / ".local" / "share" / "lapinou" / "piper-venv"
PIPER_PYTHON = PIPER_VENV / "bin" / "python"
PIPER_WORKER = Path(__file__).with_name("piper_worker.py")
PIPER_TIMEOUT_S = 60
PIPER_READY_S = 120  # chargement de la voix par le worker (« prêt »)
DEFAULT_PIPER_VOICE = "fr_FR-siwis-medium"
# Voix Piper utilisables : révision de rhasspy/piper-voices et SHA-256 de leurs fichiers (R31).
# fr_FR-siwis-medium : CC BY 4.0 (upmc est CC BY-SA : non retenue).
PIPER_REVISION = "e9ab671f2a0e50a701b34361136ba3e6a3a9eb0c"
PIPER_VOICES: dict[str, dict[str, str]] = {
    "fr_FR-siwis-medium": {
        "fr_FR-siwis-medium.onnx": "b1d946fd68d0347155fa59bce0a018924abbef3c1561181db45ca463d83963ef",
        "fr_FR-siwis-medium.onnx.json": "df814aa3dfedd00510a07144442cc0a220ff21e43d03c3b078444fc0be68fc37",
    },
}
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


def _need_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg est requis (décodage du PCM de référence) : apt install ffmpeg "
            "(paquet à prévoir dans l'installateur et l'image Docker)"
        )


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


# Voix claire : coupe le grondement sous 180 Hz, creuse la zone « caverneuse » (250 Hz), avive la
# clarté (3,2 kHz). Le filtre exact de l'essai écouté sur le lapin (docs/lapinou/mesures.md).
CLARITY_FILTER = "highpass=f=180,equalizer=f=250:t=q:w=1:g=-4,equalizer=f=3200:t=q:w=1.2:g=3"


def clarify(wav: bytes) -> bytes:
    """WAV 16 bits mono -> le même, filtré (`CLARITY_FILTER`), à la même fréquence.
    Si ffmpeg manque ou échoue : avertissement et WAV d'origine (Lapinou ne doit pas se taire)."""
    try:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg absent")
        with wave.open(io.BytesIO(wav)) as w:
            rate = w.getframerate()
        r = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", "-", "-af", CLARITY_FILTER, "-ac", "1",
             "-ar", str(rate), "-c:a", "pcm_s16le", "-f", "wav", "-"],
            input=wav, capture_output=True, check=True, timeout=30,
        )  # fmt: skip
        return r.stdout
    except (OSError, RuntimeError, subprocess.SubprocessError, wave.Error, EOFError) as e:
        log.warning("voix claire impossible (%s) : voix d'origine", e)
        return wav


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
        if samples.size == 0:
            raise RuntimeError("Kokoro n'a rien synthétisé")
        samples = samples / max(1e-6, float(np.abs(samples).max())) * 0.89
        return tts_server.wav_of((samples * 32767).astype("<i2").tobytes(), rate)

    return synth


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class PiperProcess:
    """Le worker Piper : un processus durable, relancé s'il meurt (une reprise par requête).
    Protocole : une ligne JSON `{"text": ...}` en entrée ; en sortie `{"rate": r, "bytes": n}`
    puis n octets de PCM int16 mono, ou `{"error": ...}` ; `{"ready": true}` au démarrage."""

    def __init__(self, python: Path, model: Path, worker: Path = PIPER_WORKER,
                 ready_s: float = PIPER_READY_S):
        self.python, self.model, self.worker = python, model, worker
        self.ready_s = ready_s
        self.proc: subprocess.Popen | None = None

    def _start(self) -> None:
        self.close()
        self.proc = subprocess.Popen(
            [str(self.python), "-I", str(self.worker), str(self.model)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )  # fmt: skip
        # Un worker qui ne dit jamais « prêt » (bloqué au chargement) est tué : EOFError.
        timer = threading.Timer(self.ready_s, self.proc.kill)
        timer.start()
        try:
            ready = self._line().get("ready")
        finally:
            timer.cancel()
        if ready is not True:
            raise OSError("le worker Piper n'est pas prêt")

    def _line(self) -> dict:
        assert self.proc and self.proc.stdout
        line = self.proc.stdout.readline()
        if not line:
            raise EOFError("le worker Piper s'est arrêté")
        return json.loads(line)

    def _request(self, text: str) -> tuple[bytes, int]:
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"text": text}).encode() + b"\n")
        self.proc.stdin.flush()
        head = self._line()
        if "error" in head:
            # Seulement le type d'erreur : jamais le texte de la phrase (vie privée).
            kind = re.match(r"[A-Za-z_][A-Za-z0-9_]{0,40}", str(head["error"]))
            raise RuntimeError(f"Piper : {kind.group(0) if kind else 'erreur'}")
        size = int(head["bytes"])
        pcm = self.proc.stdout.read(size)
        if len(pcm) != size:
            raise EOFError("PCM tronqué")
        return pcm, int(head["rate"])

    def synthesize(self, text: str) -> tuple[bytes, int]:
        """(PCM int16 mono, fréquence). RuntimeError si Piper échoue ou ne répond pas."""
        for attempt in (0, 1):
            timer = None
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                timer = threading.Timer(PIPER_TIMEOUT_S, self.proc.kill)
                timer.start()
                return self._request(text)
            except (OSError, EOFError, ValueError, KeyError) as e:
                self.close()  # mort, bloqué ou protocole rompu : on repart d'un worker neuf
                if attempt:
                    raise RuntimeError(f"Piper ne répond pas : {e}") from e
            finally:
                if timer:
                    timer.cancel()
        raise AssertionError("inatteignable")

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _piper_synth(voice: str, models: Path | None, python: Path | None) -> Callable[[str], bytes]:
    pins = PIPER_VOICES.get(voice)
    if pins is None:
        raise RuntimeError(f"voix Piper inconnue : {voice!r} (connues : {', '.join(PIPER_VOICES)})")
    directory = models or PIPER_MODELS
    for name, want in pins.items():
        path = directory / name
        if not path.is_file():
            raise RuntimeError(f"voix Piper absente : {path} (python -m lapinou.voice.models fetch)")
        if _sha256(path) != want:
            raise RuntimeError(f"{path} : pas le fichier attendu (SHA-256)")
    python = python or PIPER_PYTHON
    if not python.is_file():
        raise RuntimeError(
            f"Python de Piper absent : {python} (python -m lapinou.voice.models piper-setup)"
        )
    process = PiperProcess(python, directory / f"{voice}.onnx")

    def synth(text: str) -> bytes:
        pcm, rate = process.synthesize(text)
        return tts_server.wav_of(pcm, rate)

    synth.close = process.close  # type: ignore[attr-defined]
    return synth



_PIPER_NAME = re.compile(r"[a-z]{2}_[A-Z]{2}-[\w]+-(x_low|low|medium|high)")


class Voice:
    """Un moteur et une voix. `synth` (tests) remplace le moteur : texte -> WAV 16 bits mono.
    `base_url` : début de l'URL que le lapin utilise pour joindre ce serveur (port 8080)."""

    def __init__(
        self,
        engine: Engine,
        voice: str,
        *,
        models: Path | None = None,
        piper_python: Path | None = None,
        base_url: str = "",
        store: Mp3Store | None = None,
        synth: Callable[[str], bytes] | None = None,
        clarity: bool = False,
    ):
        if engine not in ENGINES:
            raise ValueError(f"moteur de synthèse inconnu : {engine!r} (kokoro ou piper)")
        self.engine, self.voice, self.models = engine, voice, models
        self.piper_python = piper_python
        self.base_url = base_url.rstrip("/")
        self.store = store if store is not None else Mp3Store()
        self._synth = synth
        self.clarity = clarity  # voix claire (`clarify`) avant l'encodage MP3
        self._lock = threading.Lock()  # un seul calcul à la fois (les moteurs ne sont pas sûrs en parallèle)
        self._cache: OrderedDict[str, bytes] = OrderedDict()  # texte -> MP3 (serveur de compatibilité)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> Voice:
        kwargs.setdefault("clarity", settings.voice.clarity)
        return cls(settings.voice.tts_engine, settings.voice.voice, **kwargs)  # type: ignore[arg-type]

    def load(self) -> None:
        """Charge le moteur (long : quelques secondes) ; sinon fait à la première phrase.
        RuntimeError si les modèles ou ffmpeg manquent, ValueError pour une voix Kokoro inconnue."""
        _need_ffmpeg()
        with self._lock:
            self._load()

    def attach(self, app: FastAPI) -> None:
        """Fait de la boutique de cette voix celle de `GET /tts/<id>.mp3` de l'application."""
        app.state.tts_store = self.store

    def close(self) -> None:
        """Arrête le worker Piper s'il y en a un."""
        close = getattr(self._synth, "close", None)
        if close:
            close()

    def _load(self) -> Callable[[str], bytes]:
        if self._synth is None:
            if self.engine == "kokoro":
                try:
                    self._synth = _kokoro_synth(self.voice, self.models)
                    return self._synth
                except RuntimeError as e:
                    # Kokoro n'est pas installé par l'installateur (R31 : Piper) : le lapin ne
                    # doit pas rester muet pour un réglage, Piper prend le relais.
                    voice = self.voice if _PIPER_NAME.fullmatch(self.voice) else DEFAULT_PIPER_VOICE
                    log.warning("voix Kokoro indisponible (%s) : Piper %s à la place", e, voice)
                    self.engine, self.voice = "piper", voice
            if self.engine == "piper":
                self._synth = _piper_synth(self.voice, self.models, self.piper_python)
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
                if self.clarity:
                    wav = clarify(wav)
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
        _need_ffmpeg()
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
