# SPDX-License-Identifier: MIT
"""lapinou-mqtt: the rabbit in Home Assistant, through an MQTT broker.

The daemon's settings are the scenario's own (LAPINOU_CONF_DIR): [mqtt] enabled=1, host=127.0.0.1,
port=1883, user=lapin, the secret mqtt_password a password with quotes, $, ;, a backslash, = and
an accent (PASSWORD); [audio] volume=60; [server] enabled=0 and [cloud] enabled=1 (the mode without
a server: cmd/say goes to LapinouCloud when it is on the bus). Its uuid.conf, the camera's picture,
the web API's state directory and programs (sleep and wakeup are stubs that log their CGI
environment and set or clear the sleep flag), /proc/net/wireless and conf.sh (the repository's)
are moved by LAPINOU_* variables. /dev/mixer is a FIFO whose OSS mixer ioctls (type 'M') are faked.

The broker (tests/fakes/fake_mqtt_broker.py) runs in the daemon's network namespace on
127.0.0.1:1883: started by sandbox_setup before the daemon, so the first connection succeeds; later
started again through nsenter. Its log gives each CONNECT (the password checked byte for byte,
not shown), SUBSCRIBE and PUBLISH. The daemons the bridge calls are fakes on the test bus
(tests/fakes/fake_karotz_services.py), which log each call and emit the signals the scenario asks
for; LapinouCloud is a second fake, started and stopped during the run.

  1. The connection: the will (availability offline, retained), the subscriptions, then every
     discovery message (retained; each a JSON object with the device, a unique_id and the
     availability topic), availability online, the state and the update entity's state.
  2. cmd/led: a colour (Led.light), a pulse (Led.pulse), off, then {"on":true} (the last colour).
  3. cmd/ears: a preset (Ears.move), positions out of range (refused), a reset (Ears.reset).
  4. Button signals -> event/button; Rfid tag_in and tag_out -> event/rfid and the last tag.
  5. cmd/say: voice-daemon without LapinouCloud; LapinouCloud.say once it is on the bus; voice
     again once it has gone.
  6. cmd/sound: a sound's name, a URL, a name that isn't one (refused).
  7. cmd/volume: the mixer, the state, and [audio] volume kept in lapinou.conf.
  8. cmd/snapshot: the camera's picture published as it is.
  9. cmd/sleep on: the web API's sleep program (as the web API's user, from 127.0.0.1); the LED
     refused while asleep; cmd/sleep off: its wakeup program.
 10. The server's assistant state, then Home Assistant's "online": everything published again.
 11. Payloads that aren't commands: ignored, the daemon goes on.
 12. The connection dropped (the will published by the broker): connected again, everything
     published again. 13. The broker stopped and started again: the same.
"""

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
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
SERVICES = HERE.parent / "fakes" / "fake_karotz_services.py"
CONF_SH = HERE.parent.parent / "image" / "core" / "usr" / "share" / "lapinou" / "conf.sh"
PICTURE = None  # read from the fake's module


def conf_value(text):
    """A value as lapinou.conf stores it (conf.sh's encoding)."""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("=", "\\=")


def write_script(path, text):
    path.write_text(text)
    path.chmod(0o755)


def sandbox_setup(workdir, sysroot):
    w = Path(workdir)
    conf = w / "lapinou-conf"
    conf.mkdir()
    (conf / "lapinou.conf").write_text(
        "[server]\nenabled=0\n[cloud]\nenabled=1\n[mqtt]\nenabled=1\nhost=127.0.0.1\nport=1883\nuser=lapin\n"
        "[audio]\nvolume=60\n")
    (conf / "lapinou.secrets").write_text(f"[secrets]\nmqtt_password={conf_value(PASSWORD)}\n", encoding="utf-8")
    (conf / "lapinou.secrets").chmod(0o600)
    (w / "uuid.conf").write_text(UUID + "\n")
    (w / "web-run").mkdir()
    cgi = w / "cgi-bin"
    cgi.mkdir()
    env_line = ('echo "$0: REQUEST_METHOD=$REQUEST_METHOD SCRIPT_NAME=$SCRIPT_NAME REQUEST_URI=$REQUEST_URI '
                'QUERY_STRING=$QUERY_STRING REMOTE_USER=$REMOTE_USER REMOTE_ADDR=$REMOTE_ADDR '
                f'GATEWAY_INTERFACE=$GATEWAY_INTERFACE" >> {w}/cgi.log\n')
    write_script(cgi / "sleep", f"#!/bin/sh\n{env_line}echo 1700000000 > {w}/web-run/sleep\n")
    write_script(cgi / "wakeup", f"#!/bin/sh\n{env_line}/bin/rm -f {w}/web-run/sleep\n")
    (w / "wireless").write_text(
        "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
        " wlan0: 0000   54.  -57.  -256        0      0      0      0      0        0\n")
    broker = w / "broker"
    broker.mkdir()
    # The broker, in the daemon's namespaces, up before the daemon starts.
    return (f"export LAPINOU_CONF_DIR={conf} LAPINOU_UUID_FILE={w}/uuid.conf LAPINOU_PICTURE={w}/picture.jpg "
            f"LAPINOU_WEB_RUN={w}/web-run LAPINOU_WEB_CGI={cgi} LAPINOU_WIRELESS={w}/wireless "
            f"LAPINOU_CONF_SH={CONF_SH}; "
            f"{sys.executable} -I {BROKER} {broker} {PORT} > /dev/null 2> {broker}/err & "
            f"i=0; while [ ! -e {broker}/ready ] && [ $i -lt 200 ]; do /bin/sleep 0.1; i=$((i + 1)); done; ")


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
    """The discovery messages' count, and whether each is a JSON object as Home Assistant wants it."""
    found = [e for e in events if e["ev"] == "publish" and e["topic"].startswith("homeassistant/")]
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
    return f"  discovery: {len(found)} messages, each retained, valid JSON, with the device, unique_id and availability: {ok}"


class Services:
    def __init__(self, bus, workdir):
        self.bus, self.log, self.seen = bus, Path(workdir) / "services.log", 0
        self.log.touch()
        self.main = self.cloud = None
        self.workdir = Path(workdir)

    def start(self, cloud=False):
        cmd = [sys.executable, "-I", str(SERVICES), self.bus.address, str(self.log)]
        if cloud:
            cmd.append("--cloud")
        else:
            cmd += ["--picture", str(self.workdir / "picture.jpg")]
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


def drive(d, bus):
    global PICTURE
    from harness import introspection

    sys.path.insert(0, str(SERVICES.parent))
    import fake_karotz_services
    PICTURE = fake_karotz_services.PICTURE

    work = Path(d.workdir)
    try:
        stamp = (Path(d.sysroot) / "karotz" / "etc" / "lapinlibre-build").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        stamp = ""
    broker = Broker(work / "broker", d.proc.pid)
    services = Services(bus, work)
    record = [f"== registered: {bus.wait_for_name(SERVICE)}", "== introspection"]
    record.extend(introspection(bus, SERVICE, PATH))

    def step(title, until=None, settle=0.5, timeout=30):
        """Wait for `until` (or 2 s, when nothing is to come), then record the broker's and the
        fakes' new lines."""
        ok = wait_until(until, timeout) if until else True
        time.sleep(settle if until else 2)
        record.append(f"== {title}" + ("" if ok else " (timed out)"))
        record.extend(describe(e, stamp) for e in broker.new())
        record.extend(f"  call {c}" for c in services.new())

    def state_has(text):
        return lambda: any(text in base64.b64decode(e["payload_b64"]).decode(errors="replace")
                           for e in published(broker.pending(), f"{BASE}/state"))

    def calls_has(text):
        return lambda: any(text in c for c in services.calls()[services.seen:])

    def cmd(name, payload):
        broker.publish(f"{BASE}/cmd/{name}", payload)

    try:
        record.append(f"fake daemons ready: {services.start()}")
        # 1. The connection
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"))
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
        record.append(f"LapinouCloud ready: {services.start(cloud=True)}")
        time.sleep(1)
        cmd("say", "Il fait beau aujourd'hui.")
        step("cmd/say with LapinouCloud on the bus", calls_has("LapinouCloud.say"))
        services.stop_cloud()
        time.sleep(1)
        cmd("say", "Au revoir.")
        step("cmd/say once LapinouCloud has gone", calls_has("Voice.tts_speak"))

        # 6. Sounds
        cmd("sound", "notify")
        step("6. cmd/sound by name", calls_has("Multimedia.play"))
        cmd("sound", "https://radio.example/stream.mp3")
        step("cmd/sound URL", calls_has("Multimedia.play"))
        cmd("sound", "../etc/passwd")
        step("cmd/sound neither: refused")

        # 7. The volume
        cmd("volume", "42")
        step("7. cmd/volume", state_has('"volume":42'))
        conf = work / "lapinou-conf" / "lapinou.conf"
        wait_until(lambda: "volume=42" in conf.read_text(), 10)
        record.append(f"lapinou.conf [audio]: {[l for l in conf.read_text().splitlines() if l.startswith('volume=')]}")

        # 8. The camera
        cmd("snapshot", "PRESS")
        step("8. cmd/snapshot", lambda: published(broker.pending(), f"{BASE}/snapshot"))

        # 9. Sleep
        cmd("sleep", "on")
        step("9. cmd/sleep on", state_has('"sleep":"on"'))
        record.extend(f"  {l.replace(str(work), '<RUN>')}" for l in (work / "cgi.log").read_text().splitlines())
        cmd("led", '{"color":"FF0000"}')
        step("cmd/led while asleep: refused")
        cmd("sleep", "off")
        step("cmd/sleep off", state_has('"sleep":"off"'))
        record.extend(f"  {l.replace(str(work), '<RUN>')}" for l in (work / "cgi.log").read_text().splitlines()[1:])

        # 10. The assistant's state, Home Assistant online
        broker.publish(f"{BASE}/assistant", "écoute", retain=True)
        step("10. the server's assistant state", state_has('"assistant":"écoute"'))
        broker.publish("homeassistant/status", "online")
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"))
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

        # 12. The connection dropped
        broker.control({"op": "drop"})
        wait_until(lambda: len([e for e in broker.pending() if e["ev"] == "connect"]) >= 1)
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"))
        again = broker.pending()
        step("12. the connection dropped", lambda: True)
        record.append(discovery_check(again))

        # 13. The broker stopped and started again
        broker.control({"op": "exit"})
        time.sleep(0.5)
        record.append(f"broker started again: {broker.restart()}")
        wait_until(lambda: published(broker.pending(), f"{BASE}/update"), 40)
        again = broker.pending()
        step("13. the broker started again", lambda: True)
        record.append(discovery_check(again))
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.get_state")
        record.append(f"get_state: {out.splitlines()[-1].strip()}")
    finally:
        services.stop()
        broker.stop()
    return record


def normalise_record(record):
    """Attempts that failed while the broker was down (their number depends on timing) as one."""
    out = []
    pair = [f"{IFACE}.mqtt_state(string \"connecting\")", f"{IFACE}.mqtt_state(string \"disconnected\")"]
    for line in record:
        out.append(line)
        if len(out) >= 4 and out[-2:] == pair and out[-4:-2] == pair:
            del out[-2:]
    return out
