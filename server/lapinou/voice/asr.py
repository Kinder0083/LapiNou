# SPDX-License-Identifier: MIT
"""Transcription d'une requête après un mot d'activation, un appui long ou la fenêtre de suite.

Le flux 16 kHz int16 passe dans le Silero VAD partagé (`vad.SileroSegmenter`) ; la première
parole, close par `silence_ms` de silence ou coupée à `max_s`, est transcrite une fois par
Parakeet (`Recogniser` de LapinLibre, R4, partagé et verrouillé : plusieurs lapins peuvent
transcrire en même temps). Aucune transcription n'est journalisée (vie privée).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from lapinou.voice import vad

NO_SPEECH_S = 5.0  # sans parole au bout de ce délai, la requête est abandonnée


class Transcriber:
    def __init__(self, models: Path | None = None, *, threads: int = 2, recogniser: vad.Transcriber | None = None):
        """`models` : dossier des modèles (défaut : celui de LapinLibre). `recogniser` : un
        `Recogniser` déjà chargé à partager (sinon chargé ici ; RuntimeError si absent)."""
        self.recogniser = recogniser or vad.load_recogniser(models, threads)

    async def transcribe_utterance(
        self, chunks: AsyncIterator[np.ndarray], max_s: float = 15, silence_ms: int = 700
    ) -> str | None:
        """Texte de la première parole du flux (blocs int16 mono 16 kHz), ou None si personne
        n'a parlé dans les 5 premières secondes (ou si la parole n'a rien donné). La lecture
        s'arrête à la fin de la parole : `chunks` n'est pas consommé davantage."""
        segmenter = vad.SileroSegmenter(self.recogniser, max_s, silence_ms / 1000)
        fed = 0
        segments: list[tuple[int, np.ndarray]] = []
        spoke = False
        async for chunk in chunks:
            pcm = np.asarray(chunk)
            if pcm.dtype != np.int16 or pcm.ndim != 1:
                raise ValueError(f"PCM int16 mono (1-D) attendu, reçu {pcm.dtype} de forme {pcm.shape}")
            fed += len(pcm)
            segments = await asyncio.to_thread(segmenter.feed, pcm)
            spoke = spoke or segmenter.speaking
            if segments:
                break
            if not spoke and fed >= NO_SPEECH_S * vad.SAMPLE_RATE:
                return None
        else:  # flux fini : la parole en cours compte
            segments = await asyncio.to_thread(segmenter.flush)
        if not segments:
            return None
        samples = np.concatenate([s for _, s in segments])
        text = await asyncio.to_thread(vad.transcribe_padded, self.recogniser, samples)
        return text or None
