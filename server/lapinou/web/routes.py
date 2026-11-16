# SPDX-License-Identifier: MIT
"""Page de réglages du serveur (port 8080) : la page (static/) et son API JSON sous /api.

Toute l'API demande une session admin (voir auth.py), sauf l'état de connexion, la connexion
et la création du premier mot de passe. Les secrets ne sont jamais renvoyés : la page ne voit
que s'ils sont réglés (`secrets`), et les change par `PUT /api/secrets`. Toute requête qui
modifie porte l'en-tête `X-LapiNou: 1` (une autre page ne peut pas l'ajouter sans
autorisation CORS : protection contre le CSRF, en plus du cookie SameSite=Strict).

Les commandes lancées par le serveur (`[update]`, `ha.mcp_command`) et le dossier OTA ne se
changent que dans config.toml, pas par la page : une session volée ne doit pas pouvoir faire
exécuter n'importe quoi.
"""

from __future__ import annotations

import asyncio
import copy
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError
from starlette.background import BackgroundTask

from .. import __version__
from ..config import SECRET_FIELDS, Settings, SettingsStore, _merge, _split_secrets
from . import auth as auth_mod

STATIC = Path(__file__).with_name("static")
CSRF_HEADER = "x-lapinou"
CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
# (section, clé) que la page ne change pas (None : toute la section).
READ_ONLY = (("admin", None), ("update", None), ("ha", "mcp_command"), ("ota", "dir"))
PAGE_SECRETS = [f"{s}.{k}" for s, k in SECRET_FIELDS if s != "admin"]


@dataclass
class WebContext:
    """Ce que la page montre et change. Un élément absent (None) : 503 sur ses routes."""

    store: SettingsStore
    db: Any = None
    memory: Any = None
    timers: Any = None
    budget: Any = None
    log: Any = None
    registry: Any = None
    voice: Any = None
    ota: Any = None
    updater: Any = None
    auth: auth_mod.Auth = field(init=False)

    def __post_init__(self) -> None:
        self.auth = auth_mod.Auth(self.store)


def _ctx(request: Request) -> WebContext:
    return request.app.state.web


def _need(obj: Any, what: str) -> Any:
    if obj is None:
        raise HTTPException(503, f"{what} indisponible sur ce serveur")
    return obj


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def require_login(request: Request) -> None:
    if not _ctx(request).auth.check(request.cookies.get(auth_mod.COOKIE)):
        raise HTTPException(401, "connexion requise")


def _set_session(response: Response, token: str, request: Request) -> None:
    response.set_cookie(auth_mod.COOKIE, token, max_age=auth_mod.SESSION_TTL_S, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https", path="/")


# --- Réglages sans secrets ----------------------------------------------------------------


def secrets_status(s: Settings) -> dict[str, bool]:
    status = {name: bool(getattr(getattr(s, name.split(".")[0]), name.split(".")[1]))
              for name in PAGE_SECRETS}
    for pname, prov in s.brain.providers.items():
        status[f"brain.providers.{pname}.api_key"] = bool(prov.api_key)
    return status


def public_view(s: Settings) -> dict[str, Any]:
    public, _ = _split_secrets(s.model_dump(mode="json"))
    public.pop("admin", None)
    return {
        "settings": public,
        "secrets": secrets_status(s),
        "choices": {"wake_method": ["asr", "both"], "tts_engine": ["kokoro", "piper"],
                    "provider_kind": ["anthropic", "openai"]},
    }


def _strip(body: dict[str, Any]) -> None:
    """Retire du corps les secrets et ce que la page ne change pas."""
    for section, key in (*SECRET_FIELDS, *READ_ONLY):
        if key is None:
            body.pop(section, None)
        elif isinstance(body.get(section), dict):
            body[section].pop(key, None)


def updated_settings(current: Settings, body: dict[str, Any]) -> dict[str, Any]:
    """Les réglages complets après les changements de la page (secrets gardés)."""
    body = copy.deepcopy(body)
    _strip(body)
    data = current.model_dump(mode="python")
    brain = body.get("brain")
    if isinstance(brain, dict) and "providers" in brain:
        providers = brain.pop("providers")
        if not isinstance(providers, dict) or not all(
                isinstance(p, dict) for p in providers.values()):
            raise HTTPException(422, "brain.providers : un objet par fournisseur")
        old = data["brain"]["providers"]
        data["brain"]["providers"] = {
            name: {**old.get(name, {}), **{k: v for k, v in p.items() if k != "api_key"}}
            for name, p in providers.items()
        }
    if "rabbits" in body:  # remplacée d'un bloc : un lapin retiré disparaît
        data["rabbits"] = body.pop("rabbits")
    return _merge(data, body)


def _validation_detail(e: ValidationError) -> list[dict[str, str]]:
    return [{"field": ".".join(str(x) for x in err["loc"]), "message": err["msg"]}
            for err in e.errors()]


# --- Corps des requêtes -------------------------------------------------------------------


class PasswordBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    old: str
    new: str


class SecretBody(BaseModel):
    name: str
    value: str


class TextBody(BaseModel):
    text: str


class UsbKeyBody(BaseModel):
    ssid: str
    password: str
    ip: str = ""
    netmask: str = "255.255.255.0"
    gateway: str = ""
    dns: str = ""
    ssh_pubkey: str = ""
    web_password: str = ""
    hostname: str = ""


# --- Routes ouvertes ----------------------------------------------------------------------

public = APIRouter(prefix="/api")


@public.get("/session")
def session(request: Request) -> dict[str, bool]:
    ctx = _ctx(request)
    return {"password_set": ctx.auth.password_set(),
            "logged_in": ctx.auth.check(request.cookies.get(auth_mod.COOKIE))}


@public.post("/setup")
async def setup(body: PasswordBody, request: Request, response: Response) -> dict[str, bool]:
    try:
        token = await asyncio.to_thread(_ctx(request).auth.setup, body.password)
    except PermissionError:
        raise HTTPException(409, "un mot de passe existe déjà : connectez-vous") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    _set_session(response, token, request)
    return {"ok": True}


@public.post("/login")
async def login(body: PasswordBody, request: Request, response: Response) -> dict[str, bool]:
    try:
        token = await asyncio.to_thread(_ctx(request).auth.login, body.password,
                                        _client(request))
    except auth_mod.Locked as e:
        raise HTTPException(429, "trop d'essais : réessayez dans une minute",
                            headers={"Retry-After": str(e.retry_after)}) from None
    if token is None:
        raise HTTPException(401, "mot de passe incorrect")
    _set_session(response, token, request)
    return {"ok": True}


@public.post("/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    _ctx(request).auth.logout(request.cookies.get(auth_mod.COOKIE))
    response.delete_cookie(auth_mod.COOKIE, path="/")
    return {"ok": True}


# --- Routes protégées ---------------------------------------------------------------------

router = APIRouter(prefix="/api", dependencies=[Depends(require_login)])


@router.post("/password")
async def change_password(body: ChangePasswordBody, request: Request,
                          response: Response) -> dict[str, bool]:
    a = _ctx(request).auth
    try:
        ok = await asyncio.to_thread(a.change, body.old, body.new, _client(request))
    except auth_mod.Locked:
        raise HTTPException(429, "trop d'essais : réessayez dans une minute") from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if not ok:
        raise HTTPException(401, "ancien mot de passe incorrect")
    _set_session(response, a.new_session(), request)
    return {"ok": True}


@router.get("/settings")
def get_settings(request: Request) -> dict[str, Any]:
    return public_view(_ctx(request).store.get())


@router.put("/settings")
async def put_settings(request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "un objet JSON est attendu")
    store = _ctx(request).store
    data = updated_settings(store.get(), body)
    try:
        new = await asyncio.to_thread(store.replace, data)
    except ValidationError as e:
        raise HTTPException(422, _validation_detail(e)) from None
    return {**public_view(new), "restart_needed": True}


@router.put("/secrets")
async def put_secret(body: SecretBody, request: Request) -> dict[str, Any]:
    store = _ctx(request).store
    data = store.get().model_dump(mode="python")
    parts = body.name.split(".")
    if body.name in PAGE_SECRETS:
        data[parts[0]][parts[1]] = body.value
    elif (len(parts) == 4 and parts[:2] == ["brain", "providers"] and parts[3] == "api_key"
          and parts[2]):
        prov = data["brain"]["providers"].setdefault(parts[2], {})
        prov["api_key"] = body.value
    else:
        raise HTTPException(422, f"secret inconnu : {body.name}")
    try:
        new = await asyncio.to_thread(store.replace, data)
    except ValidationError as e:
        raise HTTPException(422, _validation_detail(e)) from None
    return {"secrets": secrets_status(new), "restart_needed": True}


@router.get("/persona")
def get_persona(request: Request) -> dict[str, str]:
    from ..brain.base import load_persona

    return {"base": load_persona(), "extra": _ctx(request).store.get().brain.persona_extra}


@router.get("/budget")
def get_budget(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    budget = _need(ctx.budget, "le budget")
    cfg = ctx.store.get().budget
    month = budget.month()
    rows = [
        {"provider": r["provider"], "model": r["model"], "input_tokens": r["input_tokens"],
         "output_tokens": r["output_tokens"], "usd": r["usd"]}
        for r in budget.db.conn.execute(
            "SELECT * FROM usage WHERE month = ? ORDER BY usd DESC, model", (month,))
    ]
    history = [{"month": r[0], "usd": r[1]} for r in budget.db.conn.execute(
        "SELECT month, SUM(usd) FROM usage GROUP BY month ORDER BY month DESC LIMIT 12")]
    spent = budget.spent(month)
    return {"month": month, "spent": spent, "monthly_usd": cfg.monthly_usd,
            "ratio": spent / cfg.monthly_usd if cfg.monthly_usd > 0 else None,
            "warn_pct": cfg.warn_pct, "stop_pct": cfg.stop_pct, "rows": rows,
            "history": history}


def _memory_dict(m) -> dict[str, Any]:
    return {"id": m.id, "text": m.text, "created_at": m.created_at.isoformat(),
            "source": m.source}


@router.get("/memories")
def list_memories(request: Request) -> dict[str, Any]:
    memory = _need(_ctx(request).memory, "la mémoire")
    return {"memories": [_memory_dict(m) for m in memory.all()],
            "max_items": memory.max_items}


@router.post("/memories")
def add_memory(body: TextBody, request: Request) -> dict[str, Any]:
    memory = _need(_ctx(request).memory, "la mémoire")
    try:
        memory_id = memory.remember(body.text, source="web")
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    return {"id": memory_id}


@router.put("/memories/{memory_id}")
def edit_memory(memory_id: int, body: TextBody, request: Request) -> dict[str, Any]:
    memory = _need(_ctx(request).memory, "la mémoire")
    try:
        m = memory.edit(memory_id, body.text)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    if m is None:
        raise HTTPException(404, "souvenir introuvable")
    return _memory_dict(m)


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: int, request: Request) -> dict[str, bool]:
    memory = _need(_ctx(request).memory, "la mémoire")
    if memory.forget(memory_id) is None:
        raise HTTPException(404, "souvenir introuvable")
    return {"ok": True}


@router.delete("/memories")
def clear_memories(request: Request) -> dict[str, int]:
    return {"deleted": _need(_ctx(request).memory, "la mémoire").clear()}


@router.get("/timers")
def list_timers(request: Request) -> dict[str, Any]:
    timers = _need(_ctx(request).timers, "les minuteurs")
    return {"timers": [{"id": t.id, "rabbit_id": t.rabbit_id, "due_at": t.due_at.isoformat(),
                        "text": t.text, "recurrence": t.recurrence} for t in timers.all()],
            "timezone": str(timers.tz)}


@router.delete("/timers/{timer_id}")
def cancel_timer(timer_id: int, request: Request) -> dict[str, bool]:
    timers = _need(_ctx(request).timers, "les minuteurs")
    timer = next((t for t in timers.all() if t.id == timer_id), None)
    if timer is None or not timers.cancel(timer.rabbit_id, timer_id):
        raise HTTPException(404, "minuteur introuvable")
    return {"ok": True}


@router.get("/rabbits")
def list_rabbits(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    configured = ctx.store.get().rabbits
    seen = {r.id: r for r in ctx.registry.all()} if ctx.registry is not None else {}
    rabbits = []
    for rid in sorted(set(seen) | set(configured)):
        cfg, live = configured.get(rid), seen.get(rid)
        rabbits.append({
            "id": rid,
            "name": (cfg.name if cfg and cfg.name else "") or (live.name if live else rid),
            "default_lms_player": cfg.default_lms_player if cfg else "",
            "connected": bool(live and live.connected),
            "state": live.state if live else "inconnu",
        })
    return {"rabbits": rabbits}


@router.get("/journal")
def get_journal(request: Request, limit: int = 50) -> dict[str, Any]:
    ctx = _ctx(request)
    log = _need(ctx.log, "le journal")
    log.purge()
    db = log.db.conn
    convs = [dict(r) for r in db.execute(
        "SELECT * FROM conversations ORDER BY started_at DESC, id DESC LIMIT ?",
        (max(1, min(limit, 500)),))]
    for c in convs:
        c["turns"] = [{"role": t["role"], "text": t["text"], "created_at": t["created_at"]}
                      for t in db.execute("SELECT * FROM turns WHERE conversation_id = ? "
                                          "ORDER BY id", (c["id"],))]
    return {"conversations": convs, "retention_days": ctx.store.get().logs.retention_days}


@router.delete("/journal")
def clear_journal(request: Request) -> dict[str, int]:
    log = _need(_ctx(request).log, "le journal")
    cur = log.db.conn.execute("DELETE FROM conversations")
    log.db.conn.commit()
    return {"deleted": cur.rowcount}


@router.get("/voice/preview")
async def voice_preview(request: Request, text: str = "Bonjour, je suis Lapinou !") -> Response:
    voice = _need(_ctx(request).voice, "la synthèse vocale")
    try:
        mp3 = await asyncio.to_thread(voice.cached_mp3, text[:300])
    except (RuntimeError, ValueError) as e:
        raise HTTPException(503, f"synthèse impossible : {e}") from None
    return Response(mp3, media_type="audio/mpeg")


@router.get("/updates")
def get_updates(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    commit = ctx.updater.current() if ctx.updater is not None else "inconnu"
    ota = ctx.ota.status().as_dict() if ctx.ota is not None else None
    return {"server": {"version": __version__, "commit": commit}, "ota": ota}


@router.post("/updates/server")
async def update_server(request: Request) -> dict[str, Any]:
    result = await _need(_ctx(request).updater, "la mise à jour").update()
    return dict(result.__dict__)


@router.post("/updates/ota")
async def refresh_ota(request: Request) -> dict[str, Any]:
    from ..ota import OtaError

    relay = _need(_ctx(request).ota, "le relais OTA")
    try:
        status = await relay.refresh()
    except OtaError as e:
        raise HTTPException(502, str(e)) from None
    return status.as_dict()


@router.post("/restart")
async def restart(request: Request) -> dict[str, bool]:
    await _need(_ctx(request).updater, "le redémarrage").restart()
    return {"ok": True}


@router.post("/usbkey")
async def usb_key(body: UsbKeyBody, request: Request) -> FileResponse:
    from ..usbkey import UsbKeyError, WifiInput, build_usb_key

    ctx = _ctx(request)
    release = ctx.ota.key_zip() if ctx.ota is not None else None
    if release is None:
        raise HTTPException(409, "aucune clé de version téléchargée : cherchez d'abord une "
                                 "mise à jour (page « Mises à jour »)")
    version = ctx.ota.status().version or "version"
    tmp = Path(tempfile.mkdtemp(prefix="lapinou-key-"))
    try:
        out = await asyncio.to_thread(build_usb_key, release, ctx.store.get(),
                                      WifiInput(**body.model_dump()), tmp / "key.zip")
    except UsbKeyError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(422, str(e)) from None
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return FileResponse(out, media_type="application/zip",
                        filename=f"lapinou-cle-{version}.zip",
                        background=BackgroundTask(shutil.rmtree, tmp, True))


# --- Installation dans l'application -----------------------------------------------------


def install(app: FastAPI) -> None:
    app.include_router(public)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if (path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS")
                and request.headers.get(CSRF_HEADER) != "1"):
            return JSONResponse({"detail": "en-tête X-LapiNou manquant"}, status_code=403)
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/api/"):
            h["Cache-Control"] = "no-store"
        if h.get("content-type", "").startswith("text/html"):
            h["Content-Security-Policy"] = CSP
        return response
