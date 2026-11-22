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


def test_main_reports_a_failure_without_traceback(tmp_path, monkeypatch, capsys):
    def offline(directory=None):
        raise OSError("réseau coupé")

    monkeypatch.setattr(models, "fetch", offline)
    assert models.main(["models", "fetch", "--dir", str(tmp_path)]) == 1
    assert "échec du téléchargement : réseau coupé" in capsys.readouterr().err


# --- voix Piper (R31) et venv Piper (R30) -------------------------------------------------------

from lapinou.voice import tts  # noqa: E402

VOICE = "fr_FR-siwis-medium"
PIPER_CONTENT = {f"{VOICE}.onnx": b"faux modele", f"{VOICE}.onnx.json": b'{"audio": {}}'}


@pytest.fixture
def piper_pins(monkeypatch):
    monkeypatch.setitem(
        tts.PIPER_VOICES, VOICE, {n: hashlib.sha256(c).hexdigest() for n, c in PIPER_CONTENT.items()}
    )


def piper_served() -> dict[str, bytes]:
    return {url: PIPER_CONTENT[name] for name, url in models.piper_sources(VOICE).items()}


def test_piper_sources_are_pinned_to_a_revision():
    urls = models.piper_sources(VOICE)
    assert set(urls) == {f"{VOICE}.onnx", f"{VOICE}.onnx.json"}
    for url in urls.values():
        assert f"rhasspy/piper-voices/resolve/{tts.PIPER_REVISION}/fr/fr_FR/siwis/medium/" in url
    assert len(tts.PIPER_REVISION) == 40


def test_fetch_piper_downloads_verifies_and_skips(tmp_path, piper_pins):
    seen: list[str] = []
    assert models.fetch_piper(VOICE, tmp_path, make_opener(piper_served(), seen)) == tmp_path
    assert len(seen) == 2
    assert (tmp_path / f"{VOICE}.onnx").read_bytes() == PIPER_CONTENT[f"{VOICE}.onnx"]
    models.fetch_piper(VOICE, tmp_path, make_opener({}, seen))  # déjà là : aucune requête
    assert len(seen) == 2


def test_fetch_piper_rejects_a_wrong_hash(tmp_path, piper_pins):
    served = {url: b"autre chose" for url in piper_served()}
    with pytest.raises(RuntimeError, match="SHA-256"):
        models.fetch_piper(VOICE, tmp_path, make_opener(served, []))
    assert not list(tmp_path.iterdir())


def test_main_fetch_gets_asr_and_the_piper_voice(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(models, "fetch", lambda d=None: calls.append(("asr", d)) or tmp_path)
    monkeypatch.setattr(
        models, "fetch_piper", lambda voice, d=None: calls.append(("piper", voice, d)) or tmp_path
    )
    monkeypatch.setattr(models, "fetch_sounds", lambda d=None: calls.append(("sons", d)) or d)
    assert models.main(["models", "fetch"]) == 0
    assert calls == [("asr", None), ("piper", tts.DEFAULT_PIPER_VOICE, None), ("sons", tmp_path)]


def test_piper_setup_creates_a_separate_venv(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(models.shutil, "which", lambda n: "/usr/bin/uv" if n == "uv" else None)
    venv = tmp_path / "piper-venv"
    models.piper_setup(venv, run=lambda cmd, **k: ran.append(cmd))
    assert ran[0][:3] == ["/usr/bin/uv", "venv", str(venv)]
    assert ran[1][:3] == ["/usr/bin/uv", "pip", "install"]
    assert f"piper-tts=={models.PIPER_TTS_VERSION}" in ran[1]
    assert str(venv / "bin" / "python") in ran[1]


# --- Reconnaissance des sons : archive de la release sherpa-onnx ------------------------------

import tarfile  # noqa: E402

from lapinou.voice import sounds  # noqa: E402

SOUND_FILES = {sounds.MODEL: b"faux modele CED", sounds.LABELS: b"index,mid,display_name\n"}


def sound_archive(files=SOUND_FILES, extra=None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, data in {**files, **(extra or {})}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def sound_pins(monkeypatch):
    archive = sound_archive(extra={f"{sounds.MODEL_DIR}/test_wavs/1.wav": b"wav"})
    monkeypatch.setattr(models, "SOUNDS_ARCHIVE_SHA256", hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(models, "SOUNDS_SHA256",
                        {n: hashlib.sha256(c).hexdigest() for n, c in SOUND_FILES.items()})
    return archive


def test_fetch_sounds_keeps_only_the_model_and_its_labels(tmp_path, sound_pins):
    seen: list[str] = []
    models.fetch_sounds(tmp_path, make_opener({models.SOUNDS_URL: sound_pins}, seen))
    assert seen == [models.SOUNDS_URL]
    assert sounds.model_present(tmp_path)
    assert (tmp_path / sounds.MODEL).read_bytes() == SOUND_FILES[sounds.MODEL]
    assert sorted(p.name for p in (tmp_path / sounds.MODEL_DIR).iterdir()) == [
        "class_labels_indices.csv", "model.int8.onnx"]
    assert not list(tmp_path.glob("*.tar.bz2*"))
    models.fetch_sounds(tmp_path, make_opener({}, seen))  # déjà là : aucune requête
    assert len(seen) == 1


def test_fetch_sounds_rejects_an_archive_with_another_hash(tmp_path, sound_pins):
    with pytest.raises(RuntimeError, match="SHA-256"):
        models.fetch_sounds(tmp_path, make_opener({models.SOUNDS_URL: b"autre"}, []))
    assert not sounds.model_present(tmp_path)
    assert not list(tmp_path.rglob("*.onnx"))


def test_fetch_sounds_rejects_a_file_with_another_hash(tmp_path, monkeypatch, sound_pins):
    monkeypatch.setitem(models.SOUNDS_SHA256, sounds.MODEL, "0" * 64)
    with pytest.raises(RuntimeError, match="SHA-256"):
        models.fetch_sounds(tmp_path, make_opener({models.SOUNDS_URL: sound_pins}, []))
    assert not sounds.model_present(tmp_path)


def test_the_sound_model_is_pinned():
    assert models.SOUNDS_URL.startswith(
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/audio-tagging-models/")
    assert len(models.SOUNDS_ARCHIVE_SHA256) == 64
    assert set(models.SOUNDS_SHA256) == {sounds.MODEL, sounds.LABELS}
