# SPDX-License-Identifier: MIT
"""Endpoints LapinLibre des applications sur 8080 (url_app des lapins) : le contrat HTTP de
tools/app-server/app_server.py (HttpHandler), hors du contrôle de Host de la page."""

import io
import logging
import shutil
import socket
import struct
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lapinou.app import create_app
from lapinou.config import load_settings
from lapinou.web import apps as apps_mod
from lapinou.web import asr_compat
from lapinou.voice.vad import asr_server
from lapinou.web.apps import AppHost

EXAMPLES = Path(__file__).resolve().parents[2] / "tools" / "app-server" / "example-apps"
RABBIT = {"Host": "lapin"}  # nom inconnu de la page : ces routes répondent quand même


@pytest.fixture
def host(tmp_path):
    folder = tmp_path / "apps"
    shutil.copytree(EXAMPLES, folder)
    (folder / "hello" / "config.bin").write_bytes(b"jamais servi")
    return AppHost(folder, tmp_path / "uploads")


@pytest.fixture
def client(tmp_path, host):
    settings = load_settings(tmp_path / "config.toml")
    return TestClient(create_app(settings, apps=host), base_url="http://localhost")


def test_app_zip_is_the_folder_without_config_bin(client):
    r = client.get("/file/hello/1/get", headers=RABBIT)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["descriptor.xml", "main.js"]


@pytest.mark.parametrize("path", ["/file/nope/1/get", "/file/hello%2F..%2Fhello/1/get", "/file/hello/1/put"])
def test_unknown_app_is_404(client, path):
    assert client.get(path, headers=RABBIT).status_code == 404


def test_launch_grammar_lists_hosted_apps(client):
    r = client.get("/asr/grammar/launch-42.abnf", headers=RABBIT)
    assert r.status_code == 200
    assert r.headers["content-type"].lower() == "text/plain; charset=utf-8"
    assert r.text.startswith("#ABNF 1.0 UTF-8;\nlanguage fr-FR;\n")
    assert "{<app>hello</app><par>default</par>}" in r.text
    assert client.get("/asr/grammar/launch.txt", headers=RABBIT).status_code == 404


def test_grammar_without_apps_folder_is_void(tmp_path):
    host = AppHost(tmp_path / "absent", None)
    assert "public $App = $VOID;" in host.grammar_text()
    assert host.keys() == []


def test_log_lines_are_escaped_and_capped(client, host):
    client.post("/log", content="ligne 1\nfausse ligne\r" + "x" * 5000, headers=RABBIT)
    (line,) = host.reports.since(0)["lines"]
    assert "\n" not in line and "\r" not in line
    assert line.startswith("ligne 1\\nfausse ligne\\r")
    assert len(line) == apps_mod.MAX_LOG_LINE


def test_log_by_get_and_post(client, host):
    assert client.get("/log?msg=bonjour&msg=lapin", headers=RABBIT).text == "OK"
    assert client.get("/log", headers=RABBIT).text == "OK"
    assert client.post("/log", content="depuis http.post", headers=RABBIT).text == "OK"
    assert host.reports.since(0)["lines"] == ["bonjour lapin", "(no msg)", "depuis http.post"]


def test_text_page(client):
    assert client.get("/text", headers=RABBIT).text == "hello from the app server"


def test_upload_raw_body(client, tmp_path):
    r = client.post("/upload/photo cam", content=b"\xff\xd8jpeg",
                    headers={**RABBIT, "Content-Type": "image/jpeg"})
    assert r.status_code == 200 and r.text == "OK"
    (saved,) = (tmp_path / "uploads").iterdir()
    assert saved.name.endswith("-photo_cam.bin")
    assert saved.read_bytes() == b"\xff\xd8jpeg"


def test_upload_multipart_saves_file_parts_only(client, tmp_path):
    r = client.post("/upload", files={"pic": ("a/../b.jpg", b"JPEG")}, data={"note": "salut"},
                    headers=RABBIT)
    assert r.status_code == 200
    (saved,) = (tmp_path / "uploads").iterdir()
    assert saved.name.endswith("-upload-a_.._b.jpg")
    assert saved.read_bytes() == b"JPEG"


def test_upload_same_second_does_not_overwrite(client, tmp_path):
    for body in (b"un", b"deux"):
        client.post("/upload/x", content=body, headers=RABBIT)
    assert sorted(p.read_bytes() for p in (tmp_path / "uploads").iterdir()) == [b"deux", b"un"]


def test_upload_too_large_is_413(client, tmp_path, monkeypatch):
    monkeypatch.setattr(apps_mod, "MAX_UPLOAD", 10)
    r = client.post("/upload", content=b"x" * 11, headers=RABBIT)
    assert r.status_code == 413
    assert not any((tmp_path / "uploads").glob("*"))


def test_old_uploads_are_pruned(client, tmp_path, monkeypatch):
    monkeypatch.setattr(apps_mod, "KEEP_UPLOADS", 3)
    for i in range(5):
        client.post(f"/upload/n{i}", content=b"x", headers=RABBIT)
    assert len(list((tmp_path / "uploads").iterdir())) == 3


def test_uploads_are_capped_in_total_bytes(client, tmp_path, monkeypatch):
    monkeypatch.setattr(apps_mod, "KEEP_BYTES", 25)
    for i in range(4):
        client.post(f"/upload/n{i}", content=b"x" * 10, headers=RABBIT)
    files = sorted((tmp_path / "uploads").iterdir())
    assert sum(p.stat().st_size for p in files) <= 25
    assert len(files) == 2 and files[-1].name.endswith("-n3.bin")


def test_upload_is_streamed_to_a_part_file(host):
    fd, part = host.part_file()
    with open(fd, "wb") as f:
        f.write(b"flux")
    saved = host.store("x", "application/octet-stream", part)
    assert not part.exists()
    (stored,) = host.uploads.iterdir()
    assert stored.read_bytes() == b"flux" and saved[0].startswith(stored.name)


def test_too_many_uploads_at_once_is_503(client, host, monkeypatch):
    monkeypatch.setattr(apps_mod, "MAX_UPLOADS_AT_ONCE", 1)
    assert host.begin_upload()
    try:
        assert client.post("/upload", content=b"x", headers=RABBIT).status_code == 503
    finally:
        host.end_upload()
    assert client.post("/upload", content=b"x", headers=RABBIT).status_code == 200


def test_upload_disabled_without_folder(tmp_path):
    settings = load_settings(tmp_path / "config.toml")
    c = TestClient(create_app(settings, apps=AppHost(None, None)), base_url="http://localhost")
    assert c.post("/upload", content=b"x", headers=RABBIT).status_code == 404
    assert c.get("/file/hello/1/get", headers=RABBIT).status_code == 404


def test_app_routes_skip_the_csrf_header_and_host_check(client):
    # Les lapins n'envoient ni X-LapiNou ni un nom connu ; la page, elle, reste gardée.
    assert client.post("/log", content="x", headers=RABBIT).status_code == 200
    assert client.get("/", headers=RABBIT).status_code == 400


# ---- serveur ASR de compatibilité (8443) ----

def _frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def _grammar(text: bytes) -> bytes:
    return _frame(asr_server.length_field(1, text))


def _audio(data: bytes) -> bytes:
    return _frame(asr_server.length_field(2, data))


def _read_reply(s: socket.socket) -> bytes | None:
    head = s.recv(4)
    if not head:
        return None
    (size,) = struct.unpack(">I", head)
    return s.recv(size)


@pytest.fixture
def asr(host):
    servers = []

    def start(recogniser=None):
        server = asr_compat.make_server(recogniser, host, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def test_asr_compat_answers_nomatch_without_models(asr, monkeypatch):
    monkeypatch.setattr(asr_server, "AFTER_S", 0.0)
    server = asr()
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(_grammar(b"http://srv/asr/grammar/launch.abnf") + _audio(b"\xff" * 1024))
        assert _read_reply(s) == asr_server.response_message("NOMATCH", score=0)


def test_asr_compat_resolves_only_our_launch_grammar(host, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("URL lue sur le réseau")

    monkeypatch.setattr(asr_server.urllib.request, "urlopen", no_network)
    server = asr_compat.make_server(None, host, "127.0.0.1", 0)
    server.server_close()
    handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
    assert "<app>hello</app>" in handler.resolve("http://192.168.1.10/asr/grammar/l-1.abnf")
    for url in ("http://169.254.169.254/latest/meta-data", "https://evil.test/g.txt",
                "http://127.0.0.1:8080/api/settings"):
        assert "public $App = $VOID;" in handler.resolve(url)
    assert handler.resolve("public $App = oui;") == "public $App = oui;"


def test_asr_compat_does_not_touch_lapinlibre_globals(host):
    before = (asr_server.Session.recogniser, asr_server.Session.answer,
              asr_server.Session.grammar_source)
    server = asr_compat.make_server(object(), host, "127.0.0.1", 0)
    server.server_close()
    assert (asr_server.Session.recogniser, asr_server.Session.answer,
            asr_server.Session.grammar_source) == before
    assert server.RequestHandlerClass.recogniser is not None


def test_asr_compat_closes_on_oversized_frame(asr):
    server = asr()
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(struct.pack(">I", asr_compat.MAX_FRAME + 1))
        assert s.recv(4) == b""  # connexion fermée sans rien lire de plus


def test_asr_compat_closes_after_max_audio(asr, monkeypatch):
    monkeypatch.setattr(asr_server, "AFTER_S", 3600.0)
    monkeypatch.setattr(asr_compat, "MAX_AUDIO_BYTES", 1000)
    server = asr()
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(_audio(b"\xff" * 600) + _audio(b"\xff" * 600))
        assert s.recv(4) == b""


def test_asr_compat_session_has_a_maximum_duration(asr, monkeypatch):
    monkeypatch.setattr(asr_compat, "MAX_SESSION_S", 0.5)
    server = asr()
    started = time.monotonic()
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(_grammar(b"public $App = oui;"))
        assert s.recv(4) == b""
    assert time.monotonic() - started < 3


def test_asr_compat_limits_concurrent_sessions(asr, monkeypatch):
    monkeypatch.setattr(asr_compat, "MAX_SESSIONS", 1)
    server = asr()
    with socket.create_connection(server.server_address, timeout=5) as first:
        first.sendall(_grammar(b"public $App = oui;"))
        time.sleep(0.2)
        with socket.create_connection(server.server_address, timeout=5) as second:
            assert _read_reply(second) == asr_server.response_message("BUSY")


def test_asr_compat_stop_cuts_open_sessions(host):
    server = asr_compat.make_server(None, host, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(_grammar(b"public $App = oui;"))
        time.sleep(0.2)
        server.stop()
        assert s.recv(4) == b""


class FakeRecognizer:
    def __init__(self, text):
        self.text = text

    def create_stream(self):
        text = self.text

        class Stream:
            result = type("R", (), {"text": text})()

            def accept_waveform(self, rate, samples):
                pass

        return Stream()

    def decode_stream(self, stream):
        pass


class FakeRecogniser:
    def __init__(self, text="mot secret"):
        self.lock = threading.Lock()
        self.recognizer = FakeRecognizer(text)


def test_transcription_waits_a_bounded_time_for_the_shared_model():
    rec = FakeRecogniser()
    turn = threading.Semaphore(1)
    assert asr_compat.transcribe_bounded(rec, [0.0] * 16000, 1.0, turn) == "mot secret"
    rec.lock.acquire()  # l'écoute de Lapinou transcrit
    try:
        started = time.monotonic()
        assert asr_compat.transcribe_bounded(rec, [0.0] * 16000, 0.3, turn) is None
        assert time.monotonic() - started < 1.0
    finally:
        rec.lock.release()
    with turn:  # une autre session 8443 transcrit
        assert asr_compat.transcribe_bounded(rec, [0.0] * 16000, 0.2, turn) is None


def test_asr_compat_logs_neither_grammar_nor_words(host, caplog, capsys):
    server = asr_compat.make_server(FakeRecogniser("ouvre la radio"), host, "127.0.0.1", 0)
    server.server_close()
    handler = server.RequestHandlerClass.__new__(server.RequestHandlerClass)
    caplog.set_level(logging.DEBUG)
    payload, score = handler.recognised("public $App = ouvre la radio {<app>radio</app>};",
                                        "speech", [0.0] * 16000)
    assert payload == asr_server.response_message("OK", words="ouvre la radio",
                                                   semantic="<app>radio</app>", score=100)
    assert score == 100
    assert "radio" not in capsys.readouterr().out + caplog.text


def test_asr_compat_session_log_has_only_duration_and_score(asr, monkeypatch, caplog, capsys):
    monkeypatch.setattr(asr_server, "AFTER_S", 0.0)
    caplog.set_level(logging.INFO, logger="lapinou.web.asr_compat")
    server = asr()
    with socket.create_connection(server.server_address, timeout=5) as s:
        s.sendall(_grammar(b"public $App = grammaire privee;") + _audio(b"\xff" * 800))
        _read_reply(s)
    deadline = time.monotonic() + 3
    while "session ASR" not in caplog.text and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "0.1 s d'audio, score 0" in caplog.text
    assert "privee" not in caplog.text + capsys.readouterr().out
