# SPDX-License-Identifier: MIT
"""Mesure le coût de la détection de « Lapinou » par transcription (facteur temps réel, RAM).

Fait écouter au détecteur les négatifs des tests (12 min : parole française synthétique,
retournée, ralentie, accélérée) par blocs de la passerelle audio, comme un lapin, puis du
silence, et affiche : durée d'audio, temps de calcul, facteur temps réel (calcul / audio),
part de l'audio transcrite, mémoire maximale. À lancer sous un plafond de mémoire :

    cd server && systemd-run --user --scope -p MemoryMax=3500M -p MemorySwapMax=0 \
        uv run python tools/bench_wake_asr.py [--threads 2] [--repeat 1]
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

import numpy as np

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER / "tests"))

from test_wakeword import GATEWAY_BLOCK, SR, negatives  # noqa: E402

from lapinou.voice import wake_asr  # noqa: E402


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--threads", type=int, default=2, help="fils de calcul de Parakeet")
    parser.add_argument("--rabbits", type=int, default=1, help="détecteurs en parallèle (un par lapin)")
    parser.add_argument("--minutes", type=float, default=12.5, help="durée d'audio (par lapin) à écouter")
    parser.add_argument("--quiet", action="store_true",
                        help="écoute un fond sonore sans parole (coût du seul VAD) au lieu des négatifs")
    args = parser.parse_args()
    started = time.perf_counter()
    recogniser = wake_asr.load_recogniser(threads=args.threads)
    print(f"chargement : {time.perf_counter() - started:.1f} s, mémoire {rss_mb():.0f} Mo")
    if args.quiet:  # bruit rose très faible, comme les pauses des enregistrements de test
        rng = np.random.default_rng(5)
        white = np.fft.rfft(rng.standard_normal(60 * SR))
        freq = np.fft.rfftfreq(60 * SR)
        freq[0] = freq[1]
        pink = np.fft.irfft(white / np.sqrt(freq), 60 * SR)
        pieces = [("fond", (pink / pink.std() * 8).astype(np.int16))] * int(args.minutes)
    else:
        pieces = negatives()
    kept, budget = [], args.minutes * 60 * SR
    for name, pcm in pieces:
        if budget <= 0:
            break
        kept.append((name, pcm[: int(budget)]))
        budget -= len(pcm)
    pieces = kept
    detectors = [wake_asr.AsrWakeDetector(recogniser) for _ in range(args.rabbits)]
    tail = np.zeros(2 * SR, np.int16)
    audio_s = hits = 0
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for _, pcm in pieces:
        pcm = np.concatenate([pcm, tail])
        audio_s += len(pcm) / SR * len(detectors)
        for start in range(0, len(pcm), GATEWAY_BLOCK):
            for detector in detectors:  # les lapins parlent en même temps
                hits += len(detector.feed(pcm[start : start + GATEWAY_BLOCK]))
        for detector in detectors:
            detector.reset()
    wall, cpu = time.perf_counter() - wall0, time.process_time() - cpu0
    spoken = sum(d.transcribed_s for d in detectors)
    print(f"audio {audio_s / 60:.1f} min ({args.rabbits} flux), détections {hits}")
    print(f"calcul {wall:.1f} s (CPU {cpu:.1f} s) : facteur temps réel {wall / audio_s:.3f} "
          f"({audio_s / wall:.1f}x le temps réel)")
    print(f"parole transcrite {spoken / 60:.1f} min ({100 * spoken / audio_s:.0f} % de l'audio)")
    print(f"mémoire maximale {rss_mb():.0f} Mo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
