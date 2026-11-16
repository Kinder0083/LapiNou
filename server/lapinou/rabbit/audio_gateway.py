# SPDX-License-Identifier: MIT
"""Passerelle audio : reçoit le micro de chaque lapin (mu-law 16 kHz, TCP) et le rend en PCM.

Format des trames : docs/lapinou/protocole-audio.md. Le décodage protobuf réutilise les
fonctions de LapinLibre (tools/app-server/asr_server.py), sans protoc.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
_APP_SERVER_DIR = REPO / "tools" / "app-server"
if str(_APP_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_SERVER_DIR))

import asr_server  # noqa: E402  (LapinLibre : fields)

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MAX_FRAME = 64 * 1024
MAX_RABBIT_ID = 128
QUEUE_MAX = 1000  # blocs de ~2048 échantillons : plus de 2 minutes d'audio
MARK_KEEP = 10 * SAMPLE_RATE  # une marque finie est oubliée 10 s (axe du micro) après sa fin
OPEN_MARK_KEEP = 3600 * SAMPLE_RATE  # une lecture sans fin reçue est oubliée après 1 h
REWIND = SAMPLE_RATE  # first_sample qui recule de plus de 1 s : lapinou-mic a redémarré


def _build_mulaw_table() -> np.ndarray:
    """G.711 mu-law -> int16, calculé en bloc (même formule que asr_server.mulaw_sample)."""
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    x = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)
    return np.where(u & 0x80, 0x84 - x, x - 0x84).astype(np.int16)


_MULAW = _build_mulaw_table()


def mulaw_decode(data: bytes) -> np.ndarray:
    """Les octets mu-law en échantillons int16."""
    return _MULAW[np.frombuffer(data, dtype=np.uint8)]


@dataclass
class AudioChunk:
    pcm: np.ndarray  # int16, 16 kHz
    first_sample: int


@dataclass
class Frame:
    rabbit_id: str
    seq: int
    mulaw: bytes | None = None
    first_sample: int | None = None
    mark: tuple[str, int, bool] | None = None  # (playback_id, at_sample, start)
    hello: tuple[str, str | None, int] | None = None  # (rabbit_id, firmware, sample_rate)


def _text(value: bytes) -> str:
    return value.decode("utf-8")


def _parse_hello(data: bytes) -> tuple[str, str | None, int]:
    rabbit_id = firmware = rate = None
    for number, wire, value in asr_server.fields(data):
        if number == 1 and wire == 2:
            rabbit_id = _text(value)
        elif number == 2 and wire == 2:
            firmware = _text(value)
        elif number == 3 and wire == 0:
            rate = value
    if rabbit_id is None or rate is None:
        raise ValueError("Hello incomplet")
    return rabbit_id, firmware, rate


def _parse_mark(data: bytes) -> tuple[str, int, bool]:
    playback_id = at_sample = start = None
    for number, wire, value in asr_server.fields(data):
        if number == 1 and wire == 2:
            playback_id = _text(value)
        elif number == 2 and wire == 0:
            at_sample = value
        elif number == 3 and wire == 0:
            start = bool(value)
    if playback_id is None or at_sample is None or start is None:
        raise ValueError("PlaybackMark incomplet")
    return playback_id, at_sample, start


def parse_frame(data: bytes) -> Frame:
    """Décode un LapinouAudio ; ValueError si malformé ou incomplet."""
    rabbit_id = seq = None
    extra: dict[str, object] = {}
    try:
        for number, wire, value in asr_server.fields(data):
            if number == 1 and wire == 2:
                rabbit_id = _text(value)
            elif number == 2 and wire == 0:
                seq = value
            elif number == 3 and wire == 2:
                extra["mulaw"] = bytes(value)
            elif number == 4 and wire == 0:
                extra["first_sample"] = value
            elif number == 5 and wire == 2:
                extra["mark"] = _parse_mark(value)
            elif number == 6 and wire == 2:
                extra["hello"] = _parse_hello(value)
    except (IndexError, UnicodeDecodeError) as exc:
        raise ValueError(f"trame malformée : {exc!r}") from exc
    if not rabbit_id or len(rabbit_id) > MAX_RABBIT_ID or seq is None:
        raise ValueError("rabbit_id ou seq manquant")
    return Frame(rabbit_id, seq, **extra)  # type: ignore[arg-type]


class RabbitStream:
    """Le flux audio d'un lapin. Survit aux reconnexions."""

    def __init__(self, rabbit_id: str) -> None:
        self.rabbit_id = rabbit_id
        self.firmware_version: str | None = None
        self.connected = False
        # playback_id -> (échantillon de début, échantillon de fin ou None si en cours).
        # Borné : une marque finie depuis MARK_KEEP est retirée. Une marque de début (nouvelle
        # lecture ou reprise après pause) place sa clé en dernier (ordre d'insertion).
        self.playback_marks: dict[str, tuple[int, int | None]] = {}
        self._fresh_marks: set[str] = set()  # marques reçues depuis le dernier bloc audio
        self.gaps = 0  # sauts de seq
        self.lost_frames = 0  # trames manquantes au total
        self._queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(QUEUE_MAX)
        self._last_seq: int | None = None
        self._next_sample = 0

    def _new_connection(self) -> None:
        self._last_seq = None
        self.connected = True

    def _feed(self, frame: Frame) -> None:
        if self._last_seq is not None and frame.seq != (self._last_seq + 1) & 0xFFFFFFFF:
            self.gaps += 1
            self.lost_frames += (frame.seq - self._last_seq - 1) & 0xFFFFFFFF
        self._last_seq = frame.seq
        if frame.mark is not None:
            playback_id, at_sample, start = frame.mark
            if start:
                self.playback_marks.pop(playback_id, None)
                self.playback_marks[playback_id] = (at_sample, None)
            else:
                began = self.playback_marks.get(playback_id, (at_sample, None))[0]
                self.playback_marks[playback_id] = (began, at_sample)
            self._fresh_marks.add(playback_id)
        if frame.mulaw:
            first = frame.first_sample if frame.first_sample is not None else self._next_sample
            pcm = mulaw_decode(frame.mulaw)
            if first < self._next_sample - REWIND:
                # nouvel axe (lapinou-mic redémarré) : les anciennes marques n'y ont plus de sens
                for playback_id in [k for k in self.playback_marks if k not in self._fresh_marks]:
                    del self.playback_marks[playback_id]
            self._fresh_marks.clear()
            self._next_sample = first + len(pcm)
            self._prune_marks()
            if self._queue.full():  # consommateur trop lent : on perd le plus ancien
                self._queue.get_nowait()
                log.warning("%s : file audio pleine, bloc le plus ancien perdu", self.rabbit_id)
            self._queue.put_nowait(AudioChunk(pcm, first))

    def _prune_marks(self) -> None:
        if not self.playback_marks:
            return
        now = self._next_sample
        stale = [
            k
            for k, (start, end) in self.playback_marks.items()
            if (end is not None and end < now - MARK_KEEP)
            or (end is None and start < now - OPEN_MARK_KEEP)
        ]
        for playback_id in stale:
            del self.playback_marks[playback_id]

    async def frames(self) -> AsyncIterator[AudioChunk]:
        """Les blocs audio, dans l'ordre. Ne s'arrête qu'à la fermeture de la passerelle."""
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    def _close(self) -> None:
        self.connected = False
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(None)


class AudioGateway:
    """Serveur TCP des flux audio des lapins, un RabbitStream par rabbit_id.

    `on_stream(stream)` (réglable après coup) est appelé une fois par nouveau lapin, à la
    création de son flux (pas à ses reconnexions : le flux leur survit)."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9300,
        on_stream: Callable[[RabbitStream], None] | None = None,
    ) -> None:
        self.host, self.port = host, port
        self.on_stream = on_stream
        self.streams: dict[str, RabbitStream] = {}
        self._server: asyncio.Server | None = None
        self._writers: dict[str, asyncio.StreamWriter] = {}
        self._waiters: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers.values()):
            writer.close()
        if self._server is not None:
            await self._server.wait_closed()
        for stream in self.streams.values():
            stream._close()

    async def stream(self, rabbit_id: str) -> RabbitStream:
        """Le flux d'un lapin, en attendant sa première connexion si besoin."""
        while rabbit_id not in self.streams:
            await self._waiters.setdefault(rabbit_id, asyncio.Event()).wait()
        return self.streams[rabbit_id]

    @staticmethod
    async def _read_frame(reader: asyncio.StreamReader) -> Frame:
        (size,) = struct.unpack(">I", await reader.readexactly(4))
        if size > MAX_FRAME:
            raise ValueError(f"trame trop grande ({size} octets)")
        return parse_frame(await reader.readexactly(size))

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        stream: RabbitStream | None = None
        try:
            first = await self._read_frame(reader)
            if first.hello is None:
                raise ValueError("première trame sans Hello")
            hello_id, firmware, rate = first.hello
            if rate != SAMPLE_RATE or hello_id != first.rabbit_id:
                raise ValueError(f"Hello refusé (sample_rate={rate}, rabbit_id={hello_id!r})")
            rabbit_id = first.rabbit_id
            stream = self.streams.get(rabbit_id)
            if stream is None:
                stream = self.streams[rabbit_id] = RabbitStream(rabbit_id)
                if (event := self._waiters.pop(rabbit_id, None)) is not None:
                    event.set()
                if self.on_stream is not None:
                    try:
                        self.on_stream(stream)
                    except Exception:
                        log.exception("on_stream en échec pour %s", rabbit_id)
            old = self._writers.get(rabbit_id)
            if old is not None:
                old.close()
            self._writers[rabbit_id] = writer
            stream.firmware_version = firmware
            stream._new_connection()
            log.info("lapin %s connecté depuis %s", rabbit_id, peer)
            stream._feed(first)
            while True:
                frame = await self._read_frame(reader)
                if frame.rabbit_id != rabbit_id:
                    raise ValueError(f"rabbit_id changé en cours de flux ({frame.rabbit_id!r})")
                if self._writers.get(rabbit_id) is not writer:
                    return  # remplacé par une connexion plus récente
                stream._feed(frame)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except ValueError as exc:
            log.warning("flux audio %s refusé : %s", peer, exc)
        finally:
            if stream is not None and self._writers.get(stream.rabbit_id) is writer:
                del self._writers[stream.rabbit_id]
                stream.connected = False
                log.info("lapin %s déconnecté", stream.rabbit_id)
            writer.close()
