import asyncio
import json

import pytest

from bot.config import Settings
from bot.demo_manager import DemoManager
from bot.scanner import ScanResult
from bot.triangular import find_triangles

MARKETS = {
    "BTC/USDT": {"spot": True, "base": "BTC", "quote": "USDT", "taker": 0.001,
                 "limits": {"cost": {"min": 1}, "amount": {"min": 0}}},
    "PEPE/USDT": {"spot": True, "base": "PEPE", "quote": "USDT", "taker": 0.001,
                  "limits": {"cost": {"min": 1}, "amount": {"min": 0}}},
    "PEPE/BTC": {"spot": True, "base": "PEPE", "quote": "BTC", "taker": 0.001,
                 "limits": {"cost": {"min": 0.0002}, "amount": {"min": 0}}},  # ≈ $10
}
TICKERS = {
    "BTC/USDT": {"bid": 50_000, "ask": 50_001, "quoteVolume": 1e7},
    "PEPE/USDT": {"bid": 0.0000099, "ask": 0.00001, "quoteVolume": 1e7},
    "PEPE/BTC": {"bid": 2.04e-10, "ask": 2.05e-10, "quoteVolume": 200},
}
GOOD = {
    "BTC/USDT": {"asks": [[50_001, 100]], "bids": [[50_000, 100]]},
    "PEPE/USDT": {"asks": [[0.00001, 1e12]], "bids": [[0.0000099, 1e12]]},
    "PEPE/BTC": {"asks": [[2.05e-10, 1e13]], "bids": [[2.04e-10, 1e13]]},
}


class FakeScanner:
    """Отдаёт стаканы из очереди: первый запрос = «обнаружение», следующие = «исполнение»."""

    def __init__(self, queues=None):
        self.queues = queues or {}
        self.calls = 0

    def display_name(self, ex):
        return ex.upper()

    async def order_book(self, ex, symbol):
        self.calls += 1
        q = self.queues.get(symbol)
        if q and len(q) > 1:
            return q.pop(0)
        return q[0] if q else GOOD[symbol]

    def market(self, ex, symbol):
        return MARKETS.get(symbol, {"limits": {}})

    def withdraw_fee_coins(self, ex, code):
        return None

    def can_withdraw(self, ex, code):
        return None

    def can_deposit(self, ex, code):
        return None


def settings():
    return Settings(exchanges=[], tri_min_profit_percent=0.1, min_profit_percent=0.5)


def tri_result(exchange="ex"):
    return ScanResult(triangles=find_triangles(exchange, MARKETS, TICKERS, min_profit_percent=0.1))


def test_start_creates_accounts_for_each_strategy_and_amount(tmp_path):
    m = DemoManager(FakeScanner(), settings(), tmp_path / "d.json")
    labels = m.start([5, 50, 500], chat_id=1)
    assert len(labels) == 6
    assert sorted(m.accounts) == ["cross-5", "cross-50", "cross-500", "tri-5", "tri-50", "tri-500"]
    saved = json.loads((tmp_path / "d.json").read_text())
    assert saved["version"] == 2 and len(saved["accounts"]) == 6


def test_triangle_trade_with_compounding_and_min_order(tmp_path):
    m = DemoManager(FakeScanner(), settings(), tmp_path / "d.json")
    m.start([5, 50], chat_id=1)
    events = asyncio.run(m.process(tri_result()))
    assert len(events) == 1 and "треугольник" in events[0]

    small, big = m.accounts["tri-5"], m.accounts["tri-50"]
    # $5 не проходит минимальный ордер пары PEPE/BTC (≈ $10)
    assert small.trades == [] and small.rejects.get("min_order") == 1
    # $50 торгует: ~+1.7% после трёх комиссий, прибыль реинвестирована
    assert len(big.trades) == 1
    expected = 50 / 0.00001 * 0.999 * 2.04e-10 * 0.999 * 50_000 * 0.999
    assert big.balance == pytest.approx(expected)
    assert big.location == "ex"


def test_triangle_opportunity_vanishes_before_execution(tmp_path):
    # Обнаружение по хорошим стаканам, а к исполнению PEPE/BTC просел на 3%
    worse = {"asks": [[2.05e-10, 1e13]], "bids": [[1.98e-10, 1e13]]}
    scanner = FakeScanner({"PEPE/BTC": [GOOD["PEPE/BTC"], worse]})
    m = DemoManager(scanner, settings(), tmp_path / "d.json")
    m.start([50], chat_id=1)
    asyncio.run(m.process(tri_result()))
    t = m.accounts["tri-50"].trades[0]
    assert t.signal_percent > 1  # ожидали плюс
    assert t.profit_usd < 0  # а исполнилось в минус, и демо это честно показывает
    assert m.accounts["tri-50"].balance < 50


def test_same_triangle_not_repeated_within_cooldown(tmp_path):
    m = DemoManager(FakeScanner(), settings(), tmp_path / "d.json")
    m.start([50], chat_id=1)
    asyncio.run(m.process(tri_result()))
    asyncio.run(m.process(tri_result()))
    assert len(m.accounts["tri-50"].trades) == 1


def test_money_stays_on_its_exchange(tmp_path):
    m = DemoManager(FakeScanner(), settings(), tmp_path / "d.json")
    m.start([50], chat_id=1)
    asyncio.run(m.process(tri_result("ex")))
    asyncio.run(m.process(tri_result("other")))
    acc = m.accounts["tri-50"]
    assert len(acc.trades) == 1 and acc.rejects.get("location") == 1


def test_book_cache_shares_requests_between_accounts(tmp_path):
    scanner = FakeScanner()
    m = DemoManager(scanner, settings(), tmp_path / "d.json")
    m.start([20, 50, 500], chat_id=1)
    asyncio.run(m.process(tri_result()))
    # 3 стакана на обнаружение + 3 на исполнение, общие для всех трёх треугольных счетов
    assert scanner.calls == 6
    assert all(len(m.accounts[f"tri-{a}"].trades) == 1 for a in (20, 50, 500))


def test_reports_render(tmp_path):
    m = DemoManager(FakeScanner(), settings(), tmp_path / "d.json")
    assert "не созданы" in m.summary_text()
    m.start([5, 50], chat_id=1)
    asyncio.run(m.process(tri_result()))
    summary = m.summary_text()
    assert "Треугольный" in summary and "Межбиржевой" in summary and "Лучший счёт" in summary
    assert "минимального ордера" in m.details_text(5)
    assert "USDT → PEPE → BTC → USDT" in m.history_text()
    assert "Нет демо-счетов" in m.details_text(7)


def test_persist_and_reload(tmp_path):
    m = DemoManager(FakeScanner(), settings(), tmp_path / "d.json")
    m.start([50], chat_id=7)
    asyncio.run(m.process(tri_result()))
    m2 = DemoManager(FakeScanner(), settings(), tmp_path / "d.json")
    assert m2.chat_id == 7 and m2.active
    assert m2.accounts["tri-50"].balance == pytest.approx(m.accounts["tri-50"].balance)
    m2.stop()
    assert not DemoManager(FakeScanner(), settings(), tmp_path / "d.json").active
