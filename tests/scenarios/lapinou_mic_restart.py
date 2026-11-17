# SPDX-License-Identifier: MIT
"""lapinou-mic killed and started again while voice-daemon recognises speech and multimedia-daemon records.

lapinou-mic, voice-daemon and multimedia-daemon run together (tests/scenarios/lapinou_rig.py); an
ASR session and a recording run on the shared microphone. lapinou-mic is killed (SIGKILL: its socket
file stays, and nobody answers on it).

  1. During the outage the daemons must neither spin nor flood: the audio the fake ASR server gets
     is at most 16 KB/s (8-bit at 8 kHz is 8 KB/s in real time; a loop of silence would be orders of
     magnitude more), the encoder gets at most 32 KB/s, and both daemons still answer on the bus
     (multimedia-daemon's next, voice-daemon's tts_stop). Nothing is sent to LapinouMic (it is not
     on the bus) and nothing is logged.
  2. A new lapinou-mic: the ASR session and the recording go on from it, and a play is marked again
     (the fake audio server of the new daemon gets its marks; the sound that was playing during
     the outage is replaced, which ends it).
  3. Killed once more and not started again: after the retries (20 s) the ASR session ends with an
     error event, the recording ends with a recordFinished error, and the daemons still answer.
"""

import os
import re
import signal
import time
from pathlib import Path

DAEMON = "lapinou-mic"
DEVICES = ["audio"]
FAKE_IOCTL_TYPES = [0x50]  # OSS SNDCTL_DSP_*

# multimedia-daemon: the recording that lost lapinou-mic for good ends as failed.
MULTIMEDIA_WARNINGS = [r"^Record: lapinou-mic didn't come back$",
                       r"^Record Stop: the recording failed \(encoder status \d+\)$"]


def sandbox_setup(workdir, sysroot):
    from scenarios import lapinou_mic

    return lapinou_mic.sandbox_setup(workdir, sysroot)


def kill(daemon):
    os.killpg(daemon.proc.pid, signal.SIGKILL)
    daemon.proc.wait()


def drive(d, bus):
    from harness import Daemon
    from scenarios import lapinou_mic
    from scenarios.lapinou_rig import (MULTIMEDIA_PATH, MULTIMEDIA_SERVICE, VOICE_PATH, VOICE_SERVICE, Rig, call,
                                       marks)

    wait_until = lapinou_mic.wait_until
    record = [f"== registered: {bus.wait_for_name(lapinou_mic.SERVICE)}"]
    rig = Rig(d, bus, multimedia_warnings=MULTIMEDIA_WARNINGS)
    try:
        record.append(f"fake audio server ready: {rig.start_microphone(d)}")
        voice, multimedia = rig.start_clients()
        record.append(f"voice-daemon registered: {voice}, multimedia-daemon registered: {multimedia}")

        def m(*a):
            return call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, *a)

        ready, started = rig.start_asr()
        record.append(started)
        record.append(m("record", "string:r1", "string:x", "int32:0"))
        wait_until(lambda: rig.saved_size() >= 8192 and rig.encoder_size() >= 8192, 60)
        record.append(m("play", "string:p1", "string:song.mp3", "int32:0"))
        wait_until(lambda: len(marks(rig.fake_server)) >= 1, 20)
        record.append(f"== 0. before the outage: marks {marks(rig.fake_server)}")

        record.append("== 1. lapinou-mic killed")
        kill(d)
        rig.stop_microphone()
        time.sleep(1.0)
        asr0, enc0, t0 = rig.saved_size(), rig.encoder_size(), time.time()
        record.append(m("next", "string:p1", "int32:0"))  # answered, though lapinou-mic is gone
        record.append(call(bus, VOICE_SERVICE, VOICE_PATH, "tts_stop", "string:nothing", "int32:0"))
        time.sleep(2.0)
        seconds = time.time() - t0
        asr_rate, enc_rate = (rig.saved_size() - asr0) / seconds, (rig.encoder_size() - enc0) / seconds
        record.append(f"during the outage the ASR server got at most 16 KB/s: {asr_rate <= 16384}, "
                      f"the encoder at most 32 KB/s: {enc_rate <= 32768}")

        record.append("== 2. lapinou-mic started again")
        work = rig.work
        again = work / "mic2"
        again.mkdir()
        setup = (f"export LAPINOU_CONF_DIR={work}/lapinou-conf LAPINOU_UUID_FILE={work}/uuid.conf "
                 f"LAPINOU_MIC_SOCKET={rig.mic_socket}; ")
        d2 = Daemon("lapinou-mic", d.sysroot, d.build, again, ["audio"], bus, FAKE_IOCTL_TYPES, (), setup)
        rig.extra.append(d2)
        asr1, enc1 = rig.saved_size(), rig.encoder_size()
        record.append(f"fake audio server ready: {rig.start_microphone(d2)}")
        record.append(f"ASR audio comes again: {wait_until(lambda: rig.saved_size() >= asr1 + 8192, 60)}")
        record.append(f"the recording goes on: {wait_until(lambda: rig.encoder_size() >= enc1 + 8192, 60)}")
        record.append(m("play", "string:p2", "string:song.mp3", "int32:0"))
        wait_until(lambda: len(marks(rig.fake_server)) >= 1, 20)
        wait_until(lambda: len(marks(rig.fake_server)) >= 2, 20)
        record.append(m("pause", "string:p2", "int32:0"))  # (stop would end the recording too)
        wait_until(lambda: len(marks(rig.fake_server)) >= 3, 20)
        record.append(f"marks reach the new lapinou-mic: {marks(rig.fake_server)}")

        record.append("== 3. lapinou-mic killed for good")
        seen = len(bus.signals())
        kill(d2)
        rig.stop_microphone()

        def ended(pattern):
            return any(re.search(pattern, l) for l in bus.signals()[seen:])

        record.append(f"the ASR session ends with an error event: {wait_until(lambda: ended('asr_event'), 60)}")
        record.append(f"the recording ends: {wait_until(lambda: ended('recordfinished'), 60)}")
        record.append(call(bus, VOICE_SERVICE, VOICE_PATH, "asr_stop", "string:s1", "int32:0"))
        record.append(m("stop", "string:r1", "int32:0"))
        record.append("== warnings the other daemons logged beyond the expected ones")
        record.extend(rig.warnings())
    finally:
        rig.stop()
    return record


def normalise_record(record):
    """Without what depends on timing: of the signals only the speech event and the ends of a play
    or a recording, in alphabetical order (the two daemons give up at about the same time)."""
    out, section, signals = [], None, []
    for line in record + ["== end"]:
        if line.startswith("== "):
            out.extend(sorted(signals))
            signals = []
            section = line
            out.append(line)
            continue
        if section == "== signals":
            if re.search(r"playfinished|recordfinished|asr_event", line):
                signals.append(line)
            continue
        if section == "== device I/O":
            continue
        out.append(line)
    return out[:-1]
