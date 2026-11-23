"""GMGN OpenAPI: подпись запроса, разбор трендов/окопов/сделок/коллов/безопасности, лимиты 429 и тарифные 403."""

import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot.gmgn import TEST_KEY, GmgnClient, GmgnError, norm_security, norm_token

MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
MINT2 = "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY"

RANK = {"address": MINT, "symbol": "POPCAT", "price": 0.5, "market_cap": 500000, "liquidity": 80000, "volume": 120000,
        "holder_count": 1500, "top_10_holder_rate": 0.18, "dev_team_hold_rate": 0, "rug_ratio": 0.05,
        "bundler_rate": 0.02, "rat_trader_amount_rate": 0.01, "top70_sniper_hold_rate": 0.04, "smart_degen_count": 4,
        "renowned_count": 1, "creator_token_status": "creator_close", "is_wash_trading": False, "is_honeypot": 0,
        "price_change_percent5m": 3.2, "price_change_percent1h": 12, "buys": 300, "sells": 200,
        "creation_timestamp": 1_700_000_000_000}


class FakeGmgn:
    def __init__(self):
        self.seen: list = []
        self.mode = "ok"

    def app(self):
        app = web.Application()
        app.router.add_get("/v1/market/rank", self.rank)
        app.router.add_post("/v1/trenches", self.trenches)
        app.router.add_get("/v1/user/smartmoney", self.trades)
        app.router.add_get("/v1/user/kol", self.trades)
        app.router.add_post("/v1/callout/global", self.callouts)
        app.router.add_get("/v1/token/security", self.security)
        return app

    def _check(self, request):
        q = request.query
        self.seen.append((request.method, request.path, dict(q), request.headers.get("X-APIKEY")))
        assert request.headers.get("X-APIKEY") == "k1"
        assert abs(int(q["timestamp"]) - time.time()) <= 5 and len(q["client_id"]) == 36
        if self.mode == "429":
            return web.json_response({"code": 429, "error": "RATE_LIMIT_BANNED", "reset_at": int(time.time()) + 120},
                                     status=429)
        if self.mode == "plan":
            return web.json_response({"code": 403, "error": "PLAN_UPGRADE_REQUIRED"}, status=403)
        if self.mode == "401":
            return web.json_response({"code": 401, "error": "INVALID_API_KEY"}, status=401)
        return None

    async def rank(self, request):
        bad = self._check(request)
        if bad is not None:
            return bad
        assert request.query["chain"] == "sol" and request.query["interval"] == "5m"
        return web.json_response({"code": 0, "data": {"rank": [RANK, {"symbol": "no address"}]}})

    async def trenches(self, request):
        bad = self._check(request)
        if bad is not None:
            return bad
        body = await request.json()
        assert body["version"] == "v2" and body["near_completion"]["launchpad_platform_v2"] is True
        return web.json_response({"code": 0, "data": {
            "new_creation": [{"address": MINT2, "symbol": "NEW", "progress": 12}],
            "pump": [{"address": MINT, "symbol": "POP", "progress": 0.93}],
            "completed": [{"token_address": "Mig1111111111111111111111111111111111111111", "symbol": "MIG"}]}})

    async def trades(self, request):
        bad = self._check(request)
        if bad is not None:
            return bad
        now = int(time.time())
        return web.json_response({"code": 0, "data": {"list": [
            {"maker": "W1", "side": "buy", "base_address": MINT, "base_token": {"symbol": "POPCAT"}, "amount_usd": 900,
             "is_open_or_close": 0, "timestamp": now, "maker_info": {"twitter_username": "ansem"}},
            {"maker": "W2", "side": "sell", "base_address": MINT2, "amount_usd": "15.5", "timestamp": now * 1000},
            {"maker": "W3", "side": "buy"}]}})

    async def callouts(self, request):
        bad = self._check(request)
        if bad is not None:
            return bad
        body = await request.json()
        assert body == {"chains": ["sol"], "limit": 30}
        return web.json_response({"code": 0, "data": {"list": [
            {"call_token": MINT, "create_time": int(time.time() * 1000), "token_symbol": "POPCAT", "is_safe": "safe",
             "is_kol": True, "twitter_username": "caller"}, {"token_symbol": "без адреса"}]}})

    async def security(self, request):
        bad = self._check(request)
        if bad is not None:
            return bad
        return web.json_response({"code": 0, "data": {"top_10_holder_rate": "0.62", "dev_team_hold_rate": 0.2,
                                                      "suspected_insider_hold_rate": 0.35, "sniper_count": 25,
                                                      "renounced_mint": 1, "renounced_freeze_account": 0,
                                                      "creator_token_status": "creator_hold"}})


async def _with(fake, fn):
    server = TestServer(fake.app())
    await server.start_server()
    g = GmgnClient("k1", host=str(server.make_url("")).rstrip("/"))
    try:
        return await fn(g)
    finally:
        await g.close()
        await server.close()


def test_norm_token_and_security():
    t = norm_token(RANK, "trend_5m")
    assert t["mint"] == MINT and t["top10"] == 0.18 and t["dev"] == 0 and t["rug"] == 0.05 and t["smart"] == 4
    assert t["dev_sold"] is True and t["wash"] is False and t["honeypot"] is False
    assert t["created"] == 1_700_000_000 and t["chg5m"] == 3.2
    assert norm_token({"address": MINT, "top_10_holder_rate": 55})["top10"] == 0.55  # проценты тоже понимаем
    assert norm_token({"symbol": "x"}) is None and norm_token("мусор") is None
    s = norm_security({"top_10_holder_rate": "0.4", "renounced_mint": 1, "stat": {"bundler_trader_amount_rate": 0.31}})
    assert s["top10"] == 0.4 and s["mint_ok"] is True and s["bundler"] == 0.31 and s["freeze_ok"] is None


def test_without_key_no_requests(monkeypatch):
    monkeypatch.delenv("GMGN_API_KEY", raising=False)
    g = GmgnClient()
    assert not g.configured
    with pytest.raises(GmgnError):
        asyncio.run(g.trending())
    assert g.calls == 0
    assert GmgnClient(TEST_KEY).test_key


def test_all_endpoints():
    fake = FakeGmgn()

    async def run(g):
        tr = await g.trending("sol", "5m", limit=50)
        assert [t["mint"] for t in tr] == [MINT] and tr[0]["category"] == "trend_5m"
        tn = await g.trenches("sol", limit=30)
        assert {t["category"] for t in tn} == {"new", "stretch", "migrated"}
        assert next(t for t in tn if t["category"] == "new")["progress"] == 0.12
        sm = await g.smartmoney("sol")
        assert len(sm) == 2 and sm[0]["who"] == "ansem" and sm[0]["opened"] and sm[0]["usd"] == 900
        assert sm[1]["side"] == "sell" and sm[1]["at"] < 2e9  # миллисекунды → секунды
        assert len(await g.kol("sol")) == 2
        co = await g.callouts("sol", limit=30)
        assert len(co) == 1 and co[0]["kol"] and co[0]["safe"] == "safe" and co[0]["at"] < 2e9
        sec = await g.security(MINT)
        assert sec["top10"] == 0.62 and sec["insider"] == 0.35 and sec["snipers_n"] == 25 and sec["dev_sold"] is False
        assert sec["mint_ok"] is True and sec["freeze_ok"] is False
        assert g.calls == 6 and g.status()["error"] is None

    asyncio.run(_with(fake, run))
    # между запросами не меньше 0,5 с (бесплатное «ведро» GMGN — 5 единиц в секунду)
    assert len(fake.seen) == 6


def test_rate_limit_blocks_and_plan_403_is_final():
    fake = FakeGmgn()

    async def run(g):
        fake.mode = "429"
        with pytest.raises(GmgnError, match="лимит"):
            await g.trending()
        assert g.blocked_until > time.time() + 100
        n = len(fake.seen)
        with pytest.raises(GmgnError, match="подождать"):
            await g.trending()  # во время бана — ни одного запроса (повторы продлевают бан)
        assert len(fake.seen) == n
        g.blocked_until = 0
        fake.mode = "plan"
        with pytest.raises(GmgnError, match="тарифе"):
            await g.callouts()
        fake.mode = "ok"
        with pytest.raises(GmgnError, match="тарифе"):
            await g.callouts()  # больше не спрашиваем
        assert len(fake.seen) == n + 1
        assert await g.trending()  # остальные методы работают
        fake.mode = "401"
        with pytest.raises(GmgnError, match="ключ"):
            await g.kol()
        assert g.key_bad and not g.configured

    asyncio.run(_with(fake, run))
