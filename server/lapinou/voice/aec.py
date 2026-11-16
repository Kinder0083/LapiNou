# SPDX-License-Identifier: MIT
"""Annulation d'écho au serveur : retire du micro d'un lapin ce que le serveur lui a fait dire.

Le serveur connaît exactement le son joué (PCM 16 kHz de la synthèse, `reference(playback_id)`)
et le lapin situe le début et la fin de chaque lecture dans l'axe des échantillons de son
micro (marques `PlaybackMark`, `RabbitStream.playback_marks`). Pour chaque lecture :

1. la référence est posée sur `at_sample` de la marque de début, décalée du retard fin ;
2. le retard fin (tampon de sortie /dev/dsp + trajet acoustique, et l'erreur d'un fragment sur
   le tampon d'entrée : docs/lapinou/protocole-audio.md) est cherché par intercorrélation
   normalisée des 500 premières ms de la référence avec le micro, entre DELAY_RANGE_MS =
   -50 et +300 ms après la marque. Il est propre au lapin, donc gardé d'une lecture à l'autre :
   - première lecture : le micro est retenu (~850 ms) jusqu'à l'estimation ; si elle échoue
     (pic sous MIN_NCC), le retard 0 est pris sans être retenu comme fiable ;
   - lectures suivantes : traitées tout de suite avec le dernier retard fiable (0 s'il n'y en
     a pas encore) ; une nouvelle estimation est faite en arrière-plan et adoptée pour les
     lectures suivantes seulement si son pic est net (ADOPT_NCC) et l'écart d'au moins
     ADOPT_CHANGE ; sinon (phrase courte, utilisateur qui parle par-dessus) l'ancien reste ;
3. le filtre adaptatif SpeexDSP (trame 160, queue 300 ms) retire l'écho, puis le débruiteur
   SpeexDSP nettoie le résidu. Les deux états sont gardés d'une lecture à l'autre.

Hors lecture (et hors queue d'écho), la sortie est l'entrée, inchangée et sans retard.
Pendant une lecture, la sortie suit l'entrée d'au plus deux trames (trame entamée + trame
dans le débruiteur). Aucun échantillon n'est perdu ni ajouté : `next_sample` est l'index du
prochain échantillon rendu. Une trame renvoyée après reconnexion (first_sample déjà vu) est
rognée de ce qui a déjà été reçu ; un recul de plus de REWIND est un nouvel axe (lapinou-mic
redémarré) : les lectures qui commencent après la nouvelle position sont oubliées.

Pause et reprise : une nouvelle marque de début pour un playback_id qui a déjà joué reprend
la référence là où elle s'était arrêtée (karotz-player reprend au milieu du son) ; la partie
d'avant la pause garde sa queue d'écho. Un playback_id désigne donc un seul son.

Contrat d'appel :
- `process()` est appelé par un seul consommateur, dans le fil de la boucle de la passerelle
  audio (le dict `marks` est lu sans verrou) ;
- `reference(playback_id)` ne doit pas bloquer et rend le PCM complet du son (int16 16 kHz),
  ou None (son inconnu : pas annulé). Il est appelé une fois par playback_id ; le résultat est
  gardé tant que le son est dans `marks`.

Bibliothèque : speexaec (liaison Cython de libspeexdsp ; roues PyPI pour Python 3.13
seulement, en 3.12 sa compilation demande libspeexdsp-dev, python3-dev et gcc).
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
MIN_NCC = 0.3  # première estimation : pic d'intercorrélation normalisée minimal
ADOPT_NCC = 0.5  # estimations suivantes : pic net exigé (une parole sans écho donne ~0,3)
ADOPT_CHANGE = 32  # écart minimal (2 ms) pour changer de retard
REWIND = SAMPLE_RATE  # first_sample qui recule de plus : nouvel axe, pas une trame renvoyée
NOISE_SUPPRESS_DB = -15

ReferenceProvider = Callable[[str], "np.ndarray | None"]


def estimate_delay(
    ref: np.ndarray, mic: np.ndarray, max_lag: int, min_ncc: float = MIN_NCC
) -> int | None:
    """Le décalage k (0..max_lag) où mic[k:k+len(ref)] ressemble le plus à ref (intercorrélation
    normalisée, signe ignoré), ou None si le pic est sous min_ncc ou les signaux trop courts."""
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
    return best if ncc[best] >= min_ncc else None


@dataclass
class _Segment:
    """Une lecture continue (de la marque de début à la fin ou à la pause) sur l'axe du micro."""

    playback_id: str
    start: int
    end: int | None
    ref: np.ndarray  # int16, à partir de l'endroit où la lecture (re)prend
    delay: int | None  # pose de la référence : start + delay ; None = estimation attendue
    estimated: bool = False  # estimation du retard déjà faite pour ce segment

    def played(self) -> int:
        """Échantillons de la référence vraiment joués (moins si la lecture a été coupée)."""
        if self.end is None:
            return len(self.ref)
        return min(len(self.ref), max(0, self.end - self.start))


@dataclass
class _Sound:
    """Un playback_id vu dans les marques."""

    start: int  # marque de début courante
    end: int | None
    ref: np.ndarray | None = None
    fetched: bool = False
    offset: int = 0  # échantillons de la référence joués avant le segment courant
    segment: _Segment | None = None


class EchoCanceller:
    """L'annuleur d'écho d'un lapin. `reference(playback_id)` rend le PCM int16 16 kHz joué
    (ou None) ; `marks` est lu à chaque appel (en pratique `RabbitStream.playback_marks` :
    playback_id -> (début, fin ou None), une marque de début plaçant sa clé en dernier)."""

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
        # micro brut gardé pour les estimations : la fenêtre corrélée et un bloc de marge
        self._history_len = self._align + self._max_delay - self._min_delay + sample_rate
        self._sounds: dict[str, _Sound] = {}
        self._open: set[str] = set()  # sons sans marque de fin
        self._segments: list[_Segment] = []
        self._delay: int | None = None  # dernier retard fiable
        self._held = False  # la première lecture a déjà été retenue pour l'estimation
        self._pos: int | None = None  # index du premier échantillon de _buf
        self._buf = np.zeros(0, np.int16)  # reçu, pas encore traité
        self._history = np.zeros(0, np.int16)  # brut déjà traité, juste avant _pos
        self._in_ns = False  # une trame est dans le débruiteur (une trame de retard)

    @property
    def next_sample(self) -> int:
        """Index (axe du micro) du prochain échantillon que rendra `process`."""
        if self._pos is None:
            return 0
        return self._pos - (self._frame if self._in_ns else 0)

    @property
    def delay(self) -> int | None:
        """Dernier retard fin fiable (échantillons après la marque de début), ou None."""
        return self._delay

    def process(self, mic: np.ndarray, first_sample: int) -> np.ndarray:
        """Donne un bloc du micro (int16, 16 kHz) ; rend les échantillons traités disponibles,
        à partir de `next_sample` (même longueur que `mic` hors lecture)."""
        mic = np.asarray(mic)
        if mic.dtype != np.int16 or mic.ndim != 1:
            raise ValueError("micro attendu en int16 mono")
        out = []
        if self._pos is not None:
            expected = self._pos + len(self._buf)
            if expected - REWIND <= first_sample < expected:
                mic = mic[expected - first_sample :]  # trame renvoyée : la partie déjà reçue
                first_sample = expected
            elif first_sample != expected:
                rewind = first_sample < expected
                out.append(self._run(final=True, sync=not rewind))  # trou : nouveau départ
                if rewind:
                    self._forget_after(first_sample)
                self._history = np.zeros(0, np.int16)
                self._pos = None
        if self._pos is None:
            self._pos = first_sample
        self._buf = np.concatenate((self._buf, mic))
        out.append(self._run(final=False))
        return np.concatenate(out)

    # -- lectures ------------------------------------------------------------------------

    def _sync(self) -> None:
        """Suit les marques : les nouvelles sont en fin de dict, les fins attendues sont lues
        une à une ; pas de parcours complet à chaque appel."""
        assert self._pos is not None
        marks = self._marks
        new = []
        for playback_id in reversed(marks):
            sound = self._sounds.get(playback_id)
            if sound is not None and sound.start == marks[playback_id][0]:
                break
            new.append(playback_id)
        for playback_id in reversed(new):
            self._on_mark(playback_id, *marks[playback_id])
        for playback_id in list(self._open):
            sound = self._sounds[playback_id]
            value = marks.get(playback_id)
            if value is None:
                self._open.discard(playback_id)  # oubliée par la passerelle
            elif value[0] == sound.start and value[1] is not None:
                self._set_end(playback_id, sound, value[1])
        if len(self._sounds) > 2 * len(marks) + 64:
            self._sounds = {k: v for k, v in self._sounds.items() if k in marks}
            self._open &= self._sounds.keys()
        horizon = self._pos - self._history_len
        self._segments = [s for s in self._segments if self._window_end(s) > horizon]

    def _on_mark(self, playback_id: str, start: int, end: int | None) -> None:
        assert self._pos is not None
        sound = self._sounds.get(playback_id)
        if sound is None:
            sound = self._sounds[playback_id] = _Sound(start, end)
        else:  # reprise après pause (ou début après une fin orpheline)
            if sound.end is None or sound.end > start:
                self._set_end(playback_id, sound, start)  # fin non vue : pause supposée nulle
            assert sound.end is not None
            sound.offset += max(0, sound.end - sound.start)
            sound.start, sound.end, sound.segment = start, end, None
        if end is None:
            self._open.add(playback_id)
        elif end <= start:
            self._open.discard(playback_id)
            return  # fin orpheline ou lecture vide
        else:
            self._open.discard(playback_id)
            if end + self._max_delay + self._tail < self._pos - self._history_len:
                return  # déjà finie avant notre historique
        if not sound.fetched:
            ref = self._reference(playback_id)
            sound.fetched = True
            sound.ref = None if ref is None or len(ref) == 0 else np.asarray(ref, dtype=np.int16)
        if sound.ref is None or sound.offset >= len(sound.ref):
            return
        if self._delay is None and not self._held:
            delay, self._held = None, True  # première lecture : retenue jusqu'à l'estimation
        else:
            delay = self._placed(self._delay or 0)
        sound.segment = _Segment(playback_id, start, end, sound.ref[sound.offset :], delay)
        self._segments.append(sound.segment)

    def _set_end(self, playback_id: str, sound: _Sound, end: int) -> None:
        sound.end = end
        if sound.segment is not None:
            sound.segment.end = end
        self._open.discard(playback_id)

    def _forget_after(self, position: int) -> None:
        """Nouvel axe : les lectures qui commencent après `position` venaient de l'ancien."""
        self._segments = [s for s in self._segments if s.start <= position]
        for playback_id in [k for k, s in self._sounds.items() if s.start > position]:
            del self._sounds[playback_id]
            self._open.discard(playback_id)

    def _placed(self, estimated: int) -> int:
        return max(self._min_delay, estimated - DELAY_MARGIN)

    def _window_end(self, seg: _Segment) -> int:
        delay = seg.delay if seg.delay is not None else self._max_delay
        return seg.start + delay + seg.played() + self._tail

    # -- retard fin ----------------------------------------------------------------------

    def _span(self, seg: _Segment) -> tuple[int, int, int]:
        length = min(self._align, seg.played())
        return length, seg.start + self._min_delay, seg.start + self._max_delay + length

    def _lag(self, seg: _Segment, min_ncc: float) -> int | None:
        """Retard estimé pour seg (relatif à sa marque), sur le micro disponible."""
        assert self._pos is not None
        length, lo, hi = self._span(seg)
        raw_start = self._pos - len(self._history)
        if lo < raw_start or length < self._frame:
            return None  # marque trop tardive ou lecture trop courte
        raw = np.concatenate((self._history, self._buf))
        mic = raw[lo - raw_start : min(hi, self._pos + len(self._buf)) - raw_start]
        lag = estimate_delay(seg.ref[:length], mic, self._max_delay - self._min_delay, min_ncc)
        return None if lag is None else self._min_delay + lag

    def _estimate_held(self, seg: _Segment, final: bool) -> bool:
        """Première lecture : fixe seg.delay si le micro nécessaire est là (ou si final)."""
        assert self._pos is not None
        if self._pos + len(self._buf) < self._span(seg)[2] and not final:
            return False
        seg.estimated = True
        lag = self._lag(seg, MIN_NCC)
        if lag is None:
            log.debug("lecture %s : retard fin non trouvé, 0 par défaut", seg.playback_id)
        else:
            self._delay = lag
            log.debug("lecture %s : retard fin %d ms", seg.playback_id, lag * 1000 // self._rate)
        seg.delay = self._placed(lag or 0)
        return True

    def _estimate_background(self) -> None:
        """Lectures suivantes : nouvelle estimation, adoptée seulement si nette et différente."""
        assert self._pos is not None
        have = self._pos + len(self._buf)
        for seg in self._segments:
            if seg.estimated or seg.delay is None or have < self._span(seg)[2]:
                continue
            seg.estimated = True
            lag = self._lag(seg, ADOPT_NCC)
            if lag is not None and (self._delay is None or abs(lag - self._delay) >= ADOPT_CHANGE):
                ms = lag * 1000 // self._rate
                log.debug("lecture %s : retard fin %d ms adopté", seg.playback_id, ms)
                self._delay = lag

    def _ready(self, upto: int, final: bool) -> bool:
        """Estime ce qui doit l'être avant de traiter jusqu'à `upto` ; faux s'il faut attendre."""
        for seg in self._segments:
            if seg.delay is not None or upto <= seg.start + self._min_delay:
                continue
            if not self._estimate_held(seg, final):
                return False
        return True

    # -- traitement ----------------------------------------------------------------------

    def _active(self, n0: int, n1: int) -> bool:
        """Une lecture (ou sa queue d'écho) touche-t-elle [n0, n1) ?"""
        for seg in self._segments:
            if seg.delay is not None:
                a = seg.start + seg.delay
                if n1 > a and n0 < a + seg.played() + self._tail:
                    return True
        return False

    def _far(self, n0: int) -> np.ndarray:
        """La référence (somme des lectures) pour la trame du micro qui commence à n0."""
        far = np.zeros(self._frame, np.int32)
        for seg in self._segments:
            if seg.delay is None:
                continue
            lo = n0 - seg.start - seg.delay
            a, b = max(lo, 0), min(lo + self._frame, seg.played())
            if a < b:
                far[a - lo : b - lo] += seg.ref[a:b]
        return np.clip(far, -32768, 32767).astype(np.int16)

    def _run(self, final: bool, sync: bool = True) -> np.ndarray:
        assert self._pos is not None
        if sync:
            self._sync()
        self._estimate_background()
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
                    n1 = self._pos + end
                    if not self._ready(n1 + nxt, final) or self._active(n1, n1 + nxt):
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
