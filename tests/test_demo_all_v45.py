"""v45: вся демо-торговля работает и без ключей бирж (по публичным ценам), и про каждое демо видно, почему нет сделок."""

import asyncio

from bot import demo_report
from bot.config import Settings
from bot.scalper import PAPER_START_USD, Scalper
from bot.trading import Trader
from tests.fake_exchange import FakeTradingExchange, mk_market
from tests.test_scalper import dip_candles
from tests.test_ai_flow import press
from tests.test_telegram_flow import OWNER, send

bot_env = __import__("tests.test_telegram_flow", fromlist=["bot_env"]).bot_env
from tests.test_trading import FakeScanner, make_trader, triangle_opp


def run(coro):
    return asyncio.run(coro)


def keyless_scalper(live=False):
    ex = FakeTradingExchange("ExA", {"SOL/USDT": mk_market("SOL", "USDT", min_cost=5.0)},
                             {"SOL/USDT": {"bid": 98.40, "ask": 98.42, "quoteVolume": 1e8}}, {"USDT": 0.0})
    ex.ohlcv = {"SOL/USDT": dip_candles()}
    trader = Trader(FakeScanner({"a": ex}), Settings(exchanges=[]), env={})  # ключей нет совсем
    trader.state.live = live
    sc = Scalper(trader)
    sc.turn_on()
    return sc, trader, ex


# --- автопилот (режим $5) в тесте без ключа ---------------------------------------------------------------

def test_autopilot_paper_trades_without_keys():
    sc, trader, ex = keyless_scalper()
    assert not trader.working and sc.exchanges() == ["a"]
    events = run(sc.step())
    assert events and "ТЕСТ" in events[0]
    assert not ex.created  # ордера не уходят
    assert "a" in sc._paper_virtual and sc.st["paper"]["a"] < PAPER_START_USD
    ex.tickers["SOL/USDT"] = {"bid": 120.0, "ask": 120.1, "quoteVolume": 1e8}
    events = run(sc.step())
    assert "продал SOL" in events[0] and sc.st["paper"]["a"] > PAPER_START_USD
    assert "виртуальные" in sc.status_text()


def test_autopilot_live_still_needs_a_key():
    sc, trader, ex = keyless_scalper(live=True)
    assert sc.exchanges() == []
    assert run(sc.step()) == [] and "ключ" in sc.last_reason
    assert not ex.created


def test_paper_out_of_money_suggests_reset_and_reset_works():
    sc, trader, ex = keyless_scalper()
    sc.st["paper"]["a"] = 1.0
    sc._paper_virtual.add("a")
    assert run(sc.step()) == []
    assert "/scalp reset" in sc.last_reason
    assert sc.paper_reset() == 0 and sc.st["paper"] == {}
    events = run(sc.step())
    assert events and "ТЕСТ" in events[0]


def test_learning_without_keys_stays_opt_in():
    """Лаборатория на публичных свечах без ключа — только при полном автомате (public=True), как раньше."""
    sc, trader, ex = keyless_scalper()
    called = []

    async def rank(name):
        called.append(name)

    async def lab_step(name):
        return None

    sc.rank, sc.lab_step = rank, lab_step
    run(sc.learn())
    assert called == []
    run(sc.learn(public=True))
    assert called == ["a"]


# --- треугольники в тесте без ключа -------------------------------------------------------------------

def test_triangles_paper_without_keys_and_live_blocked():
    trader, ex = make_trader(env={}, auto_min_profit_percent=0.3)

    class R:
        triangles = [triangle_opp(trader)]

    trader.state.auto = True
    [res] = run(trader.auto_step(R))
    assert res.executed and not res.live and "публичным стаканам" in res.reason
    assert trader.auto_status["paper_public"] == ["a"]
    assert "публичным стаканам" in trader.auto_status_text()
    assert not ex.created
    # реальный режим без ключа — по-прежнему ничего
    trader.recent_tri.clear()
    trader.state.live = True
    assert run(trader.auto_step(R)) == []
    assert trader.auto_status["code"] == "no_keys"


# --- демо-арбитраж: не стираем историю, добавляем счёт $50 ---------------------------------------------

def test_demo_resume_keeps_history_and_adds_amounts(bot_env):
    b, _ = bot_env
    b.demo.start([5.0], OWNER)
    acc = b.demo.accounts["cross-5"]
    acc.rejects["min_order"] = 7
    acc.best_rejected, acc.best_rejected_percent = "BTC Bybit→BingX", 0.4
    b.demo.stop()
    added = b.demo.resume([5.0, 50.0])
    assert b.demo.active and b.demo.accounts["cross-5"] is acc  # история та же
    assert "cross-50" in b.demo.accounts and any("$50" in x for x in added)
    why = b.demo.why_idle()
    assert "минимального ордера" in why and "BTC" in why


def test_autonomy_adds_comparison_account_to_running_demo(bot_env):
    b, _ = bot_env
    b.demo.start([5.0], OWNER)
    done = b.autonomy.ensure()
    assert "cross-50" in b.demo.accounts and any("$50" in x for x in done)
    b.demo.stop()  # остановленное руками не трогаем
    before = set(b.demo.accounts)
    b.autonomy.ensure()
    assert not b.demo.active and set(b.demo.accounts) == before


# --- /demos: обзор и «Включить всё демо» ---------------------------------------------------------------

def test_overview_lists_every_engine_with_reason(bot_env):
    b, _ = bot_env
    rows = demo_report.rows(b)
    ids = [r["id"] for r in rows]
    assert ids == ["arb", "tri", "pilot", "lab", "spread", "news", "copy", "meme", "train", "mirror", "pulse"]
    assert all(r["why"] for r in rows)
    text = demo_report.text(b)
    assert "Вся демо-торговля" in text and ("Почему нет сделок" in text or "Состояние" in text)
    assert len(text) < 4096 * 2


def test_enable_all_turns_on_demos_without_money(bot_env):
    b, _ = bot_env
    b.copy.st["on"] = False
    b.mentors.train.st["on"] = False
    b.trader.state.live = False
    done = demo_report.enable_all(b)
    assert b.demo.active and "cross-50" in b.demo.accounts
    assert b.copy.on and b.meme.demo_on and b.mentors.train.st["on"] and b.scalper.on
    assert any("автопилот" in x for x in done) and not b.trader.state.live
    assert not b.spread.st["on"]  # без ключей обеих бирж спред не включаем (ему нужны адреса и комиссии вывода)
    assert demo_report.enable_all(b) == []  # второй раз — ничего


def test_enable_all_never_touches_real_mode(bot_env):
    b, _ = bot_env
    b.trader.state.live = True
    demo_report.enable_all(b)
    assert not b.scalper.on and b.trader.state.live


def test_demos_command_and_buttons(bot_env):
    b, session = bot_env
    b.trader.state.live = False

    async def flow():
        await send(b, "/demos")
        assert "Вся демо-торговля" in session.sent[-1][1]
        await press(b, "dm:on")
        assert "Включил демо" in session.sent[-1][1] and b.scalper.on
        await press(b, "dm:reset")
        assert "заново" in session.sent[-1][1]
        await press(b, "dm:on", user=12345)  # чужой — ничего не меняет

    run(flow())


app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env


def test_mini_app_shows_demos_and_turns_them_on(app_env):
    from tests.test_webapp import FRIEND, call, get, post
    b, _ = app_env
    b.trader.state.live = False
    b.copy.st["on"] = False

    async def flow(client):
        _, d = await get(client, "/api/profit")
        assert [r["id"] for r in d["demos"]][:3] == ["arb", "tri", "pilot"]
        assert all("why" in r for r in d["demos"])
        st, _ = await post(client, "/api/profit", {"demos_on": True}, uid=FRIEND)
        assert st == 403
        _, d = await post(client, "/api/profit", {"demos_on": True})
        assert "Включил" in d["done"][0] and b.copy.on and b.scalper.on
        assert next(r for r in d["demos"] if r["id"] == "copy")["on"]

    call(b, flow)
