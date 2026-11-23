"""Поддельные DexScreener, RugCheck и T-Invest API (для тестов и предпросмотра, без интернета)."""

from __future__ import annotations

import time

from aiohttp import web

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
NEW = "9wFFyRfZBsuAha4YcuxcXLKwMxJR43S7fPfQLusDBzvT"  # «новый» токен: проходит фильтры радара
SCAM = "ScamXyZ111111111111111111111111111111111pump"  # ловушка: нет продаж, мало ликвидности
WIF = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"


def pair(addr, symbol, name, price, liq, vol, age_h, buys=900, sells=700, ch24=4.2, pair_addr=None, image=True):
    now = time.time() * 1000
    return {
        "chainId": "solana", "dexId": "raydium", "url": f"https://dexscreener.com/solana/{(pair_addr or addr).lower()}",
        "pairAddress": pair_addr or ("P" + addr[1:]), "baseToken": {"address": addr, "name": name, "symbol": symbol},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
        "priceNative": "0.0001", "priceUsd": str(price),
        "txns": {"h1": {"buys": buys // 20, "sells": sells // 20}, "h24": {"buys": buys, "sells": sells}},
        "volume": {"h24": vol}, "priceChange": {"m5": 0.3, "h1": 1.1, "h6": -2.0, "h24": ch24},
        "liquidity": {"usd": liq}, "fdv": liq * 12, "marketCap": liq * 11, "pairCreatedAt": now - age_h * 3600_000,
        "info": {"imageUrl": f"https://cdn.dexscreener.com/cms/images/{symbol.lower()}.png"} if image else {},
    }


PAIRS = {
    BONK: [pair(BONK, "BONK", "Bonk", 0.00002612, 9_800_000, 41_000_000, 24 * 700, ch24=-3.1),
           pair(BONK, "BONK", "Bonk", 0.00002609, 120_000, 300_000, 24 * 500, pair_addr="Q" + BONK[1:])],
    WIF: [pair(WIF, "WIF", "dogwifhat", 1.734, 6_100_000, 88_000_000, 24 * 600, ch24=7.8)],
    NEW: [pair(NEW, "MOODENG", "Moo Deng Sol", 0.004271, 85_000, 640_000, 10, buys=5400, sells=4100, ch24=58.0)],
    SCAM: [pair(SCAM, "BONK", "Bonk 2.0 (real)", 0.0000009, 4_100, 21_000, 0.6, buys=420, sells=0, ch24=900, image=False)],
}

RUGS = {
    BONK: {"score_normalised": 3, "risks": [], "lpLockedPct": 100},
    WIF: {"score_normalised": 5, "risks": [], "lpLockedPct": 100},
    NEW: {"score_normalised": 12, "risks": [{"name": "Low amount of LP Providers", "level": "warn",
                                             "description": "Only a few users are providing liquidity", "score": 400}],
          "lpLockedPct": 99.4},
    SCAM: {"score_normalised": 88, "risks": [
        {"name": "Mint Authority still enabled", "level": "danger", "description": "More tokens can be minted by the owner"},
        {"name": "Freeze Authority still enabled", "level": "danger", "description": "Tokens can be frozen and prevented from trading"},
        {"name": "Top 10 holders high ownership", "level": "danger", "description": "The top 10 users hold more than 70% token supply"},
    ], "lpLockedPct": 0},
}


def mv(value: float, cur: str = "rub") -> dict:
    units = int(value)
    return {"currency": cur, "units": str(units), "nano": int(round((value - units) * 1e9))}


def q(value: float) -> dict:
    units = int(value)
    return {"units": str(units), "nano": int(round((value - units) * 1e9))}


TI_ACCOUNTS = {"accounts": [
    {"id": "2000111", "type": "ACCOUNT_TYPE_TINKOFF", "name": "Брокерский счёт", "status": "ACCOUNT_STATUS_OPEN",
     "accessLevel": "ACCOUNT_ACCESS_LEVEL_READ_ONLY"},
    {"id": "2000222", "type": "ACCOUNT_TYPE_TINKOFF_IIS", "name": "ИИС", "status": "ACCOUNT_STATUS_OPEN",
     "accessLevel": "ACCOUNT_ACCESS_LEVEL_READ_ONLY"},
    {"id": "2000333", "type": "ACCOUNT_TYPE_TINKOFF", "name": "Закрытый", "status": "ACCOUNT_STATUS_CLOSED"},
]}

TI_PORTFOLIOS = {
    "2000111": {
        "totalAmountShares": mv(48210.5), "totalAmountBonds": mv(10230), "totalAmountEtf": mv(5120.4),
        "totalAmountCurrencies": mv(1834.22), "totalAmountFutures": mv(0), "totalAmountPortfolio": mv(65395.12),
        "expectedYield": q(6.84), "dailyYield": mv(-412.3), "dailyYieldRelative": q(-0.63), "accountId": "2000111",
        "positions": [
            {"figi": "BBG004730N88", "ticker": "SBER", "instrumentType": "share", "quantity": q(60),
             "averagePositionPrice": mv(268.4), "currentPrice": mv(301.25), "expectedYield": q(1971)},
            {"figi": "BBG004731032", "ticker": "LKOH", "instrumentType": "share", "quantity": q(3),
             "averagePositionPrice": mv(7120), "currentPrice": mv(6802.5), "expectedYield": q(-952.5)},
            {"figi": "BBG00475K6C3", "ticker": "SU26238RMFS4", "instrumentType": "bond", "quantity": q(15),
             "averagePositionPrice": mv(655.1), "currentPrice": mv(682), "expectedYield": q(403.5)},
            {"figi": "TCS00A1039N1", "ticker": "TMOS", "instrumentType": "etf", "quantity": q(800),
             "averagePositionPrice": mv(6.21), "currentPrice": mv(6.4005), "expectedYield": q(152.4)},
            {"figi": "RUB000UTSTOM", "ticker": "RUB000UTSTOM", "instrumentType": "currency", "quantity": q(1834.22),
             "averagePositionPrice": mv(1), "currentPrice": mv(1), "expectedYield": q(0)},
        ],
    },
    "2000222": {"totalAmountPortfolio": mv(120000), "totalAmountBonds": mv(120000), "expectedYield": q(2.1),
                "positions": [{"ticker": "OFZ-29024", "instrumentType": "bond", "quantity": q(120),
                               "averagePositionPrice": mv(980), "currentPrice": mv(1000), "expectedYield": q(2400)}]},
}


CALLS = web.AppKey("calls", list)


def make_app(token: str = "good-token") -> web.Application:
    app = web.Application()
    app[CALLS] = []

    async def search(request):
        app[CALLS].append(("search", request.query.get("q")))
        text = (request.query.get("q") or "").lower()
        out = []
        for addr, pairs in PAIRS.items():
            if text == addr.lower() or any(text in (p["baseToken"]["symbol"] + p["baseToken"]["name"]).lower() for p in pairs):
                out += pairs
        return web.json_response({"schemaVersion": "1.0.0", "pairs": out})

    async def tokens(request):
        addrs = request.match_info["addrs"].split(",")
        app[CALLS].append(("tokens", addrs))
        return web.json_response([p for a in addrs for p in PAIRS.get(a, [])])

    async def boosts(request):
        app[CALLS].append(("boosts",))
        return web.json_response([{"chainId": "solana", "tokenAddress": a, "amount": 100, "totalAmount": 500}
                                  for a in (NEW, WIF, SCAM)] + [{"chainId": "base", "tokenAddress": "0x" + "1" * 40}])

    async def profiles(request):
        return web.json_response([{"chainId": "solana", "tokenAddress": BONK, "url": "https://dexscreener.com"}])

    async def rug(request):
        mint = request.match_info["mint"]
        app[CALLS].append(("rug", mint))
        if mint not in RUGS:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"tokenProgram": "Tokenkeg", **RUGS[mint]})

    def check_token(request):
        if request.headers.get("Authorization") != f"Bearer {token}":
            raise web.HTTPUnauthorized(text='{"code":16,"message":"authentication token is missing or invalid"}',
                                       content_type="application/json")

    async def ti_accounts(request):
        check_token(request)
        app[CALLS].append(("ti_accounts",))
        return web.json_response(TI_ACCOUNTS)

    async def ti_portfolio(request):
        check_token(request)
        body = await request.json()
        app[CALLS].append(("ti_portfolio", body))
        acc = body.get("accountId")
        if acc not in TI_PORTFOLIOS:
            return web.json_response({"code": 3, "message": "account not found"}, status=400)
        return web.json_response(TI_PORTFOLIOS[acc])

    app.router.add_get("/latest/dex/search", search)
    app.router.add_get("/tokens/v1/{chain}/{addrs}", tokens)
    app.router.add_get("/token-boosts/top/v1", boosts)
    app.router.add_get("/token-profiles/latest/v1", profiles)
    app.router.add_get("/rug/v1/tokens/{mint}/report/summary", rug)
    prefix = "/ti/tinkoff.public.invest.api.contract.v1."
    app.router.add_post(prefix + "UsersService/GetAccounts", ti_accounts)
    app.router.add_post(prefix + "OperationsService/GetPortfolio", ti_portfolio)
    return app


def wire(b, base_url: str, token: str = "good-token") -> None:
    """Переключить бота на поддельные API по адресу base_url."""
    from bot.dex import DexClient
    from bot.tinvest import TInvestClient

    import asyncio

    _TASKS = getattr(wire, "_tasks", None) or set()
    wire._tasks = _TASKS
    for old in (getattr(b, "dex", None), getattr(b, "tinvest", None)):  # старые клиенты закрываем, а не бросаем
        sess = getattr(old, "_session", None)
        if sess is not None and not sess.closed:
            try:
                t = asyncio.get_running_loop().create_task(sess.close())
                _TASKS.add(t)
                t.add_done_callback(_TASKS.discard)
            except RuntimeError:
                pass
    b.dex = DexClient(base_url, base_url + "/rug/v1")
    b.tinvest = TInvestClient(token, base_url + "/ti/tinkoff.public.invest.api.contract.v1.", ssl_ctx=False)
