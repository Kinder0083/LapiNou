# SPDX-License-Identifier: MIT
"""Histoires et jeux : une consigne courte pour le cerveau, et une chorégraphie oreilles/LED.

Le cerveau raconte lui-même ; l'outil cadre (doux, court, adapté aux enfants) et fournit les
gestes du lapin. Devinettes et questions de quiz viennent d'une petite liste intégrée, pour
que la bonne réponse soit sûre.
"""

from __future__ import annotations

import random
from collections import deque
from typing import Any

from ..brain.base import ToolResult
from ..rabbit.platform import Step
from .base import Tool, ToolContext, nullable, strict_schema

# Oreilles : 0 à 16 (17 pas par tour), 0 = droites.
STORY_STEPS = [
    Step(ears=(4, 4), led="6040FF", pulse="fade", wait_ms=600),
    Step(ears=(0, 0), wait_ms=300),
]
QUIZ_STEPS = [
    Step(ears=(8, 0), led="FFB000", pulse="pulse", wait_ms=400),
    Step(ears=(0, 8), wait_ms=400),
    Step(ears=(0, 0), led="FFB000"),
]
RIDDLE_STEPS = [
    Step(ears=(3, 13), led="00C0A0", pulse="pulse", wait_ms=500),
    Step(ears=(0, 0), led="00C0A0"),
]

RIDDLES = [
    ("Je suis plein de trous mais je garde l'eau. Qui suis-je ?", "une éponge"),
    ("Plus j'ai de gardiens, moins je suis gardé. Qui suis-je ?", "un secret"),
    ("J'ai des aiguilles mais je ne pique pas. Qui suis-je ?", "une horloge"),
    ("Je monte et je descends sans jamais bouger. Qui suis-je ?", "un escalier"),
    ("Je suis jaune, je brille dans le ciel le jour et je me cache la nuit. Qui suis-je ?",
     "le soleil"),
    ("J'ai un lit mais je ne dors jamais. Qui suis-je ?", "une rivière"),
    ("Je suis blanc, je tombe du ciel en hiver et je fonds au soleil. Qui suis-je ?",
     "la neige"),
    ("J'ai des dents mais je ne mange pas. Qui suis-je ?", "un peigne"),
]

QUIZ = [
    ("Combien de pattes a une araignée ?", "huit"),
    ("Quel animal fait « coin-coin » ?", "le canard"),
    ("De quelle couleur est le ciel quand il fait beau ?", "bleu"),
    ("Quel est le plus grand animal du monde ?", "la baleine bleue"),
    ("Combien de jours y a-t-il dans une semaine ?", "sept"),
    ("Quelle planète habitons-nous ?", "la Terre"),
    ("Quel fruit jaune les singes adorent-ils ?", "la banane"),
    ("Combien de couleurs a un arc-en-ciel ?", "sept"),
]


class StoryTools:
    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self._recent: dict[str, deque[int]] = {"riddle": deque(maxlen=4),
                                               "quiz": deque(maxlen=4)}

    def _pick(self, kind: str, items: list[tuple[str, str]]) -> tuple[str, str]:
        recent = self._recent[kind]
        choices = [i for i in range(len(items)) if i not in recent] or list(range(len(items)))
        index = self.rng.choice(choices)
        recent.append(index)
        return items[index]

    def tools(self) -> list[Tool]:
        return [
            Tool(
                "start_story",
                "À appeler quand un enfant demande une histoire : donne la consigne pour la "
                "raconter et fait bouger le lapin. Ensuite, raconte l'histoire toi-même.",
                strict_schema({"theme": nullable({
                    "type": "string", "description": "Le thème demandé, ou null."})}),
                self._story,
            ),
            Tool(
                "quiz",
                "À appeler quand un enfant veut jouer à un quiz : renvoie une question et sa "
                "réponse. Pose la question, attends la réponse, puis dis si c'est juste.",
                strict_schema({}),
                self._quiz,
            ),
            Tool(
                "riddle",
                "À appeler quand un enfant veut une devinette : renvoie une devinette et sa "
                "réponse. Pose-la sans donner la réponse ; donne un indice s'il sèche.",
                strict_schema({}),
                self._riddle,
            ),
        ]

    async def _story(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        theme = " ".join((args["theme"] or "").split())[:80] or "une aventure de Lapinou"
        return ToolResult(
            f"Raconte maintenant une histoire courte (6 à 10 phrases simples) sur le thème : "
            f"{theme}. Ton doux et joyeux, rien qui fasse peur ni de violence, une fin "
            "heureuse ; termine en demandant si l'enfant veut une suite.",
            choreography=list(STORY_STEPS),
        )

    async def _quiz(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question, answer = self._pick("quiz", QUIZ)
        return ToolResult(
            f"Question à poser : « {question} » (réponse : {answer}, ne la dis pas avant la "
            "réponse de l'enfant ; félicite chaque essai).",
            choreography=list(QUIZ_STEPS),
        )

    async def _riddle(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        riddle, answer = self._pick("riddle", RIDDLES)
        return ToolResult(
            f"Devinette à poser : « {riddle} » (réponse : {answer}, ne la dis pas tout de "
            "suite).",
            choreography=list(RIDDLE_STEPS),
        )
