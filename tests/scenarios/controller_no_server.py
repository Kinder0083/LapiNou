# SPDX-License-Identifier: MIT
"""controller with a LapiNou server configured but unreachable, and the cloud services set up.

lapinou.conf has [server] enabled=1 (so this is the server mode: the controller looks for its
platform as LapinLibre does, and karotz.conf's platform doesn't resolve) and [cloud] enabled=1, so
the long press falls back to LapinouCloud while the platform hasn't logged the rabbit in, and to
the no-server sound and the orange LED while LapinouCloud isn't on the bus either. No [time] tz is
applied (the platform gives the time zone in this mode): /etc/TZ stays empty. The steps are
controller_standalone's; the status is never available here (the platform never answers), so the
LED after the orange one is off and the triple click doesn't make the rabbit available.
"""

from scenarios import controller_standalone as base

DAEMON = "controller"
DEVICES = []

LAPINOU_CONF = "[server]\nenabled=1\nhost=lapinou.invalid\n[cloud]\nenabled=1\n[time]\ntz=Europe/Paris\n"

# controller.py's: the platform that doesn't resolve, no karotzVM.
EXPECTED_WARNINGS = [
    r"^nslookup failed on 'platform\.invalid': Unknown host$",
    r"^Platform - cannot connect to: platform\.invalid:\d+$",
    r"^cannot run VM: No such file or directory$",
]


def sandbox_setup(workdir, sysroot):
    return base.sandbox_setup(workdir, sysroot, lapinou_conf=LAPINOU_CONF)


drive = base.drive
