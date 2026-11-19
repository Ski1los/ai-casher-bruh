import asyncio

import pytest

from bot.config import Settings
from bot.scanner import Quote, Scanner, find_opportunities, net_percent, simulate_trade


def q(ex, sym, bid, ask, vol=1_000_000, fee=0.001):
    return Quote(ex, sym, bid, ask, vol, fee)


def settings(**kw):
    base = dict(exchanges=[], min_profit_percent=0.5, max_profit_percent=15, min_volume_usd=50_000)
    base.update(kw)
    return Settings(**base)


def test_net_percent_accounts_for_fees():
    assert net_percent(100, 100, 0, 0) == pytest.approx(0)
    # 2% спреда минус две комиссии по 0.1%
    assert net_percent(100, 102, 0.001, 0.001) == pytest.approx(1.796, abs=1e-3)


def test_finds_best_pair():
    quotes = {
        "a": {"X/USDT": q("a", "X/USDT", 0.99, 1.00)},
        "b": {"X/USDT": q("b", "X/USDT", 1.02, 1.03)},
        "c": {"X/USDT": q("c", "X/USDT", 1.05, 1.06)},
    }
    opps, compared = find_opportunities(quotes, settings())
    assert compared == 1
    assert len(opps) == 1
    o = opps[0]
    assert (o.buy.exchange, o.sell.exchange) == ("a", "c")
    assert o.gross_percent == pytest.approx(5.0)


def test_filters_low_volume_too_big_and_too_small_spreads():
    quotes = {
        "a": {
            "LOW/USDT": q("a", "LOW/USDT", 1, 1, vol=10),
            "FAKE/USDT": q("a", "FAKE/USDT", 1, 1),
            "TINY/USDT": q("a", "TINY/USDT", 1, 1),
        },
        "b": {
            "LOW/USDT": q("b", "LOW/USDT", 1.1, 1.1),
            "FAKE/USDT": q("b", "FAKE/USDT", 2, 2),  # +100%: разные монеты
            "TINY/USDT": q("b", "TINY/USDT", 1.003, 1.004),  # съедят комиссии
        },
    }
    opps, _ = find_opportunities(quotes, settings())
    assert opps == []


def test_simulate_trade_full_fill():
    asks = [[1.0, 50], [1.01, 100]]  # купим 50 по 1.0 и ~49.5 по 1.01
    bids = [[1.05, 1000]]
    d = simulate_trade(asks, bids, 100, 0, 0)
    assert d.filled
    coins = 50 + 50 / 1.01
    assert d.received_usd == pytest.approx(coins * 1.05)
    assert d.profit_usd > 0


def test_simulate_trade_not_enough_depth():
    d = simulate_trade([[1.0, 10]], [[1.1, 5]], 100, 0, 0)
    assert not d.filled
    assert d.amount_usd == pytest.approx(10)


class FakeExchange:
    has = {"fetchTickers": True}

    def __init__(self, name, tickers, book):
        self.name = name
        self._tickers = tickers
        self._book = book
        self.markets = {
            s: {"spot": True, "quote": "USDT", "active": True, "taker": 0.001} for s in tickers
        }
        self.markets["X/USDT:USDT"] = {"spot": False, "quote": "USDT"}  # фьючерс, игнорируем

    async def load_markets(self, reload=False):
        return self.markets

    async def fetch_tickers(self):
        return {**self._tickers, "X/USDT:USDT": {"bid": 99, "ask": 99, "quoteVolume": 1e9}}

    async def fetch_order_book(self, symbol, limit=None):
        return self._book

    async def close(self):
        pass


class BrokenExchange(FakeExchange):
    async def fetch_tickers(self):
        raise ConnectionError("timeout")


def test_scanner_end_to_end_with_fakes():
    s = settings(exchanges=[], trade_amount_usd=100)
    scanner = Scanner(s)
    scanner.exchanges = {
        "a": FakeExchange("A", {"X/USDT": {"bid": 0.99, "ask": 1.0, "quoteVolume": 1e6}},
                          {"asks": [[1.0, 1000]], "bids": [[0.99, 1000]]}),
        "b": FakeExchange("B", {"X/USDT": {"bid": 1.03, "ask": 1.04, "quoteVolume": 1e6}},
                          {"asks": [[1.04, 1000]], "bids": [[1.03, 1000]]}),
        "c": BrokenExchange("C", {}, {}),
    }
    result = asyncio.run(scanner.scan())
    assert result.exchanges_ok == ["a", "b"]
    assert "c" in result.errors
    assert len(result.opportunities) == 1
    o = result.opportunities[0]
    assert (o.buy.exchange, o.sell.exchange) == ("a", "b")
    assert o.depth is not None and o.depth.filled
    assert o.depth.profit_percent == pytest.approx(net_percent(1.0, 1.03, 0.001, 0.001))


def test_fmt_price_no_exponent():
    from bot.scanner import fmt_price
    assert fmt_price(0.0000123456) == "0.0000123456"
    assert fmt_price(65432.1) == "65432.1"
    assert fmt_price(1.5) == "1.5"
