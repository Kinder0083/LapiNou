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
import inspect
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
from .config import DEFAULT_CONFIG_PATH, ConfigError, Settings, load_settings
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
    from . import surveillance as surveillance_mod

    # Enregistrements de la surveillance sonore, GET /surveillance/clips/<jeton>.wav
    app.state.surveillance = services.surveillance if services is not None else None
    app.include_router(surveillance_mod.router)
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

    @app.get("/health", include_in_schema=False)
    def health_alias() -> dict[str, str]:
        """Alias de /api/health (sondes de supervision, Docker)."""
        return {"status": "ok"}

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
    surveillance: Any = None
    tts_server: Any = None
    asr_server: Any = None
    # Tâches de fond du démarrage (Home Assistant, chargement de la voix) : jamais attendues
    # par le démarrage, annulées à l'arrêt.
    background: set[Any] = field(default_factory=set)

    def refresh_tools(self) -> None:
        """La boîte à outils des sessions (après le démarrage de ha-mcp : sa liste d'outils)."""
        from .tools.base import ToolBox

        self.orchestrator.deps.tools = ToolBox(t for s in self.tool_sources for t in s.tools())

    async def start(self) -> None:
        """Lance tout ; ce qui peut être long (ha-mcp jusqu'à 30 s, la voix quelques
        secondes) part en tâche de fond : la page (8080) et le TTS (8081) répondent tout de
        suite, et l'orchestrateur reçoit les outils de Home Assistant dès qu'ils sont là."""
        server = self.settings.server
        await self.registry.start(port=server.platform_port)
        await self.gateway.start()
        await self.timers.start()
        self.state.start()
        if self.surveillance is not None:
            await self.surveillance.start()
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
        if self.ha is not None:
            self._background(self._start_ha(), "ha")
        load = getattr(self.voice, "load", None)
        if load is not None:  # voix préchargée : la première réponse n'attend pas le modèle
            self._background(asyncio.to_thread(load), "voix")
        log.info("serveur LapiNou démarré")

    async def _start_ha(self) -> None:
        await self.ha.start()  # journalise lui-même un ha-mcp injoignable, sans lever
        self.refresh_tools()
        log.info("Home Assistant : outils prêts")

    def _background(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=f"lapinou-{name}")
        self.background.add(task)

        def done(t: asyncio.Task) -> None:
            self.background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("démarrage : %s en échec (%s)", name, t.exception())

        task.add_done_callback(done)

    @staticmethod
    def _serve(server: Any, name: str) -> None:
        threading.Thread(target=server.serve_forever, name=name, daemon=True).start()

    async def close(self) -> None:
        """Ferme tout dans l'ordre inverse ; chaque étape a lieu même si une précédente échoue
        (la première erreur est relancée à la fin). Les sessions ASR sont coupées avant la voix."""

        async def stop_server(attr: str) -> None:
            srv = getattr(self, attr)
            if srv is not None:
                setattr(self, attr, None)
                stop = getattr(srv, "stop", None)  # asr_compat : coupe aussi les sessions
                await (asyncio.to_thread(stop) if stop else asyncio.to_thread(_stop_http, srv))

        async def stop_background() -> None:
            tasks = list(self.background)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        steps: list[tuple[str, Any]] = [
            ("background", stop_background),
            ("ota", lambda: self.ota.close() if self.ota is not None else None),
            ("asr", lambda: stop_server("asr_server")),
            ("tts", lambda: stop_server("tts_server")),
            ("orchestrator", self.orchestrator.close),
            ("surveillance",
             lambda: self.surveillance.close() if self.surveillance is not None else None),
            ("timers", self.timers.close),
            ("ha", lambda: self.ha.close() if self.ha is not None else None),
            ("gateway", self.gateway.stop),
            ("registry", self.registry.close),
            ("state", lambda: asyncio.to_thread(self.state.close)),
            ("voice", self.voice.close),
            ("db", self.db.close),
        ]
        first: BaseException | None = None
        for name, step in steps:
            try:
                result = step()
                if inspect.isawaitable(result):
                    await result
            except Exception as e:  # noqa: BLE001 (on continue de fermer le reste)
                log.exception("arrêt : %s en échec", name)
                first = first or e
        log.info("serveur LapiNou arrêté")
        if first is not None:
            raise first


def _stop_http(srv: Any) -> None:
    srv.shutdown()
    srv.server_close()


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
    data_dir = None if str(db_path) == ":memory:" else Path(db_path).parent
    orchestrator.deps.surveillance = _surveillance(settings, data_dir, recogniser)
    orchestrator.attach(registry, gateway)
    from .config import SettingsStore
    from .ota import OtaRelay
    from .updater import Updater
    from .web.apps import AppHost
    from .web.routes import WebContext

    relay = OtaRelay.from_settings(settings)
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
        surveillance=orchestrator.deps.surveillance,
    )


def _surveillance(settings: Settings, data_dir: Path | None, recogniser: Any) -> Any:
    """La surveillance sonore : seulement avec [mqtt] host, [surveillance] enabled et un
    dossier pour les enregistrements (pas avec une base en mémoire). La voix (Silero) et le
    texte (Parakeet) viennent des modèles de transcription, s'ils sont là."""
    from .surveillance import Surveillance, SurveillanceMqtt

    mqtt = SurveillanceMqtt.from_settings(settings)
    if mqtt is None or data_dir is None or not settings.surveillance.enabled:
        return None
    make_vad = None
    if recogniser is not None:
        from .voice import vad

        def make_vad():
            segmenter = vad.SileroSegmenter(recogniser, max_segment_s=30.0, silence_s=0.5)

            def speaking(pcm):
                segmenter.feed(pcm)  # les segments finis sont jetés : seule la présence compte
                return segmenter.speaking

            return speaking

    return Surveillance(settings.surveillance, mqtt, data_dir / "surveillance",
                        base_url(settings), make_vad=make_vad, transcriber=recogniser,
                        tagger=_sound_tagger())


def _sound_tagger() -> Any:
    """La reconnaissance des sons, si son modèle est là (`models fetch`) ; sinon aucune."""
    from .voice import sounds, vad

    directory = vad.asr_server.MODELS
    if not sounds.model_present(directory):
        log.info("surveillance : pas de modèle de reconnaissance des sons dans %s", directory)
        return None
    return sounds.SoundTagger(directory)


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
    try:
        settings = load_settings(args.config)
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(2) from None
    services = build_services(settings, db_path=args.db)
    uvicorn.run(create_app(settings, services=services), host="0.0.0.0",
                port=settings.server.http_port, timeout_graceful_shutdown=10)
