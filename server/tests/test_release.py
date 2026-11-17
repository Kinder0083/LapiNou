# SPDX-License-Identifier: MIT
"""Assemblage d'une version (scripts/lapinou-release.sh, Tâche 26) sur de faux fichiers.

Paire de clés jetable créée au lancement (R42) : jamais de vraie clé dans le dépôt. Le
manifeste produit est vérifié par tools/sign-ota.py --verify et par le code du relais OTA
(lapinou.ota), comme le feront les lapins et le serveur.
"""

import os
import shutil
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


def _run(*args: str | Path, script: Path = SCRIPT, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *map(str, args)], capture_output=True, text=True,
                          cwd=REPO, env=env)


# Le fichier provisoire tel qu'il est livré avant la Tâche 26, copié dans tmp_path : les tests ne
# dépendent jamais des fichiers .pub du dépôt, qui deviennent la vraie clé (docs/lapinou/release.md).
PLACEHOLDER_PUB = (b"# SPDX-License-Identifier: MIT\n"
                   b"# PLACEHOLDER: this is NOT a public key. The owner replaces this file.\n")
PUB_FILES = ("image/core/usr/share/lapinou/ota-pubkey.pub", "server/lapinou/ota-pubkey.pub")


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


def test_placeholder_public_key_is_refused(setup):
    tmp, key, pub, fw, dist = setup
    placeholder = tmp / "placeholder-ota-pubkey.pub"
    placeholder.write_bytes(PLACEHOLDER_PUB)
    r = _run("check-key", "--signing-key", key, "--pubkey", pub, "--pubkey", placeholder)
    assert r.returncode != 0
    assert "PLACEHOLDER" in r.stderr and str(placeholder) in r.stderr
    r = _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
             "--pubkey", placeholder, "--out", dist)
    assert r.returncode != 0 and "PLACEHOLDER" in r.stderr
    assert not dist.exists() or not any(dist.iterdir())


def _repo_copy(dest: Path) -> Path:
    """Une copie du dépôt (fichiers suivis et nouveaux, pas les ignorés) en dépôt git jetable :
    celui du propriétaire après l'étape 6 de docs/lapinou/release.md."""
    listed = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard", "--", "scripts", "tools", "server", "userland/proto",
                             "installer", "docs/lapinou", "LICENSES", "image/core/usr/share/lapinou",
                             ".github/workflows"], capture_output=True, check=True).stdout
    for name in filter(None, listed.decode().split("\0")):
        src = REPO / name
        if src.is_file():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / name)
    git = ["git", "-C", str(dest), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
           "-c", "commit.gpgsign=false"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "copie"], check=True)
    return dest


@pytest.mark.skipif(os.environ.get("LAPINOU_OWNER_STEP6") == "1", reason="déjà dans la copie")
def test_owner_real_public_key_keeps_the_release_and_the_tests_working(tmp_path):
    """Étape 6 du propriétaire simulée : la vraie clé publique (ici jetable) remplace les deux
    fichiers provisoires d'une copie du dépôt ; la livraison et les tests OTA du serveur passent
    toujours (aucun test ne doit exiger PLACEHOLDER dans le dépôt)."""
    key, pub = _keys(tmp_path, "owner")
    copy = _repo_copy(tmp_path / "repo")
    for rel in PUB_FILES:
        shutil.copy2(pub, copy / rel)
    script = copy / "scripts" / "lapinou-release.sh"

    r = _run("check-key", "--signing-key", key, script=script)  # sans --pubkey : les fichiers du dépôt
    assert r.returncode == 0, r.stderr
    dist = tmp_path / "dist"
    r = _run("assemble", "--version", VERSION, "--firmware", _firmware(tmp_path), "--signing-key", key,
             "--out", dist, script=script)
    assert r.returncode == 0, r.stderr
    verify_signature(load_pubkey(copy / "server/lapinou/ota-pubkey.pub"),
                     (dist / "manifest.json").read_bytes(), (dist / "manifest.json.sig").read_bytes())

    # Les tests de la copie, avec le paquet lapinou de la copie (sa clé livrée est la vraie).
    env = {**os.environ, "PYTHONPATH": str(copy / "server"), "LAPINOU_OWNER_STEP6": "1"}
    which = subprocess.run([sys.executable, "-c", "import lapinou.ota as o; print(o.PACKAGED_PUBKEY)"],
                           capture_output=True, text=True, env=env, cwd=copy / "server")
    assert which.stdout.strip() == str(copy / "server/lapinou/ota-pubkey.pub"), which.stderr
    t = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_ota_relay.py", "tests/test_release.py"],
                       capture_output=True, text=True, env=env, cwd=copy / "server")
    assert t.returncode == 0, t.stdout[-3000:] + t.stderr[-2000:]


@pytest.mark.parametrize("fails", ["sign", "verify"])
def test_failure_after_dist_is_made_empties_it(setup, fails):
    """sign-ota.py qui échoue (python3 simulé) une fois DIST rempli : DIST est vidé."""
    tmp, key, pub, fw, dist = setup
    stubs = tmp / "stubs"
    stubs.mkdir()
    stub = stubs / "python3"
    if fails == "sign":
        stub.write_text("#!/bin/sh\necho 'sign-ota: simulated failure' >&2\nexit 1\n")
    else:  # la signature passe, la vérification échoue (manifeste déjà écrit)
        stub.write_text('#!/bin/sh\ncase " $* " in *" --verify "*) exit 1 ;; esac\n'
                        f'exec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    r = _run("assemble", "--version", VERSION, "--firmware", fw, "--signing-key", key,
             "--pubkey", pub, "--out", dist, env=env)
    assert r.returncode != 0
    assert dist.is_dir() and not any(dist.iterdir())


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
    tag_push = "github.event_name == 'push' && github.ref_type == 'tag'"
    assert jobs["publish"]["if"] == tag_push  # « Run workflow » sur une étiquette : rien de publié
    signing = [s for s in jobs["preflight"]["steps"] if "OTA_SIGNING_KEY" in s.get("env", {})]
    assert [s["if"] for s in signing] == [tag_push]
    zip_step = next(s for s in jobs["key"]["steps"] if s.get("name") == "Zip the key")
    assert "RELEASE_VERSION" in zip_step["run"]
    publish = "\n".join(s.get("run", "") for s in jobs["publish"]["steps"])
    assert "gh release create" in publish and "lapinou-release.sh assemble" in publish
    assert publish.index("lapinou-release.sh assemble") < publish.index("gh release create")


def test_release_workflow_token_writes_only_in_publish():
    wf = _workflow("release.yml")
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        if name == "publish":
            assert job["permissions"] == {"contents": "write"}
        else:
            assert "write" not in str(job.get("permissions", "")), name


@pytest.mark.parametrize("name", ["release.yml", "server.yml"])
def test_checkouts_keep_no_credentials(name):
    steps = [s for job in _workflow(name)["jobs"].values() for s in job.get("steps", [])]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts
    for s in checkouts:
        assert s.get("with", {}).get("persist-credentials") is False, s


def test_server_workflow_runs_on_every_push_and_from_the_release():
    wf = _workflow("server.yml")
    on = wf[True] if True in wf else wf["on"]  # YAML 1.1 : « on » se lit True
    assert "push" in on and "workflow_call" in on
    runs = "\n".join(s.get("run", "") for job in wf["jobs"].values() for s in job["steps"])
    assert "uv run pytest" in runs
    assert "installer/tests/run.sh" in runs
    assert "shellcheck" in runs
