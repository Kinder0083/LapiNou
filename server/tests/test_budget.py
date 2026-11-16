# SPDX-License-Identifier: MIT
import datetime as dt

import pytest

from lapinou.brain.base import Usage
from lapinou.brain.budget import Budget
from lapinou.config import BudgetConfig, Settings
from lapinou.db import Database


class Clock:
    def __init__(self, when: dt.datetime) -> None:
        self.when = when

    def __call__(self) -> dt.datetime:
        return self.when


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "lapinou.db")
    d.migrate()
    yield d
    d.close()


def opus(**kw) -> Usage:
    return Usage(provider="anthropic", model="claude-opus-5-5", **kw)


def test_default_prices_in_settings():
    prices = Settings().budget.prices
    o, h = prices["claude-opus-5-5"], prices["claude-haiku-5-5"]
    assert (o.input, o.output, o.cache_read) == (4.0, 20.0, 0.20)
    assert (h.input, h.output) == (0.10, 0.50)


def test_cost_uses_model_prices(db):
    clock = Clock(dt.datetime(2026, 11, 16, 12, 0))
    b = Budget(db, BudgetConfig(monthly_usd=10.0), clock=clock)
    # 1 MTok entrée + 0,1 MTok sortie + 1 MTok lu en cache : 4 + 2 + 0,20
    cost = b.record(opus(input_tokens=1_000_000, output_tokens=100_000,
                         cache_read_tokens=1_000_000))
    assert cost == pytest.approx(6.20)
    assert b.spent() == pytest.approx(6.20)
    assert b.ratio() == pytest.approx(0.62)
    haiku = Usage("anthropic", "claude-haiku-5-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert b.record(haiku) == pytest.approx(0.60)
    assert b.ratio() == pytest.approx(0.68)


def test_web_searches_and_dated_model_ids(db):
    b = Budget(db, BudgetConfig(monthly_usd=1.0), clock=Clock(dt.datetime(2026, 11, 1)))
    # Un identifiant daté retombe sur le prix de son préfixe ; 10 $ les 1000 recherches.
    assert b.record(Usage("anthropic", "claude-opus-5-5-20261101", web_searches=2)) == (
        pytest.approx(0.02)
    )


def test_unknown_model_is_free(db):
    b = Budget(db, BudgetConfig(monthly_usd=1.0), clock=Clock(dt.datetime(2026, 11, 1)))
    assert b.record(Usage("homeassistant", "assist", input_tokens=500)) == 0.0
    assert b.ratio() == 0.0


def test_usage_accumulates_per_month_and_persists(db):
    clock = Clock(dt.datetime(2026, 11, 30, 23, 59))
    b = Budget(db, BudgetConfig(monthly_usd=4.0), clock=clock)
    b.record(opus(input_tokens=500_000))  # 2 $
    b.record(opus(input_tokens=250_000, output_tokens=10))  # 1 $ (+ 0,0002)
    assert b.ratio() == pytest.approx(0.75, abs=1e-3)
    row = db.conn.execute(
        "SELECT input_tokens, output_tokens FROM usage WHERE month = '2026-11'"
    ).fetchone()
    assert tuple(row) == (750_000, 10)

    clock.when = dt.datetime(2026, 12, 1, 0, 1)  # nouveau mois : compteur à zéro
    assert b.ratio() == 0.0
    b.record(opus(input_tokens=250_000))
    assert b.spent() == pytest.approx(1.0)

    # Une autre instance (redémarrage) relit la même base.
    again = Budget(db, BudgetConfig(monthly_usd=4.0), clock=clock)
    assert again.spent() == pytest.approx(1.0)
    assert again.spent("2026-11") == pytest.approx(3.0, abs=1e-3)


def test_zero_budget_means_nothing_to_spend(db):
    # Budget nul : aucune dépense payante permise (le routeur passe à Assist).
    b = Budget(db, BudgetConfig(monthly_usd=0.0), clock=Clock(dt.datetime(2026, 11, 1)))
    assert b.ratio() == float("inf")
