# SPDX-License-Identifier: MIT
"""Choix du cerveau pour chaque tour, secours en cas de panne, budget et refus doux.

- commande maison courte → modèle rapide ; sinon → modèle principal ;
- budget ≥ `warn_pct` → rapide ; ≥ `stop_pct` → Assist seul (rien de payant) ;
- panne (BrainError) avant toute réponse → cerveau suivant : (rapide → principal si le budget
  le permet), puis `fallback_chain`, puis Assist, puis une phrase d'excuse ;
- `Refused` → une phrase de refus douce, ajoutée à la conversation.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator

from ..config import ProviderConfig, Settings
from .anthropic_brain import AnthropicBrain
from .assist_brain import AssistBrain
from .base import (
    Brain,
    BrainError,
    BrainEvent,
    Conversation,
    Done,
    Refused,
    TextDelta,
    ToolSpec,
    Usage,
    add_assistant_text,
    awaiting_tool_results,
    last_user_text,
    load_persona,
)
from .budget import Budget
from .openai_brain import OpenAIBrain

log = logging.getLogger(__name__)

REFUSAL_FR = (
    "Ça, je préfère ne pas en parler. "
    "Tu veux plutôt que je te raconte une histoire, ou que je te dise une devinette ?"
)
DOWN_FR = "Oh, ma tête de lapin est toute embrouillée. Tu peux redemander dans un petit moment ?"
OOPS_FR = " Oups, je me suis perdu. On réessaie ?"

SIMPLE_MAX_WORDS = 10
# Débuts de verbes d'une commande maison (impératif ou infinitif).
_COMMAND_STEMS = (
    "allum", "étein", "etein", "éteign", "eteign", "ferm", "ouvr", "monte", "baiss",
    "mets", "mett", "lance", "arrêt", "arret", "augment", "diminu", "règl", "regl",
    "coup", "démarr", "demarr", "stop", "pause", "remets", "baisse", "active", "désactiv",
)
_QUESTION = re.compile(r"\b(pourquoi|comment|raconte|explique|qui|quand|combien|c'est quoi)\b")
_WORD = re.compile(r"[\w'’-]+")


def is_simple(conv: Conversation) -> bool:
    """Commande maison courte (« allume le salon ») : le modèle rapide suffit."""
    text = last_user_text(conv).lower()
    words = _WORD.findall(text)
    if not words or len(words) > SIMPLE_MAX_WORDS or _QUESTION.search(text) or "?" in text:
        return False
    return any(w.startswith(_COMMAND_STEMS) for w in words[:3])


class BrainRouter:
    def __init__(
        self,
        primary: Brain,
        fast: Brain,
        fallbacks: list[Brain],
        assist: Brain | None,
    ) -> None:
        self.primary = primary
        self.fast = fast
        self.fallbacks = fallbacks
        self.assist = assist
        # Dernier cerveau ayant répondu par lapin : une suite de tour d'outils reste chez lui.
        self._last: dict[str, Brain] = {}

    # --- Choix --------------------------------------------------------------------------------

    @staticmethod
    def _levels(budget: Budget) -> tuple[bool, bool]:
        ratio = budget.ratio()
        cfg = budget.config
        return ratio >= cfg.warn_pct / 100, ratio >= cfg.stop_pct / 100

    def pick(self, conv: Conversation, budget: Budget) -> Brain:
        return self._candidates(conv, budget)[0]

    def _candidates(self, conv: Conversation, budget: Budget) -> list[Brain]:
        warn, stop = self._levels(budget)
        if stop:
            order: list[Brain] = []
        elif warn:
            order = [self.fast]
        elif is_simple(conv):
            order = [self.fast, self.primary]
        else:
            order = [self.primary]
        if not stop:
            previous = self._last.get(conv.rabbit_id)
            if awaiting_tool_results(conv) and previous in order:
                order.remove(previous)
                order.insert(0, previous)
            order += self.fallbacks
        if self.assist is not None:
            order.append(self.assist)
        seen: list[Brain] = []
        for brain in order:
            if brain not in seen:
                seen.append(brain)
        return seen

    # --- Réponse ------------------------------------------------------------------------------

    async def respond(
        self, conv: Conversation, tools: list[ToolSpec], budget: Budget
    ) -> AsyncIterator[BrainEvent]:
        """Comme `Brain.respond`, mais ne lève jamais et ne rend jamais `Refused`."""
        for brain in self._candidates(conv, budget):
            started = False
            try:
                async for event in brain.respond(conv, tools):
                    if isinstance(event, Refused):
                        log.info("%s refuse (%s)", brain.name, event.category)
                        if event.usage is not None:
                            budget.record(event.usage)
                        add_assistant_text(conv, REFUSAL_FR)
                        yield TextDelta(REFUSAL_FR)
                        yield Done(event.usage or Usage(provider="", model=brain.name))
                        self._last[conv.rabbit_id] = brain
                        return
                    if isinstance(event, Done):
                        budget.record(event.usage)
                        self._last[conv.rabbit_id] = brain
                    started = True
                    yield event
                return
            except BrainError as e:
                if started:
                    # Une partie de la réponse est déjà dite : on ne recommence pas ailleurs.
                    log.warning("%s : panne en cours de réponse : %s", brain.name, e)
                    add_assistant_text(conv, OOPS_FR.strip())
                    yield TextDelta(OOPS_FR)
                    yield Done(Usage(provider="", model=brain.name))
                    return
                log.warning("%s en panne, cerveau suivant : %s", brain.name, e)
        add_assistant_text(conv, DOWN_FR)
        yield TextDelta(DOWN_FR)
        yield Done(Usage(provider="", model="aucun"))

    # --- Construction depuis les réglages -----------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings) -> BrainRouter:
        persona = load_persona()
        b = settings.brain

        def build(ref: str, *, effort: str, web_search: bool) -> Brain:
            name, sep, model = ref.partition(":")
            if not sep:  # identifiant Claude seul
                name, model = "anthropic", ref
            prov = b.providers.get(name) or ProviderConfig(kind=name)
            if prov.kind == "openai":
                return OpenAIBrain(prov.base_url, model, prov.api_key, persona=persona)
            if prov.kind != "anthropic":
                raise ValueError(f"fournisseur inconnu : {ref}")
            return AnthropicBrain(model, prov.api_key, web_search, effort=effort,
                                  persona=persona)

        primary = build(b.primary, effort="medium", web_search=b.web_search)
        fast = build(b.fast, effort="low", web_search=False)
        fallbacks = [build(ref, effort="low", web_search=False)
                     for ref in b.fallback_chain if ref != "homeassistant"]
        assist = AssistBrain(settings.ha.url, settings.ha.token) if settings.ha.url else None
        return cls(primary=primary, fast=fast, fallbacks=fallbacks, assist=assist)
