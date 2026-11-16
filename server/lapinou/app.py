# SPDX-License-Identifier: MIT
"""Application FastAPI du serveur LapiNou."""

from __future__ import annotations

from fastapi import FastAPI

from . import __version__
from .config import Settings
from .voice import tts


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="LapiNou", version=__version__)
    app.state.settings = settings
    app.state.tts_store = tts.Mp3Store()  # MP3 des phrases, GET /tts/<id>.mp3
    app.include_router(tts.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
