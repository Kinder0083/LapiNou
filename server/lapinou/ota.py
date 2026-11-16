# SPDX-License-Identifier: MIT
"""Relais OTA : la dernière version GitHub du dépôt (R38), servie aux lapins du réseau local.

Le serveur lit les Releases du dépôt réglé (`[ota] repo`, Kinder0083/LapiNou par défaut), avec
le jeton de `secrets.toml` pour un dépôt privé (à grain fin, lecture seule de « Contents ») ; le
jeton ne part que vers api.github.com, jamais vers l'hôte de stockage où GitHub redirige les
téléchargements. Une version comprend `manifest.json` (signé, format de `lapinou-ota`, Tâche 11)
et les fichiers qu'il liste ; chacun est vérifié (taille, SHA-256) avant que la version ne
remplace la précédente, d'un bloc. La signature, elle, est vérifiée par le lapin : le manifeste
lui est servi **octet pour octet** tel que publié. L'archive de la clé USB de la version
(`LapinLibre-<v>.zip` ou `lapinou-key-<v>.zip`, avec sa somme `.sha256`) est gardée aussi.

Disposition : `<dir>/releases/<version>/` et `<dir>/current` (le nom de la version servie).
Les lapins lisent `GET /ota/manifest.json` puis `GET /ota/<fichier>` (port 8080).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .config import Settings

log = logging.getLogger(__name__)

API = "https://api.github.com"
MANIFEST = "manifest.json"
MAX_FILE = 300 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
_KEY_ZIP = re.compile(r"(?:LapinLibre|lapinou-key)-.+\.zip")
_SHA = re.compile(r"[0-9a-f]{64}")


class OtaError(RuntimeError):
    """Échec du relais (message en français, sans le jeton)."""


@dataclass
class OtaStatus:
    version: str = ""  # version servie aux lapins (vide : aucune)
    repo: str = ""
    checked_at: str = ""
    error: str = ""
    latest: str = ""  # dernière version vue sur GitHub
    key_zip: str = ""

    def as_dict(self) -> dict[str, str]:
        return dict(self.__dict__)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def parse_manifest(data: bytes) -> dict[str, dict[str, Any]]:
    """Les fichiers d'un manifeste (nom → sha256, size) ; OtaError s'il est invalide."""
    try:
        m = json.loads(data)
        files = m["files"]
        ok = (isinstance(m.get("version"), str) and isinstance(m.get("signature"), str)
              and isinstance(files, dict) and files)
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise OtaError("manifest.json invalide")
    for name, f in files.items():
        if (name == MANIFEST or not _NAME.fullmatch(name) or not isinstance(f, dict)
                or not _SHA.fullmatch(str(f.get("sha256", "")))
                or not isinstance(f.get("size"), int) or not 0 <= f["size"] <= MAX_FILE):
            raise OtaError(f"manifest.json invalide (fichier {name!r})")
    return files


class OtaRelay:
    def __init__(
        self,
        repo: str,
        token: str,
        directory: Path | str,
        *,
        client: httpx2.AsyncClient | None = None,
        api: str = API,
        check_hours: float = 0.0,
    ) -> None:
        self.repo, self.token, self.dir = repo, token, Path(directory)
        self.api = api.rstrip("/")
        self.check_hours = check_hours
        self._client = client
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._status = OtaStatus(repo=repo, version=self._current() or "")
        self._status.key_zip = self._key_name(self._status.version)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> OtaRelay:
        o = settings.ota
        return cls(o.repo, o.token, o.dir, check_hours=o.check_hours, **kwargs)

    # --- État --------------------------------------------------------------------------

    def _current(self) -> str | None:
        try:
            version = (self.dir / "current").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if _NAME.fullmatch(version) and (self.dir / "releases" / version / MANIFEST).is_file():
            return version
        return None

    def _key_name(self, version: str) -> str:
        if not version:
            return ""
        d = self.dir / "releases" / version
        return next((p.name for p in sorted(d.iterdir()) if _KEY_ZIP.fullmatch(p.name)), "")

    def status(self) -> OtaStatus:
        return self._status

    def manifest_bytes(self) -> bytes | None:
        version = self._current()
        if version is None:
            return None
        return (self.dir / "releases" / version / MANIFEST).read_bytes()

    def file_path(self, name: str) -> Path | None:
        """Un fichier listé par le manifeste servi, ou None."""
        data = self.manifest_bytes()
        if data is None:
            return None
        try:
            files = parse_manifest(data)
        except OtaError:
            return None
        if name not in files:
            return None
        path = self.dir / "releases" / self._current() / name  # type: ignore[operator]
        return path if path.is_file() else None

    def key_zip(self) -> Path | None:
        version = self._current()
        name = self._key_name(version or "")
        return self.dir / "releases" / version / name if version and name else None

    # --- GitHub ------------------------------------------------------------------------

    def _headers(self, accept: str) -> dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "lapinou"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _latest(self, client: httpx2.AsyncClient) -> dict[str, Any]:
        url = f"{self.api}/repos/{self.repo}/releases/latest"
        r = await client.get(url, headers=self._headers("application/vnd.github+json"))
        if r.status_code != 200:
            hint = " (dépôt privé : jeton absent ou sans accès ?)" if r.status_code == 404 else ""
            raise OtaError(f"GitHub a répondu {r.status_code} pour {self.repo}{hint}")
        return r.json()

    async def _download(self, client: httpx2.AsyncClient, asset: dict[str, Any],
                        target: Path, limit: int = MAX_FILE) -> None:
        """Un fichier de la version, écrit au fil de l'eau : l'API (avec le jeton) redirige
        vers le stockage de GitHub, joint sans le jeton."""
        url, api_host = str(asset["url"]), httpx2.URL(self.api).host
        for _ in range(5):
            if not url.startswith("https://") and httpx2.URL(url).host != api_host:
                raise OtaError(f"redirection refusée pour {asset['name']}")
            headers = (self._headers("application/octet-stream")
                       if httpx2.URL(url).host == api_host
                       else {"Accept": "application/octet-stream", "User-Agent": "lapinou"})
            async with client.stream("GET", url, headers=headers, follow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    url = r.headers.get("location", "")
                    continue
                if r.status_code != 200:
                    raise OtaError(f"téléchargement de {asset['name']} : HTTP {r.status_code}")
                size = 0
                with target.open("wb") as f:
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise OtaError(f"{asset['name']} est trop gros")
                        f.write(chunk)
                return
        raise OtaError(f"trop de redirections pour {asset['name']}")

    async def refresh(self) -> OtaStatus:
        """Cherche la dernière version ; si elle est nouvelle, la télécharge, la vérifie et la
        sert à la place de l'ancienne. OtaError (et `status().error`) en cas d'échec : l'ancienne
        version reste servie."""
        async with self._lock:
            self._status.checked_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
            try:
                await self._refresh()
                self._status.error = ""
            except OtaError as e:
                self._status.error = str(e)
                log.warning("relais OTA : %s", e)
                raise
            except (httpx2.HTTPError, OSError, ValueError) as e:
                self._status.error = f"GitHub injoignable ou réponse invalide ({type(e).__name__})"
                log.warning("relais OTA : %s", e)
                raise OtaError(self._status.error) from e
            return self._status

    async def _refresh(self) -> None:
        own = self._client is None
        client = self._client or httpx2.AsyncClient(timeout=60)
        try:
            release = await self._latest(client)
            version = str(release.get("tag_name", ""))
            if not _NAME.fullmatch(version):
                raise OtaError(f"nom de version invalide : {version[:40]!r}")
            self._status.latest = version
            if version == self._current():
                return
            assets = {a["name"]: a for a in release.get("assets", [])
                      if isinstance(a, dict) and isinstance(a.get("name"), str)}
            if MANIFEST not in assets:
                raise OtaError(f"la version {version} n'a pas de manifest.json")
            self.dir.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=self.dir, prefix=".staging-"))
            try:
                await self._fetch(client, version, assets, staging)
                self._install(version, staging)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        finally:
            if own:
                await client.aclose()

    async def _fetch(self, client: httpx2.AsyncClient, version: str,
                     assets: dict[str, dict[str, Any]], staging: Path) -> None:
        await self._download(client, assets[MANIFEST], staging / MANIFEST, limit=1 << 20)
        files = parse_manifest((staging / MANIFEST).read_bytes())
        for name, want in files.items():
            if name not in assets:
                raise OtaError(f"{name} manque dans la version {version}")
            path = staging / name
            await self._download(client, assets[name], path)
            if path.stat().st_size != want["size"] or _sha256(path) != want["sha256"]:
                raise OtaError(f"{name} : taille ou SHA-256 différents du manifeste")
        key = next((n for n in sorted(assets) if _KEY_ZIP.fullmatch(n)), None)
        if key is not None and _NAME.fullmatch(key) and key + ".sha256" in assets:
            await self._download(client, assets[key + ".sha256"], staging / "key.sha256",
                                 limit=4096)
            want = (staging / "key.sha256").read_text(encoding="utf-8").split()[:1]
            (staging / "key.sha256").unlink()
            await self._download(client, assets[key], staging / key)
            if want != [_sha256(staging / key)]:
                raise OtaError(f"{key} : SHA-256 différent de sa somme publiée")

    def _install(self, version: str, staging: Path) -> None:
        releases = self.dir / "releases"
        releases.mkdir(exist_ok=True)
        final = releases / version
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)
        tmp = self.dir / ".current.new"
        tmp.write_text(version + "\n", encoding="utf-8")
        os.replace(tmp, self.dir / "current")
        for old in releases.iterdir():
            if old.name != version:
                shutil.rmtree(old, ignore_errors=True)
        self._status.version = version
        self._status.key_zip = self._key_name(version)
        log.info("relais OTA : version %s servie aux lapins", version)

    # --- Vérification périodique --------------------------------------------------------

    async def start(self) -> None:
        if self.check_hours > 0 and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="ota-relay")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            with contextlib.suppress(OtaError):
                await self.refresh()
            await asyncio.sleep(self.check_hours * 3600)


router = APIRouter()


def _relay(request: Request) -> OtaRelay:
    relay = getattr(request.app.state, "ota", None)
    if relay is None:
        raise HTTPException(404)
    return relay


@router.get("/ota/manifest.json")
def get_manifest(request: Request) -> Response:
    data = _relay(request).manifest_bytes()
    if data is None:
        raise HTTPException(404, "aucune version téléchargée")
    return Response(data, media_type="application/json",
                    headers={"Cache-Control": "no-cache"})


@router.get("/ota/{name}")
def get_file(name: str, request: Request) -> FileResponse:
    path = _relay(request).file_path(name) if _NAME.fullmatch(name) else None
    if path is None:
        raise HTTPException(404)
    return FileResponse(path, media_type="application/octet-stream")
