# SPDX-License-Identifier: MIT
"""Annulation d'écho au serveur : retire du micro d'un lapin ce que le serveur lui a fait dire.

Le serveur connaît exactement le son joué (PCM 16 kHz de la synthèse, `reference(playback_id)`)
et le lapin situe le début et la fin de chaque lecture dans l'axe des échantillons de son
micro (marques `PlaybackMark`, `RabbitStream.playback_marks`). Pour chaque lecture :

1. la référence est posée sur `at_sample` de la marque de début ;
2. le retard fin est cherché par intercorrélation normalisée des 500 premières ms de la
   référence avec le micro, entre DELAY_RANGE_MS = -50 et +300 ms après la marque. Il couvre
   le tampon de sortie de /dev/dsp et le trajet acoustique (le haut-parleur joue quelques
   dizaines de ms après la marque) et l'erreur d'un fragment sur le tampon d'entrée
   (docs/lapinou/protocole-audio.md) ; un pic trop faible laisse le retard par défaut (0) ;
3. le filtre adaptatif SpeexDSP (trame 160, queue 300 ms) retire l'écho, puis le débruiteur
   SpeexDSP nettoie le résidu.

Hors lecture (et hors queue d'écho), la sortie est l'entrée, inchangée et sans retard.
Pendant une lecture, la sortie suit l'entrée avec au plus une trame et demie de retard
(trame entamée + trame dans le débruiteur) ; au début d'une lecture, le micro est retenu
jusqu'à l'estimation du retard (environ 850 ms). Aucun échantillon n'est perdu ni ajouté :
`next_sample` est l'index du prochain échantillon rendu.

Limites : une lecture reprise après une pause (nouvelle marque de début du même
playback_id) repart du début de sa référence ; les sons sans référence (MPD, sons du lapin)
ne sont pas annulés. Bibliothèque : speexaec (liaison Cython de libspeexdsp ; roues PyPI pour
Python 3.13 seulement, en 3.12 sa compilation demande libspeexdsp-dev, python3-dev et gcc).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
import speexaec

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
DELAY_RANGE_MS = (-50, 300)  # retard fin cherché, par rapport à la marque de début
ALIGN_MS = 500  # durée de référence corrélée
DELAY_MARGIN = 160  # la référence est posée un peu avant l'écho estimé (filtre causal)
MIN_NCC = 0.15  # pic d'intercorrélation normalisée en dessous duquel l'estimation est rejetée
NOISE_SUPPRESS_DB = -15

ReferenceProvider = Callable[[str], "np.ndarray | None"]


def estimate_delay(ref: np.ndarray, mic: np.ndarray, max_lag: int) -> int | None:
    """Le décalage k (0..max_lag) où mic[k:k+len(ref)] ressemble le plus à ref (intercorrélation
    normalisée, signe ignoré), ou None si le pic est trop faible ou les signaux trop courts."""
    r = ref.astype(np.float64)
    m = mic.astype(np.float64)
    lags = min(max_lag, len(m) - len(r))
    ref_norm = float(np.sqrt(np.dot(r, r)))
    if lags < 0 or len(r) == 0 or ref_norm < 1.0:
        return None
    nfft = 1 << (len(m) + len(r)).bit_length()
    corr = np.fft.irfft(np.fft.rfft(m, nfft) * np.conj(np.fft.rfft(r, nfft)), nfft)[: lags + 1]
    energy = np.concatenate(([0.0], np.cumsum(m * m)))
    window = energy[len(r) : len(r) + lags + 1] - energy[: lags + 1]
    ncc = np.abs(corr) / (ref_norm * np.sqrt(np.maximum(window, 1.0)))
    best = int(np.argmax(ncc))
    return best if ncc[best] >= MIN_NCC else None


@dataclass
class _Playback:
    playback_id: str
    start: int  # marque de début, dans l'axe du micro
    end: int | None  # marque de fin
    ref: np.ndarray  # int16
    delay: int | None = None  # pose de la référence : start + delay

    def played(self) -> int:
        """Échantillons de la référence vraiment joués (moins si la lecture a été coupée)."""
        if self.end is None:
            return len(self.ref)
        return min(len(self.ref), max(0, self.end - self.start))

    def align_len(self, align: int) -> int:
        return min(align, self.played())


class EchoCanceller:
    """L'annuleur d'écho d'un lapin. `reference(playback_id)` rend le PCM int16 16 kHz joué
    (ou None : son inconnu, pas annulé) ; `marks` est lu à chaque appel (en pratique
    `RabbitStream.playback_marks` : playback_id -> (début, fin ou None))."""

    def __init__(
        self,
        reference: ReferenceProvider,
        marks: Mapping[str, tuple[int, int | None]],
        frame: int = 160,
        tail_ms: int = 300,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._reference = reference
        self._marks = marks
        self._frame = frame
        self._rate = sample_rate
        self._tail = sample_rate * tail_ms // 1000
        self._min_delay = sample_rate * DELAY_RANGE_MS[0] // 1000
        self._max_delay = sample_rate * DELAY_RANGE_MS[1] // 1000
        self._align = sample_rate * ALIGN_MS // 1000
        self._aec = speexaec.EchoCanceller(frame, self._tail, sample_rate)
        self._ns = speexaec.AudioPreprocessor(frame, sample_rate)
        self._ns.denoise = True
        self._ns.noise_suppress = NOISE_SUPPRESS_DB
        self._zeros = np.zeros(frame, np.int16)
        self._history_len = self._align + self._max_delay - self._min_delay + frame
        self._playbacks: dict[tuple[str, int], _Playback | None] = {}
        self._pos: int | None = None  # index du premier échantillon de _buf
        self._buf = np.zeros(0, np.int16)  # reçu, pas encore traité
        self._history = np.zeros(0, np.int16)  # brut déjà traité, juste avant _pos
        self._in_ns = False  # une trame est dans le débruiteur (une trame de retard)
        self.delays: dict[str, int] = {}  # retard fin estimé par playback_id (échantillons)

    @property
    def next_sample(self) -> int:
        """Index (axe du micro) du prochain échantillon que rendra `process`."""
        if self._pos is None:
            return 0
        return self._pos - (self._frame if self._in_ns else 0)

    def process(self, mic: np.ndarray, first_sample: int) -> np.ndarray:
        """Donne un bloc du micro (int16, 16 kHz) ; rend les échantillons traités disponibles,
        à partir de `next_sample` (même longueur que `mic` hors lecture)."""
        mic = np.asarray(mic)
        if mic.dtype != np.int16 or mic.ndim != 1:
            raise ValueError("micro attendu en int16 mono")
        out = []
        if self._pos is not None and first_sample != self._pos + len(self._buf):
            out.append(self._run(final=True))  # trou ou recouvrement : nouveau départ
            self._history = np.zeros(0, np.int16)
            self._pos = None
        if self._pos is None:
            self._pos = first_sample
        self._buf = np.concatenate((self._buf, mic))
        out.append(self._run(final=False))
        return np.concatenate(out)

    # -- lectures ------------------------------------------------------------------------

    def _sync_playbacks(self) -> None:
        assert self._pos is not None
        known: dict[tuple[str, int], _Playback | None] = {}
        for playback_id, (start, end) in list(self._marks.items()):
            if end is not None and end <= start:
                continue  # fin orpheline ou lecture vide
            key = (playback_id, start)
            pb = self._playbacks.get(key, ...)
            if pb is ...:
                pb = self._new_playback(playback_id, start, end)
            elif pb is not None:
                pb.end = end
                if self._window_end(pb) <= self._pos - self._history_len:
                    pb = None  # terminée depuis longtemps : la référence est libérée
            known[key] = pb
        self._playbacks = known
        self.delays = {pid: d for pid, d in self.delays.items() if pid in self._marks}

    def _new_playback(self, playback_id: str, start: int, end: int | None) -> _Playback | None:
        assert self._pos is not None
        if end is not None and end + self._max_delay + self._tail < self._pos - self._history_len:
            return None  # déjà finie avant notre historique
        ref = self._reference(playback_id)
        if ref is None or len(ref) == 0:
            return None
        return _Playback(playback_id, start, end, np.asarray(ref, dtype=np.int16))

    def _window_end(self, pb: _Playback) -> int:
        delay = pb.delay if pb.delay is not None else self._max_delay
        return pb.start + delay + pb.played() + self._tail

    def _needs_delay(self, pb: _Playback, upto: int) -> bool:
        """Vrai si [.., upto) touche la zone où la référence de pb peut déjà s'appliquer."""
        return pb.delay is None and upto > pb.start + self._min_delay

    def _estimate(self, pb: _Playback, final: bool) -> bool:
        """Fixe pb.delay si le micro nécessaire est là (ou si final) ; faux s'il faut attendre."""
        assert self._pos is not None
        length = pb.align_len(self._align)
        lo, hi = pb.start + self._min_delay, pb.start + self._max_delay + length
        have = self._pos + len(self._buf)
        if have < hi and not final:
            return False
        raw = np.concatenate((self._history, self._buf))
        raw_start = self._pos - len(self._history)
        lag = None
        if lo >= raw_start and length >= self._frame:
            mic = raw[lo - raw_start : min(hi, have) - raw_start]
            lag = estimate_delay(pb.ref[:length], mic, self._max_delay - self._min_delay)
        if lag is None:
            log.debug("lecture %s : retard fin non trouvé, valeur par défaut", pb.playback_id)
            estimated = 0
        else:
            estimated = self._min_delay + lag
            ms = estimated * 1000 // self._rate
            log.debug("lecture %s : retard fin %d ms", pb.playback_id, ms)
        self.delays[pb.playback_id] = estimated
        pb.delay = max(self._min_delay, estimated - DELAY_MARGIN)
        return True

    def _ready(self, upto: int, final: bool) -> bool:
        """Estime ce qui doit l'être avant de traiter jusqu'à `upto` ; faux s'il faut attendre."""
        for pb in self._playbacks.values():
            if pb is not None and self._needs_delay(pb, upto) and not self._estimate(pb, final):
                return False
        return True

    def _active(self, n0: int, n1: int) -> bool:
        """Une lecture (ou sa queue d'écho) touche-t-elle [n0, n1) ?"""
        for pb in self._playbacks.values():
            if pb is not None and pb.delay is not None:
                a = pb.start + pb.delay
                if n1 > a and n0 < a + pb.played() + self._tail:
                    return True
        return False

    def _far(self, n0: int) -> np.ndarray:
        """La référence (somme des lectures) pour la trame du micro qui commence à n0."""
        far = np.zeros(self._frame, np.int32)
        for pb in self._playbacks.values():
            if pb is None or pb.delay is None:
                continue
            lo = n0 - pb.start - pb.delay
            a, b = max(lo, 0), min(lo + self._frame, pb.played())
            if a < b:
                far[a - lo : b - lo] += pb.ref[a:b]
        return np.clip(far, -32768, 32767).astype(np.int16)

    # -- traitement ----------------------------------------------------------------------

    def _run(self, final: bool) -> np.ndarray:
        assert self._pos is not None
        self._sync_playbacks()
        frame, out, i = self._frame, [], 0
        while i < len(self._buf):
            n0 = self._pos + i
            size = min(frame, len(self._buf) - i)
            if not self._ready(n0 + size, final):
                break
            if self._active(n0, n0 + size):
                if size < frame and not final:
                    break  # trame entamée : attendre la suite
                raw = self._buf[i : i + size]
                if size < frame:  # fin forcée : pas de trame entière, rendu brut
                    self._flush_ns(out)
                    out.append(raw)
                else:
                    cleaned, _ = self._ns.process(self._aec.process(raw, self._far(n0)))
                    if self._in_ns:
                        out.append(cleaned)
                    self._in_ns = True
            else:
                self._flush_ns(out)
                # hors lecture : tout le reste inactif passe tel quel, d'un coup
                end = i + size
                while end < len(self._buf):
                    nxt = min(frame, len(self._buf) - end)
                    if not self._ready(self._pos + end + nxt, final) or self._active(
                        self._pos + end, self._pos + end + nxt
                    ):
                        break
                    end += nxt
                out.append(self._buf[i:end])
                size = end - i
            i += size
        if final:
            self._flush_ns(out)
        self._history = np.concatenate((self._history, self._buf[:i]))[-self._history_len :]
        self._buf = self._buf[i:]
        self._pos += i
        return np.concatenate(out) if out else np.zeros(0, np.int16)

    def _flush_ns(self, out: list[np.ndarray]) -> None:
        """Sort la trame retenue par le débruiteur (en lui donnant du silence)."""
        if self._in_ns:
            cleaned, _ = self._ns.process(self._zeros)
            out.append(cleaned)
            self._in_ns = False
