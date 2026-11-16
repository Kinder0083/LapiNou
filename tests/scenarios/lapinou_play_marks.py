# SPDX-License-Identifier: MIT
"""Playback marks at the instants karotz-player reports: a sound replaced by another, pause and resume.

lapinou-mic and multimedia-daemon run together (tests/scenarios/lapinou_rig.py); the player is
tests/fakes/fake_karotz_player.py, which reports "start", "end", "pause" and "resume" on the pipe
multimedia-daemon gives it, and takes 1.5 s to start a sound whose name holds "slow" (a download
before the first samples). The marks are what the fake audio server receives from lapinou-mic.

  1. play p1: (p1, start).
  2. play p2 ("slow") while p1 plays: (p1, end) at once, and (p2, start) only when p2 really starts,
     1.5 s later, not when the play was asked for.
  3. pause: (p2, end); resume: (p2, start).
  4. play p3 (starts at once) while p2 plays: (p2, end), (p3, start), each with its own id.
  5. play p4 whose player first writes a 300-byte line: dropped, and (p3, end), (p4, start) still follow.
  6. stop: (p4, end).
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
    from scenarios.lapinou_rig import MULTIMEDIA_PATH, MULTIMEDIA_SERVICE, Rig, call, marks

    wait_until = lapinou_mic.wait_until
    record = [f"== registered: {bus.wait_for_name(lapinou_mic.SERVICE)}"]
    rig = Rig(d, bus)
    try:
        record.append(f"fake audio server ready: {rig.start_microphone(d)}")
        voice, multimedia = rig.start_clients()
        record.append(f"multimedia-daemon registered: {multimedia}")
        server = rig.fake_server

        def m(*a):
            return call(bus, MULTIMEDIA_SERVICE, MULTIMEDIA_PATH, *a)

        record.append("== 1. play p1")
        record.append(m("play", "string:p1", "string:song.mp3", "int32:0"))
        wait_until(lambda: len(marks(server)) >= 1, 20)
        record.append(f"marks: {marks(server)}")

        record.append("== 2. play p2 while p1 plays, p2 starting late")
        asked = time.time()
        record.append(m("play", "string:p2", "string:slow.mp3", "int32:0"))
        wait_until(lambda: len(marks(server)) >= 2, 20)
        record.append(f"marks: {marks(server)}")
        time.sleep(0.6)  # the player is still downloading p2
        record.append(f"p2 not started yet after 0.6 s: {marks(server) == ['mark(p1, start=True)', 'mark(p1, start=False)']}")
        wait_until(lambda: len(marks(server)) >= 3, 20)
        waited = time.time() - asked
        record.append(f"marks: {marks(server)}")
        record.append(f"p2's start came after the player's start-up delay (at least 1.2 s after the play): {waited >= 1.2}")

        record.append("== 3. pause and resume")
        record.append(m("pause", "string:p2", "int32:0"))
        wait_until(lambda: len(marks(server)) >= 4, 20)
        record.append(f"marks: {marks(server)}")
        record.append(m("resume", "string:p2", "int32:0"))
        wait_until(lambda: len(marks(server)) >= 5, 20)
        record.append(f"marks: {marks(server)}")

        record.append("== 4. play p3 at once, replacing p2")
        record.append(m("play", "string:p3", "string:song.mp3", "int32:0"))
        wait_until(lambda: len(marks(server)) >= 7, 20)
        time.sleep(0.5)
        record.append(f"marks: {marks(server)[5:7]}")

        record.append("== 5. play p4, whose player first reports a line too long to be an event")
        record.append(m("play", "string:p4", "string:junk.mp3", "int32:0"))
        wait_until(lambda: len(marks(server)) >= 9, 20)
        time.sleep(0.5)
        record.append(f"marks: {marks(server)[7:9]}")

        record.append("== 6. stop")
        record.append(m("stop", "string:p4", "int32:0"))
        wait_until(lambda: len(marks(server)) >= 10, 20)
        time.sleep(0.5)
        record.append(f"marks: {marks(server)}")
        record.append("== warnings the other daemons logged beyond the expected ones")
        record.extend(rig.warnings())
    finally:
        rig.stop()
    return record


def normalise_record(record):
    """Without what depends on timing: of the signals only the end of a play."""
    out, section = [], None
    for line in record:
        if line.startswith("== "):
            section = line
            out.append(line)
            continue
        if section == "== signals" and not re.search(r"playfinished", line):
            continue
        out.append(line)
    return out
