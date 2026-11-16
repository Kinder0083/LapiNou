# SPDX-License-Identifier: MIT
"""Détection de « Lapinou » par transcription (méthode principale du mot d'activation, R23).

Le flux 16 kHz d'un lapin passe dans le détecteur de parole Silero ; chaque segment de
parole est transcrit une fois par Parakeet (sherpa-onnx, via `Recogniser` de LapinLibre,
tools/app-server/asr_server.py, importé et non copié : R4). Le mot d'activation est
reconnu seulement au début de la transcription, éventuellement après un mot court
(« hé », « dis », « ok »), avec une tolérance d'une faute de frappe sur le nom
(lapino, lapinoux, la pinou, lapi nou) ; jamais au milieu d'une phrase.

Le coût est celui de la transcription des seuls segments de parole (voir
docs/lapinou/mot-activation.md pour le facteur temps réel mesuré). `feed()` transcrit
dans l'appel : l'appeler depuis un fil (asyncio.to_thread), un détecteur par lapin.

Les modèles (~640 Mo) se téléchargent avec `python -m lapinou.voice.models fetch`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Protocol

import numpy as np

from lapinou.voice import vad
from lapinou.voice.vad import (  # noqa: F401  (ré-exportés : modèles, VAD partagés avec asr.py)
    PAD_S,
    REPO,
    SAMPLE_RATE,
    VAD_THRESHOLD,
    SileroSegmenter,
    Transcriber,
    asr_server,
    load_recogniser,
    models_present,
)

log = logging.getLogger(__name__)

# Mots courts qui peuvent précéder le nom (« hé Lapinou »).
# « et », « dit » : Parakeet entend « Et la Pinot » pour « Hé Lapinou », « dit » pour « dis ».
LEAD_WORDS = frozenset({"he", "hey", "eh", "et", "ah", "oh", "ho", "dis", "dit", "ok", "okay", "salut", "coucou"})
MAX_NAME_WORDS = 3  # « la pi nou » : le nom peut être coupé en 3 mots
CONTINUE_GAP_S = 0.2  # écart max entre deux morceaux d'une même parole coupée
CUT_FULL = 0.9  # un morceau de VAD aussi long (part de max_segment_s) a été coupé
PREFILTER_LEAD_S = 0.5  # le pré-filtre peut se déclencher un peu avant le début du segment
_PUNCT = " \t\r\n.,;:!?…-–—'\"«»()"


@dataclass(frozen=True)
class WakeHit:
    """Un « Lapinou » entendu : instantané immuable rendu par `feed()` (le lire depuis un autre
    fil est sûr). Une parole de plus de `max_segment_s` est transcrite par morceaux : chaque
    morceau qui allonge la commande produit un nouvel instantané du même `at_sample`
    (`text` et `command` plus longs, `complete=False`) ; le dernier a `complete=True`, y compris
    quand le silence seul a clos la parole. Une parole courte donne un seul instantané complet."""

    at_sample: int  # début du segment de parole, en échantillons depuis la création du détecteur
    text: str  # transcription complète de la parole jusqu'ici
    command: str  # ce qui suit le nom ; vide s'il n'y a que « Lapinou »
    complete: bool = True  # plus aucun morceau n'est attendu


class Segmenter(Protocol):
    def feed(self, pcm: np.ndarray) -> list[tuple[int, np.ndarray]]:
        """Ajoute des échantillons int16 ; rend les segments de parole terminés
        (premier échantillon, float32 -1..1)."""

    def reset(self) -> None: ...


# ---- correspondance du texte ----


def _fold(word: str) -> str:
    """Minuscules, sans accents."""
    decomposed = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _distance(a: str, b: str) -> int:
    """Distance d'édition (insertions, suppressions, remplacements) entre deux mots."""
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[len(b)]


def _tolerance(name: str) -> int:
    return len(name) // 5  # 1 faute pour « lapinou » (7 lettres), aucune sous 5 lettres


def match_wake_word(text: str, names: Sequence[str]) -> tuple[str, str] | None:
    """(nom reconnu, commande) si `text` commence par un des noms, sinon None.

    Un nom en plusieurs mots (« la pinou ») n'est accepté que s'ils ne sont séparés par aucune
    ponctuation (« Lapin, où es-tu ? » ne passe pas), et « lapin ou/où » est refusé.

    Le texte est comparé sans accents ni ponctuation, mais la commande garde la casse et
    les accents d'origine.
    """
    words = [(m.start(), m.end(), _fold(m.group())) for m in re.finditer(r"[^\W_]+", text)]
    folded = [w[2] for w in words]
    if not words:
        return None
    first = 1 if len(words) > 1 and words[0][2] in LEAD_WORDS else 0
    best: tuple[int, int, str, int] | None = None  # (fautes, mots, nom, fin du nom)
    for name in names:
        wanted = _fold(name).replace(" ", "")
        for count in range(1, MAX_NAME_WORDS + 1):
            if first + count > len(words):
                break
            group = words[first : first + count]
            if count > 1 and (
                any(re.search(r"[.,;:!?…]", text[a[1] : b[0]]) for a, b in zip(group, group[1:]))
                or (folded[first] == "lapin" and folded[first + 1] == "ou")  # « lapin où… »
            ):
                continue  # une ponctuation sépare les mots, ou c'est la question « lapin ou… »
            heard = "".join(w[2] for w in group)
            faults = _distance(heard, wanted)
            if faults <= _tolerance(wanted) and (best is None or (faults, count) < best[:2]):
                best = (faults, count, name, words[first + count - 1][1])
    if best is None:
        return None
    return best[2], text[best[3] :].strip(_PUNCT)


class AsrWakeDetector:
    """Détecteur de « Lapinou » pour un flux (un par lapin).

    `transcriber` : objet avec `transcribe(samples)` (le `Recogniser` de LapinLibre, qui
    fournit aussi le VAD). `max_segment_s` borne la durée transcrite en une fois : un
    segment plus long est découpé : seul le premier morceau est comparé au nom, les suivants
    complètent `command` par de nouveaux instantanés (voir WakeHit.complete). `prefilter` (WakeWord, méthode « both ») : un segment n'est
    transcrit que si le pré-filtre s'est déclenché pendant ce segment.
    """

    def __init__(
        self,
        transcriber: Transcriber,
        names: Sequence[str] = ("lapinou",),
        max_segment_s: float = 6.0,
        *,
        silence_s: float = 0.7,
        segmenter: Segmenter | None = None,
        prefilter=None,
    ) -> None:
        self.transcriber = transcriber
        self.names = list(names)
        self.max_segment_s = max_segment_s
        self._close_after = int((max_segment_s + silence_s + CONTINUE_GAP_S) * SAMPLE_RATE)
        self.prefilter = prefilter
        self.segmenter = segmenter or SileroSegmenter(transcriber, max_segment_s, silence_s)
        self.fed = 0  # échantillons reçus depuis la création
        self._origin = 0  # valeur de `fed` au dernier reset (le VAD repart de zéro)
        self._fires: deque[int] = deque(maxlen=64)
        self.transcribed_s = 0.0  # durée audio transcrite (mesure du coût)
        self._prev_end = 0  # fin du dernier morceau transcrit ou écarté
        self._prev_cut = False  # ce morceau a été coupé : la parole peut continuer
        self._open: WakeHit | None = None  # dernier instantané d'une détection non terminée

    def reset(self) -> list[WakeHit]:
        """Oublie la parole en cours (après une conversation) ; la numérotation continue.
        Rend l'instantané complet d'une détection restée ouverte, s'il y en a une."""
        self.segmenter.reset()
        self._origin = self.fed
        self._fires.clear()
        self._prev_cut = False
        if self.prefilter is not None:
            self.prefilter.reset()
        return self._close()

    def feed(self, pcm: np.ndarray) -> list[WakeHit]:
        """Ajoute des échantillons int16 16 kHz ; rend les instantanés de « Lapinou » entendus
        (voir WakeHit : une nouvelle détection, une commande qui s'allonge, la clôture)."""
        pcm = np.asarray(pcm)
        if pcm.dtype != np.int16 or pcm.ndim != 1:
            raise ValueError(f"PCM int16 mono (1-D) attendu, reçu {pcm.dtype} de forme {pcm.shape}")
        self.fed += len(pcm)
        if self.prefilter is not None and self.prefilter.feed(pcm) is not None:
            self._fires.append(self.fed)
        hits: list[WakeHit] = []
        for start, samples in self.segmenter.feed(pcm):
            start += self._origin
            cut_size = int(self.max_segment_s * SAMPLE_RATE)
            # Un segment contigu à un morceau coupé continue la même phrase.
            continues = self._prev_cut and start - self._prev_end <= CONTINUE_GAP_S * SAMPLE_RATE
            if not continues:
                hits += self._close()
            for offset in range(0, len(samples), cut_size):
                piece = samples[offset : offset + cut_size]
                last = offset + cut_size >= len(samples)
                # Coupé : pas le dernier morceau, ou un morceau plein (la parole a pu continuer).
                cut = not last or len(piece) >= CUT_FULL * cut_size
                if continues:
                    hits += self._continue(piece, cut)
                else:
                    hit = self._examine(start, piece)
                    if hit is not None:
                        hit = replace(hit, complete=not cut)
                        self._open = hit if cut else None
                        hits.append(hit)
                self._prev_end, self._prev_cut = start + offset + len(piece), cut
                continues = True
        # Plus de parole depuis assez longtemps : la suite d'un morceau coupé n'arrivera pas.
        # (Elle arriverait au plus tard max_segment_s + silence_s après le morceau.)
        if self._open is not None and self.fed - self._prev_end >= self._close_after:
            hits += self._close()
        return hits

    def _close(self) -> list[WakeHit]:
        """Clôture la détection ouverte : son instantané complet, ou rien."""
        if self._open is None:
            return []
        closed, self._open = replace(self._open, complete=True), None
        self._prev_cut = False
        return [closed]

    def _continue(self, piece: np.ndarray, cut: bool) -> list[WakeHit]:
        """Morceau qui poursuit une phrase : jamais comparé au nom ; il n'est transcrit que si
        une détection est ouverte, dont il complète la commande (nouvel instantané)."""
        if self._open is None:
            return []
        text = self._transcribe(piece)
        grown = replace(
            self._open,
            text=f"{self._open.text} {text}".strip(),
            command=f"{self._open.command} {text.strip(_PUNCT)}".strip(),
            complete=not cut,
        )
        self._open = grown if cut else None
        return [grown]

    def _transcribe(self, samples: np.ndarray) -> str:
        self.transcribed_s += len(samples) / SAMPLE_RATE
        return vad.transcribe_padded(self.transcriber, samples)

    def _examine(self, start: int, samples: np.ndarray) -> WakeHit | None:
        if self.prefilter is not None:
            end = start + len(samples)
            lead = int(PREFILTER_LEAD_S * SAMPLE_RATE)
            while self._fires and self._fires[0] < start - lead:
                self._fires.popleft()
            if not any(start - lead <= fire <= end + SAMPLE_RATE for fire in self._fires):
                return None
        text = self._transcribe(samples)
        found = match_wake_word(text, self.names)
        if found is None:
            return None
        return WakeHit(at_sample=start, text=text, command=found[1])


# ---- configuration ----


def build_detector(voice, recogniser: Transcriber) -> AsrWakeDetector:
    """Le détecteur de la configuration `voice` (VoiceConfig) pour un nouveau flux.

    « asr » : transcription seule. « both » : transcription gardée par le classifieur
    openWakeWord si `voice.wake_model` est renseigné (sinon « asr », avec un avertissement).
    « oww » n'utilise pas ce détecteur : voir `lapinou.voice.wakeword.load_wakeword`.
    """
    if voice.wake_method == "oww":
        raise ValueError("wake_method = « oww » : utiliser lapinou.voice.wakeword.load_wakeword")
    prefilter = None
    if voice.wake_method == "both":
        if voice.wake_model:
            from lapinou.voice import wakeword

            prefilter = wakeword.load_wakeword(voice)
        else:
            log.warning("wake_method = « both » sans voice.wake_model : transcription seule")
    return AsrWakeDetector(recogniser, silence_s=voice.vad_silence_ms / 1000, prefilter=prefilter)
