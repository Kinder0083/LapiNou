# SPDX-License-Identifier: MIT
"""Enregistre le micro d'un vrai lapin pour l'entraînement (exemples négatifs).

Le lapin envoie son micro au port 9300 du serveur : arrêtez d'abord le serveur LapiNou
(il occupe ce port), puis lancez depuis server/ par exemple

    uv run python tools/train_wakeword/record_negatives.py --rabbit salon --minutes 60 \
        --out ~/wakeword-train/real

Pendant l'enregistrement : télévision, radio, conversations, le lapin qui parle (TTS,
musique)... mais **personne ne dit « Lapinou »**. Les fichiers (WAV 16 kHz, PCM 16 bits,
une minute chacun) se donnent ensuite à `train.py features --real-negatives DOSSIER`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lapinou.rabbit.audio_gateway import SAMPLE_RATE, AudioGateway  # noqa: E402


def write_wav(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.astype("<i2").tobytes())


async def record(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    gateway = AudioGateway(args.host, args.port)
    await gateway.start()
    print(f"en attente du lapin {args.rabbit!r} sur le port {gateway.port}...", flush=True)
    try:
        stream = await gateway.stream(args.rabbit)
        target = int(args.minutes * 60 * SAMPLE_RATE)
        per_file = int(args.file_s * SAMPLE_RATE)
        parts: list[np.ndarray] = []
        buffered = written = 0
        async for chunk in stream.frames():
            parts.append(chunk.pcm)
            buffered += len(chunk.pcm)
            while buffered >= per_file or (written + buffered >= target and buffered):
                pcm = np.concatenate(parts)
                piece, rest = pcm[:per_file], pcm[per_file:]
                name = f"{args.rabbit}_{time.strftime('%Y%m%d-%H%M%S')}_{written // per_file:04d}.wav"
                write_wav(out / name, piece)
                written += len(piece)
                parts, buffered = [rest], len(rest)
                print(f"{name} ({written / SAMPLE_RATE / 60:.1f} / {args.minutes} min)", flush=True)
            if written >= target:
                return 0
        print("flux fermé avant la fin", file=sys.stderr)
        return 1
    finally:
        await gateway.stop()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rabbit", required=True, help="identifiant du lapin (rabbit_id du flux audio)")
    p.add_argument("--minutes", type=float, default=60.0)
    p.add_argument("--out", required=True)
    p.add_argument("--file-s", type=float, default=60.0, help="durée de chaque fichier")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9300)
    return asyncio.run(record(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
