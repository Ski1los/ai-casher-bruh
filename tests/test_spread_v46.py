"""v46: спред — карантин проблемных монет (LUNC и т. п.), «висящие» спреды, тонкие рынки, риск хода цены,
несколько сделок одновременно и застрявшие сделки, которые больше не держат место."""

from __future__ import annotations

import asyncio
import time

from bot import spread as sp_mod
from bot.scanner import Opportunity, Quote, ScanResult
from tests.fake_exchange import mk_market
from tests.test_spread_v43 import scan, setup, sol_net
from tests.test_telegram_flow import OWNER, send

bot_env = __import__("tests.test_telegram_flow", fromlist=["bot_env"]).bot_env
app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env


def opp(coin, gross=2.0, vol=1e9, src="bybit", dst="bingx", ask=100.0):
    buy = Quote(src, f"{coin}/USDT", ask - 0.1, ask, vol, 0.001)
    sell = Quote(dst, f"{coin}/USDT", ask * (1 + gross / 100), ask * (1 + gross / 100) + 0.1, vol, 0.001)
    return Opportunity(f"{coin}/USDT", buy, sell, gross, gross - 0.2)


def result(*opps):
    res = ScanResult(opportunities=list(opps))
    res.finished_at = time.time()
    return res


def add_coin(bybit, bingx, coin, ask=100.0, bid=102.0):
    for w in (bybit, bingx):
        w.nets[coin] = {"nets": {"SOL": sol_net()}}
        w.markets[f"{coin}/USDT"] = mk_market(coin, "USDT", min_cost=1.0)
    bybit.tickers[f"{coin}/USDT"] = {"bid": ask - 0.1, "ask": ask, "last": ask, "quoteVolume": 1e9}
    bingx.tickers[f"{coin}/USDT"] = {"bid": bid, "ask": bid + 0.1, "last": bid, "quoteVolume": 1e9}


# --- карантин -------------------------------------------------------------------------------------------

def test_lunc_is_quarantined_from_the_start_and_unban_sticks(bot_env):
    b, _ = bot_env
    setup(b)
    sp = b.spread
    assert sp.bad("LUNC") and sp.bad("USTC")
    cands = sp.candidates(result(opp("LUNC", 3.0), opp("SOL")))
    assert [o.symbol for o in cands] == ["SOL/USDT"] and sp.skipped["bad"] == 1
    assert "Карантин" in sp.panel_text() and "LUNC" in sp.panel_text()
    assert sp.unban("LUNC") and not sp.bad("LUNC")
    _ = sp.st  # повторное чтение состояния не возвращает снятый карантин
    assert not sp.bad("LUNC")


def test_two_structural_rejects_in_a_day_quarantine_and_escalate(bot_env):
    b, _ = bot_env
    setup(b, bybit_net=sol_net(withdraw=False))
    sp = b.spread
    sp.st["on"] = True
    now = time.time()
    for i in range(2):
        b.transfers._cur.clear()
        ev = asyncio.run(sp.evaluate("SOL/USDT", "bybit", "bingx", 2.0))
        assert ev.reason == "wallet"
        sp._note(ev, now + i)
    x = sp.bad("SOL", now + 2)
    assert x and x["until"] - now >= 86000 and "вывод" in x["why"]
    # после карантина снова два отказа — уже 3 дня
    sp.st["bad"]["SOL"]["until"] = now  # истёк
    sp.strike("SOL", "снова закрыт", now + 10)
    x = sp.strike("SOL", "снова закрыт", now + 20)
    assert x and x["until"] - now >= 3 * 86400 - 100


def test_spread_hanging_for_12h_goes_to_quarantine(bot_env):
    b, _ = bot_env
    setup(b)
    sp = b.spread
    t0 = time.time()
    for k in range(0, 13 * 6 + 1):  # каждые 10 минут 13 часов подряд
        cands = sp.candidates(result(opp("ABC", 4.0)), now=t0 + k * 600)
    assert cands == [] and sp.bad("ABC", t0 + 13 * 3600)
    assert "держится" in sp.st["bad"]["ABC"]["why"]
    # если спред пропадал (перерыв > 15 мин), отсчёт начинается заново
    sp.candidates(result(opp("XYZ", 4.0)), now=t0)
    sp.candidates(result(opp("XYZ", 4.0)), now=t0 + 11 * 3600)
    sp.candidates(result(opp("XYZ", 4.0)), now=t0 + 12.5 * 3600)
    assert not sp.bad("XYZ", t0 + 12.5 * 3600)


def test_thin_markets_are_skipped(bot_env):
    b, _ = bot_env
    setup(b)
    sp = b.spread
    assert sp.candidates(result(opp("TINY", vol=20_000))) == [] and sp.skipped["volume"] == 1


def test_volatile_coin_rejected_when_price_moves_more_than_profit(bot_env):
    b, _ = bot_env
    bybit, bingx = setup(b)
    # минутные свечи: цена скачет на ±1.5% каждую минуту — за 3 мин перевода уйдёт больше прибыли ~1.2%
    closes = [100 * (1.015 if i % 2 else 0.985) for i in range(61)]
    bingx.ohlcv = {"SOL/USDT": [[i * 60000, c, c, c, c, 1] for i, c in enumerate(closes)]}
    ev = asyncio.run(b.spread.evaluate("SOL/USDT", "bybit", "bingx", 2.0))
    assert ev.reason == "volatile" and "обычно уходит" in ev.checks[-1][1]
    # спокойная цена — проходит, и в анализе видно, что риск проверен
    bingx.ohlcv = {"SOL/USDT": [[i * 60000, 100 + i * 0.001, 0, 0, 100 + i * 0.001 + (i % 2) * 0.01, 1]
                                for i in range(61)]}
    ev = asyncio.run(b.spread.evaluate("SOL/USDT", "bybit", "bingx", 2.0))
    assert ev.ok and any("ход цены" in t for s, t in ev.checks if s == "ok")


# --- больше позиций --------------------------------------------------------------------------------------

def test_several_runs_at_once_each_a_different_coin(bot_env):
    b, _ = bot_env
    bybit, bingx = setup(b)
    add_coin(bybit, bingx, "AAA")
    add_coin(bybit, bingx, "BBB")
    sp = b.spread
    sp.st["on"] = True
    res = result(opp("SOL"), opp("AAA"), opp("BBB"))
    asyncio.run(sp.process(res))
    assert sorted(r["coin"] for r in sp.active()) == ["AAA", "BBB", "SOL"]
    assert bybit.created == [] and bybit.withdrawn == []  # тест на бумаге
    # лимит одновременных сделок
    for r in sp.st["runs"]:
        r["stage"] = "done"
    sp.st["max_runs"] = 1
    asyncio.run(sp.process(res))
    assert len(sp.active()) == 1


def test_stuck_paper_run_frees_its_slot_and_quarantines_coin(bot_env):
    b, _ = bot_env
    _, bingx = setup(b)
    sp = b.spread
    sp.st["on"] = True
    asyncio.run(sp.process(scan(b)))
    run = sp.active()[0]
    del bingx.tickers["SOL/USDT"]  # пара пропала: продать нельзя (так раньше «висел» LUNC)
    asyncio.run(sp.step(now=run["eta"] + 1))
    assert run["stage"] == "arrived"
    msgs = asyncio.run(sp.step(now=run["eta"] + sp_mod.PAPER_STUCK_SEC + 10))
    assert run["stage"] == "stuck" and sp.active() == [] and "карантин" in msgs[0]
    assert sp.bad("SOL") and sp.st["stuck"] == 1


def test_live_transfer_over_a_day_becomes_stuck(bot_env):
    b, _ = bot_env
    setup(b, live=True, book=[{"address": "x", "chain": "SOL", "status": "0"}])
    sp = b.spread
    run = {"id": "r1", "mode": "live", "coin": "SOL", "src": "bybit", "dst": "bingx", "symbol": "SOL/USDT",
           "network": "SOL", "minutes": 3, "stage": "sent", "opened": time.time() - 25 * 3600, "spent": 10.0,
           "bought": 0.1, "sent": 0.1, "fee": 0.0, "exp_usd": 10.1, "exp_pct": 1.0, "gross": 2.0,
           "eta": time.time() - 25 * 3600, "item": "nope", "item_at": None, "arrived_at": None}
    sp.st["runs"].append(run)
    msgs = asyncio.run(sp.step())
    assert run["stage"] == "stuck" and "больше суток" in run["note"] and msgs


def test_withdraw_failure_after_buy_quarantines_coin(bot_env):
    b, _ = bot_env
    setup(b)
    sp = b.spread
    run = {"id": "r2", "mode": "test", "coin": "SOL", "src": "bybit", "dst": "bingx", "symbol": "SOL/USDT",
           "spent": 10.0, "opened": time.time(), "exp_pct": 1.0, "note": "вывод не прошёл: сеть на техработах"}
    sp._finish(run, 9.95, "back", time.time())
    assert sp.bad("SOL") and "вывод не прошёл" in sp.st["bad"]["SOL"]["why"]


# --- команды и приложение --------------------------------------------------------------------------------

def test_commands_ban_unban_runs(bot_env):
    b, session = bot_env
    setup(b)
    sp = b.spread

    async def flow():
        await send(b, "/spread ban PEPE 2")
        assert sp.bad("PEPE") and "карантине на 2" in session.sent[-1][1]
        await send(b, "/spread bad")
        assert "PEPE" in session.sent[-1][1] and "LUNC" in session.sent[-1][1]
        await send(b, "/spread unban PEPE")
        assert not sp.bad("PEPE")
        await send(b, "/spread runs 5")
        assert sp.max_runs == 5
        await send(b, "/spread runs 9")
        assert sp.max_runs == 5 and "1–5" in session.sent[-1][1]
        await send(b, "/spread")
        assert "до 5 сделок одновременно" in session.sent[-1][1] or any(
            "до 5 сделок одновременно" in t for _, t in session.sent[-3:])

    asyncio.run(flow())


def test_mini_app_quarantine_and_runs(app_env):
    from tests.test_webapp import FRIEND, call, get, post
    b, _ = app_env
    setup(b)

    async def flow(client):
        _, d = await get(client, "/api/spread")
        assert d["max_runs"] == 3 and any(x["coin"] == "LUNC" for x in d["bad"])
        st, _ = await post(client, "/api/spread", {"unban": "LUNC"}, uid=FRIEND)
        assert st == 403
        _, d = await post(client, "/api/spread", {"unban": "LUNC", "max_runs": 2})
        assert not any(x["coin"] == "LUNC" for x in d["bad"]) and d["max_runs"] == 2
        st, _ = await post(client, "/api/spread", {"max_runs": 7})
        assert st == 400
        _, d = await post(client, "/api/spread", {"ban": "DOGE"})
        assert any(x["coin"] == "DOGE" for x in d["bad"])

    call(b, flow)


def test_quarantine_memory_stays_small(bot_env):
    b, _ = bot_env
    setup(b)
    sp = b.spread
    for i in range(100):
        sp.ban(f"C{i}", 3600 + i, "тест")
    assert len(sp.st["bad"]) <= 60
    assert OWNER  # фикстура владельца на месте
