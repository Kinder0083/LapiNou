# SPDX-License-Identifier: MIT
"""lapinou-mic, voice-daemon and multimedia-daemon together: one microphone, shared.

lapinou-mic is the only program to open /dev/audio (a FIFO the scenario writes, as in lapinou_mic.py);
voice-daemon and multimedia-daemon get the microphone from its local socket (LAPINOU_MIC_SOCKET, in
the run's directory) and have no /dev/audio of their own (tests/scenarios/lapinou_rig.py sets the
three up). The scenario keeps writing pseudo-random mu-law bytes to the microphone all the time (a
daemon waits for its next block in its main loop, as on the rabbit, where the microphone never
stops).

  1. asr_start_string: the fake ASR server (voice_daemon.py's, held open) receives the microphone as
     the daemon sends it, 8-bit at 8 kHz: every second byte of the stream lapinou-mic serves (its
     gain applied), in 1024-byte chunks.
  2. play, with the fake karotz-player (tests/fakes/fake_karotz_player.py): multimedia-daemon calls
     mark_playback(id, true) when the player reports its sound's first samples, then (id, false)
     after stop, and the same for a sound that ends by itself; the marks are what the fake audio
     server receives from lapinou-mic.
  3. record, while the ASR session goes on: the encoder (a stub that keeps its input) gets 16-bit
     unsigned samples, two bytes per microphone byte, from the same stream.

The other daemons' syslog warnings are checked here (the harness checks lapinou-mic's own).
lapinou_play_marks and lapinou_mic_restart go further.
"""

import re
import time

DAEMON = "lapinou-mic"
DEVICES = ["audio"]
FAKE_IOCTL_TYPES = [0x50]  # OSS SNDCTL_DSP_*


def sandbox_setup(workdir, sysroot):
    from scenarios import lapinou_mic

    return lapinou_mic.sandbox_setup(workdir, sysroot)


def drive(d, bus):
    from scenarios import lapinou_mic
    from scenarios.lapinou_rig import (MULTIMEDIA_PATH, MULTIMEDIA_SERVICE, VOICE_PATH, VOICE_SERVICE, Rig, call,
                                       marks)

    wait_until = lapinou_mic.wait_until
    record = [f"== registered: {bus.wait_for_name(lapinou_mic.SERVICE)}"]
    rig = Rig(d, bus)
    try:
        ready = rig.start_microphone(d)
        record.append(f"microphone opened by lapinou-mic: {rig.mic.fd is not None}")
        record.append(f"fake audio server ready: {ready}")
        voice, multimedia = rig.start_clients()
        record.append(f"voice-daemon registered: {voice}")
        record.append(f"multimedia-daemon registered: {multimedia}")

        record.append("== 1. asr_start_string")
        ready, started = rig.start_asr()
        record.append(f"fake ASR servers ready: {ready}")
        record.append(started)
        wait_until(lambda: rig.saved_size() >= 8192, 60)
        time.sleep(0.5)
        heard = open(rig.saved, "rb").read()
        gained = lapinou_mic.with_gain(bytes(rig.feeder.written))
        # The stream was joined at some byte; every second one of it is kept.
        matches = bool(heard) and (heard in gained[0::2] or heard in gained[1::2])
        record.append(f"asr audio received (at least 8192 bytes): {len(heard) >= 8192}, "
                      f"every second byte of the microphone's with +{lapinou_mic.GAIN_DB} dB: {matches}")
        record.extend(l for l in rig.fakes.new_log() if "chunk of" in l or "grammar" in l)

        record.append("== 2. play and the marks")
        m = lambda *a: call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, *a)  # noqa: E731
        record.append(m("play", "string:p1", "string:song.mp3", "int32:0"))
        wait_until(lambda: len(marks(rig.fake_server)) >= 1, 20)
        record.append(f"after play: {marks(rig.fake_server)}")
        record.append(m("stop", "string:p1", "int32:0"))
        wait_until(lambda: len(marks(rig.fake_server)) >= 2, 20)
        record.append(f"after stop: {marks(rig.fake_server)}")
        record.append(m("play", "string:p2", "string:quick.mp3", "int32:0"))
        wait_until(lambda: len(marks(rig.fake_server)) >= 4, 30)
        record.append(f"after a sound that ends by itself: {marks(rig.fake_server)}")
        time.sleep(0.5)

        record.append("== 3. record, the session still going")
        before = rig.saved_size()
        record.append(m("record", "string:r1", "string:x", "int32:0"))
        wait_until(lambda: rig.encoder_size() >= 8192, 60)
        wait_until(lambda: rig.saved_size() >= before + 4096, 30)
        data = rig.record_out.read_bytes() if rig.record_out.exists() else b""
        samples = [data[i] | data[i + 1] << 8 for i in range(0, len(data) - 1, 2)]
        record.append(f"encoder input (at least 8192 bytes): {len(data) >= 8192}, "
                      f"most samples not silence: {sum(1 for s in samples if s != 0x8000) > len(samples) // 2}")
        record.append(f"asr audio kept coming meanwhile: {rig.saved_size() >= before + 4096}")
        record.append(m("stop", "string:r1", "int32:0"))
        record.append("recording ended: " + str(wait_until(lambda: any("recordfinished" in l for l in bus.signals()), 20)))

        record.append(call(bus, VOICE_SERVICE, VOICE_PATH, "asr_stop", "string:s1", "int32:0"))
        record.append("== warnings the other daemons logged beyond the expected ones")
        record.extend(rig.warnings())
    finally:
        rig.stop()
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
