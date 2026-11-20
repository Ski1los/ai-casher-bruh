"""Локальный предпросмотр Mini App на ненастоящих данных (без Telegram и без бирж).

    python scripts/webapp_preview.py      → http://localhost:8080/app

Нужен только разработчику, чтобы посмотреть дизайн. В облаке не используется.
"""

from __future__ import annotations

import asyncio
import math
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import web  # noqa: E402

from tests.fake_exchange import FakeTradingExchange, mk_market  # noqa: E402

OWNER = 6069246522
PRICES = {"BTC": 97350.0, "ETH": 3640.0, "SOL": 236.5, "TON": 5.62, "PEPE": 0.0000201, "DOGE": 0.384,
          "XRP": 1.42, "BNB": 652.0, "ADA": 1.05, "TRX": 0.212}


class PreviewExchange(FakeTradingExchange):
    def __init__(self, name, shift, balances):
        rnd = random.Random(name)
        markets, tickers = {}, {}
        for coin, p in PRICES.items():
            s = f"{coin}/USDT"
            markets[s] = mk_market(coin, "USDT", min_cost=1)
            p *= 1 + shift
            tickers[s] = {"bid": p * 0.9998, "ask": p * 1.0002, "last": p, "quoteVolume": rnd.uniform(5e6, 9e8),
                          "percentage": rnd.uniform(-6, 7), "high": p * 1.03, "low": p * 0.96}
        for coin in ("ETH", "SOL", "PEPE", "TON", "XRP"):
            s = f"{coin}/BTC"
            markets[s] = mk_market(coin, "BTC", min_cost=0.00001)
            r = PRICES[coin] / PRICES["BTC"]
            tickers[s] = {"bid": r * 0.9997, "ask": r * 1.0003, "last": r, "quoteVolume": 300, "percentage": 0.5}
        # перекос для треугольника: PEPE/BTC чуть дороже, чем через USDT
        tickers["PEPE/BTC"]["bid"] *= 1.0062
        tickers["PEPE/BTC"]["ask"] *= 1.0066
        super().__init__(name, markets, tickers, balances)
        self.has = {"fetchTickers": True, "fetchOHLCV": True}

    async def fetch_ohlcv(self, symbol, timeframe="15m", limit=120):
        step = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}[timeframe] * 1000
        rnd = random.Random(f"{self.name}{symbol}{timeframe}")
        last = self.tickers[symbol]["last"]
        vol = {"5m": 0.002, "15m": 0.0035, "1h": 0.007, "4h": 0.014, "1d": 0.03}[timeframe]
        closes = [last]
        for _ in range(limit - 1):
            closes.append(closes[-1] / (1 + rnd.gauss(0.0004, vol)))
        closes.reverse()
        now = int(time.time() * 1000) // step * step
        out, prev = [], closes[0] * (1 - vol / 2)
        for i, c in enumerate(closes):
            o = prev
            h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
            lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
            out.append([now - (limit - 1 - i) * step, o, h, lo, c, rnd.uniform(50, 400) * (1 + 3 * abs(c - o) / o / vol)])
            prev = c
        return out

    async def fetch_ticker(self, symbol):
        t = dict(self.tickers[symbol])
        jitter = 1 + math.sin(time.time() / 7) * 0.0006
        t["last"] *= jitter
        return t


def build():
    os.environ["WEBAPP_DEV_USER"] = str(OWNER)
    os.environ.setdefault("AI_API_KEY", "")
    import bot.telegram_bot as tb
    from bot.config import Settings
    from bot.demo import DemoTrade
    from bot.demo_manager import DemoManager
    from bot.trading import Trader, TradeRecord

    tmp = ROOT / ".preview"
    tmp.mkdir(exist_ok=True)
    tb.TRADING_FILE = tmp / "trading.json"
    tb.SETTINGS_FILE = tmp / "settings.json"
    tb.SUBSCRIBERS_FILE = tmp / "subs.json"
    tb.DEMO_FILE = tmp / "demo.json"
    for f in tmp.glob("*.json"):
        f.unlink()

    settings = Settings(exchanges=[], tri_min_profit_percent=0.1, min_profit_percent=0.3)
    b = tb.ArbitrageBot("123456:" + "A" * 35, settings, [OWNER])
    b.backup = None

    async def no_send(*a, **kw):  # предпросмотр не пишет в настоящий Telegram
        return None

    b._send = no_send
    b.scanner.exchanges = {
        "bybit": PreviewExchange("Bybit", 0.0, {"USDT": 41.27, "BTC": 0.00031, "ETH": 0.0042, "TON": 3.1, "PEPE": 152_000}),
        "bingx": PreviewExchange("BingX", 0.0011, {"USDT": 18.4, "SOL": 0.052}),
    }
    b.subscribers = tb.SubscriberStore(tmp / "subs.json")
    b.demo = DemoManager(b.scanner, settings, tmp / "demo.json")
    env = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "BINGX_API_KEY": "k", "BINGX_API_SECRET": "s"}
    b.trader = Trader(b.scanner, settings, env=env)
    b.scalper.trader = b.trader  # автопилот должен смотреть на того же трейдера
    b.trader.key_status = {"bybit": "ok", "bingx": "ok"}
    for name in ("bybit", "bingx"):
        b.scanner.exchanges[name].apiKey = "k"
    b.scanner.exchanges["bingx"].funding = {"USDT": 12.5}  # деньги «не на том счёте» — /doctor подскажет
    b.scanner.exchanges["bingx"].options = {"accountsByType": {"funding": "fund", "spot": "spot"}}

    if os.getenv("PREVIEW_FUNDING") == "1":  # сценарий «закинул $5, а они легли на Funding»
        b.scanner.exchanges["bybit"].balance = {"USDT": 0.0}
        b.scanner.exchanges["bybit"].funding = {"USDT": 5.0}
        b.scanner.exchanges["bybit"].options = {"accountsByType": {"funding": "FUND", "spot": "UNIFIED"}}
        b.trader.state.live = True
    blocked = os.getenv("PREVIEW_BLOCKED") == "1"  # сценарий «ключ только чтение и $1 на счёте»
    if blocked:
        b.scanner.exchanges["bybit"].balance = {"USDT": 1.14}
        b.scanner.exchanges["bingx"].balance = {"USDT": 0.07}
        b.trader.state.live = True

    async def bybit_key_info(params=None):  # как отвечает Bybit: ключ без IP, истекает через 76 дней
        return {"result": {"readOnly": 1 if blocked else 0, "ips": ["*"], "deadlineDay": 76,
                           "permissions": {"Spot": [] if blocked else ["SpotTrade"], "Wallet": ["AccountTransfer"]}}}

    async def bingx_key_info(params=None):
        return {"code": 0, "data": {"apiKey": "k", "permissions": [1, 2], "ipAddresses": [], "note": ""}}

    b.scanner.exchanges["bybit"].privateGetV5UserQueryApi = bybit_key_info
    b.scanner.exchanges["bingx"].accountV1PrivateGetAccountApiPermissions = bingx_key_info

    # Phantom: адрес-пример и снимок без обращения к сети Solana
    from bot.solana import PhantomWatcher
    b.phantom = PhantomWatcher("7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU")

    async def fake_phantom(price_fn, force=False, mint_price_fn=None):
        prices = await price_fn(["SOL", "BONK"])
        assets = [{"coin": "SOL", "amount": 0.84, "usd": 0.84 * prices.get("SOL", 0)},
                  {"coin": "USDC", "amount": 12.0, "usd": 12.0},
                  {"coin": "BONK", "amount": 250000, "usd": None}]
        return {"address": b.phantom.address, "assets": assets, "updated_at": time.time(),
                "total_usd": sum(a["usd"] or 0 for a in assets)}

    b.phantom.get = fake_phantom

    # «Как заработать сейчас»: примеры объявлений Bybit, процентов и новостей без сети
    def _ann(title, key="latest_activities", days=5, tags=()):
        return {"title": title, "description": "", "type": {"key": key}, "tags": list(tags),
                "url": "https://announcements.bybit.com/en/article/" + str(abs(hash(title))),
                "publishTime": int((time.time() - 7200) * 1000), "startDateTimestamp": int((time.time() - 7200) * 1000),
                "endDateTimestamp": int((time.time() + days * 86400) * 1000)}

    sample_anns = {"retCode": 0, "result": {"list": [
        _ann("Bybit Launchpool: Stake USDT, MNT to Share 3,000,000 PLUME (PLUME)!"),
        _ann("Token Splash: Trade WAL to Share 1,500,000 WAL (WAL)", days=9),
        _ann("Puzzle Hunt: Complete Tasks to Share 200,000 USDT Airdrop", days=12),
        _ann("Easy Earn: Up to 12% APR on USDT Flexible Savings for New Users", days=20),
        _ann("WSOT 2026: Trade to Win From a 10,000,000 USDT Prize Pool", days=30),
        _ann("New Listing: Walrus (WAL) — Spot Trading", key="new_crypto", days=0, tags=["Spot"]),
        _ann("Delisting of LOOM (LOOM) Spot Trading Pairs", key="delistings", days=0, tags=["Spot"]),
    ]}}
    sample_earn = {"retCode": 0, "result": {"list": [
        {"category": "FlexibleSaving", "estimateApr": "8.5%", "coin": "USDT", "status": "Available"},
        {"category": "FlexibleSaving", "estimateApr": "5.2%", "coin": "USDC", "status": "Available"},
        {"category": "FlexibleSaving", "estimateApr": "1.1%", "coin": "BTC", "status": "Available"},
        {"category": "FlexibleSaving", "estimateApr": "6.3%", "coin": "SOL", "status": "Available"},
        {"category": "FlexibleSaving", "estimateApr": "4%", "coin": "MNT", "status": "Available"}]}}

    async def opps_json(url, params):
        if "announcements" in url:
            return sample_anns
        return sample_earn if params.get("category") == "FlexibleSaving" else {"result": {"list": [
            {"category": "OnChain", "estimateApr": "7.1%", "coin": "SOL", "status": "Available"}]}}

    async def opps_news(known=None, force=False):
        now = time.time()
        return [{"title": t, "url": "https://forklog.com/news", "source": s, "published": now - h * 3600,
                 "coins": c} for t, s, h, c in [
            ("Биткоин обновил максимум на фоне притока в ETF", "ForkLog", 1, ["BTC"]),
            ("Bybit запустила новый Launchpool с пулом USDT", "ForkLog", 3, []),
            ("Solana network activity hits record as memecoins return", "Cointelegraph", 5, ["SOL"]),
        ]]

    # автопилот: пример результатов проверки на истории (3 стратегии, проверка на новых данных)
    def _r(sym, strat, trades, wins, net, test, tp, ok=True):
        return {"symbol": sym, "strategy": strat, "trades": trades, "wins": wins, "net_pct": net, "test_pct": test,
                "worst_pct": -2.4, "tp": tp, "rsi_buy": 32.0, "open_pct": None, "days": 3.5, "ok": ok}

    b.scalper.ranking["bybit"] = [_r("SOL/USDT", "bounce", 11, 10, 4.2, 1.1, 0.009),
                                  _r("SUI/USDT", "dip", 8, 7, 2.6, 0.4, 0.006),
                                  _r("ETH/USDT", "trend", 7, 6, 2.1, 0.0, 0.006),
                                  _r("DOGE/USDT", "dip", 9, 6, 1.4, -0.8, 0.013, ok=False),
                                  _r("TON/USDT", "bounce", 3, 2, 0.6, 0.2, 0.009, ok=False)]
    b.scalper.ranked_at["bybit"] = time.time() + 10 ** 9  # не пересчитывать в предпросмотре
    b.scalper.st["use_coins"] = True
    # лаборатория и щит: пример
    sc = b.scalper
    sc.lab["stats"].update({
        sc.lab_key("bybit", "SOL/USDT", "bounce"): {"rets": [0.7, 0.7, -0.2, 0.7], "sum": 1.9, "n": 4, "wins": 3, "since": 0},
        sc.lab_key("bybit", "SUI/USDT", "dip"): {"rets": [0.4, -6.2, 0.4], "sum": -5.4, "n": 3, "wins": 2, "since": 0},
        sc.lab_key("bybit", "ETH/USDT", "trend"): {"rets": [0.4], "sum": 0.4, "n": 1, "wins": 1, "since": 0}})
    sc.lab["open"][sc.lab_key("bybit", "ETH/USDT", "trend")] = {"entry": 3000, "tp": 3020, "be": 3005, "stop": 2820,
                                                               "fee": 0.001, "entry_ts": 0, "checked": 0, "opened": 0}
    sc.set_risk({"LOOM": (time.time() + 30 * 86400, "Bybit объявил делистинг: Delisting of LOOM/USDT")})
    sc.st["blocked"]["DOGE"] = [time.time() + 20 * 3600, "был стоп-лосс"]

    b.opps._get_json = opps_json
    b.opps.news = opps_news

    # история: портфель за 3 дня, демо-счета со сделками, журнал реальных сделок
    rnd = random.Random(7)
    now = time.time()
    b.demo.start([5, 50, 500], OWNER)
    b.demo.created_at = now - 30 * 3600
    for acc in b.demo.accounts.values():
        acc.created_at = now - 30 * 3600
        acc.scans = 2160
        n = {"cross": 3, "tri": 14}[acc.strategy]
        bal = acc.start_balance
        for k in range(n):
            exp = rnd.uniform(0.15, 0.6)
            real = exp - rnd.uniform(-0.05, 0.45) - (0.3 if acc.strategy == "cross" and acc.start_balance < 50 else 0)
            t = DemoTrade(symbol="USDT → PEPE → BTC → USDT" if acc.strategy == "tri" else "TON/USDT", buy_exchange="bybit",
                          sell_exchange="bybit" if acc.strategy == "tri" else "bingx", opened_at=now - (30 - k * 2) * 3600,
                          cost_usd=bal, buy_price=0, coins_bought=0, withdraw_fee_coins=0, withdraw_fee_known=True,
                          coins_arrived=0, expected_usd=bal * (1 + exp / 100), signal_percent=exp,
                          closed_at=now - (30 - k * 2) * 3600 + 5, received_usd=bal * (1 + real / 100), strategy=acc.strategy)
            bal = t.received_usd
            acc.trades.append(t)
        acc.balance = bal
        acc.location = "bybit"
        acc.rejects = {"depth": rnd.randint(40, 300), "min_order": rnd.randint(0, 80), "fee": rnd.randint(10, 90)}
    for i, (kind, what, spent, sc, got, gc, pnl, src) in enumerate([
        ("buy", "TON/USDT", 10, "USDT", 1.776, "TON", None, "manual"),
        ("triangle", "USDT → PEPE → BTC → USDT", 10, "USDT", 10.021, "USDT", 0.021, "auto"),
        ("triangle", "USDT → SOL → BTC → USDT", 10, "USDT", 9.987, "USDT", -0.013, "auto"),
        ("buy", "BTC/USDT", 5, "USDT", 0.0000513, "BTC", None, "ai"),
        ("triangle", "USDT → ETH → BTC → USDT", 10, "USDT", 10.034, "USDT", 0.034, "auto"),
    ]):
        b.trader.state.log.append(TradeRecord(now - (5 - i) * 5400, "bybit", kind, what, True, spent, sc, got, gc, spent,
                                              pnl_usd=pnl, source=src).__dict__)
    b.trader.state.day_trades = 3
    b.trader.state.day_pnl_usd = 0.042
    b.trader.state.day = b.trader._today()
    return b


async def main():
    from tests import fake_market_api as fm

    b = build()
    port = int(os.getenv("PORT", "8080"))
    fm.wire(b, f"http://127.0.0.1:{port}/fake")  # мемкоины и Т-Инвестиции на ненастоящих данных
    await b.trader.inspect_keys()
    b.trader.state.auto = True
    await b._scan()
    await b.trader.auto_step(b.scanner.last_result)
    p = await b.trader.portfolio()            # настоящий итог фейковых балансов
    rnd, now, v, hist = random.Random(3), time.time(), p["total_usd"], []
    for i in range(1, 3 * 96):               # случайное блуждание назад от текущего итога
        hist.append([now - i * 900, round(v, 4)])
        v /= 1 + rnd.gauss(0.0004, 0.004)
    b.trader.state.history = hist[::-1] + [[now, p["total_usd"]]]
    app = web.Application()
    b.webapp.setup(app)
    app.add_subapp("/fake", fm.make_app())
    app.router.add_get("/", lambda r: web.HTTPFound("/app"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Предпросмотр: http://localhost:{port}/app", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
