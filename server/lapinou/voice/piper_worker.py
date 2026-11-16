# SPDX-License-Identifier: MIT
"""Worker Piper : lancé par `tts.PiperProcess` avec le Python du venv Piper (R30), jamais importé
par le serveur (piper-tts est GPL-3.0-or-later). Ne dépend que de la bibliothèque standard et
de `piper`.

    python -I piper_worker.py MODELE.onnx

stdin : une ligne JSON `{"text": "..."}` par phrase. stdout : `{"ready": true}` une fois la voix
chargée, puis pour chaque phrase `{"rate": 22050, "bytes": N}` suivi de N octets de PCM int16
mono, ou `{"error": "..."}`. Le texte n'est jamais écrit sur stderr (vie privée).
"""

from __future__ import annotations

import json
import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage : piper_worker.py MODELE.onnx", file=sys.stderr)
        return 2
    # stdout est réservé au protocole : tout print() égaré de la bibliothèque part sur stderr.
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    from piper import PiperVoice

    voice = PiperVoice.load(argv[1])
    out.write(b'{"ready": true}\n')
    for line in sys.stdin.buffer:
        try:
            text = json.loads(line)["text"]
            chunks = list(voice.synthesize(text))
            if not chunks:
                raise RuntimeError("rien synthétisé")
            pcm = b"".join(chunk.audio_int16_bytes for chunk in chunks)
            out.write(json.dumps({"rate": chunks[0].sample_rate, "bytes": len(pcm)}).encode())
            out.write(b"\n" + pcm)
        except Exception as error:  # noqa: BLE001 : le worker reste vivant pour la phrase suivante
            out.write(json.dumps({"error": type(error).__name__ + ": " + str(error)[:80]}).encode())
            out.write(b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
