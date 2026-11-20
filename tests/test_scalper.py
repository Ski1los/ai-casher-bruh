"""Режим $5 (ловец откатов): индикаторы, проверка на истории, живой цикл на фейковой бирже, вторая копия бота."""

import asyncio

from bot.config import Settings
from bot.scalper import (MAX_HOLD_SEC, Params, Scalper, backtest, best_params, ema, rsi, signal_at,
                         target_price, trade_return)
from bot.trading import Trader
from tests.fake_exchange import FakeTradingExchange, mk_market
from tests.test_trading import KEYS, FakeScanner


def run(coro):
    return asyncio.run(coro)


def dip_candles(cycles=15, end_in_dip=True, dip=0.025):
    """Свечи: ровно → откат на 2,5% за 30 минут → возврат. В конце (по желанию) свежий откат."""
    closes = []
    for _ in range(cycles):
        closes += [100 * (1.0002 if i % 2 else 0.9998) for i in range(44)]
        closes += [100 * (1 - dip * (k + 1) / 6) for k in range(6)]
        closes += [100 * (1 - dip) + 100 * dip * (k + 1) / 10 for k in range(10)]
    if end_in_dip:
        closes += [100 * (1.0002 if i % 2 else 0.9998) for i in range(44)]
        closes += [100 * (1 - dip * (k + 1) / 6) for k in range(6)]
        closes.append(closes[-1])  # текущая, ещё не закрытая свеча
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([i * 300_000, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c, 1000])
        prev = c
    return out


def make(balance=5.1, live=True, min_cost=5.0, candles=None):
    ex = FakeTradingExchange("ExA", {"SOL/USDT": mk_market("SOL", "USDT", min_cost=min_cost)},
                             {"SOL/USDT": {"bid": 98.40, "ask": 98.42, "quoteVolume": 1e8}}, {"USDT": balance})
    ex.ohlcv = {"SOL/USDT": candles if candles is not None else dip_candles()}
    trader = Trader(FakeScanner({"a": ex}), Settings(exchanges=[]), env=KEYS)
    trader.state.live = live
    sc = Scalper(trader)
    sc.turn_on()
    return sc, trader, ex


def test_indicators():
    assert ema([1, 1, 1], 5) == [1, 1, 1]
    up = [float(i) for i in range(30)]
    assert rsi(up)[-1] == 100.0
    down = [float(30 - i) for i in range(30)]
    assert rsi(down)[-1] < 5


def test_target_price_covers_both_fees():
    entry, fee = 100.0, 0.001
    tp = target_price(entry, 0.008, fee)
    assert abs(trade_return(entry, tp, fee) - 0.008) < 1e-12


def test_backtest_finds_profit_on_dips_and_skips_crash():
    candles = dip_candles(end_in_dip=False)
    res = backtest(candles, Params(0.006, 30), 0.001)
    assert res["trades"] >= 10 and res["wins"] == res["trades"] and res["net_pct"] > 0
    best = best_params(candles, 0.001)
    assert best and best["net_pct"] >= res["net_pct"]
    # обвал: -0,5% каждую свечу — это не откат, покупок нет
    closes = [100 * 0.995 ** i for i in range(400)]
    crash = [[i, c, c, c * 0.999, c, 1] for i, c in enumerate(closes)]
    assert backtest(crash, Params(0.006, 30), 0.001)["trades"] == 0


def test_signal_only_on_last_closed_dip():
    candles = dip_candles()
    closes = [c[4] for c in candles[:-1]]
    assert signal_at(closes, ema(closes, 50), rsi(closes), len(closes) - 1, Params(0.006, 30))
    flat = [100.0 + (0.01 if i % 2 else 0) for i in range(120)]
    assert not signal_at(flat, ema(flat, 50), rsi(flat), 119, Params(0.006, 30))


def test_live_cycle_with_five_dollars():
    sc, trader, ex = make(balance=5.1)
    events = run(sc.step())
    assert events and "купил SOL" in events[0]
    pos = sc.st["pos"]
    assert ex.created[0][:2] == ("buy_cost", "SOL/USDT") and abs(ex.created[0][2] - 5.1) < 1e-6
    limit = ex.created[1]
    assert limit[0] == "limit" and limit[2] == "sell" and limit[4] > 98.42  # продажа выше покупки
    assert limit[3] * limit[4] >= 5.0  # продажа тоже проходит минимум биржи
    # продажа исполнилась на бирже
    o = ex.orders[pos["order_id"]]
    o.update(status="closed", filled=o["amount"], cost=o["amount"] * o["price"],
             fee={"currency": "USDT", "cost": o["amount"] * o["price"] * 0.001})
    events = run(sc.step())
    assert events and "продал SOL" in events[0]
    assert sc.st["pos"] is None
    stats = sc.st["stats"]
    assert stats["cycles"] == 1 and stats["wins"] == 1 and stats["pnl"] > 0
    assert trader.state.log[-1]["pnl_usd"] > 0 and trader.state.log[-1]["source"] == "scalp"
    assert trader.state.day_trades == 2


def test_not_enough_money_explained():
    sc, trader, ex = make(balance=3.0)
    assert run(sc.step()) == []
    assert "мало денег" in sc.last_reason and "Funding" in sc.last_reason
    assert not ex.created


def test_paper_mode_uses_virtual_five_dollars():
    sc, trader, ex = make(balance=0.0, live=False)
    events = run(sc.step())
    assert events and "ТЕСТ" in events[0]
    assert not ex.created  # в тесте ордера не уходят
    assert sc.st["paper_usd"] < 0.01
    ex.tickers["SOL/USDT"] = {"bid": 120.0, "ask": 120.1, "quoteVolume": 1e8}
    events = run(sc.step())
    assert "продал SOL" in events[0]
    assert sc.st["paper_usd"] > 5.0


def test_waits_when_no_dip_and_breakeven_after_a_day():
    flat = dip_candles(end_in_dip=False)
    sc, trader, ex = make(balance=5.1, candles=flat)
    assert run(sc.step()) == []
    assert "жду отката" in sc.last_reason
    # открытая позиция сутки без цели → продажа в безубыток
    sc2, trader2, ex2 = make(balance=5.1)
    run(sc2.step())
    pos = sc2.st["pos"]
    old_tp = pos["tp"]
    events = run(sc2.step(now=pos["opened_at"] + MAX_HOLD_SEC + 60))
    assert events and "безубыток" in events[0]
    assert sc2.st["pos"]["tp"] < old_tp and sc2.st["pos"]["breakeven"]
    assert ex2.orders["2"]["status"] == "canceled"  # старая продажа снята, новая выставлена
    assert ex2.created[-1][0] == "limit"


def test_no_stop_below_exchange_minimum():
    sc, trader, ex = make(balance=5.1)
    run(sc.step())
    ex.tickers["SOL/USDT"] = {"bid": 90.0, "ask": 90.05, "quoteVolume": 1e8}  # -8,5%
    assert run(sc.step()) == []
    assert "продать нельзя" in sc.last_reason
    assert sc.st["pos"] is not None


def test_duplicate_copy_pauses_new_buys():
    from bot.telegram_bot import ConflictWatch

    w = ConflictWatch()
    w.note(1000)
    w.note(1010)
    assert not w.active(1020)  # короткий конфликт при обновлении на Render — не дубль
    w.note(1300)
    assert w.active(1310)
    assert not w.active(1300 + ConflictWatch.WINDOW + 400)

    sc, trader, ex = make(balance=5.1)
    sc.duplicate_fn = lambda: True
    assert run(sc.step()) == []
    assert "две копии" in sc.last_reason and not ex.created


def test_scalp_commands_and_stop(bot_env):
    from tests.test_telegram_flow import OWNER, send

    b, session = bot_env
    ex = FakeTradingExchange("ExA", {"SOL/USDT": mk_market("SOL", "USDT", min_cost=5.0)},
                             {"SOL/USDT": {"bid": 98.40, "ask": 98.42, "quoteVolume": 1e8}}, {"USDT": 5.1})
    ex.ohlcv = {"SOL/USDT": dip_candles()}
    b.scanner.exchanges = {"a": ex}
    b.trader = Trader(b.scanner, b.settings, env=KEYS)
    b.scalper = Scalper(b.trader, b.duplicate_running)
    b.trader.state.auto = True

    async def flow():
        await send(b, "/scalp on")
        text = "\n".join(t for _, t in session.sent[-3:])
        assert "Режим $5 включён" in text and "треугольниками выключил" in text
        assert b.scalper.on and not b.trader.state.auto
        await send(b, "/scalp")
        assert "ловец откатов" in session.sent[-1][1]
        await send(b, "/why")
        assert "Режим $5 включён" in session.sent[-1][1]
        await send(b, "/stop")
        assert not b.scalper.on

    run(flow())
    assert OWNER


def test_coarse_amount_step_is_skipped():
    """У BTC шаг количества ≈ $0,09: с $5 после округления продажа не прошла бы минимум — бот не покупает."""
    sc, trader, ex = make(balance=5.0)
    ex.markets["SOL/USDT"]["precision"] = {"amount": 0.001}  # 0,001 × $98 ≈ $0,10
    assert run(sc.step()) == []
    assert "крупный шаг" in sc.last_reason and not ex.created
    sc2, trader2, ex2 = make(balance=5.0)
    ex2.markets["SOL/USDT"]["precision"] = {"amount": 0.00001}
    assert run(sc2.step())  # мелкий шаг: $5 хватает
