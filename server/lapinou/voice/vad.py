# SPDX-License-Identifier: MIT
"""Briques partagées de la voix : Silero VAD, chargement de Parakeet (R4 : `Recogniser` de
LapinLibre, tools/app-server/asr_server.py, importé et non copié), présence des modèles.

Utilisé par `wake_asr` (mot d'activation) et `asr` (transcription d'une requête).
Les modèles (~640 Mo) se téléchargent avec `python -m lapinou.voice.models fetch`.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import numpy as np

REPO = Path(__file__).resolve().parents[3]
_APP_SERVER_DIR = REPO / "tools" / "app-server"
if str(_APP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_SERVER_DIR))

import asr_server  # noqa: E402  (LapinLibre : Recogniser, MODELS, check_models)

SAMPLE_RATE = 16000
# Seuil de Silero : le micro du lapin est faible (valeur de LapinLibre, asr_server.VAD_THRESHOLD).
VAD_THRESHOLD = asr_server.VAD_THRESHOLD
MIN_SPEECH_S = 0.25
PAD_S = 0.3  # silence ajouté de chaque côté d'un segment : Parakeet entend mieux un mot isolé


class Recognising(Protocol):
    def transcribe(self, samples: np.ndarray) -> str:
        """Texte de `samples` (float32, -1..1, 16 kHz)."""


class SileroSegmenter:
    """Segments de parole : Silero VAD de sherpa-onnx, réglé comme celui de LapinLibre."""

    def __init__(self, recogniser: asr_server.Recogniser, max_segment_s: float, silence_s: float):
        sherpa = recogniser.sherpa
        config = sherpa.VadModelConfig()
        config.silero_vad.model = str(Path(recogniser.models) / "silero_vad.onnx")
        config.silero_vad.threshold = VAD_THRESHOLD
        config.silero_vad.min_silence_duration = silence_s
        config.silero_vad.min_speech_duration = MIN_SPEECH_S
        config.silero_vad.max_speech_duration = max_segment_s
        config.sample_rate = SAMPLE_RATE
        self._vad = sherpa.VoiceActivityDetector(config, buffer_size_in_seconds=max_segment_s + 30)
        self._window = config.silero_vad.window_size
        self._pending = np.zeros(0, np.float32)

    @property
    def speaking(self) -> bool:
        """De la parole est en cours (détectée dans la dernière fenêtre)."""
        return bool(self._vad.is_speech_detected())

    def feed(self, pcm: np.ndarray) -> list[tuple[int, np.ndarray]]:
        self._pending = np.concatenate((self._pending, pcm.astype(np.float32) / 32768.0))
        segments = []
        while len(self._pending) >= self._window:
            self._vad.accept_waveform(self._pending[: self._window])
            self._pending = self._pending[self._window :]
            segments += self._drain()
        return segments

    def flush(self) -> list[tuple[int, np.ndarray]]:
        """Fin du flux : rend la parole en cours comme un segment terminé."""
        self._vad.flush()
        return self._drain()

    def _drain(self) -> list[tuple[int, np.ndarray]]:
        segments = []
        while not self._vad.empty():
            front = self._vad.front
            segments.append((int(front.start), np.asarray(front.samples, np.float32)))
            self._vad.pop()
        return segments

    def reset(self) -> None:
        self._vad.reset()
        self._pending = np.zeros(0, np.float32)


def transcribe_padded(transcriber: Recognising, samples: np.ndarray) -> str:
    """Transcription d'un segment entouré de silence (PAD_S de chaque côté)."""
    pad = np.zeros(int(PAD_S * SAMPLE_RATE), np.float32)
    return transcriber.transcribe(np.concatenate((pad, samples, pad))).strip()


@lru_cache(maxsize=4)
def _models_present(models: Path) -> bool:
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    return all((models / name).is_file() for name in asr_server.MODEL_SHA256)


def models_present(models: Path | None = None) -> bool:
    """sherpa-onnx s'importe et les fichiers des modèles sont là. Ne calcule pas les SHA-256
    (640 Mo) : `load_recogniser()` les vérifie au chargement. Résultat gardé en mémoire."""
    return _models_present(Path(models or asr_server.MODELS))


def load_recogniser(models: Path | None = None, threads: int = 2) -> asr_server.Recogniser:
    """Charge Parakeet + Silero (une fois, partagé par tous les lapins). RuntimeError si
    sherpa-onnx ou un modèle manque (python -m lapinou.voice.models fetch)."""
    return asr_server.Recogniser(Path(models or asr_server.MODELS), threads)
