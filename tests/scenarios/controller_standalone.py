# SPDX-License-Identifier: MIT
"""controller in LapiNou's standalone mode: no platform, the long press goes to LapinouCloud.

The scenario's lapinou.conf (LAPINOU_CONF_DIR) has [server] enabled=0, [cloud] enabled=1 and
[time] tz=Europe/Paris; karotz.conf is the scenarios' own (a platform that doesn't resolve). The
controller doesn't look for the platform at all (any attempt would log a warning, which fails the
run): once the network is up it becomes available, with the available look (green pulse, ears up),
not the disconnected one (red, ears down), and writes the zone's TZ from /etc/timezone.list to
/etc/TZ (a file mounted over the sysroot's, as in controller_platform).

The fake daemons (controller.py's) own their names before the controller starts, so its start-up
calls are recorded too. A fake LapinouCloud (com.mindscape.karotz.LapinouCloud) comes and goes:
- long press without LapinouCloud: the no-server sound (/usr/karotz/sounds/lapinou-no-server.mp3)
  and the orange LED, then 3 s later the status LED again;
- with LapinouCloud on the bus: LapinouCloud.start_listening(), stop_listening() at the release,
  and no sound or LED from the controller;
- LapinouCloud gone: the no-server sound again.
Then the buttons and a tag: a click goes nowhere (no platform), the triple click resets and comes
back available, the tag listener's has_tag_app is FALSE (no platform configuration: the listener
gives the tag its own action), and the status stays available.

controller_no_server.py runs the same steps with a LapiNou server configured but unreachable.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scenarios import conf_fixture
from scenarios.controller import SERVICE, FakeDaemons, call

DAEMON = "controller"
DEVICES = []

CLOUD = "com.mindscape.karotz.LapinouCloud"
TAG = "e0040100c0ffee42"

LAPINOU_CONF = "[server]\nenabled=0\n[cloud]\nenabled=1\n[time]\ntz=Europe/Paris\n"

# Syslog warnings the run is known to log (harness.py): no karotzVM in the sysroot.
EXPECTED_WARNINGS = [
    r"^cannot run VM: No such file or directory$",
]

# The fake daemons, started by sandbox_setup (before the controller) and used by drive().
STATE = {}

# The fake LapinouCloud: owns the name, records the calls it gets and answers them (no value).
CLOUD_HELPER = r'''
import sys
import dbus, dbus.lowlevel, dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SystemBus()
NAME = "com.mindscape.karotz.LapinouCloud"
log = open(sys.argv[1], "a")

def on_message(conn, msg):
    if msg.get_type() != dbus.lowlevel.MESSAGE_TYPE_METHOD_CALL or msg.get_destination() != NAME:
        return dbus.lowlevel.HANDLER_RESULT_NOT_YET_HANDLED
    args = ", ".join(repr(str(a)) for a in msg.get_args_list())
    log.write("call LapinouCloud %s %s.%s(%s)\n" % (msg.get_path(), msg.get_interface(), msg.get_member(), args))
    log.flush()
    if not msg.get_no_reply():
        conn.send_message(dbus.lowlevel.MethodReturnMessage(msg))
    return dbus.lowlevel.HANDLER_RESULT_HANDLED

bus.add_message_filter(on_message)
bus.request_name(NAME)

def on_stdin(source, condition):
    loop.quit()
    return False

GLib.io_add_watch(sys.stdin, GLib.IO_IN | GLib.IO_HUP, on_stdin)
print("ready", flush=True)
loop = GLib.MainLoop()
loop.run()
'''


class BusAddress:
    """What FakeDaemons needs of harness.Bus before the harness hands it over: its environment."""

    def __init__(self, workdir):
        self.address = f"unix:path={Path(workdir) / 'bus.sock'}"

    def env(self):
        return {**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": self.address}


class FakeCloud:
    def __init__(self, bus):
        fd, self.log = tempfile.mkstemp(prefix="karotz-lapinou-cloud-calls-")
        os.close(fd)
        self.proc = subprocess.Popen([sys.executable, "-c", CLOUD_HELPER, self.log], env=bus.env(),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "ready"
        self.read = 0

    def calls(self):
        with open(self.log) as f:
            f.seek(self.read)
            text = f.read()
            self.read = f.tell()
        return text.splitlines()

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        os.unlink(self.log)


def has_owner(bus, name):
    out = bus.send("--dest=org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus.NameHasOwner",
                   f"string:{name}")
    return "boolean true" in out


def sandbox_setup(workdir, sysroot, lapinou_conf=LAPINOU_CONF):
    """The scenarios' karotz.conf, the scenario's lapinou.conf, an /etc/TZ to write, the fake daemons."""
    sysroot = Path(sysroot).resolve()
    w = Path(workdir)
    conf = w / "lapinou-conf"
    conf.mkdir()
    (conf / "lapinou.conf").write_text(lapinou_conf)
    (w / "TZ").write_text("")
    STATE["TZ"] = w / "TZ"
    STATE["fake"] = FakeDaemons(BusAddress(w))
    return (conf_fixture.sandbox_setup(w, sysroot, karotz=conf_fixture.KAROTZ_CONF) +
            f"{conf_fixture.mount_tool()} --bind {w / 'TZ'} {sysroot / 'usr' / 'etc' / 'TZ'}; "
            f"export LAPINOU_CONF_DIR={conf}; ")


def drive(d, bus):
    fake = STATE.pop("fake")
    record = [f"== registered: {bus.wait_for_name(SERVICE)}"]
    cloud = None
    try:
        def step(title, wait=1.0):
            time.sleep(wait)
            record.append(f"== {title}")
            record.extend(fake.calls())
            if cloud is not None:
                record.extend(cloud.calls())

        step("start-up", 4.0)
        record.append(call(bus, "get_status"))
        record.append(f"/etc/TZ: {STATE['TZ'].read_bytes()!r}")

        fake.emit("Button", "lclick_start")
        step("long press, LapinouCloud absent: the no-server sound, orange")
        fake.emit("Button", "lclick_stop")
        step("release", 0.5)
        step("3 s after the press: the status LED again", 3.0)

        cloud = FakeCloud(bus)
        step(f"LapinouCloud on the bus: {has_owner(bus, CLOUD)}")
        fake.emit("Button", "lclick_start")
        step("long press, LapinouCloud present: start_listening")
        fake.emit("Button", "lclick_stop")
        step("release: stop_listening")

        cloud.close()
        cloud = None
        for _ in range(100):
            if not has_owner(bus, CLOUD):
                break
            time.sleep(0.05)
        step(f"LapinouCloud gone: {not has_owner(bus, CLOUD)}")
        fake.emit("Button", "lclick_start")
        fake.emit("Button", "lclick_stop")
        step("long press, LapinouCloud gone: the no-server sound again", 4.0)

        fake.emit("Button", "click")
        step("click")
        fake.emit("Button", "tclick")
        step("triple click", 4.5)
        record.append(call(bus, "has_tag_app", f"string:{TAG}"))
        fake.emit("Rfid", "tag_in", "ssiiii", TAG, "content", 3, 1, 4, 2)
        fake.emit("Rfid", "tag_out", "s", TAG)
        step("a tag")
        record.append(call(bus, "get_status"))
    finally:
        if cloud is not None:
            cloud.close()
        fake.close()
    return record
