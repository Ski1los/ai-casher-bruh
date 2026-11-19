import pytest

from bot.triangular import find_triangles, path_min_order_ok, simulate_path, usd_prices


def mk(base, quote, fee=0.001, min_cost=0, min_amount=0):
    return {
        "spot": True,
        "active": True,
        "base": base,
        "quote": quote,
        "taker": fee,
        "limits": {"cost": {"min": min_cost}, "amount": {"min": min_amount}},
    }


def tk(bid, ask, qv=10_000_000):
    return {"bid": bid, "ask": ask, "quoteVolume": qv}


# Рынок, где PEPE/BTC «отстал»: через BTC выгоднее
MARKETS = {
    "BTC/USDT": mk("BTC", "USDT"),
    "PEPE/USDT": mk("PEPE", "USDT"),
    "PEPE/BTC": mk("PEPE", "BTC"),
}
TICKERS = {
    "BTC/USDT": tk(50_000, 50_001),
    "PEPE/USDT": tk(0.0000099, 0.00001),  # купить PEPE за 0.00001
    # 1 PEPE = 2.04e-10 BTC = 0.0000102 USDT → +2% относительно покупки
    "PEPE/BTC": tk(2.04e-10, 2.05e-10, qv=200),  # объём в BTC: 200 BTC ≈ $10M
}


def test_usd_prices():
    p = usd_prices(TICKERS)
    assert p["USDT"] == 1
    assert p["BTC"] == pytest.approx(50_000.5)


def test_finds_profitable_cycle_with_fees():
    opps = find_triangles("ex", MARKETS, TICKERS, min_profit_percent=0.1)
    assert opps, "цикл должен найтись"
    best = opps[0]
    assert best.path == ["USDT", "PEPE", "BTC", "USDT"]
    assert [leg.side for leg in best.legs] == ["buy", "sell", "sell"]
    expected_gross = (1 / 0.00001) * 2.04e-10 * 50_000
    assert best.gross_percent == pytest.approx((expected_gross - 1) * 100)
    assert best.net_percent == pytest.approx((expected_gross * 0.999**3 - 1) * 100)
    # обратный цикл убыточен и не попадает в список
    assert all(o.path != ["USDT", "BTC", "PEPE", "USDT"] for o in opps)


def test_filters_low_volume_and_suspicious_profit():
    low = dict(TICKERS, **{"PEPE/BTC": tk(2.04e-10, 2.05e-10, qv=0.1)})  # ≈ $5k
    assert find_triangles("ex", MARKETS, low, min_profit_percent=0.1, min_volume_usd=50_000) == []
    assert find_triangles("ex", MARKETS, TICKERS, min_profit_percent=0.1, max_profit_percent=1.0) == []


def test_ignores_inactive_and_broken_tickers():
    markets = dict(MARKETS, **{"PEPE/BTC": dict(mk("PEPE", "BTC"), active=False)})
    assert find_triangles("ex", markets, TICKERS, min_profit_percent=0.1) == []
    crossed = dict(TICKERS, **{"PEPE/BTC": tk(3e-10, 2e-10, qv=200)})  # bid > ask = мусор
    assert find_triangles("ex", MARKETS, crossed, min_profit_percent=0.1) == []


def test_simulate_path_deep_books_equals_rate_product():
    opp = find_triangles("ex", MARKETS, TICKERS, min_profit_percent=0.1)[0]
    books = [
        {"asks": [[0.00001, 1e12]], "bids": []},
        {"asks": [], "bids": [[2.04e-10, 1e12]]},
        {"asks": [], "bids": [[50_000, 1e6]]},
    ]
    d = simulate_path(books, opp.legs, 100)
    assert d.filled
    assert d.profit_percent == pytest.approx(opp.net_percent)


def test_simulate_path_thin_book_not_filled():
    opp = find_triangles("ex", MARKETS, TICKERS, min_profit_percent=0.1)[0]
    books = [
        {"asks": [[0.00001, 1e6]], "bids": []},  # всего $10 предложения
        {"asks": [], "bids": [[2.04e-10, 1e12]]},
        {"asks": [], "bids": [[50_000, 1e6]]},
    ]
    assert not simulate_path(books, opp.legs, 100).filled


def test_min_order_per_leg():
    opp = find_triangles("ex", MARKETS, TICKERS, min_profit_percent=0.1)[0]
    markets = dict(MARKETS, **{"PEPE/BTC": mk("PEPE", "BTC", min_cost=0.0002)})  # мин. 0.0002 BTC ≈ $10
    assert not path_min_order_ok(opp.legs, 5, markets)
    assert path_min_order_ok(opp.legs, 50, markets)
