# SPDX-License-Identifier: MIT
"""Serveur de compatibilité (port 8443) : la reconnaissance vocale de LapinLibre pour
voice-daemon (voice.conf [asr]).

Même protocole que `asr_server.Session` (tools/app-server/asr_server.py, R4 : ses fonctions sont
importées, le fichier n'est pas modifié) : voice-daemon envoie la grammaire (ou l'URL de la
grammaire de lancement du contrôleur, `/asr/grammar/<id>.abnf`) puis l'audio (8 kHz mu-law),
chaque message précédé de sa longueur, et reçoit l'alternative reconnue. La session est
réécrite ici pour ce qui diffère de LapinLibre :

- une grammaire donnée par URL n'est jamais téléchargée : la grammaire de lancement est celle
  d'`AppHost`, toute autre URL vaut une grammaire vide ($VOID) (pas de SSRF) ;
- bornes : message ≤ MAX_FRAME, session ≤ MAX_SESSION_S, audio ≤ MAX_AUDIO_BYTES, au plus
  MAX_SESSIONS sessions à la fois (au-delà : BUSY) ;
- le `Recogniser` est celui de l'écoute de Lapinou (aucun second Parakeet). Le 8443 passe
  après elle : une seule transcription 8443 à la fois, et son verrou n'est attendu que
  LOCK_WAIT_S (sinon NOMATCH) ; l'écoute de Lapinou n'attend donc au plus qu'une transcription
  d'un segment (≤ SPEECH_MAX_S d'audio) ;
- sans modèles, chaque session répond NOMATCH (rien ne se lance au hasard, au contraire du
  « first » de LapinLibre) ;
- vie privée : ni grammaire, ni transcription, ni réponse dans le journal ; seulement la durée
  d'audio et le score. L'audio n'est pas gardé.
"""

from __future__ import annotations

import logging
import re
import socket
import socketserver
import struct
import threading
import time
from typing import Any

from ..voice.vad import asr_server
from .apps import AppHost

log = logging.getLogger(__name__)

MAX_FRAME = 64 * 1024
MAX_SESSION_S = 30.0
MAX_AUDIO_BYTES = 30 * asr_server.RATE  # 30 s d'audio à 8 kHz mu-law
MAX_SESSIONS = 4
LOCK_WAIT_S = 2.0
RECV_TIMEOUT_S = 30.0

_URL = re.compile(r"https?://\S+")


def transcribe_bounded(recogniser: Any, samples: list[float], wait_s: float,
                       turn: threading.Semaphore) -> str | None:
    """Les mots des échantillons (16 kHz), ou None si le `Recogniser` partagé n'est pas libre
    dans `wait_s` (l'écoute de Lapinou a la priorité). `turn` : une transcription 8443 à la fois."""
    deadline = time.monotonic() + wait_s
    if not turn.acquire(timeout=wait_s):
        return None
    try:
        if not recogniser.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return None
        try:  # comme asr_server.Recogniser.transcribe, verrou déjà pris
            stream = recogniser.recognizer.create_stream()
            stream.accept_waveform(asr_server.MODEL_RATE, samples)
            recogniser.recognizer.decode_stream(stream)
            return stream.result.text.strip()
        finally:
            recogniser.lock.release()
    finally:
        turn.release()


def _framed(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


class Session(socketserver.BaseRequestHandler):
    """Une session voice-daemon. Réglages par sous-classe (`make_server`) : rien de global."""

    recogniser: Any = None
    apps: AppHost | None = None
    slots: threading.BoundedSemaphore
    turn: threading.Semaphore
    server: _Server

    def resolve(self, grammar: str) -> str:
        """Le texte de la grammaire ; une URL n'est jamais lue sur le réseau."""
        if not _URL.fullmatch(grammar.strip()):
            return grammar
        assert self.apps is not None
        return self.apps.grammar_source(grammar.strip()) or self.apps.void_grammar()

    def recv_exact(self, n: int, deadline: float) -> bytes:
        data = b""
        while len(data) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self.request.settimeout(min(RECV_TIMEOUT_S, remaining))
            chunk = self.request.recv(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def handle(self) -> None:
        if not self.slots.acquire(blocking=False):
            log.info("session ASR (8443) refusée : %d sessions déjà ouvertes", MAX_SESSIONS)
            try:
                self.request.sendall(_framed(asr_server.response_message("BUSY")))
            except OSError:
                pass
            return
        self.server.track(self.request, True)
        try:
            self._session()
        finally:
            self.server.track(self.request, False)
            self.slots.release()

    def _session(self) -> None:
        deadline = time.monotonic() + MAX_SESSION_S
        grammar, audio, started, answered, score = "", 0, None, False, None
        listening = asr_server.Listening(self.recogniser) if self.recogniser is not None else None
        why = "fin"
        try:
            while True:
                (size,) = struct.unpack(">I", self.recv_exact(4, deadline))
                if size > MAX_FRAME:
                    why = "message trop grand"
                    break
                chunk = b""
                for number, _, value in asr_server.fields(self.recv_exact(size, deadline)):
                    if number == 1:
                        grammar = value.decode(errors="replace")
                    elif number == 2:
                        chunk += value
                audio += len(chunk)
                if audio > MAX_AUDIO_BYTES:
                    why = "trop d'audio"
                    break
                if chunk and started is None:
                    started = time.monotonic()
                if answered:
                    continue
                if listening is not None:
                    heard = listening.feed(chunk) if chunk else None
                    if heard is not None:
                        final = listening.samples >= asr_server.NOINPUT_S * asr_server.MODEL_RATE
                        answer = self.recognised(grammar, *heard, final=final)
                        if answer is None:
                            listening.again()
                        else:
                            payload, score = answer
                            self.request.sendall(_framed(payload))
                            answered = True
                elif started is not None and time.monotonic() - started >= asr_server.AFTER_S:
                    self.request.sendall(_framed(asr_server.response_message("NOMATCH", score=0)))
                    answered, score = True, 0
        except TimeoutError:
            why = "durée maximale"
        except (EOFError, OSError, ValueError, IndexError):
            pass
        log.info("session ASR (8443) : %.1f s d'audio, score %s (%s)",
                 audio / asr_server.RATE, "-" if score is None else score, why)

    def recognised(self, grammar: str, kind: str, samples: Any,
                   final: bool = True) -> tuple[bytes, int] | None:
        """(réponse, score) pour ce qui a été entendu, ou None pour écouter encore ; même règle
        que asr_server.Session.recognised, sans rien journaliser du contenu."""
        if kind == "noinput":
            return asr_server.response_message("NOINPUT"), 0
        heard = transcribe_bounded(self.recogniser, samples, LOCK_WAIT_S, self.turn)
        if heard is None:  # modèle occupé par l'écoute de Lapinou
            return asr_server.response_message("NOMATCH"), 0
        match = asr_server.best_match(heard, asr_server.choices(self.resolve(grammar)))
        short = (len(samples) / asr_server.MODEL_RATE < asr_server.SHORT_S
                 and (match is None or match[2] < asr_server.MATCH_MIN))
        if not final and (not heard or short):
            return None
        if match is None:
            if not heard:
                return asr_server.response_message("NOMATCH"), 0
            return asr_server.response_message("OK", words=heard, score=80), 80
        words, semantic, sim = match
        if sim < asr_server.MATCH_MIN:
            return asr_server.response_message("NOMATCH", words=heard), round(sim * 100)
        score = round(sim * 100)
        return asr_server.response_message("OK", words=words, semantic=semantic, score=score), score


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True  # redémarrage rapide du service (TIME_WAIT)
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any):
        self._active: set[socket.socket] = set()
        self._active_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def track(self, sock: socket.socket, open_: bool) -> None:
        with self._active_lock:
            (self._active.add if open_ else self._active.discard)(sock)

    def close_sessions(self) -> None:
        """Coupe les sessions en cours (arrêt du service, avant la fermeture de la voix)."""
        with self._active_lock:
            socks = list(self._active)
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self) -> None:
        self.shutdown()
        self.close_sessions()
        self.server_close()


def make_server(recogniser: Any, apps: AppHost, listen: str, port: int) -> _Server:
    """Le serveur, prêt pour `serve_forever()` (dans un fil) ; port 0 = libre."""
    handler = type("Session", (Session,), {
        "recogniser": recogniser, "apps": apps,
        "slots": threading.BoundedSemaphore(MAX_SESSIONS), "turn": threading.Semaphore(1),
    })
    return _Server((listen, port), handler)
