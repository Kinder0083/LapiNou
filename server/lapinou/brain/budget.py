# SPDX-License-Identifier: MIT
"""Budget mensuel des cerveaux payants, cumulé dans la table `usage` (mois, fournisseur, modèle)."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable

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

    def cost(self, usage: Usage) -> float:
        usd = usage.web_searches * self.config.web_search_per_1k / 1000
        price = self.price(usage.model)
        if price is None:
            if usage.model not in self._unpriced and (usage.input_tokens or usage.output_tokens):
                self._unpriced.add(usage.model)
                log.info("pas de prix pour %s/%s : compté gratuit", usage.provider, usage.model)
            return usd
        return usd + (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_write_tokens * price.cache_write
        ) / MTOK

    def record(self, usage: Usage) -> float:
        """Ajoute la consommation au mois courant ; retourne son coût en dollars."""
        usd = self.cost(usage)
        input_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        with self._lock:
            self.db.conn.execute(
                "INSERT INTO usage(month, provider, model, input_tokens, output_tokens, usd) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(month, provider, model) DO UPDATE SET "
                "input_tokens = input_tokens + excluded.input_tokens, "
                "output_tokens = output_tokens + excluded.output_tokens, "
                "usd = usd + excluded.usd",
                (self.month(), usage.provider, usage.model, input_tokens,
                 usage.output_tokens, usd),
            )
            self.db.conn.commit()
        return usd

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
