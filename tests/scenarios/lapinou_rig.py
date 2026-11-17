# SPDX-License-Identifier: MIT
"""What lapinou_shared_mic, lapinou_play_marks and lapinou_mic_restart share (not a scenario).

lapinou-mic is the scenario's own daemon (DAEMON, with /dev/audio a FIFO as in lapinou_mic.py);
voice-daemon and multimedia-daemon run beside it on the same bus, each in its own namespaces and
directory (voice/, multimedia/), without /dev/audio: they get the microphone from lapinou-mic's
socket. multimedia-daemon's karotz-player is tests/fakes/fake_karotz_player.py, and its encoder
(flac) a stub that keeps its input.
"""

import os
import random
import re
import sys
import threading
import time
from pathlib import Path

VOICE_SERVICE = "com.mindscape.karotz.Voice"
VOICE_PATH = "/com/mindscape/karotz/Voice"
MULTIMEDIA_SERVICE = "com.mindscape.karotz.Multimedia"
MULTIMEDIA_PATH = "/com/mindscape/karotz/Multimedia"
IFACE = "com.mindscape.karotz.KarotzInterface"
PLAYER_CTRL = "/tmp/karotz-player.ctrl"
FAKE_PLAYER = Path(__file__).resolve().parent.parent / "fakes" / "fake_karotz_player.py"


def write_stub(path, text):
    path.write_text(text)
    path.chmod(0o755)


class Feeder(threading.Thread):
    """Writes pseudo-random mu-law bytes to the microphone, and keeps what it wrote."""

    def __init__(self, mic):
        super().__init__(daemon=True)
        self.mic, self.rng, self.written = mic, random.Random(7), bytearray()
        self.running = True

    def run(self):
        while self.running:
            block = bytes(self.rng.randrange(256) for _ in range(1024))
            self.written += block
            try:
                self.mic.write(block)
            except OSError:  # nobody reads the microphone any more
                return
            time.sleep(0.02)

    def stop(self):
        self.running = False
        self.join(10)


def syslog_warnings(daemon, allowed):
    """The daemon's syslog warnings and worse that `allowed` (regular expressions) doesn't match."""
    path = Path(daemon.syslog)
    if not path.exists():
        return []
    patterns = [re.compile(p) for p in allowed]
    out = []
    for line in path.read_text(errors="replace").splitlines():
        level, _, message = line.partition(" ")
        if level.isdigit() and int(level) <= 4 and not any(p.search(message) for p in patterns):
            out.append(line)
    return out


def call(bus, service, path, method, *args):
    out = bus.send(f"--dest={service}", path, f"{IFACE}.{method}", *args)
    lines = [l.strip() for l in out.splitlines() if not l.startswith("method return")]
    return f"{method}{args}: {' | '.join(lines)}"


MARK = re.compile(r"mark\((\w+), at_sample=\d+, start=(True|False)\)")


def marks(fake):
    """The marks the fake audio server has received, without their (timing-dependent) samples."""
    found = []
    for line in fake.lines():
        m = MARK.search(line)
        if m:
            found.append(f"mark({m.group(1)}, start={m.group(2)})")
    return found


class Rig:
    """lapinou-mic (the scenario's daemon d) with voice-daemon and multimedia-daemon beside it."""

    def __init__(self, d, bus, voice_warnings=(), multimedia_warnings=()):
        from scenarios import lapinou_mic

        self.d, self.bus, self.lapinou_mic = d, bus, lapinou_mic
        self.work = Path(d.workdir)
        self.voice_warnings, self.multimedia_warnings = voice_warnings, multimedia_warnings
        self.mic_socket = f"{self.work}/run/mic.sock"
        self.record_out = self.work / "encoder-input"
        self.voice = self.multimedia = self.fakes = self.mic = self.fake_server = self.feeder = None
        self.extra = []  # lapinou-mic daemons started again

    # --- lapinou-mic and its microphone ---
    def start_microphone(self, d):
        """The microphone's FIFO of daemon d, a fake audio server in its namespace, a feeder."""
        self.mic = self.lapinou_mic.Microphone(d.fifos["audio"])
        self.fake_server = self.lapinou_mic.FakeServer(d.proc.pid)
        ready = self.fake_server.start()
        self.lapinou_mic.wait_until(lambda: os.path.exists(self.mic_socket), 20)
        self.feeder = Feeder(self.mic)
        self.feeder.start()
        return ready

    def stop_microphone(self):
        if self.feeder is not None:
            self.feeder.stop()
        if self.mic is not None:
            self.mic.close()
        if self.fake_server is not None:
            self.fake_server.stop()
        self.feeder = self.mic = self.fake_server = None

    # --- the other two daemons ---
    def start_clients(self):
        from harness import Daemon
        from scenarios import conf_fixture

        d, work = self.d, self.work
        vdir, mdir = work / "voice", work / "multimedia"
        vdir.mkdir()
        mdir.mkdir()
        self.voice = Daemon("voice-daemon", d.sysroot, d.build, vdir, [], self.bus, (), (),
                            conf_fixture.sandbox_setup(vdir, d.sysroot, voice=conf_fixture.VOICE_CONF)
                            + f"export LAPINOU_MIC_SOCKET={self.mic_socket}; ")
        stubs = mdir / "stub-bin"
        stubs.mkdir()
        write_stub(stubs / "mpd", "#!/bin/true\n")
        write_stub(stubs / "karotz-player", f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_PLAYER}" "$@"\n')
        write_stub(stubs / "flac", '#!/bin/sh\nexec /bin/cat > "$ENCODER_INPUT"\n')
        if os.path.lexists(PLAYER_CTRL):
            os.unlink(PLAYER_CTRL)
        open(PLAYER_CTRL, "w").close()
        self.multimedia = Daemon("multimedia-daemon", d.sysroot, d.build, mdir, [], self.bus, (), (),
                                 f"export LAPINOU_MIC_SOCKET={self.mic_socket} FAKE_PLAYER_CTRL={PLAYER_CTRL} "
                                 f"ENCODER_INPUT={self.record_out}; ")
        return (self.bus.wait_for_name(VOICE_SERVICE), self.bus.wait_for_name(MULTIMEDIA_SERVICE))

    def start_asr(self, grammar="oui | non"):
        """The fake ASR server (held open, saving the audio it gets) and an ASR session."""
        from scenarios import voice_daemon

        os.environ["KAROTZ_ASR_SAVE_AUDIO"] = "1"
        try:
            self.fakes = voice_daemon.FakeServers(self.voice.proc.pid)
        finally:
            del os.environ["KAROTZ_ASR_SAVE_AUDIO"]
        self.fakes.asr_mode("hold")
        self.saved = os.path.join(self.fakes.ctrl, "audio")
        return self.fakes.ready, call(self.bus, VOICE_SERVICE, VOICE_PATH, "asr_start_string", "string:s1",
                                      f"string:{grammar}", "string:fr-FR", "int32:0")

    def saved_size(self):
        return os.path.getsize(self.saved) if os.path.exists(self.saved) else 0

    def encoder_size(self):
        return self.record_out.stat().st_size if self.record_out.exists() else 0

    def warnings(self):
        return [f"voice-daemon: {syslog_warnings(self.voice, self.voice_warnings)}",
                f"multimedia-daemon: {syslog_warnings(self.multimedia, self.multimedia_warnings)}"]

    def stop(self):
        self.stop_microphone()
        for sub in (self.voice, self.multimedia, *self.extra):
            if sub is not None:
                sub.stop()
        if self.fakes is not None:
            self.fakes.stop()
        if os.path.lexists(PLAYER_CTRL):
            os.unlink(PLAYER_CTRL)
