# SPDX-License-Identifier: MIT
"""Endpoints LapinLibre des applications sur 8080 (url_app des lapins) : le contrat HTTP de
tools/app-server/app_server.py (HttpHandler), hors du contrôle de Host de la page."""

import io
import shutil
import socket
import struct
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lapinou.app import create_app
from lapinou.config import load_settings
from lapinou.web import apps as apps_mod
from lapinou.web import asr_compat
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


@pytest.mark.parametrize("path", ["/file/nope/1/get", "/file/../1/get", "/file/hello/1/put"])
def test_unknown_app_is_404(client, path):
    assert client.get(path, headers=RABBIT).status_code == 404


def test_launch_grammar_lists_hosted_apps(client):
    r = client.get("/asr/grammar/launch-42.abnf", headers=RABBIT)
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.text.startswith("#ABNF 1.0 UTF-8;\nlanguage fr-FR;\n")
    assert "{<app>hello</app><par>default</par>}" in r.text
    assert client.get("/asr/grammar/launch.txt", headers=RABBIT).status_code == 404


def test_grammar_without_apps_folder_is_void(tmp_path):
    host = AppHost(tmp_path / "absent", None)
    assert "public $App = $VOID;" in host.grammar_text()
    assert host.keys() == []


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


def _field(number: int, payload: bytes) -> bytes:
    from lapinou.voice.vad import asr_server

    return asr_server.length_field(number, payload)


def test_asr_compat_answers_nomatch_without_models(host, monkeypatch):
    from lapinou.voice.vad import asr_server

    monkeypatch.setattr(asr_server, "AFTER_S", 0.0)
    server = asr_compat.make_server(None, host, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(server.server_address, timeout=5) as s:
            s.sendall(_frame(_field(1, b"http://srv/asr/grammar/launch.abnf")))
            s.sendall(_frame(_field(2, b"\xff" * 1024)))
            (size,) = struct.unpack(">I", s.recv(4))
            reply = s.recv(size)
        assert reply == asr_server.response_message("NOMATCH", score=0)
    finally:
        server.shutdown()
        server.server_close()


def test_asr_compat_resolves_launch_grammar_from_apps(host):
    from lapinou.voice.vad import asr_server

    server = asr_compat.make_server(None, host, "127.0.0.1", 0)
    server.server_close()
    text = asr_server.resolve("http://192.168.1.10/asr/grammar/launch-1.abnf")
    assert "<app>hello</app>" in text


def test_asr_compat_uses_the_shared_recogniser(host):
    from lapinou.voice.vad import asr_server

    shared = object()
    server = asr_compat.make_server(shared, host, "127.0.0.1", 0)
    server.server_close()
    assert asr_server.Session.recogniser is shared
    assert asr_server.Session.answer == "recognise"
