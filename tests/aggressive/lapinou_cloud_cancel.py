# SPDX-License-Identifier: MIT
"""lapinou-cloud: a question answered, then 20 questions in a row, each dropped by the next
start_listening() while the language model is still answering (its answer 2 s late), then one
question answered.

The settings, the fake services and the fake microphone are tests/scenarios/lapinou_cloud.py's. The
daemon's open descriptors and resident memory are read from outside the sandbox (the innermost
process whose command line ends with /lapinou-cloud) after the first question and at the end, both
idle. What must hold: 22 language model requests, two text to speech requests and playbacks (the
first and the last questions'), as many descriptors at the end as after the first question, the
daemon still running (and, as for every run, no warning in its log).
"""

import os
import time
from pathlib import Path

from scenarios import lapinou_cloud as base

DAEMON, DEVICES = base.DAEMON, base.DEVICES
EXPECTED_WARNINGS = base.EXPECTED_WARNINGS
ROUNDS = 20


def sandbox_setup(workdir, sysroot):
    return base.sandbox_setup(workdir, sysroot)


def daemon_process(root_pid):
    """The pid of the daemon: the descendant of root_pid whose command line ends with /lapinou-cloud."""
    parents, cmdlines = {}, {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = open(f"/proc/{entry}/stat").read()
            parents[int(entry)] = int(stat[stat.rindex(")") + 2:].split()[1])
            cmdlines[int(entry)] = open(f"/proc/{entry}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ValueError):
            pass

    def under(pid):
        while pid in parents and pid > 1:
            if pid == root_pid:
                return True
            pid = parents[pid]
        return False
    pids = [p for p, c in cmdlines.items() if under(p) and c.rstrip().endswith("/lapinou-cloud")]
    pids = [p for p in pids if not any(parents.get(q) == p for q in pids)]
    return pids[0] if len(pids) == 1 else None


def usage(root_pid):
    pid = daemon_process(root_pid)
    if pid is None:
        return -1, -1
    try:
        rss = next(int(l.split()[1]) for l in open(f"/proc/{pid}/status") if l.startswith("VmRSS:"))
        return len(os.listdir(f"/proc/{pid}/fd")), rss
    except (OSError, StopIteration, ValueError):
        return -1, -1


def drive(d, bus):
    work = Path(d.workdir)
    services = base.Services(bus, work)
    cloud = base.Cloud(work / "cloud")
    mic = base.Microphone(str(work / "mic.sock"))
    mic.start()
    record = [f"registered: {bus.wait_for_name(base.SERVICE)}"]

    def call(method):
        return bus.send(f"--dest={base.SERVICE}", base.PATH, f"{base.IFACE}.{method}")

    def llm_requests():
        return sum(e["kind"] == "llm" for e in cloud.entries())

    def question():
        """One question answered and played: idle again."""
        played = len([c for c in services.calls() if c.startswith("Multimedia.play(")])
        call("start_listening")
        time.sleep(1.3)
        call("stop_listening")
        ok = base.wait_until(lambda: len([c for c in services.calls() if c.startswith("Multimedia.play(")]) > played, 30)
        services.emit("Multimedia", "multi_playfinished", "lapinou-cloud", 4)
        time.sleep(1)
        return ok

    try:
        record.append(f"fake daemons ready: {services.start()}")
        record.append(f"a first question played: {question()}")
        first = usage(d.proc.pid)
        cloud.set("llm-delay", "2")
        for _ in range(ROUNDS):
            before = llm_requests()
            call("start_listening")  # drops the question before, still waiting for its answer
            time.sleep(1.3)
            call("stop_listening")
            base.wait_until(lambda: llm_requests() > before, 20)
        cloud.clear("llm-delay")
        record.append(f"the last question played: {question()}")
        time.sleep(3)  # the last dropped answer, 2 s late: nothing comes of it
        last = usage(d.proc.pid)
        entries = cloud.entries()
        record.append(f"language model requests: {sum(e['kind'] == 'llm' for e in entries)}")
        record.append(f"text to speech requests: {sum(e['kind'] == 'tts' for e in entries)}")
        record.append(f"plays: {sum(c.startswith('Multimedia.play(') for c in services.calls())}")
        record.append(f"descriptors after the first question, at the end: {first[0]}, {last[0]}")
        record.append(f"resident memory after the first question, at the end (kB): {first[1]}, {last[1]}")
        record.append(f"still running: {d.alive()}")
    finally:
        services.stop()
        mic.close()
    return record


def value(record, prefix):
    return next((l[len(prefix):] for l in record if l.startswith(prefix)), None)


def summary(record):
    return "; ".join(l for l in record if l.startswith(("descriptors", "resident")))


def check(record):
    problems = []
    for prefix, want in (("a first question played: ", "True"), ("the last question played: ", "True"),
                         ("language model requests: ", str(ROUNDS + 2)), ("text to speech requests: ", "2"),
                         ("plays: ", "2"), ("still running: ", "True")):
        if value(record, prefix) != want:
            problems.append(f"{prefix}{value(record, prefix)}, not {want}")
    fds = (value(record, "descriptors after the first question, at the end: ") or "").split(", ")
    if len(fds) != 2 or fds[0] != fds[1] or fds[0] == "-1":
        problems.append(f"descriptors: {fds}")
    return problems
