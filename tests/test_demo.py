import asyncio

import pytest

from bot.config import Settings
from bot.demo import DemoEngine, load_account
from bot.scanner import Opportunity, Quote, ScanResult, net_percent


class FakeScanner:
    """Подменяет биржи: стаканы и данные о монетах задаём руками."""

    def __init__(self, books, withdraw_fee=None, min_cost=1.0):
        self.books = books  # {(exchange, symbol): {"asks": .., "bids": ..}}
        self.withdraw_fee = withdraw_fee
        self.min_cost = min_cost

    def display_name(self, ex):
        return ex.upper()

    async def order_book(self, ex, symbol):
        return self.books[(ex, symbol)]

    def market(self, ex, symbol):
        return {"taker": 0.001, "limits": {"cost": {"min": self.min_cost}, "amount": {"min": 0}}}

    def withdraw_fee_coins(self, ex, code):
        return self.withdraw_fee

    def can_withdraw(self, ex, code):
        return None

    def can_deposit(self, ex, code):
        return None


def opp(buy_ex="a", sell_ex="b", ask=1.0, bid=1.03):
    return Opportunity(
        "X/USDT",
        Quote(buy_ex, "X/USDT", ask * 0.999, ask, 1e6, 0.001),
        Quote(sell_ex, "X/USDT", bid, bid * 1.001, 1e6, 0.001),
        (bid / ask - 1) * 100,
        net_percent(ask, bid, 0.001, 0.001),
    )


def make(tmp_path, amount, **scanner_kw):
    books = {
        ("a", "X/USDT"): {"asks": [[1.0, 1e6]], "bids": [[0.999, 1e6]]},
        ("b", "X/USDT"): {"asks": [[1.031, 1e6]], "bids": [[1.03, 1e6]]},
    }
    settings = Settings(exchanges=[], min_profit_percent=0.5, demo_transfer_min=0, demo_withdraw_fee_usd=0.5)
    engine = DemoEngine(FakeScanner(books, **scanner_kw), settings, tmp_path / "demo.json")
    engine.start(amount)
    return engine


def test_small_balance_rejected_because_of_withdraw_fee(tmp_path):
    # $5, спред ~2.8%, вывод $0.5 = 10% баланса → сделки быть не должно
    engine = make(tmp_path, 5)
    events = asyncio.run(engine.process(ScanResult(opportunities=[opp()])))
    assert events == []
    assert engine.acc.open_trade is None
    assert engine.acc.rejects == {"fees": 1}
    assert "вывод стоит" in engine.acc.best_rejected


def test_full_cycle_with_compounding(tmp_path):
    engine = make(tmp_path, 100)
    events = asyncio.run(engine.process(ScanResult(opportunities=[opp()])))
    assert len(events) == 1 and "открыта" in events[0]
    t = engine.acc.open_trade
    assert t is not None and engine.acc.balance == pytest.approx(0)
    assert t.withdraw_fee_coins == pytest.approx(0.5)  # $0.5 по цене 1.0

    # Следующий скан: перевод «доехал» (transfer_min=0) → продаём на b
    events = asyncio.run(engine.process(ScanResult()))
    assert len(events) == 1 and "закрыта" in events[0]
    expected = (100 / 1.0 * 0.999 - 0.5) * 1.03 * 0.999
    assert engine.acc.balance == pytest.approx(expected)
    assert engine.acc.location == "b"

    # Деньги теперь на b: связка «купить на a» недоступна
    asyncio.run(engine.process(ScanResult(opportunities=[opp()])))
    assert engine.acc.open_trade is None
    assert engine.acc.rejects.get("location") == 1

    # Состояние сохранилось на диск
    saved = load_account(tmp_path / "demo.json")
    assert saved.balance == pytest.approx(expected)
    assert len(saved.trades) == 1


def test_min_order_respected(tmp_path):
    engine = make(tmp_path, 5, min_cost=10)
    asyncio.run(engine.process(ScanResult(opportunities=[opp()])))
    assert engine.acc.rejects == {"min_order": 1}


def test_price_moves_during_transfer(tmp_path):
    engine = make(tmp_path, 100, withdraw_fee=0.0)
    asyncio.run(engine.process(ScanResult(opportunities=[opp()])))
    # Пока монета шла, цена на b упала на 5%
    engine.scanner.books[("b", "X/USDT")]["bids"] = [[0.98, 1e6]]
    asyncio.run(engine.process(ScanResult()))
    t = engine.acc.trades[-1]
    assert t.profit_usd < 0
    assert t.signal_percent > 0  # сигнал обещал плюс, а вышло в минус
