"""v43: ⚖️ спред-арбитраж между биржами — анализ до покупки, тест на бумаге, реальная сделка, откат, кнопки."""

from __future__ import annotations

import asyncio
import time

import pytest

from bot import spread as sp_mod
from bot import transfers as tr
from bot import users_ui
from bot.scanner import Opportunity, Quote, ScanResult
from tests.fake_exchange import mk_market
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import send
from tests.test_transfers_v42 import BINGX_ADDR, BYBIT_ADDR, Wallet, net

FRIEND = 778
app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env


def sol_net(fee=0.0005, withdraw=True, deposit=True, network="SOL", contract=None):
    n = net(network, fee, withdraw=withdraw, deposit=deposit, mn=0.01)
    if contract:
        n["info"] = {"contractAddress": contract}
    return n


def setup(b, live=False, bybit_usdt=100.0, ask=100.0, bid=102.0, bybit_net=None, bingx_net=None, book=None):
    """Bybit: SOL дешевле (ask 100), BingX: дороже (bid 102). Комиссия вывода SOL — 0.0005."""
    bybit = Wallet("Bybit", {"SOL": {"nets": {"SOL": bybit_net or sol_net()}}, "USDT": {"nets": {"APT": net("APT", 0.01)}}},
                   BYBIT_ADDR, {"USDT": bybit_usdt})
    bingx = Wallet("BingX", {"SOL": {"nets": {"SOL": bingx_net or sol_net()}}, "USDT": {"nets": {"APT": net("APT", 0.01)}}},
                   BINGX_ADDR, {"USDT": 5.0})
    bybit.markets["SOL/USDT"] = mk_market("SOL", "USDT", min_cost=1.0)
    bingx.markets["SOL/USDT"] = mk_market("SOL", "USDT", min_cost=1.0)
    bybit.tickers["SOL/USDT"] = {"bid": ask - 0.1, "ask": ask, "last": ask, "quoteVolume": 1e9}
    bingx.tickers["SOL/USDT"] = {"bid": bid, "ask": bid + 0.1, "last": bid, "quoteVolume": 1e9}
    bybit.book_rows = book

    async def query_address(params=None):
        rows = [] if book is None else book
        return {"result": {"rows": rows if (params or {}).get("coin") != "baseCoin" else []}}

    bybit.privateGetV5AssetWithdrawQueryAddress = query_address
    b.scanner.exchanges = {"bybit": bybit, "bingx": bingx}
    b.trader.creds = {"bybit": {"apiKey": "k", "secret": "s"}, "bingx": {"apiKey": "k", "secret": "s"}}
    for n, c in b.trader.creds.items():
        b.trader._apply_creds(n, c)
    b.trader.key_status = {}
    b.trader.key_info = {"bybit": {"withdraw": True, "spot_trade": True, "ip_bound": True},
                         "bingx": {"withdraw": True, "spot_trade": True, "ip_bound": True}}
    b.transfers.env = {}
    b.spread._book.clear()
    b.transfers.st["allowed"] = live
    b.trader.state.live = live
    b.spread.st["paper"] = not live
    return bybit, bingx


def scan(b, gross=2.0, src="bybit", dst="bingx"):
    buy = Quote(src, "SOL/USDT", 99.9, 100.0, 1e9, 0.001)
    sell = Quote(dst, "SOL/USDT", 102.0, 102.1, 1e9, 0.001)
    res = ScanResult(opportunities=[Opportunity("SOL/USDT", buy, sell, gross, gross - 0.2)])
    res.finished_at = time.time()
    b.scanner.last_result = res
    return res


def ev_of(b, gross=2.0):
    return asyncio.run(b.spread.evaluate("SOL/USDT", "bybit", "bingx", gross))


def test_full_analysis_counts_every_fee_before_buying(bot_env):
    b, _ = bot_env
    setup(b)
    ev = ev_of(b)
    assert ev.ok, ev.checks
    assert ev.usd == pytest.approx(10.0)              # лимит сделки max_trade = $10
    assert ev.coins == pytest.approx(0.0999)          # 10 / 100 минус торговая комиссия 0.1%
    assert ev.send == pytest.approx(0.0994)           # минус комиссия вывода 0.0005 SOL
    assert ev.network == "SOL" and ev.minutes == 3 and ev.address == BINGX_ADDR
    assert ev.received == pytest.approx(0.0994 * 102 * 0.999)
    assert ev.pct == pytest.approx((0.0994 * 102 * 0.999 / 10 - 1) * 100)
    assert any("после ВСЕХ комиссий" in t for _, t in ev.checks)
    assert any("адресной книге Bybit" in t for s, t in ev.checks if s == "info")  # тест: книга не обязательна


def test_every_reason_to_skip(bot_env):
    b, _ = bot_env

    def reason(**kw):
        gross = kw.pop("gross", 2.0)
        setup(b, **kw)
        b.transfers._cur.clear()
        return ev_of(b, gross).reason

    assert reason(bybit_net=sol_net(withdraw=False)) == "wallet"           # вывод закрыт (как Terra)
    assert reason(bingx_net=sol_net(deposit=False)) == "wallet"            # ввод закрыт на бирже продажи
    assert reason(bybit_net=sol_net(network="BTC"), bingx_net=sol_net(network="BTC")) == "net"  # медленная сеть
    assert reason(bybit_net=sol_net(contract="0xAAA"), bingx_net=sol_net(contract="0xBBB")) == "suspect"
    assert reason(gross=6.0) == "suspect"                                  # огромный спред без контракта
    assert reason(gross=6.0, bybit_net=sol_net(contract="0xA"), bingx_net=sol_net(contract="0xa")) != "suspect"
    assert reason(bid=100.5) == "fees"                                     # спред меньше комиссий
    assert reason(bybit_net=sol_net(fee=0.01)) == "fees"                   # комиссия вывода съедает прибыль
    assert reason(bybit_usdt=0.5) == "money"
    assert reason(live=True) == "addr"                                     # реальный режим: адреса нет в книге
    assert reason(live=True, book=[{"address": BINGX_ADDR, "chain": "SOL", "status": "0"}]) == ""
    assert reason(live=True, book=[{"address": BINGX_ADDR, "chain": "SOL", "status": "1"}]) == "addr"  # новый: 24 ч
    setup(b, live=True)
    b.transfers.st["allowed"] = False
    assert ev_of(b).reason == "keys"


def test_paper_trade_in_test_mode_moves_no_money(bot_env):
    b, _ = bot_env
    bybit, bingx = setup(b)
    sp = b.spread
    sp.st["on"] = True
    msgs = asyncio.run(sp.process(scan(b)))
    assert msgs == []  # тест на бумаге: одно сообщение — с итогом
    run = sp.active()[0]
    assert run["mode"] == "test" and run["stage"] == "sent"
    assert bybit.created == [] and bybit.withdrawn == [] and bingx.created == []
    # пока «едет» — ничего не происходит, новых сделок нет (одна за раз)
    assert asyncio.run(sp.process(scan(b))) == []
    # доехало: продаёт по стакану BingX в момент прибытия
    msgs = asyncio.run(sp.step(now=run["eta"] + 1))
    assert run["stage"] == "done" and run["pnl"] == pytest.approx(0.0994 * 102 * 0.999 - 10, abs=1e-5)
    assert "Спред завершён" in msgs[0] and "ТЕСТ" in msgs[0]
    assert sp.st["totals"]["test"][0] == 1 and sp.st["totals"]["live"][0] == 0
    rec = b.trader.state.log[-1]
    assert rec["kind"] == "spread" and rec["live"] is False and rec["source"] == "spread"
    assert bybit.balance["USDT"] == 100.0  # деньги не двигались


def test_paper_trade_waits_for_breakeven_then_stop(bot_env):
    b, _ = bot_env
    _, bingx = setup(b)
    sp = b.spread
    sp.st["on"] = True
    asyncio.run(sp.process(scan(b)))
    run = sp.active()[0]
    bingx.tickers["SOL/USDT"]["bid"] = 100.0  # пока ехали, цена упала: продать в плюс нельзя
    asyncio.run(sp.step(now=run["eta"] + 1))
    assert run["stage"] == "arrived"
    bingx.tickers["SOL/USDT"]["bid"] = 96.0  # просадка больше 3% от безубытка — стоп
    msgs = asyncio.run(sp.step(now=run["eta"] + 60))
    assert run["stage"] == "done" and run["pnl"] < 0 and "стоп" in msgs[0]


def test_decide_rules():
    run = {"spent": 10.0, "arrived_at": 1000.0}
    assert sp_mod.Spread.decide(run, 10.2, 1001) == "в плюс"
    assert sp_mod.Spread.decide(run, 9.9, 1001) is None
    assert "стоп" in sp_mod.Spread.decide(run, 9.6, 1001)
    assert "2 ч" in sp_mod.Spread.decide(run, 9.9, 1000 + sp_mod.HOLD_SEC)


def test_live_trade_buys_withdraws_and_sells(bot_env):
    b, _ = bot_env
    bybit, bingx = setup(b, live=True, book=[{"address": BINGX_ADDR, "chain": "SOL", "status": "0"}])
    sp = b.spread
    sp.st["on"] = True
    msgs = asyncio.run(sp.process(scan(b)))
    assert "Спред: сделка открыта" in msgs[0], msgs
    assert bybit.created[0][:2] == ("buy_cost", "SOL/USDT")
    code, amount, address, params = bybit.withdrawn[0]
    assert (code, address, params["network"]) == ("SOL", BINGX_ADDR, "SOL")
    assert amount == pytest.approx(0.0999 - 0.0005)
    run = sp.active()[0]
    assert run["mode"] == "live" and run["stage"] == "sent"
    # перевод дошёл (так его отмечает слежение bot/transfers.py)
    item = b.transfers.st["items"][-1]
    item["status"], item["got"] = "done", amount
    bingx.balance["SOL"] = amount
    msgs = asyncio.run(sp.step())
    assert run["stage"] == "done" and run["pnl"] > 0
    assert bingx.created[-1][:2] == ("sell", "SOL/USDT")
    assert bingx.balance["USDT"] == pytest.approx(5.0 + amount * 102 * 0.999)
    assert "Спред завершён" in msgs[0] and "ТЕСТ" not in msgs[0]
    kinds = [(r["kind"], r["live"]) for r in b.trader.state.log[-2:]]
    assert kinds == [("buy", True), ("spread", True)]
    assert sp.st["totals"]["live"] == [1, pytest.approx(run["pnl"])]


def test_live_withdraw_refused_after_buy_sells_back(bot_env):
    b, _ = bot_env
    bybit, _ = setup(b, live=True, book=[{"address": BINGX_ADDR, "chain": "SOL", "status": "0"}])

    async def refuse(code, amount, address, params=None):
        raise RuntimeError("withdraw address not in whitelist")

    bybit.withdraw = refuse
    sp = b.spread
    sp.st["on"] = True
    msgs = asyncio.run(sp.process(scan(b)))
    run = sp.st["runs"][-1]
    assert run["stage"] == "back" and "вывод не прошёл" in run["note"]
    assert "Продаю обратно" in msgs[0]
    msgs = asyncio.run(sp.step())
    assert run["stage"] == "done" and run["pnl"] < 0 and run["pnl"] > -0.1  # потеря — только комиссии и спред стакана
    assert bybit.created[-1][:2] == ("sell", "SOL/USDT")
    assert "вернул обратно" in msgs[0]


def test_address_book_hint_when_only_book_is_missing(bot_env):
    b, _ = bot_env
    setup(b, live=True)
    sp = b.spread
    sp.st["on"] = True
    msgs = asyncio.run(sp.process(scan(b)))
    assert len(msgs) == 1 and "📒" in msgs[0] and BINGX_ADDR in msgs[0] and "SOL" in msgs[0]
    assert not sp.active()
    assert sp.st["checks"][-1]["reason"] == "addr"
    # второй раз сразу не напоминает (пауза по связке)
    assert asyncio.run(sp.process(scan(b))) == []


def test_stop_button_pauses_live_run(bot_env):
    b, _ = bot_env
    setup(b, live=True, book=[{"address": BINGX_ADDR, "chain": "SOL", "status": "0"}])
    sp = b.spread
    sp.st["on"] = True
    asyncio.run(sp.process(scan(b)))
    asyncio.run(b.trader.panic())
    assert sp.st["on"] is False and b.trader.state.live is False and sp.st["paper"] is True
    msgs = asyncio.run(sp.step())
    assert "Спред на паузе" in msgs[0] and "/sell bingx SOL all" in msgs[0]
    assert asyncio.run(sp.step()) == []  # говорит один раз


def test_telegram_panel_buttons_and_settings(bot_env):
    b, session = bot_env
    setup(b)
    users_ui.add_user(b, FRIEND, "Петя", "ссылка")
    scan(b)

    async def run():
        await send(b, "/spread")
        text = session.sent[-1][1]
        assert "Спред-арбитраж" in text and "ТЕСТ" in text
        assert {"sp:on", "sp:check", "sp:hist", "sp:addr"} <= set(buttons(session.markups[-1]))
        await press(b, "sp:on", user=FRIEND)  # приглашённый смотреть может, включать — нет
        assert b.spread.st["on"] is False
        await press(b, "sp:on")
        assert b.spread.st["on"] is True
        await send(b, "/spread min 1.5")
        assert b.spread.st["min_pct"] == 1.5
        await send(b, "/spread min 50")
        assert b.spread.st["min_pct"] == 1.5 and "Напиши так" in session.sent[-1][1]
        await send(b, "/spread check SOL")
        assert "Проверка спреда" in session.sent[-1][1] and "SOL" in session.sent[-1][1]
        await press(b, "sp:check")
        assert "Проверка спреда" in session.sent[-1][1]
        await press(b, "sp:addr")
        assert "Адреса для адресных книг" in session.sent[-1][1] and BINGX_ADDR in session.sent[-1][1]
        await send(b, "/spread off")
        assert b.spread.st["on"] is False
        # остальной бот торгует по-настоящему — а спред всё равно на бумаге, пока не переключишь
        b.trader.state.live = True
        assert b.spread.live is False and "sp:real" in buttons(panel_markup(b))
        await send(b, "/spread real")
        assert "Разрешить переводы" in session.sent[-1][1] and b.spread.st["paper"] is True
        b.transfers.st["allowed"] = True
        await send(b, "/spread real", user=FRIEND)
        assert b.spread.st["paper"] is True
        await press(b, "sp:real")
        assert "РЕАЛЬНЫЕ" in session.sent[-1][1] and "sp:real2" in buttons(session.markups[-1])
        await press(b, "sp:real2")
        assert b.spread.st["paper"] is False and b.spread.live is True
        await press(b, "sp:paper")
        assert b.spread.st["paper"] is True and b.spread.live is False

    asyncio.run(run())


def panel_markup(b):
    return sp_mod.panel_kb(b)


def test_state_survives_restart(bot_env):
    b, _ = bot_env
    setup(b)
    b.spread.st["on"] = True
    asyncio.run(b.spread.process(scan(b)))
    saved = b.trader.export()
    b.trader.restore(saved)
    assert b.spread.st["on"] and b.spread.active()[0]["coin"] == "SOL"


def test_floor_step_and_all_amount_never_round_up():
    assert tr.floor_step(1.23456789, 0.001) == 1.234
    assert tr.floor_step(0.29, None) == 0.29
    assert tr.floor_step(1.123456789, None) == 1.12345678


def test_plan_reason_codes():
    assert sp_mod.plan_reason(["вывод SOL с Bybit сейчас закрыт самой биржей"]) == "wallet"
    assert sp_mod.plan_reason(["$50 больше лимита $30 за перевод"]) == "limits"
    assert sp_mod.plan_reason(["у ключа Bybit нет права вывода"]) == "keys"
    assert sp_mod.plan_reason(["меньше минимума вывода 1 SOL"]) == "min"


def test_mini_app_spread_api(app_env):
    from tests.test_webapp import FRIEND as VIEWER
    from tests.test_webapp import call, get, post

    b, _ = app_env
    setup(b)
    scan(b)

    async def fn(client):
        st, r = await get(client, "/api/spread")
        assert st == 200 and r["on"] is False and r["live"] is False and r["max_usd"] == 10
        st, r = await post(client, "/api/spread", {"on": True}, uid=VIEWER)
        assert st == 403
        st, r = await post(client, "/api/spread", {"on": True})
        assert st == 200 and r["on"] is True
        st, r = await post(client, "/api/spread/check", {})
        assert st == 200 and r["items"][0]["coin"] == "SOL" and r["items"][0]["ok"] and r["items"][0]["pct"] > 1
        st, r = await post(client, "/api/spread/check", {"coin": "SOL"})
        assert st == 200 and r["items"][0]["src"] == "Bybit"
        # реальные сделки: нужны /live, разрешённые переводы и подтверждение
        st, r = await post(client, "/api/spread", {"paper": False, "confirm": True})
        assert st == 400 and "/live" in r["error"]
        b.trader.state.live = True
        st, r = await post(client, "/api/spread", {"paper": False, "confirm": True})
        assert st == 400 and "Разрешить переводы" in r["error"]
        b.transfers.st["allowed"] = True
        st, r = await post(client, "/api/spread", {"paper": False})
        assert st == 400 and "подтверждение" in r["error"]
        st, r = await post(client, "/api/spread", {"paper": False, "confirm": True})
        assert st == 200 and r["on"] and r["live"] and not r["paper"]
        st, r = await post(client, "/api/spread", {"paper": True}, uid=VIEWER)
        assert st == 403
        return True

    assert call(b, fn)
