# SPDX-License-Identifier: MIT
"""Téléchargeur des modèles de transcription, sans réseau : ouvreur d'URL factice et SHA-256
de petits faux fichiers à la place des vrais."""

from __future__ import annotations

import hashlib
import io

import pytest

from lapinou.voice import models, wake_asr

asr_server = wake_asr.asr_server
CONTENT = {name: f"contenu de {name}".encode() for name in asr_server.MODEL_SHA256}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def make_opener(served: dict[str, bytes], seen: list[str]):
    def opener(url, timeout=None):
        seen.append(url)
        return FakeResponse(served[url])

    return opener


@pytest.fixture
def pins(monkeypatch):
    monkeypatch.setattr(
        asr_server, "MODEL_SHA256", {n: hashlib.sha256(c).hexdigest() for n, c in CONTENT.items()}
    )


def served_files() -> dict[str, bytes]:
    return {url: CONTENT[name] for name, url in models.sources().items()}


def test_sources_use_the_pinned_hugging_face_revision_and_the_silero_release():
    urls = models.sources()
    parakeet = [u for n, u in urls.items() if n.startswith(asr_server.PARAKEET)]
    assert len(parakeet) == 4 and all(models.HF_REVISION in u for u in parakeet)
    assert urls["silero_vad.onnx"] == asr_server.RELEASES + "silero_vad.onnx"
    assert models.HF_REVISION.startswith("2bda32ec70b0")


def test_fetch_downloads_verifies_and_skips_what_is_there(tmp_path, pins):
    seen: list[str] = []
    assert models.fetch(tmp_path, make_opener(served_files(), seen)) == tmp_path
    assert len(seen) == len(CONTENT)
    for name, content in CONTENT.items():
        assert (tmp_path / name).read_bytes() == content
    assert not list(tmp_path.rglob("*.part"))
    models.fetch(tmp_path, make_opener({}, seen))  # tout est là et valide : aucune requête
    assert len(seen) == len(CONTENT)


def test_fetch_redownloads_a_corrupted_file(tmp_path, pins):
    models.fetch(tmp_path, make_opener(served_files(), []))
    victim = tmp_path / "silero_vad.onnx"
    victim.write_bytes("abîmé".encode())
    seen: list[str] = []
    models.fetch(tmp_path, make_opener(served_files(), seen))
    assert seen == [asr_server.RELEASES + "silero_vad.onnx"]
    assert victim.read_bytes() == CONTENT["silero_vad.onnx"]


def test_checksum_mismatch_is_an_error_and_keeps_nothing(tmp_path, pins):
    served = served_files()
    bad = next(u for n, u in models.sources().items() if n.endswith("encoder.int8.onnx"))
    served[bad] = b"pas le bon fichier"
    with pytest.raises(RuntimeError, match="SHA-256"):
        models.fetch(tmp_path, make_opener(served, []))
    assert not (tmp_path / asr_server.PARAKEET / "encoder.int8.onnx").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_main_usage(capsys):
    assert models.main(["models"]) == 2
    assert models.main(["models", "fetch", "--bogus", "x"]) == 2
    assert "usage" in capsys.readouterr().err
