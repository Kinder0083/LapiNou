# SPDX-License-Identifier: MIT
"""Ce que les outils partagent : contexte d'appel, outil, boîte à outils, annonceur.

Chaque outil expose `spec() -> ToolSpec` (présenté au cerveau) et
`async call(args, ctx) -> ToolResult`. Les schémas sont stricts (toutes les propriétés
requises, `additionalProperties: false`) ; un champ facultatif est typé `[..., "null"]`.
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..brain.base import ToolResult, ToolSpec, validate_args

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolContext:
    """Qui appelle : le lapin qui a entendu la demande."""

    rabbit_id: str


class Announcer(Protocol):
    """Fait dire une phrase à un lapin (implémenté par l'orchestrateur, Tâche 22)."""

    def say(self, rabbit_id: str, text: str) -> Awaitable[None]: ...


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)

    async def call(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await self.handler(args, ctx)


def strict_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    out = dict(schema)
    out["type"] = [schema["type"], "null"]
    if "enum" in schema:
        out["enum"] = [*schema["enum"], None]
    return out


def error(text: str) -> ToolResult:
    return ToolResult(text, is_error=True)


class ToolBox:
    """Les outils actifs, par nom ; valide les arguments et ne laisse jamais filer d'exception."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"outil en double : {tool.name}")
            self._tools[tool.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return error(f"Outil inconnu ou désactivé : {name}.")
        problem = validate_args(tool.input_schema, args)
        if problem:
            return error(problem)
        try:
            return await tool.call(args, ctx)
        except Exception:
            log.exception("outil %s en échec", name)
            return error(f"L'outil {name} a eu un problème ; dis-le simplement.")


# --- Texte ------------------------------------------------------------------------------


def fold(text: str) -> str:
    """Minuscules, sans accents, espaces simples (pour comparer des noms dits à voix haute)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", bare).split())


_LEADING = re.compile(r"^(?:(?:dans|sur|a|au|aux|en|chez|de|du|des|la|le|les|l|d)\s+)+")


def best_match(query: str, names: Sequence[str], cutoff: float = 0.75) -> int | None:
    """Indice du nom le plus proche de `query` (« dans la cuisine » → « Cuisine »), ou None."""
    q = _LEADING.sub("", fold(query))
    if not q:
        return None
    folded = [fold(n) for n in names]
    for i, name in enumerate(folded):
        if name == q:
            return i
    words = set(q.split())
    for i, name in enumerate(folded):
        if len(q) >= 3 and (q in name or name in q or words <= set(name.split())):
            return i
    scores = [difflib.SequenceMatcher(None, q, name).ratio() for name in folded]
    if scores:
        best = max(range(len(scores)), key=scores.__getitem__)
        if scores[best] >= cutoff:
            return best
    return None
