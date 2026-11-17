# SPDX-License-Identifier: MIT
"""lapinou-mic: the microphone streamed to the LapiNou server, and shared on a local socket.

The daemon's settings are the scenario's own (LAPINOU_CONF_DIR): [server] enabled=1 and
host=127.0.0.1 (both are needed, R49: see lapinou_mic_no_server.py), and no
[audio] mic_gain_db, so the default gain, +12 dB, applies. Its uuid.conf (LAPINOU_UUID_FILE) and its
local socket (LAPINOU_MIC_SOCKET) are in the run's directory. /dev/audio is a FIFO the scenario
keeps open for writing, so what it writes is what the microphone "hears"; its OSS ioctls (type 'P')
are faked.

The fake server (tests/fakes/fake_audio_server.py) runs inside the daemon's network namespace
(nsenter, as in voice_daemon.py) on 127.0.0.1:9300 and logs each frame it decodes; the record
gathers runs of consecutive audio frames into one line ("audio seq A-B: samples X-Y", Y excluded;
the connection's first frame, which carries the Hello, is shown on its own).

  1. The first frame of a connection carries the Hello (sample_rate 16000, rabbit_id from uuid.conf,
     firmware_version the build stamp /karotz/etc/lapinlibre-build).
  2. 4096 bytes written to the microphone: 4096 mu-law bytes reach the server, which are the written
     bytes with the gain applied (decoded, scaled by 10^(12/20) in Q12, clipped, encoded again).
  3. mark_playback("p1", true) then ("p1", false) after 2048 more bytes: mark frames at the samples
     captured so far.
  4. The server killed: stream_state("disconnected"); 40960 bytes (2.56 s) written meanwhile, more
     than the 2 s buffer keeps; the server started again: the daemon reconnects (stream_state
     "connected"), the first frame carries the Hello and the oldest audio kept, and seq has jumped
     over the frames dropped.
  5. set_streaming(false): the connection is closed; two clients on the local socket receive the same
     bytes, the microphone's with the gain applied; set_streaming(true): a new connection, its Hello
     alone (nothing was kept while streaming was off).
  6. A Wi-Fi drop: the server stopped (SIGSTOP), so the connection stays open but takes nothing (the
     fake server's receive window is small, the daemon's send buffer 16 KB); 64 KB written: the
     daemon gives the connection up within 20 s (stream_state "disconnected"), then, the server
     continued (SIGCONT), connects again with a Hello. Only facts that don't depend on timing are
     recorded here.
  7. The local socket is mode 0666 (for clients that don't run as root).
"""

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DAEMON = "lapinou-mic"
DEVICES = ["audio"]
FAKE_IOCTL_TYPES = [0x50]  # OSS SNDCTL_DSP_*
SERVICE = "com.mindscape.karotz.LapinouMic"
PATH = "/com/mindscape/karotz/LapinouMic"
IFACE = "com.mindscape.karotz.LapinouMic"
UUID = "0e1f2a3b-4c5d-6e7f-8091-a2b3c4d5e6f7"
PORT = 9300
GAIN_DB = 12
FAKE_SERVER = Path(__file__).resolve().parent.parent / "fakes" / "fake_audio_server.py"


def sandbox_setup(workdir, sysroot):
    conf = Path(workdir) / "lapinou-conf"
    conf.mkdir()
    (conf / "lapinou.conf").write_text("[server]\nenabled=1\nhost=127.0.0.1\n")
    (Path(workdir) / "uuid.conf").write_text(UUID + "\n")
    return (f"export LAPINOU_CONF_DIR={conf} LAPINOU_UUID_FILE={workdir}/uuid.conf "
            f"LAPINOU_MIC_SOCKET={workdir}/run/mic.sock; ")


# --- G.711 mu-law and the gain, as the daemon computes them ------------------

def mulaw_decode(b):
    u = ~b & 0xFF
    t = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)
    return 0x84 - t if u & 0x80 else t - 0x84


def mulaw_encode(s):
    sign = 0
    if s < 0:
        sign, s = 0x80, -s
    s = min(s, 32635) + 0x84
    exp = 7
    while exp > 0 and not s & (0x80 << exp):
        exp -= 1
    return ~(sign | exp << 4 | (s >> (exp + 3)) & 0x0F) & 0xFF


def gain_table(db):
    factor = int(10 ** (db / 20) * 4096 + 0.5)
    return bytes(mulaw_encode(max(-32768, min(32767, (mulaw_decode(b) * factor) >> 12))) for b in range(256))


GAIN = gain_table(GAIN_DB)


def with_gain(data):
    return data.translate(GAIN)


# --- helpers ---------------------------------------------------------------

def wait_until(predicate, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


class Microphone:
    """The writing end of the /dev/audio FIFO, held open once the daemon has opened it."""

    def __init__(self, path):
        self.fd = None
        deadline = time.time() + 60
        while time.time() < deadline and self.fd is None:
            try:
                self.fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:  # ENXIO: the daemon hasn't opened it yet
                time.sleep(0.2)

    def write(self, data):
        while data:
            try:
                data = data[os.write(self.fd, data):]
            except BlockingIOError:
                time.sleep(0.05)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)


class FakeServer:
    def __init__(self, pid):
        self.pid = pid
        self.ctrl = tempfile.mkdtemp(prefix="karotz-mic-fake-")
        self.seen = 0
        self.proc = None
        assert os.readlink(f"/proc/{pid}/ns/net") != os.readlink("/proc/self/ns/net")

    def start(self):
        ready = os.path.join(self.ctrl, "ready")
        if os.path.exists(ready):
            os.unlink(ready)
        self.proc = subprocess.Popen(["nsenter", "-t", str(self.pid), "-U", "-n", "--preserve-credentials", "--",
                                      sys.executable, "-I", str(FAKE_SERVER), self.ctrl, str(PORT)],
                                     stdout=subprocess.DEVNULL, stderr=open(os.path.join(self.ctrl, "err"), "a"))
        return wait_until(lambda: os.path.exists(ready), 20)

    def kill(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None

    def lines(self):
        try:
            return open(os.path.join(self.ctrl, "log")).read().splitlines()
        except OSError:
            return []

    def audio(self):
        try:
            return open(os.path.join(self.ctrl, "audio"), "rb").read()
        except OSError:
            return b""

    def new_lines(self):
        lines = self.lines()
        new, self.seen = lines[self.seen:], len(lines)
        return new

    def stop(self):
        self.kill()
        shutil.rmtree(self.ctrl, ignore_errors=True)


AUDIO_FRAME = re.compile(r"frame rabbit_id=(\S+) seq=(\d+) mulaw=(\d+) first_sample=(\d+)$")


def summarise(lines, stamp):
    """The fake server's lines, runs of audio frames gathered, rabbit_id and the build stamp masked."""
    out, run = [], None

    def flush():
        if run:
            seqs = f"{run[0]}-{run[1]}" if run[1] > run[0] else f"{run[0]}"
            out.append(f"  audio seq {seqs}: samples {run[2]}-{run[3]}")

    for line in lines:
        m = AUDIO_FRAME.match(line)
        if m and m.group(1) == UUID:
            seq, n, first = int(m.group(2)), int(m.group(3)), int(m.group(4))
            if run and seq == run[1] + 1 and first == run[3]:
                run[1], run[3] = seq, first + n
            else:
                flush()
                run = [seq, seq, first, first + n]
            continue
        flush()
        run = None
        line = line.replace(f"rabbit_id={UUID}", "rabbit_id=<uuid.conf>")
        if stamp:
            line = line.replace(f"firmware_version={stamp},", "firmware_version=<build stamp>,")
        out.append("  " + line)
    flush()
    return out


def call(bus, method, *args):
    out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return f"{method}{args}: {' | '.join(lines) or 'ok'}"


def states(bus):
    return [s for s in bus.signals() if ".stream_state(" in s]


def receive(conn, n, timeout=20):
    conn.settimeout(timeout)
    data = b""
    try:
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    return data


def drive(d, bus):
    from harness import introspection

    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    record.append("== introspection")
    record.extend(introspection(bus, SERVICE, PATH))
    try:
        stamp = (Path(d.sysroot) / "karotz" / "etc" / "lapinlibre-build").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        stamp = ""
    mic = Microphone(d.fifos["audio"])
    record.append(f"microphone opened by the daemon: {mic.fd is not None}")
    fake = FakeServer(d.proc.pid)
    clients = []
    try:
        record.append("== 1. connection")
        record.append(f"fake server ready: {fake.start()}")
        wait_until(lambda: any(l.startswith("frame") for l in fake.lines()))
        wait_until(lambda: len(states(bus)) >= 1, 10)
        record.extend(summarise(fake.new_lines(), stamp))

        record.append("== 2. 4096 bytes from the microphone")
        first = bytes(range(256)) * 16
        mic.write(first)
        wait_until(lambda: len(fake.audio()) >= len(first))
        time.sleep(0.5)
        record.extend(summarise(fake.new_lines(), stamp))
        got = fake.audio()
        record.append(f"mu-law bytes received: {len(got)}, the microphone's with +{GAIN_DB} dB: {got == with_gain(first)}")

        record.append("== 3. playback marks")
        record.append(call(bus, "mark_playback", "string:p1", "boolean:true"))
        wait_until(lambda: any("mark(" in l for l in fake.lines()[fake.seen:]))
        second = bytes(range(255, -1, -1)) * 8
        mic.write(second)
        wait_until(lambda: len(fake.audio()) >= len(first) + len(second))
        record.append(call(bus, "mark_playback", "string:p1", "boolean:false"))
        wait_until(lambda: sum("mark(" in l for l in fake.lines()) >= 2)
        time.sleep(0.5)
        record.extend(summarise(fake.new_lines(), stamp))

        record.append("== 4. the server stopped, then started again")
        fake.kill()
        record.append(f"disconnected: {wait_until(lambda: len(states(bus)) >= 2)}")
        before = len(fake.audio())
        third = bytes((i * 7) & 0xFF for i in range(40960))
        mic.write(third)
        time.sleep(1)
        record.append(f"fake server ready: {fake.start()}")
        record.append(f"reconnected: {wait_until(lambda: len(states(bus)) >= 3, 40)}")
        wait_until(lambda: len(fake.audio()) - before >= 30720)
        time.sleep(0.5)
        record.extend(summarise(fake.new_lines(), stamp))
        kept = fake.audio()[before:]
        record.append(f"after the reconnection: {len(kept)} bytes, the last ones written with the gain: "
                      f"{with_gain(third).endswith(kept)}")

        record.append("== 5. streaming off, local clients, streaming on")
        record.append(call(bus, "set_streaming", "boolean:false"))
        wait_until(lambda: len(states(bus)) >= 4, 10)
        wait_until(lambda: any(l.startswith("closed") for l in fake.lines()[fake.seen:]), 10)
        record.extend(summarise(fake.new_lines(), stamp))
        path = Path(d.workdir) / "run" / "mic.sock"
        for _ in range(2):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(path))
            clients.append(c)
        # Until the daemon has accepted both: a short probe each time, read back by both.
        accepted = False
        deadline = time.time() + 30
        while not accepted and time.time() < deadline:
            mic.write(bytes(64))
            time.sleep(0.5)
            for c in clients:
                c.setblocking(False)
            try:
                accepted = all(c.recv(65536) for c in clients)
            except BlockingIOError:
                accepted = False
            for c in clients:
                c.setblocking(True)
        time.sleep(0.5)
        for c in clients:  # what is left of the probes
            c.setblocking(False)
            try:
                while c.recv(65536):
                    pass
            except BlockingIOError:
                pass
            c.setblocking(True)
        record.append(f"both clients accepted: {accepted}")
        block = bytes(range(128)) * 16
        mic.write(block)
        a, b = (receive(c, len(block)) for c in clients)
        record.append(f"client 1: {len(a)} bytes, client 2: {len(b)} bytes, the same: {a == b}, "
                      f"the microphone's with the gain: {a == with_gain(block)}")
        record.append(f"nothing streamed meanwhile: {fake.new_lines() == []}")
        record.append(call(bus, "set_streaming", "boolean:true"))
        wait_until(lambda: len(states(bus)) >= 5, 10)
        wait_until(lambda: any(l.startswith("frame") for l in fake.lines()[fake.seen:]), 10)
        time.sleep(0.5)
        record.extend(summarise(fake.new_lines(), stamp))

        record.append("== 6. a Wi-Fi drop: the server stops taking anything")
        fake.proc.send_signal(signal.SIGSTOP)
        stopped = time.time()
        mic.write(bytes((i * 13) & 0xFF for i in range(65536)))
        dropped = wait_until(lambda: len(states(bus)) >= 6, 30)
        took = time.time() - stopped
        fake.proc.send_signal(signal.SIGCONT)
        record.append(f"disconnected: {dropped}, within 20 s: {took < 20}")
        record.append(f"reconnected: {wait_until(lambda: len(states(bus)) >= 7, 30)}")

        def newest_has_hello():
            numbers = [int(m.group(1)) for m in map(re.compile(r"connection (\d+)$").match, fake.lines()) if m]
            return bool(numbers) and numbers[-1] > 3 and f"connection {numbers[-1]}: hello first" in fake.lines()
        record.append(f"the new connection's first frame carries the Hello: {wait_until(newest_has_hello, 10)}")
        fake.new_lines()

        record.append("== 7. the local socket")
        record.append(f"mode: {oct(path.stat().st_mode & 0o777)}")

        record.append("== 8. nothing but digital silence: the device is opened again")
        # Seen on a real rabbit: when the player ends a sound, the codec gives only zeros
        # (mu-law 0xFF) until /dev/audio is opened again.
        silence = bytes([0xFF])
        reopened = False
        deadline = time.time() + 15
        while not reopened and time.time() < deadline:
            try:
                mic.write(silence * 1600)  # once the daemon closed its end: EPIPE
            except BrokenPipeError:
                reopened = True
            time.sleep(0.1)
        record.append(f"the daemon closed the silent device: {reopened}")
        mic.close()
        mic = Microphone(d.fifos["audio"])
        record.append(f"and opened it again: {mic.fd is not None}")
    finally:
        for c in clients:
            c.close()
        mic.close()
        fake.stop()
    return record
