# SPDX-License-Identifier: MIT
"""Téléchargement des modèles : transcription (Parakeet TDT 0.6B v3 int8 + Silero VAD) et voix
Piper `fr_FR-siwis-medium` (R31, révision et SHA-256 épinglés dans `tts.PIPER_VOICES`).
`python -m lapinou.voice.models piper-setup` crée le venv séparé de Piper (R30).

`python -m lapinou.voice.models fetch [--dir DOSSIER]` (défaut : ~/.cache/lapinlibre/asr, là
où `Recogniser` de LapinLibre les cherche). Les SHA-256 attendus sont ceux de
`asr_server.MODEL_SHA256` (importés, R4) ; `asr_server.py --fetch` est laissé tel quel mais
ne marche plus (R25) : l'archive de la release GitHub a été republiée avec un autre export du
modèle. Ici les trois fichiers Parakeet viennent de la révision Hugging Face épinglée qui a
exactement ces SHA-256 ; `tokens.txt` de la même révision ; `silero_vad.onnx` de la release
sherpa-onnx. Un fichier dont le SHA-256 diffère est refusé et rien n'est conservé.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

from lapinou.voice import tts, vad

asr_server = vad.asr_server

HF_REVISION = "2bda32ec70b097a55adaa07d9a7173915b43cc78"
HF = f"https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/resolve/{HF_REVISION}/"


def sources() -> dict[str, str]:
    """Chemin relatif au dossier des modèles -> URL (fichiers de `asr_server.MODEL_SHA256`)."""
    prefix = asr_server.PARAKEET + "/"
    return {
        name: (HF + name[len(prefix) :]) if name.startswith(prefix) else asr_server.RELEASES + name
        for name in asr_server.MODEL_SHA256
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path, want: str, opener: Callable) -> None:
    """Télécharge `url` vers `target` si absent ou altéré ; RuntimeError (et rien conservé) si
    le SHA-256 ne correspond pas."""
    if target.is_file() and _sha256(target) == want:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with opener(url, timeout=60) as response, part.open("wb") as f:
            for block in iter(lambda: response.read(1 << 20), b""):
                digest.update(block)
                f.write(block)
        if digest.hexdigest() != want:
            raise RuntimeError(f"{target.name} : SHA-256 {digest.hexdigest()} au lieu de {want}")
        part.replace(target)
    finally:
        part.unlink(missing_ok=True)


def fetch(directory: Path | None = None, opener: Callable = urllib.request.urlopen) -> Path:
    """Télécharge les fichiers manquants ou altérés ; RuntimeError si un SHA-256 ne correspond
    pas (le fichier partiel est supprimé). Rend le dossier."""
    directory = Path(directory or asr_server.MODELS)
    for name, url in sources().items():
        _download(url, directory / name, asr_server.MODEL_SHA256[name], opener)
    asr_server.check_models(directory)
    vad._models_present.cache_clear()
    return directory


PIPER_HF = "https://huggingface.co/rhasspy/piper-voices/resolve/" + tts.PIPER_REVISION


def piper_sources(voice: str) -> dict[str, str]:
    """Nom de fichier -> URL (révision épinglée de rhasspy/piper-voices) pour une voix de
    `tts.PIPER_VOICES` ; `fr_FR-siwis-medium` est dans `fr/fr_FR/siwis/medium/`."""
    lang_region, speaker, quality = voice.split("-")
    base = f"{PIPER_HF}/{lang_region.split('_')[0]}/{lang_region}/{speaker}/{quality}/"
    return {name: base + name for name in tts.PIPER_VOICES[voice]}


def fetch_piper(
    voice: str = tts.DEFAULT_PIPER_VOICE,
    directory: Path | None = None,
    opener: Callable = urllib.request.urlopen,
) -> Path:
    """Télécharge la voix Piper (.onnx et .onnx.json, SHA-256 vérifiés). Rend le dossier."""
    directory = Path(directory or tts.PIPER_MODELS)
    for name, url in piper_sources(voice).items():
        _download(url, directory / name, tts.PIPER_VOICES[voice][name], opener)
    return directory


PIPER_TTS_VERSION = "1.8.0"


def piper_setup(venv: Path | None = None, run: Callable = subprocess.run) -> Path:
    """Crée le venv séparé de Piper (piper-tts est GPL-3.0-or-later, R30 : le serveur MIT ne
    l'importe pas, il l'exécute) ; rend le Python du venv. Avec uv si présent, sinon venv + pip."""
    venv = Path(venv or tts.PIPER_VENV)
    python = venv / "bin" / "python"
    package = f"piper-tts=={PIPER_TTS_VERSION}"
    uv = shutil.which("uv")
    if uv:
        run([uv, "venv", str(venv)], check=True)
        run([uv, "pip", "install", "-p", str(python), package], check=True)
    else:
        run([sys.executable, "-m", "venv", str(venv)], check=True)
        run([str(python), "-m", "pip", "install", package], check=True)
    return python


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args[:1] == ["fetch"] and len(args) in (1, 3) and (len(args) == 1 or args[1] == "--dir"):
        try:
            directory = fetch(Path(args[2]) if len(args) == 3 else None)
            fetch_piper(tts.DEFAULT_PIPER_VOICE)
        except (OSError, RuntimeError) as error:  # réseau coupé, SHA-256 différent, disque plein
            print(f"échec du téléchargement : {error}", file=sys.stderr)
            return 1
        print(f"modèles dans {directory} ; voix Piper dans {tts.PIPER_MODELS}")
        return 0
    if args == ["piper-setup"]:
        try:
            python = piper_setup()
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"échec de l'installation de Piper : {error}", file=sys.stderr)
            return 1
        print(f"Python de Piper : {python}")
        return 0
    print(
        "usage : python -m lapinou.voice.models fetch [--dir DOSSIER] | piper-setup",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
