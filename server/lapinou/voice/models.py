# SPDX-License-Identifier: MIT
"""Téléchargement des modèles de la transcription (Parakeet TDT 0.6B v3 int8 + Silero VAD).

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
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path

from lapinou.voice import wake_asr

asr_server = wake_asr.asr_server

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


def fetch(directory: Path | None = None, opener: Callable = urllib.request.urlopen) -> Path:
    """Télécharge les fichiers manquants ou altérés ; RuntimeError si un SHA-256 ne correspond
    pas (le fichier partiel est supprimé). Rend le dossier."""
    directory = Path(directory or asr_server.MODELS)
    for name, url in sources().items():
        want = asr_server.MODEL_SHA256[name]
        target = directory / name
        if target.is_file() and _sha256(target) == want:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        try:
            with opener(url, timeout=60) as response, part.open("wb") as f:
                for block in iter(lambda: response.read(1 << 20), b""):
                    digest.update(block)
                    f.write(block)
            if digest.hexdigest() != want:
                raise RuntimeError(f"{name} : SHA-256 {digest.hexdigest()} au lieu de {want}")
            part.replace(target)
        finally:
            part.unlink(missing_ok=True)
    asr_server.check_models(directory)
    wake_asr._models_present.cache_clear()
    return directory


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args[:1] == ["fetch"] and len(args) in (1, 3) and (len(args) == 1 or args[1] == "--dir"):
        directory = fetch(Path(args[2]) if len(args) == 3 else None)
        print(f"modèles dans {directory}")
        return 0
    print("usage : python -m lapinou.voice.models fetch [--dir DOSSIER]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
