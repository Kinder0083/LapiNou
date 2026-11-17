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
un envoi est borné (MAX_UPLOAD), écrit sur le disque au fil de l'eau, au plus MAX_UPLOADS_AT_ONCE
à la fois (sinon 503) ; seuls les KEEP_UPLOADS derniers sont gardés, KEEP_BYTES en tout ; deux
envois de la même seconde ne s'écrasent pas. Un compte rendu est mis sur une ligne (retours à
la ligne échappés) et coupé à MAX_LOG_LINE.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from ..rabbit.platform import app_server

MAX_UPLOAD = 32 * 1024 * 1024
MAX_UPLOADS_AT_ONCE = 2
KEEP_UPLOADS = 200
KEEP_BYTES = 512 * 1024 * 1024
MAX_LOG = 64 * 1024  # corps de POST /log lu au plus
MAX_LOG_LINE = 1024  # ce qui en est gardé (AppReports : 1000 lignes, donc ~1 Mio au plus)
DEFAULT_LANG = "fr-FR"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_PART = ".part-"  # envoi en cours d'écriture (ignoré par le ménage tant qu'il est récent)
PART_MAX_AGE_S = 3600  # un .part plus vieux est un reste (serveur arrêté pendant un envoi)
TEXT = "text/plain; charset=UTF-8"


_ESCAPES = {"\\": "\\\\", "\r": "\\r", "\n": "\\n"}


def _escape(char: str) -> str:
    """Un caractère de contrôle (Cc) ou un séparateur de ligne ou de paragraphe (Zl, Zp) en
    \\xNN ou \\uNNNN : rien de ce qu'envoie une application ne coupe ni ne déguise une ligne
    du journal."""
    if char in _ESCAPES:
        return _ESCAPES[char]
    if unicodedata.category(char) in ("Cc", "Zl", "Zp"):
        code = ord(char)
        return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"
    return char


def one_line(text: str) -> str:
    """Un compte rendu sur une ligne (contrôles et séparateurs échappés), coupé à
    MAX_LOG_LINE."""
    return "".join(_escape(c) for c in text)[:MAX_LOG_LINE]


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
        self._uploading = 0

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

    def void_grammar(self) -> str:
        """Une grammaire sans alternative ($VOID)."""
        return _NoApps(self.lang).grammar_text()

    def zip(self, key: str) -> bytes | None:
        apps = self._apps()
        if apps is None or key not in apps.keys():
            return None
        return apps.zip(key)

    def grammar_source(self, url: str) -> str | None:
        """La grammaire de lancement nommée par URL (`…/asr/grammar/<id>.abnf`), ou None."""
        return self.grammar_text() if re.search(r"/asr/grammar/[^/]+\.abnf$", url) else None

    def report(self, text: str) -> None:
        self.reports.add(one_line(text))

    # ---- envois ----

    def begin_upload(self) -> bool:
        """Une place pour un envoi (False : trop d'envois en cours)."""
        with self._lock:
            if self._uploading >= MAX_UPLOADS_AT_ONCE:
                return False
            self._uploading += 1
            return True

    def end_upload(self) -> None:
        with self._lock:
            self._uploading -= 1

    def part_file(self) -> tuple[int, Path]:
        """Un fichier temporaire dans le dossier des envois (rangé ensuite par `store`)."""
        assert self.uploads is not None
        self.uploads.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=_PART, dir=self.uploads)
        return fd, Path(name)

    def store(self, name: str, ctype: str, part: Path) -> list[str]:
        """Range l'envoi écrit dans `part` (corps brut, ou une pièce par fichier d'un multipart)
        et retire `part` ; ce qui a été rangé, pour le journal."""
        assert self.uploads is not None
        saved: list[str] = []
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            with self._lock:
                if ctype.startswith("multipart/form-data"):
                    msg = email.message_from_bytes(
                        b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\n\r\n"
                        + part.read_bytes(), policy=email.policy.HTTP)
                    for piece in msg.iter_parts() if msg.is_multipart() else ():
                        field = one_line(str(piece.get_param("name", header="content-disposition")
                                             or "field"))[:100]
                        data = piece.get_payload(decode=True) or b""
                        if piece.get_filename():
                            safe = _SAFE.sub("_", piece.get_filename())[-100:]
                            target = self._target(f"{stamp}-{name}-{safe}")
                            target.write_bytes(data)
                            saved.append(f"{field}={target.name} ({len(data)} bytes)")
                        else:
                            saved.append(f"{field}=({len(data)} bytes)")
                else:
                    target = self._target(f"{stamp}-{name}.bin")
                    size = part.stat().st_size
                    os.replace(part, target)
                    saved.append(f"{target.name} ({size} bytes, {ctype or 'no type'})")
                self._prune()
        finally:
            part.unlink(missing_ok=True)
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
        """Garde les KEEP_UPLOADS envois les plus récents, KEEP_BYTES au plus en tout."""
        assert self.uploads is not None
        now = time.time()
        files = []
        for p in self.uploads.iterdir():
            if not p.is_file():
                continue
            st = p.stat()
            if p.name.startswith(_PART):
                if now - st.st_mtime > PART_MAX_AGE_S:
                    p.unlink(missing_ok=True)  # reste d'un envoi interrompu
                continue
            files.append((p, st))
        files.sort(key=lambda f: (f[1].st_mtime_ns, f[0].name), reverse=True)
        total = 0
        for i, (path, st) in enumerate(files):
            total += st.st_size
            if i >= KEEP_UPLOADS or total > KEEP_BYTES:
                path.unlink(missing_ok=True)


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
        app_server.log(f"http: /file/{one_line(key)}/{one_line(version)}/get: not found")
        raise HTTPException(404)
    app_server.log(f"http: sending {one_line(key)} ({len(data)} bytes)")
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
    _host(request).report(" ".join(params.get("msg", ["(no msg)"])))
    return PlainTextResponse("OK", media_type=TEXT)


def _too_large(request: Request, limit: int) -> bool:
    declared = request.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > limit


@router.post("/log")
async def log_post(request: Request) -> PlainTextResponse:
    if _too_large(request, MAX_LOG):
        raise HTTPException(413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_LOG:
            raise HTTPException(413)
    _host(request).report(body.decode(errors="replace"))
    return PlainTextResponse("OK", media_type=TEXT)


@router.post("/upload")
@router.post("/upload/{name:path}")
async def upload(request: Request, name: str = "") -> PlainTextResponse:
    host = _host(request)
    if host.uploads is None:
        raise HTTPException(404)
    if _too_large(request, MAX_UPLOAD):
        raise HTTPException(413)
    if not host.begin_upload():
        raise HTTPException(503, "trop d'envois en cours")
    try:
        fd, part = await asyncio.to_thread(host.part_file)
        try:
            size = 0
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413)
                    await asyncio.to_thread(f.write, chunk)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        safe = _SAFE.sub("_", name.strip("/"))[:100] or "upload"
        saved = await asyncio.to_thread(host.store, safe,
                                        request.headers.get("content-type", ""), part)
    finally:
        host.end_upload()
    app_server.log(f"http: upload /upload/{safe}: {', '.join(saved) or 'nothing'}")
    return PlainTextResponse("OK", media_type=TEXT)
