# SPDX-License-Identifier: MIT
"""Mot d'activation « Lapinou » sur des voix de synthèse jamais vues à l'entraînement.

Fichiers (server/tools/train_wakeword/train.py export-tests) : mu-law 8 bits 16 kHz,
comme le flux des lapins. Les 5 min de négatifs enregistrées sont complétées à 10 min
par une version dérivée déterministe (audio retourné + bruit rose), pour garder le dépôt
léger.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from lapinou.config import Settings
from lapinou.rabbit.audio_gateway import mulaw_decode
from lapinou.voice import wakeword
from lapinou.voice.wakeword import DEFAULT_MODEL, WakeWord, feature_models_present

DATA = Path(__file__).parent / "data"
SR = 16000
GATEWAY_BLOCK = 2048  # taille typique des blocs de la passerelle audio

needs_models = pytest.mark.skipif(
    not feature_models_present() or not DEFAULT_MODEL.is_file(),
    reason="modèles openWakeWord absents (uv run python -m lapinou.voice.wakeword download)",
)


def read_wav(path: Path) -> np.ndarray:
    """WAV mono 16 kHz mu-law (format 7) ou PCM 16 bits -> int16."""
    data = path.read_bytes()
    assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    pos, fmt, body = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos : pos + 4], struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHI", data[pos + 8 : pos + 16])
        elif cid == b"data":
            body = data[pos + 8 : pos + 8 + size]
        pos += 8 + size + (size & 1)
    assert fmt is not None and body is not None
    tag, channels, rate = fmt
    assert (channels, rate) == (1, SR)
    if tag == 7:
        return mulaw_decode(body)
    assert tag == 1
    return np.frombuffer(body, "<i2").copy()


def feed_all(detector: WakeWord, pcm: np.ndarray, block: int = GATEWAY_BLOCK) -> list[float]:
    hits = []
    for start in range(0, len(pcm), block):
        score = detector.feed(pcm[start : start + block])
        if score is not None:
            hits.append(score)
    return hits


def negatives() -> list[tuple[str, np.ndarray]]:
    """10 min : les fichiers enregistrés, puis chacun retourné et mêlé de bruit rose."""
    stored = [(p.name, read_wav(p)) for p in sorted(DATA.glob("not_lapinou_*.wav"))]
    rng = np.random.default_rng(16)
    derived = []
    for name, pcm in stored:
        white = np.fft.rfft(rng.standard_normal(len(pcm)))
        f = np.fft.rfftfreq(len(pcm))
        f[0] = f[1]
        pink = np.fft.irfft(white / np.sqrt(f), len(pcm))
        pink *= 300 / np.sqrt(np.mean(pink**2))  # environ -40 dBFS
        mixed = pcm[::-1].astype(np.float64) * 0.7 + pink
        derived.append((f"{name} retourné", np.clip(mixed, -32768, 32767).astype(np.int16)))
    return stored + derived


def test_download_checks_sha256(tmp_path, monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"pas un modele"

    monkeypatch.setattr(wakeword.urllib.request, "urlopen", lambda url, timeout: FakeResponse())
    with pytest.raises(RuntimeError, match="SHA-256"):
        wakeword.download_feature_models(tmp_path)
    assert not any(tmp_path.iterdir())


@needs_models
def test_positives_detected():
    files = sorted(DATA.glob("lapinou_*.wav"))
    assert len(files) == 10
    detector = WakeWord(DEFAULT_MODEL, Settings().voice.wake_threshold)
    detected = []
    for path in files:
        detector.reset()
        pcm = np.concatenate((read_wav(path), np.zeros(SR, np.int16)))
        hits = feed_all(detector, pcm)
        detected.append(bool(hits))
        assert all(h >= detector.threshold for h in hits)
    assert sum(detected) >= 9, dict(zip((p.name for p in files), detected))


@needs_models
def test_no_false_detection_in_ten_minutes():
    clips = negatives()
    total = sum(len(pcm) for _, pcm in clips) / SR
    assert total >= 600
    detector = WakeWord(DEFAULT_MODEL, Settings().voice.wake_threshold)
    false = {name: hits for name, pcm in clips if (hits := feed_all(detector, pcm))}
    assert false == {}


@needs_models
def test_result_does_not_depend_on_block_size():
    pcm = read_wav(sorted(DATA.glob("lapinou_*.wav"))[0])
    detector = WakeWord(DEFAULT_MODEL, 0.0)  # seuil nul : le premier score est renvoyé
    scores = {}
    for block in (160, 1280, 2048, 5000):
        detector.reset()
        for start in range(0, len(pcm), block):
            detector.feed(pcm[start : start + block])
        scores[block] = detector.last_score
    assert max(scores.values()) - min(scores.values()) < 1e-5


@needs_models
def test_one_detection_per_utterance_and_reset():
    pcm = np.concatenate((read_wav(sorted(DATA.glob("lapinou_*.wav"))[0]), np.zeros(SR, np.int16)))
    detector = WakeWord(DEFAULT_MODEL, Settings().voice.wake_threshold)
    assert len(feed_all(detector, pcm)) == 1
    assert detector.feed(np.zeros(100, np.int16)) is None  # moins d'un bloc de 80 ms
    detector.reset()
    assert detector.last_score == 0.0
    assert len(feed_all(detector, pcm, block=pcm.size)) == 1  # un seul gros bloc
