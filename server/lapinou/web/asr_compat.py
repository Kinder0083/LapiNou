# SPDX-License-Identifier: MIT
"""Serveur de compatibilité (port 8443) : la reconnaissance vocale de LapinLibre pour
voice-daemon (voice.conf [asr]), `asr_server.Session` importé tel quel (R4).

voice-daemon envoie la grammaire (ou l'URL de la grammaire de lancement du contrôleur,
`/asr/grammar/<id>.abnf`, servie ici par `AppHost`) puis l'audio, et reçoit l'alternative
reconnue. La reconnaissance utilise le `Recogniser` déjà chargé pour l'écoute de Lapinou
(aucun second chargement de Parakeet) ; sans modèles, chaque session répond NOMATCH (rien n'est
lancé au hasard, au contraire du « first » de LapinLibre).
"""

from __future__ import annotations

import socketserver
from typing import Any

from ..voice.vad import asr_server
from .apps import AppHost


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True  # redémarrage rapide du service (TIME_WAIT)
    daemon_threads = True


def make_server(recogniser: Any, apps: AppHost, listen: str, port: int) -> _Server:
    """Le serveur, prêt pour `serve_forever()` (dans un fil) ; port 0 = libre."""
    session = asr_server.Session
    session.keep = None  # l'audio des sessions n'est pas gardé (vie privée)
    session.recogniser = recogniser
    session.answer = "recognise" if recogniser is not None else "nomatch"
    asr_server.set_grammar_source(apps.grammar_source)
    return _Server((listen, port), session)
