# SPDX-License-Identifier: MIT
"""Politique des actions Home Assistant : appliquée par LapiNou avant tout appel à ha-mcp.

Seuls les services de `SERVICES` sont permis, domaine par domaine (un nom de service est
aussi un script : `script.ouvrir_garage` ne doit pas passer pour `script.bonne_nuit`).

Sensibles : `lock.*`, `alarm_control_panel.*`, `cover` de classe garage, portail ou porte (ou
de classe inconnue), les entités marquées sensibles, `climate` réglé hors de la plage permise,
toute entité dont l'identifiant ou le nom contient un radical ou un mot sensible (R33 : verrou,
garage, chauffage, porte, four…), et les scripts ou scènes dont le contenu touche à l'un de ces
domaines ou n'a pas pu être lu. Une action sensible est refusée, sauf si son entité figure dans
`allowed_entities` : elle demande alors une confirmation vocale (et le code PIN s'il est réglé).
Le reste n'est permis que dans les domaines de `allowed_domains`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import SafetyConfig
from .base import fold

SERVICES: dict[str, set[str]] = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle"},
    "script": {"turn_on", "toggle"},
    "scene": {"turn_on"},
    "cover": {"open_cover", "close_cover", "stop_cover", "set_cover_position"},
    "climate": {"set_temperature", "turn_on", "turn_off"},
    "media_player": {"volume_set", "media_play", "media_pause", "media_stop", "media_next_track"},
    # Toujours sensibles : seulement par la voie de la confirmation.
    "lock": {"lock", "unlock", "open"},
    "alarm_control_panel": {"alarm_arm_home", "alarm_arm_away", "alarm_arm_night",
                            "alarm_disarm"},
}
# Le contenu d'un script ou d'une scène n'a pas été lu : sensible dans le doute.
UNREAD = "contenu du script ou de la scène illisible"

_ENTITY = re.compile(r"^([a-z0-9_]+)\.[a-z0-9_]+$")
_CLIMATE_KEYS = ("temperature", "target_temp_low", "target_temp_high")
_YES = {"oui", "ouais", "ok", "okay", "daccord", "confirme", "confirmer", "vas y", "vasy",
        "bien sur", "exactement", "yes"}
_NO = {"non", "pas", "annule", "annuler", "stop", "arrete", "surtout pas", "jamais"}
_DIGITS = {"zero": "0", "un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4",
           "cinq": "5", "six": "6", "sept": "7", "huit": "8", "neuf": "9"}
_VERBS = {
    "unlock": "déverrouiller", "lock": "verrouiller", "open": "ouvrir",
    "alarm_disarm": "désactiver", "alarm_arm_home": "activer", "alarm_arm_away": "activer",
    "alarm_arm_night": "activer", "open_cover": "ouvrir", "close_cover": "fermer",
    "stop_cover": "arrêter", "set_cover_position": "régler", "turn_on": "allumer ou lancer",
    "turn_off": "éteindre", "toggle": "basculer", "set_temperature": "régler",
}


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class NeedsConfirmation:
    phrase: str  # la question officielle, dite telle quelle par LapiNou (pas par le modèle)
    needs_pin: bool = False


@dataclass(frozen=True)
class Deny:
    reason: str


Decision = Allow | NeedsConfirmation | Deny


def _words(text: str) -> str:
    """Identifiant ou nom en mots (« switch.four_cuisine » → « switch four cuisine »)."""
    return fold(text.replace("_", " ").replace(".", " "))


class Policy:
    def __init__(self, safety: SafetyConfig) -> None:
        self.safety = safety
        parts = [rf"\w*{re.escape(fold(k))}\w*" for k in safety.assist_blocked_stems]
        parts += [rf"\b{re.escape(fold(k))}[sx]?\b" for k in safety.assist_blocked_words]
        self._blocked = re.compile("|".join(parts)) if parts else None

    def sensitive_name(self, text: str) -> str | None:
        """Le radical ou mot sensible contenu dans `text`, ou None."""
        if self._blocked is None or not text:
            return None
        m = self._blocked.search(_words(text))
        return m.group(0) if m else None

    def sensitive_reason(
        self,
        domain: str,
        entity_id: str,
        *,
        device_class: str | None = None,
        data: dict[str, Any] | None = None,
        friendly_name: str | None = None,
        linked_reason: str | None = UNREAD,
    ) -> str | None:
        s = self.safety
        if domain in s.sensitive_domains:
            return f"le domaine {domain} (serrures, alarme…) est sensible"
        if entity_id in s.sensitive_entities:
            return f"{entity_id} est marqué sensible"
        for text in (entity_id, friendly_name or ""):
            word = self.sensitive_name(text)
            if word:
                return f"« {word} » désigne un appareil sensible"
        if domain == "cover":
            if device_class is None:
                return "type de volet ou de porte inconnu (garage ou portail possible)"
            if device_class in s.sensitive_cover_classes:
                return f"une porte de type {device_class} est sensible"
        if domain == "climate" and data:
            low, high = s.climate_range
            for key in _CLIMATE_KEYS:
                value = data.get(key)
                if isinstance(value, (int, float)) and not low <= value <= high:
                    return f"le chauffage hors de {low:g}–{high:g} °C est sensible"
        if domain in ("script", "scene") and linked_reason is not None:
            return linked_reason
        return None

    def precheck(self, domain: str, service: str, entity_id: str) -> Deny | None:
        """Refus sans rien savoir de l'entité : forme, domaine, service hors liste."""
        m = _ENTITY.match(entity_id or "")
        if not m:
            return Deny("il faut une seule entité précise (domaine.nom)")
        if m.group(1) != domain:
            return Deny(f"{entity_id} n'appartient pas au domaine {domain}")
        if service not in SERVICES.get(domain, set()):
            return Deny(f"le service {domain}.{service} n'est pas permis")
        return None

    def check(
        self,
        domain: str,
        service: str,
        entity_id: str,
        *,
        device_class: str | None = None,
        data: dict[str, Any] | None = None,
        friendly_name: str | None = None,
        linked_reason: str | None = UNREAD,
    ) -> Decision:
        """`linked_reason` (scripts et scènes) : None si leur contenu a été lu et n'a rien de
        sensible, sinon la raison ; par défaut, contenu non lu."""
        malformed = self.precheck(domain, service, entity_id)
        if malformed is not None:
            return malformed
        reason = self.sensitive_reason(
            domain, entity_id, device_class=device_class, data=data,
            friendly_name=friendly_name, linked_reason=linked_reason,
        )
        if reason is not None:
            if entity_id not in self.safety.allowed_entities:
                return Deny(f"{reason} ; cette action est désactivée dans les réglages")
            return NeedsConfirmation(
                self.phrase(domain, service, entity_id, friendly_name, data),
                bool(self.safety.pin),
            )
        if domain in self.safety.allowed_domains or entity_id in self.safety.allowed_entities:
            return Allow()
        return Deny(f"le domaine {domain} n'est pas autorisé dans les réglages")

    def phrase(
        self, domain: str, service: str, entity_id: str, friendly_name: str | None,
        data: dict[str, Any] | None,
    ) -> str:
        """La question de confirmation, écrite par LapiNou (le modèle ne la reformule pas)."""
        name = (friendly_name or entity_id).replace("«", "").replace("»", "")[:60]
        verb = _VERBS.get(service, service)
        extra = ""
        if data and isinstance(data.get("temperature"), (int, float)):
            extra = f" à {data['temperature']:g} degrés"
        what = f"« {name} »"
        if domain == "alarm_control_panel" and not friendly_name:
            what = "l'alarme"
        ask = "Pour confirmer, dis le code secret." if self.safety.pin else (
            "Tu confirmes ? Réponds oui ou non.")
        return f"Action sensible : {verb} {what}{extra}. {ask}"

    def refuses(self, utterance: str) -> bool:
        text = fold(utterance)
        return any(w in _NO for w in text.split()) or any(n in text for n in _NO if " " in n)

    def spoken_digits(self, utterance: str) -> str:
        return "".join(_DIGITS.get(w, re.sub(r"\D", "", w)) for w in fold(utterance).split())

    def confirms(self, utterance: str) -> bool:
        """La réponse vaut-elle confirmation ? « oui » ; avec un PIN, exactement ses chiffres."""
        if self.refuses(utterance):
            return False
        pin = re.sub(r"\D", "", self.safety.pin)
        if pin:
            return self.spoken_digits(utterance) == pin
        text = fold(utterance).replace("d accord", "daccord")
        return any(w in _YES for w in text.split()) or any(y in text for y in _YES if " " in y)
