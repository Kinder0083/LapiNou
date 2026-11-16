# SPDX-License-Identifier: MIT
"""A fake karotz-player for the scenarios that follow lapinou-mic's playback marks.

  python3 -I tests/fakes/fake_karotz_player.py [-include CONF] [-playlist] PATH_OR_URL

It speaks karotz-player's side of the protocol multimedia-daemon uses, without sound:

  - the commands, in the file named by $FAKE_PLAYER_CTRL (the scenario's stand-in for
    /tmp/karotz-player.ctrl, a regular file the daemon writes from its start: a command is a new
    first line, noticed by the file's change time): loadfile 'X' 0 replaces the sound, pause toggles;
  - the report on $LAPINOU_PLAYER_EVENTS_FD (docs/lapinou/protocole-audio.md, "start", "end",
    "pause", "resume", one per line), at the instants the real player would write them: "start" when
    the sound's first samples would reach /dev/dsp, "end" when it is over or replaced, "pause" and
    "resume" when pause toggles a sound that has started.

A path or URL holding "slow" takes 1.5 s to start (a download before the first samples), one holding
"junk" reports a 300-byte line before its start, "quick" ends 1 s after it started, and the player then exits, as without -idle.
"""

import os
import sys
import time

SLOW_START = 1.5
QUICK_LENGTH = 1.0


def main():
    args = sys.argv[1:]
    target = ""
    skip = False
    for a in args:
        if skip:
            skip = False
        elif a == "-include":
            skip = True
        elif not a.startswith("-"):
            target = a
    events = int(os.environ["LAPINOU_PLAYER_EVENTS_FD"])
    ctrl = os.environ["FAKE_PLAYER_CTRL"]

    def report(what):
        os.write(events, what.encode() + b"\n")

    def last_change():
        try:
            return os.stat(ctrl).st_mtime_ns
        except OSError:
            return 0

    item = None  # [path, time it starts, started?, time it started, paused?]

    def begin(path):
        return [path, time.time() + (SLOW_START if "slow" in path else 0), False, 0.0, False]

    item = begin(target)
    seen = last_change()
    while True:
        time.sleep(0.02)
        now = time.time()
        changed = last_change()
        if changed != seen:
            seen = changed
            try:
                line = open(ctrl).read().split("\n")[0].strip()
            except OSError:
                line = ""
            if line.startswith("loadfile "):
                path = line.split("'")[1] if "'" in line else line.split()[1]
                if item and item[2]:
                    report("end")
                item = begin(path)
            elif line == "pause" and item and item[2]:
                item[4] = not item[4]
                report("pause" if item[4] else "resume")
            elif line == "pause" and item:
                item[4] = not item[4]
        if item and not item[2] and not item[4] and now >= item[1]:
            item[2], item[3] = True, now
            if "junk" in item[0]:  # a line too long to be an event, before the real one
                os.write(events, b"x" * 300 + b"\n")
            report("start")
        if item and item[2] and not item[4] and "quick" in item[0] and now - item[3] >= QUICK_LENGTH:
            report("end")
            return


if __name__ == "__main__":
    main()
