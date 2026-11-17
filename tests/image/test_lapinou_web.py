# SPDX-License-Identifier: MIT
"""The LapiNou web page (Task 12): its programs (image/core/usr/www/cgi-bin/lapinou, sourcing web.sh
and image/core/usr/share/lapinou/web-lapinou.sh), the root helper they go through
(image/core/usr/share/lapinou/webroot.sh, run by lapinou-webroot), and the page's own files.

The programs run as test_web_api.py runs LapinLibre's (its Sandbox: CGI variables, every absolute
path moved into a sandbox, stubs on PATH, the image's BusyBox under qemu when it can). lapinou-webroot
is a stand-in that checks its words as the real one does and runs webroot.sh in the sandbox;
lapinou-ota, inetd.sh and the bus are stubs that record what they are asked. The real
lapinou-webroot (userland/src/usr/lapinou-webroot) is built with the host's compiler, when there is
one, and checked on its own.

Run alone: python3 -I -B tests/image/test_lapinou_web.py
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api import Sandbox, WebApiCase, sandbox_copy, stub  # noqa: E402

CORE = ROOT / "image/core"
WWW = CORE / "usr/www"
LAPINOU_SHARE = CORE / "usr/share/lapinou"
CGIS = ["status", "conf_get", "conf_set", "secret_set", "test", "ota", "restart", "volume", "press"]
SET_OK = {"return": "0", "cleared": []}
# A click of the head's button as dbus-monitor prints it (button-daemon's signal).
CLICK = ("signal time=1 sender=:1.4 -> destination=(null destination) serial=9 path=/com/mindscape/karotz/Button; "
         "interface=com.mindscape.karotz.KarotzInterface; member=click\n")
LIBRARIES = [LAPINOU_SHARE / "web-lapinou.sh", LAPINOU_SHARE / "webroot.sh", LAPINOU_SHARE / "conf.sh"]
WRAPPER_SRC = ROOT / "userland/src/usr/lapinou-webroot/lapinou-webroot.c"
PAGE = ["index.html", "lapinou/app.js", "lapinou/style.css", "lapinou/tokens.css", "lapinou/lapin.svg", "lapinou/fr.json"]
NASTY = 'a"b$(id);c'


class LapinouSandbox(Sandbox):
    def __init__(self, busybox):
        super().__init__(busybox)
        sb = self.sb
        for src in LIBRARIES:
            sandbox_copy(src, sb / src.relative_to(CORE), sb)
        # conf.sh's default folder follows ":-", which sandbox_copy leaves alone.
        conf_sh = sb / "usr/share/lapinou/conf.sh"
        conf_sh.write_text(conf_sh.read_text(encoding="utf-8").replace(":-/usr/etc/conf}", f":-{sb}/usr/etc/conf}}"),
                           encoding="utf-8", newline="\n")
        for name in CGIS:
            src = WWW / "cgi-bin/lapinou" / name
            sandbox_copy(src, sb / src.relative_to(CORE), sb)
        # lapinou-webroot: the real one's word check, then webroot.sh as "root".
        stub(sb / "usr/karotz/bin/lapinou-webroot",
             f'echo "webroot $*" >> {self.actions}\n'
             '[ $# -ge 1 ] && [ $# -le 3 ] || exit 126\n'
             'for w; do case $w in "" | *[!a-z0-9_-]*) exit 126 ;; esac; done\n'
             f'exec {sb}/bin/sh {sb}/usr/share/lapinou/webroot.sh "$@"')
        # lapinou-ota: records; check answers as ota.answer says (or fails with ota.fail).
        stub(sb / "usr/karotz/bin/lapinou-ota",
             f'echo "lapinou-ota $*" >> {self.actions}\n'
             f'if [ -f {sb}/ota.fail ]; then cat {sb}/ota.fail >&2; exit 1; fi\n'
             'case $1 in\n'
             f'check) cat {sb}/ota.answer ;;\n'
             f'install) echo "Downloading usr.tar.gz" > {sb}/usr/ota/last.log ;;\n'
             'esac')
        self.write("ota.answer", '{"installed":"v1.0.0","available":"v1.1.0"}\n')
        # inetd.sh conf: hashes the new password as the real one does (the form only), and keeps
        # each password it was given in passwords.log, for the tests to see whose was put in place.
        stub(sb / "karotz/scripts/inetd.sh",
             f'echo "inetd.sh $*" >> {self.actions}\n'
             f'c={sb}/usr/etc/conf\n'
             f'[ -s $c/web_password ] && cat $c/web_password >> {sb}/passwords.log\n'
             '[ -s $c/web_password ] && { echo \'$6$salt$hashhashhash\' > $c/web_password.hash; rm -f $c/web_password; }\n'
             'exit 0')
        stub(sb / "usr/etc/init.d/S40lapinlibre", f'echo "S40lapinlibre $*" >> {self.actions}')
        # The bus: LapinouMqtt answers get_state with mqtt.state and set_volume with true; the rest
        # is test_web_api's stand-in.
        (self.bin / "dbus-send").rename(self.bin / "dbus-send-api")
        stub(self.bin / "dbus-send",
             'case "$*" in\n'
             '*LapinouMqtt.get_state*)\n'
             f'    {{ printf "dbus-send"; printf "\\037%s" "$@"; printf "\\n"; }} >> {self.actions}\n'
             f'    [ -f {sb}/mqtt.state ] || {{ echo "Error org.freedesktop.DBus.Error.ServiceUnknown: no" >&2; exit 1; }}\n'
             f'    echo "method return"; echo "   string \\"$(cat {sb}/mqtt.state)\\""; exit 0 ;;\n'
             '*LapinouMqtt.set_volume*)\n'
             f'    {{ printf "dbus-send"; printf "\\037%s" "$@"; printf "\\n"; }} >> {self.actions}\n'
             '    echo "method return"; echo "   boolean true"; exit 0 ;;\n'
             'esac\n'
             f'exec {self.bin}/dbus-send-api "$@"')
        for d in ("usr/ota", "usr/etc/conf", "tmp", "usr/etc/ssl/certs"):
            (sb / d).mkdir(parents=True, exist_ok=True)
        self.write("usr/etc/conf/wpa.conf", 'network={\nssid="home net"\npsk="oldpassword"\nkey_mgmt=WPA-PSK\n'
                   'scan_ssid=1\nproto=WPA RSN\n}\n', 0o600)
        self.write("usr/etc/conf/sys_version", "v1.0.0\n")

    def conf(self):
        path = self.sb / "usr/etc/conf/lapinou.conf"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def secrets(self):
        path = self.sb / "usr/etc/conf/lapinou.secrets"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def conf_value(self, section, key):
        """The value as conf.sh reads it back."""
        r = subprocess.run([str(self.bin / "sh"), "-c", f'. {self.sb}/usr/share/lapinou/conf.sh; lapinou_get "$1" "$2"', "sh",
                            section, key], env={"PATH": str(self.bin)}, capture_output=True, timeout=60)
        return r.stdout.decode("utf-8")[:-1]


def form(**fields):
    from urllib.parse import urlencode
    return urlencode(fields)


class LapinouCase(WebApiCase):
    def setUp(self):
        self.box = LapinouSandbox(self.busybox)
        self.sb = self.box.sb

    def post(self, path, **fields):
        return self.box.run(f"lapinou/{path}", method="POST", body=form(**fields))

    def post_json(self, path, **fields):
        status, headers, body = self.post(path, **fields)
        self.assertEqual(status, 200, body)
        self.assertReplyHeaders(headers)
        return json.loads(body)

    def get_json(self, path, query=""):
        return self.json(f"lapinou/{path}", query=query)

    def no_password(self):
        (self.sb / "usr/etc/conf/web_password.hash").unlink(missing_ok=True)

    def press_status(self, token):
        return self.post_json("press", action="status", token=token)

    def press_the_head(self, signal=CLICK):
        """press start, then a click of the head's button (the fake bus's signal), then waits for
        it; the token start gave."""
        reply = self.post_json("press", action="start")
        self.assertEqual(reply["return"], "0", reply)
        token = reply["token"]
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        self.box.write("signal.out", signal)
        for _ in range(150):
            if self.press_status(token)["pressed"]:
                return token
            time.sleep(0.2)
        self.fail("the press wasn't seen")

    def first_password(self, token, value="newpass123"):
        return self.post_json("secret_set", name="web_password", value=value, token=token)


class SettingsTest(LapinouCase):
    """conf_set, conf_get and secret_set."""

    def test_value_is_stored_verbatim_and_never_run(self):
        marker = self.sb / "pwned"
        for value in (NASTY, f"$(touch {marker})", f"`touch {marker}`", f"x;touch {marker}", "it's é \\ = [mqtt]"):
            with self.subTest(value=value):
                reply = self.post_json("conf_set", mqtt__user=value)
                self.assertEqual(reply, SET_OK)
                self.assertEqual(self.box.conf_value("mqtt", "user"), value)
                self.assertEqual(self.get_json("conf_get")["settings"]["mqtt.user"], value)
        self.assertFalse(marker.exists())
        self.assertNotIn("uid=", self.box.conf())
        # The value never reached an argument.
        self.assertFalse([line for line in self.box.actions_log().splitlines() if "b$(id)" in line])

    def test_several_settings_at_once_and_the_defaults(self):
        reply = self.post_json("conf_set", mqtt__host="192.168.1.50", mqtt__port="1884", mqtt__enabled="1",
                               audio__volume="35", time__tz="Europe/Paris")
        self.assertEqual(reply["return"], "0")
        settings = self.get_json("conf_get")["settings"]
        self.assertEqual((settings["mqtt.host"], settings["mqtt.port"], settings["mqtt.enabled"], settings["audio.volume"],
                          settings["time.tz"]), ("192.168.1.50", "1884", "1", "35", "Europe/Paris"))
        self.post_json("conf_set", mqtt__port="")
        self.assertEqual(self.get_json("conf_get")["settings"]["mqtt.port"], "")

    def test_bad_values_write_nothing(self):
        self.post_json("conf_set", mqtt__host="h1")
        before = self.box.conf()
        for fields, word in (({"mqtt__port": "70000"}, "65535"), ({"mqtt__host": "a b"}, "adresse"),
                             ({"audio__volume": "101"}, "100"), ({"cloud__llm_kind": "evil"}, "anthropic"),
                             ({"ota__repo": "someone/else"}, "réglage"), ({"mqtt__nothing": "1"}, "réglage"),
                             ({"cloud__llm_url": "ftp://x"}, "http"), ({"mqtt__user": "a\tb"}, "contrôle"),
                             ({"mqtt__host": "ok", "mqtt__port": "x"}, "65535")):
            with self.subTest(fields=fields):
                reply = self.post_json("conf_set", **fields)
                self.assertError(reply, word)
                self.assertEqual(self.box.conf(), before)

    def test_secret_is_never_given_back(self):
        values = {"mqtt_password": "hunter2-mqtt", "cloud_stt_key": "sk-stt-123", "cloud_llm_key": 'sk "llm" $x',
                  "cloud_tts_key": "sk-tts-456", "ota_token": "github_pat_789"}
        reply = self.get_json("conf_get")
        self.assertEqual(reply["settings"]["mqtt_password"], {"set": False})
        for name, value in values.items():
            self.assertEqual(self.post_json("secret_set", name=name, value=value), {"return": "0"})
        status, _, body = self.box.run("lapinou/conf_get")
        self.assertEqual(status, 200)
        for name, value in values.items():
            self.assertNotIn(value.encode(), body)
            self.assertEqual(json.loads(body)["settings"][name], {"set": True})
        self.assertNotIn(b"oldpassword", body)
        self.assertEqual(json.loads(body)["settings"]["wifi_password"], {"set": True})
        self.assertIn('cloud_llm_key=sk "llm" $x\n', self.box.secrets())
        self.assertEqual(stat.S_IMODE((self.sb / "usr/etc/conf/lapinou.secrets").stat().st_mode), 0o600)
        # Nor logged, nor in an argument.
        everything = self.box.actions_log() + "\n".join(self.box.syslog_lines())
        for value in values.values():
            self.assertNotIn(value, everything)
        # An empty value removes it.
        self.post_json("secret_set", name="ota_token", value="")
        self.assertEqual(self.get_json("conf_get")["settings"]["ota_token"], {"set": False})

    def test_only_post_changes(self):
        for path, query in (("conf_set", "mqtt__user=x"), ("secret_set", "name=mqtt_password&value=x"),
                            ("test", "what=server"), ("restart", "what=mqtt"), ("ota", "action=check"),
                            ("ota", "action=install"), ("volume", "value=10")):
            with self.subTest(path=path, query=query):
                status, _, body = self.box.run(f"lapinou/{path}", query)
                self.assertEqual(status, 405, body)
        self.assertEqual(self.box.conf(), "")
        self.assertEqual(self.box.secrets(), "")
        self.assertNotIn("lapinou-ota", self.box.actions_log())
        self.assertNotIn("killall", self.box.actions_log())

    def test_cross_site_requests_refused(self):
        for headers in ({"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _, _ = self.box.run("lapinou/conf_set", method="POST", body=form(mqtt__user="x"), headers=headers)
                self.assertEqual(status, 403)
                status, _, _ = self.box.run("lapinou/secret_set", method="POST", body=form(name="web_password", value="newpass123"),
                                            headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.box.conf(), "")
        self.assertNotIn("inetd.sh", self.box.actions_log())

    def test_secrets_need_the_page_password(self):
        self.no_password()
        # No secret yet: the page works without a password.
        self.assertEqual(self.post_json("secret_set", name="mqtt_password", value="first"), {"return": "0"})
        status = self.get_json("status")
        self.assertEqual((status["password"], status["password_needed"]), ("0", "1"))
        # A secret is stored and there is no password: everything but the password's creation is refused.
        for path, kw in (("conf_get", {}), ("conf_set", {"method": "POST", "body": form(mqtt__user="x")}),
                         ("secret_set", {"method": "POST", "body": form(name="mqtt_password", value="y")}),
                         ("test", {"method": "POST", "body": form(what="server")}),
                         ("ota", {"query": "action=status"}), ("restart", {"method": "POST", "body": form(what="mqtt")})):
            with self.subTest(path=path):
                status, headers, body = self.box.run(f"lapinou/{path}", user=None, **kw)
                self.assertEqual(status, 401, body)
                self.assertEqual(json.loads(body)["return"], "1")
        self.assertIn("mqtt_password=first", self.box.secrets())
        # The page offers to create the password: it is taken after a press on the head (R46).
        reply = self.first_password(self.press_the_head())
        self.assertEqual(reply, {"return": "0"})
        self.assertIn("inetd.sh conf", self.box.actions_log())
        self.assertTrue((self.sb / "usr/etc/conf/web_password.hash").read_text().startswith("$6$"))
        self.assertFalse((self.sb / "usr/etc/conf/web_password").exists())
        self.assertEqual(self.get_json("status")["password_needed"], "0")

    def test_web_password_rules(self):
        for value, word in (("short", "8 à 64"), ("with space1", "espace"), ('with"quote', "guillemet"),
                            ("with:colon1", "deux-points"), ("back\\slash1", "barre")):
            with self.subTest(value=value):
                self.assertError(self.post_json("secret_set", name="web_password", value=value), word)
        self.assertNotIn("inetd.sh", self.box.actions_log())

    def test_wifi_password_refuses_quote_and_backslash(self):
        wpa = self.sb / "usr/etc/conf/wpa.conf"
        before = wpa.read_text()
        for value in ('pass"word1', "pass\\word1"):
            with self.subTest(value=value):
                reply = self.post_json("secret_set", name="wifi_password", value=value)
                self.assertError(reply, "guillemet")
                self.assertIn("barre oblique inverse", reply["msg"])
                self.assertEqual(wpa.read_text(), before)
        self.assertError(self.post_json("conf_set", network__wifi_ssid='my"net'), "guillemet")
        self.assertEqual(self.post_json("secret_set", name="wifi_password", value="new $pass;1"), {"return": "0"})
        self.assertEqual(self.post_json("conf_set", network__wifi_ssid="Box 5G", network__ip="192.168.1.20",
                                        network__netmask="255.255.255.0", network__gateway="192.168.1.1",
                                        network__dns="192.168.1.1", network__hostname="kitchen"), SET_OK)
        self.assertEqual(wpa.read_text(), 'network={\nssid="Box 5G"\npsk="new $pass;1"\nkey_mgmt=WPA-PSK\nscan_ssid=1\n'
                                          'proto=WPA RSN\n}\n')
        self.assertEqual(stat.S_IMODE(wpa.stat().st_mode), 0o600)
        self.assertEqual((self.sb / "usr/etc/conf/network.conf").read_text(),
                         'IP="192.168.1.20"\nNETMASK="255.255.255.0"\nGATEWAY="192.168.1.1"\nDNS="192.168.1.1"\n')
        self.assertEqual((self.sb / "usr/etc/conf/hostname").read_text(), "kitchen\n")
        settings = self.get_json("conf_get")["settings"]
        self.assertEqual((settings["network.wifi_ssid"], settings["network.ip"], settings["network.hostname"]),
                         ("Box 5G", "192.168.1.20", "kitchen"))
        self.assertEqual(self.post_json("conf_set", network__ip=""), SET_OK)
        self.assertIn('IP=""', (self.sb / "usr/etc/conf/network.conf").read_text())

    def test_server_host_sets_the_rabbits_servers(self):
        self.assertEqual(self.post_json("conf_set", server__host="192.168.1.30", server__enabled="1"), SET_OK)
        karotz = (self.sb / "usr/etc/conf/karotz.conf").read_text()
        for line in ("ip_server=192.168.1.30", "port_server=9123", "url_app=http://192.168.1.30:8080",
                     "domain_app=192.168.1.30", "lang=en-GB"):
            self.assertIn(line + "\n", karotz)
        voice = (self.sb / "usr/etc/conf/voice.conf").read_text()
        self.assertIn("fr=http://192.168.1.30:8081/fr\n", voice)
        self.assertIn("server0=192.168.1.30\n", voice)


class ActionsTest(LapinouCase):
    """ota, test, restart, volume and status."""

    def test_ota_check_and_install_call_lapinou_ota(self):
        reply = self.post_json("ota", action="check")
        self.assertEqual((reply["return"], reply["installed"], reply["available"]), ("0", "v1.0.0", "v1.1.0"))
        self.assertIn("lapinou-ota check", self.box.actions_log())
        self.assertEqual((self.sb / "usr/ota/available").read_text(), "v1.1.0\n")
        kept = json.loads((self.sb / "usr/ota/check.json").read_text())
        self.assertEqual((kept["installed"], kept["available"]), ("v1.0.0", "v1.1.0"))
        status = self.get_json("ota", "action=status")
        self.assertEqual((status["running"], status["last_check"]["available"]), (False, "v1.1.0"))
        self.assertEqual(self.get_json("status")["last_check"]["available"], "v1.1.0")
        reply = self.post_json("ota", action="install")
        self.assertEqual(reply, {"return": "0", "started": True})
        for _ in range(100):
            if "lapinou-ota install" in self.box.actions_log():
                break
            __import__("time").sleep(0.1)
        self.assertIn("lapinou-ota install", self.box.actions_log())
        self.assertNotIn("allow-downgrade", self.box.actions_log())
        self.assertIn("Downloading", self.get_json("ota", "action=status")["log"])

    def test_ota_check_failure(self):
        self.box.write("ota.fail", "lapinou-ota: ERROR: the signature doesn't match\n")
        reply = self.post_json("ota", action="check")
        self.assertError(reply, "signature")
        self.assertFalse((self.sb / "usr/ota/available").exists())

    def test_ota_running_refuses_a_second(self):
        lock = self.sb / "tmp/lapinou-ota.lock"
        holder = subprocess.Popen(["flock", str(lock), "sleep", "5"])
        try:
            for _ in range(50):
                if lock.exists():
                    break
                __import__("time").sleep(0.1)
            __import__("time").sleep(0.3)
            self.assertError(self.post_json("ota", action="install"), "en cours")
            self.assertTrue(self.get_json("ota", "action=status")["running"])
        finally:
            holder.kill()
            holder.wait()
        self.assertNotIn("lapinou-ota install", self.box.actions_log())

    def test_tests(self):
        # The server: its health endpoint.
        self.assertError(self.post_json("test", what="server"), "Aucun serveur")
        self.post_json("conf_set", server__host="192.168.1.30")
        reply = self.post_json("test", what="server")
        self.assertEqual(reply["return"], "0")
        self.assertIn("\x1fhttp://192.168.1.30:8080/api/health", self.box.actions_log())
        self.box.write("curl.code", "000")
        self.assertError(self.post_json("test", what="server"), "ne répond pas")
        # A cloud service: its key goes to curl's configuration on stdin, never in its arguments.
        self.box.write("curl.code", "401")
        self.post_json("secret_set", name="cloud_llm_key", value="sk-ant-secret")
        self.assertError(self.post_json("test", what="llm"), "refuse la clé")
        log = self.box.actions_log()
        args = [line for line in log.splitlines() if line.startswith("curl\x1f")]
        self.assertIn("https://api.anthropic.com/v1/models", args[-1])
        self.assertNotIn("sk-ant-secret", "\n".join(args))
        self.assertIn('curl-config header = "x-api-key: sk-ant-secret"', log)
        self.box.write("curl.code", "200")
        self.post_json("conf_set", cloud__stt_url="http://192.168.1.40:8000/v1/audio/transcriptions")
        self.assertEqual(self.post_json("test", what="stt")["return"], "0")
        self.assertIn("\x1fhttp://192.168.1.40:8000/v1/models", self.box.actions_log())
        # MQTT: the bridge's own state.
        self.assertError(self.post_json("test", what="mqtt"), "pas activé")
        self.post_json("conf_set", mqtt__enabled="1")
        self.box.write("mqtt.state", "connected")
        self.assertEqual(self.post_json("test", what="mqtt")["return"], "0")
        self.box.write("mqtt.state", "disconnected")
        self.assertError(self.post_json("test", what="mqtt"), "broker")
        self.assertError(self.post_json("test", what="evil"), "what")

    def test_restart(self):
        self.assertEqual(self.post_json("restart", what="mqtt"), {"return": "0"})
        self.assertIn("killall lapinou-mqtt", self.box.actions_log())
        self.assertEqual(self.post_json("restart", what="cloud"), {"return": "0"})
        self.assertIn("killall lapinou-cloud", self.box.actions_log())
        self.assertError(self.post_json("restart", what="controller"), "what")

    def test_volume(self):
        self.assertEqual(self.post_json("volume", value="40"), {"return": "0"})
        self.assertIn("LapinouMqtt.set_volume\x1fint32:40", self.box.actions_log())
        self.assertError(self.post_json("volume", value="140"), "value")

    def test_status(self):
        self.box.write("mqtt.state", "connected")
        self.post_json("conf_set", server__enabled="1", server__host="192.168.1.30", audio__volume="55")
        reply = self.get_json("status")
        for key, value in (("return", "0"), ("version", "v1.0.0"), ("mode", "server"), ("server_host", "192.168.1.30"),
                           ("server_link", "connected"), ("mqtt", "connected"), ("volume", "55"), ("password", "1"),
                           ("password_needed", "0"), ("last_check", None)):
            self.assertEqual(reply[key], value, key)


class CorrectionTest(LapinouCase):
    """The review's points (rapport de correction 1): R46 and the helper's files."""

    def secret_is_set(self, name):
        return self.get_json("conf_get")["settings"][name]["set"]

    def test_values_with_equals_and_brackets(self):
        for value in ("a=b", "[mqtt]", "x=[secrets]=y", "host=evil\n[mqtt]"):
            with self.subTest(value=value):
                if "\n" in value:
                    self.assertError(self.post_json("conf_set", mqtt__user=value), "contrôle")
                    continue
                self.assertEqual(self.post_json("conf_set", mqtt__user=value), SET_OK)
                self.assertEqual(self.box.conf_value("mqtt", "user"), value)
        # Still one [mqtt] section with one user line.
        conf = self.box.conf()
        self.assertEqual(conf.count("\n[mqtt]\n") + conf.startswith("[mqtt]\n"), 1, conf)
        self.assertEqual(len([l for l in conf.splitlines() if l.startswith("user=")]), 1, conf)

    def test_a_changed_address_clears_its_key(self):
        self.post_json("conf_set", mqtt__host="192.168.1.50")
        self.post_json("secret_set", name="cloud_llm_key", value="sk-ant-kept")
        self.post_json("secret_set", name="mqtt_password", value="mqtt-kept")
        # The same value again: nothing changes.
        self.assertEqual(self.post_json("conf_set", mqtt__host="192.168.1.50"), SET_OK)
        self.assertTrue(self.secret_is_set("mqtt_password"))
        # A new address: the key goes, and the page is told.
        reply = self.post_json("conf_set", cloud__llm_url="https://evil.example")
        self.assertEqual(reply, {"return": "0", "cleared": ["cloud_llm_key"]})
        self.assertFalse(self.secret_is_set("cloud_llm_key"))
        self.assertNotIn("sk-ant-kept", self.box.secrets())
        # Tester then sends no key to the new address.
        self.post_json("test", what="llm")
        self.assertNotIn("sk-ant-kept", self.box.actions_log())
        self.assertNotIn("x-api-key: sk", self.box.actions_log())
        # The broker's address too; the kind of model as well.
        self.assertEqual(self.post_json("conf_set", mqtt__host="evil.example")["cleared"], ["mqtt_password"])
        self.post_json("secret_set", name="cloud_llm_key", value="sk-ant-2")
        self.assertEqual(self.post_json("conf_set", cloud__llm_kind="openai")["cleared"], ["cloud_llm_key"])
        # A new key given with the new address is kept.
        reply = self.post_json("conf_set", cloud__stt_url="https://stt.example/v1", secret__cloud_stt_key="sk-new")
        self.assertEqual(reply, SET_OK)
        self.assertTrue(self.secret_is_set("cloud_stt_key"))
        self.assertIn("cloud_stt_key=sk-new\n", self.box.secrets())

    def test_http_only_on_the_local_network_with_a_key(self):
        # Without a key, any address.
        self.assertEqual(self.post_json("conf_set", cloud__tts_url="http://tts.example/v1")["return"], "0")
        # A key with an http:// address outside the local network: refused, either way round.
        self.assertError(self.post_json("secret_set", name="cloud_tts_key", value="sk-tts"), "https")
        self.assertError(self.post_json("conf_set", cloud__tts_url="http://evil.example:8080/v1",
                                        secret__cloud_tts_key="sk-tts"), "https://")
        self.assertNotIn("sk-tts", self.box.secrets())
        # On the local network, http:// is fine.
        for url in ("http://192.168.1.40:8000/v1", "http://10.0.0.5/v1", "http://172.20.1.1/v1", "http://speech.local/v1",
                    "https://api.example.com/v1"):
            with self.subTest(url=url):
                self.assertEqual(self.post_json("conf_set", cloud__tts_url=url, secret__cloud_tts_key="sk-tts")["return"], "0")
        for url in ("http://172.32.0.1/v1", "http://192.169.0.1/v1", "http://8.8.8.8/v1", "http://user@evil.example/v1",
                    "http://192.168.1.1.evil.example/v1"):
            with self.subTest(url=url):
                self.assertError(self.post_json("conf_set", cloud__tts_url=url, secret__cloud_tts_key="sk-tts"), "https://")
        # A key already stored with such an address (written by hand): Tester doesn't send it.
        self.box.write("usr/etc/conf/lapinou.conf", "[cloud]\ntts_url=http://evil.example/v1\n")
        self.box.actions.unlink(missing_ok=True)
        self.assertError(self.post_json("test", what="tts"), "http://")
        self.assertNotIn("curl", self.box.actions_log())

    def test_volume_follows_the_password_rule(self):
        self.no_password()
        self.post_json("secret_set", name="mqtt_password", value="first")
        status, _, body = self.box.run("lapinou/volume", method="POST", body=form(value="40"), user=None)
        self.assertEqual(status, 401, body)
        self.assertNotIn("set_volume", self.box.actions_log())

    def test_first_password_needs_a_press_on_the_head(self):
        self.no_password()
        # Without the press: refused.
        self.assertError(self.first_password(""), "tête du lapin")
        self.assertFalse((self.sb / "usr/etc/conf/web_password.hash").exists())
        # A press too old doesn't count.
        old = "0123456789abcdef0123456789abcdef"
        self.box.write("var/run/lapinou-press.ok", f"{old} 1000\n")
        self.assertError(self.first_password(old), "tête du lapin")
        self.box.write("var/run/lapinou-press.ok", f"{old} {int(time.time()) - 61}\n")
        self.assertError(self.first_password(old), "tête du lapin")
        (self.sb / "var/run/lapinou-press.ok").unlink()
        # The LED blinks while the press is waited for; then the press is seen.
        token = self.press_the_head()
        led = [c for c in self.box.dbus_calls() if c[0] == "Led"]
        self.assertEqual(led[0][1], "pulse")
        self.assertEqual(self.first_password(token), {"return": "0"})
        self.assertTrue((self.sb / "usr/etc/conf/web_password.hash").exists())
        # Used up; and with a password, there is no press to make.
        self.assertFalse((self.sb / "var/run/lapinou-press.ok").exists())
        self.assertError(self.post_json("press", action="start"), "déjà un mot de passe")
        # The LED goes back to the web API's colour once the watch ends.
        for _ in range(100):
            if [c for c in self.box.dbus_calls() if c[:2] == ("Led", "light")]:
                break
            time.sleep(0.2)
        self.assertEqual([c for c in self.box.dbus_calls() if c[:2] == ("Led", "light")][-1][2][-1], "string:00FF00")

    def test_a_password_change_needs_no_press(self):
        self.assertEqual(self.post_json("secret_set", name="web_password", value="changed123"), {"return": "0"})
        self.assertNotIn("dbus-monitor", self.box.actions_log())

    def test_helper_files_are_never_readable_by_others(self):
        # chmod records the mode a file was made with, before changing it: every file the helper
        # writes is made without group or other permissions.
        (self.box.bin / "chmod").rename(self.box.bin / "chmod-real")
        stub(self.box.bin / "chmod", f'for a; do last=$a; done\nls -ln "$last" >> {self.sb}/made.log\n'
                                     f'exec {self.box.bin}/chmod-real "$@"')
        self.post_json("secret_set", name="mqtt_password", value="hunter2")
        self.post_json("conf_set", mqtt__host="192.168.1.50", secret__mqtt_password="hunter3", network__hostname="kitchen",
                       network__ip="192.168.1.20")
        self.post_json("secret_set", name="wifi_password", value="newwifi123")
        self.post_json("secret_set", name="web_password", value="changed123")
        self.post_json("ota", action="check")
        made = (self.sb / "made.log").read_text().splitlines()
        self.assertTrue(made)
        for line in made:
            self.assertRegex(line, r"^-rw-------", line)
        self.assertTrue([line for line in made if "/hostname." in line], made)
        for name, mode in (("lapinou.secrets", 0o600), ("wpa.conf", 0o600), ("lapinou.conf", 0o644), ("network.conf", 0o644),
                           ("hostname", 0o644)):
            self.assertEqual(stat.S_IMODE((self.sb / "usr/etc/conf" / name).stat().st_mode), mode, name)
        self.assertEqual(stat.S_IMODE((self.sb / "usr/ota/check.json").stat().st_mode), 0o644)

    def helper(self, *args, stdin=b""):
        r = subprocess.run([str(self.box.bin / "sh"), str(self.sb / "usr/share/lapinou/webroot.sh"), *args],
                           input=stdin, env={"PATH": str(self.box.bin)}, capture_output=True, timeout=120)
        return json.loads(r.stdout)

    def test_stdin_is_capped_never_cut(self):
        text = (LAPINOU_SHARE / "webroot.sh").read_text(encoding="utf-8")
        self.assertNotIn("$(cat)", text)
        self.assertError(self.helper("secret", "ota_token", stdin=b"x" * 20000), "trop longue")
        # A set request one byte over the limit is refused whole, not cut to its first 8192 bytes.
        line = b"mqtt\tuser\t" + b"u" * 200 + b"\n"
        over = line + b"mqtt\thost\th1\n" + b"#" * (8193 - len(line) - 13)
        self.assertEqual(len(over), 8193)
        self.assertError(self.helper("set", stdin=over), "trop longue")
        self.assertEqual(self.box.conf(), "")
        # At the limit: taken (the junk line is then refused as malformed, nothing written either).
        self.assertError(self.helper("set", stdin=over[:8192]), "mal formé")

    def test_userinfo_and_fragment_tricks_refused(self):
        self.post_json("secret_set", name="cloud_llm_key", value="sk-ant-x")
        for url, word in (("http://evil.example#@10.0.0.1/v1", "https://"), ("http://evil.example?@10.0.0.1/v1", "https://"),
                          ("http://user@10.0.0.1/v1", "identifiant"), ("https://user:pw@api.example.com/v1", "identifiant"),
                          ("http://evil.example/#@10.0.0.1", "https://")):
            with self.subTest(url=url):
                self.assertError(self.post_json("conf_set", cloud__llm_url=url, secret__cloud_llm_key="sk-ant-x"), word)
        # Written by hand: Tester still sends no key there.
        self.box.write("usr/etc/conf/lapinou.conf", "[cloud]\nllm_url=http://evil.example#@10.0.0.1/v1\n")
        self.box.actions.unlink(missing_ok=True)
        self.assertEqual(self.post_json("test", what="llm")["return"], "1")
        self.assertNotIn("curl", self.box.actions_log())

    def test_press_only_from_the_head_button(self):
        self.no_password()
        token = self.post_json("press", action="start")["token"]
        # The watch asks the bus for button-daemon's own signal only.
        for _ in range(50):
            if "dbus-monitor" in self.box.actions_log():
                break
            time.sleep(0.2)
        rule = [line for line in self.box.actions_log().splitlines() if line.startswith("dbus-monitor")][0]
        for part in ("sender='com.mindscape.karotz.Button'", "path='/com/mindscape/karotz/Button'", "member='click'"):
            self.assertIn(part, rule)
        # A click from another object is ignored.
        self.box.write("signal.out", CLICK.replace("path=/com/mindscape/karotz/Button", "path=/com/mindscape/karotz/Fake"))
        time.sleep(3)
        self.assertFalse(self.press_status(token)["pressed"])
        self.assertError(self.first_password(token), "tête du lapin")

    def test_press_belongs_to_the_browser_that_asked(self):
        self.no_password()
        token = self.press_the_head()
        other = "f" * 32
        # Another client: no token, a made-up one, or none at all; and a second wait can't start.
        self.assertFalse(self.press_status(other)["pressed"])
        self.assertFalse(self.press_status("")["pressed"])
        self.assertError(self.first_password(other), "tête du lapin")
        self.assertError(self.first_password(""), "tête du lapin")
        self.assertError(self.post_json("press", action="start"), "attend déjà un appui")
        self.assertTrue((self.sb / "var/run/lapinou-press.ok").exists())
        # Its own browser still can.
        self.assertTrue(self.press_status(token)["pressed"])
        self.assertEqual(self.first_password(token), {"return": "0"})

    def passwords(self):
        path = self.sb / "passwords.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_first_password_race_only_the_token_holder_wins(self):
        # The owner's request (with the press's token) against requests without one, all at once,
        # several times: only the owner's password is ever put in place.
        import threading
        for round_ in range(3):
            with self.subTest(round=round_):
                self.no_password()
                (self.sb / "passwords.log").unlink(missing_ok=True)
                for name in ("lapinou-press.ok", "lapinou-press.pending"):
                    (self.sb / "var/run" / name).unlink(missing_ok=True)
                token = self.press_the_head()
                replies = {}

                def send(who, tok, value):
                    replies[who] = self.first_password(tok, value)
                threads = [threading.Thread(target=send, args=("owner", token, "ownerpass1"))]
                threads += [threading.Thread(target=send, args=(f"other{i}", "", f"attacker{i}x")) for i in range(3)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(120)
                self.assertEqual(replies["owner"], {"return": "0"})
                for i in range(3):
                    self.assertEqual(replies[f"other{i}"]["return"], "1", replies)
                self.assertEqual(self.passwords(), ["ownerpass1"])
                # Wait for the watch to end (it gives the LED back) before the next round.
                for _ in range(100):
                    if len([c for c in self.box.dbus_calls() if c[:2] == ("Led", "light")]) > round_:
                        break
                    time.sleep(0.2)
                time.sleep(0.5)

    def test_first_mode_only_while_there_is_no_password(self):
        # The helper itself: "first" is refused once a password exists, token or not; without
        # "first" (a request httpd authenticated), never the first password.
        self.assertError(self.helper("secret", "web_password", "first", stdin=b"\nnewpass123"), "déjà un mot de passe")
        self.assertEqual(self.passwords(), [])
        self.no_password()
        self.assertError(self.helper("secret", "web_password", stdin=b"\nnewpass123"), "pas encore de mot de passe")
        token = self.press_the_head()
        self.assertError(self.helper("secret", "web_password", stdin=f"{token}\nnewpass123".encode()), "pas encore de mot de passe")
        self.assertError(self.helper("secret", "mqtt_password", "first", stdin=b"x"), "inconnue")
        self.assertEqual(self.passwords(), [])
        self.assertEqual(self.helper("secret", "web_password", "first", stdin=f"{token}\nnewpass123".encode()), {"return": "0"})
        self.assertEqual(self.passwords(), ["newpass123"])

    def test_press_colour_only_for_its_browser(self):
        self.no_password()
        reply = self.post_json("press", action="start")
        colour = reply["color"]
        self.assertIn(colour, ("rouge", "bleu", "vert", "jaune", "violet", "blanc"))
        hexes = {"rouge": "FF0000", "bleu": "0000FF", "vert": "00FF00", "jaune": "FFFF00", "violet": "FF00FF", "blanc": "FFFFFF"}
        for _ in range(50):
            pulses = [c for c in self.box.dbus_calls() if c[:2] == ("Led", "pulse")]
            if pulses:
                break
            time.sleep(0.2)
        self.assertEqual(pulses[0][2][1], f"string:{hexes[colour]}")
        self.assertEqual(self.press_status(reply["token"]).get("color"), colour)
        for token in ("", "f" * 32):
            self.assertNotIn("color", self.press_status(token))
        busy = self.post_json("press", action="start")
        self.assertError(busy, "n'appuyez pas sur la tête")
        self.assertNotIn(colour, busy["msg"])
        self.assertNotIn("token", busy)

    def test_press_is_used_once_by_two_at_once(self):
        self.no_password()
        token = self.press_the_head()
        # The first password replaced by the hash by the first taker: two takers at once, one wins.
        import threading
        replies = []

        def take():
            _, _, out = self.box.run("lapinou/secret_set", method="POST", body=form(name="web_password", value="newpass123",
                                                                                    token=token))
            replies.append(json.loads(out))
        threads = [threading.Thread(target=take) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(120)
        self.assertEqual(sorted(r["return"] for r in replies), ["0", "1"], replies)
        self.assertFalse((self.sb / "var/run/lapinou-press.ok").exists())


class FilesTest(unittest.TestCase):
    """The programs' and the page's files: listed, safe, small and self-contained."""

    def test_programs_are_listed_and_safe(self):
        listed = (ROOT / "system/core-files.txt").read_text(encoding="utf-8")
        for name in CGIS:
            self.assertRegex(listed, rf"(?m)^usr:www/cgi-bin/lapinou/{name} f 0755 0:0 core\b")
        self.assertRegex(listed, r"(?m)^usr:www/cgi-bin/lapinou d 0755 0:0\b")
        for path in ("web-lapinou.sh", "webroot.sh"):
            self.assertRegex(listed, rf"(?m)^usr:share/lapinou/{re.escape(path)} f 0644 0:0 core\b")
        # The root helper: setuid root, for the group web only.
        self.assertRegex(listed, r"(?m)^usr:karotz/bin/lapinou-webroot f 4750 0:80 own\b")
        for name in PAGE:
            self.assertRegex(listed, rf"(?m)^usr:www/{re.escape(name)} f 0644 0:0 core\b")
        self.assertRegex(listed, r"(?m)^usr:www/avance.html f 0644 0:0 core\b")
        on_disk = sorted(p.name for p in (WWW / "cgi-bin/lapinou").iterdir())
        self.assertEqual(on_disk, sorted(CGIS))
        paths = [WWW / "cgi-bin/lapinou" / c for c in CGIS]
        for path in [*paths, *LIBRARIES]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("SPDX-License-Identifier: MIT", "\n".join(text.splitlines()[:5]))
            code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
            for bad in ("eval ", "sh -c", "bash -c", "`"):
                self.assertNotIn(bad, code, f"{path.name}: {bad}")
            r = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True, timeout=30)
            self.assertEqual((r.returncode, r.stderr), (0, ""), path)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh\n"), path)
            self.assertIn(". /usr/share/lapinlibre/web.sh\nweb_begin ", text)
            self.assertIn(". /usr/share/lapinou/web-lapinou.sh\n", text)
        # A value never goes into lapinou-webroot's arguments: only the fixed words and checked names.
        for path in paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                if "lapinou_reply" in line and not line.lstrip().startswith("#"):
                    self.assertRegex(line, r'lapinou_reply (get|set|secret "\$name"|test "\$PV"|ota "\$action"|restart "\$PV"|press start|press status|secret web_password first|secret web_password)( |$)', line)
        if shutil.which("shellcheck"):
            r = subprocess.run(["shellcheck", "-s", "sh", "-e", "SC1091,SC2034,SC2154", *map(str, paths),
                                str(LAPINOU_SHARE / "web-lapinou.sh"), str(LAPINOU_SHARE / "webroot.sh")],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout[-3000:])

    def test_page_is_self_contained_and_small(self):
        total = 0
        for name in PAGE:
            path = WWW / name
            text = path.read_text(encoding="utf-8")
            total += len(text.encode("utf-8"))
            self.assertIn("SPDX-License-Identifier: MIT", "\n".join(text.splitlines()[:5]) if not name.endswith(".json")
                          else text[:200], name)
            # Nothing from outside: no CDN, no web font, no remote image.
            self.assertNotRegex(text, r"(src|href)=[\"']?(https?:)?//", name)
            self.assertNotRegex(text, r"url\(\s*[\"']?(https?:)?//", name)
            self.assertNotIn("@import", text, name)
        # The whole web page (R45): LapiNou's and LapinLibre's ("Avancé") together.
        for name in ("avance.html", "app.js", "style.css"):
            total += (WWW / name).stat().st_size
        self.assertLess(total, 150 * 1024, "the rabbit's whole web page must stay under 150 KB")
        script = (WWW / "lapinou/app.js").read_text(encoding="utf-8")
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "http://",
                    "https://"):
            self.assertNotIn(bad, script)
        css = (WWW / "lapinou/style.css").read_text(encoding="utf-8") + (WWW / "lapinou/tokens.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("prefers-color-scheme: dark", css)

    def test_texts_come_from_fr_json(self):
        texts = json.loads((WWW / "lapinou/fr.json").read_text(encoding="utf-8"))
        script = (WWW / "lapinou/app.js").read_text(encoding="utf-8")
        page = (WWW / "index.html").read_text(encoding="utf-8")
        used = set(re.findall(r'\bt\("([a-z0-9_.]+)"\s*[,)]', script)) | set(re.findall(r'data-t="([a-z0-9_.]+)"', page))
        self.assertTrue(used)
        self.assertEqual(sorted(used - set(texts)), [], "texts the page uses that fr.json lacks")
        # The keys app.js makes from a prefix.
        made = [f"led.c{c}" for c in re.search(r"LED_COLORS = \[([^\]]*)\]", script).group(1).replace('"', "").replace(" ", "").split(",")]
        made += [f"ears.{e}" for e in re.findall(r"(\w+): \[\d+, \d+\]", script)]
        made += [f"mode.{m}" for m in ("server", "cloud", "none")]
        made += [f"link.{s}" for s in ("connected", "connecting", "disconnected", "disabled", "absent")]
        self.assertEqual(sorted(set(made) - set(texts)), [])
        # The tabs of the brief, the quick controls, and the old page kept under "Avancé".
        for tab in ("etat", "reseau", "mqtt", "serveur", "cloud", "voix", "securite", "maj"):
            self.assertIn(f'data-tab="{tab}"', page)
        self.assertIn('href="avance.html"', page)
        for path in ("lapinou/status", "lapinou/conf_get", "lapinou/conf_set", "lapinou/secret_set", "lapinou/test",
                     "lapinou/ota", "lapinou/restart", "lapinou/volume", "lapinou/press", "lapinlibre/status", "leds", "ears",
                     "tts"):
            self.assertRegex(script, rf'"{re.escape(path)}"', path)

    def test_old_page_moved_unchanged(self):
        old = (WWW / "avance.html").read_text(encoding="utf-8")
        self.assertIn('<section id="sleep" class="card action-card">', old)
        self.assertIn('src="app.js"', old)
        self.assertNotIn("lapinou/", (WWW / "app.js").read_text(encoding="utf-8"))


class WrapperTest(unittest.TestCase):
    """lapinou-webroot itself, built with the host's compiler: the words it takes, and that the
    script gets them, the standard input, a clean environment and no other descriptor."""

    @classmethod
    def setUpClass(cls):
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc:
            raise unittest.SkipTest("no C compiler")
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name)
        cls.real = out / "real"
        cls.test = out / "test"
        for exe, extra in ((cls.real, []), (cls.test, ["-DLAPINOU_WEBROOT_TEST"])):
            subprocess.run([cc, "-std=gnu99", "-Wall", "-Wextra", "-Werror", *extra, "-o", str(exe), str(WRAPPER_SRC)],
                           check=True, timeout=120)
        cls.script = out / "script.sh"
        cls.script.write_text('echo "args:$*"\necho "stdin:$(cat)"\nenv | sort\nls /proc/$$/fd\n', newline="\n")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_test(self, *args, stdin=b"", env=None):
        e = {"LAPINOU_WEBROOT_SCRIPT": str(self.script), "IFS": "x", "ENV": "/evil", "SECRET": "1", **(env or {})}
        return subprocess.run([str(self.test), *args], input=stdin, env=e, capture_output=True, timeout=30)

    def test_passes_words_stdin_and_a_clean_environment(self):
        r = self.run_test("secret", "mqtt_password", stdin=NASTY.encode())
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.decode()
        self.assertIn("args:secret mqtt_password\n", out)
        self.assertIn(f"stdin:{NASTY}\n", out)
        self.assertNotIn("SECRET=", out)
        self.assertNotIn("ENV=/evil", out)
        self.assertIn("PATH=/bin:/sbin:/usr/bin:/usr/sbin\n", out)

    def test_refuses_bad_words(self):
        for args in ((), ("a", "b", "c", "d"), ("set;id",), ("x" * 33,), ("Set",), ("",), ("../x",), ("a b",)):
            with self.subTest(args=args):
                r = self.run_test(*args)
                self.assertEqual(r.returncode, 126, r.stdout)
                self.assertEqual(r.stdout, b"")

    def test_real_build_refuses_a_caller_outside_the_group_web(self):
        if os.getuid() == 0:
            self.skipTest("run as root")
        r = subprocess.run([str(self.real), "get"], capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 126)
        self.assertIn(b"group web", r.stderr)


if __name__ == "__main__":
    unittest.main()
