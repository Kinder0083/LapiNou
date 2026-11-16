# SPDX-License-Identifier: MIT
"""Ce que tous les cerveaux partagent : conversation, outils, événements, persona.

Les messages de `Conversation.messages` suivent le format de l'API Messages d'Anthropic
(rôles `user` / `assistant`, contenu en blocs `text`, `tool_use`, `tool_result`) : c'est le
format commun, que les autres cerveaux traduisent. Un tour se déroule ainsi :

1. l'appelant ajoute la phrase de l'enfant (`add_user_text`) ;
2. `respond` diffuse des `TextDelta` et des `ToolCall`, puis `Done` (ou `Refused`), et
   ajoute lui-même le tour de l'assistant à la conversation ;
3. s'il y a eu des `ToolCall`, l'appelant exécute les outils, ajoute tous les résultats en un
   seul message (`add_tool_results`) et rappelle `respond`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..rabbit.platform import Step

PERSONA_PATH = Path(__file__).resolve().parents[1] / "persona_fr.md"


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    choreography: list[Step] | None = None


@dataclass
class Conversation:
    rabbit_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    memories: list[str] = field(default_factory=list)


@dataclass
class Usage:
    """Consommation d'une réponse, pour le budget."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCall:
    """Un appel d'outil demandé par le modèle.

    `error` est rempli quand les arguments ne respectent pas le schéma de l'outil :
    l'appelant ne doit pas l'exécuter et renvoie `ToolResult(error, is_error=True)`.
    """

    id: str
    name: str
    args: dict[str, Any]
    error: str | None = None


@dataclass
class Done:
    usage: Usage


@dataclass
class Refused:
    """Le modèle a refusé de répondre ; `usage` est facturé quand même."""

    category: str | None
    usage: Usage | None = None


BrainEvent = TextDelta | ToolCall | Done | Refused


class BrainError(Exception):
    """Le fournisseur n'a pas pu répondre (réseau, quota, erreur HTTP) : on passe au suivant."""


class Brain(Protocol):
    name: str

    def respond(
        self, conv: Conversation, tools: list[ToolSpec]
    ) -> AsyncIterator[BrainEvent]: ...


# --- Conversation ------------------------------------------------------------------------


def add_user_text(conv: Conversation, text: str) -> None:
    conv.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})


def add_assistant_text(conv: Conversation, text: str) -> None:
    conv.messages.append({"role": "assistant", "content": [{"type": "text", "text": text}]})


def add_tool_results(conv: Conversation, results: list[tuple[str, ToolResult]]) -> None:
    """Tous les résultats d'un même tour dans un seul message `user` (appels parallèles)."""
    conv.messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for call_id, result in results
            ],
        }
    )


def content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content or [])


def message_text(message: dict[str, Any]) -> str:
    texts = (b["text"] for b in content_blocks(message) if b.get("type") == "text")
    return " ".join(texts).strip()


def last_user_text(conv: Conversation) -> str:
    """La dernière phrase de l'enfant (les messages de résultats d'outils sont sautés)."""
    for message in reversed(conv.messages):
        if message.get("role") == "user":
            text = message_text(message)
            if text:
                return text
    return ""


def awaiting_tool_results(conv: Conversation) -> bool:
    """Vrai si le dernier message rend des résultats d'outils (suite d'un même tour)."""
    if not conv.messages or conv.messages[-1].get("role") != "user":
        return False
    blocks = content_blocks(conv.messages[-1])
    return bool(blocks) and all(b.get("type") == "tool_result" for b in blocks)


# --- Persona et contexte -----------------------------------------------------------------

_COMMENT = re.compile(r"<!--.*?-->\s*", re.S)


def load_persona(path: Path = PERSONA_PATH) -> str:
    """Le prompt système, sans le commentaire de licence."""
    return _COMMENT.sub("", path.read_text(encoding="utf-8")).strip()


_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
_MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def french_date(now: dt.datetime) -> str:
    return (
        f"Nous sommes le {_DAYS[now.weekday()]} {now.day} {_MONTHS[now.month - 1]} "
        f"{now.year}, il est {now.hour} h {now.minute:02d}."
    )


def context_text(conv: Conversation, now: dt.datetime) -> str:
    """La partie changeante du prompt système (date, souvenirs), placée après la persona."""
    lines = [french_date(now)]
    if conv.memories:
        lines.append("Ce que tu sais de la famille :")
        lines.extend(f"- {m}" for m in conv.memories)
    return "\n".join(lines)


# --- Validation des arguments d'outil ---------------------------------------------------

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _check(schema: dict[str, Any], value: Any, where: str) -> str | None:
    expected = schema.get("type")
    if expected is not None:
        names = expected if isinstance(expected, list) else [expected]
        ok = False
        for name in names:
            types = _TYPES.get(name)
            if types is None:
                ok = True  # type inconnu : on ne juge pas
            elif isinstance(value, types) and not (
                isinstance(value, bool) and name in ("integer", "number")
            ):
                ok = True
        if not ok:
            return f"{where} : type attendu {expected}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where} : valeur hors de {schema['enum']}"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{where} : champ obligatoire « {key} » absent"
        for key, item in value.items():
            if key in props:
                problem = _check(props[key], item, f"{where}.{key}")
                if problem:
                    return problem
            elif schema.get("additionalProperties") is False:
                return f"{where} : champ inconnu « {key} »"
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            problem = _check(schema["items"], item, f"{where}[{i}]")
            if problem:
                return problem
    return None


def validate_args(schema: dict[str, Any], args: Any) -> str | None:
    """None si `args` respecte le schéma (sous-ensemble courant de JSON Schema), sinon l'erreur."""
    if not isinstance(args, dict):
        return "arguments : objet JSON attendu"
    return _check({"type": "object", **schema}, args, "arguments")


def is_strict_compatible(schema: dict[str, Any]) -> bool:
    """Le mode `strict` exige additionalProperties false et toutes les propriétés requises."""
    props = schema.get("properties", {})
    return schema.get("additionalProperties") is False and set(schema.get("required", [])) == set(
        props
    )
