# SPDX-License-Identifier: MIT
"""Relais OTA : versions GitHub (faux GitHub, aucun appel réel) servies aux lapins."""

import hashlib
import json

import httpx2
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from lapinou.config import OtaConfig, Settings
from lapinou.ota import PACKAGED_PUBKEY, OtaError, OtaRelay

API = "https://api.github.com"
REPO = "Kinder0083/LapiNou"
# Paire jetable, créée au lancement des tests (R42) : jamais de vraie clé dans le dépôt.
SIGNER = Ed25519PrivateKey.generate()
OTHER = Ed25519PrivateKey.generate()


def _pem(private: Ed25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeGitHub:
    """Une dernière version publiée, ses fichiers servis par un hôte de stockage à part (comme
    GitHub : l'API renvoie une redirection vers objects.githubusercontent.com)."""

    def __init__(self, version="v1.2.3", files=None, key=True):
        self.requests: list[httpx2.Request] = []
        self.status = 200
        self.fail_network = False
        self.storage = "https://objects.example.com"  # où l'API redirige les téléchargements
        self.set_release(version, files, key)

    def set_release(self, version, files=None, key=True, manifest_files=None):
        files = dict(files or {"zImage": b"K" * 1000, "rootfs.img.gz": b"R" * 3000,
                               "usr.tar.gz": b"U" * 5000})
        if key:  # la clé USB est listée par le manifeste signé (R41)
            files[f"LapinLibre-{version}.zip"] = b"PK\x03\x04cle"
        listed = manifest_files or {n: {"sha256": _sha(d), "size": len(d)}
                                    for n, d in files.items()}
        # Mise en forme et ordre volontairement inhabituels : servis tels quels, octet pour octet.
        self.manifest = (
            '{"signature": "c2lnbmF0dXJl",\n  "version":"%s", "files": %s }\n'
            % (version, json.dumps(listed, indent=3))
        ).encode()
        self.assets = {"manifest.json": self.manifest,
                       "manifest.json.sig": SIGNER.sign(self.manifest), **files}
        if key:  # somme publiée à part, comme release.yml : elle ne fait plus foi
            zip_name = f"LapinLibre-{version}.zip"
            self.assets[zip_name + ".sha256"] = (
                f"{_sha(self.assets[zip_name])}  {zip_name}\n".encode())
        self.version = version

    def release(self):
        return {"tag_name": self.version, "assets": [
            {"id": i, "name": n, "size": len(d), "url": f"{API}/repos/{REPO}/releases/assets/{i}",
             "browser_download_url": f"https://github.com/{REPO}/releases/download/x/{n}"}
            for i, (n, d) in enumerate(self.assets.items())
        ]}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.fail_network:
            raise httpx2.ConnectError("pas de réseau")
        url = str(request.url)
        if url == f"{API}/repos/{REPO}/releases/latest":
            if self.status != 200:
                return httpx2.Response(self.status, json={"message": "Not Found"})
            return httpx2.Response(200, json=self.release())
        if url.startswith(f"{API}/repos/{REPO}/releases/assets/"):
            assert request.headers["accept"] == "application/octet-stream"
            i = int(url.rsplit("/", 1)[1])
            return httpx2.Response(302, headers={
                "location": f"{self.storage}/blob/{i}?sig=abc"})
        if "/blob/" in url:
            i = int(url.split("/blob/")[1].split("?")[0])
            return httpx2.Response(200, content=list(self.assets.values())[i])
        return httpx2.Response(404)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))


# Le fichier provisoire tel qu'il est livré avant la Tâche 26 : copié dans tmp_path, pour que les
# tests ne dépendent jamais du fichier du dépôt (qui devient la vraie clé, docs/lapinou/release.md).
PLACEHOLDER_PUB = (b"# SPDX-License-Identifier: MIT\n"
                   b"# PLACEHOLDER: this is NOT a public key. The owner replaces this file.\n")


def _placeholder(tmp_path):
    path = tmp_path / "placeholder-ota-pubkey.pub"
    path.write_bytes(PLACEHOLDER_PUB)
    return path


def _relay(gh, tmp_path, token="", clock=None, pubkey=None):
    kwargs = {"clock": clock} if clock else {}
    if pubkey is None:
        pubkey = tmp_path / "ota-pubkey.pub"
        pubkey.write_bytes(_pem(SIGNER))
    return OtaRelay(REPO, token, tmp_path / "ota", client=gh.client(), pubkey_path=pubkey,
                    **kwargs)


def _app(relay):
    from lapinou.app import create_app

    app = create_app(Settings())
    app.state.ota = relay
    return TestClient(app)


async def test_manifest_served_as_signed(tmp_path):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    status = await relay.refresh()
    assert status.version == "v1.2.3" and status.error == ""
    client = _app(relay)
    r = client.get("/ota/manifest.json")
    assert r.status_code == 200
    assert r.content == gh.manifest  # octet pour octet : la signature reste valable
    assert r.headers["content-type"].startswith("application/json")
    for name in ("zImage", "rootfs.img.gz", "usr.tar.gz"):
        r = client.get(f"/ota/{name}")
        assert r.status_code == 200 and r.content == gh.assets[name]
    for path in ("/ota/autre", "/ota/..%2Fcurrent", "/ota/LapinLibre-v1.2.3.zip.sha256"):
        assert client.get(path).status_code == 404


async def test_nothing_served_before_first_download(tmp_path):
    client = _app(_relay(FakeGitHub(), tmp_path))
    assert client.get("/ota/manifest.json").status_code == 404
    assert client.get("/ota/zImage").status_code == 404


async def test_token_goes_to_github_api_only(tmp_path):
    gh = FakeGitHub()
    await _relay(gh, tmp_path, token="github_pat_SECRET").refresh()
    for req in gh.requests:
        if req.url.host == "api.github.com":
            assert req.headers["authorization"] == "Bearer github_pat_SECRET"
        else:
            assert "authorization" not in req.headers


async def test_public_repo_without_token(tmp_path):
    gh = FakeGitHub()
    await _relay(gh, tmp_path).refresh()
    assert all("authorization" not in r.headers for r in gh.requests)


async def test_same_version_is_not_downloaded_again(tmp_path):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    gh.requests.clear()
    await relay.refresh()
    assert [str(r.url) for r in gh.requests] == [f"{API}/repos/{REPO}/releases/latest"]


async def test_corrupt_file_keeps_previous_release(tmp_path):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    old = gh.manifest
    good = {"zImage": b"k2", "rootfs.img.gz": b"r2", "usr.tar.gz": b"u2"}
    listed = {n: {"sha256": _sha(d), "size": len(d)} for n, d in good.items()}
    gh.set_release("v1.3.0", {**good, "usr.tar.gz": b"u3"}, manifest_files=listed)
    with pytest.raises(OtaError, match="usr.tar.gz"):
        await relay.refresh()
    assert relay.status().version == "v1.2.3"
    assert "usr.tar.gz" in relay.status().error
    client = _app(relay)
    assert client.get("/ota/manifest.json").content == old
    assert client.get("/ota/usr.tar.gz").content == b"U" * 5000
    leftovers = [p.name for p in (tmp_path / "ota").iterdir()]
    assert all(not n.startswith(".") for n in leftovers), leftovers


async def test_new_release_replaces_old_one(tmp_path):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    gh.set_release("v1.3.0", {"zImage": b"k2", "rootfs.img.gz": b"r2", "usr.tar.gz": b"u2"})
    assert (await relay.refresh()).version == "v1.3.0"
    client = _app(relay)
    assert client.get("/ota/manifest.json").content == gh.manifest
    assert client.get("/ota/zImage").content == b"k2"


async def test_rabbit_with_the_old_manifest_still_gets_the_old_files(tmp_path):
    """Le lapin lit le manifeste (et sa signature), la version change, puis il télécharge :
    par /ota/<version>/<fichier>, il reçoit les fichiers du manifeste qu'il a vérifié."""
    now = [1_000_000.0]
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path, clock=lambda: now[0])
    await relay.refresh()
    client = _app(relay)
    old_manifest = client.get("/ota/manifest.json").content
    old_sig = client.get("/ota/manifest.json.sig").content
    SIGNER.public_key().verify(old_sig, old_manifest)
    version = json.loads(old_manifest)["version"]
    gh.set_release("v1.3.0", {"zImage": b"k2", "rootfs.img.gz": b"r2", "usr.tar.gz": b"u2"})
    await relay.refresh()
    now[0] += 300
    r = client.get(f"/ota/{version}/zImage")
    assert r.status_code == 200 and r.content == b"K" * 1000
    assert client.get(f"/ota/{version}/manifest.json").content == old_manifest
    assert client.get(f"/ota/{version}/manifest.json.sig").content == old_sig
    assert client.get("/ota/v1.3.0/zImage").content == b"k2"
    assert client.get("/ota/zImage").content == b"k2"  # forme courte : la version en service
    for path in (f"/ota/{version}/autre", "/ota/v9.9.9/zImage", "/ota/..%2F/zImage"):
        assert client.get(path).status_code == 404
    now[0] += 301
    await relay.refresh()  # sursis écoulé : l'ancienne n'est plus servie
    assert client.get(f"/ota/{version}/zImage").status_code == 404


async def test_good_signature_is_served_with_the_manifest(tmp_path):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    sig = _app(relay).get("/ota/manifest.json.sig")
    assert sig.status_code == 200 and sig.content == gh.assets["manifest.json.sig"]


@pytest.mark.parametrize("case", ["autre clé", "tronquée", "absente", "manifeste modifié"])
async def test_bad_or_missing_signature_is_refused(tmp_path, case):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    gh.set_release("v1.3.0", {"zImage": b"k2"})
    if case == "autre clé":
        gh.assets["manifest.json.sig"] = OTHER.sign(gh.manifest)
    elif case == "tronquée":
        gh.assets["manifest.json.sig"] = gh.assets["manifest.json.sig"][:63]
    elif case == "absente":
        del gh.assets["manifest.json.sig"]
    else:
        gh.assets["manifest.json"] = gh.manifest.replace(b"v1.3.0", b"v1.3.0", 1) + b" "
    gh.requests.clear()
    with pytest.raises(OtaError, match="signature|manifest.json.sig"):
        await relay.refresh()
    assert relay.status().version == "v1.2.3"
    assert "signature" in relay.status().error or "sig" in relay.status().error
    # Aucun fichier de la version refusée n'a été téléchargé.
    blobs = [str(r.url) for r in gh.requests if "/blob/" in str(r.url)]
    names = list(gh.assets)
    assert all(names[int(u.split("/blob/")[1].split("?")[0])].startswith("manifest.json")
               for u in blobs)
    assert _app(relay).get("/ota/zImage").content == b"K" * 1000


async def test_placeholder_or_missing_public_key_refuses_everything(tmp_path):
    gh = FakeGitHub()
    for pubkey in (_placeholder(tmp_path), tmp_path / "absente.pub"):
        relay = _relay(gh, tmp_path, pubkey=pubkey)
        gh.requests.clear()
        with pytest.raises(OtaError, match="clé publique OTA"):
            await relay.refresh()
        assert [str(r.url) for r in gh.requests] == [f"{API}/repos/{REPO}/releases/latest"]
        assert relay.status().version == ""
    # Par défaut, le relais prend la clé livrée avec le serveur.
    assert OtaRelay(REPO, "", tmp_path / "x").pubkey_path == PACKAGED_PUBKEY


async def test_usb_key_needs_a_valid_public_key_now(tmp_path):
    """La clé USB est construite depuis une version installée : sa signature est revérifiée
    avec la clé publique du moment (provisoire ou changée : refus)."""
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    assert relay.key_zip() is not None
    (tmp_path / "ota-pubkey.pub").write_bytes(_pem(OTHER))
    with pytest.raises(OtaError, match="signature"):
        relay.key_zip()
    relay.pubkey_path = _placeholder(tmp_path)
    with pytest.raises(OtaError, match="PLACEHOLDER"):
        relay.key_zip()


async def test_manifest_version_must_match_the_release(tmp_path):
    gh = FakeGitHub()
    gh.set_release("v1.2.3")
    gh.version = "v1.2.4"  # étiquette différente du champ « version » signé
    with pytest.raises(OtaError, match="v1.2.4"):
        await _relay(gh, tmp_path).refresh()


async def test_previous_release_kept_for_a_grace_period(tmp_path):
    """Un lapin peut être en train de télécharger l'ancienne : elle reste 10 minutes."""
    now = [1_000_000.0]
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path, clock=lambda: now[0])
    await relay.refresh()
    gh.set_release("v1.3.0", {"zImage": b"k2", "rootfs.img.gz": b"r2", "usr.tar.gz": b"u2"})
    await relay.refresh()
    releases = tmp_path / "ota" / "releases"
    assert sorted(p.name for p in releases.iterdir()) == ["v1.2.3", "v1.3.0"]
    now[0] += 599
    await relay.refresh()  # même version : rien à télécharger, mais le ménage est fait
    assert sorted(p.name for p in releases.iterdir()) == ["v1.2.3", "v1.3.0"]
    now[0] += 2
    await relay.refresh()
    assert sorted(p.name for p in releases.iterdir()) == ["v1.3.0"]
    assert _app(relay).get("/ota/zImage").content == b"k2"


async def test_unsafe_manifest_is_refused(tmp_path):
    gh = FakeGitHub()
    gh.set_release("v1.2.3", {"../evil": b"x"})
    with pytest.raises(OtaError):
        await _relay(gh, tmp_path).refresh()
    gh.set_release("../v9", {"zImage": b"x"})
    with pytest.raises(OtaError):
        await _relay(gh, tmp_path).refresh()


async def test_usb_key_zip_checked_and_kept(tmp_path):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    key = relay.key_zip()
    assert key is not None and key.name == "LapinLibre-v1.2.3.zip"
    assert key.read_bytes() == gh.assets["LapinLibre-v1.2.3.zip"]
    # Archive différente de ce que dit le manifeste signé : la version n'est pas retenue,
    # même si la somme publiée à part concorde avec elle (R41 : seul le manifeste fait foi).
    gh.set_release("v2.0.0")
    bad = b"PK\x03\x04autre"
    gh.assets["LapinLibre-v2.0.0.zip"] = bad
    gh.assets["LapinLibre-v2.0.0.zip.sha256"] = f"{_sha(bad)}  LapinLibre-v2.0.0.zip\n".encode()
    with pytest.raises(OtaError, match="LapinLibre-v2.0.0.zip"):
        await relay.refresh()
    assert relay.key_zip() == key


async def test_separate_checksum_is_not_the_authority(tmp_path):
    gh = FakeGitHub()
    gh.assets["LapinLibre-v1.2.3.zip.sha256"] = b"0" * 64 + b"  LapinLibre-v1.2.3.zip\n"
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    assert relay.key_zip().read_bytes() == gh.assets["LapinLibre-v1.2.3.zip"]


async def test_key_zip_not_in_manifest_is_ignored(tmp_path):
    gh = FakeGitHub(key=False)
    gh.assets["LapinLibre-v1.2.3.zip"] = b"PK\x03\x04hors-manifeste"
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    assert relay.key_zip() is None


async def test_manifest_file_named_key_sha256_is_kept(tmp_path):
    gh = FakeGitHub()
    gh.set_release("v1.2.3", {"zImage": b"k", "key.sha256": b"donnees"})
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    assert _app(relay).get("/ota/key.sha256").content == b"donnees"


def test_api_must_be_https(tmp_path):
    with pytest.raises(ValueError):
        OtaRelay(REPO, "t", tmp_path, api="http://api.github.com")


async def test_plain_http_redirect_is_refused(tmp_path):
    gh = FakeGitHub()
    gh.storage = "http://objects.example.com"
    relay = _relay(gh, tmp_path, token="github_pat_SECRET")
    with pytest.raises(OtaError, match="https"):
        await relay.refresh()
    assert all(r.url.scheme == "https" for r in gh.requests)
    assert relay.status().version == ""


async def test_github_errors_are_reported(tmp_path):
    gh = FakeGitHub()
    gh.status = 404
    relay = _relay(gh, tmp_path)
    with pytest.raises(OtaError, match="404"):
        await relay.refresh()
    assert relay.status().error
    gh.status, gh.fail_network = 200, True
    with pytest.raises(OtaError, match="GitHub"):
        await relay.refresh()


async def test_release_survives_restart(tmp_path):
    gh = FakeGitHub()
    await _relay(gh, tmp_path).refresh()
    again = _relay(FakeGitHub(), tmp_path)
    assert again.status().version == "v1.2.3"
    assert _app(again).get("/ota/manifest.json").content == gh.manifest


def test_from_settings():
    s = Settings(ota=OtaConfig(repo="moi/fork", token="t", dir="/tmp/x",
                               pubkey_path="/etc/lapinou/ota.pub"))
    relay = OtaRelay.from_settings(s)
    assert (relay.repo, relay.token, str(relay.dir)) == ("moi/fork", "t", "/tmp/x")
    assert str(relay.pubkey_path) == "/etc/lapinou/ota.pub"
    assert OtaRelay.from_settings(Settings()).pubkey_path == PACKAGED_PUBKEY
    assert PACKAGED_PUBKEY.name == "ota-pubkey.pub"  # R42 amendée : extension .pub


# --- Rapport de correction 3 --------------------------------------------------------------


async def _switched(tmp_path, now):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path, clock=lambda: now[0])
    await relay.refresh()
    gh.set_release("v1.3.0", {"zImage": b"k2", "rootfs.img.gz": b"r2", "usr.tar.gz": b"u2"})
    await relay.refresh()
    return relay


async def test_grace_period_ends_at_serve_time_without_refresh(tmp_path):
    """Le délai est vérifié à chaque requête : pas besoin d'une nouvelle vérification GitHub
    (qui peut ne jamais venir) pour que l'ancienne version cesse d'être servie."""
    now = [1_000_000.0]
    relay = await _switched(tmp_path, now)
    client = _app(relay)
    now[0] += 600
    assert client.get("/ota/v1.2.3/zImage").content == b"K" * 1000
    now[0] += 1
    assert client.get("/ota/v1.2.3/zImage").status_code == 404
    assert client.get("/ota/v1.2.3/manifest.json").status_code == 404
    assert client.get("/ota/v1.3.0/zImage").content == b"k2"
    assert (tmp_path / "ota" / "releases" / "v1.2.3").is_dir()  # le ménage viendra plus tard


async def test_retire_time_is_recorded_and_survives_restart(tmp_path):
    now = [1_000_000.0]
    await _switched(tmp_path, now)
    marker = tmp_path / "ota" / "releases" / "v1.2.3" / ".retired"
    assert float(marker.read_text()) == 1_000_000.0
    again = _relay(FakeGitHub(), tmp_path, clock=lambda: now[0])
    now[0] += 300
    assert _app(again).get("/ota/v1.2.3/zImage").status_code == 200
    now[0] += 301
    assert _app(again).get("/ota/v1.2.3/zImage").status_code == 404
    # Le marqueur n'est pas un fichier servi.
    assert _app(again).get("/ota/v1.3.0/.retired").status_code == 404


async def test_leftover_release_without_retire_time_is_not_served(tmp_path):
    now = [1_000_000.0]
    relay = await _switched(tmp_path, now)
    (tmp_path / "ota" / "releases" / "v1.2.3" / ".retired").unlink()
    assert _app(relay).get("/ota/v1.2.3/zImage").status_code == 404
    await relay.refresh()
    assert not (tmp_path / "ota" / "releases" / "v1.2.3").exists()


async def test_usb_key_archive_rechecked_on_disk(tmp_path):
    gh = FakeGitHub()
    relay = _relay(gh, tmp_path)
    await relay.refresh()
    key = relay.key_zip()
    key.write_bytes(b"PK\x03\x04modifiee")
    with pytest.raises(OtaError, match="a changé"):
        relay.key_zip()


async def test_unsupported_public_key_type_is_a_clear_error(tmp_path, monkeypatch):
    from cryptography.exceptions import UnsupportedAlgorithm

    from lapinou import ota

    def unsupported(data):
        raise UnsupportedAlgorithm("courbe inconnue")

    monkeypatch.setattr(ota, "load_pem_public_key", unsupported)
    relay = _relay(FakeGitHub(), tmp_path)
    with pytest.raises(OtaError, match="clé publique OTA illisible"):
        await relay.refresh()
