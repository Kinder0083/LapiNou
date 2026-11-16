# SPDX-License-Identifier: MIT
"""Test de fumée : `python -m lapinou --config …` démarre les cinq ports, répond à la santé et
s'arrête proprement sur SIGTERM (comme `systemctl stop`).

Aucun modèle n'est chargé ni téléchargé : HOME est un dossier vide (pas de modèles de
transcription ni de voix), Home Assistant, MQTT et Lyrion ne sont pas réglés, le relais OTA ne
vérifie GitHub qu'à la demande.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM : Linux (VM, LXC)")


def _free_ports(n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture
def server(tmp_path):
    platform, mic, http, tts, asr = ports = _free_ports(5)
    config = tmp_path / "etc" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        f"[server]\nplatform_port = {platform}\nmic_port = {mic}\nhttp_port = {http}\n"
        f"tts_port = {tts}\nasr_port = {asr}\npublic_url = \"http://127.0.0.1:{http}\"\n"
        f"[ota]\ndir = \"{tmp_path / 'ota'}\"\ncheck_hours = 0\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LAPINOU_")}
    env.update(HOME=str(home), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "lapinou", "--config", str(config),
         "--db", str(tmp_path / "data" / "lapinou.db")],
        cwd=SERVER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        yield proc, ports, tmp_path
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_starts_five_ports_health_and_stops_on_sigterm(server):
    proc, ports, tmp_path = server
    deadline = time.monotonic() + 60
    while not all(_listening(p) for p in ports):
        assert proc.poll() is None, proc.stdout.read()
        assert time.monotonic() < deadline, "les cinq ports n'écoutent pas après 60 s"
        time.sleep(0.2)
    http = ports[2]
    with urllib.request.urlopen(f"http://127.0.0.1:{http}/api/health", timeout=5) as r:
        assert r.status == 200
        assert json.loads(r.read())["status"] == "ok"
    with urllib.request.urlopen(f"http://127.0.0.1:{http}/asr/grammar/launch.abnf",
                                timeout=5) as r:
        assert r.read().startswith(b"#ABNF 1.0")  # endpoints LapinLibre des applications
    assert (tmp_path / "data" / "lapinou.db").is_file()

    proc.send_signal(signal.SIGTERM)
    out, _ = proc.communicate(timeout=30)
    # uvicorn relance le signal après un arrêt complet : systemd tient SIGTERM pour propre.
    assert proc.returncode in (0, -signal.SIGTERM), out
    assert "serveur LapiNou arrêté" in out, out
    assert "Traceback" not in out, out
    assert not any(_listening(p) for p in ports)
