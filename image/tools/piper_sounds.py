# SPDX-License-Identifier: MIT
"""LapiNou's spoken sounds (image/core/usr/karotz/sounds/), spoken by Piper's fr_FR-siwis-medium voice,
made once and kept in the repo (SOUNDS: the file names and their texts):

- lapinou-no-server.mp3: the controller's long press when neither the LapiNou server nor the cloud
  services can listen (docs/system.md, "The platform connection").

    python -I -B image/tools/piper_sounds.py --voices DIR [NAME.mp3 ...]

DIR holds the voice's fr_FR-siwis-medium.onnx and .onnx.json, checked against VOICE's sha256 before
use; fetch them once from VOICE_URL (Hugging Face, rhasspy/piper-voices). The Python running this needs
piper-tts (PIPER, its tested version); the host's ffmpeg encodes each sound as 64 kbit/s mono MP3 at
24 kHz, the rate and bit rate of the rabbit's other spoken sounds, which madplay plays. The samples are
evened to a peak of about -1 dB first, as speech_sounds.py does.

Licence: the voice was trained on the SIWIS French Speech Synthesis Database (CC BY 4.0), so the
sounds are CC BY 4.0 too: see README.md.
"""

import argparse
import array
import hashlib
import io
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "image" / "core" / "usr" / "karotz" / "sounds"
PIPER = "1.8.0"
VOICE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium/"
VOICE = {
    "fr_FR-siwis-medium.onnx": "b1d946fd68d0347155fa59bce0a018924abbef3c1561181db45ca463d83963ef",
    "fr_FR-siwis-medium.onnx.json": "df814aa3dfedd00510a07144442cc0a220ff21e43d03c3b078444fc0be68fc37",
}
SOUNDS = {
    "lapinou-no-server.mp3": "Je n'arrive pas à me connecter.",
}


def check_voice(voices):
    for name, want in VOICE.items():
        f = voices / name
        if not f.is_file():
            raise SystemExit(f"no {f}: fetch it from {VOICE_URL}{name}")
        got = hashlib.sha256(f.read_bytes()).hexdigest()
        if got != want:
            raise SystemExit(f"{f}: sha256 {got}, expected {want}")
    return voices / "fr_FR-siwis-medium.onnx"


def mp3(pcm, rate):
    """16-bit mono samples, evened to about -1 dB peak, as 64 kbit/s mono MP3 at 24 kHz."""
    samples = array.array("h", pcm)
    peak = max(1, max(abs(s) for s in samples))
    gain = 0.89 * 32767 / peak
    samples = array.array("h", (max(-32768, min(32767, round(s * gain))) for s in samples))
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
           "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", "-map_metadata", "-1",
           "-fflags", "+bitexact", "-flags:a", "+bitexact", "-write_xing", "0", "-id3v2_version", "0",
           "-f", "mp3", "pipe:1"]
    r = subprocess.run(cmd, input=samples.tobytes(), capture_output=True, timeout=120)
    if r.returncode != 0 or not r.stdout:
        raise SystemExit(f"ffmpeg failed: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stdout


def main(argv):
    p = argparse.ArgumentParser(description="LapiNou's spoken sounds, by Piper.")
    p.add_argument("--voices", type=Path, required=True, help="the folder holding fr_FR-siwis-medium.onnx(.json)")
    p.add_argument("names", nargs="*", help="only these sounds (default: all)")
    args = p.parse_args(argv)
    model = check_voice(args.voices)
    import importlib.metadata

    from piper import PiperVoice

    version = importlib.metadata.version("piper-tts")
    if version != PIPER:
        print(f"warning: piper-tts {version}, tested with {PIPER}", file=sys.stderr)
    voice = PiperVoice.load(str(model))
    for name in args.names or SOUNDS:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as w:
            voice.synthesize_wav(SOUNDS[name], w)
        buffer.seek(0)
        with wave.open(buffer, "rb") as w:
            rate, pcm = w.getframerate(), w.readframes(w.getnframes())
        data = mp3(pcm, rate)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes, {len(pcm) / 2 / rate:.1f} s at {rate} Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
