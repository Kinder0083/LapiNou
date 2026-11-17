# SPDX-License-Identifier: MIT
"""Fake Karotz daemons on a test bus, for lapinou-mqtt's and lapinou-cloud's scenarios
(tests/scenarios/lapinou_mqtt.py, tests/scenarios/lapinou_cloud.py).

    fake_karotz_services.py BUS_ADDRESS LOG [--cloud]

Owns com.mindscape.karotz.{Led,Ears,Button,Rfid,Voice,Controller,Multimedia} (each at
/com/mindscape/karotz/<Name>, interface com.mindscape.karotz.KarotzInterface, with the methods of
userland/dbus/*.xml that lapinou-mqtt and lapinou-cloud call), or, with --cloud,
com.mindscape.karotz.LapinouCloud alone (say(s) on interface com.mindscape.karotz.LapinouCloud).
Every call is logged to LOG as one line "Name.method(args)" and answered TRUE
(Controller.get_status: 1, 0, 0). (The sound, the camera and sleep go through the web API's
programs, whose dbus-send is the web API test's stub.) The scenario makes the fakes emit their
signals through com.lapinou.TestControl.emit(s name, s member, s json_args) on any of the objects
(Button.click, Rfid.tag_in, Controller.karotz_status_changed, Multimedia.multi_playfinished).
Writes "ready" to LOG's directory (ready or ready-cloud) once every name is owned.
"""

import json
import os
import sys

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

IFACE = "com.mindscape.karotz.KarotzInterface"
CLOUD_IFACE = "com.mindscape.karotz.LapinouCloud"
CONTROL = "com.lapinou.TestControl"
log_file = None
objects = {}


def note(line):
    log_file.write(line + "\n")
    log_file.flush()


def fmt(args):
    return ", ".join(repr(a) if isinstance(a, str) else str(a) for a in args)


class Fake(dbus.service.Object):
    def __init__(self, bus, name):
        self.name = name
        super().__init__(bus, f"/com/mindscape/karotz/{name}")

    def called(self, method, *args):
        note(f"{self.name}.{method}({fmt([str(a) if isinstance(a, str) else int(a) for a in args])})")

    @dbus.service.method(CONTROL, in_signature="sss", out_signature="")
    def emit(self, name, member, json_args):
        getattr(objects[str(name)], str(member))(*json.loads(json_args))


class Led(Fake):
    @dbus.service.method(IFACE, in_signature="ss", out_signature="b")
    def light(self, id_, color):
        self.called("light", id_, color)
        return True

    @dbus.service.method(IFACE, in_signature="sssii", out_signature="b")
    def pulse(self, id_, c1, c2, period, length):
        self.called("pulse", id_, c1, c2, period, length)
        return True


class Ears(Fake):
    @dbus.service.method(IFACE, in_signature="ss", out_signature="b")
    def reset(self, id_, ears):
        self.called("reset", id_, ears)
        return True

    @dbus.service.method(IFACE, in_signature="siiii", out_signature="b")
    def move(self, id_, lp, ls, rp, rs):
        self.called("move", id_, lp, ls, rp, rs)
        return True


class Button(Fake):
    @dbus.service.signal(IFACE, signature="")
    def click(self):
        pass

    @dbus.service.signal(IFACE, signature="")
    def dclick(self):
        pass

    @dbus.service.signal(IFACE, signature="")
    def tclick(self):
        pass

    @dbus.service.signal(IFACE, signature="")
    def lclick_start(self):
        pass

    @dbus.service.signal(IFACE, signature="")
    def lclick_stop(self):
        pass


class Rfid(Fake):
    @dbus.service.signal(IFACE, signature="ssiiii")
    def tag_in(self, uid, blocks, app, type_, pict, color):
        pass

    @dbus.service.signal(IFACE, signature="s")
    def tag_out(self, uid):
        pass


class Voice(Fake):
    @dbus.service.method(IFACE, in_signature="sssi", out_signature="b")
    def tts_speak(self, id_, text, lang, behaviour):
        self.called("tts_speak", id_, text, lang, behaviour)
        return True


class Controller(Fake):
    @dbus.service.method(IFACE, in_signature="", out_signature="iii")
    def get_status(self):
        self.called("get_status")
        return 1, 0, 0

    @dbus.service.signal(IFACE, signature="i")
    def karotz_status_changed(self, status):
        pass


class Multimedia(Fake):
    @dbus.service.method(IFACE, in_signature="ssi", out_signature="b")
    def play(self, id_, url, behaviour):
        self.called("play", id_, url, behaviour)
        return True

    @dbus.service.method(IFACE, in_signature="si", out_signature="b")
    def stop(self, id_, behaviour):
        self.called("stop", id_, behaviour)
        return True

    @dbus.service.signal(IFACE, signature="si")
    def multi_playfinished(self, id_, reason):
        pass


class Cloud(Fake):
    def __init__(self, bus):
        self.name = "LapinouCloud"
        dbus.service.Object.__init__(self, bus, "/com/mindscape/karotz/LapinouCloud")

    @dbus.service.method(CLOUD_IFACE, in_signature="s", out_signature="")
    def say(self, text):
        self.called("say", text)


def main():
    global log_file
    args = sys.argv[1:]
    address, log = args[0], args[1]
    cloud = "--cloud" in args
    DBusGMainLoop(set_as_default=True)
    bus = dbus.bus.BusConnection(address)
    log_file = open(log, "a")
    names = []
    if cloud:
        objects["LapinouCloud"] = Cloud(bus)
        names.append(dbus.service.BusName("com.mindscape.karotz.LapinouCloud", bus))
    else:
        for cls in (Led, Ears, Button, Rfid, Voice, Controller, Multimedia):
            objects[cls.__name__] = cls(bus, cls.__name__)
            names.append(dbus.service.BusName(f"com.mindscape.karotz.{cls.__name__}", bus))
    open(os.path.join(os.path.dirname(log), "ready-cloud" if cloud else "ready"), "w").close()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
