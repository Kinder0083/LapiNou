# SPDX-License-Identifier: MIT
"""Serveur de compatibilité (port 8081) : le protocole de `tts_server.py` de LapinLibre.

voice-daemon du lapin POSTe un texte enveloppé de balises SSML (`<s><break .../>texte...</s>`)
sur `/<langue>` et lit le MP3 de la réponse (`audio/mpeg`) : c'est la voix de Voice.tts, donc
des notifications Home Assistant (MQTT cmd/say). On répond comme `tts_server.Handler` (texte
extrait par `tts_server.plain_text`, chemin vide = `en`, 500 si la synthèse échoue), avec en
plus 400 pour un texte vide et 413 pour un texte trop long. La voix est celle des réglages,
quelle que soit la langue du chemin.
"""

from __future__ import annotations

import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lapinou.voice import tts
from lapinou.voice.tts import Voice

tts_server = tts.tts_server
MAX_BODY = 16 * tts.MAX_TEXT_CHARS  # les balises SSML pèsent : borne de lecture du corps


def make_server(voice: Voice, listen: str, port: int) -> ThreadingHTTPServer:
    """Le serveur HTTP, prêt pour `serve_forever()` (dans un fil) ; port 0 = libre."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length).decode("utf-8", "replace")
            text = tts_server.plain_text(body)
            if not text:
                self.send_error(400, "empty text")
                return
            if len(text) > tts.MAX_TEXT_CHARS:
                self.send_error(413, "text too long")
                return
            started = time.time()
            try:
                mp3 = voice.cached_mp3(text)
            except (RuntimeError, OSError, ValueError) as e:
                tts_server.log(f"synthesis failed ({type(e).__name__})")
                self.send_error(500)
                return
            # Le texte n'est pas journalisé (vie privée), comme pour la transcription.
            tts_server.log(f"{len(text)} chars -> {len(mp3)} bytes ({time.time() - started:.1f} s)")
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(mp3)))
            self.end_headers()
            self.wfile.write(mp3)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer((listen, port), Handler)
