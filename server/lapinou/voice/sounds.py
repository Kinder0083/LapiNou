# SPDX-License-Identifier: MIT
"""Reconnaissance des sons d'un enregistrement de surveillance : CED mini (AudioSet, 527 sons),
par sherpa-onnx, sur le processeur du serveur (≈ 0,02 s pour 10 s de son).

Le modèle rend les sons d'AudioSet avec leur probabilité ; `describe` les regroupe en
catégories françaises (`CATEGORIES`) et choisit l'alerte la plus grave (`ALERTS`, dans l'ordre).
Le modèle est téléchargé par `python -m lapinou.voice.models fetch` (archive de la release
sherpa-onnx, SHA-256 épinglés) dans le dossier des modèles de transcription ; sans lui, les
enregistrements partent sans sons reconnus.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

MODEL_DIR = "sherpa-onnx-ced-mini-audio-tagging-2024-04-19"
MODEL = f"{MODEL_DIR}/model.int8.onnx"
LABELS = f"{MODEL_DIR}/class_labels_indices.csv"
TOP_K = 10

# Catégorie -> (nom dit dans Home Assistant, sons d'AudioSet qui en sont).
CATEGORIES: dict[str, tuple[str, frozenset[str]]] = {
    "detonation": ("détonation", frozenset({"Explosion", "Gunshot, gunfire"})),
    "verre": ("verre brisé", frozenset({"Glass", "Shatter", "Breaking", "Smash, crash"})),
    "alarme": ("alarme", frozenset({"Alarm", "Smoke detector, smoke alarm", "Fire alarm",
                                    "Siren", "Civil defense siren", "Buzzer", "Car alarm"})),
    "cris": ("cris", frozenset({"Shout", "Yell", "Screaming", "Children shouting"})),
    "pleurs": ("pleurs", frozenset({"Crying, sobbing", "Baby cry, infant cry"})),
    "coups": ("coups ou porte", frozenset({"Knock", "Slam", "Bang", "Thump, thud", "Door",
                                           "Sliding door"})),
    "chien": ("chien", frozenset({"Dog", "Bark", "Howl", "Whimper (dog)",
                                  "Canidae, dogs, wolves"})),
    "chat": ("chat", frozenset({"Cat", "Meow", "Caterwaul"})),
    "sonnette": ("sonnette", frozenset({"Doorbell", "Ding-dong"})),
    "telephone": ("téléphone", frozenset({"Telephone", "Telephone bell ringing", "Ringtone"})),
    "pas": ("pas", frozenset({"Walk, footsteps"})),
    "parole": ("parole", frozenset({"Speech", "Male speech, man speaking",
                                    "Female speech, woman speaking", "Child speech, kid speaking",
                                    "Conversation", "Hubbub, speech noise, speech babble"})),
    "rires": ("rires", frozenset({"Laughter", "Baby laughter"})),
    "toux": ("toux", frozenset({"Cough"})),
    "musique": ("musique", frozenset({"Music", "Musical instrument"})),
    "television": ("télévision", frozenset({"Television"})),
    "eau": ("eau", frozenset({"Water", "Water tap, faucet", "Drip"})),
    "aspirateur": ("aspirateur", frozenset({"Vacuum cleaner"})),
}
# Les catégories qui sont une alerte, de la plus grave à la moins grave.
ALERTS = ("detonation", "verre", "alarme", "cris", "pleurs", "coups", "chien")


@dataclass
class Sounds:
    sons: list[str]  # noms français des catégories reconnues, la plus probable d'abord
    alerte: str  # le nom de l'alerte la plus grave, ou ""
    etiquettes: list[tuple[str, float]]  # les sons d'AudioSet les plus probables (3 au plus)


def describe(tags: list[tuple[str, float]], min_prob: float) -> Sounds:
    """`tags` : (son d'AudioSet, probabilité), du plus probable au moins probable."""
    seen: dict[str, float] = {}
    for label, prob in tags:
        if prob < min_prob:
            continue
        for key, (_, labels) in CATEGORIES.items():
            if label in labels and key not in seen:
                seen[key] = prob
    keys = sorted(seen, key=lambda k: -seen[k])
    alert = next((k for k in ALERTS if k in seen), "")
    return Sounds(
        sons=[CATEGORIES[k][0] for k in keys],
        alerte=CATEGORIES[alert][0] if alert else "",
        etiquettes=[(label, round(float(prob), 2)) for label, prob in tags[:3]],
    )


def model_present(directory: Path) -> bool:
    return (Path(directory) / MODEL).is_file() and (Path(directory) / LABELS).is_file()


class SoundTagger:
    """Le modèle, chargé au premier usage ; `tags(samples int16 16 kHz)` rend les TOP_K sons
    les plus probables. Un seul calcul à la fois (sherpa-onnx n'est pas sûr entre fils)."""

    def __init__(self, directory: Path, threads: int = 2) -> None:
        self.directory = Path(directory)
        self.threads = threads
        self._tagger: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        import sherpa_onnx

        config = sherpa_onnx.AudioTaggingConfig(
            model=sherpa_onnx.AudioTaggingModelConfig(
                ced=str(self.directory / MODEL), num_threads=self.threads, provider="cpu"),
            labels=str(self.directory / LABELS), top_k=TOP_K)
        return sherpa_onnx.AudioTagging(config)

    def tags(self, samples: np.ndarray, rate: int = 16000) -> list[tuple[str, float]]:
        with self._lock:
            if self._tagger is None:
                self._tagger = self._load()
            stream = self._tagger.create_stream()
            stream.accept_waveform(rate, np.asarray(samples, np.float32) / 32768.0)
            return [(e.name, float(e.prob)) for e in self._tagger.compute(stream)]
