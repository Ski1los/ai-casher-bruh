"""Режим $5 (ловец откатов): индикаторы, проверка на истории, живой цикл на фейковой бирже, вторая копия бота."""

import asyncio

import pytest

from bot.config import Settings
from bot.scalper import (MAX_HOLD_SEC, Params, Scalper, backtest, best_params, ema, indicators, rsi, signal_at,
                         target_price, trade_return)
from bot.trading import TradeError, Trader
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
        closes.append(closes[-1] * 1.004)  # первая зелёная свеча после отката: сигнал и «Отката», и «Отскока»
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
    assert best and best["ok"] and best["train_pct"] > 0 and best["test_pct"] > 0 and best["test_trades"] >= 3
    assert best["strategy"] in ("dip", "bounce")  # на откатах «Тренд» не нужен
    # обвал: -0,5% каждую свечу — это не откат, покупок нет
    closes = [100 * 0.995 ** i for i in range(400)]
    crash = [[i, c, c, c * 0.999, c, 1] for i, c in enumerate(closes)]
    assert backtest(crash, Params(0.006, 30), 0.001)["trades"] == 0


def test_signal_only_on_last_closed_dip():
    candles = dip_candles()
    closes = [c[4] for c in candles[:-1]]
    assert signal_at(closes, indicators(closes), len(closes) - 1, Params(0.006, 30))
    assert signal_at(closes, indicators(closes), len(closes) - 1, Params(0.006, 40, "bounce"))
    flat = [100.0 + (0.01 if i % 2 else 0) for i in range(120)]
    for strat in ("dip", "bounce", "trend"):
        assert not signal_at(flat, indicators(flat), 119, Params(0.006, 60, strat))


def test_live_cycle_with_five_dollars():
    sc, trader, ex = make(balance=5.1)
    events = run(sc.step())
    assert events and "купил SOL" in events[0]
    pos = sc.positions.get("a")
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
    assert sc.positions.get("a") is None
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
    assert sc.st["paper"]["a"] < 0.01
    ex.tickers["SOL/USDT"] = {"bid": 120.0, "ask": 120.1, "quoteVolume": 1e8}
    events = run(sc.step())
    assert "продал SOL" in events[0]
    assert sc.st["paper"]["a"] > 5.0


def test_waits_when_no_dip_and_breakeven_after_a_day():
    flat = dip_candles(end_in_dip=False)
    sc, trader, ex = make(balance=5.1, candles=flat)
    assert run(sc.step()) == []
    assert "жду сигнала" in sc.last_reason
    # открытая позиция сутки без цели → продажа в безубыток
    sc2, trader2, ex2 = make(balance=5.1)
    run(sc2.step())
    pos = sc2.positions.get("a")
    old_tp = pos["tp"]
    events = run(sc2.step(now=pos["opened_at"] + MAX_HOLD_SEC + 60))
    assert events and "безубыток" in events[0]
    assert sc2.positions.get("a")["tp"] < old_tp and sc2.positions.get("a")["breakeven"]
    assert ex2.orders["2"]["status"] == "canceled"  # старая продажа снята, новая выставлена
    assert ex2.created[-1][0] == "limit"


def test_no_stop_below_exchange_minimum():
    sc, trader, ex = make(balance=5.1)
    run(sc.step())
    ex.tickers["SOL/USDT"] = {"bid": 90.0, "ask": 90.05, "quoteVolume": 1e8}  # -8,5%
    assert run(sc.step()) == []
    assert "продать нельзя" in sc.last_reason
    assert sc.positions.get("a") is not None


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
        assert "Режим $5 (автопилот)" in session.sent[-1][1]
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


def test_two_exchanges_trade_their_own_money_at_once():
    def ex_with(balance):
        ex = FakeTradingExchange("Ex", {"SOL/USDT": mk_market("SOL", "USDT", min_cost=5.0)},
                                 {"SOL/USDT": {"bid": 98.40, "ask": 98.42, "quoteVolume": 1e8}}, {"USDT": balance})
        ex.ohlcv = {"SOL/USDT": dip_candles()}
        return ex

    bybit, bingx = ex_with(5.8), ex_with(5.54)
    env = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "BINGX_API_KEY": "k", "BINGX_API_SECRET": "s"}
    trader = Trader(FakeScanner({"bybit": bybit, "bingx": bingx}), Settings(exchanges=[]), env=env)
    trader.state.live = True
    sc = Scalper(trader)
    sc.turn_on()
    events = run(sc.step())
    assert len(events) == 2 and set(sc.positions) == {"bybit", "bingx"}
    assert bybit.created[0][0] == "buy_cost" and bingx.created[0][0] == "buy_cost"
    assert sc.status_dict()["positions"][0]["exchange"] == "bybit"
    assert sc.status_text().count("позиция:") == 2


def test_old_single_position_state_is_migrated():
    sc, trader, ex = make(balance=5.1)
    trader.state.scalp = {"on": True, "pos": {"exchange": "a", "symbol": "SOL/USDT"}, "paper_usd": 4.0}
    assert sc.positions == {"a": {"exchange": "a", "symbol": "SOL/USDT"}}
    assert "pos" not in trader.state.scalp and "paper_usd" not in trader.state.scalp


def test_paper_balance_follows_real_money_until_first_trade():
    sc, trader, ex = make(balance=1.0)
    trader.state.live = False

    async def boom(name, ccy):
        raise RuntimeError("timeout")

    real_free = trader.free
    trader.free = boom
    assert run(sc._free_usd("a")) == 5.0 and sc.st["paper"].get("a") is None  # ошибку не запоминаем
    trader.free = real_free
    assert run(sc._free_usd("a")) == 5.0 and "a" in sc._paper_virtual
    ex.balance["USDT"] = 5.54  # деньги пришли позже
    assert run(sc._free_usd("a")) == 5.54 and "a" not in sc._paper_virtual


def test_walk_forward_rejects_strategy_that_stopped_working():
    """В первой части истории откаты отрастают, а в последней — продолжают падать: проверка «вслепую» это ловит."""
    good = dip_candles(cycles=10, end_in_dip=False)
    closes = [c[4] for c in good]
    last = closes[-1]
    for _ in range(6):  # откат, ложный разворот и падение дальше до стоп-лосса вместо возврата
        closes += [last * (1.0002 if i % 2 else 0.9998) for i in range(44)]
        closes += [last * (1 - 0.025 * (k + 1) / 6) for k in range(6)]
        closes.append(closes[-1] * 1.004)
        for _ in range(18):
            closes.append(closes[-1] * 0.995)  # ≈ −8,6%: срабатывает стоп-лосс
        closes += [closes[-1] + (last - closes[-1]) * (k + 1) / 8 for k in range(8)]  # потом возврат к уровню
    candles, prev = [], closes[0]
    for i, c in enumerate(closes):
        candles.append([i * 300_000, prev, max(prev, c) * 1.0005, min(prev, c) * 0.9995, c, 1000])
        prev = c
    res = best_params(candles, 0.001)
    assert res and res["train_pct"] > 0 and res["test_pct"] < 0 and not res["ok"]


def test_trend_strategy_buys_pullback_in_uptrend():
    closes = [100 * 1.0006 ** i for i in range(260)]  # ровный рост
    closes += [closes[-1] * (1 - 0.0015 * (k + 1)) for k in range(4)]  # откат к короткой средней
    closes.append(closes[-1] * 1.0013)  # и разворот вверх (RSI ещё не перегрет)
    ind = indicators(closes)
    assert signal_at(closes, ind, len(closes) - 1, Params(0.006, 62, "trend"))
    assert not signal_at(closes, ind, len(closes) - 1, Params(0.006, 30, "dip"))  # «Откат» тут молчит


def test_market_brake_blocks_buying_when_btc_falls():
    sc, trader, ex = make(balance=5.1)
    ex.markets["BTC/USDT"] = mk_market("BTC", "USDT")
    btc = [100.0] * 30 + [100 * (1 - 0.003 * k) for k in range(1, 14)]  # −3,9% за час
    ex.ohlcv["BTC/USDT"] = [[i * 300_000, c, c, c, c, 1] for i, c in enumerate(btc)]
    assert run(sc.step()) == []
    assert "тормоз рынка" in sc.last_reason and not ex.created
    ex.ohlcv["BTC/USDT"] = [[i * 300_000, 100, 100, 100, 100, 1] for i in range(40)]
    sc._market.clear()
    assert run(sc.step()) and ex.created  # рынок спокоен — покупает


def test_webapp_autopilot_start_and_coins(bot_env, monkeypatch):
    from tests import test_webapp as tw
    from tests.test_telegram_flow import OWNER

    monkeypatch.delenv("WEBAPP_DEV_USER", raising=False)
    b, _ = bot_env
    sc, trader, ex = make(balance=5.1, live=False)
    sc.turn_off()
    b.scanner, b.trader, b.scalper = trader.scanner, trader, sc
    b.allowed_ids.add(tw.FRIEND)
    from bot.webapp import WebApp
    b.webapp = WebApp(b, tw.TOKEN)

    async def flow(client):
        st, r = await tw.post(client, "/api/scalp", {"on": True, "coins": True})
        assert st == 200 and r["scalp"]["on"] and r["scalp"]["use_coins"]
        st, _ = await tw.post(client, "/api/scalp", {"coins": False}, uid=tw.FRIEND)
        assert st == 403  # только владелец
        st, r = await tw.post(client, "/api/scalp", {"coins": False}, uid=OWNER)
        assert st == 200 and r["scalp"]["on"] and not r["scalp"]["use_coins"]

    tw.call(b, flow)


def test_failed_tp_cancel_does_not_submit_market_sell():
    sc, trader, ex = make(balance=5.1)
    run(sc.step())
    pos = sc.positions["a"]
    original_id = pos["order_id"]
    before = list(ex.created)

    async def cancel_uncertain(*args, **kwargs):
        raise TimeoutError("timeout")

    ex.cancel_order = cancel_uncertain
    with pytest.raises(TradeError, match="не удалось подтвердить отмену"):
        run(sc._stop(90.0, 1000))
    assert pos["order_id"] == original_id
    assert ex.created == before
    assert ex.orders[original_id]["status"] == "open"


def test_partial_cancel_pauses_and_blocks_repeat_sales():
    sc, trader, ex = make(balance=5.1)
    run(sc.step())
    pos = sc.positions["a"]
    oid = pos["order_id"]
    before = list(ex.created)

    async def partial_cancel(*args, **kwargs):
        return {"id": oid, "status": "canceled", "filled": 0.001}

    ex.cancel_order = partial_cancel
    with pytest.raises(TradeError, match="не удалось подтвердить отмену"):
        run(sc._stop(90.0, 1000))
    assert not sc.on and pos["reconcile_required"] and pos["order_id"] == oid
    assert ex.created == before
    sc.turn_on()  # без сверки включение не должно приводить к новому ордеру
    events = run(sc._step_one("a", 1100))
    assert events and "сверки" in events[0]
    assert not sc.on and ex.created == before


def test_cancel_without_confirmed_status_blocks_replacement():
    sc, trader, ex = make(balance=5.1)
    run(sc.step())
    pos = sc.positions["a"]
    before = list(ex.created)

    async def unknown_cancel(*args, **kwargs):
        return {"id": pos["order_id"]}

    async def unknown_fetch(*args, **kwargs):
        return None

    ex.cancel_order = unknown_cancel
    trader._fetch_order = unknown_fetch
    with pytest.raises(TradeError, match="не удалось подтвердить отмену"):
        run(sc._cancel_tp(pos))
    assert not sc.on and pos["reconcile_required"] and ex.created == before


@pytest.mark.parametrize("test_trades,expected", [(0, False), (1, False), (2, False), (3, True)])
def test_strategy_requires_closed_trades_in_holdout(monkeypatch, test_trades, expected):
    import bot.scalper as module
    candles = [[i, 100, 101, 99, 100, 1] for i in range(200)]
    def fake_backtest(candles, params, fee, start=0, ind=None):
        count = test_trades if start else 8
        net = (1.0 if count else 0.0) if start else 3.0
        return {"trades": count, "wins": count, "net_pct": net, "days": 1,
                "tp": params.tp, "rsi_buy": params.rsi_buy, "strategy": params.strategy}
    monkeypatch.setattr(module, "backtest", fake_backtest)
    result = module.best_params(candles, 0.001)
    assert result["test_trades"] == test_trades
    assert result["ok"] is expected


def test_backtest_stop_gap_uses_open_not_unreachable_stop(monkeypatch):
    import bot.scalper as module
    candles = [[i, 100, 100, 99, 100, 1] for i in range(55)]
    candles[52] = [52, 80, 81, 79, 80, 1]
    monkeypatch.setattr(module, "signal_at", lambda closes, ind, i, p: i == 50)
    result = module.backtest(candles, module.Params(), fee=0)
    entry = 100 * (1 + module.SLIPPAGE)
    expected = (80 * (1 - module.SLIPPAGE) / entry - 1) * 100
    assert result["trades"] == 1
    assert result["net_pct"] == pytest.approx(expected, abs=0.001)


def test_partial_external_cancel_pauses_and_prevents_replacement():
    sc, trader, ex = make(balance=5.1)
    run(sc.step())
    pos = sc.positions["a"]
    oid = pos["order_id"]
    ex.orders[oid].update(status="canceled", filled=0.001)
    before = list(ex.created)
    events = run(sc.step())
    assert not sc.on and pos["reconcile_required"] and pos["order_id"] == oid
    assert events and "частично" in events[0]
    sc.turn_on()
    run(sc.step())
    assert not sc.on and ex.created == before


def test_tp_creation_timeout_pauses_and_prevents_duplicate():
    sc, trader, ex = make(balance=5.1)
    original = ex.create_order
    async def accepted_but_timed_out(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("response lost")
    ex.create_order = accepted_but_timed_out
    events = run(sc.step())
    pos = sc.positions["a"]
    assert events and "статус продажи" in events[0]
    assert not sc.on and pos["reconcile_required"]
    before = list(ex.created)
    sc.turn_on()
    run(sc.step())
    assert not sc.on and ex.created == before


def test_tp_creation_without_id_pauses(monkeypatch):
    sc, trader, ex = make(balance=5.1)
    async def no_id(*args, **kwargs):
        return {}
    ex.create_order = no_id
    events = run(sc.step())
    assert events and "статус продажи" in events[0]
    assert not sc.on and sc.positions["a"]["reconcile_required"]
