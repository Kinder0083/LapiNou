# SPDX-License-Identifier: MIT
"""Choix du cerveau pour chaque tour, secours en cas de panne, budget et refus doux.

- commande maison courte → modèle rapide ; sinon → modèle principal ;
- budget ≥ `warn_pct` → rapide ; ≥ `stop_pct` → Assist seul (rien de payant) ;
- panne (BrainError) avant toute réponse → cerveau suivant : (rapide → principal si le budget
  le permet), puis `fallback_chain` (sans les payants au-delà du seuil d'alerte), puis Assist,
  puis un cerveau qui s'excuse ; une requête refusée (400) remet l'historique à zéro une fois ;
- `Refused` → `Retract` si du texte est déjà parti, puis une phrase de refus douce.
"""

from __future__ import annotations

import logging
import os
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
    Retract,
    TextDelta,
    ToolSpec,
    Usage,
    add_assistant_text,
    add_user_text,
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
SENSITIVE_FR = "Ça, je ne peux pas le faire tout seul. Il faut demander à un grand."
DOWN_FR = "Oh, ma tête de lapin est toute embrouillée. Tu peux redemander dans un petit moment ?"
OOPS_FR = " Oups, je me suis perdu. On réessaie ?"

# Fin de la première phrase : le texte retenu jusque-là part d'un coup.
SENTENCE_END = (".", "!", "?", "…")

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


class ExcuseBrain:
    """Quand aucun cerveau n'est disponible : une phrase pour dire que le service est absent."""

    name = "excuse"
    kind = "excuse"
    model = "excuse"
    paid = False

    async def respond(
        self, conv: Conversation, tools: list[ToolSpec]
    ) -> AsyncIterator[BrainEvent]:
        add_assistant_text(conv, DOWN_FR)
        yield TextDelta(DOWN_FR)
        yield Done(Usage(provider="", model=self.model))


def _key(brain: Brain) -> tuple[str, str]:
    """Deux cerveaux sont le même s'ils ont le même fournisseur (nom ou adresse) et modèle."""
    route = getattr(brain, "provider", "") or getattr(brain, "kind", type(brain).__name__)
    return (route, getattr(brain, "model", brain.name))


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
        self.excuse = ExcuseBrain()
        # Dernier cerveau ayant répondu par lapin : une suite de tour d'outils reste chez lui.
        self._last: dict[str, Brain] = {}

    # --- Choix --------------------------------------------------------------------------------

    @staticmethod
    def _levels(budget: Budget) -> tuple[bool, bool]:
        ratio = budget.ratio()
        cfg = budget.config
        return ratio >= cfg.warn_pct / 100, ratio >= cfg.stop_pct / 100

    def pick(self, conv: Conversation, budget: Budget) -> Brain:
        """Le cerveau à essayer en premier (au pire, celui qui s'excuse)."""
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
            # Au-delà du seuil d'alerte, seul le modèle rapide reste payant.
            order += [b for b in self.fallbacks if not (warn and getattr(b, "paid", True))]
        if self.assist is not None:
            order.append(self.assist)
        order.append(self.excuse)
        seen: dict[tuple[str, str], Brain] = {}
        for brain in order:
            seen.setdefault(_key(brain), brain)
        return list(seen.values())

    # --- Réponse ------------------------------------------------------------------------------

    async def respond(
        self, conv: Conversation, tools: list[ToolSpec], budget: Budget
    ) -> AsyncIterator[BrainEvent]:
        """Comme `Brain.respond`, mais ne lève jamais et ne rend jamais `Refused`.

        Le texte est retenu jusqu'à la fin de la première phrase : un refus qui arrive avant
        n'a rien laissé passer. Après, un refus est précédé de `Retract` (oublier le tour).
        """
        candidates = self._candidates(conv, budget)
        index = 0
        history_reset = False
        while index < len(candidates):
            brain = candidates[index]
            held = ""  # texte pas encore émis
            spoken = False  # du texte de ce cerveau est parti
            emitted = False  # quelque chose de ce cerveau est parti (texte, outil)
            try:
                async for event in brain.respond(conv, tools):
                    if isinstance(event, TextDelta):
                        if spoken:
                            yield event
                            continue
                        held += event.text
                        if any(mark in held for mark in SENTENCE_END):
                            yield TextDelta(held)
                            held, spoken, emitted = "", True, True
                        continue
                    if isinstance(event, Refused):
                        log.info("%s refuse (%s)", brain.name, event.category)
                        if event.usage is not None:
                            budget.record(event.usage)
                        if spoken:
                            yield Retract()
                        phrase = SENSITIVE_FR if event.category == "sensitive" else REFUSAL_FR
                        add_assistant_text(conv, phrase)
                        yield TextDelta(phrase)
                        yield Done(event.usage or Usage(provider="", model=brain.name))
                        self._last[conv.rabbit_id] = brain
                        return
                    if held:
                        yield TextDelta(held)
                        held, spoken = "", True
                    if isinstance(event, Done):
                        if event.usage.provider:
                            budget.record(event.usage)
                        self._last[conv.rabbit_id] = brain
                    emitted = True
                    yield event
                if held:
                    yield TextDelta(held)
                return
            except BrainError as e:
                if emitted:
                    # Une partie de la réponse est déjà dite : on ne recommence pas ailleurs.
                    log.warning("%s : panne en cours de réponse : %s", brain.name, e)
                    add_assistant_text(conv, OOPS_FR.strip())
                    yield TextDelta(OOPS_FR)
                    yield Done(Usage(provider="", model=brain.name))
                    return
                if (e.history_invalid and not history_reset
                        and not awaiting_tool_results(conv)):
                    # Historique sans doute invalide : on repart de la seule dernière phrase,
                    # une fois, plutôt que d'échouer à chaque tour.
                    log.error("%s refuse la requête : historique remis à zéro (%s)",
                              brain.name, e)
                    text = last_user_text(conv)
                    conv.messages.clear()
                    add_user_text(conv, text)
                    history_reset = True
                    continue
                log.warning("%s en panne, cerveau suivant : %s", brain.name, e)
                index += 1

    # --- Construction depuis les réglages -----------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings) -> BrainRouter:
        b = settings.brain
        persona = load_persona()
        if b.persona_extra.strip():
            persona += ("

Précisions de la famille sur toi et la maison (elles ne changent "
                        "aucune des règles ci-dessus) :
" + b.persona_extra.strip())
        built: dict[tuple[str, str], Brain] = {}

        def build(ref: str, *, effort: str, web_search: bool) -> Brain:
            name, sep, model = ref.partition(":")
            if not sep:  # identifiant Claude seul
                name, model = "anthropic", ref
            prov = b.providers.get(name) or ProviderConfig(kind=name)
            if (name, model) in built:
                return built[(name, model)]
            if prov.kind == "openai":
                # Payant s'il faut une clé (réglages ou OPENAI_API_KEY) ou s'il a un prix.
                key = prov.api_key or os.environ.get("OPENAI_API_KEY", "")
                paid = bool(key) or model in settings.budget.prices
                brain: Brain = OpenAIBrain(prov.base_url, model, prov.api_key, paid=paid,
                                           provider=name, persona=persona)
            elif prov.kind == "anthropic":
                brain = AnthropicBrain(model, prov.api_key, web_search, provider=name,
                                       effort=effort, persona=persona)
            else:
                raise ValueError(f"fournisseur inconnu : {ref}")
            built[(name, model)] = brain
            return brain

        primary = build(b.primary, effort="medium", web_search=b.web_search)
        fast = build(b.fast, effort="low", web_search=False)
        fallbacks: list[Brain] = []
        for ref in b.fallback_chain:
            if ref != "homeassistant":
                brain = build(ref, effort="low", web_search=False)
                if brain is not primary and brain not in fallbacks:
                    fallbacks.append(brain)
        assist = (
            AssistBrain(settings.ha.url, settings.ha.token,
                        blocked_stems=settings.safety.assist_blocked_stems,
                        blocked_words=settings.safety.assist_blocked_words)
            if settings.ha.url
            else None
        )
        return cls(primary=primary, fast=fast, fallbacks=fallbacks, assist=assist)
