# SPDX-License-Identifier: MIT
"""Politique des actions Home Assistant : appliquée par LapiNou avant tout appel à ha-mcp.

Sensibles par défaut : `lock.*`, `alarm_control_panel.*`, `cover` de classe garage ou portail,
les entités marquées sensibles, `climate` réglé hors de la plage permise. Une action sensible
est refusée, sauf si son entité figure dans `allowed_entities` : elle demande alors une
confirmation vocale (et le code PIN s'il est réglé). Le reste n'est permis que dans les
domaines de `allowed_domains`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..config import SafetyConfig
from .base import fold

_ENTITY = re.compile(r"^([a-z0-9_]+)\.[a-z0-9_]+$")
_SERVICE = re.compile(r"^[a-z0-9_]+$")
_CLIMATE_KEYS = ("temperature", "target_temp_low", "target_temp_high")
_YES = {"oui", "ouais", "ok", "okay", "daccord", "confirme", "confirmer", "vas y", "vasy",
        "bien sur", "exactement", "yes"}
_NO = {"non", "pas", "annule", "annuler", "stop", "arrete", "surtout pas", "jamais"}
_DIGITS = {"zero": "0", "un": "1", "une": "1", "deux": "2", "trois": "3", "quatre": "4",
           "cinq": "5", "six": "6", "sept": "7", "huit": "8", "neuf": "9"}


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class NeedsConfirmation:
    phrase: str  # la question à poser, à voix haute
    needs_pin: bool = False


@dataclass(frozen=True)
class Deny:
    reason: str


Decision = Allow | NeedsConfirmation | Deny


class Policy:
    def __init__(self, safety: SafetyConfig) -> None:
        self.safety = safety

    def sensitive_reason(
        self, domain: str, entity_id: str, device_class: str | None, data: dict[str, Any] | None
    ) -> str | None:
        s = self.safety
        if domain in s.sensitive_domains:
            return f"le domaine {domain} (serrures, alarme…) est sensible"
        if entity_id in s.sensitive_entities:
            return f"{entity_id} est marqué sensible"
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
        return None

    def check(
        self,
        domain: str,
        service: str,
        entity_id: str,
        *,
        device_class: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Decision:
        m = _ENTITY.match(entity_id or "")
        if not m:
            return Deny("il faut une seule entité précise (domaine.nom)")
        if m.group(1) != domain:
            return Deny(f"{entity_id} n'appartient pas au domaine {domain}")
        if not _SERVICE.match(service or ""):
            return Deny(f"service invalide : {service!r}")
        reason = self.sensitive_reason(domain, entity_id, device_class, data)
        if reason is not None:
            if entity_id not in self.safety.allowed_entities:
                return Deny(f"{reason} ; cette action est désactivée dans les réglages")
            needs_pin = bool(self.safety.pin)
            phrase = f"C'est une action sensible ({domain}.{service} sur {entity_id}). " + (
                "Pour confirmer, dis le code secret." if needs_pin else "Tu confirmes ?"
            )
            return NeedsConfirmation(phrase, needs_pin)
        if domain in self.safety.allowed_domains or entity_id in self.safety.allowed_entities:
            return Allow()
        return Deny(f"le domaine {domain} n'est pas autorisé dans les réglages")

    def confirms(self, utterance: str) -> bool:
        """La réponse vaut-elle confirmation ? « oui » ; avec un PIN, le PIN dit en entier."""
        text = fold(utterance).replace("d accord", "daccord")
        words = text.split()
        if any(w in _NO for w in words) or any(n in text for n in _NO if " " in n):
            return False
        pin = re.sub(r"\D", "", self.safety.pin)
        if pin:
            digits = "".join(_DIGITS.get(w, re.sub(r"\D", "", w)) for w in words)
            return pin in digits
        return any(w in _YES for w in words) or any(y in text for y in _YES if " " in y)
