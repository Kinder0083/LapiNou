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

from lapinou.rabbit import audio_gateway
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
    return _stream_lags(canceller, marks, mic, events, block)[0]


def _stream_lags(canceller, marks, mic, events, block=GATEWAY_BLOCK):
    """Comme _stream ; rend aussi, par bloc, (position, retard de la sortie sur l'entrée)."""
    out, lags, pending = [], [], sorted(events)
    for pos in range(0, len(mic), block):
        while pending and pending[0][0] < pos + block:
            at, pid, start = pending.pop(0)
            if start:  # comme RabbitStream : une marque de début passe en dernier
                marks.pop(pid, None)
                marks[pid] = (at, None)
            else:
                marks[pid] = (marks.get(pid, (at, None))[0], at)
        out.append(canceller.process(mic[pos : pos + block], pos))
        lags.append((pos, min(pos + block, len(mic)) - canceller.next_sample))
    return np.concatenate(out), lags


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
    assert canceller.delay == pytest.approx(int(0.120 * SR), abs=16)
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
    assert canceller.delay == pytest.approx(int(delay_ms * SR / 1000), abs=16)
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


# -- plusieurs lectures --------------------------------------------------------------------


@pytest.fixture(scope="module")
def second() -> np.ndarray:
    pcm = np.concatenate([read_wav(DATA / f"asr_{p}.wav") for p in ("he_heure_2", "pinede_2")])
    return np.round(_scale(pcm, FAR_DBFS)).astype(np.int16)


def _two_playbacks(far, second, delay2_ms: float | None, near=None):
    """p1 = far (écho 120 ms) puis, 1 s après sa fin, p2 = second (écho delay2_ms, ou aucun)."""
    p2 = START + len(far) + SR
    total = p2 + len(second) + int(TAIL_S * SR)
    mic = np.random.default_rng(2).normal(0.0, 32768.0 * 10 ** (NOISE_DBFS / 20), total)
    gain = 10 ** (ECHO_GAIN_DB / 20)
    d1 = START + int(0.120 * SR)
    mic[d1 : d1 + len(far)] += far * gain
    if delay2_ms is not None:
        d2 = p2 + int(delay2_ms * SR / 1000)
        mic[d2 : d2 + len(second)] += second * gain
    if near is not None:
        mic[p2 + SR // 4 : p2 + SR // 4 + len(near)] += near
    mic = np.clip(np.round(mic), -32768, 32767).astype(np.int16)
    marks: dict[str, tuple[int, int | None]] = {}
    canceller = aec.EchoCanceller({"p1": far, "p2": second}.get, marks)
    events = [
        (START, "p1", True),
        (START + len(far), "p1", False),
        (p2, "p2", True),
        (p2 + len(second), "p2", False),
    ]
    out, lags = _stream_lags(canceller, marks, mic, events)
    return canceller, mic, out, lags, p2


def test_later_playback_not_held_and_cancelled_at_once(far, second):
    canceller, mic, out, lags, p2 = _two_playbacks(far, second, 120)
    during = [lag for pos, lag in lags if pos >= p2 - GATEWAY_BLOCK]
    assert max(during) < 2 * 160  # trame entamée + débruiteur : pas de retenue de 850 ms
    s = slice(p2, p2 + len(second))  # dès le début : filtre déjà convergé, retard connu
    assert _db(mic[s]) - _db(out[s]) >= 20.0
    assert canceller.delay == pytest.approx(int(0.120 * SR), abs=16)


def test_failed_estimate_keeps_previous_delay(far, second, near):
    """p2 sans écho, l'utilisateur parle : l'estimation échoue, le retard de p1 reste."""
    canceller, *_ = _two_playbacks(far, second, None, near)
    assert canceller.delay == pytest.approx(int(0.120 * SR), abs=16)


def test_clear_new_delay_adopted(far, second):
    canceller, *_ = _two_playbacks(far, second, 200)
    assert canceller.delay == pytest.approx(int(0.200 * SR), abs=16)


def test_pause_resume_continues_reference(far):
    """karotz-player reprend au milieu du son : la référence aussi."""
    pause_at, pause = 8 * SR, int(0.6 * SR)
    resume = START + pause_at + pause
    rest = far[pause_at:]
    total = resume + len(rest) + int(TAIL_S * SR)
    mic = np.random.default_rng(3).normal(0.0, 32768.0 * 10 ** (NOISE_DBFS / 20), total)
    gain, d = 10 ** (ECHO_GAIN_DB / 20), int(0.120 * SR)
    mic[START + d : START + d + pause_at] += far[:pause_at] * gain
    mic[resume + d : resume + d + len(rest)] += rest * gain
    mic = np.clip(np.round(mic), -32768, 32767).astype(np.int16)
    marks: dict[str, tuple[int, int | None]] = {}
    canceller = aec.EchoCanceller({"tts-1": far}.get, marks)
    events = [
        (START, "tts-1", True),
        (START + pause_at, "tts-1", False),
        (resume, "tts-1", True),
        (resume + len(rest), "tts-1", False),
    ]
    out = _stream(canceller, marks, mic, events)
    s = slice(resume + SR // 2, resume + len(rest))
    assert _db(mic[s]) - _db(out[s]) >= 20.0
    s = slice(START + pause_at, START + pause_at + d)  # queue d'écho d'avant la pause
    assert _db(mic[s]) - _db(out[s]) >= 15.0


def test_resent_block_trimmed():
    """Trame renvoyée en entier après reconnexion : la partie déjà reçue n'est pas rendue."""
    mic = np.random.default_rng(4).integers(-3000, 3000, 10 * GATEWAY_BLOCK, dtype=np.int16)
    canceller = aec.EchoCanceller(lambda pid: None, {})
    b = GATEWAY_BLOCK
    out = [canceller.process(mic[: 3 * b], 0)]
    out.append(canceller.process(mic[2 * b : 3 * b], 2 * b))  # renvoyée en entier
    out.append(canceller.process(mic[3 * b - 1000 : 5 * b], 3 * b - 1000))  # recouvrement
    out.append(canceller.process(mic[5 * b :], 5 * b))
    np.testing.assert_array_equal(np.concatenate(out), mic)


def test_axis_reset_forgets_playbacks_of_old_axis(far):
    """lapinou-mic redémarré : first_sample repart de 0 ; une lecture de l'ancien axe
    (début à 2 s) ne doit pas s'appliquer au nouvel axe."""
    rng = np.random.default_rng(5)
    marks: dict[str, tuple[int, int | None]] = {}
    canceller = aec.EchoCanceller({"old": far}.get, marks)
    old = rng.integers(-3000, 3000, 3 * SR, dtype=np.int16)
    _stream(canceller, marks, old, [(2 * SR, "old", True)])
    marks.clear()  # la passerelle oublie les marques de l'ancien axe
    new = rng.integers(-3000, 3000, 6 * SR, dtype=np.int16)
    first = canceller.process(new[:GATEWAY_BLOCK], 0)
    out = [first[-GATEWAY_BLOCK:]]  # le début de `first` vide l'ancien axe
    for pos in range(GATEWAY_BLOCK, len(new), GATEWAY_BLOCK):
        out.append(canceller.process(new[pos : pos + GATEWAY_BLOCK], pos))
    np.testing.assert_array_equal(np.concatenate(out), new)


def test_10000_playbacks_bounded(far):
    """Longue durée : marques élaguées, état et coût de l'annuleur bornés."""
    stream = audio_gateway.RabbitStream("r1")
    ref = far[: 2 * GATEWAY_BLOCK]
    canceller = aec.EchoCanceller(
        lambda pid: ref if pid.endswith("000") else None, stream.playback_marks
    )
    silence = b"\xff" * GATEWAY_BLOCK
    seq = pos = 0
    largest = 0
    t0 = time.perf_counter()
    for n in range(10_000):
        for start in (True, False):
            stream._feed(audio_gateway.Frame("r1", seq, mark=(f"tts-{n}", pos, start)))
            stream._feed(audio_gateway.Frame("r1", seq + 1, mulaw=silence, first_sample=pos))
            chunk = stream._queue.get_nowait()
            canceller.process(chunk.pcm, chunk.first_sample)
            seq, pos = seq + 2, pos + GATEWAY_BLOCK
        largest = max(largest, len(stream.playback_marks))
        assert len(canceller._sounds) <= 2 * len(stream.playback_marks) + 64
    elapsed = time.perf_counter() - t0
    assert largest <= 64
    assert len(canceller._segments) <= 2
    assert elapsed < 0.02 * pos / SR  # < 2 % d'un cœur
