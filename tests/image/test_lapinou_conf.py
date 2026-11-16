# SPDX-License-Identifier: MIT
"""LapiNou's settings on the rabbit: image/core/usr/share/lapinou/conf.sh (lapinou_get, lapinou_set,
lapinou_secret_get, lapinou_secret_set), the C reader in userland/src/usr/libkarotz-common/
lapinou_conf.c, and the USB key's new settings in image/usbkey/applysettings.sh.

The shell code runs under the image's own BusyBox (qemu-user) when this machine can run it, and under
the host's sh and bash too. The C reader is built with the host's compiler and GLib when they exist.

Run alone: python3 -I -B tests/image/test_lapinou_conf.py
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core_boot import arm_busybox  # noqa: E402

CONF_SH = ROOT / "image/core/usr/share/lapinou/conf.sh"
APPLYSETTINGS = ROOT / "image/usbkey/applysettings.sh"
INSTALL_LIB = ROOT / "image/usbkey/install-lib.sh"
C_DIR = ROOT / "userland/src/usr/libkarotz-common"
APPLETS = ["sh", "awk", "sed", "grep", "cat", "tr", "date", "mv", "rm", "mkdir", "chmod", "printf", "env",
           "sleep", "touch", "ls", "cp", "head", "tail", "cut", "md5sum", "sync", "kill", "wc"]
# Values a settings page or the USB key could carry: each must come back exactly as given.
NASTY = ['a b"c$d;e`f é', "it's", "back\\slash and \\n and \\\\ and \\=", "a=b=c", "# not a comment", "[mqtt]",
         "line one\nline two", " leading and trailing ", "$(touch {m})", "`touch {m}`", "${HOME}", "tab\there",
         "ünïcödé 日本語 🐇", "x" * 4000, "end with backslash\\", "100%", "a\rb"]
TOP_DIRS = "mnt|karotz|bin|sbin|tmp|usr|etc|dev|proc|sys"


def shells(tmp: Path):
    """(name, argv prefix, PATH) for each way to run the library on this machine."""
    out = []
    busybox = arm_busybox()
    if busybox:
        bin_dir = tmp / "bb-bin"
        bin_dir.mkdir()
        for name in APPLETS:
            (bin_dir / name).write_text(f'#!/bin/sh\nexec {busybox} {name} "$@"\n', newline="\n")
            (bin_dir / name).chmod(0o755)
        out.append(("busybox", [str(busybox), "sh"], str(bin_dir)))
    for name in ("sh", "bash"):
        path = shutil.which(name)
        if path:
            out.append((name, [path], os.environ.get("PATH", "/usr/bin:/bin")))
    return out


def per_shell(test):
    """Run the test once for each shell, with a fresh configuration directory."""
    def run(self):
        for shell in self.shells:
            with self.subTest(shell=shell[0]):
                shutil.rmtree(self.conf, ignore_errors=True)
                test(self, shell)
    run.__name__ = test.__name__
    return run


class ConfShellTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.conf = self.tmp / "conf"
        self.shells = shells(self.tmp)
        self.assertTrue(self.shells, "no shell to run conf.sh")

    def run_in(self, shell, script, **env):
        _, argv, path = shell
        full = {"PATH": path, "LAPINOU_CONF_DIR": str(self.conf), "CONF_SH": str(CONF_SH), "HOME": str(self.tmp), **env}
        return subprocess.run([*argv, "-c", f'. "$CONF_SH"; {script}'], env=full, capture_output=True, timeout=300)

    @per_shell
    def test_roundtrip_special_chars(self, shell):
        value = 'a b"c$d;e`f é'
        r = self.run_in(shell, 'lapinou_set mqtt user "$V" && lapinou_get mqtt user', V=value)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.decode(), value + "\n")

    @per_shell
    def test_roundtrip_every_nasty_value(self, shell):
        marker = self.tmp / "pwned"
        for raw in NASTY:
            value = raw.replace("{m}", str(marker))
            r = self.run_in(shell, 'lapinou_set cloud llm_model "$V" && lapinou_get cloud llm_model', V=value)
            self.assertEqual(r.stdout.decode("utf-8"), value + "\n", f"{raw!r}: {r.stderr!r}")
        self.assertFalse(marker.exists())
        # One line each: the file stays a plain INI file.
        self.assertEqual((self.conf / "lapinou.conf").read_text(encoding="utf-8").count("\n"), 2)

    @per_shell
    def test_no_command_execution(self, shell):
        marker = self.tmp / "pwned"
        for value in (f"$(touch {marker})", f"`touch {marker}`", f"x; touch {marker}", f'"; touch {marker}; "'):
            self.run_in(shell, 'lapinou_set mqtt user "$V"; lapinou_get mqtt user; lapinou_secret_set mqtt_password "$V"; '
                               'lapinou_secret_get mqtt_password; v=$(lapinou_get mqtt user); echo "$v"', V=value)
            self.assertFalse(marker.exists(), f"{shell[0]}: {value!r} ran")

    @per_shell
    def test_hostile_names_are_refused(self, shell):
        for section, key in (("mqtt]\n[evil", "user"), ("mqtt", "user\nx"), ("mq tt", "user"), ("mqtt", "a=b"),
                             ("", "user"), ("mqtt", "$(id)")):
            r = self.run_in(shell, 'lapinou_set "$S" "$K" v', S=section, K=key)
            self.assertNotEqual(r.returncode, 0, (section, key))
        self.assertFalse((self.conf / "lapinou.conf").exists())

    @per_shell
    def test_defaults(self, shell):
        r = self.run_in(shell, 'lapinou_get mqtt port 1883; lapinou_get mqtt nothing; lapinou_set mqtt port ""; '
                               'lapinou_get mqtt port 1883')
        self.assertEqual(r.stdout.decode(), "1883\n\n1883\n")

    @per_shell
    def test_set_replaces_in_place_and_keeps_the_rest(self, shell):
        self.conf.mkdir()
        (self.conf / "lapinou.conf").write_text(
            "# mine\n[server]\nhost=a\n[mqtt]\nhost=h\nport=1\nport=2\n[audio]\nvolume=5\n", newline="\n")
        r = self.run_in(shell, "lapinou_set mqtt port 1884; lapinou_set mqtt user u; lapinou_set ota url http://x; "
                               "lapinou_set audio volume 7; lapinou_get mqtt port")
        self.assertEqual(r.stdout.decode(), "1884\n")
        self.assertEqual((self.conf / "lapinou.conf").read_text(),
                         "# mine\n[server]\nhost=a\n[mqtt]\nhost=h\nport=1884\nuser=u\n[audio]\nvolume=7\n"
                         "[ota]\nurl=http://x\n")

    @per_shell
    def test_secrets_mode_0600(self, shell):
        r = self.run_in(shell, 'umask 022; lapinou_secret_set mqtt_password "$V"; lapinou_secret_set ota_token t2; '
                               'lapinou_secret_get mqtt_password', V='p@ss "w"$rd')
        self.assertEqual(r.stdout.decode(), 'p@ss "w"$rd\n')
        self.assertEqual(stat.S_IMODE((self.conf / "lapinou.secrets").stat().st_mode), 0o600)
        self.run_in(shell, "umask 077; lapinou_set mqtt user u")
        self.assertEqual(stat.S_IMODE((self.conf / "lapinou.conf").stat().st_mode), 0o644)
        self.assertEqual(list(self.conf.glob("*.new")), [], "no temporary file left behind")

    @per_shell
    def test_secret_absent_from_conf(self, shell):
        self.run_in(shell, 'lapinou_set mqtt user u; lapinou_secret_set mqtt_password "$V"', V="hunter2-secret")
        self.assertNotIn("hunter2-secret", (self.conf / "lapinou.conf").read_text())
        self.assertNotIn("mqtt_password", (self.conf / "lapinou.conf").read_text())
        self.assertIn("mqtt_password=", (self.conf / "lapinou.secrets").read_text())
        r = self.run_in(shell, "lapinou_secret_get cloud_llm_key; lapinou_get mqtt mqtt_password")
        self.assertEqual(r.stdout.decode(), "\n\n")

    @per_shell
    def test_failed_write_keeps_the_old_file(self, shell):
        # The new file is written beside the old and moved over it.
        self.run_in(shell, "lapinou_set mqtt user first")
        before = (self.conf / "lapinou.conf").read_text()
        self.run_in(shell, "awk() { return 1; }; lapinou_set mqtt user second")
        self.assertEqual((self.conf / "lapinou.conf").read_text(), before)
        self.assertEqual(list(self.conf.glob("*.new")), [])


class ApplySettingsTest(unittest.TestCase):
    """image/usbkey/applysettings.sh with its paths in a sandbox, as the key's install runs it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def sandbox(self, settings):
        sb = Path(self._tmp.name) / "sb"
        for d in ("mnt/usbkey", "usr/etc/conf", "tmp"):
            (sb / d).mkdir(parents=True, exist_ok=True)
        for src in (APPLYSETTINGS, INSTALL_LIB, CONF_SH):
            text = src.read_text(encoding="utf-8")
            text = re.sub(rf"(?<![\w.$/])/({TOP_DIRS})(?=[/\s\"');$]|$)", lambda m: str(sb) + m.group(0), text, flags=re.M)
            (sb / "mnt/usbkey" / ("lapinou-conf.sh" if src == CONF_SH else src.name)).write_text(text, encoding="utf-8", newline="\n")
        (sb / "mnt/usbkey/lapinlibre.txt").write_bytes(settings.encode("utf-8"))
        return sb

    def run_script(self, sb, *args):
        return subprocess.run(["bash", str(sb / "mnt/usbkey/applysettings.sh"), *args], cwd=sb, capture_output=True,
                              text=True, env={**os.environ, "ERROR_SOUND": "none"}, timeout=120)

    def apply(self, settings, *args):
        sb = self.sandbox("WIFI_SSID=testssid\nWIFI_PASSWORD=testpassword\n" + settings)
        return sb, self.run_script(sb, *args)

    def test_applysettings_new_keys(self):
        sb, r = self.apply("MQTT_HOST=192.168.1.50\nMQTT_PORT=1884\nMQTT_USER=lapin ou\nMQTT_PASSWORD=pa$s\"w;d é\n"
                           "LAPINOU_SERVER=192.168.1.10\nTZ=Europe/Paris\nCLOUD_LLM_KIND=openai\n"
                           "CLOUD_LLM_URL=https://api.example.com/v1\nCLOUD_LLM_MODEL=gpt-x\nCLOUD_LLM_KEY=sk-abc\n"
                           "CLOUD_STT_URL=https://stt.example.com\nCLOUD_STT_KEY=k1\nCLOUD_TTS_URL=https://tts.example.com\n"
                           "CLOUD_TTS_KEY=k2\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        conf = sb / "usr/etc/conf"
        text = (conf / "lapinou.conf").read_text(encoding="utf-8")
        for line in ("[mqtt]", "host=192.168.1.50", "port=1884", "user=lapin ou", "[server]", "host=192.168.1.10",
                     "[time]", "tz=Europe/Paris", "llm_kind=openai", "llm_url=https://api.example.com/v1",
                     "llm_model=gpt-x", "stt_url=https://stt.example.com", "tts_url=https://tts.example.com"):
            self.assertIn(line + "\n", text)
        # The library reads back what the key wrote, and the secrets are only in the 0600 file.
        for key, want in (("mqtt_password", 'pa$s"w;d é'), ("cloud_llm_key", "sk-abc"), ("cloud_stt_key", "k1"),
                          ("cloud_tts_key", "k2")):
            got = subprocess.run(["bash", "-c", f'. "{CONF_SH}"; lapinou_secret_get {key}'], capture_output=True,
                                 env={**os.environ, "LAPINOU_CONF_DIR": str(conf)}).stdout.decode()
            self.assertEqual(got, want + "\n")
        for secret in ("sk-abc", "k1", "k2", "pa$s"):
            self.assertNotIn(secret, text)
        self.assertEqual(stat.S_IMODE((conf / "lapinou.secrets").stat().st_mode), 0o600)
        log = (sb / "mnt/usbkey/install-log.txt").read_text(encoding="utf-8")
        for secret in ("sk-abc", "pa$s", "k1", "k2"):
            self.assertNotIn(secret, log, "keys and passwords must not be logged")

    def test_mqtt_host_alone_lands_in_mqtt_section(self):
        sb, r = self.apply("MQTT_HOST=192.168.1.50\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((sb / "usr/etc/conf/lapinou.conf").read_text(), "[mqtt]\nhost=192.168.1.50\nenabled=1\n")
        self.assertFalse((sb / "usr/etc/conf/lapinou.secrets").exists())

    def test_empty_keeps_the_rabbits(self):
        sb = self.sandbox("WIFI_SSID=testssid\nWIFI_PASSWORD=testpassword\nMQTT_HOST=\nMQTT_PASSWORD=\nTZ=\n")
        (sb / "usr/etc/conf/lapinou.conf").write_text("[mqtt]\nhost=old\n", newline="\n")
        r = self.run_script(sb)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((sb / "usr/etc/conf/lapinou.conf").read_text(), "[mqtt]\nhost=old\n")
        self.assertFalse((sb / "usr/etc/conf/lapinou.secrets").exists())

    def test_unknown_key_is_still_an_error(self):
        sb, r = self.apply("MQTT_HOSTT=192.168.1.50\n", "--check")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown setting MQTT_HOSTT", (sb / "mnt/usbkey/install-log.txt").read_text())

    def test_bad_values_are_errors(self):
        for line in ("MQTT_PORT=0", "MQTT_PORT=70000", "MQTT_PORT=abc", "MQTT_HOST=a b", "CLOUD_LLM_KIND=gemini",
                     "CLOUD_LLM_URL=ftp://x", "TZ=Europe/Paris;reboot", "CLOUD_LLM_MODEL=a b"):
            with self.subTest(line=line):
                self.setUp()
                _, r = self.apply(line + "\n", "--check")
                self.assertNotEqual(r.returncode, 0)

    def test_missing_library_fails_the_check_and_writes_nothing(self):
        for args in (("--check",), ()):
            with self.subTest(args=args):
                self.setUp()
                sb = self.sandbox("WIFI_SSID=testssid
WIFI_PASSWORD=testpassword
MQTT_HOST=192.168.1.50
")
                (sb / "mnt/usbkey/lapinou-conf.sh").unlink()
                r = self.run_script(sb, *args)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("lapinou-conf.sh is missing", (sb / "mnt/usbkey/install-log.txt").read_text())
                self.assertEqual(list((sb / "usr/etc/conf").iterdir()), [], "nothing is written")

    def test_check_writes_nothing(self):
        sb, r = self.apply("MQTT_HOST=192.168.1.50\n", "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((sb / "usr/etc/conf/lapinou.conf").exists())


@unittest.skipUnless(shutil.which("gcc") and shutil.which("pkg-config") and
                     subprocess.run(["pkg-config", "--exists", "glib-2.0"]).returncode == 0, "no gcc or GLib here")
class ConfCTest(unittest.TestCase):
    """lapinou_conf.c reads what conf.sh writes."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        (tmp / "main.c").write_text(
            '#include "lapinou_conf.h"\n#include <stdio.h>\n'
            "int main(int argc, char **argv) {\n"
            "  char *v = argv[1][0] == 'S' ? lapinou_secret_get(argv[2])\n"
            "                              : lapinou_conf_get(argv[2], argv[3], argc > 4 ? argv[4] : NULL);\n"
            '  if (!v) { puts("<null>"); return 0; }\n'
            "  fputs(v, stdout); putchar(10); g_free(v); return 0; }\n", newline="\n")
        flags = subprocess.run(["pkg-config", "--cflags", "--libs", "glib-2.0"], capture_output=True, text=True).stdout.split()
        r = subprocess.run(["gcc", "-Wall", "-Werror", "-I", str(C_DIR), "-o", str(tmp / "t"), str(tmp / "main.c"),
                            str(C_DIR / "lapinou_conf.c"), *flags], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.exe = tmp / "t"
        cls.tmp = tmp

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def c_get(self, conf, *args):
        r = subprocess.run([str(self.exe), *args], env={"LAPINOU_CONF_DIR": str(conf)}, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.decode("utf-8")

    def test_reads_what_the_shell_wrote(self):
        conf = self.tmp / "conf"
        shutil.rmtree(conf, ignore_errors=True)
        marker = self.tmp / "pwned"
        for raw in NASTY:
            value = raw.replace("{m}", str(marker))
            subprocess.run(["bash", "-c", '. "$CONF_SH"; lapinou_set mqtt user "$V"; lapinou_secret_set ota_token "$V"'],
                           env={**os.environ, "CONF_SH": str(CONF_SH), "LAPINOU_CONF_DIR": str(conf), "V": value}, check=True)
            self.assertEqual(self.c_get(conf, "C", "mqtt", "user"), value + "\n", repr(raw))
            self.assertEqual(self.c_get(conf, "S", "ota_token"), value + "\n", repr(raw))
        self.assertFalse(marker.exists())

    def test_defaults_and_absent(self):
        conf = self.tmp / "conf2"
        self.assertEqual(self.c_get(conf, "C", "mqtt", "port", "1883"), "1883\n")  # no file at all
        self.assertEqual(self.c_get(conf, "C", "mqtt", "port"), "<null>\n")
        self.assertEqual(self.c_get(conf, "S", "ota_token"), "<null>\n")
        conf.mkdir()
        (conf / "lapinou.conf").write_text("[mqtt]\nhost=h\nport=\n[audio]\nport=9\n", newline="\n")
        self.assertEqual(self.c_get(conf, "C", "mqtt", "host", "d"), "h\n")
        self.assertEqual(self.c_get(conf, "C", "mqtt", "port", "1883"), "1883\n")  # empty counts as absent
        self.assertEqual(self.c_get(conf, "C", "mqtt", "user", "d"), "d\n")
        self.assertEqual(self.c_get(conf, "C", "bad name", "x", "d"), "d\n")


if __name__ == "__main__":
    unittest.main()
