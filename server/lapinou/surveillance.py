# SPDX-License-Identifier: MIT
"""Surveillance sonore : le micro du lapin, quand la maison est sous alarme.

Conception : docs/superpowers/specs/2026-11-21-surveillance-sonore-design.md.

Le flux micro que le serveur reçoit déjà (lapinou-mic, 16 kHz, écho retiré) passe, pour un lapin
armé, par un `Detector` : niveau RMS de chaque bloc en dBFS, « bruit » (au-dessus du seuil
pendant `min_noise_ms` d'affilée, fini après `calm_s` calmes), « voix » (Silero VAD, finie après
`calm_s` sans parole) et enregistrements de `clip_s` secondes dont les `pre_s` d'avant, l'un
après l'autre tant que le bruit ou la voix dure. Les blocs joués par le lapin lui-même ne
comptent pas pour la détection.

`Surveillance` publie, par MQTT, des entités Home Assistant dans l'appareil « Lapinou » que
lapinou-mqtt déclare (`identifiers` `lapinou_<id>`) : un interrupteur, un seuil, le niveau,
« Bruit détecté », « Voix détectée » et un événement à la fin de chaque enregistrement (lien du
WAV, texte entendu s'il y a eu de la parole). Elles sont indisponibles quand le lapin ou le
serveur l'est. L'état armé et le seuil de chaque lapin survivent à un redémarrage
(`state.json`). Hors surveillance, rien n'est examiné, gardé ni publié.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

if TYPE_CHECKING:
    from .config import Settings, SurveillanceConfig

log = logging.getLogger(__name__)

RATE = 16000
FLOOR_DB = -90.0
THRESHOLD_MIN, THRESHOLD_MAX = -70, -10
LEVEL_STEP_DB = 2.0  # un niveau qui bouge de moins est republié seulement toutes les 30 s
LEVEL_REPEAT_S = 30
QUEUE_MAX = 200  # blocs d'un lapin en attente d'examen (~25 s) : au-delà, les plus vieux partent
_RABBIT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{22}$")
KEEPALIVE_S = 30


def level_db(pcm: np.ndarray) -> float:
    """RMS de `pcm` (int16) en dBFS (sinus plein = -3 dBFS), au moins FLOOR_DB."""
    if not len(pcm):
        return FLOOR_DB
    rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64)))))
    if rms <= 0:
        return FLOOR_DB
    return max(FLOOR_DB, 20 * np.log10(rms / 32768))


@dataclass
class Clip:
    samples: np.ndarray  # int16, 16 kHz
    kinds: set[str]  # « bruit », « voix »
    peak_db: float
    started: dt.datetime


@dataclass
class _Recording:
    parts: list[np.ndarray]
    length: int
    kinds: set[str]
    peak_db: float
    started: dt.datetime


class Detector:
    """La détection d'un lapin, sur le nombre d'échantillons reçus (pas l'horloge) : chaque
    `feed` rend ses événements, `("level", dB)`, `("noise", bool)`, `("voice", bool)`,
    `("clip", Clip)`. `vad(pcm) -> bool` : de la parole dans ce bloc. `clock()` : l'heure du
    début d'un enregistrement."""

    def __init__(self, cfg: SurveillanceConfig, vad: Callable[[np.ndarray], bool] | None = None,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self.cfg = cfg
        self.threshold_db = float(cfg.threshold_db)
        self._vad = vad
        self._clock = clock
        self._min_noise = int(cfg.min_noise_ms * RATE / 1000)
        self._calm = int(cfg.calm_s * RATE)
        self._pre = int(cfg.pre_s * RATE)
        self._clip = int(cfg.clip_s * RATE)
        self.reset()

    def reset(self) -> None:
        self.noise = self.voice = False
        self._loud_run = self._noise_calm = self._voice_calm = 0
        self._ring: deque[np.ndarray] = deque()
        self._ring_len = 0
        self._rec: _Recording | None = None
        self._win_n = 0
        self._win_max = FLOOR_DB
        self._last_level: float | None = None
        self._since_level = 0

    def feed(self, pcm: np.ndarray, muted: bool = False) -> list[tuple[str, Any]]:
        n = len(pcm)
        if not n:
            return []
        events: list[tuple[str, Any]] = []
        db = level_db(pcm)
        self._level(db, n, events)
        loud = not muted and db > self.threshold_db
        speech = False
        if not muted and self._vad is not None:
            try:
                speech = bool(self._vad(pcm))
            except Exception:  # noqa: BLE001 - la VAD en panne n'arrête pas le bruit
                log.exception("surveillance : VAD en échec")
                self._vad = None
        # Bruit : `min_noise` d'affilée au-dessus du seuil ; fini après `calm` calmes.
        self._loud_run = self._loud_run + n if loud else 0
        onset = False
        if not self.noise and loud and self._loud_run >= self._min_noise:
            self.noise, onset = True, True
            self._noise_calm = 0
            events.append(("noise", True))
        elif self.noise:
            self._noise_calm = 0 if loud else self._noise_calm + n
            if self._noise_calm >= self._calm:
                self.noise = False
                events.append(("noise", False))
        # Voix : dès un bloc de parole ; finie après `calm` sans parole.
        if not self.voice and speech:
            self.voice, onset = True, True
            self._voice_calm = 0
            events.append(("voice", True))
        elif self.voice:
            self._voice_calm = 0 if speech else self._voice_calm + n
            if self._voice_calm >= self._calm:
                self.voice = False
                events.append(("voice", False))
        self._record(pcm, db, onset, events)
        return events

    def _level(self, db: float, n: int, events: list) -> None:
        """Au plus un niveau par seconde : le maximum de la seconde, s'il a bougé de
        LEVEL_STEP_DB ou que le dernier date de LEVEL_REPEAT_S."""
        self._win_n += n
        self._since_level += n
        self._win_max = max(self._win_max, db)
        if self._win_n < RATE:
            return
        level = round(self._win_max, 1)
        self._win_n, self._win_max = 0, FLOOR_DB
        if (self._last_level is None or abs(level - self._last_level) >= LEVEL_STEP_DB
                or self._since_level >= LEVEL_REPEAT_S * RATE):
            self._last_level, self._since_level = level, 0
            events.append(("level", level))

    def _flags(self) -> set[str]:
        return ({"bruit"} if self.noise else set()) | ({"voix"} if self.voice else set())

    def _record(self, pcm: np.ndarray, db: float, onset: bool, events: list) -> None:
        # La mémoire tournante : les `pre` derniers échantillons, ce bloc compris.
        self._ring.append(pcm)
        self._ring_len += len(pcm)
        while self._ring and self._ring_len - len(self._ring[0]) >= self._pre:
            self._ring_len -= len(self._ring.popleft())
        rec = self._rec
        if rec is None:
            if not onset:
                return
            parts = list(self._ring)
            extra = self._ring_len - self._pre
            if extra > 0:
                parts[0] = parts[0][extra:]
            self._rec = _Recording(parts, sum(len(p) for p in parts), self._flags(), db,
                                   self._clock())
        else:
            rec.parts.append(pcm)
            rec.length += len(pcm)
            rec.kinds |= self._flags()
            rec.peak_db = max(rec.peak_db, db)
        rec = self._rec
        if rec.length < self._clip:
            return
        samples = np.concatenate(rec.parts)
        rest = samples[self._clip:]
        events.append(("clip", Clip(samples[: self._clip], rec.kinds, round(rec.peak_db, 1),
                                    rec.started)))
        self._rec = None
        if self.noise or self.voice:  # ça dure : l'enregistrement suivant commence aussitôt
            self._rec = _Recording([rest], len(rest), self._flags(),
                                   level_db(rest) if len(rest) else FLOOR_DB, self._clock())


class ClipStore:
    """Les WAV (16 kHz, mono, 16 bits) des enregistrements, nommés par un jeton aléatoire de
    128 bits (le seul moyen de les lire) ; gardés `keep_days` jours, `keep_max` au plus."""

    def __init__(self, directory: Path, keep_days: int = 7, keep_max: int = 500) -> None:
        self.dir = Path(directory)
        self.keep_days, self.keep_max = keep_days, keep_max

    def save(self, samples: np.ndarray) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(16)
        tmp = self.dir / f".{token}.tmp"
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(np.asarray(samples, np.int16).tobytes())
        os.replace(tmp, self.dir / f"{token}.wav")
        return token

    def path(self, token: str) -> Path | None:
        if not _TOKEN.match(token or ""):
            return None
        p = self.dir / f"{token}.wav"
        return p if p.is_file() else None

    def purge(self) -> int:
        if not self.dir.is_dir():
            return 0
        files = sorted(self.dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        limit = time.time() - self.keep_days * 86400
        removed = 0
        for i, p in enumerate(files):
            if i >= self.keep_max or p.stat().st_mtime < limit:
                p.unlink(missing_ok=True)
                removed += 1
        return removed


# --- MQTT -------------------------------------------------------------------------------------


def _base(prefix: str, rabbit_id: str) -> str:
    return f"{prefix}/{rabbit_id}/surveillance"


def server_availability(prefix: str) -> str:
    return f"{prefix}/server/availability"


def discovery(prefix: str, discovery_prefix: str, rabbit_id: str) -> list[tuple[str, str]]:
    """Les messages de découverte Home Assistant des entités de surveillance d'un lapin."""
    from . import __version__

    base = _base(prefix, rabbit_id)
    common = {
        "availability": [{"topic": f"{prefix}/{rabbit_id}/availability"},
                         {"topic": server_availability(prefix)}],
        "availability_mode": "all",
        "device": {"identifiers": [f"lapinou_{rabbit_id}"]},
        "origin": {"name": "lapinou-server", "sw_version": __version__},
    }
    entities = [
        ("switch", "surv_armed", {
            "name": "Surveillance sonore", "icon": "mdi:ear-hearing",
            "command_topic": f"{base}/armed/set", "state_topic": f"{base}/armed"}),
        ("number", "surv_threshold", {
            "name": "Seuil de bruit", "icon": "mdi:volume-vibrate", "entity_category": "config",
            "command_topic": f"{base}/threshold/set", "state_topic": f"{base}/threshold",
            "min": THRESHOLD_MIN, "max": THRESHOLD_MAX, "step": 1, "mode": "slider",
            "unit_of_measurement": "dBFS"}),
        ("sensor", "surv_level", {
            "name": "Niveau sonore", "icon": "mdi:waveform", "state_topic": f"{base}/level",
            "unit_of_measurement": "dBFS", "state_class": "measurement"}),
        ("binary_sensor", "surv_noise", {
            "name": "Bruit détecté", "device_class": "sound", "state_topic": f"{base}/noise"}),
        ("binary_sensor", "surv_voice", {
            "name": "Voix détectée", "icon": "mdi:account-voice",
            "state_topic": f"{base}/voice"}),
        ("event", "surv_event", {
            "name": "Événement sonore", "icon": "mdi:microphone-message",
            "state_topic": f"{base}/event", "event_types": ["bruit", "voix"]}),
    ]
    out = []
    for component, obj, cfg in entities:
        payload = {**cfg, "unique_id": f"lapinou_{rabbit_id}_{obj}", **common}
        out.append((f"{discovery_prefix}/{component}/{rabbit_id}/{obj}/config",
                    json.dumps(payload, ensure_ascii=False)))
    return out


def _paho_client() -> Any:
    import paho.mqtt.client as mqtt

    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                       client_id="lapinou-server-surveillance")


class SurveillanceMqtt:
    """Le client MQTT de la surveillance (le sien : son testament dit le serveur parti)."""

    def __init__(self, host: str, port: int = 1883, username: str = "", password: str = "", *,
                 prefix: str = "lapinou", discovery_prefix: str = "homeassistant",
                 client_factory: Callable[[], Any] = _paho_client) -> None:
        self.host, self.port = host, port
        self._username, self._password = username, password
        self.prefix, self.discovery_prefix = prefix, discovery_prefix
        self._factory = client_factory
        self.client: Any = None
        self.connected = False
        self.on_connected: Callable[[], None] | None = None
        self.on_message: Callable[[str, str], None] | None = None

    def __repr__(self) -> str:
        return f"SurveillanceMqtt(host={self.host!r}, port={self.port})"

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs) -> SurveillanceMqtt | None:
        m = settings.mqtt
        if not m.host:
            return None
        return cls(m.host, m.port, m.username, m.password, prefix=m.prefix,
                   discovery_prefix=m.discovery_prefix, **kwargs)

    def start(self) -> None:
        if self.client is not None:
            return
        client = self._factory()
        if self._username:
            client.username_pw_set(self._username, self._password or None)
        client.will_set(server_availability(self.prefix), "offline", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.reconnect_delay_set(1, 30)
        client.connect_async(self.host, self.port, KEEPALIVE_S)
        client.loop_start()
        self.client = client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.warning("surveillance : connexion MQTT refusée (%s)", reason_code)
            return
        self.connected = True
        client.subscribe(f"{self.prefix}/+/surveillance/+/set", qos=1)
        client.subscribe(f"{self.discovery_prefix}/status", qos=1)
        client.publish(server_availability(self.prefix), "online", qos=1, retain=True)
        if self.on_connected is not None:
            self.on_connected()

    def _on_message(self, client, userdata, msg) -> None:
        if self.on_message is not None:
            try:
                self.on_message(msg.topic, msg.payload.decode("utf-8", "replace").strip())
            except Exception:  # noqa: BLE001
                log.exception("surveillance : message MQTT %s en échec", msg.topic)

    def publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if self.client is None:
            return
        try:
            self.client.publish(topic, payload, qos=1, retain=retain)
        except Exception as e:  # noqa: BLE001
            log.warning("surveillance : publication MQTT impossible (%s)", type(e).__name__)

    def close(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        try:
            info = client.publish(server_availability(self.prefix), "offline", qos=1,
                                  retain=True)
            if client.is_connected():
                info.wait_for_publish(1.0)
            client.disconnect()
        except Exception as e:  # noqa: BLE001
            log.warning("surveillance : fermeture MQTT incomplète (%s)", type(e).__name__)
        client.loop_stop()


# --- Le tout -------------------------------------------------------------------------------


@dataclass
class _Rabbit:
    id: str
    detector: Detector
    armed: bool = False
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue())
    task: asyncio.Task | None = None


class Surveillance:
    """La surveillance de tous les lapins. `feed` (depuis la session d'un lapin, sur la boucle)
    ne fait que mettre le bloc en file ; l'examen et les publications ont lieu dans la tâche du
    lapin. Sans MQTT (`mqtt` None) ou `enabled = false`, rien ne tourne."""

    def __init__(self, cfg: SurveillanceConfig, mqtt: SurveillanceMqtt | None, directory: Path,
                 base_url: str, *, make_vad: Callable[[], Callable[[np.ndarray], bool]] | None = None,
                 transcriber: Any = None,
                 clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self.cfg = cfg
        self.mqtt = mqtt if cfg.enabled else None
        self.dir = Path(directory)
        self.store = ClipStore(self.dir / "clips", cfg.keep_days, cfg.keep_max)
        self.base_url = base_url.rstrip("/")
        self._make_vad = make_vad
        self._transcriber = transcriber if cfg.transcribe else None
        self._clock = clock
        self._rabbits: dict[str, _Rabbit] = {}
        self._saved = self._load()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bg: set[asyncio.Task] = set()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.mqtt is not None

    # --- État gardé --------------------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return self.dir / "state.json"

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        data = {rid: {"armed": r.armed, "threshold": r.detector.threshold_db}
                for rid, r in self._rabbits.items()}
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self._state_path)
        except OSError as e:
            log.warning("surveillance : état non enregistré (%s)", e)

    # --- Cycle de vie ------------------------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            return
        self._loop = asyncio.get_running_loop()
        self.mqtt.on_connected = lambda: self._threadsafe(self._announce_all)
        self.mqtt.on_message = lambda t, p: self._threadsafe(self._command, t, p)
        self.mqtt.start()
        await asyncio.to_thread(self.store.purge)

    async def close(self) -> None:
        tasks = [r.task for r in self._rabbits.values() if r.task is not None] + list(self._bg)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.mqtt is not None:
            await asyncio.to_thread(self.mqtt.close)

    def _threadsafe(self, fn, *args) -> None:
        """Depuis le fil de paho : `fn` sur la boucle (ou tout de suite sans boucle)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            if threading.get_ident() == getattr(loop, "_thread_id", None):
                fn(*args)
            else:
                loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass

    # --- Lapins -------------------------------------------------------------------------------

    def add(self, rabbit_id: str) -> None:
        """Un lapin dont le flux micro arrive : ses entités, son état gardé, sa tâche."""
        if not self.enabled or rabbit_id in self._rabbits or not _RABBIT_ID.match(rabbit_id):
            return
        vad = None
        if self._make_vad is not None:
            try:
                vad = self._make_vad()
            except Exception:  # noqa: BLE001 - sans VAD, le bruit est encore détecté
                log.exception("lapin %s : VAD de la surveillance impossible", rabbit_id)
        detector = Detector(self.cfg, vad, self._clock)
        saved = self._saved.get(rabbit_id, {})
        try:
            detector.threshold_db = float(min(THRESHOLD_MAX, max(
                THRESHOLD_MIN, float(saved.get("threshold", self.cfg.threshold_db)))))
        except (TypeError, ValueError):
            pass
        rabbit = _Rabbit(rabbit_id, detector, armed=bool(saved.get("armed", False)))
        self._rabbits[rabbit_id] = rabbit
        rabbit.task = asyncio.get_running_loop().create_task(self._run(rabbit))
        if rabbit.armed:
            log.info("lapin %s : surveillance sonore armée (gardée)", rabbit_id)
        if self.mqtt.connected:
            self._announce(rabbit)

    def feed(self, rabbit_id: str, pcm: np.ndarray, muted: bool) -> None:
        rabbit = self._rabbits.get(rabbit_id)
        if rabbit is None or not rabbit.armed:
            return
        if rabbit.queue.qsize() >= QUEUE_MAX:
            rabbit.queue.get_nowait()
        rabbit.queue.put_nowait((pcm, muted))

    def _topic(self, rabbit_id: str, name: str) -> str:
        return f"{_base(self.mqtt.prefix, rabbit_id)}/{name}"

    def _pub(self, rabbit_id: str, name: str, payload: str, retain: bool = True) -> None:
        self.mqtt.publish(self._topic(rabbit_id, name), payload, retain)

    def _announce_all(self) -> None:
        for rabbit in self._rabbits.values():
            self._announce(rabbit)

    def _announce(self, rabbit: _Rabbit) -> None:
        for topic, payload in discovery(self.mqtt.prefix, self.mqtt.discovery_prefix, rabbit.id):
            self.mqtt.publish(topic, payload)
        self._pub(rabbit.id, "armed", "ON" if rabbit.armed else "OFF")
        self._pub(rabbit.id, "threshold", _fmt(rabbit.detector.threshold_db))
        if not rabbit.armed:
            self._quiet(rabbit)

    def _quiet(self, rabbit: _Rabbit) -> None:
        self._pub(rabbit.id, "noise", "OFF")
        self._pub(rabbit.id, "voice", "OFF")
        self._pub(rabbit.id, "level", "None")

    def _command(self, topic: str, payload: str) -> None:
        if topic == f"{self.mqtt.discovery_prefix}/status":
            if payload == "online":  # Home Assistant redémarré : il réapprend tout
                self._announce_all()
            return
        parts = topic.split("/")
        if len(parts) != 5 or parts[2] != "surveillance" or parts[4] != "set":
            return
        rabbit = self._rabbits.get(parts[1])
        if rabbit is None:
            return
        if parts[3] == "armed":
            armed = payload.upper() == "ON"
            if payload.upper() not in ("ON", "OFF"):
                return
            if armed != rabbit.armed:
                rabbit.armed = armed
                rabbit.detector.reset()
                while not rabbit.queue.empty():
                    rabbit.queue.get_nowait()
                log.info("lapin %s : surveillance sonore %s", rabbit.id,
                         "armée" if armed else "désarmée")
                self._save()
            self._pub(rabbit.id, "armed", "ON" if armed else "OFF")
            if not armed:
                self._quiet(rabbit)
        elif parts[3] == "threshold":
            try:
                value = float(payload)
            except ValueError:
                return
            if not np.isfinite(value):
                return
            value = float(round(min(THRESHOLD_MAX, max(THRESHOLD_MIN, value))))
            rabbit.detector.threshold_db = value
            self._save()
            self._pub(rabbit.id, "threshold", _fmt(value))

    async def _run(self, rabbit: _Rabbit) -> None:
        while True:
            pcm, muted = await rabbit.queue.get()
            if not rabbit.armed:
                continue
            events = await asyncio.to_thread(rabbit.detector.feed, pcm, muted)
            if not rabbit.armed:  # désarmé pendant l'examen : rien n'est publié
                continue
            for kind, value in events:
                if kind == "level":
                    self._pub(rabbit.id, "level", _fmt(value))
                elif kind in ("noise", "voice"):
                    self._pub(rabbit.id, kind, "ON" if value else "OFF")
                    if value:
                        log.info("lapin %s : %s détecté(e)", rabbit.id,
                                 "bruit" if kind == "noise" else "voix")
                elif kind == "clip":
                    task = asyncio.get_running_loop().create_task(self._clip(rabbit.id, value))
                    self._bg.add(task)
                    task.add_done_callback(self._bg.discard)

    async def _clip(self, rabbit_id: str, clip: Clip) -> None:
        token = await asyncio.to_thread(self.store.save, clip.samples)
        await asyncio.to_thread(self.store.purge)
        text = ""
        if "voix" in clip.kinds and self._transcriber is not None:
            try:
                from .voice import vad

                samples = clip.samples.astype(np.float32) / 32768.0
                text = await asyncio.to_thread(vad.transcribe_padded, self._transcriber, samples)
            except Exception:  # noqa: BLE001 - l'événement part sans texte
                log.exception("lapin %s : transcription de l'enregistrement en échec", rabbit_id)
        event = {
            "event_type": "voix" if "voix" in clip.kinds else "bruit",
            "enregistrement": f"{self.base_url}/surveillance/clips/{token}.wav",
            "texte": text,
            "niveau": clip.peak_db,
            "debut": clip.started.isoformat(),
            "bruit": "bruit" in clip.kinds,
            "voix": "voix" in clip.kinds,
        }
        log.info("lapin %s : enregistrement de surveillance (%s)", rabbit_id,
                 ", ".join(sorted(clip.kinds)))
        if self.mqtt is not None:
            self._pub(rabbit_id, "event", json.dumps(event, ensure_ascii=False), retain=False)


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


router = APIRouter()


@router.get("/surveillance/clips/{name}", include_in_schema=False)
def get_clip(name: str, request: Request) -> FileResponse:
    """Un enregistrement de surveillance, par son jeton (sans connexion : le jeton est le
    secret, comme les MP3 de /tts/)."""
    hub: Surveillance | None = getattr(request.app.state, "surveillance", None)
    path = None
    if hub is not None and name.endswith(".wav"):
        path = hub.store.path(name[:-4])
    if path is None:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/wav")
