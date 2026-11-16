# SPDX-License-Identifier: MIT
"""Annulation d'écho (lapinou.voice.aec) sur signaux synthétiques.

L'écho est la référence (phrases asr_*.wav de la tâche 16) retardée et atténuée, ajoutée
au micro simulé ; la parole proche est une autre phrase, au niveau bas du micro du lapin
(~ -35 dBFS), plus faible que l'écho comme sur le lapin. Le flux est donné par blocs de
2048 échantillons, les marques de lecture arrivant avant l'audio qu'elles situent, comme
dans la passerelle audio.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from lapinou.voice import aec
from test_wakeword import DATA, GATEWAY_BLOCK, SR, read_wav

FAR_DBFS = -20.0  # référence (ce que le lapin joue)
ECHO_GAIN_DB = -6.0
NEAR_DBFS = -35.0  # parole de l'utilisateur au micro du lapin
NOISE_DBFS = -65.0
START = SR  # la lecture commence 1 s après le début du flux
TAIL_S = 1.5  # micro encore donné après la fin de la lecture


def _scale(x: np.ndarray, dbfs: float) -> np.ndarray:
    x = x.astype(np.float64)
    return x / np.sqrt(np.mean(x**2)) * 32768.0 * 10 ** (dbfs / 20)


def _db(x: np.ndarray) -> float:
    return 10 * np.log10(np.mean(np.asarray(x, np.float64) ** 2) + 1e-12)


@pytest.fixture(scope="module")
def far() -> np.ndarray:
    phrases = ("dis_lumiere_2", "lapin_2", "milieu_2", "seul_2")
    pcm = np.concatenate([read_wav(DATA / f"asr_{p}.wav") for p in phrases])
    return np.round(_scale(pcm, FAR_DBFS)).astype(np.int16)


@pytest.fixture(scope="module")
def near() -> np.ndarray:
    return _scale(read_wav(DATA / "asr_allume_salon_1.wav"), NEAR_DBFS)


def _mic(
    far: np.ndarray, delay_ms: float, near: np.ndarray | None = None, near_at: int = 0
) -> np.ndarray:
    total = START + len(far) + int(TAIL_S * SR)
    mic = np.random.default_rng(1).normal(0.0, 32768.0 * 10 ** (NOISE_DBFS / 20), total)
    d = START + int(delay_ms * SR / 1000)
    mic[d : d + len(far)] += far.astype(np.float64)[: total - d] * 10 ** (ECHO_GAIN_DB / 20)
    if near is not None:
        mic[near_at : near_at + len(near)] += near
    return np.clip(np.round(mic), -32768, 32767).astype(np.int16)


def _stream(
    canceller: aec.EchoCanceller, marks: dict, mic: np.ndarray, events, block: int = GATEWAY_BLOCK
) -> np.ndarray:
    """Donne `mic` par blocs ; `events` = [(at_sample, playback_id, start)] appliqués à `marks`
    avant le bloc qui contient at_sample. Rend la sortie concaténée."""
    out, pending = [], sorted(events)
    for pos in range(0, len(mic), block):
        while pending and pending[0][0] < pos + block:
            at, pid, start = pending.pop(0)
            if start:
                marks[pid] = (at, None)
            else:
                marks[pid] = (marks.get(pid, (at, None))[0], at)
        out.append(canceller.process(mic[pos : pos + block], pos))
    return np.concatenate(out)


def _playback(far: np.ndarray, mic: np.ndarray, **kwargs):
    marks: dict[str, tuple[int, int | None]] = {}
    refs = {"tts-1": far}
    canceller = aec.EchoCanceller(refs.get, marks, **kwargs)
    events = [(START, "tts-1", True), (START + len(far), "tts-1", False)]
    return canceller, _stream(canceller, marks, mic, events)


def test_no_playback_output_identical():
    marks: dict[str, tuple[int, int | None]] = {}
    canceller = aec.EchoCanceller(lambda pid: pytest.fail("aucune lecture"), marks)
    rng = np.random.default_rng(0)
    pos = 0
    for size in (2048, 2048, 1000, 37, 2048, 160, 5000):
        mic = rng.integers(-3000, 3000, size, dtype=np.int16)
        out = canceller.process(mic, pos)
        assert out.dtype == np.int16
        np.testing.assert_array_equal(out, mic)
        pos += size
    assert canceller.next_sample == pos


def test_echo_attenuated_20_db(far):
    """Écho retardé de 120 ms après la marque, atténué de 6 dB : -20 dB au moins une fois
    le filtre convergé (après 1,5 s de lecture)."""
    mic = _mic(far, 120)
    canceller, out = _playback(far, mic)
    assert len(out) == canceller.next_sample
    assert canceller.delays["tts-1"] == pytest.approx(int(0.120 * SR), abs=16)
    s = slice(START + int(1.5 * SR), START + len(far))
    attenuation = _db(mic[s]) - _db(out[s])
    assert attenuation >= 20.0, f"écho atténué de {attenuation:.1f} dB seulement"


def test_near_speech_kept_during_echo(far, near):
    """Parole proche 9 dB sous l'écho, pendant la lecture : conservée (forme et niveau)."""
    at = START + 5 * SR
    mic = _mic(far, 120, near, at)
    _, out = _playback(far, mic)
    s = slice(at, at + len(near))
    corr = np.corrcoef(out[s].astype(np.float64), near)[0, 1]
    assert corr >= 0.9, f"corrélation avec la parole proche {corr:.2f}"
    assert abs(_db(out[s]) - _db(near)) <= 3.0


@pytest.mark.parametrize("delay_ms", [0, 40, 250])
def test_fine_delay_found(far, delay_ms):
    mic = _mic(far, delay_ms)
    canceller, out = _playback(far, mic)
    assert canceller.delays["tts-1"] == pytest.approx(int(delay_ms * SR / 1000), abs=16)
    s = slice(START + int(1.5 * SR), START + len(far))
    assert _db(mic[s]) - _db(out[s]) >= 20.0


def test_identity_before_and_after_playback(far):
    mic = _mic(far, 120)
    canceller, out = _playback(far, mic)
    assert len(out) == len(mic)  # tout rendu à la fin du flux (plus de lecture en cours)
    np.testing.assert_array_equal(out[:START], mic[:START])
    after = START + len(far) + int(0.120 * SR) + aec.SAMPLE_RATE * 300 // 1000 + 2 * 160
    np.testing.assert_array_equal(out[after:], mic[after:])


def test_unknown_reference_and_orphan_end_pass_through(far):
    mic = _mic(far, 120)
    asked: list[str] = []

    def reference(pid: str) -> np.ndarray | None:
        asked.append(pid)
        return None

    marks: dict[str, tuple[int, int | None]] = {"orphan": (500, 500)}  # fin sans début
    canceller = aec.EchoCanceller(reference, marks)
    out = _stream(canceller, marks, mic, [(START, "mpd-1", True)])
    np.testing.assert_array_equal(out, mic)
    assert asked == ["mpd-1"]


def test_gap_in_stream_keeps_samples(far):
    """Trames perdues (first_sample qui saute) : rien n'est perdu ni inventé, pas d'erreur."""
    mic = _mic(far, 120)
    marks: dict[str, tuple[int, int | None]] = {"tts-1": (START, None)}
    canceller = aec.EchoCanceller({"tts-1": far}.get, marks)
    given = 0
    out = []
    for pos in range(0, len(mic), GATEWAY_BLOCK):
        if pos == START + 3 * GATEWAY_BLOCK:
            continue  # bloc perdu
        out.append(canceller.process(mic[pos : pos + GATEWAY_BLOCK], pos))
        given += len(mic[pos : pos + GATEWAY_BLOCK])
    marks["tts-1"] = (START, START + len(far))
    out.append(canceller.process(np.zeros(SR, np.int16), len(mic)))
    assert sum(len(o) for o in out) == given + SR


def test_cpu_per_rabbit_is_modest(far):
    mic = _mic(far, 120)
    t0 = time.perf_counter()
    _playback(far, mic)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.1 * len(mic) / SR  # < 10 % d'un cœur en temps réel (mesuré : ~1 %)
