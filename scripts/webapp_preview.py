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
        "okx": PreviewExchange("OKX", 0.0011, {"USDT": 18.4, "SOL": 0.052}),
        "mexc": PreviewExchange("MEXC Global", -0.0009, {}),
    }
    b.subscribers = tb.SubscriberStore(tmp / "subs.json")
    b.demo = DemoManager(b.scanner, settings, tmp / "demo.json")
    env = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "OKX_API_KEY": "k", "OKX_API_SECRET": "s", "OKX_API_PASSWORD": "p"}
    b.trader = Trader(b.scanner, settings, env=env)
    b.trader.key_status = {"bybit": "ok", "okx": "ok"}

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
                          sell_exchange="bybit" if acc.strategy == "tri" else "okx", opened_at=now - (30 - k * 2) * 3600,
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
    b = build()
    await b._scan()
    p = await b.trader.portfolio()            # настоящий итог фейковых балансов
    rnd, now, v, hist = random.Random(3), time.time(), p["total_usd"], []
    for i in range(1, 3 * 96):               # случайное блуждание назад от текущего итога
        hist.append([now - i * 900, round(v, 4)])
        v /= 1 + rnd.gauss(0.0004, 0.004)
    b.trader.state.history = hist[::-1] + [[now, p["total_usd"]]]
    app = web.Application()
    b.webapp.setup(app)
    app.router.add_get("/", lambda r: web.HTTPFound("/app"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"Предпросмотр: http://localhost:{port}/app", flush=True)
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
