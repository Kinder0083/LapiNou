# SPDX-License-Identifier: MIT
"""Clé USB construite par le serveur, relue par le vrai `applysettings.sh` de la clé.

`data/usbkey/` : copies à l'identique de `image/usbkey/{applysettings.sh,install-lib.sh,
lapinlibre.txt}` et `image/core/usr/share/lapinou/conf.sh` (→ `lapinou-conf.sh`) de la branche
lapinou/fw (b3ad9a6). Le script tourne dans un bac à sable dont il ne sort pas : ses chemins
absolus y sont réécrits, comme dans `tests/image/test_lapinou_conf.py` du firmware.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from lapinou.config import MqttConfig, ServerConfig, Settings
from lapinou.usbkey import UsbKeyError, WifiInput, build_usb_key, check_values, format_line

DATA = Path(__file__).parent / "data" / "usbkey"
REPO = Path(__file__).resolve().parents[2]
TOP_DIRS = "mnt|karotz|bin|sbin|tmp|usr|etc|dev|proc|sys"
SCRIPTS = {"applysettings.sh": "image/usbkey/applysettings.sh",
           "install-lib.sh": "image/usbkey/install-lib.sh",
           "lapinou-conf.sh": "image/core/usr/share/lapinou/conf.sh"}

pytestmark = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None,
                                reason="bash et awk nécessaires")

TRICKY = 'p@ss "x" $y'


def _sandbox(tmp_path: Path, lapinlibre: bytes) -> Path:
    sb = tmp_path / "sb"
    for d in ("mnt/usbkey", "usr/etc/conf", "tmp", "dev"):
        (sb / d).mkdir(parents=True, exist_ok=True)
    (sb / "dev/null").touch()
    for name in SCRIPTS:
        text = (DATA / name).read_text(encoding="utf-8")
        text = re.sub(rf"(?<![\w.$/])/({TOP_DIRS})(?=[/\s\"');$]|$)",
                      lambda m: str(sb) + m.group(0), text, flags=re.M)
        (sb / "mnt/usbkey" / name).write_text(text, encoding="utf-8", newline="\n")
    (sb / "mnt/usbkey/lapinlibre.txt").write_bytes(lapinlibre)
    return sb


def _run(sb: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(sb / "mnt/usbkey/applysettings.sh"), *args], cwd=sb,
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, "ERROR_SOUND": "none"})


def _parsed(tmp_path: Path, lapinlibre: bytes, keys: list[str]) -> dict[str, str]:
    """Les valeurs telles que la boucle de lecture de applysettings.sh les comprend : le
    script jusqu'à la fin de cette boucle (« is_address() » la suit), puis un affichage."""
    sb = _sandbox(tmp_path, lapinlibre)
    script = (sb / "mnt/usbkey/applysettings.sh").read_text(encoding="utf-8")
    head = script[: script.index("\nis_address()")]
    dump = "".join(f'printf "%s\\0%s\\0" {k} "${{{k}}}"\n' for k in keys)
    probe = sb / "mnt/usbkey/probe.sh"
    probe.write_text(head + "\n" + dump, encoding="utf-8", newline="\n")
    r = subprocess.run(["bash", str(probe)], cwd=sb, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    parts = r.stdout.decode("utf-8").split("\0")[:-1]
    return dict(zip(parts[::2], parts[1::2]))


def _release_zip(tmp_path: Path) -> Path:
    z = tmp_path / "LapinLibre-v1.2.3.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(DATA / "lapinlibre.txt", "lapinlibre.txt")
        zf.writestr("autorun", "#!/bin/bash\necho autorun\n")
        zf.writestr("sound/ok.mp3", b"\xff\xfbMP3")
        zf.writestr("rootfs.img.gz", b"\x1f\x8b" + bytes(range(256)) * 40)
    return z


def _settings(**mqtt) -> Settings:
    return Settings(server=ServerConfig(public_url="http://192.168.1.10:8080",
                                        timezone="Europe/Paris"),
                    mqtt=MqttConfig(**mqtt))


def _lapinlibre(out: Path) -> bytes:
    with zipfile.ZipFile(out) as zf:
        return zf.read("lapinlibre.txt")


def test_data_is_the_firmware_copy():
    """Quand le dépôt a les scripts LapiNou de la clé, la copie de test doit les suivre."""
    if "LAPINOU_SERVER" not in (REPO / SCRIPTS["applysettings.sh"]).read_text(encoding="utf-8"):
        pytest.skip("branche lapinou/fw pas encore fusionnée")
    for name, rel in {**SCRIPTS, "lapinlibre.txt": "image/usbkey/lapinlibre.txt"}.items():
        assert (DATA / name).read_bytes() == (REPO / rel).read_bytes(), f"{name} : copie à suivre"


@pytest.mark.parametrize("value", [
    TRICKY, "  espaces autour  ", '"guillemets"', "'apostrophes'", "a'b\"c", "#dièse",
    "x=y=z", "back\\slash", "$(id);`id`", "é à ü ß 漢", "", "\t tab",
])
def test_format_line_reads_back_identical(tmp_path, value):
    text = format_line("MQTT_PASSWORD", value) + "\n"
    assert _parsed(tmp_path, text.encode("utf-8"), ["MQTT_PASSWORD"]) == {"MQTT_PASSWORD": value}


@pytest.mark.parametrize("value", ["a\nb", "a\rb", "a\0b", "a\x1bb"])
def test_format_line_refuses_control_characters(value):
    with pytest.raises(UsbKeyError):
        format_line("MQTT_PASSWORD", value)


def test_build_usb_key_round_trip(tmp_path):
    """Le mot de passe MQTT du plan (`p@ss "x" $y`) et un mot de passe Wi-Fi piégé arrivent
    sur le lapin tels quels, après le vrai applysettings.sh (contrôle puis application)."""
    wifi_pw = "p@ss 'x' $y; é"
    out = build_usb_key(
        _release_zip(tmp_path),
        _settings(host="192.168.1.50", port=1884, username="lapin ou", password=TRICKY),
        WifiInput(ssid="Maison $HOME", password=wifi_pw, ip="192.168.1.103",
                  gateway="192.168.1.1", dns="192.168.1.1",
                  ssh_pubkey="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc moi@pc",
                  web_password="Lapin-2026!", hostname="salon"),
        tmp_path / "out" / "cle.zip",
    )
    assert out == tmp_path / "out" / "cle.zip"
    text = _lapinlibre(out)
    keys = ["WIFI_SSID", "WIFI_PASSWORD", "IP", "NETMASK", "GATEWAY", "DNS", "SSH_PUBKEY",
            "WEB_PASSWORD", "HOSTNAME", "LAPINOU_SERVER", "MQTT_HOST", "MQTT_PORT",
            "MQTT_USER", "MQTT_PASSWORD", "TZ", "SERVER", "TELNET"]
    assert _parsed(tmp_path / "p", text, keys) == {
        "WIFI_SSID": "Maison $HOME", "WIFI_PASSWORD": wifi_pw, "IP": "192.168.1.103",
        "NETMASK": "255.255.255.0", "GATEWAY": "192.168.1.1", "DNS": "192.168.1.1",
        "SSH_PUBKEY": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc moi@pc",
        "WEB_PASSWORD": "Lapin-2026!", "HOSTNAME": "salon", "LAPINOU_SERVER": "192.168.1.10",
        "MQTT_HOST": "192.168.1.50", "MQTT_PORT": "1884", "MQTT_USER": "lapin ou",
        "MQTT_PASSWORD": TRICKY, "TZ": "Europe/Paris", "SERVER": "", "TELNET": "",
    }
    # Le gabarit de la version est gardé (ses explications), sans BOM ni CRLF.
    assert b"# --- Wi-Fi" in text and b"\r" not in text and not text.startswith(b"\xef\xbb\xbf")

    sb = _sandbox(tmp_path / "a", text)
    r = _run(sb)
    assert r.returncode == 0, r.stdout + r.stderr
    conf = sb / "usr/etc/conf"
    wpa = (conf / "wpa.conf").read_text(encoding="utf-8")
    assert 'ssid="Maison $HOME"\n' in wpa and f'psk="{wifi_pw}"\n' in wpa
    assert (conf / "web_password").read_text() == "Lapin-2026!\n"
    got = subprocess.run(["bash", "-c", f'. "{DATA / "lapinou-conf.sh"}"; '
                          "lapinou_secret_get mqtt_password"], capture_output=True,
                         env={**os.environ, "LAPINOU_CONF_DIR": str(conf)}).stdout.decode()
    assert got == TRICKY + "\n"
    lapinou_conf = (conf / "lapinou.conf").read_text(encoding="utf-8")
    assert "user=lapin ou\n" in lapinou_conf and "host=192.168.1.10\n" in lapinou_conf
    assert "tz=Europe/Paris\n" in lapinou_conf
    assert "url_app=http://192.168.1.10:8080" in (conf / "karotz.conf").read_text()
    # Les autres fichiers de la clé sont recopiés à l'identique.
    with zipfile.ZipFile(out) as zf, zipfile.ZipFile(tmp_path / "LapinLibre-v1.2.3.zip") as src:
        assert sorted(zf.namelist()) == sorted(src.namelist())
        for name in src.namelist():
            if name != "lapinlibre.txt":
                assert zf.read(name) == src.read(name)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600  # mots de passe dedans


def test_build_usb_key_dhcp_and_no_mqtt(tmp_path):
    out = build_usb_key(_release_zip(tmp_path), _settings(),
                        WifiInput(ssid="Maison", password="motdepasse"), tmp_path / "k.zip")
    values = _parsed(tmp_path, _lapinlibre(out),
                     ["IP", "MQTT_HOST", "MQTT_PASSWORD", "WIFI_SSID", "LAPINOU_SERVER"])
    assert values == {"IP": "", "MQTT_HOST": "", "MQTT_PASSWORD": "", "WIFI_SSID": "Maison",
                      "LAPINOU_SERVER": "192.168.1.10"}
    assert _run(_sandbox(tmp_path / "c", _lapinlibre(out)), "--check").returncode == 0


def test_wifi_password_with_double_quote_is_refused_like_the_key(tmp_path):
    """applysettings.sh refuse « " » et « \\ » dans le Wi-Fi (wpa.conf) : la clé
    ne s'installerait pas, le serveur refuse donc de la construire, avant d'écrire."""
    out = tmp_path / "k.zip"
    with pytest.raises(UsbKeyError, match="Wi-Fi"):
        build_usb_key(_release_zip(tmp_path), _settings(),
                      WifiInput(ssid="Maison", password=TRICKY), out)
    assert not out.exists()
    sb = _sandbox(tmp_path, (format_line("WIFI_SSID", "Maison") + "\n"
                             + format_line("WIFI_PASSWORD", TRICKY) + "\n").encode())
    r = _run(sb, "--check")
    assert r.returncode != 0 and "can't contain" in r.stdout


# Chaque cas : les valeurs, et si la clé est acceptée. Le contrôle du serveur et celui de la
# clé (applysettings.sh --check) doivent dire la même chose.
CASES = [
    ({}, True),
    ({"WIFI_PASSWORD": "court"}, False),
    ({"WIFI_PASSWORD": "x" * 64}, False),
    ({"WIFI_SSID": 'a\\b'}, False),
    ({"WIFI_SSID": ""}, False),
    ({"IP": "192.168.1.5", "GATEWAY": "192.168.1.1", "DNS": "1.1.1.1"}, True),
    ({"IP": "192.168.1.5", "GATEWAY": "", "DNS": "1.1.1.1"}, False),
    ({"IP": "192.168.1"}, False),
    ({"SSH_PUBKEY": "ssh-rsa AAAAB3Nza== moi"}, True),
    ({"SSH_PUBKEY": "pas une clé"}, False),
    ({"WEB_PASSWORD": "ok-ok-ok"}, True),
    ({"WEB_PASSWORD": "avec espace"}, False),
    ({"WEB_PASSWORD": "deux:points"}, False),
    ({"WEB_PASSWORD": "accentué1"}, False),
    ({"WEB_PASSWORD": "court"}, False),
    ({"HOSTNAME": "Salon"}, False),
    ({"HOSTNAME": "lapin-salon"}, True),
    ({"LAPINOU_SERVER": "mon-serveur.local"}, True),
    ({"LAPINOU_SERVER": "http://192.168.1.10"}, False),
    ({"MQTT_HOST": "192.168.1.50", "MQTT_PORT": "70000"}, False),
    ({"MQTT_HOST": "192.168.1.50", "MQTT_PORT": "1883", "MQTT_USER": "a b",
      "MQTT_PASSWORD": TRICKY}, True),
    ({"TZ": "Europe/Paris"}, True),
    ({"TZ": "Europe Paris"}, False),
]


@pytest.mark.parametrize(("values", "ok"), CASES)
def test_server_check_agrees_with_key_check(tmp_path, values, ok):
    full = {"WIFI_SSID": "Maison", "WIFI_PASSWORD": "motdepasse", "NETMASK": "255.255.255.0"}
    full.update(values)
    assert (check_values(full) == []) is ok, check_values(full)
    text = "".join(format_line(k, v) + "\n" for k, v in full.items())
    r = _run(_sandbox(tmp_path, text.encode("utf-8")), "--check")
    assert (r.returncode == 0) is ok, r.stdout


def test_release_without_lapinlibre_txt(tmp_path):
    z = tmp_path / "r.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("autorun", "x")
    with pytest.raises(UsbKeyError, match="lapinlibre.txt"):
        build_usb_key(z, _settings(), WifiInput(ssid="M", password="motdepasse"),
                      tmp_path / "k.zip")


def test_release_with_unsafe_names_is_refused(tmp_path):
    z = tmp_path / "r.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(DATA / "lapinlibre.txt", "lapinlibre.txt")
        zf.writestr("../evil", "x")
    with pytest.raises(UsbKeyError):
        build_usb_key(z, _settings(), WifiInput(ssid="M", password="motdepasse"),
                      tmp_path / "k.zip")
    assert not (tmp_path / "k.zip").exists()
