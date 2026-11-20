"""Автопилот v20: лаборатория (проверка стратегий вживую без денег), щит (стоп-лосс, серия убытков,
делистинги и плохие новости), кэш свечей."""

import asyncio
import time

from bot.opportunities import Promo, risk_coins
from bot.scalper import CANDLE_SEC, COIN_COOLDOWN_SEC, STREAK_PAUSE_SEC
from tests.test_ai_flow import buttons
from tests.test_scalper import dip_candles, make
from tests.test_telegram_flow import send


def run(coro):
    return asyncio.run(coro)


def lab_key(sc):
    r = sc.ranking["a"][0]
    return sc.lab_key("a", r["symbol"], r["strategy"]), r


def test_lab_opens_paper_trade_on_live_signal_and_closes_at_target():
    sc, trader, ex = make(balance=5.1, live=False)
    run(sc.rank("a"))
    run(sc.lab_step("a", now=1_000_000))
    key, r = lab_key(sc)
    pos = sc.lab["open"][key]
    assert pos["tp"] > pos["entry"] > pos["stop"]
    assert sc.lab_verdict("a", r["symbol"], r["strategy"])[0] == "new"
    # свеча входа закрылась и задела цель
    candles = [list(c) for c in ex.ohlcv["SOL/USDT"]]
    candles[-1][2] = pos["tp"] * 1.01
    candles.append([candles[-1][0] + 300_000, candles[-1][4], candles[-1][4], candles[-1][4], candles[-1][4], 1])
    ex.ohlcv["SOL/USDT"] = candles
    run(sc.lab_step("a", now=1_000_000 + CANDLE_SEC))
    assert key not in sc.lab["open"]
    s = sc.lab["stats"][key]
    assert s["n"] == 1 and s["wins"] == 1 and s["sum"] > 0.5
    assert not ex.created  # лаборатория никогда не отправляет ордера
    assert "Лаборатория" in sc.lab_text() and "1 сделок (1 в плюс)" in sc.lab_text()


def test_lab_runs_once_per_candle():
    sc, trader, ex = make(balance=5.1, live=False)
    run(sc.rank("a"))
    run(sc.lab_step("a", now=1_000_000))
    key, _ = lab_key(sc)
    sc.lab["open"].clear()
    run(sc.lab_step("a", now=1_000_010))  # та же 5-минутка: ничего не делает
    assert key not in sc.lab["open"]


def test_strategy_losing_in_lab_is_not_traded_for_real():
    sc, trader, ex = make(balance=5.1)
    run(sc.rank("a"))
    key, r = lab_key(sc)
    sc.lab["stats"][key] = {"rets": [-6.1, 0.9, -6.1], "sum": -11.3, "n": 3, "wins": 1, "since": 0}
    assert run(sc.step()) == []
    assert not ex.created and "в лаборатории вживую -11.30%" in sc.last_reason
    sc.lab["stats"][key] = {"rets": [0.9, 0.9, -0.1], "sum": 1.7, "n": 3, "wins": 2, "since": 0}
    events = run(sc.step())
    assert events and "купил SOL" in events[0]


def test_shield_after_stop_loss_and_loss_streak():
    sc, trader, ex = make(balance=5.1, live=False)
    now = time.time()
    for coin in ("SOL", "XRP"):
        sc._cur = "a"
        sc.pos = {"exchange": "a", "symbol": f"{coin}/USDT", "amount": 1.0, "cost": 5.0, "entry": 5.0, "tp": 5.1,
                  "tp_pct": 0.009, "fee": 0.001, "opened_at": now - 3600, "order_id": "paper", "live": False,
                  "breakeven": False, "strategy": "dip"}
        out = sc._closed(4.7, "стоп-лосс", now)
    assert "не покупаю сутки" in out[0] and "2 убытка подряд" in out[0]
    assert abs(sc.st["blocked"]["SOL"][0] - (now + COIN_COOLDOWN_SEC)) < 1
    assert abs(sc.st["pause"]["a"] - (now + STREAK_PAUSE_SEC)) < 1
    assert run(sc.step()) == [] and "щит" in sc.last_reason
    assert {x["kind"] for x in sc.shield_rows()} == {"blocked", "pause"}
    sc.st["pause"]["a"] = 0
    assert run(sc.step()) == []  # пауза снята, но SOL после стоп-лосса под защитой
    assert "под защитой" in sc.last_reason and "SOL — был стоп-лосс" in sc.last_reason


def test_manual_sale_at_loss_does_not_trigger_shield():
    sc, trader, ex = make(balance=5.1, live=False)
    sc._cur = "a"
    sc.pos = {"exchange": "a", "symbol": "SOL/USDT", "amount": 1.0, "cost": 5.0, "entry": 5.0, "tp": 5.1,
              "tp_pct": 0.009, "fee": 0.001, "opened_at": time.time(), "order_id": "paper", "live": False,
              "breakeven": False}
    sc._closed(4.9, "продажа вручную", time.time())
    assert not sc.st["blocked"] and not sc.st["streak"].get("a")


def test_risk_coins_from_delisting_and_bad_news():
    now = time.time()
    anns = [Promo(id="1", title="Delisting of LOOM (LOOM)", kind="delisting", url="u", published=now - 86400,
                  coins=["LOOM"]),
            Promo(id="2", title="Old delisting (OLD)", kind="delisting", url="u", published=now - 60 * 86400,
                  coins=["OLD"])]
    news = [{"title": "Solana DeFi protocol hacked for $20M", "source": "CT", "published": now - 3600, "coins": ["SOL"]},
            {"title": "Bitcoin falls after exchange hack", "source": "CT", "published": now - 600, "coins": ["BTC"]},
            {"title": "Взлом моста Sui", "source": "ForkLog", "published": now - 20 * 3600, "coins": ["SUI"]},
            {"title": "Solana растёт", "source": "ForkLog", "published": now - 60, "coins": ["SOL"]}]
    risk = risk_coins(anns, news, now)
    assert set(risk) == {"LOOM", "SOL"}  # старое и BTC — не риск, Sui — новость старше 12 ч
    assert "делистинг" in risk["LOOM"][1] and "hacked" in risk["SOL"][1]
    sc, trader, ex = make(balance=5.1)
    sc.set_risk(risk)
    assert run(sc.step()) == [] and not ex.created and "hacked" in sc.last_reason


def test_signal_candles_downloaded_once_per_candle():
    sc, trader, ex = make(balance=5.1)
    period = int(time.time() // CANDLE_SEC)
    candles = dip_candles()
    shift = period * CANDLE_SEC * 1000 - candles[-1][0]
    ex.ohlcv["SOL/USDT"] = [[c[0] + shift, *c[1:]] for c in candles]  # последняя свеча — текущая 5-минутка
    calls = []
    orig = ex.fetch_ohlcv

    async def counting(*a, **kw):
        calls.append(a)
        return await orig(*a, **kw)

    ex.fetch_ohlcv = counting
    run(sc.signal_candles("a", "SOL/USDT"))
    run(sc.signal_candles("a", "SOL/USDT"))
    assert len(calls) == 1


def test_lab_command_and_report(bot_env):
    b, session = bot_env
    sc, trader, ex = make(balance=5.1, live=False)
    b.trader, b.scalper = trader, sc
    run(sc.rank("a"))
    key, _ = lab_key(sc)
    sc.lab["stats"][key] = {"rets": [0.9, 0.9, 0.9], "sum": 2.7, "n": 3, "wins": 3, "since": 0}
    run(send(b, "/lab"))
    assert "🟢 SOL" in session.sent[-1][1] and "sc:lab" in buttons(session.markups[-1])
    assert "Лаборатория: проверено вживую 1" in b.report_text() and "Автопилот" in b.report_text()
    ctx = b.ai_context()["trading"]["scalp_mode_5usd"]
    assert ctx["lab"][0]["verdict"] == "ok" and "shield" in ctx
