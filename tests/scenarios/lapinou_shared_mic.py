# SPDX-License-Identifier: MIT
"""lapinou-mic, voice-daemon and multimedia-daemon together: one microphone, shared.

lapinou-mic is the only program to open /dev/audio (a FIFO the scenario writes, as in lapinou_mic.py);
voice-daemon and multimedia-daemon get the microphone from its local socket (LAPINOU_MIC_SOCKET, in
the run's directory) and have no /dev/audio of their own. The scenario keeps writing pseudo-random
mu-law bytes to the microphone all the time (a daemon waits for its next block in its main loop, as on
the rabbit, where the microphone never stops). All three programs run on the same D-Bus bus, each in
its own namespaces and its own directory (voice/ and multimedia/ in the run's).

  1. asr_start_string: the fake ASR server (voice_daemon.py's, held open) receives the microphone as
     the daemon sends it, 8-bit at 8 kHz: every second byte of the stream lapinou-mic serves (its
     gain applied), in 1024-byte chunks.
  2. play, with a karotz-player stub that opens the sound device ($LAPINOU_DSP_DEVICE, a regular
     file in the run's directory) once it runs: multimedia-daemon calls mark_playback(id, true) when
     the player has the device open, then (id, false) after stop, and the same for a sound that ends
     by itself; the marks are what the fake audio server receives from lapinou-mic.
  3. record, while the ASR session goes on: the encoder (a stub that keeps its input) gets 16-bit
     unsigned samples, two bytes per microphone byte, from the same stream. (After the plays: the
     player would inherit the encoder's pipe and keep the encoder from ending.)

The other daemons' syslog warnings are checked here (the harness checks lapinou-mic's own).
"""

import os
import random
import re
import threading
import time
from pathlib import Path

DAEMON = "lapinou-mic"
DEVICES = ["audio"]
FAKE_IOCTL_TYPES = [0x50]  # OSS SNDCTL_DSP_*

VOICE_SERVICE = "com.mindscape.karotz.Voice"
VOICE_PATH = "/com/mindscape/karotz/Voice"
MULTIMEDIA_SERVICE = "com.mindscape.karotz.Multimedia"
MULTIMEDIA_PATH = "/com/mindscape/karotz/Multimedia"
IFACE = "com.mindscape.karotz.KarotzInterface"
PLAYER_CTRL = "/tmp/karotz-player.ctrl"

# Syslog warnings the other two daemons may log.
VOICE_WARNINGS = []
MULTIMEDIA_WARNINGS = []


def sandbox_setup(workdir, sysroot):
    from scenarios import lapinou_mic

    return lapinou_mic.sandbox_setup(workdir, sysroot)


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
            self.mic.write(block)
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


def drive(d, bus):
    from harness import Daemon
    from scenarios import conf_fixture, lapinou_mic, voice_daemon

    work = Path(d.workdir)
    wait_until = lapinou_mic.wait_until
    record = [f"== registered: {bus.wait_for_name(lapinou_mic.SERVICE)}"]
    mic = lapinou_mic.Microphone(d.fifos["audio"])
    record.append(f"microphone opened by lapinou-mic: {mic.fd is not None}")
    fake_server = lapinou_mic.FakeServer(d.proc.pid)
    feeder = Feeder(mic)
    voice = multimedia = fakes = None
    record_out = work / "encoder-input"
    dsp = work / "dsp"
    dsp.write_text("")
    dsp = dsp.resolve()  # what the player's descriptor reads back as
    mic_socket = f"{work}/run/mic.sock"
    try:
        record.append(f"fake audio server ready: {fake_server.start()}")
        wait_until(lambda: os.path.exists(mic_socket), 20)
        feeder.start()

        # The other two daemons: no /dev/audio, the socket instead.
        vdir, mdir = work / "voice", work / "multimedia"
        vdir.mkdir()
        mdir.mkdir()
        voice = Daemon("voice-daemon", d.sysroot, d.build, vdir, [], bus, (), (),
                       conf_fixture.sandbox_setup(vdir, d.sysroot, voice=conf_fixture.VOICE_CONF)
                       + f"export LAPINOU_MIC_SOCKET={mic_socket}; ")
        stubs = mdir / "stub-bin"
        stubs.mkdir()
        write_stub(stubs / "mpd", "#!/bin/true\n")
        write_stub(stubs / "karotz-player",
                   '#!/bin/sh\nexec 3>>"$LAPINOU_DSP_DEVICE"\ncase "$*" in *quick*) exec /bin/sleep 2;; '
                   '*) exec /bin/sleep 300;; esac\n')
        write_stub(stubs / "flac", '#!/bin/sh\nexec /bin/cat > "$ENCODER_INPUT"\n')
        if os.path.lexists(PLAYER_CTRL):
            os.unlink(PLAYER_CTRL)
        open(PLAYER_CTRL, "w").close()
        multimedia = Daemon("multimedia-daemon", d.sysroot, d.build, mdir, [], bus, (), (),
                            f"export LAPINOU_MIC_SOCKET={mic_socket} LAPINOU_DSP_DEVICE={dsp} "
                            f"ENCODER_INPUT={record_out}; ")
        record.append(f"voice-daemon registered: {bus.wait_for_name(VOICE_SERVICE)}")
        record.append(f"multimedia-daemon registered: {bus.wait_for_name(MULTIMEDIA_SERVICE)}")

        record.append("== 1. asr_start_string")
        os.environ["KAROTZ_ASR_SAVE_AUDIO"] = "1"
        try:
            fakes = voice_daemon.FakeServers(voice.proc.pid)
        finally:
            del os.environ["KAROTZ_ASR_SAVE_AUDIO"]
        record.append(f"fake ASR servers ready: {fakes.ready}")
        fakes.asr_mode("hold")
        record.append(call(bus, VOICE_SERVICE, VOICE_PATH, "asr_start_string", "string:s1", "string:oui | non",
                           "string:fr-FR", "int32:0"))
        saved = os.path.join(fakes.ctrl, "audio")

        def saved_size():
            return os.path.getsize(saved) if os.path.exists(saved) else 0

        wait_until(lambda: saved_size() >= 8192, 60)
        time.sleep(0.5)
        heard = open(saved, "rb").read()
        gained = lapinou_mic.with_gain(bytes(feeder.written))
        # The stream was joined at some byte; every second one of it is kept.
        matches = bool(heard) and (heard in gained[0::2] or heard in gained[1::2])
        record.append(f"asr audio received (at least 8192 bytes): {len(heard) >= 8192}, "
                      f"every second byte of the microphone's with +{lapinou_mic.GAIN_DB} dB: {matches}")
        record.extend(l for l in fakes.new_log() if "chunk of" in l or "grammar" in l)

        record.append("== 2. play and the marks")
        record.append(call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, "play", "string:p1", "string:song.mp3", "int32:0"))
        wait_until(lambda: len(marks(fake_server)) >= 1, 20)
        record.append(f"after play: {marks(fake_server)}")
        record.append(call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, "stop", "string:p1", "int32:0"))
        wait_until(lambda: len(marks(fake_server)) >= 2, 20)
        record.append(f"after stop: {marks(fake_server)}")
        record.append(call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, "play", "string:p2", "string:quick.mp3", "int32:0"))
        wait_until(lambda: len(marks(fake_server)) >= 4, 30)
        record.append(f"after a sound that ends by itself: {marks(fake_server)}")
        time.sleep(0.5)

        record.append("== 3. record, the session still going")
        before = saved_size()
        record.append(call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, "record", "string:r1", "string:x", "int32:0"))
        wait_until(lambda: record_out.exists() and record_out.stat().st_size >= 8192, 60)
        wait_until(lambda: saved_size() >= before + 4096, 30)
        data = record_out.read_bytes() if record_out.exists() else b""
        samples = [data[i] | data[i + 1] << 8 for i in range(0, len(data) - 1, 2)]
        record.append(f"encoder input (at least 8192 bytes): {len(data) >= 8192}, "
                      f"most samples not silence: {sum(1 for s in samples if s != 0x8000) > len(samples) // 2}")
        record.append(f"asr audio kept coming meanwhile: {saved_size() >= before + 4096}")

        record.append(call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, "stop", "string:r1", "int32:0"))
        record.append("recording ended: " + str(wait_until(lambda: any("recordfinished" in l for l in bus.signals()), 20)))

        record.append(call(bus, VOICE_SERVICE, VOICE_PATH, "asr_stop", "string:s1", "int32:0"))
        record.append("== warnings the other daemons logged beyond the expected ones")
        record.append(f"voice-daemon: {syslog_warnings(voice, VOICE_WARNINGS)}")
        record.append(f"multimedia-daemon: {syslog_warnings(multimedia, MULTIMEDIA_WARNINGS)}")
    finally:
        feeder.stop()
        for sub in (voice, multimedia):
            if sub is not None:
                sub.stop()
        if fakes is not None:
            fakes.stop()
        mic.close()
        fake_server.stop()
        if os.path.lexists(PLAYER_CTRL):
            os.unlink(PLAYER_CTRL)
    return record


def normalise_record(record):
    """Without what depends on timing: of the signals only the end of a play or a recording
    (lapinou-mic's connection states and multimedia's status callbacks come and go)."""
    out, section = [], None
    for line in record:
        if line.startswith("== "):
            section = line
            out.append(line)
            continue
        if section == "== signals" and not re.search(r"playfinished|recordfinished", line):
            continue
        out.append(line)
    return out
