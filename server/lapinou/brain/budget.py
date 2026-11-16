# SPDX-License-Identifier: MIT
"""Budget mensuel des cerveaux payants, cumulé dans la table `usage` (mois, fournisseur, modèle)."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from dataclasses import replace

from ..config import BudgetConfig, ModelPrice
from ..db import Database
from .base import Usage

log = logging.getLogger(__name__)

MTOK = 1_000_000


class Budget:
    def __init__(
        self,
        db: Database,
        config: BudgetConfig,
        clock: Callable[[], dt.datetime] = dt.datetime.now,
    ) -> None:
        self.db = db
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        self._unpriced: set[str] = set()

    def month(self) -> str:
        return self._clock().strftime("%Y-%m")

    def price(self, model: str) -> ModelPrice | None:
        """Le prix du modèle ; un identifiant daté (« modèle-20261101 ») prend celui du préfixe."""
        prices = self.config.prices
        if model in prices:
            return prices[model]
        for name in sorted(prices, key=len, reverse=True):
            if model.startswith(name + "-"):
                return prices[name]
        return None

    def _price_for(self, usage: Usage) -> ModelPrice | None:
        price = self.price(usage.model)
        if price is not None or usage.provider != "anthropic":
            if price is None and usage.model not in self._unpriced and (
                usage.input_tokens or usage.output_tokens
            ):
                self._unpriced.add(usage.model)
                log.info("pas de prix pour %s/%s : compté gratuit", usage.provider, usage.model)
            return price
        # Modèle Claude inconnu (nouvelle cible de repli) : jamais gratuit.
        price = self.price(usage.requested_model) if usage.requested_model else None
        if price is None and self.config.prices:
            price = max(self.config.prices.values(), key=lambda p: p.input + p.output)
        if usage.model not in self._unpriced:
            self._unpriced.add(usage.model)
            log.warning("pas de prix pour %s : facturé comme %s", usage.model,
                        usage.requested_model or "le modèle le plus cher")
        return price

    def cost(self, usage: Usage) -> float:
        """Coût d'une consommation simple (sans `iterations`)."""
        usd = usage.web_searches * self.config.web_search_per_1k / 1000
        price = self._price_for(usage)
        if price is None:
            return usd
        return usd + (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_write_tokens * price.cache_write
        ) / MTOK

    def _parts(self, usage: Usage) -> list[Usage]:
        """Ce qui est facturé : chaque tentative avec son modèle, ou la consommation entière."""
        if not usage.iterations:
            return [usage]
        parts = [replace(it, provider=usage.provider,
                         requested_model=it.requested_model or usage.requested_model,
                         web_searches=0, iterations=[])
                 for it in usage.iterations]
        parts[0] = replace(parts[0], web_searches=usage.web_searches)
        return parts

    def record(self, usage: Usage) -> float:
        """Ajoute la consommation au mois courant ; retourne son coût en dollars."""
        total = 0.0
        month = self.month()
        with self._lock:
            for part in self._parts(usage):
                usd = self.cost(part)
                total += usd
                input_tokens = part.input_tokens + part.cache_read_tokens + part.cache_write_tokens
                self.db.conn.execute(
                    "INSERT INTO usage(month, provider, model, input_tokens, output_tokens, usd) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(month, provider, model) DO UPDATE SET "
                    "input_tokens = input_tokens + excluded.input_tokens, "
                    "output_tokens = output_tokens + excluded.output_tokens, "
                    "usd = usd + excluded.usd",
                    (month, part.provider, part.model, input_tokens, part.output_tokens, usd),
                )
            self.db.conn.commit()
        return total

    def spent(self, month: str | None = None) -> float:
        row = self.db.conn.execute(
            "SELECT COALESCE(SUM(usd), 0) FROM usage WHERE month = ?", (month or self.month(),)
        ).fetchone()
        return float(row[0])

    def ratio(self) -> float:
        """Part du budget mensuel consommée (1.0 = épuisé ; budget nul = rien à dépenser)."""
        if self.config.monthly_usd <= 0:
            return float("inf")
        return self.spent() / self.config.monthly_usd
