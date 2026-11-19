"""Аналитика без ИИ: комиссии, безубыточность, «почти получилось» и рекомендации."""

import time

import pytest

from bot.analytics import analyze, analysis_text, cross_break_even, triangle_break_even
from bot.config import Settings, parse_fee_overrides
from bot.demo import DemoAccount, DemoTrade
from bot.report import ScanStats
from bot.scanner import NearMiss, Quote, find_opportunities
from bot.triangular import find_triangles


class Ex:
    def __init__(self, name, fee):
        self.name = name
        self.markets = {
            f"C{i}/USDT": {"spot": True, "quote": "USDT", "taker": fee} for i in range(5)
        }
        self.markets["X/BTC"] = {"spot": True, "quote": "BTC", "taker": 0.5}  # не к USDT: не учитывается


class FakeScanner:
    def __init__(self, fees):
        self.exchanges = {n: Ex(n.upper(), f) for n, f in fees.items()}

    def display_name(self, n):
        return self.exchanges[n].name if n in self.exchanges else n


class FakeDemo:
    def __init__(self, accounts):
        self.accounts = {f"{a.strategy}-{a.start_balance:g}": a for a in accounts}


def trade(expected, actual, strategy="tri"):
    return DemoTrade("USDT → A → B → USDT", "okx", "okx", time.time(), 100, 1, 1, 0, True, 1, 100,
                     expected, closed_at=time.time(), received_usd=100 * (1 + actual / 100), strategy=strategy)


def test_break_even_math():
    assert triangle_break_even(0.001) == pytest.approx(0.3006, abs=1e-4)  # 1/0.999³ − 1
    assert cross_break_even(0.001, 0.002) == pytest.approx(0.3007, abs=1e-4)


def test_parse_fee_overrides():
    assert parse_fee_overrides("bybit:0.1, MEXC=0.05%; bad, okx:abc, gate:9") == {"bybit": 0.001, "mexc": 0.0005}
    assert parse_fee_overrides("") == {}


def test_fees_break_even_and_withdraw_note():
    s = Settings(demo_withdraw_fee_usd=0.5)
    sc = FakeScanner({"mexc": 0.0005, "gate": 0.002})
    demo = FakeDemo([DemoAccount(start_balance=5, balance=5, strategy="cross"),
                     DemoAccount(start_balance=500, balance=500, strategy="cross")])
    a = analyze(sc, s, demo)
    assert a.fees == {"mexc": 0.0005, "gate": 0.002}
    assert a.tri_break_even["mexc"] < a.tri_break_even["gate"]
    assert a.cross_break_even[2] == pytest.approx(cross_break_even(0.0005, 0.002))
    assert a.cross_by_amount[5] == pytest.approx(a.cross_break_even[2] + 10)  # $0.5 вывода = 10% от $5
    assert any("$5" in n and "фейковые" in n for n in a.notes)
    assert not any("$500" in n and "фейковые" in n for n in a.notes)
    text = analysis_text(a, sc, s)
    assert "MEXC: 0.050%" in text and "окупается от 0.15%" in text
    d = a.to_dict()
    assert d["fees_taker_percent"] == {"mexc": 0.05, "gate": 0.2}


def test_losing_triangles_raise_threshold():
    s = Settings(tri_min_profit_percent=0.15)
    acc = DemoAccount(start_balance=50, balance=49, strategy="tri", scans=100)
    acc.trades = [trade(0.2, -0.1), trade(0.25, -0.05), trade(0.3, 0.0)]
    acc.rejects = {"depth": 40, "min_order": 2}
    a = analyze(FakeScanner({"okx": 0.001}), s, FakeDemo([acc]))
    sug = {x.key: x for x in a.suggestions}
    assert "tri_min" in sug and sug["tri_min"].value == pytest.approx(0.15 + 0.3, abs=0.01)
    assert any("после проверки стакана" in b for b in a.bottlenecks)
    st = a.accounts[0]
    assert st["avg_execution_loss_percent"] == pytest.approx(0.3, abs=1e-3)
    assert st["win_rate_percent"] == 0


def test_near_miss_suggests_lower_threshold_only_when_profitable():
    s = Settings(tri_min_profit_percent=0.15)
    stats = ScanStats(scans=50)
    stats.near_tri = NearMiss(0.08, 0.38, "USDT → A → B → USDT на okx")
    a = analyze(FakeScanner({"okx": 0.001}), s, FakeDemo([]), stats)
    assert [(x.key, x.value) for x in a.suggestions] == [("tri_min", 0.064)]

    stats.near_tri = NearMiss(-0.1, 0.2, "USDT → A → B → USDT на okx")
    a = analyze(FakeScanner({"okx": 0.001}), s, FakeDemo([]), stats)
    assert not a.suggestions
    assert any("съедают комиссии" in n for n in a.notes)


def test_flaky_exchange_note():
    stats = ScanStats(scans=20)
    stats.exchange_errors["htx"] = 15
    a = analyze(FakeScanner({"okx": 0.001}), Settings(), FakeDemo([]), stats)
    assert any("htx" in n and "75%" in n for n in a.notes)


def test_scanner_records_near_misses():
    s = Settings(min_profit_percent=0.5, max_profit_percent=15, min_volume_usd=0)
    q = lambda ex, bid, ask: Quote(ex, "PEPE/USDT", bid, ask, 1e6, 0.001)  # noqa: E731
    near: list = []
    opps, _ = find_opportunities({"a": {"x": q("a", 1.0, 1.001)}, "b": {"x": q("b", 1.002, 1.003)}}, s, near)
    assert opps == [] and len(near) == 1
    assert near[0].text == "PEPE/USDT a→b" and near[0].net_percent < 0 < near[0].gross_percent

    m = lambda b, qt: {"spot": True, "active": True, "base": b, "quote": qt, "taker": 0.001}  # noqa: E731
    markets = {"BTC/USDT": m("BTC", "USDT"), "ETH/USDT": m("ETH", "USDT"), "ETH/BTC": m("ETH", "BTC")}
    tickers = {
        "BTC/USDT": {"bid": 50000, "ask": 50001, "quoteVolume": 1e9},
        "ETH/USDT": {"bid": 2500, "ask": 2500.5, "quoteVolume": 1e9},
        "ETH/BTC": {"bid": 0.05, "ask": 0.05001, "quoteVolume": 1e5},
    }
    near = []
    assert find_triangles("okx", markets, tickers, min_profit_percent=0.15, min_volume_usd=0, near=near) == []
    assert len(near) == 1 and near[0].net_percent < 0
