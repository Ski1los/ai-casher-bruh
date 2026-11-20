"""Mini App: проверка подписи Telegram, права доступа и все действия API на фейковых биржах."""

import asyncio
import copy
import json
import time
import urllib.parse

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot.ai import AIAssistant, Endpoint, LLMClient
from bot.trading import HISTORY_EVERY_SEC, HISTORY_KEEP, Trader
from bot.webapp import WebApp, check_init_data, downsample, sign_init_data
from tests.fake_exchange import tri_exchange
from tests.test_ai import FakeLLM, serve
from tests.test_telegram_flow import OWNER, STRANGER

TOKEN = "123456:" + "A" * 35
FRIEND = 222  # есть в ALLOWED_USER_IDS, но не владелец
KEYS = {"A_API_KEY": "key", "A_API_SECRET": "secret"}


def auth(uid, token=TOKEN, **kw):
    return {"X-Telegram-Init-Data": sign_init_data({"id": uid, "first_name": "Тест"}, token, **kw)}


async def fake_ohlcv(symbol, timeframe="15m", limit=120):
    now = int(time.time() * 1000)
    return [[now - (limit - i) * 900_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0] for i in range(limit)]


@pytest.fixture
def app_env(bot_env, monkeypatch):
    monkeypatch.delenv("WEBAPP_DEV_USER", raising=False)
    b, session = bot_env
    ex = tri_exchange()
    ex.markets = copy.deepcopy(ex.markets)
    ex.fetch_ohlcv = fake_ohlcv
    b.scanner.exchanges["a"] = ex
    b.trader = Trader(b.scanner, b.settings, env=KEYS)
    b.allowed_ids.add(FRIEND)
    b.webapp = WebApp(b, TOKEN)
    return b, session


def call(b, fn):
    """Поднимает настоящий aiohttp-сервер с Mini App и выполняет fn(client)."""

    async def main():
        app = web.Application()
        b.webapp.setup(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await fn(client)
        finally:
            await asyncio.sleep(0)  # дать отработать фоновым сохранениям
            await client.close()

    return asyncio.run(main())


async def get(client, path, uid=OWNER):
    r = await client.get(path, headers=auth(uid))
    return r.status, await r.json()


async def post(client, path, body, uid=OWNER):
    r = await client.post(path, json=body, headers=auth(uid))
    return r.status, await r.json()


# ---------------------------------------------------------------------------
# Подпись initData
# ---------------------------------------------------------------------------


def test_init_data_signature_and_expiry():
    init = sign_init_data({"id": 5, "first_name": "A"}, TOKEN)
    assert check_init_data(init, TOKEN)["id"] == 5
    assert check_init_data(init, "999:" + "B" * 35) is None  # чужой бот
    # подделка любого поля ломает подпись
    pairs = dict(urllib.parse.parse_qsl(init))
    pairs["user"] = json.dumps({"id": OWNER})
    assert check_init_data(urllib.parse.urlencode(pairs), TOKEN) is None
    old = sign_init_data({"id": 5}, TOKEN, auth_date=int(time.time()) - 2 * 86400)
    assert check_init_data(old, TOKEN) is None  # просрочено
    for bad in ("", "hash=abc", "user=%7B%7D", "a=b&hash="):
        assert check_init_data(bad, TOKEN) is None


def test_downsample_keeps_ends():
    pts = [[i, i] for i in range(1000)]
    out = downsample(pts, 100)
    assert len(out) <= 101 and out[0] == [0, 0] and out[-1] == [999, 999]
    assert downsample(pts[:5], 100) == pts[:5]


def test_record_equity_history():
    import types

    nothing = types.SimpleNamespace(exchanges={})
    t = Trader(nothing, None, env={})
    t.record_equity(10, now=0)
    t.record_equity(11, now=60)
    t.record_equity(12, now=120)  # чаще 15 минут: последняя точка обновляется, а не копится
    assert t.state.history == [[0, 10], [120, 12]]
    t.record_equity(13, now=120 + HISTORY_EVERY_SEC)
    assert len(t.state.history) == 3
    for i in range(HISTORY_KEEP + 50):
        t.record_equity(1, now=10_000 + i * HISTORY_EVERY_SEC)
    assert len(t.state.history) == HISTORY_KEEP
    # история переживает перезапуск вместе с остальным состоянием
    t2 = Trader(nothing, None, env={})
    t2.restore(json.loads(json.dumps(t.export())))
    assert t2.state.history == t.state.history


# ---------------------------------------------------------------------------
# Статика и доступ
# ---------------------------------------------------------------------------


def test_static_is_versioned_and_gzipped(app_env):
    b, _ = app_env

    async def flow(c):
        r = await c.get("/app")
        html = await r.text()
        assert r.status == 200 and "no-cache" in r.headers["Cache-Control"]
        assert "{{V}}" not in html and f"app.js?v={b.webapp._version}" in html
        r = await c.get("/app/s/app.js", headers={"Accept-Encoding": "gzip"})
        assert r.status == 200 and "immutable" in r.headers["Cache-Control"]
        assert r.headers.get("Content-Encoding") == "gzip"
        assert "Telegram" in await r.text()  # клиент сам распаковал
        for bad in ("/app/s/nope.js", "/app/s/..%2Ftelegram_bot.py", "/app/s/%2e%2e%2fmain.py"):
            assert (await c.get(bad)).status == 404

    call(b, flow)


def test_api_requires_telegram_signature(app_env):
    b, _ = app_env

    async def flow(c):
        r = await c.get("/api/overview")
        assert r.status == 401
        r = await c.get("/api/overview", headers=auth(OWNER, token="999:" + "B" * 35))
        assert r.status == 401
        status, data = await get(c, "/api/overview", uid=STRANGER)
        assert status == 403 and "ALLOWED_USER_IDS" in data["error"]
        status, data = await get(c, "/api/overview")
        assert status == 200 and data["user"]["owner"] is True and data["preview"] is False
        assert data["trading"]["live"] is False and data["exchanges"]

    call(b, flow)


def test_dev_user_only_without_init_data(app_env, monkeypatch):
    b, _ = app_env
    monkeypatch.setenv("WEBAPP_DEV_USER", str(OWNER))
    b.webapp = WebApp(b, TOKEN)

    async def flow(c):
        r = await c.get("/api/overview")
        assert r.status == 200 and (await r.json())["preview"] is True
        # поддельная подпись не превращается в dev-доступ
        r = await c.get("/api/overview", headers={"X-Telegram-Init-Data": "hash=abc"})
        assert r.status == 401

    call(b, flow)


def test_only_owner_can_trade(app_env):
    b, _ = app_env

    async def flow(c):
        status, data = await get(c, "/api/overview", uid=FRIEND)
        assert status == 200 and data["user"]["owner"] is False
        body = {"exchange": "a", "side": "buy", "coin": "BTC", "amount": "5"}
        for path, payload in [("/api/plan", body), ("/api/execute", {"id": "x"}), ("/api/mode", {"live": False}),
                              ("/api/auto", {"on": True}), ("/api/stop", {}), ("/api/triangle", {"key": "x"}),
                              ("/api/cancel", {"exchange": "a", "id": "1", "symbol": "BTC/USDT"}),
                              ("/api/set", {"key": "max_trade", "value": 500})]:
            status, data = await post(c, path, payload, uid=FRIEND)
            assert status == 403, path
        assert b.settings.max_trade_usd != 500
        status, _ = await post(c, "/api/set", {"key": "min_profit", "value": 0.7}, uid=FRIEND)
        assert status == 200 and b.settings.min_profit_percent == 0.7  # обычные настройки можно

    call(b, flow)


# ---------------------------------------------------------------------------
# Торговля
# ---------------------------------------------------------------------------


def test_trade_flow_test_then_live(app_env):
    b, session = app_env
    ex = b.scanner.exchanges["a"]

    async def flow(c):
        status, data = await post(c, "/api/plan", {"exchange": "a", "side": "buy", "coin": "btc", "amount": "5"})
        assert status == 200
        plan = data["plan"]
        assert plan["live"] is False and plan["spent"] == 5 and plan["got_ccy"] == "BTC"
        status, data = await post(c, "/api/execute", {"id": plan["id"]})
        assert status == 200 and data["live"] is False
        assert ex.orders == {}  # тестовый режим ничего не отправил
        status, data = await post(c, "/api/execute", {"id": plan["id"]})
        assert status == 400  # повторное нажатие не исполняет второй раз

        # реальный режим только с явным подтверждением
        status, data = await post(c, "/api/mode", {"live": True})
        assert status == 400 and b.trader.state.live is False
        await post(c, "/api/plan", {"exchange": "a", "side": "buy", "coin": "BTC", "amount": "5"})
        assert b.trader.pending
        status, data = await post(c, "/api/mode", {"live": True, "confirm": "LIVE"})
        assert status == 200 and data["live"] is True
        assert not b.trader.pending  # тестовые предпросмотры сброшены

        status, data = await post(c, "/api/plan", {"exchange": "a", "side": "buy", "coin": "BTC", "amount": "5"})
        assert data["plan"]["live"] is True
        status, data = await post(c, "/api/execute", {"id": data["plan"]["id"]})
        assert status == 200 and data["record"]["live"] is True
        assert len(ex.orders) == 1 and ex.balance["BTC"] > 0

        status, data = await post(c, "/api/plan", {"exchange": "a", "side": "buy", "coin": "BTC", "amount": "5",
                                                   "type": "limit", "price": "40000"})
        status, data = await post(c, "/api/execute", {"id": data["plan"]["id"]})
        assert data["record"]["kind"] == "limit"
        status, data = await get(c, "/api/orders")
        orders = data["exchanges"][0]["orders"]
        assert len(orders) == 1 and orders[0]["price"] == 40000
        status, _ = await post(c, "/api/cancel", {"exchange": "a", "id": orders[0]["id"], "symbol": "BTC/USDT"})
        assert status == 200
        assert (await get(c, "/api/orders"))[1]["exchanges"][0]["orders"] == []

        status, data = await post(c, "/api/auto", {"on": True})
        assert data["auto"] is True
        status, data = await post(c, "/api/stop", {})
        assert status == 200 and "Стоп" in data["text"] and "<b>" not in data["text"]
        assert b.trader.state.live is False and b.trader.state.auto is False

        status, data = await get(c, "/api/overview")
        log = data["trading"]["log"]
        assert any(r["live"] for r in log) and data["trading"]["today"]["trades"] >= 1
        await asyncio.sleep(0.05)

    call(b, flow)
    assert any("Реальный режим включён" in t for _, t in session.sent)  # владелец получил уведомление в чат


def test_plan_errors_are_readable(app_env):
    b, _ = app_env

    async def flow(c):
        cases = [
            ({"exchange": "a", "side": "hold", "coin": "BTC", "amount": "5"}, "buy"),
            ({"exchange": "zzz", "side": "buy", "coin": "BTC", "amount": "5"}, ""),
            ({"exchange": "a", "side": "buy", "coin": "BTC", "amount": "5000"}, "$"),
            ({"exchange": "a", "side": "buy", "coin": "BTC", "amount": "5", "type": "limit", "price": "abc"}, "числом"),
        ]
        for body, word in cases:
            status, data = await post(c, "/api/plan", body)
            assert status == 400 and data["error"] and word in data["error"], body
        r = await c.post("/api/plan", data="not json", headers=auth(OWNER))
        assert r.status == 400

    call(b, flow)


def test_portfolio_market_symbols(app_env):
    b, _ = app_env
    b.scanner.exchanges["a"].balance = {"USDT": 40.0, "BTC": 0.0002}

    async def flow(c):
        status, data = await get(c, "/api/portfolio")
        assert status == 200 and data["total_usd"] == pytest.approx(50, rel=0.01)
        assert data["history"] and data["history"][-1][1] == pytest.approx(50, rel=0.01)
        status, data = await get(c, "/api/market?ex=a&symbol=btc/usdt&tf=1h")
        assert status == 200 and data["symbol"] == "BTC/USDT" and len(data["candles"]) == 120
        assert data["fee_pct"] == pytest.approx(0.1)
        status, data = await get(c, "/api/market?ex=a&symbol=NOPE/USDT")
        assert status == 400 and "NOPE/USDT" in data["error"]
        status, data = await get(c, "/api/symbols?ex=a")
        names = [r[0] for r in data["symbols"]]
        assert "BTC/USDT" in names and "PEPE/BTC" not in names  # только пары к USDT

    call(b, flow)


def test_scan_and_triangle_from_app(app_env):
    b, _ = app_env

    async def flow(c):
        status, data = await post(c, "/api/scan", {})
        assert status == 200
        tri = next(s for s in data["scan"]["tri"] if s["exchange"] == "a")
        assert tri["executable"] is True and tri["path"] == ["USDT", "PEPE", "BTC", "USDT"]
        status, data = await post(c, "/api/triangle", {"key": tri["key"]})
        assert status == 200 and data["executed"] is True and data["live"] is False and data["pnl"] > 0
        status, data = await post(c, "/api/triangle", {"key": "нет|такого"})
        assert status == 400 and "устарел" in data["error"]

    call(b, flow)


# ---------------------------------------------------------------------------
# Демо, настройки, ИИ
# ---------------------------------------------------------------------------


def test_demo_from_app(app_env):
    b, _ = app_env

    async def flow(c):
        status, data = await post(c, "/api/demo/start", {"amounts": [5, 50]})
        assert status == 200 and data["active"] and len(data["accounts"]) == 4
        assert all(a["curve"] for a in data["accounts"])
        result = await b._scan()
        await b._run_demo(result)
        await b._run_demo(await b._scan())
        status, data = await get(c, "/api/demo")
        assert data["history"] and any(a["trades"] for a in data["accounts"])
        tri = next(a for a in data["accounts"] if a["id"] == "tri-50")
        assert len(tri["curve"]) >= 2 and tri["curve"][-1][1] == pytest.approx(tri["equity"], rel=1e-6)
        status, data = await post(c, "/api/demo/start", {"amounts": [0.5]})
        assert status == 400
        status, data = await post(c, "/api/demo/start", {"amounts": [1, 2, 3, 4, 5, 6]})
        assert status == 400
        status, data = await post(c, "/api/demo/stop", {})
        assert data["active"] is False

    call(b, flow)


def test_settings_validation(app_env):
    b, _ = app_env

    async def flow(c):
        status, data = await post(c, "/api/set", {"key": "min_profit", "value": 999})
        assert status == 400 and "от" in data["error"]
        status, data = await post(c, "/api/set", {"key": "hack", "value": 1})
        assert status == 400
        status, data = await post(c, "/api/set", {"key": "max_trade", "value": 25})
        assert status == 200 and data["settings"]["max_trade"]["value"] == 25 and data["settings"]["max_trade"]["risk"]

    call(b, flow)


def test_ai_chat_with_order_preview(app_env):
    b, _ = app_env

    def reply(body):
        return "Комиссии съедают прибыль.\nSET tri_min=0.4 — запас\nORDER buy a BTC 5 — попробовать"

    async def flow(c):
        status, data = await post(c, "/api/ai", {"q": "что делать?"})
        assert status == 400 and "AI_API_KEY" in data["error"]
        fake = FakeLLM(reply=reply)
        runner, base = await serve(fake)
        b.ai = AIAssistant(LLMClient([Endpoint("groq", base, "gsk_ok")]))
        try:
            status, data = await post(c, "/api/ai", {"q": "что делать?"})
            assert status == 200 and "Комиссии" in data["html"]
            assert data["proposals"][0]["key"] == "tri_min" and data["proposals"][0]["value"] == 0.4
            assert data["plans"][0]["source"] == "ai" and data["plans"][0]["live"] is False
            status, data = await post(c, "/api/ai", {"q": "что делать?"}, uid=FRIEND)
            assert data["plans"] == [] and "владелец" in data["errors"][0]
        finally:
            await runner.cleanup()

    call(b, flow)
