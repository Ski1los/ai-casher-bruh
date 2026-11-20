"""BingX, ключи (срок/права), ценовые алерты, кошелёк Phantom, /doctor, /price и их API в Mini App."""

import asyncio
import copy
import json
import time

import ccxt.async_support as ccxt
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import extras_ui
from bot.alerts import AlertBook, AlertError, build_alert, parse_alert
from bot.doctor import doctor_text, run_doctor
from bot.scanner import make_exchange
from bot.solana import KNOWN_MINTS, PhantomWatcher, WalletError, fetch_wallet, valid_address
from bot.trading import (Trader, days_left, human_error, is_geo_block, parse_bingx_key, parse_bybit_key)
from bot.webapp import WebApp
from tests.fake_exchange import tri_exchange
from tests.test_telegram_flow import OWNER, STRANGER, send
from tests.test_webapp import TOKEN, auth

KEYS = {"A_API_KEY": "key", "A_API_SECRET": "secret"}
ADDRESS = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
SEED = "abandon ability able about above absent absorb abstract absurd abuse access accident"


# ---------------------------------------------------------------------------
# BingX и запасной домен
# ---------------------------------------------------------------------------


def test_bingx_is_spot_only_and_needs_no_passphrase():
    async def run():
        ex = make_exchange("bingx")
        try:
            calls = []

            async def fake_fetch(url, method="GET", headers=None, body=None):
                calls.append(url)
                return {"code": 0, "data": {"symbols": []}}

            ex.fetch = fake_fetch
            await ex.load_markets()
            assert calls and all("/spot/" in u for u in calls)  # фьючерсы BingX не грузим
            assert not ex.requiredCredentials.get("password")  # passphrase не нужен
            assert ex.name == "BingX"
        finally:
            await ex.close()

    asyncio.run(run())


def test_hostname_override(monkeypatch):
    monkeypatch.setenv("BYBIT_HOSTNAME", "bytick.com")

    async def run():
        ex = make_exchange("bybit")
        try:
            assert ex.hostname == "bytick.com"
            assert "bytick.com" in ex.implode_hostname(ex.urls["api"]["public"])
        finally:
            await ex.close()

    asyncio.run(run())


def test_bingx_aliases_and_default_exchanges():
    from bot.config import DEFAULT_EXCHANGES
    from tests.test_telegram_flow import exchanges

    class S:
        pass

    s = S()
    s.exchanges = {**exchanges(), "bingx": tri_exchange()}
    s.display_name = lambda n: n
    t = Trader(s, None, env={})
    for word in ("бингх", "BingX", "бингикс"):
        assert t.resolve_exchange(word) == "bingx"
    assert DEFAULT_EXCHANGES.split(",")[:2] == ["bybit", "bingx"]


def test_geo_block_is_recognized():
    e = ccxt.ExchangeNotAvailable('bybit GET https://api.bybit.com 403 Forbidden <H1>403 ERROR</H1> The Amazon '
                                  'CloudFront distribution is configured to block access from your country.')
    assert is_geo_block(e)
    assert "из его страны" in human_error(e)
    assert not is_geo_block(ccxt.NetworkError("timeout"))


# ---------------------------------------------------------------------------
# Права и срок действия ключей
# ---------------------------------------------------------------------------


def test_parse_bybit_key_expiry_and_permissions():
    now = 1_700_000_000
    info = parse_bybit_key({"readOnly": 0, "ips": ["*"], "deadlineDay": 20,
                            "permissions": {"Spot": ["SpotTrade"], "Wallet": ["AccountTransfer"]}}, now=now)
    assert info["spot_trade"] and not info["withdraw"] and not info["ip_bound"]
    assert days_left(info, now) == pytest.approx(20)

    info = parse_bybit_key({"readOnly": 1, "ips": ["1.2.3.4"], "expiredAt": "2023-12-01T00:00:00Z",
                            "permissions": {"Wallet": ["AccountTransfer", "Withdraw"]}})
    assert info["withdraw"] and info["read_only"] and info["ip_bound"]
    assert info["expires_at"] == pytest.approx(1701388800)


def test_parse_bingx_key_finds_withdraw():
    # формат ответа /openApi/v1/account/apiPermissions (как в официальном SDK bingx-py)
    info = parse_bingx_key({"apiKey": "k", "permissions": [1, 2], "ipAddresses": [], "note": ""})
    assert info["spot_trade"] and info["read"] and not info.get("withdraw") and info["inactive_rule"]
    info = parse_bingx_key({"permissions": ["2", 5], "ipAddresses": ["1.2.3.4"]})
    assert info["withdraw"] and not info.get("spot_trade") and info["ip_bound"] and not info["inactive_rule"]
    assert parse_bingx_key(None) == {"ip_bound": False, "inactive_rule": True}


def test_inspect_keys_and_warnings():
    async def run():
        class Sc:
            def __init__(self):
                self.exchanges = {"bybit": tri_exchange(), "bingx": tri_exchange()}

            def display_name(self, n):
                return {"bybit": "Bybit", "bingx": "BingX"}[n]

        sc = Sc()

        async def bybit_info(params=None):
            return {"result": {"ips": [], "deadlineDay": 5, "permissions": {"Spot": ["SpotTrade"]}}}

        async def bingx_info(params=None):
            return {"code": 0, "data": {"permissions": [1, 2, 5], "ipAddresses": []}}

        sc.exchanges["bybit"].privateGetV5UserQueryApi = bybit_info
        sc.exchanges["bingx"].accountV1PrivateGetAccountApiPermissions = bingx_info
        env = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "BINGX_API_KEY": "k", "BINGX_API_SECRET": "s"}
        t = Trader(sc, None, env=env)
        info = await t.inspect_keys()
        assert days_left(info["bybit"]) == pytest.approx(5, abs=0.01)
        warns = t.key_warnings()
        assert any("Bybit" in w and "через 5 дн" in w for w in warns)
        assert any("BingX" in w and "вывода" in w for w in warns)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Ценовые алерты
# ---------------------------------------------------------------------------


def test_parse_alert_variants():
    s = parse_alert("ton < 5")
    assert (s.coin, s.op, s.price, s.pct) == ("TON", "<", 5, None)
    s = parse_alert("BTC выше 100000 bybit", ["bybit"])
    assert (s.op, s.price, s.exchange) == (">", 100000, "bybit")
    assert parse_alert("SOL +5%").pct == 5
    assert parse_alert("SOL -3%").pct == -3
    assert parse_alert("eth ниже 3%").pct == -3
    assert parse_alert("TON 4,5").price == 4.5
    for bad in ("", "TON", "TON 5%", "TON abc", "TON -5", "@@ 5"):
        with pytest.raises(AlertError):
            parse_alert(bad)


def test_build_alert_direction_and_validation():
    a = build_alert(parse_alert("TON 5"), current=6, exchange="bybit", chat_id=1)
    assert a.op == "<" and a.hit(5) and not a.hit(5.5)
    a = build_alert(parse_alert("TON +10%"), current=6, exchange="bybit", chat_id=1)
    assert a.op == ">" and a.price == pytest.approx(6.6)
    with pytest.raises(AlertError):
        build_alert(parse_alert("TON < 7"), current=6, exchange="bybit", chat_id=1)  # уже ниже


def test_alert_book_check_export_restore():
    book = AlertBook()
    book.add(build_alert(parse_alert("TON < 5"), 6, "a", 1))
    book.add(build_alert(parse_alert("BTC > 60000"), 50000, "a", 1))
    prices = {("a", "TON"): 4.9, ("a", "BTC"): 55000}

    async def price_of(ex, coin):
        return prices[(ex, coin)]

    fired = asyncio.run(book.check(price_of))
    assert [a.coin for a, _ in fired] == ["TON"] and len(book.items) == 1
    clone = AlertBook()
    clone.restore(json.loads(json.dumps(book.export())))
    assert clone.items[0].coin == "BTC" and clone.items[0].op == ">"
    assert clone.remove(clone.items[0].id, chat_id=2) is None  # чужой чат не может удалить
    assert clone.remove(clone.items[0].id, chat_id=1)


# ---------------------------------------------------------------------------
# Phantom (Solana) — фейковый RPC-узел
# ---------------------------------------------------------------------------


def rpc_app(calls, fail=False):
    async def handler(request):
        body = await request.json()
        calls.append(body["method"])
        if fail:
            return web.json_response({"error": {"message": "Too many requests"}}, status=429)
        if body["method"] == "getBalance":
            return web.json_response({"result": {"value": 1_500_000_000}})
        program = body["params"][1]["programId"]
        if program.startswith("Tokenz"):
            return web.json_response({"result": {"value": []}})

        def acc(mint, amount):
            return {"account": {"data": {"parsed": {"info": {"mint": mint, "tokenAmount": {"uiAmountString": amount}}}}}}

        usdc = next(m for m, c in KNOWN_MINTS.items() if c == "USDC")
        return web.json_response({"result": {"value": [acc(usdc, "12.5"), acc("Unknown1111111111111111111111111111", "7"),
                                                       acc(usdc, "0.5"), acc("Zero111111111111111111111111111111", "0")]}})

    app = web.Application()
    app.router.add_post("/", handler)
    return app


def test_phantom_reads_balances_and_prices():
    async def run():
        calls = []
        server = TestServer(rpc_app(calls))
        await server.start_server()
        try:
            w = PhantomWatcher(ADDRESS, rpc_url=str(server.make_url("/")))

            async def prices(coins):
                assert "SOL" in coins and "USDC" in coins
                return {"SOL": 200.0, "USDC": 1.0}

            snap = await w.get(prices)
            coins = {a["coin"]: a for a in snap["assets"]}
            assert coins["SOL"]["amount"] == 1.5 and coins["SOL"]["usd"] == 300
            assert coins["USDC"]["amount"] == 13.0  # два счёта одного токена сложены
            assert len(coins) == 3  # нулевой токен скрыт, неизвестный показан коротким адресом
            assert snap["total_usd"] == 313
            n = len(calls)
            await w.get(prices)  # кэш: повторный запрос не идёт в сеть
            assert len(calls) == n
            assert "313" in w.text(snap)
            await w.close()
        finally:
            await server.close()

    asyncio.run(run())


def test_phantom_errors_are_soft():
    async def run():
        server = TestServer(rpc_app([], fail=True))
        await server.start_server()
        try:
            w = PhantomWatcher(ADDRESS, rpc_url=str(server.make_url("/")))

            async def prices(coins):
                return {}

            snap = await w.get(prices)
            assert "подождать" in snap["error"] and snap["assets"] == []
            await w.close()
        finally:
            await server.close()

    asyncio.run(run())


def test_phantom_address_validation():
    assert valid_address(ADDRESS)
    assert not valid_address("0x52908400098527886E0F7030069857D2E4169EE7")  # адрес Ethereum
    assert not valid_address(SEED)
    w = PhantomWatcher()
    with pytest.raises(WalletError):
        w.set_address("hello")
    assert asyncio.run(w.get(None)) is None  # без адреса ничего не запрашиваем


def test_fetch_wallet_raises_when_all_token_calls_fail():
    class Boom:
        def post(self, *a, **kw):
            raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        asyncio.run(fetch_wallet(ADDRESS, "http://x", Boom()))


# ---------------------------------------------------------------------------
# Telegram: /alert, /phantom, /price, /doctor
# ---------------------------------------------------------------------------


@pytest.fixture
def ex_env(bot_env):
    b, session = bot_env
    ex = tri_exchange()
    ex.markets = copy.deepcopy(ex.markets)
    ex.funding = {"USDT": 7.0}
    ex.options = {"accountsByType": {"funding": "fund", "spot": "spot"}}
    b.scanner.exchanges = {"a": ex}
    b.trader = Trader(b.scanner, b.settings, env=KEYS)
    return b, session


def test_alert_command_and_firing(ex_env):
    b, session = ex_env

    async def flow():
        await send(b, "/alert BTC > 60000")
        assert "напишу" in session.sent[-1][1] and len(b.alerts.items) == 1
        assert len(json.loads(b.settings_file.read_text())["alerts"]) == 1
        await send(b, "/alert BTC < 70000")
        assert "уже стоит" in session.sent[-1][1]
        await send(b, "/alert NOPE < 1")
        assert "не нашёл" in session.sent[-1][1]
        await send(b, "/alerts")
        assert "BTC ≥ $60 000" in session.sent[-1][1] and "al:del:" in str(session.markups[-1])

        assert await extras_ui.check_alerts_once(b) == 0  # цена ещё не дошла
        b.scanner.exchanges["a"].tickers["BTC/USDT"] = {"bid": 61_000, "ask": 61_001}
        assert await extras_ui.check_alerts_once(b) == 1
        assert session.sent[-1][0] == OWNER and "🔔" in session.sent[-1][1] and "$61 000.50" in session.sent[-1][1]

    asyncio.run(flow())
    assert b.alerts.items == []
    assert json.loads(b.settings_file.read_text())["alerts"] == []


def test_phantom_command_rejects_seed_and_saves_address(ex_env, monkeypatch):
    b, session = ex_env

    async def fake_get(price_fn, force=False, mint_price_fn=None):
        return {"address": b.phantom.address, "assets": [{"coin": "SOL", "amount": 1, "usd": 200}],
                "updated_at": time.time(), "total_usd": 200}

    monkeypatch.setattr(b.phantom, "get", fake_get)

    async def flow():
        await send(b, f"/phantom {SEED}")
        assert "сид-фраза" in session.sent[-1][1].lower() and b.phantom.address == ""
        assert "сид" not in b.settings_file.read_text() if b.settings_file.exists() else True
        await send(b, "/phantom 0xabc")
        assert "не похоже" in session.sent[-1][1]
        b.allowed_ids.add(STRANGER)
        await send(b, f"/phantom {ADDRESS}", user=STRANGER)
        assert "только владелец" in session.sent[-1][1] and b.phantom.address == ""
        await send(b, f"/phantom {ADDRESS}")
        assert b.phantom.address == ADDRESS and "$200.00" in session.sent[-1][1]
        await send(b, "/phantom off")
        assert b.phantom.address == ""

    asyncio.run(flow())
    assert json.loads(b.settings_file.read_text())["phantom"] == ""
    assert SEED.split()[0] not in b.settings_file.read_text()


def test_phantom_address_survives_restart(ex_env):
    b, _ = ex_env
    b.phantom.set_address(ADDRESS)
    b.alerts.add(build_alert(parse_alert("TON < 5"), 6, "a", OWNER))
    b._save_settings()
    import bot.telegram_bot as tb
    b2 = tb.ArbitrageBot(TOKEN, b.settings, [OWNER])
    assert b2.phantom.address == ADDRESS and b2.alerts.items[0].coin == "TON"


def test_price_command(ex_env):
    b, session = ex_env
    other = tri_exchange()
    other.markets = copy.deepcopy(other.markets)
    other.tickers = copy.deepcopy(other.tickers)
    other.tickers["BTC/USDT"] = {"bid": 51_000, "ask": 51_001, "percentage": 1.5}
    b.scanner.exchanges["b"] = other

    asyncio.run(send(b, "/price btc"))
    text = session.sent[-1][1]
    assert "BTC/USDT" in text and "+1.50%" in text and "до комиссий" in text


def test_doctor_reports_everything(ex_env):
    b, session = ex_env
    asyncio.run(send(b, "/doctor"))
    text = session.sent[-1][1]
    for part in ("Проверка всего", "Биржи", "ExA", "ключ работает", "деньги на другом счёте", "«Funding» ≈ $7.00", "Phantom", "Итог"):
        assert part in text, part
    assert len(text) < 4096


def test_doctor_marks_bad_key_and_geo_block(ex_env):
    b, _ = ex_env
    b.scanner.exchanges["a"].auth_error = True

    async def blocked(symbol):
        raise ccxt.ExchangeNotAvailable("403 Forbidden CloudFront block access from your country")

    b.scanner.exchanges["a"].fetch_ticker = blocked
    checks = asyncio.run(run_doctor(b))
    bad = [c for c in checks if c.status == "bad"]
    assert any("страны" in c.text for c in bad) and any(c.group == "Ключи" for c in bad)
    assert "❌" in doctor_text(checks)


# ---------------------------------------------------------------------------
# API Mini App
# ---------------------------------------------------------------------------


def test_webapp_extras_api(ex_env, monkeypatch):
    b, _ = ex_env
    monkeypatch.delenv("WEBAPP_DEV_USER", raising=False)
    friend = 222
    b.allowed_ids.add(friend)
    b.webapp = WebApp(b, TOKEN)

    async def fake_get(price_fn, force=False, mint_price_fn=None):
        return {"address": b.phantom.address, "assets": [], "updated_at": time.time(), "total_usd": 0}

    monkeypatch.setattr(b.phantom, "get", fake_get)

    async def main():
        app = web.Application()
        b.webapp.setup(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            async def get(path, uid=OWNER):
                r = await client.get(path, headers=auth(uid))
                return r.status, await r.json()

            async def post(path, body, uid=OWNER):
                r = await client.post(path, json=body, headers=auth(uid))
                return r.status, await r.json()

            st, r = await get("/api/phantom")
            assert st == 200 and r == {"address": ""}
            st, r = await post("/api/phantom", {"address": ADDRESS}, uid=friend)
            assert st == 403
            st, r = await post("/api/phantom", {"address": SEED})
            assert st == 400 and "сид-фразу" in r["error"]
            st, r = await post("/api/phantom", {"address": ADDRESS})
            assert st == 200 and r["address"] == ADDRESS

            st, r = await post("/api/alerts", {"text": "BTC > 60000"}, uid=friend)
            assert st == 200 and len(r["alerts"]) == 1
            st, r = await get("/api/alerts")  # у владельца свои алерты
            assert r["alerts"] == []
            st, r = await post("/api/alerts", {"text": "BTC 10%"})
            assert st == 400
            fid = b.alerts.items[0].id
            st, r = await post("/api/alerts/delete", {"id": fid})  # чужой алерт не удалить
            assert len(b.alerts.items) == 1
            st, r = await post("/api/alerts/delete", {"id": fid}, uid=friend)
            assert r["alerts"] == []

            st, r = await get("/api/price?coin=btc")
            assert st == 200 and r["rows"][0]["bid"] == 50_000
            st, r = await get("/api/doctor")
            assert st == 200 and r["summary"]["ok"] >= 1 and any(c["group"] == "Ключи" for c in r["checks"])
            st, _ = await get("/api/doctor", uid=STRANGER)
            assert st == 403
        finally:
            await client.close()

    asyncio.run(main())


def test_phantom_falls_back_to_second_solana_node(monkeypatch):
    import bot.solana as sol

    calls = []

    async def fake_fetch(address, url, session):
        calls.append(url)
        if url == "slow":
            raise asyncio.TimeoutError()
        return {"SOL": 2.0}

    async def prices(coins):
        return {"SOL": 100.0}

    async def flow():
        monkeypatch.setattr(sol, "fetch_wallet", fake_fetch)
        w = PhantomWatcher(ADDRESS)
        w.rpc_urls = ["slow", "fast"]
        snap = await w.get(prices)
        await w.close()
        return w, snap

    w, snap = asyncio.run(flow())
    assert snap["total_usd"] == 200.0 and "error" not in snap
    assert calls == ["slow", "fast"] and w.rpc_urls[0] == "fast"
