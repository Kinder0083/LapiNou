# SPDX-License-Identifier: MIT
"""A fake LapiNou audio server (TCP 9300) for tests/scenarios/lapinou_mic.py.

  python3 -I tests/fakes/fake_audio_server.py CTRL PORT

Run inside the daemon's network namespace (nsenter). It listens on 127.0.0.1:PORT and reads the
frames of docs/lapinou/protocole-audio.md: a 4-byte big-endian length, then a LapinouAudio, decoded
with tools/app-server/asr_server.py's field reader as server/lapinou/rabbit/audio_gateway.py does,
with the same checks (frames of at most 65536 bytes, a Hello in the first frame of a connection with
sample_rate 16000 and the frame's rabbit_id). Each frame becomes a line of CTRL/log (and a
connection whose first frame passes those checks "connection N: hello first"); the mu-law bytes
of every connection are appended to CTRL/audio. CTRL/ready appears once it listens.
"""

import os
import re
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools", "app-server"))
import asr_server  # noqa: E402  (fields, as the server uses it)

MAX_FRAME = 64 * 1024


def message(data, spec):
    """{name: value} of the fields named in spec ({number: (name, wire)})."""
    out = {}
    for number, wire, value in asr_server.fields(data):
        if number in spec and spec[number][1] == wire:
            out[spec[number][0]] = value
    return out


FRAME = {1: ("rabbit_id", 2), 2: ("seq", 0), 3: ("mulaw", 2), 4: ("first_sample", 0), 5: ("mark", 2),
         6: ("hello", 2)}
HELLO = {1: ("rabbit_id", 2), 2: ("firmware_version", 2), 3: ("sample_rate", 0)}
MARK = {1: ("playback_id", 2), 2: ("at_sample", 0), 3: ("start", 0)}


def serve(ctrl, port):
    lock = threading.Lock()
    logf = open(os.path.join(ctrl, "log"), "a", buffering=1)
    audio = open(os.path.join(ctrl, "audio"), "ab", buffering=0)
    # Connections are numbered on from the ones a previous run of the server logged.
    count = [sum(1 for line in open(os.path.join(ctrl, "log")) if re.fullmatch(r"connection \d+\n?", line))]

    def log(line):
        with lock:
            logf.write(line + "\n")

    def read_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def session(conn, number):
        log(f"connection {number}")
        first = True
        try:
            while True:
                header = read_exact(conn, 4)
                if header is None:
                    break
                size = int.from_bytes(header, "big")
                if size > MAX_FRAME:
                    log(f"refused: frame of {size} bytes")
                    break
                body = read_exact(conn, size)
                if body is None:
                    log("truncated frame")
                    break
                f = message(body, FRAME)
                if "rabbit_id" not in f or "seq" not in f:
                    log("refused: no rabbit_id or seq")
                    break
                parts = [f"seq={f['seq']}"]
                if "hello" in f:
                    h = message(f["hello"], HELLO)
                    parts.append(f"hello(rabbit_id={h.get('rabbit_id', b'').decode()}, "
                                 f"firmware_version={h.get('firmware_version', b'').decode()}, "
                                 f"sample_rate={h.get('sample_rate')})")
                    if h.get("sample_rate") != 16000 or h.get("rabbit_id") != f["rabbit_id"]:
                        log("refused: bad hello " + " ".join(parts))
                        break
                elif first:
                    log("refused: first frame without hello " + " ".join(parts))
                    break
                if first:
                    log(f"connection {number}: hello first")
                first = False
                if "mulaw" in f:
                    audio.write(f["mulaw"])
                    parts.append(f"mulaw={len(f['mulaw'])} first_sample={f.get('first_sample')}")
                if "mark" in f:
                    m = message(f["mark"], MARK)
                    parts.append(f"mark({m.get('playback_id', b'').decode()}, at_sample={m.get('at_sample')}, "
                                 f"start={bool(m.get('start'))})")
                log(f"frame rabbit_id={f['rabbit_id'].decode()} " + " ".join(parts))
        except OSError as e:
            log(f"error {e}")
        log(f"closed {number}")
        conn.close()

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # A small receive window (inherited by the connections): when the server is stopped (SIGSTOP,
    # tests/scenarios/lapinou_mic.py), the daemon's sends stop after a few kilobytes, as when the
    # Wi-Fi drops.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    s.bind(("127.0.0.1", port))
    s.listen(8)
    open(os.path.join(ctrl, "ready"), "w").close()
    while True:
        conn, _ = s.accept()
        count[0] += 1
        threading.Thread(target=session, args=(conn, count[0]), daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    serve(sys.argv[1], int(sys.argv[2]))
