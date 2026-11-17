# SPDX-License-Identifier: MIT
"""lapinou-mic without a server turned on (R49): the microphone never leaves the rabbit.

The same sandbox as lapinou_mic.py (its FIFO for /dev/audio, uuid.conf, local socket), but
lapinou.conf has [server] host=127.0.0.1 and enabled=0: a server address alone is not enough. The
fake audio server listens on 127.0.0.1:9300 in the daemon's network namespace all along.

  1. The daemon starts and says the microphone is only shared locally.
  2. 4096 bytes written to the microphone: a client of the local socket receives them (gain
     applied); the fake server sees no connection at all, even after set_streaming(true).
"""

import socket
import time
from pathlib import Path

DAEMON = "lapinou-mic"
DEVICES = ["audio"]
FAKE_IOCTL_TYPES = [0x50]  # OSS SNDCTL_DSP_*


def sandbox_setup(workdir, sysroot):
    from scenarios import lapinou_mic

    env = lapinou_mic.sandbox_setup(workdir, sysroot)
    (Path(workdir) / "lapinou-conf" / "lapinou.conf").write_text("[server]\nhost=127.0.0.1\nenabled=0\n")
    return env


def drive(d, bus):
    from scenarios import lapinou_mic as m

    record = [f"== registered: {bus.wait_for_name(m.SERVICE)}"]
    mic = m.Microphone(d.fifos["audio"])
    record.append(f"microphone opened by the daemon: {mic.fd is not None}")
    fake = m.FakeServer(d.proc.pid)
    client = None
    try:
        record.append("== 1. no server turned on")
        record.append(f"fake server ready: {fake.start()}")
        record.append("== 2. the microphone, shared locally only")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = Path(d.workdir) / "run" / "mic.sock"
        m.wait_until(path.exists, 20)
        client.connect(str(path))
        time.sleep(1)  # accepted by the daemon's main loop
        block = bytes(range(256)) * 16
        mic.write(block)
        got = m.receive(client, len(block))
        record.append(f"local client: {len(got)} bytes, the microphone's with the gain: {got == m.with_gain(block)}")
        record.append(m.call(bus, "set_streaming", "boolean:true"))
        mic.write(block)
        m.receive(client, len(block))
        time.sleep(3)  # longer than the daemon's first connection attempt would take
        record.append(f"connections the fake server saw: {sum(l.startswith('connection') for l in fake.lines())}")
    finally:
        if client is not None:
            client.close()
        mic.close()
        fake.stop()
    return record
