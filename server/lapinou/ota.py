# SPDX-License-Identifier: MIT
"""Relais OTA : la dernière version GitHub du dépôt (R38), servie aux lapins du réseau local.

Le serveur lit les Releases du dépôt réglé (`[ota] repo`, Kinder0083/LapiNou par défaut), avec
le jeton de `secrets.toml` pour un dépôt privé (à grain fin, lecture seule de « Contents ») ; le
jeton ne part que vers api.github.com, jamais vers l'hôte de stockage où GitHub redirige les
téléchargements. Toutes les adresses GitHub sont en https, redirections comprises.

Signature (R42) : Ed25519 sur les octets exacts de `manifest.json`, signature brute de 64
octets publiée à part (`manifest.json.sig`). La clé publique est le fichier (contenu PEM, extension .pub) de
`[ota] pubkey_path` (par défaut `ota-pubkey.pub` livré avec le serveur). Le serveur vérifie la
signature **avant** de télécharger les fichiers et d'installer la version, donc avant toute
clé USB construite à partir d'elle ; clé absente ou provisoire (PLACEHOLDER) : refus.
Chaque fichier listé est ensuite vérifié (taille, SHA-256), l'archive de la clé USB comprise
(R41). Le champ `version` du manifeste doit être le nom de la version (étiquette GitHub).

Disposition : `<dir>/releases/<version>/` (manifest.json, manifest.json.sig, fichiers) et
`<dir>/current`. Une version remplacée reste servie au moins 10 minutes (GRACE_S).

Adresses pour les lapins (port 8080, docs/lapinou/ota.md) :
- `GET /ota/manifest.json` et `GET /ota/manifest.json.sig` : la version en service ;
- `GET /ota/<version>/<fichier>` : un fichier (ou manifest.json[.sig]) d'une version encore
  servie, la courante ou celle en sursis ; le lapin prend `<version>` dans le manifeste qu'il a
  vérifié, et reçoit donc toujours les fichiers de ce manifeste-là ;
- `GET /ota/<fichier>` : un fichier de la version en service (forme courte).
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
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response

from .config import Settings

log = logging.getLogger(__name__)

API = "https://api.github.com"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.json.sig"
PACKAGED_PUBKEY = Path(__file__).with_name("ota-pubkey.pub")
MAX_FILE = 300 * 1024 * 1024
GRACE_S = 600  # une version remplacée reste servie 10 minutes
FIRST_CHECK_S = 60  # première vérification périodique une minute après le démarrage
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


def parse_manifest(data: bytes) -> tuple[str, dict[str, dict[str, Any]]]:
    """(version, fichiers : nom → sha256, size) d'un manifeste ; OtaError s'il est invalide."""
    try:
        m = json.loads(data)
        files = m["files"]
        version = m["version"]
        ok = (isinstance(version, str) and _NAME.fullmatch(version) is not None
              and isinstance(files, dict) and bool(files))
    except (ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise OtaError("manifest.json invalide")
    for name, f in files.items():
        if (name in (MANIFEST, SIGNATURE) or not _NAME.fullmatch(name) or not isinstance(f, dict)
                or not _SHA.fullmatch(str(f.get("sha256", "")))
                or not isinstance(f.get("size"), int) or not 0 <= f["size"] <= MAX_FILE):
            raise OtaError(f"manifest.json invalide (fichier {name!r})")
    return version, files


def load_pubkey(path: Path) -> Ed25519PublicKey:
    """La clé publique OTA ; OtaError si elle manque, est provisoire ou n'est pas Ed25519."""
    try:
        data = path.read_bytes()
    except OSError:
        raise OtaError(f"clé publique OTA absente ({path}) : mises à jour refusées") from None
    if b"PLACEHOLDER" in data:
        raise OtaError("clé publique OTA provisoire (PLACEHOLDER) : mises à jour refusées tant "
                       "que la vraie clé n'est pas installée")
    try:
        key = load_pem_public_key(data)
    except ValueError:
        raise OtaError(f"clé publique OTA illisible ({path})") from None
    if not isinstance(key, Ed25519PublicKey):
        raise OtaError("la clé publique OTA doit être une clé Ed25519")
    return key


def verify_signature(key: Ed25519PublicKey, manifest: bytes, signature: bytes) -> None:
    if len(signature) != 64:
        raise OtaError("signature du manifeste invalide (64 octets attendus)")
    try:
        key.verify(signature, manifest)
    except InvalidSignature:
        raise OtaError("signature du manifeste fausse : version refusée") from None


class OtaRelay:
    def __init__(
        self,
        repo: str,
        token: str,
        directory: Path | str,
        *,
        pubkey_path: Path | str | None = None,
        client: httpx2.AsyncClient | None = None,
        api: str = API,
        check_hours: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not api.startswith("https://"):
            raise ValueError("l'API GitHub doit être en https")
        self.repo, self.token, self.dir = repo, token, Path(directory)
        self.pubkey_path = Path(pubkey_path) if pubkey_path else PACKAGED_PUBKEY
        self.api = api.rstrip("/")
        self.check_hours = check_hours
        self._clock = clock
        self._client = client
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._status = OtaStatus(repo=repo, version=self._current() or "")
        self._status.key_zip = self._key_name(self._status.version)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> OtaRelay:
        o = settings.ota
        return cls(o.repo, o.token, o.dir, pubkey_path=o.pubkey_path or None,
                   check_hours=o.check_hours, **kwargs)

    # --- État --------------------------------------------------------------------------

    def _release(self, version: str) -> Path | None:
        """Le dossier d'une version encore servie (courante ou en sursis), ou None."""
        if not _NAME.fullmatch(version):
            return None
        d = self.dir / "releases" / version
        return d if (d / MANIFEST).is_file() and (d / SIGNATURE).is_file() else None

    def _current(self) -> str | None:
        try:
            version = (self.dir / "current").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return version if self._release(version) is not None else None

    def _key_name(self, version: str) -> str:
        """L'archive de la clé USB listée par le manifeste signé de cette version, ou ""."""
        d = self._release(version) if version else None
        if d is None:
            return ""
        try:
            _, files = parse_manifest((d / MANIFEST).read_bytes())
        except (OSError, OtaError):
            return ""
        return next((n for n in sorted(files) if _KEY_ZIP.fullmatch(n)), "")

    def status(self) -> OtaStatus:
        return self._status

    def manifest_bytes(self) -> bytes | None:
        version = self._current()
        return None if version is None else (self.dir / "releases" / version / MANIFEST).read_bytes()

    def version_file(self, version: str, name: str) -> Path | None:
        """`name` (manifest.json, sa signature ou un fichier listé) d'une version encore
        servie, ou None."""
        d = self._release(version)
        if d is None or not _NAME.fullmatch(name):
            return None
        if name not in (MANIFEST, SIGNATURE):
            try:
                _, files = parse_manifest((d / MANIFEST).read_bytes())
            except (OSError, OtaError):
                return None
            if name not in files:
                return None
        path = d / name
        return path if path.is_file() else None

    def file_path(self, name: str) -> Path | None:
        """Un fichier de la version en service, ou None."""
        version = self._current()
        return None if version is None else self.version_file(version, name)

    def key_zip(self) -> Path | None:
        """L'archive de la clé USB de la version en service, sa signature revérifiée avec la clé
        publique actuelle ; None sinon (OtaError si la clé publique est absente ou provisoire)."""
        version = self._current()
        name = self._key_name(version or "")
        if not version or not name:
            return None
        d = self.dir / "releases" / version
        verify_signature(load_pubkey(self.pubkey_path), (d / MANIFEST).read_bytes(),
                         (d / SIGNATURE).read_bytes())
        return d / name

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
            if not url.startswith("https://"):  # jamais de jeton ni de fichier en clair
                raise OtaError(f"adresse non https refusée pour {asset['name']}")
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
            self._prune()
            if version == self._current():
                return
            key = load_pubkey(self.pubkey_path)  # sans clé valable : rien n'est téléchargé
            assets = {a["name"]: a for a in release.get("assets", [])
                      if isinstance(a, dict) and isinstance(a.get("name"), str)}
            for needed in (MANIFEST, SIGNATURE):
                if needed not in assets:
                    raise OtaError(f"la version {version} n'a pas de {needed}")
            self.dir.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=self.dir, prefix=".staging-"))
            try:
                await self._fetch(client, key, version, assets, staging)
                self._install(version, staging)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        finally:
            if own:
                await client.aclose()

    async def _fetch(self, client: httpx2.AsyncClient, key: Ed25519PublicKey, version: str,
                     assets: dict[str, dict[str, Any]], staging: Path) -> None:
        await self._download(client, assets[MANIFEST], staging / MANIFEST, limit=1 << 20)
        await self._download(client, assets[SIGNATURE], staging / SIGNATURE, limit=1024)
        manifest = (staging / MANIFEST).read_bytes()
        verify_signature(key, manifest, (staging / SIGNATURE).read_bytes())  # avant tout le reste
        listed_version, files = parse_manifest(manifest)
        if listed_version != version:
            raise OtaError(f"le manifeste dit {listed_version!r}, la version est {version!r}")
        for name, want in files.items():  # la clé USB comprise (R41)
            if name not in assets:
                raise OtaError(f"{name} manque dans la version {version}")
            path = staging / name
            await self._download(client, assets[name], path)
            if path.stat().st_size != want["size"] or _sha256(path) != want["sha256"]:
                raise OtaError(f"{name} : taille ou SHA-256 différents du manifeste")

    def _install(self, version: str, staging: Path) -> None:
        releases = self.dir / "releases"
        releases.mkdir(exist_ok=True)
        final = releases / version
        previous = self._current()
        if final.exists():  # jamais la version servie (elle a été écartée plus haut)
            shutil.rmtree(final)
        os.replace(staging, final)
        tmp = self.dir / ".current.new"
        tmp.write_text(version + "\n", encoding="utf-8")
        os.replace(tmp, self.dir / "current")
        if previous is not None:  # remplacée maintenant : servie encore GRACE_S
            now = self._clock()
            os.utime(releases / previous, (now, now))
        self._prune()
        self._status.version = version
        self._status.key_zip = self._key_name(version)
        log.info("relais OTA : version %s servie aux lapins", version)

    def _prune(self) -> None:
        """Efface les versions remplacées depuis plus de GRACE_S (jamais la version servie)."""
        releases = self.dir / "releases"
        if not releases.is_dir():
            return
        current, now = self._current(), self._clock()
        for old in releases.iterdir():
            if old.name != current and now - old.stat().st_mtime > GRACE_S:
                shutil.rmtree(old, ignore_errors=True)

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
        await asyncio.sleep(FIRST_CHECK_S)
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


def _serve(path: Path | None, name: str) -> Response:
    if path is None:
        raise HTTPException(404)
    if name == MANIFEST:
        return Response(path.read_bytes(), media_type="application/json",
                        headers={"Cache-Control": "no-cache"})
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "no-cache" if name == SIGNATURE else "max-age=600"})


@router.get("/ota/{name}")
def get_current(name: str, request: Request) -> Response:
    """manifest.json, sa signature ou un fichier de la version en service."""
    return _serve(_relay(request).file_path(name), name)


@router.get("/ota/{version}/{name}")
def get_versioned(version: str, name: str, request: Request) -> Response:
    """Un fichier d'une version encore servie (la courante, ou la précédente en sursis)."""
    return _serve(_relay(request).version_file(version, name), name)
