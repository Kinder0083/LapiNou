# SPDX-License-Identifier: MIT
"""Configuration du serveur : config.toml + secrets.toml (0600, séparé)."""

from __future__ import annotations

import copy
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

DEFAULT_CONFIG_PATH = Path("/etc/lapinou/config.toml")
SECRETS_NAME = "secrets.toml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ServerConfig(_Section):
    platform_port: int = 9123
    mic_port: int = 9300
    http_port: int = 8080
    tts_port: int = 8081
    asr_port: int = 8443


class MqttConfig(_Section):
    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""  # secret


class HaConfig(_Section):
    url: str = ""
    token: str = ""  # secret


class LmsConfig(_Section):
    host: str = ""
    port: int = 9000


class ProviderConfig(_Section):
    kind: str = "anthropic"  # anthropic | openai
    base_url: str = ""
    api_key: str = ""  # secret


class BrainConfig(_Section):
    primary: str = "claude-opus-5-5"
    fast: str = "claude-haiku-5-5"
    # Fournisseurs essayés si le cerveau choisi échoue : « homeassistant » (Assist), un
    # identifiant de modèle Claude, ou « fournisseur:modèle » (clé de `providers`).
    fallback_chain: list[str] = Field(default_factory=lambda: ["homeassistant"])
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    web_search: bool = True  # recherche web (outil serveur) pour le modèle principal


class ModelPrice(_Section):
    """Prix en dollars par million de jetons."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0


def _default_prices() -> dict[str, ModelPrice]:
    # Écriture en cache (5 min) : 1,25 x l'entrée ; lecture : 0,1 x. Haiku 5.5 : prix des
    # requêtes jusqu'à 100 000 jetons d'entrée.
    return {
        "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
        "claude-haiku-5-5": ModelPrice(
            input=0.10, output=0.50, cache_read=0.01, cache_write=0.125
        ),
        # Cibles possibles du repli côté serveur.
        "claude-opus-5": ModelPrice(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25),
        "claude-opus-4-8": ModelPrice(input=5.0, output=25.0, cache_read=0.50, cache_write=6.25),
        "claude-sonnet-5": ModelPrice(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
    }


class BudgetConfig(_Section):
    monthly_usd: float = 20.0
    warn_pct: int = 80
    stop_pct: int = 100
    # Modèle absent de la table : gratuit (modèle local, Assist), sauf un modèle Claude, facturé
    # au prix du modèle demandé (ou du plus cher de la table).
    prices: dict[str, ModelPrice] = Field(default_factory=_default_prices)
    web_search_per_1k: float = 10.0


class VoiceConfig(_Section):
    # Détection de « Lapinou » : « asr » (transcription Parakeet, principale, R23), « oww »
    # (classifieur openWakeWord seul) ou « both » (openWakeWord garde la transcription).
    wake_method: Literal["asr", "oww", "both"] = "asr"
    tts_engine: str = "piper"
    voice: str = "fr_FR-siwis-medium"
    wake_threshold: float = 0.9  # validé : tools/train_wakeword/README.md
    # Classifieur openWakeWord entraîné localement (tools/train_wakeword) ; vide = absent.
    # Non distribué : il hérite des licences NC/SA de ses données d'entraînement.
    wake_model: str = ""
    vad_silence_ms: int = 700
    followup_s: int = 5


class SafetyConfig(_Section):
    sensitive_domains: list[str] = Field(
        default_factory=lambda: ["lock", "alarm_control_panel"]
    )
    # `cover` n'est sensible que pour ces device_class (garage, portail).
    sensitive_cover_classes: list[str] = Field(default_factory=lambda: ["garage", "gate"])
    # `climate` hors de cette plage (°C) est sensible.
    climate_range: list[float] = Field(default_factory=lambda: [16.0, 24.0])
    allowed_entities: list[str] = Field(default_factory=list)
    pin: str = ""  # secret
    # Assist (repli) ne passe pas par la politique d'actions sensibles : toute phrase qui cite
    # l'un de ces mots (singulier ou pluriel) lui est refusée. Dans Home Assistant, n'exposez pas
    # à Assist les entités sensibles (serrures, alarme, portes, garage, chauffage, four...).
    assist_blocked_keywords: list[str] = Field(
        default_factory=lambda: [
            "porte", "serrure", "verrou", "alarme", "garage", "portail", "chauffage",
            "radiateur", "thermostat", "four", "plaque",
        ]
    )


class MemoryConfig(_Section):
    enabled: bool = True
    max_items: int = 50


class LogsConfig(_Section):
    retention_days: int = 7


class RabbitConfig(_Section):
    name: str = ""
    default_lms_player: str = ""


class Settings(_Section):
    server: ServerConfig = Field(default_factory=ServerConfig)
    mqtt: MqttConfig = Field(default_factory=MqttConfig)
    ha: HaConfig = Field(default_factory=HaConfig)
    lms: LmsConfig = Field(default_factory=LmsConfig)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    logs: LogsConfig = Field(default_factory=LogsConfig)
    rabbits: dict[str, RabbitConfig] = Field(default_factory=dict)

    # Emplacement du config.toml d'origine (utilisé par save_settings).
    _path: Path | None = PrivateAttr(default=None)

    @property
    def path(self) -> Path | None:
        return self._path


def _secrets_path(config_path: Path) -> Path:
    return config_path.with_name(SECRETS_NAME)


def _take(section: dict[str, Any], key: str) -> dict[str, Any]:
    value = section.pop(key, "")
    return {key: value} if value else {}


def _split_secrets(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Sépare un dict de réglages en (public, secrets)."""
    public = copy.deepcopy(data)
    secrets: dict[str, Any] = {}
    for name, key in (("mqtt", "password"), ("ha", "token"), ("safety", "pin")):
        taken = _take(public[name], key)
        if taken:
            secrets[name] = taken
    providers: dict[str, Any] = {}
    for pname, prov in public["brain"]["providers"].items():
        taken = _take(prov, "api_key")
        if taken:
            providers[pname] = taken
    if providers:
        secrets["brain"] = {"providers": providers}
    return public, secrets


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def load_settings(path: Path = DEFAULT_CONFIG_PATH) -> Settings:
    """Charge config.toml puis fusionne secrets.toml (même dossier)."""
    path = Path(path)
    data = _merge(_read_toml(path), _read_toml(_secrets_path(path)))
    settings = Settings.model_validate(data)
    settings._path = path
    return settings


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def save_settings(s: Settings) -> None:
    """Écrit config.toml (sans secrets) et secrets.toml (0600), atomiquement."""
    if s._path is None:
        raise ValueError("Settings sans chemin : utiliser load_settings()")
    public, secrets = _split_secrets(s.model_dump(mode="python"))
    _atomic_write(_secrets_path(s._path), tomli_w.dumps(secrets).encode(), 0o600)
    _atomic_write(s._path, tomli_w.dumps(public).encode(), 0o644)
