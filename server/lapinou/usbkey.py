# SPDX-License-Identifier: MIT
"""Clé USB d'installation d'un lapin : l'archive de la version, avec `lapinlibre.txt` rempli.

Le format est celui que lit `image/usbkey/applysettings.sh` (sur la clé) : une ligne
`CLÉ=valeur`, espaces de début et de fin retirés, puis **une** paire de guillemets (doubles ou
simples) qui entoure toute la valeur est retirée ; aucune séquence d'échappement. Chaque valeur
est donc écrite entre guillemets doubles (`CLÉ="valeur"`) : ses espaces, guillemets, `$`, `;`,
`#`, accents arrivent tels quels. Une fin de ligne ou un caractère de contrôle ne peut pas y
figurer : refusés.

Les contrôles de `applysettings.sh --check` sont refaits ici (`check_values`), pour refuser une
clé qui ne s'installerait pas avant que la famille ne la copie (le test les compare au vrai
script).
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .config import Settings

SETTINGS_FILE = "lapinlibre.txt"


class UsbKeyError(ValueError):
    """La clé ne peut pas être construite (message pour la famille, en français)."""


@dataclass(frozen=True)
class WifiInput:
    """Ce que la page demande pour un lapin : son réseau, et ses accès (SSH, page web)."""

    ssid: str
    password: str
    ip: str = ""  # vide : DHCP
    netmask: str = "255.255.255.0"
    gateway: str = ""
    dns: str = ""
    ssh_pubkey: str = ""
    web_password: str = ""
    hostname: str = ""


def _has_control(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def format_line(key: str, value: str) -> str:
    """`KEY="value"`, relu à l'identique par applysettings.sh."""
    if _has_control(value):
        raise UsbKeyError(f"{key} : pas de retour à la ligne ni de caractère de contrôle")
    return f'{key}="{value}"'


_ADDRESS = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")
_SSH_KEY = re.compile(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp[0-9]+) [A-Za-z0-9+/=]+( .*)?")
_HOSTNAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_TZ = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+:./,-]*")


def _web_password_ok(pw: str) -> bool:
    return (8 <= len(pw) <= 64 and all("!" <= c <= "~" for c in pw)
            and not any(c in pw for c in '"\\:'))


def check_values(v: dict[str, str]) -> list[str]:
    """Les problèmes que `applysettings.sh --check` trouverait (vide : la clé s'installe)."""
    g = v.get
    problems = [f"{k} : pas de retour à la ligne ni de caractère de contrôle"
                for k, val in v.items() if _has_control(val)]
    ssid, pw = g("WIFI_SSID", ""), g("WIFI_PASSWORD", "")
    if not (ssid and pw):
        problems.append("il faut le nom du réseau Wi-Fi et son mot de passe")
    if '"' in ssid + pw or "\\" in ssid + pw:
        problems.append("le nom et le mot de passe du Wi-Fi ne peuvent pas contenir \" ni \\")
    if pw and not (8 <= len(pw) and len(pw.encode()) <= 63):
        problems.append("le mot de passe Wi-Fi doit faire de 8 à 63 caractères (WPA)")
    if g("IP"):
        for k, name in (("IP", "l'adresse"), ("NETMASK", "le masque"),
                        ("GATEWAY", "la passerelle"), ("DNS", "le DNS")):
            if not _ADDRESS.fullmatch(g(k, "")):
                problems.append(f"adresse fixe : {name} doit ressembler à 192.168.1.10")
    if g("SSH_PUBKEY") and not _SSH_KEY.fullmatch(g("SSH_PUBKEY", "")):
        problems.append("la clé SSH doit être une ligne de clé publique (ssh-ed25519 AAAA…)")
    if g("WEB_PASSWORD") and not _web_password_ok(g("WEB_PASSWORD", "")):
        problems.append("le mot de passe de la page du lapin : 8 à 64 caractères, sans espace, "
                        "accent, \", \\ ni :")
    if g("HOSTNAME") and not _HOSTNAME.fullmatch(g("HOSTNAME", "")):
        problems.append("le nom du lapin : minuscules, chiffres et tirets (ex. lapin-salon)")
    for k, what in (("LAPINOU_SERVER", "l'adresse du serveur LapiNou"),
                    ("MQTT_HOST", "l'adresse du broker MQTT")):
        if g(k) and not _HOST.fullmatch(g(k, "")):
            problems.append(f"{what} doit être un nom ou une adresse, sans http:// ni port")
    port = g("MQTT_PORT", "")
    if port and not (re.fullmatch(r"[0-9]{1,5}", port) and 1 <= int(port) <= 65535):
        problems.append("le port MQTT doit être un nombre de 1 à 65535")
    if g("TZ") and not _TZ.fullmatch(g("TZ", "")):
        problems.append("le fuseau horaire doit ressembler à Europe/Paris")
    return problems


def server_host(settings: Settings) -> str:
    """Le nom ou l'adresse par lequel les lapins joignent ce serveur."""
    from .app import base_url

    return urlsplit(base_url(settings)).hostname or ""


def key_values(settings: Settings, wifi: WifiInput) -> dict[str, str]:
    """Les réglages de `lapinlibre.txt` (une valeur vide garde ce que le lapin a)."""
    values = {
        "WIFI_SSID": wifi.ssid, "WIFI_PASSWORD": wifi.password, "IP": wifi.ip,
        "NETMASK": wifi.netmask if wifi.ip else "", "GATEWAY": wifi.gateway, "DNS": wifi.dns,
        "SSH_PUBKEY": wifi.ssh_pubkey.strip(), "WEB_PASSWORD": wifi.web_password,
        "HOSTNAME": wifi.hostname, "LAPINOU_SERVER": server_host(settings),
        "TZ": settings.server.timezone,
    }
    mqtt = settings.mqtt
    if mqtt.host:
        values.update(MQTT_HOST=mqtt.host, MQTT_PORT=str(mqtt.port), MQTT_USER=mqtt.username,
                      MQTT_PASSWORD=mqtt.password)
    return values


_LINE = re.compile(r"#?\s*([A-Z][A-Z0-9_]*)=.*")


def fill_template(template: str, values: dict[str, str]) -> str:
    """Le `lapinlibre.txt` de la version, avec les valeurs non vides à la place de leur ligne
    (commentée ou non) ; celles que le gabarit n'a pas sont ajoutées à la fin."""
    todo = {k: v for k, v in values.items() if v}
    lines = []
    for line in template.lstrip("﻿").splitlines():
        m = _LINE.fullmatch(line.strip())
        if m and m.group(1) in todo:
            key = m.group(1)
            lines.append(format_line(key, todo.pop(key)))
        else:
            lines.append(line)
    if todo:
        lines += ["", "# Ajouté par le serveur LapiNou."]
        lines += [format_line(k, v) for k, v in todo.items()]
    return "\n".join(lines) + "\n"


def _safe_name(name: str) -> bool:
    p = PurePosixPath(name)
    return (bool(name) and "\\" not in name and not p.is_absolute() and ".." not in p.parts
            and ":" not in p.parts[0])


def build_usb_key(release_zip: Path, settings: Settings, wifi: WifiInput, out: Path) -> Path:
    """L'archive de la clé (`release_zip`, celle de la version), `lapinlibre.txt` rempli, écrite
    dans `out` (mode 0600 : elle contient des mots de passe). UsbKeyError si la clé ne
    s'installerait pas ou si l'archive est invalide ; rien n'est alors écrit."""
    values = key_values(settings, wifi)
    problems = check_values(values)
    if problems:
        raise UsbKeyError("Clé non construite : " + " ; ".join(problems) + ".")
    try:
        src = zipfile.ZipFile(release_zip)
    except (OSError, zipfile.BadZipFile) as e:
        raise UsbKeyError(f"archive de la clé illisible : {e}") from e
    with src:
        infos = src.infolist()
        bad = [i.filename for i in infos if not _safe_name(i.filename)]
        if bad:
            raise UsbKeyError(f"archive de la clé invalide (chemin {bad[0]!r})")
        if SETTINGS_FILE not in src.namelist():
            raise UsbKeyError(f"archive de la clé sans {SETTINGS_FILE}")
        template = src.read(SETTINGS_FILE).decode("utf-8").replace("\r\n", "\n")
        text = fill_template(template, values)
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.name + ".")
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w") as dst:
                for info in map(copy.copy, infos):  # les originaux servent à la lecture
                    if info.filename == SETTINGS_FILE:
                        info.compress_type = zipfile.ZIP_DEFLATED
                        dst.writestr(info, text.encode("utf-8"))
                        continue
                    with src.open(info) as r, dst.open(info, "w") as w:
                        while chunk := r.read(1 << 20):
                            w.write(chunk)
            os.replace(tmp, out)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    return out
