# SPDX-License-Identifier: MIT
"""Transcription d'une requête : logique de flux (segmenteur factice), puis les vrais modèles
(Parakeet + Silero), ignorés tant qu'ils manquent (python -m lapinou.voice.models fetch).
Audio : les synthèses françaises de server/tests/data et l'enregistrement fr.wav du modèle."""

from __future__ import annotations

import numpy as np
import pytest
from test_wake_asr import SR, read_pcm16
from test_wakeword import DATA, read_wav

from lapinou.voice import asr, vad

BLOCK = 2048


class FakeRecogniser:
    def transcribe(self, samples):
        return " allume le salon "


class FakeSegmenter:
    """Parole entre les échantillons `start` et `end` ; le segment sort `silence` après."""

    def __init__(self, *, start=None, end=0, tail=None):
        self.start, self.end, self.tail, self.fed, self.flushed = start, end, tail, 0, False

    @property
    def speaking(self):
        return self.start is not None and self.start <= self.fed <= self.end

    def feed(self, pcm):
        self.fed += len(pcm)
        if self.tail is not None and self.fed >= self.tail:
            return [(self.start, np.ones(8000, np.float32))]
        return []

    def flush(self):
        self.flushed = True
        return [(self.start, np.ones(8000, np.float32))] if self.start is not None else []


def blocks(seconds, counter=None):
    async def gen():
        for _ in range(int(seconds * SR) // BLOCK):
            if counter is not None:
                counter.append(1)
            yield np.zeros(BLOCK, np.int16)

    return gen()


def transcriber(monkeypatch, segmenter):
    monkeypatch.setattr(vad, "SileroSegmenter", lambda *a, **k: segmenter)
    return asr.Transcriber(recogniser=FakeRecogniser())


async def test_no_speech_within_five_seconds_is_none_and_stops_reading(monkeypatch):
    read: list[int] = []
    assert await transcriber(monkeypatch, FakeSegmenter()).transcribe_utterance(blocks(30, read)) is None
    assert len(read) * BLOCK < 5.5 * SR


async def test_stream_end_without_speech_is_none(monkeypatch):
    assert await transcriber(monkeypatch, FakeSegmenter()).transcribe_utterance(blocks(2)) is None


async def test_speech_is_transcribed_and_reading_stops_at_its_end(monkeypatch):
    read: list[int] = []
    seg = FakeSegmenter(start=SR, end=2 * SR, tail=3 * SR)
    text = await transcriber(monkeypatch, seg).transcribe_utterance(blocks(30, read))
    assert text == "allume le salon"
    assert len(read) * BLOCK < 3.5 * SR


async def test_speech_may_start_after_the_five_seconds_it_is_not_cut(monkeypatch):
    """Une parole commencée avant 5 s n'est pas abandonnée même si elle dépasse 5 s."""
    seg = FakeSegmenter(start=4 * SR, end=9 * SR, tail=10 * SR)
    assert await transcriber(monkeypatch, seg).transcribe_utterance(blocks(30)) == "allume le salon"


async def test_stream_ending_during_speech_flushes_the_vad(monkeypatch):
    seg = FakeSegmenter(start=SR // 2, end=10 * SR)
    assert await transcriber(monkeypatch, seg).transcribe_utterance(blocks(2)) == "allume le salon"
    assert seg.flushed


async def test_wrong_pcm_format_is_refused(monkeypatch):
    async def floats():
        yield np.zeros(BLOCK, np.float32)

    with pytest.raises(ValueError, match="int16"):
        await transcriber(monkeypatch, FakeSegmenter()).transcribe_utterance(floats())


# ---- vrais modèles ----

needs_models = pytest.mark.skipif(
    not vad.models_present(),
    reason="modèles Parakeet/Silero ou sherpa-onnx absents (python -m lapinou.voice.models fetch)",
)


@pytest.fixture(scope="module")
def real():
    return asr.Transcriber(recogniser=vad.load_recogniser(threads=2))


async def stream(pcm, tail_s=2.0, block=BLOCK):
    pcm = np.concatenate([pcm, np.zeros(int(tail_s * SR), np.int16)])
    for i in range(0, len(pcm), block):
        yield pcm[i : i + block]


def words(text):
    return " ".join(text.lower().replace(",", " ").replace(".", " ").replace("?", " ").split())


@needs_models
@pytest.mark.parametrize(
    "slug, expected",
    [
        ("allume_salon", "allume le salon"),
        ("he_heure", "quelle heure est il"),
        ("dis_lumiere", "la lumière de la cuisine"),
    ],
)
async def test_french_synthetic_requests(real, slug, expected):
    text = await real.transcribe_utterance(stream(read_wav(DATA / f"asr_{slug}_1.wav")))
    assert text is not None
    assert expected.replace("-", " ") in words(text).replace("-", " "), "transcription inattendue"


@needs_models
async def test_lapinlibre_french_recording(real):
    path = vad.asr_server.MODELS / vad.asr_server.PARAKEET / "test_wavs" / "fr.wav"
    if not path.is_file():
        pytest.skip("test_wavs/fr.wav absent (il vient de l'archive du modèle)")
    text = await real.transcribe_utterance(stream(read_pcm16(path)), max_s=30)
    assert text and len(text.split()) >= 5


@needs_models
async def test_five_seconds_of_silence_is_none(real):
    assert await real.transcribe_utterance(stream(np.zeros(5 * SR, np.int16), tail_s=0)) is None


@needs_models
async def test_silence_then_speech_within_five_seconds(real):
    pcm = np.concatenate([np.zeros(3 * SR, np.int16), read_wav(DATA / "asr_allume_salon_1.wav")])
    assert await real.transcribe_utterance(stream(pcm)) is not None


@needs_models
async def test_max_s_cuts_a_long_speech(real):
    path = vad.asr_server.MODELS / vad.asr_server.PARAKEET / "test_wavs" / "fr.wav"
    if not path.is_file():
        pytest.skip("test_wavs/fr.wav absent")
    pcm = read_pcm16(path)
    full = await real.transcribe_utterance(stream(pcm), max_s=30)
    short = await real.transcribe_utterance(stream(pcm), max_s=2)
    assert short and len(short) < len(full)
