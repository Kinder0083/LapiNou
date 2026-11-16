# SPDX-License-Identifier: MIT
"""Détection de « Lapinou » par transcription : correspondance du texte, détecteur de flux
(transcripteur et segmenteur factices) puis les vrais modèles (Parakeet + Silero).

Les tests avec les vrais modèles sont ignorés (skip) tant que ceux-ci ne sont pas là :
`python -m lapinou.voice.models fetch` (~640 Mo, SHA-256 vérifié) et sherpa-onnx
(uv sync). Fichiers : les enregistrements français de LapinLibre (test_wavs du modèle),
les synthèses de la tâche 16 (lapinou_*.wav, not_lapinou_*.wav) et les phrases complètes
asr_*.wav (train.py export-asr-tests).
"""

from __future__ import annotations

import dataclasses
import logging
import wave

import numpy as np
import pytest
from test_wakeword import DATA, GATEWAY_BLOCK, SR, negatives, read_wav

from lapinou.config import Settings, VoiceConfig
from lapinou.voice import wake_asr
from lapinou.voice.wake_asr import AsrWakeDetector, WakeHit, build_detector, match_wake_word

NAMES = ["lapinou"]


# ---- correspondance du texte ----


@pytest.mark.parametrize(
    "text, command",
    [
        ("Lapinou, allume le salon.", "allume le salon"),
        ("Lapinou", ""),
        ("Lapinou.", ""),
        ("Lapinou !", ""),
        ("lapinou allume la lumière de la cuisine", "allume la lumière de la cuisine"),
        ("Hé Lapinou, quelle heure est-il ?", "quelle heure est-il"),
        ("Dis, Lapinou, éteins tout", "éteins tout"),
        ("Ok Lapinou: musique", "musique"),
        ("Eh Lapinou", ""),
        ("Dit Lapinou. Était la lumière", "Était la lumière"),
        ("Et Lapinot, quelle heure est-il ?", "quelle heure est-il"),
        ("L'apinou allume le salon.", "allume le salon"),
        ("Lapinot.", ""),
        ("Lapino, allume le salon", "allume le salon"),  # un « u » manquant
        ("Lapinoux allume", "allume"),
        ("Lapinau, allume", "allume"),
        ("La pinou, allume le salon", "allume le salon"),
        ("Lapi nou. Quelle heure est-il ?", "Quelle heure est-il"),
        ("La pi nou, éteins", "éteins"),
        ("LAPI-NOU, allume", "allume"),
    ],
)
def test_matches_the_name_at_the_start(text, command):
    assert match_wake_word(text, NAMES) == ("lapinou", command)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "...",
        "Mon lapinou est mignon",  # au milieu : jamais
        "Le lapinou dort. Lapinou, viens",  # le nom n'est pas au début
        "Hier, dis Lapinou",
        "J'ai dit à Lapinou de venir",
        "Le lapin mange une carotte",
        "Lapin",
        "Lapins et lapines",
        "Nous marchons dans la pinède",
        "La pinède est belle",
        "La pinotte",
        "Un lapin",
        "Dis",  # un mot d'introduction seul
        "Dis bonjour",
        "Allume le salon",
        "Lapin, où es-tu ?",  # la ponctuation sépare les mots
        "Lapin ou lièvre ?",
        "Lapin où",
        "Lapi. Nou",
    ],
)
def test_ignores_everything_else(text):
    assert match_wake_word(text, NAMES) is None


def test_other_names_and_best_name():
    assert match_wake_word("Karotte, quelle heure ?", ["lapinou", "karotte"]) == ("karotte", "quelle heure")
    assert match_wake_word("Lapinou", ["karotte"]) is None


# ---- détecteur de flux (factices) ----


class RunSegmenter:
    """Un segment = une suite d'échantillons non nuls terminée par 100 échantillons nuls."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.buf, self.at = [], 0

    def feed(self, pcm):
        out = []
        for x in pcm:
            if x != 0:
                self.buf.append(x)
            elif self.buf:
                out.append((self.at - len(self.buf), np.array(self.buf, np.float32) / 32768))
                self.buf = []
            self.at += 1
        return out


class ScriptedTranscriber:
    """Rend les textes dans l'ordre et note la durée de chaque segment transcrit (sans le silence
    ajouté de chaque côté)."""

    def __init__(self, *texts):
        self.texts, self.lengths = list(texts), []

    def transcribe(self, samples):
        self.lengths.append(len(samples) - 2 * int(wake_asr.PAD_S * SR))
        return self.texts.pop(0)


def speech(n):
    return np.ones(n, np.int16) * 100


def silence(n):
    return np.zeros(n, np.int16)


def test_hit_has_text_command_and_segment_start():
    asr = ScriptedTranscriber("Lapinou, allume le salon.")
    detector = AsrWakeDetector(asr, segmenter=RunSegmenter())
    assert detector.feed(silence(1000)) == []
    assert detector.feed(speech(500)) == []  # pas encore terminé : rien n'est transcrit
    assert asr.lengths == []
    hits = detector.feed(silence(10))
    assert hits == [WakeHit(at_sample=1000, text="Lapinou, allume le salon.", command="allume le salon")]
    assert asr.lengths == [500]


def test_name_alone_has_empty_command_and_other_speech_no_hit():
    detector = AsrWakeDetector(
        ScriptedTranscriber("Lapinou", "Il fait beau aujourd'hui", "Mon lapinou est mignon"),
        segmenter=RunSegmenter(),
    )
    audio = np.concatenate([silence(50), speech(300), silence(50), speech(300), silence(50), speech(300), silence(5)])
    assert [(h.text, h.command) for h in detector.feed(audio)] == [("Lapinou", "")]


def test_each_segment_is_transcribed_once_whatever_the_block_size():
    audio = np.concatenate([silence(40), speech(300), silence(150), speech(200), silence(150)])
    counts = []
    for block in (1, 7, 128, len(audio)):
        asr = ScriptedTranscriber("Lapinou", "Lapinou, éteins")
        detector = AsrWakeDetector(asr, segmenter=RunSegmenter())
        hits = [h for i in range(0, len(audio), block) for h in detector.feed(audio[i : i + block])]
        counts.append((len(asr.lengths), [(h.at_sample, h.command) for h in hits]))
    assert counts == [(2, [(40, ""), (490, "éteins")])] * 4


def test_long_sentence_is_cut_but_its_chunks_complete_the_command():
    n = int(2.5 * SR)
    asr = ScriptedTranscriber("Lapinou, ajoute du lait", "et des oeufs.", "Lapinou pour demain")
    detector = AsrWakeDetector(asr, max_segment_s=1.0, segmenter=RunSegmenter())
    hits = detector.feed(np.concatenate([speech(n), silence(5)]))
    assert asr.lengths == [SR, SR, n - 2 * SR]
    # « Lapinou » en début de 3e morceau : au milieu de la phrase, jamais une détection
    assert [(h.at_sample, h.command, h.complete) for h in hits] == [
        (0, "ajoute du lait", False),
        (0, "ajoute du lait et des oeufs", False),
        (0, "ajoute du lait et des oeufs Lapinou pour demain", True),
    ]
    assert detector.transcribed_s == pytest.approx(2.5)


def test_hits_are_immutable_snapshots():
    (hit,) = AsrWakeDetector(ScriptedTranscriber("Lapinou"), segmenter=RunSegmenter()).feed(
        np.concatenate([speech(50), silence(5)])
    )
    assert hit.complete
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.command = "autre"


def test_name_inside_a_long_sentence_is_not_a_wake_word():
    asr = ScriptedTranscriber("Il fait très beau aujourd'hui", "Lapinou, allume le salon")
    detector = AsrWakeDetector(asr, max_segment_s=1.0, segmenter=RunSegmenter())
    assert detector.feed(np.concatenate([speech(int(2.5 * SR)), silence(5)])) == []
    assert asr.lengths == [SR]  # les morceaux suivants ne sont même pas transcrits


class ScriptSegmenter:
    """Rend à chaque feed() les segments (début, durée en s) préparés, comme le VAD."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)

    def feed(self, pcm):
        return [(a, np.full(int(d * SR), 0.1, np.float32)) for a, d in (self.rounds.pop(0) if self.rounds else [])]

    def reset(self):
        pass


def test_vad_cut_segments_continue_the_open_hit():
    asr = ScriptedTranscriber("Lapinou, ajoute du lait", "et des oeufs", "pour demain")
    segmenter = ScriptSegmenter([(0, 1.0)], [(SR, 1.0)], [(2 * SR, 0.4)])
    detector = AsrWakeDetector(asr, max_segment_s=1.0, segmenter=segmenter)
    (first,) = detector.feed(silence(10))
    assert (first.command, first.complete) == ("ajoute du lait", False)
    (second,) = detector.feed(silence(10))  # même parole : un nouvel instantané, même at_sample
    assert (second.at_sample, second.command, second.complete) == (0, "ajoute du lait et des oeufs", False)
    (last,) = detector.feed(silence(10))
    assert (last.command, last.complete) == ("ajoute du lait et des oeufs pour demain", True)
    assert last.text == "Lapinou, ajoute du lait et des oeufs pour demain"
    assert first.command == "ajoute du lait"  # les instantanés rendus ne changent plus


def test_silence_alone_completes_a_cut_hit_within_the_bound():
    """Parole coupée pile à max_segment_s puis plus rien : feed() clôt la détection quand le
    flux a avancé de max_segment_s + silence_s + 0,2 s après la fin du morceau (la suite de la
    parole serait arrivée avant)."""
    asr = ScriptedTranscriber("Lapinou, ajoute du lait")
    detector = AsrWakeDetector(asr, max_segment_s=1.0, segmenter=ScriptSegmenter([(0, 1.0)]))
    (hit,) = detector.feed(silence(10))
    assert not hit.complete
    bound = int((1.0 + 0.7 + wake_asr.CONTINUE_GAP_S) * SR)  # après la fin du morceau (SR)
    assert detector.feed(silence(SR + bound - 10 - 1)) == []
    (done,) = detector.feed(silence(1))
    assert (done.at_sample, done.command, done.complete) == (0, "ajoute du lait", True)
    assert detector.feed(silence(SR)) == []  # une seule clôture


def test_reset_returns_the_open_hit_complete():
    detector = AsrWakeDetector(
        ScriptedTranscriber("Lapinou, ajoute"), max_segment_s=1.0, segmenter=ScriptSegmenter([(0, 1.0)])
    )
    detector.feed(silence(10))
    (done,) = detector.reset()
    assert (done.command, done.complete) == ("ajoute", True)
    assert detector.reset() == []


def test_vad_cut_segment_followed_by_the_name_is_not_a_wake_word():
    asr = ScriptedTranscriber("Il parle depuis un moment", "Lapinou, allume")
    segmenter = ScriptSegmenter([(0, 1.0)], [(SR, 0.5)])
    detector = AsrWakeDetector(asr, max_segment_s=1.0, segmenter=segmenter)
    assert detector.feed(silence(10)) == [] and detector.feed(silence(10)) == []
    assert asr.lengths == [SR]


def test_after_a_pause_the_name_is_a_new_utterance():
    asr = ScriptedTranscriber("Il parle depuis un moment", "Lapinou, allume")
    segmenter = ScriptSegmenter([(0, 1.0)], [(2 * SR, 0.5)])  # 1 s de silence entre les deux
    detector = AsrWakeDetector(asr, max_segment_s=1.0, segmenter=segmenter)
    detector.feed(silence(10))
    hits = detector.feed(silence(10))
    # la première parole (sans nom) n'a rien rendu ; la seconde est une nouvelle détection
    assert [(h.at_sample, h.command, h.complete) for h in hits] == [(2 * SR, "allume", True)]


def test_reset_drops_the_speech_in_progress_and_keeps_numbering():
    asr = ScriptedTranscriber("Lapinou")
    detector = AsrWakeDetector(asr, segmenter=RunSegmenter())
    detector.feed(speech(300))  # interrompu
    detector.reset()
    hits = detector.feed(np.concatenate([silence(100), speech(50), silence(5)]))
    assert [h.at_sample for h in hits] == [300 + 100]


def test_feed_wants_int16_mono():
    detector = AsrWakeDetector(ScriptedTranscriber(), segmenter=RunSegmenter())
    with pytest.raises(ValueError):
        detector.feed(np.zeros(10, np.float32))
    with pytest.raises(ValueError):
        detector.feed(np.zeros((2, 10), np.int16))


class FakePrefilter:
    """Se déclenche quand un bloc contient la valeur 7 ; note ses reset()."""

    def __init__(self):
        self.resets = 0

    def feed(self, pcm):
        return 0.99 if (pcm == 7).any() else None

    def reset(self):
        self.resets += 1


def test_prefilter_gates_the_transcription():
    asr = ScriptedTranscriber("Lapinou", "Lapinou")
    prefilter = FakePrefilter()
    detector = AsrWakeDetector(asr, segmenter=RunSegmenter(), prefilter=prefilter)
    plain, marked = speech(300), speech(300)
    marked[10] = 7
    hits = []
    for part in (silence(30000), plain, silence(30000), marked, silence(5)):
        hits += detector.feed(part)
    assert asr.lengths == [300]  # le premier segment n'a pas été transcrit
    assert [h.at_sample for h in hits] == [60300]
    detector.reset()
    assert prefilter.resets == 1


# ---- configuration ----


def test_wake_method_defaults_to_asr_and_is_validated():
    assert Settings().voice.wake_method == "asr"
    assert VoiceConfig(wake_method="both").wake_method == "both"
    with pytest.raises(ValueError):
        VoiceConfig(wake_method="magic")


def test_build_detector_by_method(caplog, monkeypatch):
    asr = ScriptedTranscriber()
    monkeypatch.setattr(wake_asr, "SileroSegmenter", lambda *a, **k: RunSegmenter())
    detector = build_detector(VoiceConfig(vad_silence_ms=900), asr)
    assert detector.prefilter is None
    with caplog.at_level(logging.WARNING):
        both = build_detector(VoiceConfig(wake_method="both"), asr)
    assert both.prefilter is None and "wake_model" in caplog.text
    with pytest.raises(ValueError, match="load_wakeword"):
        build_detector(VoiceConfig(wake_method="oww"), asr)


def test_build_detector_both_loads_the_prefilter(monkeypatch):
    from lapinou.voice import wakeword

    marker = FakePrefilter()
    monkeypatch.setattr(wake_asr, "SileroSegmenter", lambda *a, **k: RunSegmenter())
    monkeypatch.setattr(wakeword, "load_wakeword", lambda voice: marker)
    detector = build_detector(VoiceConfig(wake_method="both", wake_model="/x.onnx"), ScriptedTranscriber())
    assert detector.prefilter is marker


# ---- vrais modèles : Parakeet + Silero ----

needs_models = pytest.mark.skipif(
    not wake_asr.models_present(),
    reason="modèles Parakeet/Silero ou sherpa-onnx absents (python -m lapinou.voice.models fetch)",
)

TAIL = np.zeros(SR * 2, np.int16)  # le VAD termine un segment après 0,7 s de silence


@pytest.fixture(scope="module")
def recogniser():
    return wake_asr.load_recogniser(threads=2)


def listen(recogniser, pcm, block=GATEWAY_BLOCK, **kw) -> list[WakeHit]:
    """Un détecteur neuf écoute pcm (puis du silence) par blocs de la passerelle audio."""
    detector = AsrWakeDetector(recogniser, **kw)
    pcm = np.concatenate([pcm, TAIL])
    return [h for i in range(0, len(pcm), block) for h in detector.feed(pcm[i : i + block])]


def read_pcm16(path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert (w.getnchannels(), w.getsampwidth()) == (1, 2)
        pcm = np.frombuffer(w.readframes(w.getnframes()), "<i2")
        rate = w.getframerate()
    positions = np.arange(0, len(pcm) - 1, rate / SR)  # vers 16 kHz
    return np.interp(positions, np.arange(len(pcm)), pcm.astype(np.float64)).astype(np.int16)


@needs_models
def test_lapinlibre_french_recording_has_no_detection(recogniser):
    path = wake_asr.asr_server.MODELS / wake_asr.asr_server.PARAKEET / "test_wavs" / "fr.wav"
    if not path.is_file():
        pytest.skip("test_wavs/fr.wav absent (il vient de l'archive du modèle, pas du téléchargeur)")
    assert listen(recogniser, read_pcm16(path)) == []


@needs_models
@pytest.mark.parametrize(
    "slug, command",
    [
        ("allume_salon", "allume le salon"),
        ("he_heure", "quelle heure est-il"),
        ("dis_lumiere", "la lumière de la cuisine"),
    ],
)
@pytest.mark.parametrize("voice", [1, 2])
def test_name_then_command_on_synthetic_speech(recogniser, slug, command, voice):
    """Voix 1 (siwis, nette) : la fin de la commande est exacte (Parakeet entend « était » pour
    « éteins »). Voix 2 (gilles, « low ») : une longue pause après le nom coupe le segment,
    le nom seul est alors reconnu (commande vide) et la commande suit dans le segment suivant."""
    hits = listen(recogniser, read_wav(DATA / f"asr_{slug}_{voice}.wav"))
    assert len(hits) == 1, [h.text for h in hits]
    if voice == 1 or hits[0].command:
        assert hits[0].command.lower().endswith(command)
    assert 0.5 * SR <= hits[0].at_sample <= 0.9 * SR  # début de la parole


@needs_models
@pytest.mark.parametrize("slug", ["milieu", "lapin", "pinede"])
@pytest.mark.parametrize("voice", [1, 2])
def test_traps_are_not_detected(recogniser, slug, voice):
    assert listen(recogniser, read_wav(DATA / f"asr_{slug}_{voice}.wav")) == []


@needs_models
@pytest.mark.parametrize("voice", [1, 2])
def test_name_alone_has_an_empty_command(recogniser, voice):
    hits = listen(recogniser, read_wav(DATA / f"asr_seul_{voice}.wav"))
    assert [h.command for h in hits] == [""], [h.text for h in hits]


@needs_models
def test_recall_on_the_task16_positives(recogniser):
    """lapinou_*.wav : « Lapinou » seul en voix « low » bruitées, difficiles pour une
    transcription (mesure : docs/lapinou/mot-activation.md). Au plus une détection chacun,
    toujours sans commande, et un plancher de rappel pour voir une régression."""
    found = [listen(recogniser, read_wav(p)) for p in sorted(DATA.glob("lapinou_*.wav"))]
    assert all(len(hits) <= 1 for hits in found)
    detected = [h for hits in found for h in hits]
    assert all(h.command == "" for h in detected), [h.text for h in detected]
    assert len(detected) >= 2, f"{len(detected)}/10"


@needs_models
def test_twelve_minutes_of_negatives_have_no_detection(recogniser):
    pieces = negatives()
    assert sum(len(p) for _, p in pieces) >= 12 * 60 * SR
    detector = AsrWakeDetector(recogniser)
    hits = []
    for name, pcm in pieces:
        for i in range(0, len(pcm), GATEWAY_BLOCK):
            hits += [(name, h.text) for h in detector.feed(pcm[i : i + GATEWAY_BLOCK])]
        hits += [(name, h.text) for h in detector.feed(TAIL)]
        detector.reset()
    assert hits == []
    assert detector.transcribed_s > 0  # la parole a bien été transcrite
