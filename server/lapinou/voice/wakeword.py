# SPDX-License-Identifier: MIT
"""Mot d'activation « Lapinou » : inférence openWakeWord (ONNX Runtime).

Chaîne openWakeWord : PCM 16 kHz -> mélspectrogramme (32 bandes, pas de 10 ms) ->
plongement de 96 valeurs toutes les 80 ms (fenêtre de 76 trames mél) -> notre
classifieur sur les 16 derniers plongements (environ 2 s d'audio) -> score 0..1.

Les deux modèles de caractéristiques d'openWakeWord (melspectrogram.onnx,
embedding_model.onnx) ne sont pas dans le dépôt : `download_feature_models()` les
télécharge (URL épinglées, SHA-256 vérifié) dans server/models/openwakeword/.
Commande : `uv run python -m lapinou.voice.wakeword download`.
Le classifieur n'est pas distribué (ses données d'entraînement sont sous licences NC/SA) :
chacun l'entraîne avec server/tools/train_wakeword/ (README) et l'indique dans
`voice.wake_model` ; `load_wakeword()` le charge ou explique pourquoi c'est impossible.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from functools import lru_cache
from pathlib import Path

import numpy as np
import onnxruntime as ort

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
DEFAULT_MODEL = MODELS_DIR / "lapinou_fr.onnx"  # emplacement conseillé (ignoré par git)
RECIPE = "server/tools/train_wakeword/README.md"
FEATURE_DIR = MODELS_DIR / "openwakeword"

_OWW = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
FEATURE_MODELS = {
    "melspectrogram.onnx": (
        _OWW + "melspectrogram.onnx",
        "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    ),
    "embedding_model.onnx": (
        _OWW + "embedding_model.onnx",
        "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    ),
}

SAMPLE_RATE = 16000
CHUNK = 1280  # 80 ms : un plongement par bloc
MEL_CONTEXT = 480  # échantillons du bloc précédent donnés au mélspectrogramme
MEL_WINDOW = 76  # trames mél par plongement
MEL_PER_CHUNK = 8
N_FEATURES = 16  # plongements vus par le classifieur
PRIME_S = 2.0  # silence de départ (état après reset)


def feature_models_present(feature_dir: Path = FEATURE_DIR) -> bool:
    return all((feature_dir / name).is_file() for name in FEATURE_MODELS)


def download_feature_models(feature_dir: Path = FEATURE_DIR) -> None:
    """Télécharge les modèles de caractéristiques manquants et vérifie leur SHA-256."""
    feature_dir.mkdir(parents=True, exist_ok=True)
    for name, (url, sha256) in FEATURE_MODELS.items():
        target = feature_dir / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == sha256:
            continue
        with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 (URL fixe)
            data = response.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            raise RuntimeError(f"{name} : SHA-256 {digest} au lieu de {sha256}")
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)


def _session(path: Path, threads: int) -> ort.InferenceSession:
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


class FeatureModels:
    """Les deux modèles openWakeWord, partageables entre lapins."""

    def __init__(self, feature_dir: Path = FEATURE_DIR, threads: int = 1) -> None:
        if not feature_models_present(feature_dir):
            raise FileNotFoundError(
                f"modèles openWakeWord absents de {feature_dir} "
                "(uv run python -m lapinou.voice.wakeword download)"
            )
        self._mel = _session(feature_dir / "melspectrogram.onnx", threads)
        self._emb = _session(feature_dir / "embedding_model.onnx", threads)
        self._primed: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    def melspec(self, frames: np.ndarray) -> np.ndarray:
        """(n, CHUNK + MEL_CONTEXT) échantillons -> (n * 8, 32) trames mél.

        Un appel par bloc : le modèle borne la dynamique (top_db) sur tout son tenseur
        d'entrée, un lot changerait donc le résultat. Bloc par bloc, comme le flux
        d'openWakeWord, le résultat ne dépend pas du découpage de l'audio.
        """
        out = [self._mel.run(None, {"input": f[None, :].astype(np.float32)})[0] for f in frames]
        return np.concatenate(out).reshape(-1, 32) / 10.0 + 2.0  # transformation openWakeWord

    def embed(self, windows: np.ndarray) -> np.ndarray:
        """(n, 76, 32) trames mél -> (n, 96) plongements."""
        out = self._emb.run(None, {"input_1": windows[..., None].astype(np.float32)})[0]
        return out.reshape(-1, 96)

    def primed_state(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(queue audio, tampon mél, plongements) après PRIME_S secondes de silence."""
        if self._primed is None:
            stream = FeatureStream(self, prime=False)
            stream.mel = np.ones((MEL_WINDOW, 32), np.float32)  # départ openWakeWord
            feats = stream.push(np.zeros(int(PRIME_S * SAMPLE_RATE), np.int16))
            self._primed = (stream.tail, stream.mel, feats[-N_FEATURES:])
        tail, mel, feats = self._primed
        return tail.copy(), mel.copy(), feats.copy()


class FeatureStream:
    """PCM en continu -> un plongement par bloc de 80 ms (calcul par lots)."""

    def __init__(self, models: FeatureModels, prime: bool = True) -> None:
        self.models = models
        self.pending = np.zeros(0, np.int16)
        self.tail = np.zeros(MEL_CONTEXT, np.int16)
        self.mel = np.zeros((0, 32), np.float32)
        self.primed_features = np.zeros((0, 96), np.float32)
        if prime:
            self.tail, self.mel, self.primed_features = models.primed_state()

    def push(self, pcm: np.ndarray) -> np.ndarray:
        """Les nouveaux plongements (k, 96) produits par ces échantillons."""
        buf = np.concatenate((self.pending, np.asarray(pcm).astype(np.int16, copy=False)))
        n = len(buf) // CHUNK
        self.pending = buf[n * CHUNK :]
        if n == 0:
            return np.zeros((0, 96), np.float32)
        audio = np.concatenate((self.tail, buf[: n * CHUNK]))
        self.tail = audio[-MEL_CONTEXT:]
        index = np.arange(n)[:, None] * CHUNK + np.arange(CHUNK + MEL_CONTEXT)[None, :]
        mel = np.concatenate((self.mel, self.models.melspec(audio[index])))
        ends = len(mel) - MEL_PER_CHUNK * np.arange(n - 1, -1, -1)
        windows = np.stack([mel[e - MEL_WINDOW : e] for e in ends])
        self.mel = mel[-MEL_WINDOW:]
        return self.models.embed(windows)


def clip_features(models: FeatureModels, pcm: np.ndarray, piece: int = CHUNK * 256) -> np.ndarray:
    """Tous les plongements d'un enregistrement, depuis l'état « silence » (entraînement)."""
    stream = FeatureStream(models)
    rows = [stream.primed_features]
    for start in range(0, len(pcm), piece):
        rows.append(stream.push(pcm[start : start + piece]))
    return np.concatenate(rows)


@lru_cache(maxsize=4)
def _shared_models(feature_dir: Path) -> FeatureModels:
    return FeatureModels(feature_dir)


class WakeWordUnavailable(RuntimeError):
    """Le détecteur openWakeWord ne peut pas être chargé (modèle absent, non configuré)."""


class WakeWord:
    """Détecteur « Lapinou » pour un flux (un par lapin)."""

    def __init__(self, model_path: Path, threshold: float, feature_dir: Path = FEATURE_DIR) -> None:
        if not Path(model_path).is_file():
            raise WakeWordUnavailable(
                f"classifieur « Lapinou » absent : {model_path} (il n'est pas distribué ; "
                f"l'entraîner avec la recette de {RECIPE})"
            )
        if not feature_models_present(Path(feature_dir)):
            raise WakeWordUnavailable(
                f"modèles openWakeWord absents de {feature_dir} "
                "(uv run python -m lapinou.voice.wakeword download)"
            )
        self.threshold = threshold
        self._models = _shared_models(Path(feature_dir))
        self._model = _session(Path(model_path), 1)
        self._input = self._model.get_inputs()[0].name
        self.reset()

    def reset(self) -> None:
        """Oublie l'audio passé (après une conversation) : repart d'un silence."""
        self._stream = FeatureStream(self._models)
        self._features = self._stream.primed_features
        self._armed = True
        self.last_score = 0.0

    def feed(self, pcm: np.ndarray) -> float | None:
        """Ajoute des échantillons int16 16 kHz ; renvoie le score si « Lapinou » est détecté.

        Une détection par passage au-dessus du seuil : le score doit redescendre sous le
        seuil avant qu'une nouvelle détection soit possible.
        """
        pcm = np.asarray(pcm)
        if pcm.dtype != np.int16 or pcm.ndim != 1:
            raise ValueError(f"PCM int16 mono (1-D) attendu, reçu {pcm.dtype} de forme {pcm.shape}")
        new = self._stream.push(pcm)
        if len(new) == 0:
            return None
        feats = np.concatenate((self._features, new))
        self._features = feats[-N_FEATURES:]
        start = len(feats) - N_FEATURES - len(new) + 1
        windows = np.stack([feats[s : s + N_FEATURES] for s in range(start, start + len(new))])
        scores = self._model.run(None, {self._input: windows.astype(np.float32)})[0].reshape(-1)
        detected: float | None = None
        for score in scores:
            if score >= self.threshold:
                if self._armed:
                    detected = max(detected or 0.0, float(score))
                    self._armed = False
                elif detected is not None:
                    detected = max(detected, float(score))
            else:
                self._armed = True
        self.last_score = float(scores[-1])
        return detected


def load_wakeword(voice) -> WakeWord:
    """Le détecteur de la configuration `voice` (lapinou.config.VoiceConfig).

    Lève WakeWordUnavailable avec la marche à suivre si `voice.wake_model` est vide ou
    si un modèle manque.
    """
    if not voice.wake_model:
        raise WakeWordUnavailable(
            "voice.wake_model n'est pas renseigné : le classifieur « Lapinou » n'est pas "
            f"distribué, l'entraîner avec la recette de {RECIPE} puis indiquer son chemin"
        )
    return WakeWord(Path(voice.wake_model).expanduser(), voice.wake_threshold)


def main(argv: list[str]) -> int:
    if argv[1:] == ["download"]:
        download_feature_models()
        print(f"modèles openWakeWord dans {FEATURE_DIR}")
        return 0
    print("usage : python -m lapinou.voice.wakeword download", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
