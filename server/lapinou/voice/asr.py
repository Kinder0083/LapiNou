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
    def __init__(
        self,
        models: Path | None = None,
        *,
        threads: int = 2,
        recogniser: vad.Recognising | None = None,
    ):
        """`models` : dossier des modèles (défaut : celui de LapinLibre). `recogniser` : un
        `Recogniser` déjà chargé à partager (sinon chargé ici ; RuntimeError si absent)."""
        self.recogniser = recogniser or vad.load_recogniser(models, threads)

    async def transcribe_utterance(
        self,
        chunks: AsyncIterator[np.ndarray],
        max_s: float = 15,
        silence_ms: int = 700,
        idle_timeout_s: float = 7.0,
        no_speech_s: float = NO_SPEECH_S,
    ) -> str | None:
        """Texte de la première parole du flux (blocs int16 mono 16 kHz), ou None si personne
        n'a parlé : `no_speech_s` (5 s) sans voix (comptées depuis la dernière activité vocale,
        un clic bref n'y change rien), ou aucun bloc pendant `idle_timeout_s` (flux bloqué, lapin
        déconnecté), ou transcription vide. La lecture s'arrête à la fin de la parole.

        `chunks` est fermé (`aclose`) à toute sortie. Annuler la tâche est permis : l'appel en
        cours dans un fil (VAD ou reconnaissance, bornés) finit seul, sans retenir l'appelant."""
        try:
            return await self._run(
                chunks.__aiter__(), max_s, silence_ms, idle_timeout_s, no_speech_s
            )
        finally:
            aclose = getattr(chunks, "aclose", None)
            if aclose is not None:
                await aclose()

    async def _run(
        self, chunks, max_s: float, silence_ms: int, idle_timeout_s: float, no_speech_s: float
    ) -> str | None:
        segmenter = vad.SileroSegmenter(self.recogniser, max_s, silence_ms / 1000)
        fed = 0
        last_voice = 0  # dernier échantillon où de la parole était détectée
        segments: list[tuple[int, np.ndarray]] = []
        while True:
            try:
                chunk = await asyncio.wait_for(chunks.__anext__(), idle_timeout_s)
            except StopAsyncIteration:  # flux fini : la parole en cours compte
                segments = await asyncio.to_thread(segmenter.flush)
                break
            except TimeoutError:
                return None
            pcm = np.asarray(chunk)
            if pcm.dtype != np.int16 or pcm.ndim != 1:
                raise ValueError(f"PCM int16 mono (1-D) attendu, reçu {pcm.dtype} de forme {pcm.shape}")
            fed += len(pcm)
            segments = await asyncio.to_thread(segmenter.feed, pcm)
            if segments:
                break
            if segmenter.speaking:
                last_voice = fed
            elif fed - last_voice >= no_speech_s * vad.SAMPLE_RATE:
                return None
        if not segments:
            return None
        text = await asyncio.to_thread(vad.transcribe_padded, self.recogniser, segments[0][1])
        return text or None
