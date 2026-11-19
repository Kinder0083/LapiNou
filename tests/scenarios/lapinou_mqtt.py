# SPDX-License-Identifier: MIT
"""lapinou-mqtt: the rabbit in Home Assistant, through an MQTT broker.

The daemon's settings are the scenario's own (LAPINOU_CONF_DIR): [mqtt] enabled=1, host=127.0.0.1,
port=1883, user=lapin, the secret mqtt_password a password with quotes, $, ;, a backslash, = and
an accent (PASSWORD); [audio] volume=60; [server] enabled=0 and [cloud] enabled=1 (the mode without
a server: cmd/say goes to LapinouCloud when it is on the bus). Its uuid.conf, /proc/net/wireless
and conf.sh (the repository's) are moved by LAPINOU_* variables.

The web API's own programs (sound, snapshot, sleep, wakeup) and library run for real, in the
sandbox of tests/image/test_web_api.py (paths moved in, its stand-ins on PATH: dbus-send records
the calls and answers TRUE, the camera's stand-in writes its JPEG): LAPINOU_WEB_CGI, WEB_RUN and
WEB_DATA point into it. The other daemons the bridge calls (Led, Ears, Voice, the controller,
LapinouCloud) are fakes on the test bus (tests/fakes/fake_karotz_services.py), which log each call
and emit the signals the scenario asks for.

/dev/mixer is a FIFO whose OSS mixer ioctls (type 'M') are faked; SOUND_MIXER_READ_DEVMASK answers
from the ioctl data file (a volume control, then none). The broker (tests/fakes/fake_mqtt_broker.py)
and a fake MPD (tests/fakes/fake_mpd.py, its volume commands) run in the daemon's network
namespace: started by sandbox_setup before the daemon, the broker later again through nsenter.

  1. The connection: the will (availability offline, retained), the subscriptions, then every
     discovery message (retained; each a JSON object with the device, a unique_id and the
     availability topic), availability online, the state and the update entity's state.
  2. cmd/led: a colour (Led.light), a pulse (Led.pulse), off, then {"on":true} (the last colour).
  3. cmd/ears: a preset (Ears.move), positions out of range (refused), a reset (Ears.reset).
  4. Button signals -> event/button; Rfid tag_in and tag_out -> event/rfid and the last tag.
  5. cmd/say: voice-daemon without LapinouCloud, marked to lapinou-mic as a local playback
     (LapinouMic.mark_playback("local:<id>", true) first, false at voice-daemon's tts_event for that
     id, or when the next cmd/say starts); LapinouCloud.say once it is on the bus; voice again once
     it has gone. A retained cmd/say: done once, when sent (and not again at step 12's new
     subscription).
  6. cmd/sound through the web API's sound: a sound's name, a URL with characters to escape, a
     URL the web API refuses (its "::" rule), a name it doesn't know.
  7. cmd/volume: with a volume control, the mixer (and [audio] volume kept); without one, MPD's
     setvol; MPD refusing: the state published again with the volume as it was. The web page's
     set_volume on the bus: the same path; out of range: FALSE. The update entity: the daily
     check's version (LAPINOU_OTA_AVAILABLE) published as latest_version at the next tick.
  8. cmd/snapshot through the web API's snapshot: the picture it stored is published; refused while
     an application owns the rabbit (the controller's state 21).
  9. cmd/sleep on: the web API's sleep (the LED off, the ears at 4, its sleep flag); the LED
     refused while asleep; cmd/sleep off: its wakeup (its LED state, the ears at 0).
  9b. The web page's lists and functions: the selects of the stick's radios (radios.txt: a name
     with quotes and an accent, a URL to escape, a name given twice, no name: its URL, a
     name that starts like JSON), moods (moods.txt labels, one
     given twice) and stories, read through the web API's radios_list, moods_list and
     stories_list after every connection; cmd/radio, cmd/mood, cmd/story by label (an unknown
     one: logged), cmd/mood_random, cmd/media, cmd/clock, cmd/ears_random (its random positions
     masked), cmd/sleep silent; radios.txt and the stories taken away: their selects removed
     (an empty retained message) at Home Assistant's next "online".
 10. The server's assistant state, then Home Assistant's "online": everything published again.
 11. Payloads that aren't commands: ignored, the daemon goes on.
 12. The connection dropped (the will published by the broker): connected again, everything
     published again, the retained command not done again.
 13. The broker stopped and started again: the same.
"""

import base64
import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

DAEMON = "lapinou-mqtt"
DEVICES = ["mixer"]
FAKE_IOCTL_TYPES = [0x4D]  # OSS SOUND_MIXER_*
SERVICE = "com.mindscape.karotz.LapinouMqtt"
PATH = "/com/mindscape/karotz/LapinouMqtt"
IFACE = "com.mindscape.karotz.LapinouMqtt"
UUID = "1a2b3c4d-5e6f-4a8b-9c0d-e1f2a3b4c5d6"
PORT = 1883
PASSWORD = 'mot de "passe" $HOME; \\ =é'
BASE = f"lapinou/{UUID}"
HERE = Path(__file__).resolve().parent
BROKER = HERE.parent / "fakes" / "fake_mqtt_broker.py"
MPD = HERE.parent / "fakes" / "fake_mpd.py"
SERVICES = HERE.parent / "fakes" / "fake_karotz_services.py"
CONF_SH = HERE.parent.parent / "image" / "core" / "usr" / "share" / "lapinou" / "conf.sh"
MIXER_WITH_VOLUME = "80044dfe 01000000\n"  # SOUND_MIXER_READ_DEVMASK: SOUND_MASK_VOLUME
MIXER_WITHOUT_VOLUME = "80044dfe 00000000\n"
WEB = None  # the web API's sandbox (tests/image/test_web_api.py's Sandbox), made by sandbox_setup
PICTURE = None  # the camera stand-in's JPEG (test_web_api.JPEG)


def conf_value(text):
    """A value as lapinou.conf stores it (conf.sh's encoding)."""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("=", "\\=")


def sandbox_setup(workdir, sysroot):
    global WEB, PICTURE
    sys.path.insert(0, str(HERE.parent / "image"))
    import test_web_api

    w = Path(workdir)
    conf = w / "lapinou-conf"
    conf.mkdir()
    (conf / "lapinou.conf").write_text(
        "[server]\nenabled=0\n[cloud]\nenabled=1\n[mqtt]\nenabled=1\nhost=127.0.0.1\nport=1883\nuser=lapin\n"
        "[audio]\nvolume=60\n")
    (conf / "lapinou.secrets").write_text(f"[secrets]\nmqtt_password={conf_value(PASSWORD)}\n", encoding="utf-8")
    (conf / "lapinou.secrets").chmod(0o600)
    (w / "uuid.conf").write_text(UUID + "\n")
    (w / "sys_version").write_text("v1.0.0\n")
    (w / "ioctl-data").write_text(MIXER_WITH_VOLUME)
    WEB = test_web_api.Sandbox(None)
    PICTURE = test_web_api.JPEG
    WEB.mount_stick()  # its sounds, moods (1, 2, "x y") and story (tale)
    WEB.write("mnt/usbkey/radios.txt",
              '# the stick\'s stations\nFrance Inter|http://radio.example/inter.mp3\n'
              '"Radio" Été|https://radio.example/b?x=1&y=%C3%A9\nFrance Inter|http://radio.example/inter2.mp3\n'
              '|http://radio.example/noname\n{Jazz} FM|http://radio.example/jazz\n')
    WEB.write("mnt/usbkey/moods/moods.txt", "1:Joyeux\n2:Joyeux\n")
    (w / "wireless").write_text(
        "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
        " wlan0: 0000   54.  -57.  -256        0      0      0      0      0        0\n")
    broker = w / "broker"
    broker.mkdir()
    sb = WEB.sb
    return (f"export LAPINOU_CONF_DIR={conf} LAPINOU_UUID_FILE={w}/uuid.conf "
            f"LAPINOU_WEB_RUN={sb}/var/run/lapinlibre-web LAPINOU_WEB_DATA={sb}/usr/etc/conf/web "
            f"LAPINOU_WEB_CGI={sb}/usr/www/cgi-bin LAPINOU_WIRELESS={w}/wireless LAPINOU_CONF_SH={CONF_SH} "
            f"LAPINOU_SYS_VERSION={w}/sys_version LAPINOU_OTA_AVAILABLE={w}/ota-available; "
            f"{sys.executable} -I {BROKER} {broker} {PORT} > /dev/null 2> {broker}/err & "
            f"{sys.executable} -I {MPD} {broker} 6600 > /dev/null 2> {broker}/mpd-err & "
            f"i=0; while {{ [ ! -e {broker}/ready ] || [ ! -e {broker}/mpd-ready ]; }} && [ $i -lt 200 ]; do "
            f"/bin/sleep 0.1; i=$((i + 1)); done; ")


# --- helpers -------------------------------------------------------------------------------

def wait_until(predicate, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


class Broker:
    """The fake broker's log and control socket (CTRL is the run's broker/ directory)."""

    def __init__(self, ctrl, pid):
        self.ctrl, self.pid, self.seen, self.proc = Path(ctrl), pid, 0, None

    def events(self):
        try:
            return [json.loads(l) for l in (self.ctrl / "log").read_text().splitlines() if l.strip()]
        except (OSError, ValueError):
            return []

    def new(self):
        ev = self.events()
        out, self.seen = ev[self.seen:], len(ev)
        return out

    def pending(self):
        return self.events()[self.seen:]

    def control(self, msg):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(10)
            s.connect(str(self.ctrl / "ctl.sock"))
            s.sendall(json.dumps(msg).encode() + b"\n")
            try:
                return s.recv(16)
            except OSError:
                return b""

    def publish(self, topic, payload, retain=False):
        data = payload if isinstance(payload, bytes) else payload.encode()
        return self.control({"topic": topic, "payload_b64": base64.b64encode(data).decode(), "retain": retain})

    def restart(self):
        """The broker started again, inside the daemon's network namespace."""
        ready = self.ctrl / "ready"
        if ready.exists():
            ready.unlink()
        self.proc = subprocess.Popen(["nsenter", "-t", str(self.pid), "-U", "-n", "--preserve-credentials", "--",
                                      sys.executable, "-I", str(BROKER), str(self.ctrl), str(PORT)],
                                     stdout=subprocess.DEVNULL, stderr=open(self.ctrl / "err", "a"))
        return wait_until(ready.exists, 20)

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()


def published(events, topic):
    return [e for e in events if e["ev"] == "publish" and e["topic"] == topic]


def describe(e, stamp):
    """A broker event as a record line: the rabbit's id and the build stamp masked."""
    kind = e["ev"]
    if kind == "connect":
        will = e["will"]
        pw = base64.b64decode(e["password_b64"]) if e["password_b64"] else None
        line = (f"connect: protocol={e['protocol']} clean={e['clean']} keepalive={e['keepalive']} "
                f"client_id={e['client_id']} username={e['username']} password intact: {pw == PASSWORD.encode()} "
                f"will={will and (will['topic'], base64.b64decode(will['payload_b64']).decode(), will['qos'], will['retain'])}")
    elif kind == "subscribe":
        line = f"subscribe: {e['topics']}"
    elif kind == "publish":
        payload = base64.b64decode(e["payload_b64"])
        if e["topic"].endswith("/snapshot"):
            text = f"{len(payload)} bytes, the camera's picture: {payload == PICTURE}"
        else:
            text = payload.decode(errors="replace")
        line = f"publish {e['topic']} qos={e['qos']} retain={e['retain']}: {text}"
    elif kind == "will":
        line = f"will published: {e['topic']} {base64.b64decode(e['payload_b64']).decode()}"
    else:
        line = kind
    line = line.replace(UUID, "<id>")
    if stamp:
        line = line.replace(stamp, "<stamp>")
    return "  " + line


def discovery_check(events):
    """The discovery messages' count, and whether each is a JSON object as Home Assistant wants it
    (an empty one removes an entity: counted apart)."""
    found = [e for e in events if e["ev"] == "publish" and e["topic"].startswith("homeassistant/")
             and e["payload_b64"]]
    removed = [e for e in events if e["ev"] == "publish" and e["topic"].startswith("homeassistant/")
               and not e["payload_b64"] and e["retain"]]
    ok = True
    for e in found:
        try:
            c = json.loads(base64.b64decode(e["payload_b64"]))
        except ValueError:
            ok = False
            continue
        parts = e["topic"].split("/")
        ok &= (len(parts) == 5 and parts[2] == UUID and parts[4] == "config" and e["retain"] and isinstance(c, dict)
               and c.get("unique_id") == f"lapinou_{UUID}_{parts[3]}" and c.get("availability_topic") == f"{BASE}/availability"
               and c.get("device", {}).get("identifiers") == [f"lapinou_{UUID}"])
    return (f"  discovery: {len(found)} messages, each retained, valid JSON, with the device, unique_id and "
            f"availability: {ok}; {len(removed)} removals")


class Services:
    def __init__(self, bus, workdir):
        self.bus, self.log, self.seen = bus, Path(workdir) / "services.log", 0
        self.log.touch()
        self.main = self.cloud = None
        self.workdir = Path(workdir)

    def start(self, cloud=False):
        cmd = [sys.executable, "-I", str(SERVICES), self.bus.address, str(self.log)] + (["--cloud"] if cloud else [])
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open(self.workdir / "services.err", "a"))
        if cloud:
            self.cloud = proc
        else:
            self.main = proc
        return wait_until((self.workdir / ("ready-cloud" if cloud else "ready")).exists, 20)

    def stop_cloud(self):
        self.cloud.terminate()
        self.cloud.wait()
        self.cloud = None
        (self.workdir / "ready-cloud").unlink()

    def calls(self):
        return self.log.read_text().splitlines()

    def new(self):
        lines = self.calls()
        out, self.seen = lines[self.seen:], len(lines)
        return out

    def stop(self):
        for p in (self.main, self.cloud):
            if p:
                p.terminate()
                p.wait()

    def emit(self, name, member, *args):
        return self.bus.send(f"--dest=com.mindscape.karotz.{name}", f"/com/mindscape/karotz/{name}",
                             "com.lapinou.TestControl.emit", f"string:{name}", f"string:{member}",
                             f"string:{json.dumps(list(args))}")


class WebCalls:
    """What the web API's programs did since the last look: their daemons' calls (dbus-send's
    stand-in; the request's own id, random, left out; the controller's state asked, too) and
    their log lines."""

    def __init__(self, box):
        self.box, self.calls_seen, self.log_seen = box, 0, 0

    def calls(self):
        return [(d, m, a[1:]) for d, m, a in self.box.dbus_calls()]

    def new(self):
        calls, logs = self.calls(), self.box.syslog_lines()
        out = [f"  web API: {d}.{m}({', '.join(a)})" for d, m, a in calls[self.calls_seen:]]
        out += [f"  web API log: {re.sub(r'^[^ ]* ', '', l)}" for l in logs[self.log_seen:]]
        self.calls_seen, self.log_seen = len(calls), len(logs)
        return [l.replace(str(self.box.sb), "<SB>") for l in out]


def drive(d, bus):
    from harness import introspection

    work = Path(d.workdir)
    try:
        stamp = (Path(d.sysroot) / "karotz" / "etc" / "lapinlibre-build").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        stamp = ""
    broker = Broker(work / "broker", d.proc.pid)
    services = Services(bus, work)
    web = WebCalls(WEB)
    mpd_log = work / "broker" / "mpd.log"
    mpd_seen = [0]
    record = [f"== registered: {bus.wait_for_name(SERVICE)}", "== introspection"]
    record.extend(introspection(bus, SERVICE, PATH))

    def mpd_new():
        lines = mpd_log.read_text().splitlines() if mpd_log.exists() else []
        out, mpd_seen[0] = lines[mpd_seen[0]:], len(lines)
        return [f"  MPD: {l}" for l in out]

    def step(title, until=None, settle=0.5, timeout=30):
        """Wait for `until` (or 2 s, when nothing is to come), then record the broker's, the fakes',
        the web API's and MPD's new lines."""
        ok = wait_until(until, timeout) if until else True
        time.sleep(settle if until else 2)
        record.append(f"== {title}" + ("" if ok else " (timed out)"))
        record.extend(describe(e, stamp) for e in broker.new())
        record.extend(f"  call {c}" for c in services.new())
        record.extend(web.new())
        record.extend(mpd_new())

    def state_has(text):
        return lambda: any(text in base64.b64decode(e["payload_b64"]).decode(errors="replace")
                           for e in published(broker.pending(), f"{BASE}/state"))

    def calls_has(text):
        return lambda: any(text in c for c in services.calls()[services.seen:])

    def web_calls(n):
        return lambda: len(web.calls()) >= web.calls_seen + n

    def web_logged(text):
        return lambda: any(text in l for l in WEB.syslog_lines()[web.log_seen:])

    def cmd(name, payload, retain=False):
        broker.publish(f"{BASE}/cmd/{name}", payload, retain)

    def lists_published():
        """The last list's select (published once the three lists are read, after a connection)."""
        return lambda: published(broker.pending(), f"homeassistant/select/{UUID}/story/config")

    try:
        record.append(f"fake daemons ready: {services.start()}")
        # 1. The connection
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"))
        wait_until(lists_published())
        first = broker.pending()
        step("1. connection", lambda: True)
        record.append(discovery_check(first))
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.get_state")
        record.append(f"get_state: {out.splitlines()[-1].strip()}")

        # 2. The LED
        cmd("led", '{"color":"FF0000"}')
        step("2. cmd/led colour", state_has('"led":"FF0000"'))
        cmd("led", '{"color":"00ff00","pulse":"0000FF","period_ms":500}')
        step("cmd/led pulse", state_has('"led":"00FF00"'))
        cmd("led", '{"color":"000000"}')
        step("cmd/led off (Home Assistant's command_off_template)", state_has('"led":"000000"'))
        cmd("led", '{"on":true}')
        step("cmd/led on without a colour: the last one", state_has('"led":"00FF00"'))

        # 3. The ears
        cmd("ears", '{"preset":"perplexe"}')
        step("3. cmd/ears preset", state_has('"preset":"perplexe"'))
        cmd("ears", '{"left":3,"right":20}')
        step("cmd/ears out of range: refused")
        cmd("ears", '{"reset":true}')
        step("cmd/ears reset", state_has('"preset":"debout"'))

        # 4. The button and the tags
        for member in ("click", "dclick", "tclick", "lclick_start", "lclick_stop"):
            services.emit("Button", member)
        step("4. Button signals", lambda: len(published(broker.pending(), f"{BASE}/event/button")) >= 5)
        services.emit("Rfid", "tag_in", "D0021A3502B3C4D5", "", 0, 0, 0, 0)
        step("Rfid tag_in", state_has('"last_tag":"D0021A3502B3C4D5"'))
        services.emit("Rfid", "tag_out", "D0021A3502B3C4D5")
        step("Rfid tag_out", lambda: published(broker.pending(), f"{BASE}/event/rfid"))

        # 5. Speech
        cmd("say", "Bonjour, je suis Lapinou !")
        step("5. cmd/say without LapinouCloud", calls_has("Voice.tts_speak"))
        services.emit("Voice", "tts_event", "lapinou-mqtt:7", 4)
        step("voice-daemon done (tts_event): the local playback's end mark", calls_has("mark_playback"))
        record.append(f"LapinouCloud ready: {services.start(cloud=True)}")
        time.sleep(1)
        cmd("say", "Il fait beau aujourd'hui.")
        step("cmd/say with LapinouCloud on the bus", calls_has("LapinouCloud.say"))
        services.stop_cloud()
        time.sleep(1)
        cmd("say", "Au revoir.")
        step("cmd/say once LapinouCloud has gone", calls_has("Voice.tts_speak"))
        cmd("say", "Un message retenu.", retain=True)
        step("a retained cmd/say: done once, when sent", calls_has("Voice.tts_speak"))

        # 6. Sounds, through the web API's sound
        cmd("sound", "test-tone")
        step("6. cmd/sound by name", web_calls(2))
        cmd("sound", "https://radio.example/stream.mp3?a=1&b=%C3%A9")
        step("cmd/sound URL", web_calls(2))
        cmd("sound", "http://radio.example::80/stream")
        step("cmd/sound URL the web API refuses", web_logged("url"))
        cmd("sound", "pas-un-son")
        step("cmd/sound name the web API doesn't know", web_logged("pas-un-son"))

        # 7. The volume
        conf = work / "lapinou-conf" / "lapinou.conf"

        def kept():
            return [l for l in conf.read_text().splitlines() if l.startswith("volume=")]
        cmd("volume", "42")
        step("7. cmd/volume, the mixer has a volume control", state_has('"volume":42'))
        wait_until(lambda: kept() == ["volume=42"], 10)
        record.append(f"lapinou.conf [audio]: {kept()}")
        (work / "ioctl-data").write_text(MIXER_WITHOUT_VOLUME)
        cmd("volume", "30")
        step("cmd/volume, no volume control: MPD's", state_has('"volume":30'))
        wait_until(lambda: kept() == ["volume=30"], 10)
        record.append(f"lapinou.conf [audio]: {kept()}")
        (work / "broker" / "mpd-refuse").touch()
        cmd("volume", "20")
        step("cmd/volume, MPD refuses: the volume as it was",
             lambda: len(published(broker.pending(), f"{BASE}/state")) >= 1)
        record.append(f"lapinou.conf [audio]: {kept()}")
        (work / "broker" / "mpd-refuse").unlink()
        (work / "ioctl-data").write_text(MIXER_WITH_VOLUME)
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.set_volume", "int32:25")
        step("set_volume 25 (the web page)", state_has('"volume":25'))
        record.append(f"set_volume 25: {out.splitlines()[-1].strip()}")
        wait_until(lambda: kept() == ["volume=25"], 10)
        record.append(f"lapinou.conf [audio]: {kept()}")
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.set_volume", "int32:101")
        record.append(f"set_volume 101: {out.splitlines()[-1].strip()}")

        # The update entity: the daily check's version, published at the next tick
        (work / "ota-available").write_text("v1.1.0\n")
        step("the update check found v1.1.0", lambda: any(
            "latest_version" in base64.b64decode(e["payload_b64"]).decode(errors="replace")
            for e in published(broker.pending(), f"{BASE}/update")), timeout=45)

        # 8. The camera, through the web API's snapshot
        cmd("snapshot", "PRESS")
        step("8. cmd/snapshot", lambda: published(broker.pending(), f"{BASE}/snapshot"))
        stored = sorted(p.name for p in (WEB.sb / "var/run/lapinlibre-web/snapshots").glob("*.jpg"))
        record.append(f"the web API's snapshots: {len(stored)}")
        WEB.write("var/run/lapinlibre-web/controller", "21\n")
        cmd("snapshot", "PRESS")
        step("cmd/snapshot while an application owns the rabbit: refused", web_logged("application"))
        (WEB.sb / "var/run/lapinlibre-web/controller").unlink()

        # 9. Sleep, through the web API's sleep and wakeup
        cmd("sleep", "on")
        step("9. cmd/sleep on", state_has('"sleep":"on"'))
        cmd("led", '{"color":"FF0000"}')
        step("cmd/led while asleep: refused (the state again)", lambda: published(broker.pending(), f"{BASE}/state"))
        cmd("sleep", "off")
        step("cmd/sleep off", state_has('"sleep":"off"'))
        cmd("sleep", "silent")
        step("cmd/sleep silent: the web API's wakeup without its sound", web_calls(2))

        # 9b. The web page's lists and functions
        cmd("radio", '"Radio" Été')
        step("9b. cmd/radio by its label: the station's URL to the web API's sound", web_calls(2))
        cmd("radio", "France Inter (3)")
        step("cmd/radio, the second station of the same name (its line, 3)", web_calls(2))
        cmd("radio", "{Jazz} FM")
        step("cmd/radio, a label that starts like JSON: still a label", web_calls(2))
        cmd("radio", "Radio Inconnue")
        step("cmd/radio, no such station: logged")
        cmd("mood", "Joyeux (2)")
        step("cmd/mood by its label", web_calls(3))
        cmd("mood_random", "PRESS")
        step("cmd/mood_random", web_calls(3))
        cmd("story", "tale")
        step("cmd/story (no label: its id)", web_calls(2))
        cmd("media", "pause")
        step("cmd/media pause", web_calls(1))
        cmd("media", "stop")
        step("cmd/media stop", web_calls(1))
        cmd("media", "rewind")
        step("cmd/media, not an action: logged")
        cmd("clock", "PRESS")
        step("cmd/clock: the time said", web_calls(1))
        cmd("ears_random", "PRESS")
        step("cmd/ears_random", web_calls(2))
        cmd("ears", '{"reset":true}')
        step("cmd/ears reset: the ears known again", state_has('"preset":"debout"'))
        (WEB.sb / "mnt/usbkey/radios.txt").unlink()
        (WEB.sb / "mnt/usbkey/stories/tale.mp3").unlink()

        # 10. The assistant's state, Home Assistant online
        broker.publish(f"{BASE}/assistant", "écoute", retain=True)
        step("10. the server's assistant state", state_has('"assistant":"écoute"'))
        broker.publish("homeassistant/status", "online")
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"))
        wait_until(lists_published())
        again = broker.pending()
        step("Home Assistant online: everything again", lambda: True)
        record.append(discovery_check(again))

        # 11. Not commands
        cmd("led", "{bad")
        cmd("led", '{"color":{"r":1}}')
        cmd("dance", "now")
        broker.publish(f"{BASE}/cmd/say", b"\xff\xfe")
        step("11. payloads that aren't commands: ignored")
        record.append(f"still running: {d.alive()}")

        # 12. The connection dropped: the retained cmd/say comes again with the new subscription
        broker.control({"op": "drop"})
        wait_until(lambda: len([e for e in broker.pending() if e["ev"] == "connect"]) >= 1)
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"))
        wait_until(lists_published())
        again = broker.pending()
        step("12. the connection dropped (the retained cmd/say not done again)", lambda: True, settle=2)
        record.append(discovery_check(again))

        # 13. The broker stopped and started again
        broker.control({"op": "exit"})
        time.sleep(0.5)
        record.append(f"broker started again: {broker.restart()}")
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"), 40)
        wait_until(lists_published())
        again = broker.pending()
        step("13. the broker started again", lambda: True)
        record.append(discovery_check(again))
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.get_state")
        record.append(f"get_state: {out.splitlines()[-1].strip()}")
        # The broker stopped for good: its last "disconnected" seen before the signals are recorded.
        broker.stop()
        broker.proc = None
        wait_until(lambda: sum(".mqtt_state(" in s and "disconnected" in s for s in bus.signals()) >= 3, 15)
        record.append("== the daemon's log: refusals, ignored commands, the volume")
        log = Path(d.syslog).read_text(errors="replace").splitlines() if Path(d.syslog).exists() else []
        record.extend("  " + l.replace(UUID, "<id>") for l in log
                      if re.search(r"refused|ignored|retained|ended with|killed|volume|cmd/", l))
    finally:
        services.stop()
        broker.stop()
        WEB.tmp.cleanup()
    return record


def normalise_record(record):
    """Attempts that failed while the broker was down (their number depends on timing) as one, the
    snapshot's name (its time), and the ears' random positions (cmd/ears_random)."""
    out = []
    random_ears = random_mood = clock = False
    pair = [f"{IFACE}.mqtt_state(string \"connecting\")", f"{IFACE}.mqtt_state(string \"disconnected\")"]
    for line in record:
        line = re.sub(r"snapshot_\d{4}(_\d{2}){5}", "snapshot_<time>", line)
        if line.startswith("== "):
            random_ears = line == "== cmd/ears_random"
            random_mood = line == "== cmd/mood_random"
            clock = line.startswith("== cmd/clock")
        if random_mood:
            line = re.sub(r"moods/\d+\.mp3", "moods/<n>.mp3", line)
        if clock:
            line = re.sub(r"\d+:\d+", "<hh:mm>", line)
        if random_ears:
            line = re.sub(r'"left":\d+,"right":\d+,"preset":"[^"]*"', '"left":<n>,"right":<n>,"preset":<p>', line)
            line = re.sub(r"int32:\d+", "int32:<n>", line)
            line = re.sub(r'"(left|right)":"\d+"', r'"\1":"<n>"', line)
        out.append(line)
        if len(out) >= 4 and out[-2:] == pair and out[-4:-2] == pair:
            del out[-2:]
    return out
