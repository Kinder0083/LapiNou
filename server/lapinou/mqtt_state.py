# SPDX-License-Identifier: MIT
"""État de l'assistant publié sur MQTT : `<prefix>/<id>/assistant` (retenu, QoS 1).

`prefix` est `mqtt.prefix` (« lapinou » par défaut) : il doit valoir `[mqtt] prefix` de
lapinou.conf sur le lapin, sinon le lapin n'entend pas l'état.

`lapinou-mqtt`, sur le lapin, relit ce topic et le recopie dans l'état de l'appareil (capteur
« Assistant » de Home Assistant, 64 octets au plus). Valeurs : « repos », « écoute »,
« réfléchit », « parle », « hors ligne ». Client paho-mqtt dans son propre fil (connexion et
reconnexion automatiques) ; l'état de chaque lapin est republié à chaque (re)connexion. Sans
`mqtt.host`, rien n'est publié. Le mot de passe est transmis tel quel et jamais journalisé.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

PREFIX = "lapinou"
LABELS = {
    "idle": "repos",
    "listening": "écoute",
    "followup": "écoute",
    "thinking": "réfléchit",
    "speaking": "parle",
    "offline": "hors ligne",
}
# Comme lapinou-mqtt : un identifiant qui pourrait sortir de son topic (« / », « + », « # »)
# n'est pas publié.
_RABBIT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
KEEPALIVE_S = 30
CLOSE_WAIT_S = 1.0  # à l'arrêt, attente de l'envoi de chaque « hors ligne »


def _paho_client() -> Any:
    import paho.mqtt.client as mqtt

    return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="lapinou-server")


class AssistantState:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        *,
        prefix: str = PREFIX,
        client_factory: Callable[[], Any] = _paho_client,
    ) -> None:
        self.host, self.port = host, port
        self._username, self._password = username, password
        self.prefix = prefix
        self._factory = client_factory
        self._client: Any = None
        self._last: dict[str, str] = {}  # lapin -> dernière valeur publiée
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"AssistantState(host={self.host!r}, port={self.port})"

    @classmethod
    def from_settings(cls, settings: Settings) -> AssistantState:
        m = settings.mqtt
        return cls(m.host, m.port, m.username, m.password, prefix=m.prefix)

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def topic(self, rabbit_id: str) -> str:
        return f"{self.prefix}/{rabbit_id}/assistant"

    def start(self) -> None:
        """Lance la connexion (en arrière-plan : un broker absent ne bloque rien)."""
        if not self.enabled or self._client is not None:
            return
        client = self._factory()
        if self._username:
            # MQTT 3.1.1 interdit un mot de passe sans identifiant.
            client.username_pw_set(self._username, self._password or None)
        client.on_connect = self._on_connect
        client.reconnect_delay_set(1, 30)
        client.connect_async(self.host, self.port, KEEPALIVE_S)
        client.loop_start()
        self._client = client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.warning("MQTT : connexion refusée (%s)", reason_code)
            return
        with self._lock:
            last = dict(self._last)
        for rabbit_id, label in last.items():
            client.publish(self.topic(rabbit_id), label, qos=1, retain=True)

    def publish(self, rabbit_id: str, state: str) -> None:
        """Publie l'état (clé de LABELS) ; ne lève pas."""
        label = LABELS[state]
        if not self.enabled or not _RABBIT_ID.match(rabbit_id):
            return
        with self._lock:
            if self._last.get(rabbit_id) == label:
                return
            self._last[rabbit_id] = label
        if self._client is None:
            return  # publié à la connexion
        try:
            self._client.publish(self.topic(rabbit_id), label, qos=1, retain=True)
        except Exception as e:  # noqa: BLE001 - MQTT absent : tout le reste fonctionne
            log.warning("MQTT : publication impossible (%s)", type(e).__name__)

    def close(self) -> None:
        """Tous les lapins « hors ligne », puis déconnexion."""
        client, self._client = self._client, None
        if client is None:
            return
        with self._lock:
            ids = list(self._last)
            self._last = {i: LABELS["offline"] for i in ids}
        try:
            sent = [
                client.publish(self.topic(rabbit_id), LABELS["offline"], qos=1, retain=True)
                for rabbit_id in ids
            ]
            if client.is_connected():
                for info in sent:
                    info.wait_for_publish(CLOSE_WAIT_S)
            client.disconnect()
        except Exception as e:  # noqa: BLE001
            log.warning("MQTT : fermeture incomplète (%s)", type(e).__name__)
        client.loop_stop()
