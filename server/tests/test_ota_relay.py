# SPDX-License-Identifier: MIT
"""Relais OTA : versions GitHub (faux GitHub, aucun appel réel) servies aux lapins."""

import hashlib
import json

import httpx2
import pytest
from fastapi.testclient import TestClient

from lapinou.config import OtaConfig, Settings
from lapinou.ota import OtaError, OtaRelay

API = "https://api.github.com"
REPO = "Kinder0083/LapiNou"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeGitHub:
    """Une dernière version publiée, ses fichiers servis par un hôte de stockage à part (comme
    GitHub : l'API renvoie une redirection vers objects.githubusercontent.com)."""

    def __init__(self, version="v1.2.3", files=None, key=True):
        self.requests: list[httpx2.Request] = []
        self.status = 200
        self.fail_network = False
        self.set_release(version, files, key)

    def set_release(self, version, files=None, key=True, manifest_files=None):
        files = files or {"zImage": b"K" * 1000, "rootfs.img.gz": b"R" * 3000,
                          "usr.tar.gz": b"U" * 5000}
        listed = manifest_files or {n: {"sha256": _sha(d), "size": len(d)}
                                    for n, d in files.items()}
        # Mise en forme et ordre volontairement inhabituels : servis tels quels, octet pour octet.
        self.manifest = (
            '{"signature": "c2lnbmF0dXJl",\n  "version":"%s", "files": %s }\n'
            % (version, json.dumps(listed, indent=3))
        ).encode()
        self.assets = {"manifest.json": self.manifest, **files}
        if key:
            zip_name = f"LapinLibre-{version}.zip"
            self.assets[zip_name] = b"PK\x03\x04cle"
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
                "location": f"https://objects.example.com/blob/{i}?sig=abc"})
        if url.startswith("https://objects.example.com/blob/"):
            i = int(url.split("/blob/")[1].split("?")[0])
            return httpx2.Response(200, content=list(self.assets.values())[i])
        return httpx2.Response(404)

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))


def _relay(gh, tmp_path, token=""):
    return OtaRelay(REPO, token, tmp_path / "ota", client=gh.client())


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
    assert sorted(p.name for p in (tmp_path / "ota" / "releases").iterdir()) == ["v1.3.0"]


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
    # Somme fausse : la version n'est pas retenue.
    gh.set_release("v2.0.0")
    gh.assets["LapinLibre-v2.0.0.zip.sha256"] = b"0" * 64 + b"  LapinLibre-v2.0.0.zip\n"
    with pytest.raises(OtaError, match="LapinLibre-v2.0.0.zip"):
        await relay.refresh()
    assert relay.key_zip() == key


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
    s = Settings(ota=OtaConfig(repo="moi/fork", token="t", dir="/tmp/x"))
    relay = OtaRelay.from_settings(s)
    assert (relay.repo, relay.token, str(relay.dir)) == ("moi/fork", "t", "/tmp/x")
