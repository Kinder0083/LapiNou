# SPDX-License-Identifier: MIT
"""Assemblage d'une version (scripts/lapinou-release.sh, Tâche 26) sur de faux fichiers.

Paire de clés jetable créée au lancement (R42) : jamais de vraie clé dans le dépôt. Le
manifeste produit est vérifié par tools/sign-ota.py --verify et par le code du relais OTA
(lapinou.ota), comme le feront les lapins et le serveur.
"""

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (Encoding, NoEncryption, PrivateFormat,
                                                          PublicFormat)

from lapinou.ota import _KEY_ZIP, load_pubkey, parse_manifest, verify_signature

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "lapinou-release.sh"
SIGN_OTA = REPO / "tools" / "sign-ota.py"
VERSION = "v1.2.3"
IMAGES = ("zImage", "rootfs.img.gz", "usr.tar.gz")
KEY_ZIP = f"lapinou-key-{VERSION}.zip"
SERVER = f"lapinou-server-{VERSION}.tar.gz"
ASSETS = {"manifest.json", "manifest.json.sig", *IMAGES, KEY_ZIP, SERVER}


def _keys(tmp: Path, name: str) -> tuple[Path, Path]:
    """Une paire jetable : clé privée (ota-signing.key) et publique (.pub, ligne SPDX en tête,
    comme les fichiers du dépôt)."""
    private = Ed25519PrivateKey.generate()
    key = tmp / f"{name}-ota-signing.key"
    key.write_bytes(private.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    key.chmod(0o600)
    pub = tmp / f"{name}-ota-pubkey.pub"
    pub.write_bytes(b"# SPDX-License-Identifier: MIT\n"
                    + private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    return key, pub


def _firmware(tmp: Path, release_version: str = VERSION) -> Path:
    fw = tmp / "firmware"
    fw.mkdir()
    for i, name in enumerate(IMAGES):
        (fw / name).write_bytes(os.urandom(1000 + i))
    (fw / "install.conf").write_text(f"BUILD_ID=0123456789abcdef\nBUILD_VERSION={VERSION}\n"
                                     f"RELEASE_VERSION={release_version}\n")
    (fw / KEY_ZIP).write_bytes(b"PK\x05\x06" + bytes(18))  # une archive zip vide
    return fw


def _run(*args: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                          cwd=REPO)


@pytest.fixture
def setup(tmp_path):
    key, pub = _keys(tmp_path, "good")
    return tmp_path, key, pub, _firmware(tmp_path), tmp_path / "dist"


def test_assemble_signs_a_release_the_relay_and_sign_ota_accept(setup):
    tmp, key, pub, fw, dist = setup
    r = _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
             "--pubkey", pub, "--pubkey", pub, "--out", dist)
    assert r.returncode == 0, r.stderr
    assert {p.name for p in dist.iterdir()} == ASSETS  # rien d'autre (ni install.conf, ni .sha256)

    manifest = (dist / "manifest.json").read_bytes()
    signature = (dist / "manifest.json.sig").read_bytes()
    assert len(signature) == 64
    verify_signature(load_pubkey(pub), manifest, signature)  # le code du relais OTA
    version, files = parse_manifest(manifest)
    assert version == VERSION
    assert set(files) == ASSETS - {"manifest.json", "manifest.json.sig"}  # clé USB et serveur compris (R41)
    assert [n for n in files if _KEY_ZIP.fullmatch(n)] == [KEY_ZIP]  # le relais trouve la clé USB
    for name in IMAGES:
        assert (dist / name).read_bytes() == (fw / name).read_bytes()

    v = subprocess.run([sys.executable, "-I", str(SIGN_OTA), "--verify", "--pubkey", str(pub), "--dir",
                        str(dist)], capture_output=True, text=True)
    assert v.returncode == 0, v.stderr

    with tarfile.open(dist / SERVER) as t:
        names = t.getnames()
    prefix = f"lapinou-server-{VERSION}/"
    assert all(n.startswith(prefix) or n == prefix.rstrip("/") for n in names)
    for needed in ("server/pyproject.toml", "server/uv.lock", "server/lapinou/ota.py",
                   "server/lapinou/ota-pubkey.pub", "tools/app-server", "userland/proto/lapinou-audio.proto",
                   "installer/lapinou-install.sh", "LICENSES/MIT.txt"):
        assert prefix + needed in names, needed
    assert not any(".pem" in n or n.endswith(".key") for n in names)


def test_other_signer_is_refused_by_the_relay(setup):
    tmp, key, pub, fw, dist = setup
    assert _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
                "--pubkey", pub, "--out", dist).returncode == 0
    _, other = _keys(tmp, "other")
    v = subprocess.run([sys.executable, "-I", str(SIGN_OTA), "--verify", "--pubkey", str(other), "--dir",
                        str(dist)], capture_output=True, text=True)
    assert v.returncode == 1


@pytest.mark.parametrize("how", ["missing", "empty", "not-a-key"])
def test_no_signing_key_fails_before_anything_is_made(setup, how):
    tmp, key, pub, fw, dist = setup
    if how == "missing":
        key = tmp / "absent-ota-signing.key"
    elif how == "empty":
        key.write_bytes(b"")
    else:
        key.write_text("pas une clé\n")
    r = _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
             "--pubkey", pub, "--out", dist)
    assert r.returncode != 0
    assert "OTA_SIGNING_KEY" in r.stderr
    assert not dist.exists() or not any(dist.iterdir())


def test_key_that_does_not_match_the_committed_public_keys_is_refused(setup):
    tmp, key, pub, fw, dist = setup
    _, other = _keys(tmp, "other")
    r = _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
             "--pubkey", pub, "--pubkey", other, "--out", dist)
    assert r.returncode != 0
    assert str(other) in r.stderr
    assert not dist.exists() or not any(dist.iterdir())


def test_placeholder_public_keys_of_the_repository_are_refused(setup):
    tmp, key, pub, fw, dist = setup
    # Sans --pubkey : les deux fichiers du dépôt, provisoires tant que le propriétaire n'a pas
    # mis la vraie clé (docs/lapinou/release.md).
    r = _run("check-key", "--signing-key", key)
    assert r.returncode != 0
    assert "PLACEHOLDER" in r.stderr
    assert "image/core/usr/share/lapinou/ota-pubkey.pub" in r.stderr


def test_check_key_accepts_the_matching_pair(setup):
    tmp, key, pub, fw, dist = setup
    r = _run("check-key", "--signing-key", key, "--pubkey", pub)
    assert r.returncode == 0, r.stderr
    assert "BEGIN" not in r.stdout + r.stderr  # aucune clé n'est affichée


@pytest.mark.parametrize("version", ["v1.2", "1.2.3", "v1.2.3-rc1", ""])
def test_version_must_be_a_release_tag(setup, version):
    tmp, key, pub, fw, dist = setup
    r = _run("assemble", "--version", version, "--firmware", fw, "--signing-key", key,
             "--pubkey", pub, "--out", dist)
    assert r.returncode != 0
    assert not dist.exists() or not any(dist.iterdir())


def test_images_built_as_another_version_are_refused(tmp_path):
    key, pub = _keys(tmp_path, "good")
    fw = _firmware(tmp_path, release_version="0.0.0-0123456789ab")
    dist = tmp_path / "dist"
    r = _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
             "--pubkey", pub, "--out", dist)
    assert r.returncode != 0
    assert "RELEASE_VERSION" in r.stderr
    assert not (dist / "manifest.json").exists()


def test_missing_firmware_file_is_refused(setup):
    tmp, key, pub, fw, dist = setup
    (fw / KEY_ZIP).unlink()
    r = _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
             "--pubkey", pub, "--out", dist)
    assert r.returncode != 0
    assert KEY_ZIP in r.stderr
    assert not (dist / "manifest.json").exists()


def _workflow(name: str) -> dict:
    return yaml.safe_load((REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_release_workflow_signs_only_after_the_checks_and_never_shows_the_key():
    wf = _workflow("release.yml")
    jobs = wf["jobs"]
    assert set(jobs["publish"]["needs"]) >= {"preflight", "server", "key"}
    assert jobs["server"]["uses"] == "./.github/workflows/server.yml"
    assert "preflight" in jobs["key"]["needs"]  # pas de longue construction sans clé de signature
    text = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    # Le secret n'est lu que dans l'environnement d'une étape, jamais dans une commande.
    for line in text.splitlines():
        if "secrets.OTA_SIGNING_KEY" in line:
            assert line.strip() == "OTA_SIGNING_KEY: ${{ secrets.OTA_SIGNING_KEY }}", line
    assert text.count("shred -u") >= 2  # preflight et publish
    assert ".pem" not in text
    publish = "\n".join(s.get("run", "") for s in jobs["publish"]["steps"])
    assert "gh release create" in publish and "lapinou-release.sh assemble" in publish
    assert publish.index("lapinou-release.sh assemble") < publish.index("gh release create")


def test_server_workflow_runs_on_every_push_and_from_the_release():
    wf = _workflow("server.yml")
    on = wf[True] if True in wf else wf["on"]  # YAML 1.1 : « on » se lit True
    assert "push" in on and "workflow_call" in on
    runs = "\n".join(s.get("run", "") for job in wf["jobs"].values() for s in job["steps"])
    assert "uv run pytest" in runs
    assert "installer/tests/run.sh" in runs
    assert "shellcheck" in runs
