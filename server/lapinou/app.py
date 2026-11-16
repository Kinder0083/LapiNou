# SPDX-License-Identifier: MIT
"""Application FastAPI du serveur LapiNou, et son câblage de production (`build_services`).

`create_app(settings)` seul : l'API HTTP (tests). Avec `services`, le démarrage de
l'application lance aussi la plateforme des lapins (9123), la passerelle audio (9300), le
serveur TTS de compatibilité (8081), Home Assistant (ha-mcp), les minuteurs et l'état MQTT ;
l'arrêt les ferme dans l'ordre inverse.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Settings, load_settings
from .voice import tts

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("/var/lib/lapinou/lapinou.db")
_UNSET: Any = object()


def create_app(
    settings: Settings, *, services: Services | None = None, web: Any = None, apps: Any = None
) -> FastAPI:
    """`web` (`WebContext`) : ce que la page de réglages montre ; sinon celui des services, ou
    une page sans base (réglages et connexion seulement). `apps` (`AppHost`) : les applications
    LapinLibre servies aux lapins ; sinon celles des services, ou aucune."""
    from . import ota
    from .config import SettingsStore
    from .web import apps as apps_mod
    from .web import routes

    lifespan = None
    if services is not None:

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            services.voice.attach(app)
            try:
                await services.start()  # en cas d'échec, ce qui a démarré est refermé
                yield
            finally:
                await services.close()

    # Ni /docs, ni /redoc, ni /openapi.json : rien d'autre que la page et l'API sur 8080.
    app = FastAPI(title="LapiNou", version=__version__, lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.tts_store = tts.Mp3Store()  # MP3 des phrases, GET /tts/<id>.mp3
    app.state.services = services
    app.include_router(tts.router)
    if web is None:
        web = services.web if services is not None and services.web is not None else (
            routes.WebContext(store=SettingsStore(settings)))
    app.state.web = web
    app.state.ota = web.ota  # GET /ota/manifest.json et ses fichiers, pour les lapins
    app.include_router(ota.router)
    if apps is None:
        apps = services.apps if services is not None and services.apps is not None else (
            apps_mod.AppHost(None, None))
    app.state.apps = apps  # /file/…/get, /asr/grammar/…, /log, /upload (url_app des lapins)
    app.include_router(apps_mod.router)
    routes.install(app)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


@dataclass
class Services:
    settings: Settings
    db: Any
    registry: Any
    gateway: Any
    voice: Any
    orchestrator: Any
    timers: Any
    state: Any
    ha: Any = None
    tool_sources: list[Any] = field(default_factory=list)
    ota: Any = None
    web: Any = None
    apps: Any = None
    recogniser: Any = None
    tts_server: Any = None
    asr_server: Any = None
    _threads: list[threading.Thread] = field(default_factory=list)

    def refresh_tools(self) -> None:
        """La boîte à outils des sessions (après le démarrage de ha-mcp : sa liste d'outils)."""
        from .tools.base import ToolBox

        self.orchestrator.deps.tools = ToolBox(t for s in self.tool_sources for t in s.tools())

    async def start(self) -> None:
        server = self.settings.server
        await self.registry.start(port=server.platform_port)
        await self.gateway.start()
        if self.ha is not None:
            await self.ha.start()
            self.refresh_tools()
        await self.timers.start()
        self.state.start()
        from .web import apps as apps_mod
        from .web import asr_compat, tts_compat

        self.tts_server = tts_compat.make_server(self.voice, "0.0.0.0", server.tts_port)
        self._serve(self.tts_server, "tts-compat")
        self.asr_server = asr_compat.make_server(
            self.recogniser, self.apps or apps_mod.AppHost(None, None), "0.0.0.0",
            server.asr_port)
        self._serve(self.asr_server, "asr-compat")
        if self.ota is not None:
            await self.ota.start()
        if self.web is not None:
            self.web.auth.prepare_setup()  # code d'installation si aucun mot de passe (R40)
        log.info("serveur LapiNou démarré")

    def _serve(self, server: Any, name: str) -> None:
        thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    async def close(self) -> None:
        if self.ota is not None:
            await self.ota.close()
        for attr in ("asr_server", "tts_server"):
            srv = getattr(self, attr)
            if srv is not None:
                await asyncio.to_thread(srv.shutdown)
                srv.server_close()
                setattr(self, attr, None)
        self._threads.clear()
        await self.orchestrator.close()
        await self.timers.close()
        if self.ha is not None:
            await self.ha.close()
        await self.gateway.stop()
        await self.registry.close()
        await asyncio.to_thread(self.state.close)
        self.voice.close()
        self.db.close()
        log.info("serveur LapiNou arrêté")


def _lan_address() -> str:
    """L'adresse locale par laquelle ce serveur sort sur le réseau (aucun paquet envoyé)."""
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET : seulement pour choisir l'interface
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def base_url(settings: Settings) -> str:
    """Début des URL que le lapin utilise pour joindre ce serveur (MP3 des réponses, clé USB) :
    LAPINOU_BASE_URL, sinon `server.public_url`, sinon l'adresse locale détectée."""
    configured = os.environ.get("LAPINOU_BASE_URL") or settings.server.public_url
    return configured.rstrip("/") or f"http://{_lan_address()}:{settings.server.http_port}"


def build_services(
    settings: Settings,
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    voice: Any = None,
    recogniser: Any = _UNSET,
    brain: Any = None,
) -> Services:
    """Tout le serveur, construit depuis les réglages ; rien n'est lancé (voir `start`)."""
    from .brain.budget import Budget
    from .brain.router import BrainRouter
    from .conversation import ConversationLog, Orchestrator, SessionDeps
    from .db import Database
    from .mqtt_state import AssistantState
    from .rabbit.audio_gateway import AudioGateway
    from .rabbit.registry import RabbitRegistry
    from .tools.base import ToolBox
    from .tools.ha import HomeAssistantTools
    from .tools.lms import LmsTools
    from .tools.memory import MemoryTools
    from .tools.policy import Policy
    from .tools.stories import StoryTools
    from .tools.timers import TimerTools
    from .voice import asr, vad, wake_asr

    db = Database(db_path)
    db.migrate()
    registry = RabbitRegistry(timezone=settings.server.timezone)
    gateway = AudioGateway(port=settings.server.mic_port)
    if voice is None:
        voice = tts.Voice.from_settings(settings, base_url=base_url(settings))
    if recogniser is _UNSET:
        recogniser = vad.load_recogniser() if vad.models_present() else None
        if recogniser is None:
            log.error("modèles de transcription absents (python -m lapinou.voice.models fetch) :"
                      " ni mot d'activation ni écoute")
    transcriber = asr.Transcriber(recogniser=recogniser) if recogniser is not None else None
    make_detector = None
    if recogniser is not None:
        voice_cfg = settings.voice
        if voice_cfg.wake_method == "oww":
            log.warning("wake_method = « oww » non géré par l'orchestrateur : transcription (R23)")
            voice_cfg = voice_cfg.model_copy(update={"wake_method": "asr"})

        def make_detector():
            return wake_asr.build_detector(voice_cfg, recogniser)

    from .voice.aec import EchoCanceller

    orchestrator = Orchestrator()
    ha = (HomeAssistantTools.from_settings(settings, Policy(settings.safety))
          if settings.ha.url else None)
    memory = MemoryTools(db, enabled=settings.memory.enabled,
                         max_items=settings.memory.max_items, pin=settings.safety.pin)
    timers = TimerTools(db, orchestrator, tz=settings.server.timezone)
    lms = LmsTools.from_settings(settings) if settings.lms.host else None
    sources = [s for s in (ha, memory if settings.memory.enabled else None, timers, lms,
                           StoryTools()) if s is not None]
    orchestrator.deps = SessionDeps(
        brain=brain or BrainRouter.from_settings(settings),
        budget=Budget(db, settings.budget),
        tools=ToolBox(t for s in sources for t in s.tools()),
        voice=voice,
        transcriber=transcriber,
        make_detector=make_detector,
        make_aec=EchoCanceller,
        ha=ha,
        memory=memory if settings.memory.enabled else None,
        log=ConversationLog(db, settings.logs.retention_days),
        state=AssistantState.from_settings(settings),
        followup_s=settings.voice.followup_s,
        silence_ms=settings.voice.vad_silence_ms,
    )
    orchestrator.attach(registry, gateway)
    from .config import SettingsStore
    from .ota import OtaRelay
    from .updater import Updater
    from .web.routes import WebContext

    from .web.apps import AppHost

    relay = OtaRelay.from_settings(settings)
    data_dir = None if str(db_path) == ":memory:" else Path(db_path).parent
    apps = AppHost(data_dir / "apps" if data_dir else None,
                   data_dir / "uploads" if data_dir else None)
    web = WebContext(
        store=SettingsStore(settings), db=db, memory=memory, timers=timers,
        budget=orchestrator.deps.budget, log=orchestrator.deps.log, registry=registry,
        voice=voice, ota=relay, updater=Updater.from_settings(settings),
        setup_token_path=(None if str(db_path) == ":memory:"
                          else Path(db_path).parent / "setup-token"),
    )
    return Services(
        settings=settings, db=db, registry=registry, gateway=gateway, voice=voice,
        orchestrator=orchestrator, timers=timers, state=orchestrator.deps.state, ha=ha,
        tool_sources=sources, ota=relay, web=web, apps=apps, recogniser=recogniser,
    )


def main(argv: list[str] | None = None) -> None:
    """`python -m lapinou [--config CHEMIN] [--db CHEMIN]` : le serveur complet, jusqu'à SIGTERM
    ou SIGINT (arrêt propre : services refermés dans l'ordre inverse)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="python -m lapinou", description="Serveur LapiNou")
    parser.add_argument("--config", type=Path,
                        default=Path(os.environ.get("LAPINOU_CONFIG", DEFAULT_CONFIG_PATH)),
                        help="config.toml (secrets.toml à côté) ; défaut : LAPINOU_CONFIG, "
                             "sinon %(default)s")
    parser.add_argument("--db", type=Path,
                        default=Path(os.environ.get("LAPINOU_DB", DEFAULT_DB_PATH)),
                        help="base SQLite ; son dossier garde aussi apps/, uploads/ et le code "
                             "d'installation ; défaut : LAPINOU_DB, sinon %(default)s")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    services = build_services(settings, db_path=args.db)
    uvicorn.run(create_app(settings, services=services), host="0.0.0.0",
                port=settings.server.http_port, timeout_graceful_shutdown=10)
