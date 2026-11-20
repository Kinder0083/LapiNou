# SPDX-License-Identifier: MIT
"""controller with a platform and LapiNou's cloud services: the long press's routing.

lapinou.conf has [server] enabled=1 and [cloud] enabled=1, and a fake LapinouCloud
(controller_standalone's) is on the bus before the controller starts. The set-up is
controller_platform's, up to the platform's connection; then:
- a long press before the login goes to LapinouCloud (start_listening), and the platform gets nothing;
- the platform logs the rabbit in while the button is held: the release ends the press where it
  started, stop_listening() to LapinouCloud, and no LONG_STOP to the platform (it never had the
  LONG_START);
- a long press once logged in is LapinLibre's: LONG_START and LONG_STOP to the platform, no
  LapinouCloud call.
"""

from pathlib import Path

from scenarios import controller_platform as base
from scenarios.controller_standalone import BusAddress, FakeCloud

DAEMON = "controller"
DEVICES = []
# controller_platform's (with the reconnection at the end that finds no network).
EXPECTED_WARNINGS = base.EXPECTED_WARNINGS
normalise_record = base.normalise_record

LAPINOU_CONF = "[server]\nenabled=1\nhost=10.9.0.1\n[cloud]\nenabled=1\n"

STATE = {}


def sandbox_setup(workdir, sysroot):
    w = Path(workdir)
    conf = w / "lapinou-conf"
    conf.mkdir()
    (conf / "lapinou.conf").write_text(LAPINOU_CONF)
    setup = base.sandbox_setup(workdir, sysroot)
    STATE["cloud"] = FakeCloud(BusAddress(w))
    return f"export LAPINOU_CONF_DIR={conf}; " + setup


def trigger(platform, p, fake, step):
    cloud = STATE["cloud"]
    fake.emit("Button", "lclick_start")
    step("long press before the login", settle=2.0)
    record_cloud(step, cloud, "after the press")

    platform.send(p, f'id: "p-auth" authenticationToken {{ token: "{base.TOKEN}" }}')
    step("authentication while the button is held", 'correlationId: "p-auth"')
    fake.emit("Button", "lclick_stop")
    step("release after the login: no LONG_STOP", settle=2.0)
    record_cloud(step, cloud, "after the release")

    fake.emit("Button", "lclick_start")
    fake.emit("Button", "lclick_stop")
    step("long press once logged in: the platform's", "type: LONG_START", "type: LONG_STOP", settle=2.0)
    record_cloud(step, cloud, "after the logged-in press")


def record_cloud(step, cloud, title):
    """A step whose title lists the calls LapinouCloud got since the last one."""
    calls = cloud.calls()
    step(f"LapinouCloud, {title}: {calls if calls else 'no call'}", settle=0.1, timeout=1)


def drive(d, bus):
    try:
        return base.drive(d, bus, trigger=trigger, when="connected")
    finally:
        STATE.pop("cloud").close()
