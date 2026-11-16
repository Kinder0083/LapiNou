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
import threading
import time
import urllib.request
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM : Linux (VM, LXC)")
ATTEMPTS = 3  # les ports libres choisis peuvent être pris entre-temps : on recommence


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


class Server:
    """`python -m lapinou` en sous-processus ; sa sortie est lue en continu (jamais de tube
    plein qui bloquerait le serveur)."""

    def __init__(self, tmp_path: Path, ports: list[int]):
        platform, mic, http, tts, asr = self.ports = ports
        config = tmp_path / "etc" / "config.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text(
            f"[server]\nplatform_port = {platform}\nmic_port = {mic}\nhttp_port = {http}\n"
            f"tts_port = {tts}\nasr_port = {asr}\npublic_url = \"http://127.0.0.1:{http}\"\n"
            f"[ota]\ndir = \"{tmp_path / 'ota'}\"\ncheck_hours = 0\n",
            encoding="utf-8",
        )
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("LAPINOU_")}
        env.update(HOME=str(home), PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lapinou", "--config", str(config),
             "--db", str(tmp_path / "data" / "lapinou.db")],
            cwd=SERVER, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)

    @property
    def output(self) -> str:
        return "".join(self.lines)

    def wait_output(self, timeout: float) -> str:
        self._reader.join(timeout)
        return self.output

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._reader.join(5)


def _start(tmp_path: Path) -> Server:
    """Le serveur, ses cinq ports à l'écoute (60 s au plus)."""
    for attempt in range(ATTEMPTS):
        server = Server(tmp_path, _free_ports(5))
        deadline = time.monotonic() + 60
        while not all(_listening(p) for p in server.ports):
            if server.proc.poll() is not None:
                out = server.wait_output(5)
                if "Address already in use" in out and attempt < ATTEMPTS - 1:
                    break  # port pris entre le choix et le démarrage : autres ports
                pytest.fail(f"le serveur s'est arrêté au démarrage :\n{out}")
            if time.monotonic() > deadline:
                server.kill()
                pytest.fail(f"les cinq ports n'écoutent pas après 60 s :\n{server.output}")
            time.sleep(0.2)
        else:
            return server
    pytest.fail("ports toujours pris")


@pytest.fixture
def server(tmp_path):
    srv = _start(tmp_path)
    try:
        yield srv, tmp_path
    finally:
        srv.kill()


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as r:
        assert r.status == 200
        return r.read()


def test_starts_five_ports_health_and_stops_on_sigterm(server):
    srv, tmp_path = server
    http = srv.ports[2]
    assert json.loads(_get(f"http://127.0.0.1:{http}/api/health"))["status"] == "ok"
    assert json.loads(_get(f"http://127.0.0.1:{http}/health")) == {"status": "ok"}
    # endpoints LapinLibre des applications
    assert _get(f"http://127.0.0.1:{http}/asr/grammar/launch.abnf").startswith(b"#ABNF 1.0")
    assert (tmp_path / "data" / "lapinou.db").is_file()

    srv.proc.send_signal(signal.SIGTERM)
    srv.proc.wait(30)
    out = srv.wait_output(10)
    # uvicorn relance le signal après un arrêt complet : systemd tient SIGTERM pour propre.
    assert srv.proc.returncode in (0, -signal.SIGTERM), out
    assert "serveur LapiNou arrêté" in out, out
    assert "Traceback" not in out, out
    assert not any(_listening(p) for p in srv.ports)
