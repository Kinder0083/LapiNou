# SPDX-License-Identifier: MIT
"""Endpoints LapinLibre des applications, sur le port 8080 (`url_app` des lapins).

Même contrat HTTP que `HttpHandler` de tools/app-server/app_server.py, avec sa classe `Apps`
importée et non copiée (R4) :

- `GET /file/<apiKey>/<version>/get` : l'application hébergée, en zip (sans `config.bin`) ;
- `GET /asr/grammar/<id>.abnf` : la grammaire de lancement du contrôleur (ABNF) ;
- `GET /log?msg=…` et `POST /log` : le compte rendu d'une application, dans le journal ;
- `POST /upload` et `POST /upload/<NOM>` : ce qu'une application envoie (photo de
  `webcam.photo(url)`, enregistrement, formulaire), rangé dans le dossier des envois ;
- `GET /text` : une page de test pour `http.get`.

Comme chez LapinLibre, rien de tout cela ne demande de mot de passe (réseau local). En plus :
un envoi est borné (MAX_UPLOAD), seuls les KEEP_UPLOADS derniers sont gardés, et deux envois
de la même seconde ne s'écrasent pas.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import re
import threading
import time
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from ..rabbit.platform import app_server

MAX_UPLOAD = 32 * 1024 * 1024
MAX_LOG = 64 * 1024
KEEP_UPLOADS = 200
DEFAULT_LANG = "fr-FR"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
TEXT = "text/plain; charset=UTF-8"


class AppHost:
    """Le dossier des applications hébergées (un sous-dossier par application, comme
    `--apps` de LapinLibre) et celui des envois ; l'un ou l'autre peut manquer (None ou absent :
    aucune application, envois refusés)."""

    def __init__(self, folder: Path | str | None, uploads: Path | str | None,
                 lang: str = DEFAULT_LANG):
        self.folder = Path(folder) if folder else None
        self.uploads = Path(uploads) if uploads else None
        self.lang = lang
        self.reports = app_server.AppReports()
        self._lock = threading.Lock()

    def _apps(self) -> app_server.Apps | None:
        if self.folder is None or not self.folder.is_dir():
            return None
        apps = app_server.Apps(self.folder)
        apps.settings = {"lang": self.lang}
        return apps

    def keys(self) -> list[str]:
        apps = self._apps()
        return apps.keys() if apps else []

    def grammar_text(self) -> str:
        return (self._apps() or _NoApps(self.lang)).grammar_text()

    def zip(self, key: str) -> bytes | None:
        apps = self._apps()
        if apps is None or key not in apps.keys():
            return None
        return apps.zip(key)

    def grammar_source(self, url: str) -> str | None:
        """Pour asr_server.set_grammar_source : la grammaire de lancement nommée par URL."""
        return self.grammar_text() if re.search(r"/asr/grammar/[^/]+\.abnf$", url) else None

    def save(self, name: str, ctype: str, body: bytes) -> list[str]:
        """Range un envoi (corps brut, ou une pièce par fichier d'un multipart) ; ce qui a été
        rangé, pour le journal."""
        assert self.uploads is not None
        self.uploads.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with self._lock:
            if ctype.startswith("multipart/form-data"):
                msg = email.message_from_bytes(
                    b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\n\r\n" + body,
                    policy=email.policy.HTTP)
                parts = msg.iter_parts() if msg.is_multipart() else ()
                for part in parts:
                    field = part.get_param("name", header="content-disposition") or "field"
                    data = part.get_payload(decode=True) or b""
                    if part.get_filename():
                        safe = _SAFE.sub("_", part.get_filename())[-100:]
                        target = self._target(f"{stamp}-{name}-{safe}")
                        target.write_bytes(data)
                        saved.append(f"{field}={target.name} ({len(data)} bytes)")
                    else:
                        saved.append(f"{field}=({len(data)} bytes)")
            else:
                target = self._target(f"{stamp}-{name}.bin")
                target.write_bytes(body)
                saved.append(f"{target.name} ({len(body)} bytes, {ctype or 'no type'})")
            self._prune()
        return saved

    def _target(self, filename: str) -> Path:
        assert self.uploads is not None
        target = self.uploads / filename
        stem, dot, ext = filename.rpartition(".")
        n = 1
        while target.exists():
            target = self.uploads / (f"{stem}-{n}.{ext}" if dot else f"{filename}-{n}")
            n += 1
        return target

    def _prune(self) -> None:
        assert self.uploads is not None
        files = sorted((p for p in self.uploads.iterdir() if p.is_file()),
                       key=lambda p: (p.stat().st_mtime_ns, p.name))
        for old in files[:-KEEP_UPLOADS] if len(files) > KEEP_UPLOADS else ():
            old.unlink(missing_ok=True)


class _NoApps(app_server.Apps):
    """Aucune application : la grammaire de lancement vaut $VOID."""

    def __init__(self, lang: str):
        self.settings = {"lang": lang}

    def keys(self) -> list[str]:
        return []


router = APIRouter(include_in_schema=False)


def _host(request: Request) -> AppHost:
    return request.app.state.apps


@router.get("/file/{key}/{version}/get")
def app_zip(key: str, version: str, request: Request) -> Response:
    data = _host(request).zip(key)
    if data is None:
        app_server.log(f"http: /file/{key}/{version}/get: not found")
        raise HTTPException(404)
    app_server.log(f"http: sending {key} ({len(data)} bytes)")
    return Response(data, media_type="application/zip")


@router.get("/asr/grammar/{name}")
def grammar(name: str, request: Request) -> PlainTextResponse:
    if not name.endswith(".abnf"):
        raise HTTPException(404)
    return PlainTextResponse(_host(request).grammar_text(), media_type=TEXT)


@router.get("/text")
def text() -> PlainTextResponse:
    return PlainTextResponse("hello from the app server", media_type=TEXT)


@router.get("/log")
def log_get(request: Request) -> PlainTextResponse:
    params = urllib.parse.parse_qs(request.url.query)
    _host(request).reports.add(" ".join(params.get("msg", ["(no msg)"]))[:MAX_LOG])
    return PlainTextResponse("OK", media_type=TEXT)


async def _body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413)
    return bytes(body)


@router.post("/log")
async def log_post(request: Request) -> PlainTextResponse:
    body = await _body(request, MAX_LOG)
    _host(request).reports.add(body.decode(errors="replace"))
    return PlainTextResponse("OK", media_type=TEXT)


@router.post("/upload")
@router.post("/upload/{name:path}")
async def upload(request: Request, name: str = "") -> PlainTextResponse:
    host = _host(request)
    if host.uploads is None:
        raise HTTPException(404)
    body = await _body(request, MAX_UPLOAD)
    safe = _SAFE.sub("_", name.strip("/"))[:100] or "upload"
    saved = await asyncio.to_thread(host.save, safe, request.headers.get("content-type", ""),
                                    body)
    app_server.log(f"http: upload /upload/{safe}: {', '.join(saved) or 'nothing'}")
    return PlainTextResponse("OK", media_type=TEXT)
