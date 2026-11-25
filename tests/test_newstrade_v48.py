"""v48: 🤖 торговля по новостям — новость → токен → проверки → демо-сделка → экзамен → реал."""

from __future__ import annotations

import asyncio
import time

from bot import news_trade as ntm
from tests.fake_exchange import FakeTradingExchange, mk_market
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import OWNER, send

bot_env = __import__("tests.test_telegram_flow", fromlist=["bot_env"]).bot_env
app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env


def run(coro):
    return asyncio.run(coro)


def candles(now, base, hours=3):
    """5-минутные свечи: цена стояла на base."""
    start = int((now - hours * 3600) // 300 * 300)
    return [[(start + i * 300) * 1000, base, base, base, base, 1000] for i in range(hours * 12 + 1)]


def setup(b, coin="TON", price=2.0, vol=5e6, spread=0.001, base=None, usdt=50.0):
    now = time.time()
    ex = FakeTradingExchange("ExA", {f"{coin}/USDT": mk_market(coin, "USDT"), "BTC/USDT": mk_market("BTC", "USDT")},
                             {f"{coin}/USDT": {"bid": price, "ask": price * (1 + spread), "quoteVolume": vol,
                                               "last": price},
                              "BTC/USDT": {"bid": 50_000, "ask": 50_001, "quoteVolume": 1e9, "last": 50_000}},
                             {"USDT": usdt})
    ex.ohlcv = {f"{coin}/USDT": candles(now, price if base is None else base)}
    b.scanner.exchanges = {"a": ex}
    b.news.st["on"] = True
    return b.newstrade, ex


def ev(coins=("TON",), cat="listing_major", ago=120, title="Binance Will List Toncoin (TON)", eid="e1"):
    now = time.time()
    return {"id": eid, "t": now - ago, "seen": now - ago, "title": title, "ru": "", "coins": list(coins), "cat": cat,
            "old": False}


def set_price(ex, coin, price):
    t = ex.tickers[f"{coin}/USDT"]
    t.update(bid=price, ask=price * 1.001, last=price)


# --- поиск токена ----------------------------------------------------------------------------------------

def test_name_candidates_skip_tickers_and_common_words():
    assert ntm.name_candidates("Binance Will List Walrus and Hyperliquid on Monday") == ["Walrus", "Hyperliquid"]
    assert ntm.name_candidates("SEC sues Coinbase over ETH staking") == []


def test_token_from_ticker_and_coingecko(bot_env):
    b, _ = bot_env
    nt, ex = setup(b, coin="WAL")
    now = time.time()
    e = ev(coins=["BTC", "USDT"], title="Upbit adds Walrus to KRW market")  # BTC/стейблы — не токен новости
    calls = []

    async def fake_cg(word, now):
        calls.append(word)
        return {"Walrus": "WAL"}.get(word)

    nt.coingecko = fake_cg
    assert run(nt.find_tokens(e, now, [4])) == ["WAL"] and calls == ["Walrus"]
    assert "WAL" in e["coins"]  # модуль новостей тоже будет мерить влияние на WAL
    e2 = ev(coins=["NOPE"])
    assert run(nt.find_tokens(e2, now, [4])) == []  # нет пары к USDT на наших биржах
    # новость «про рынок» (ФРС) — названия в CoinGecko не ищем
    calls.clear()
    assert run(nt.find_tokens(ev(coins=[], cat="dovish", title="Powell Signals Cut"), now, [4])) == [] and not calls


def test_coingecko_parses_and_caches(bot_env, monkeypatch):
    b, _ = bot_env
    nt = b.newstrade
    hits = []

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self, content_type=None):
            return {"coins": [{"name": "Walrus Fake", "symbol": "WALF", "market_cap_rank": 50},
                              {"name": "Walrus", "symbol": "wal", "market_cap_rank": 120}]}

    class Sess:
        closed = False

        def get(self, url, params=None, headers=None):
            hits.append(params["query"])
            return Resp()

    nt._session = Sess()
    assert run(nt.coingecko("Walrus", 1000.0)) == "WAL"
    assert run(nt.coingecko("Walrus", 2000.0)) == "WAL" and hits == ["Walrus"]  # из кэша


# --- проверки перед покупкой ----------------------------------------------------------------------------

def test_every_skip_reason(bot_env, monkeypatch):
    b, _ = bot_env
    nt, ex = setup(b)
    now = time.time()

    def why(e=None, **kw):
        plan, reason = run(nt.check(e or ev(), "TON", now))
        assert plan is None
        return reason

    assert "старше 30 мин" in why(ev(ago=3600))
    assert "меньше порога" in why(ev(cat="partner"))
    monkeypatch.setattr(b.scalper, "shield_reason", lambda c, now=None: "взлом")
    assert "под щитом" in why()
    monkeypatch.setattr(b.scalper, "shield_reason", lambda c, now=None: None)
    monkeypatch.setattr(b.news, "pause", lambda now=None: (now + 60, "данные США"))
    assert "пауза: данные США" in why()
    monkeypatch.setattr(b.news, "pause", lambda now=None: None)
    ex.tickers["TON/USDT"]["quoteVolume"] = 10_000
    assert "тонкий рынок" in why()
    ex.tickers["TON/USDT"].update(quoteVolume=5e6, ask=2.05)
    assert "больше 0.6%" in why()
    ex.tickers["TON/USDT"]["ask"] = 2.002
    ex.ohlcv["TON/USDT"] = candles(now, 1.85)  # уже +8% с момента новости
    assert "опоздали" in why()
    ex.ohlcv["TON/USDT"] = candles(now, 2.1)  # −4.8% после хорошей новости
    assert "не поверил" in why()
    ex.ohlcv["TON/USDT"] = []
    assert "нет свечей" in why()
    ex.ohlcv["TON/USDT"] = candles(now, 1.99)
    plan, reason = run(nt.check(ev(), "TON", now))
    assert plan and not reason and plan["ex"] == "a" and 1.5 <= plan["tp"] <= 8


def test_skip_reasons_are_shown(bot_env):
    b, _ = bot_env
    nt, ex = setup(b, vol=1000)
    assert run(nt.process([ev()])) == []
    assert nt.st["skips"][-1]["coin"] == "TON" and "тонкий рынок" in nt.st["skips"][-1]["why"]
    assert "Почему не купил" in nt.panel_text() and nt.st["found"] == 1 and nt.st["checked"] == 1
    # та же история второй раз не обрабатывается, старые и плохие новости — тоже
    assert run(nt.process([ev(), {**ev(eid="e2"), "old": True}, ev(cat="hack", eid="e3")])) == []
    assert nt.st["checked"] == 1


# --- демо-сделки --------------------------------------------------------------------------------------

def test_demo_open_and_take_profit(bot_env):
    b, _ = bot_env
    nt, ex = setup(b)
    msgs = run(nt.process([ev()]))
    assert len(msgs) == 1 and "ДЕМО" in msgs[0] and "Купил TON" in msgs[0] and not ex.created
    pos = nt.st["open"][0]
    assert not pos["live"] and abs(pos["cost"] - 3.33) < 0.01 and nt.st["cash"] < ntm.DEMO_START
    assert any(r["source"] == "news" and not r["live"] for r in b.trader.state.log)
    set_price(ex, "TON", pos["tp"] * 1.001)
    out = run(nt.manage())
    assert len(out) == 1 and "цель" in out[0] and not nt.st["open"]
    c = nt.st["closed"][-1]
    assert c["pnl"] > 0 and nt.equity() > ntm.DEMO_START
    assert nt.st["cats"]["listing_major"][0] == 1  # экзамен посчитал сделку


def test_demo_stop_breakeven_trailing_time_and_bad_news(bot_env, monkeypatch):
    b, _ = bot_env
    nt, ex = setup(b)
    run(nt.process([ev()]))
    pos = nt.st["open"][0]
    set_price(ex, "TON", pos["entry"] * 0.97)
    assert "стоп" in run(nt.manage())[0]
    set_price(ex, "TON", 2.0)
    # безубыток: +1.6%, потом откат к входу
    run(nt.process([ev(eid="e2")]))
    pos = nt.st["open"][0]
    set_price(ex, "TON", pos["entry"] * 1.016)
    assert run(nt.manage()) == [] and pos["be"] and pos["sl"] > pos["entry"]
    set_price(ex, "TON", pos["entry"])
    assert "безубыток" in run(nt.manage())[0]
    set_price(ex, "TON", 2.0)
    # тянущийся стоп: +3%, потом −1.5% от пика
    run(nt.process([ev(eid="e3")]))
    pos = nt.st["open"][0]
    set_price(ex, "TON", pos["entry"] * 1.03)
    assert run(nt.manage()) == []
    set_price(ex, "TON", pos["entry"] * 1.03 * 0.985)
    assert "защита прибыли" in run(nt.manage())[0]
    set_price(ex, "TON", 2.0)
    # 4 часа прошло
    run(nt.process([ev(eid="e4")]))
    assert "4 ч прошло" in run(nt.manage(time.time() + ntm.HOLD_SEC + 1))[0]
    # плохая новость по монете — продать сразу
    run(nt.process([ev(eid="e5")]))
    monkeypatch.setattr(b.news, "risk", lambda now=None: {"TON": (time.time() + 3600, "взлом")})
    assert "плохая новость" in run(nt.manage())[0]
    assert nt.st["cats"]["listing_major"][0] == 5


def test_limits_open_and_day(bot_env, monkeypatch):
    b, _ = bot_env
    nt, ex = setup(b)
    monkeypatch.setattr(ntm, "MAX_OPEN", 1)
    run(nt.process([ev()]))
    run(nt.process([ev(eid="e2", coins=["TON"])]))
    assert len(nt.st["open"]) == 1 and "уже открыто" in nt.st["skips"][-1]["why"]


def test_exam_and_real_problem(bot_env):
    b, _ = bot_env
    nt, _ = setup(b)
    assert not nt.graduated("listing_major")
    assert "API-ключ" in nt.real_problem()
    b.trader.creds = {"a": {"apiKey": "k", "secret": "s"}}
    assert "/live" in nt.real_problem()
    b.trader.state.live = True
    assert "экзамен" in nt.real_problem()
    nt.st["cats"]["listing_major"] = [8, 6.0, 5]  # 8 сделок, в среднем +0.75%, в плюс 62%
    assert nt.graduated("listing_major") and nt.real_problem() is None
    nt.st["cats"]["listing_major"] = [8, 2.0, 5]  # в среднем +0.25% — мало
    assert not nt.graduated("listing_major")
    nt.st["cats"]["listing_major"] = [8, 6.0, 3]  # в плюс 37% — мало
    assert not nt.graduated("listing_major")


# --- реал --------------------------------------------------------------------------------------------

def live_ready(b, nt):
    b.trader.creds = {"a": {"apiKey": "k", "secret": "s"}}
    b.scanner.exchanges["a"].apiKey, b.scanner.exchanges["a"].secret = "k", "s"
    b.trader.state.live = True
    nt.st["cats"]["listing_major"] = [10, 9.0, 7]
    nt.st["real"] = True


def test_live_only_when_real_live_and_graduated(bot_env):
    b, _ = bot_env
    nt, ex = setup(b)
    live_ready(b, nt)
    nt.st["cats"] = {}
    run(nt.process([ev()]))  # тип не сдал экзамен — демо
    assert not nt.st["open"][0]["live"] and not ex.created
    run(nt.sell_all())
    nt.st["cats"]["listing_major"] = [10, 9.0, 7]
    b.trader.state.live = False  # бот не в /live — демо
    run(nt.process([ev(eid="e2")]))
    assert not nt.st["open"][0]["live"] and not ex.created
    run(nt.sell_all())
    b.trader.state.live = True
    msgs = run(nt.process([ev(eid="e3")]))
    pos = nt.st["open"][0]
    assert pos["live"] and "💵" in msgs[0] and ex.created and ex.created[0][0] == "buy_cost"
    assert pos["cost"] <= b.settings.max_trade_usd and ex.balance.get("TON", 0) > 0
    set_price(ex, "TON", pos["tp"] * 1.001)
    out = run(nt.manage())
    assert "цель" in out[0] and not nt.st["open"] and nt.totals(True)[0] == 1
    assert nt.st["cats"]["listing_major"][0] == 11  # демо e2 посчиталась, реальные сделки экзамен не меняют


def test_two_live_losses_return_to_demo(bot_env):
    b, _ = bot_env
    nt, ex = setup(b)
    b.settings.daily_loss_usd = 100
    live_ready(b, nt)
    for i in range(2):
        run(nt.process([ev(eid=f"l{i}")]))
        pos = nt.st["open"][0]
        assert pos["live"]
        set_price(ex, "TON", pos["entry"] * 0.97)
        out = run(nt.manage())
        set_price(ex, "TON", 2.0)
    assert not nt.st["real"] and "Вернул торговлю по новостям в демо" in out[0]
    assert "подряд в минус" in nt.st["reverted"]
    run(nt.process([ev(eid="after")]))
    assert not nt.st["open"][0]["live"]


def test_stop_returns_to_demo(bot_env):
    b, _ = bot_env
    nt, _ = setup(b)
    live_ready(b, nt)
    run(b.trader.panic())
    assert not nt.st["real"] and "/stop" in nt.st["reverted"]


# --- команды и приложение -----------------------------------------------------------------------------

def test_newstrade_command_and_buttons(bot_env):
    b, session = bot_env
    nt, _ = setup(b)

    async def flow():
        await send(b, "/newstrade")
        text = session.sent[-1][1]
        assert "Торговля по новостям" in text and "демо" in text
        await send(b, "/newstrade real")
        assert "Пока нельзя" in session.sent[-1][1]
        await send(b, "/newstrade off")
        assert not nt.st["on"]
        await send(b, "/newstrade on")
        assert nt.st["on"]
        await send(b, "/newstrade help")
        assert "/newstrade sell" in session.sent[-1][1]
        live_ready(b, nt)
        nt.st["real"] = False
        await send(b, "/newstrade real")
        assert "Включить реальные сделки" in session.sent[-1][1] and not nt.st["real"]
        await press(b, "nx:real2")
        assert nt.st["real"] and "включены" in session.sent[-1][1]
        await press(b, "nx:demo")
        assert not nt.st["real"]
        await send(b, "/newstrade real", user=222)  # не владелец
        assert not nt.st["real"]
        await send(b, "/news")
        assert "nx:panel" in buttons(session.markups[-1])

    run(flow())


def test_demos_row_and_doctor(bot_env):
    b, _ = bot_env
    nt, _ = setup(b)
    from bot import demo_report
    from bot.doctor import _automat_checks
    row = next(r for r in demo_report.rows(b) if r["id"] == "newstrade")
    assert row["on"] and "ждёт" in row["why"]
    run(nt.process([ev()]))
    row = next(r for r in demo_report.rows(b) if r["id"] == "newstrade")
    assert "в сделке: TON" in row["why"]
    checks = [c for c in _automat_checks(b, time.time()) if "Торговля по новостям" in c.title]
    assert checks and "демо-счёт" in checks[0].text
    nt.st["on"] = False
    assert any("торговля по новостям" in x for x in demo_report.enable_all(b)) and nt.st["on"]
    assert not nt.st["real"]


def test_mini_app_newstrade(app_env):
    from tests.test_webapp import FRIEND, call, get, post
    b, _ = app_env
    nt = b.newstrade

    async def flow(client):
        _, d = await get(client, "/api/news")
        assert d["trade"]["on"] is True and d["trade"]["start"] == ntm.DEMO_START
        st, _ = await post(client, "/api/news", {"trade_on": False}, uid=FRIEND)
        assert st == 403 and nt.st["on"]
        _, d = await post(client, "/api/news", {"trade_on": False})
        assert d["trade"]["on"] is False
        st, d = await post(client, "/api/news", {"trade_real": True, "confirm": True})
        assert st == 400 and "/live" in d["error"] and not nt.st["real"]
        live_ready(b, nt)
        nt.st["real"] = False
        b.trader.creds = {"a": {"apiKey": "k", "secret": "s"}}
        st, _ = await post(client, "/api/news", {"trade_real": True})
        assert st == 400 and not nt.st["real"]  # без подтверждения
        _, d = await post(client, "/api/news", {"trade_real": True, "confirm": True})
        assert d["trade"]["real"] is True
        _, d = await post(client, "/api/news", {"trade_real": False})
        assert d["trade"]["real"] is False

    call(b, flow)


def test_ai_context_and_owner(bot_env):
    b, _ = bot_env
    ctx = b.ai_context()["extras"]
    news_ctx = next((v for v in ctx.values() if isinstance(v, dict) and "news_trading" in v), None)
    assert news_ctx and news_ctx["news_trading"]["mode"]
    assert b.owner_id == OWNER
