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

    # 🔁 кошелёк: статус монет по сетям (как ccxt fetch_currencies), адреса, выводы
    async def fetch_currencies(self, params=None):
        bybit = self.name == "Bybit"
        n = (lambda net, fee, w=True, d=True: {"network": net, "fee": fee, "withdraw": w, "deposit": d, "active": True,
                                                 "limits": {"withdraw": {"min": 1}}})
        out = {"USDT": {"nets": {"TRX" if bybit else "TRC20": n("TRC20", 1.0 if bybit else 1.5),
                                  "ETH" if bybit else "ERC20": n("ERC20", 4.0), "SOL": n("SOL", 1.0, w=not bybit)}},
               "TON": {"nets": {"TON": n("TON", 0.05)}},
               "LUNA": {"nets": {"LUNA" if bybit else "Terra": n("LUNA", 0.1, w=not bybit)}, "withdraw": not bybit}}
        for c in PRICES:
            out.setdefault(c, {"nets": {c: n(c, 0.0005)}})
        return {c: {"id": c, "code": c, "active": True, "withdraw": v.get("withdraw", True), "deposit": True,
                    "networks": v["nets"]} for c, v in out.items()}

    async def fetch_deposit_address(self, code, params=None):
        return {"address": ("TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE" if self.name == "BingX" else
                            "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"), "tag": "4815162342" if code == "TON" else None}

    async def withdraw(self, code, amount, address, params=None):
        self.balance[code] = self.balance.get(code, 0) - amount
        return {"id": f"pw{int(time.time())}"}

    async def fetch_withdrawals(self, code=None, since=None, limit=None, params=None):
        return []

    async def fetch_deposits(self, code=None, since=None, limit=None, params=None):
        return []

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
                           "permissions": {"Spot": [] if blocked else ["SpotTrade"], "Wallet": ["AccountTransfer", "Withdraw"]}}}

    async def bingx_key_info(params=None):
        return {"code": 0, "data": {"apiKey": "k", "permissions": [1, 2, 5], "ipAddresses": ["203.0.113.7"], "note": ""}}

    b.trader.state.transfers = {"allowed": True, "items": [{"id": "1", "at": time.time() - 3600, "coin": "USDT", "amount": 15.0,
                                                           "src": "bingx", "dst": "bybit", "network": "TRC20", "fee": 1.5,
                                                           "usd": 15.0, "status": "done", "source": "manual"}]}
    b.scanner.exchanges["bybit"].privateGetV5UserQueryApi = bybit_key_info
    now = time.time()
    b.trader.state.spread = {  # ⚖️ спред: пример итогов и проверок
        "on": True, "paper": True,
        "totals": {"test": [3, 0.214], "live": [0, 0.0]},
        "checks": [{"at": now - 60, "coin": "SOL", "src": "bybit", "dst": "bingx", "gross": 1.84, "pct": 1.12, "reason": "",
                    "why": ""},
                   {"at": now - 120, "coin": "TON", "src": "bingx", "dst": "bybit", "gross": 0.92, "pct": -1.4,
                    "reason": "fees", "why": ""},
                   {"at": now - 180, "coin": "XRP", "src": "bybit", "dst": "bingx", "gross": 4.1, "pct": None,
                    "reason": "wallet", "why": ""}],
        "runs": [{"id": "a1", "mode": "test", "coin": "SOL", "src": "bybit", "dst": "bingx", "symbol": "SOL/USDT",
                  "network": "SOL", "minutes": 3, "stage": "done", "opened": now - 900, "spent": 10.0, "bought": 0.0422,
                  "sent": 0.0412, "fee": 0.001, "exp_usd": 10.11, "exp_pct": 1.1, "gross": 1.8, "eta": now - 700,
                  "received": 10.09, "pnl": 0.09, "closed": now - 690, "arrived_at": now - 700, "note": ""}],
    }
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

    b.scalper.ranking["bybit"] = [dict(_r("SOL/USDT", "bounce", 11, 10, 4.2, 1.1, 0.009),
                                       alts=[{"strategy": "dip", "tp": 0.006, "rsi_buy": 25.0, "train_pct": 1.2,
                                              "net_pct": 1.8, "trades": 7},
                                             {"strategy": "trend", "tp": 0.006, "rsi_buy": 25.0, "train_pct": -0.4,
                                              "net_pct": -0.3, "trades": 4}]),
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
        sc.lab_key("bybit", "ETH/USDT", "trend"): {"rets": [0.4], "sum": 0.4, "n": 1, "wins": 1, "since": 0},
        sc.lab_key("bybit", "SOL/USDT", "dip"): {"rets": [0.4, 0.4, 0.4, -0.1, 0.4, 0.4, 0.4], "sum": 2.3, "n": 7,
                                                 "wins": 6, "since": 0},
        sc.lab_key("bybit", "SOL/USDT", "trend"): {"rets": [0.4, -6.1], "sum": -5.7, "n": 2, "wins": 1, "since": 0}})
    sc.lab["open"][sc.lab_key("bybit", "ETH/USDT", "trend")] = {"entry": 3000, "tp": 3020, "be": 3005, "stop": 2820,
                                                               "fee": 0.001, "entry_ts": 0, "checked": 0, "opened": 0}
    sc.set_risk({"LOOM": (time.time() + 30 * 86400, "Bybit объявил делистинг: Delisting of LOOM/USDT")})
    sc.st["blocked"]["DOGE"] = [time.time() + 20 * 3600, "был стоп-лосс"]

    # копитрейдинг: пример кошельков, копий и уроков
    from bot.copytrade import analyze_history
    ct, now = b.copy, time.time()
    ct.st["on"] = True
    hist_good = analyze_history([{"ts": now - 86400 + k * 3000, "side": s, "mint": f"M{k // 2}", "sol": v,
                                  "amount": 100, "usd": 0}
                                 for k, (s, v) in enumerate([("buy", 1), ("sell", 1.7), ("buy", 1), ("sell", 0.8),
                                                            ("buy", .5), ("sell", 1.1), ("buy", 1), ("sell", 1.4)])], now)
    hist_bot = dict(hist_good, grade="bot", grade_text="🤖 бот-снайпер: сотни сделок, копировать бесполезно")
    for addr, name, status, rets, src, hist, tokens in [
        ("7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", "7xKX…gAsU (MOODENG, PNUT)", "star",
         [18.0, -12.0, 34.0, 6.0, 9.5, -4.0, 22.0], "auto", hist_good, ["MOODENG", "PNUT", "GOAT"]),
        ("5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", "Кит из GMGN", "watch", [12.0, -8.0, 3.0], "user", hist_good,
         ["WIF", "POPCAT"]),
        ("9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "9WzD…AWWM (FWOG)", "watch", [], "auto", None, ["FWOG"]),
        ("H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS", "H8sM…3WjS (CHILLGUY)", "dropped",
         [-30.0, -22.0, -41.0, -18.0], "auto", hist_bot, ["CHILLGUY"])]:
        w = ct._new_wallet(name, src, now - 3 * 86400)
        w.update(status=status, rets=rets, n=len(rets), wins=sum(r > 0 for r in rets), sum=round(sum(rets), 1),
                 hist=hist, tokens=tokens, last_trade=now - 1800, primed=True,
                 reason={"star": "копии вживую: 7 сделок, +73.5%", "dropped": "4 убытка подряд, копия в среднем на +31% "
                         "дороже его цены (не успеваем)"}.get(status, ""))
        ct.st["wallets"][addr] = w
    feats = {"liq": 85000, "age_h": 10, "rug": 12, "chase": 0.04, "h1": 12, "fdv_liq": 12}
    ct.st["open"]["x|m1"] = {"wallet": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", "mint": "m1", "symbol": "GOAT",
                             "opened": now - 2400, "raw": 1.0, "entry": 1.0185, "last": 1.12, "peak": 1.15, "liq0": 9e4,
                             "w_amount": 5, "w_usd": 120, "feats": feats, "passed": True, "reasons": []}
    ct.st["open"]["y|m2"] = {"wallet": "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM", "mint": "m2", "symbol": "FWOG",
                             "opened": now - 600, "raw": 1.0, "entry": 1.0185, "last": 0.93, "peak": 1.0, "liq0": 9e3,
                             "w_amount": 5, "w_usd": 40, "feats": dict(feats, liq=9000), "passed": False,
                             "reasons": ["ликвидность $9.0K < $20K"]}
    for sym, ret, why, passed, mis in [("PNUT", 33.6, "кошелёк продал", True, []),
                                       ("CHILLGUY", -41.8, "стоп-лосс", True, ["late", "pump"]),
                                       ("RETARDIO", -90.0, "ликвидность ушла", False, ["liq", "rugpull"]),
                                       ("MOODENG", 17.2, "кошелёк продал", True, [])]:
        ct.st["closed"].append({"wallet": "x", "mint": sym, "symbol": sym, "ret": ret, "why": why, "opened": now - 9000,
                                "closed": now - 3000, "feats": feats, "passed": passed, "hard_fail": False, "mistakes": mis})
    ct.st["mistakes"] = {"late": 4, "liq": 3, "fast": 2, "dump": 2, "rugpull": 1}
    ct.st["candidates"] = {f"c{k}": {"seen": 1, "tokens": [], "first": now} for k in range(23)}
    ct._lesson("🔴 Кошелёк H8sM…3WjS (CHILLGUY) выгнан: 4 убытка подряд, копия в среднем на +31% дороже его цены "
               "(не успеваем). Ищу замену.", now - 7200)
    ct._lesson("🧠 Урок: «переплата к цене кошелька не больше» 30% → 20%. На истории копий так было бы +48.3% вместо "
               "+21.0% (19 сделок вместо 24).", now - 3600)
    ct._lesson("⭐ Кошелёк 7xKX…gAsU (MOODENG, PNUT) повышен: его копии без денег дали +73.5% за 7 сделок. Теперь его "
               "покупки приходят сигналом.", now - 1200)
    ct.st["filter"]["max_chase"] = 0.2

    # 💸 реальная торговля: поддельный кошелёк «Бот» (без сети и без ключа)
    from bot.memetrade import MemeTrader

    class PreviewWallet:
        address = "BoT7r4d3rWa11et1111111111111111111111111111"

        async def sol_balance(self):
            return 41_250_000

    ct._sol_usd = (now + 10**9, 152.4)
    real = os.environ.get("PREVIEW_REAL") == "1"  # по умолчанию — как у новичка: кошелька нет, торгует демо
    b.meme = MemeTrader(ct, env={}, wallet=PreviewWallet() if real else None)
    b.meme.start_demo_if_new()
    rs = b.meme.st
    rs.update(on=real, size_usd=2.0, tp_pct=0.5, stop_pct=0.3)
    ct.st["calib"] = {"rts": [0.021, 0.018, 0.024, 0.02, 0.019], "cost": 0.013}
    rs["learn"].update(good_ok=True, good_n=14, good_avg=3.4)
    ct._lesson("🧪 Калибровка: по настоящим котировкам Jupiter вход+выход стоит ≈ 2.6% (я считал 3.7%). Копии без "
               "денег теперь считаются с этой стоимостью — оценки кошельков честнее.", now - 900)
    ct._lesson("🧠 Урок по выходам: тейк-профит нет → +50%. На 31 копии с настоящим путём цены так было бы +96.0% "
               "вместо +58.2%. Применяю к демо и реальным сделкам.", now - 600)
    d = b.meme.demo
    sol = 152.4
    d.update(start_usd=10.0, start_sol=int(10 / sol * 1e9))
    d["positions"]["d1"] = {"mint": "d1", "symbol": "GOAT", "source": "copy", "key": "x|d1", "category": "star",
                            "wallet": "7xKX…gAsU (MOODENG, PNUT)", "opened": now - 2400, "sol_in": 13_100_000,
                            "raw": 0, "price_in": None, "value0": 12_800_000, "value": 15_200_000, "src": "jupiter",
                            "usd_in": 2.0, "sell_why": None}
    demo_closed = [("PNUT", 0.71, 35.6, "копия: кошелёк продал", "star"), ("WIF", -0.62, -31.0, "стоп −30%", "good"),
                   ("POPCAT", 1.04, 52.0, "тейк-профит +50%", "star"), ("FWOG", 0.18, 9.0, "копия: кошелёк продал", "good"),
                   ("CHILLGUY", -0.58, -29.0, "копия: кошелёк продал", "star"), ("MEW", 0.33, 16.5, "копия: кошелёк продал", "star")]
    for k, (sym, usd, pc, why, cat) in enumerate(demo_closed):
        d["closed"].append({"symbol": sym, "mint": sym, "pnl_sol": usd / sol, "pnl_usd": usd, "pct": pc, "why": why,
                            "opened": now - 90000 + k * 12000, "closed": now - 85000 + k * 12000, "source": "copy",
                            "sig": None, "category": cat})
    pnl = sum(x[1] for x in demo_closed)
    d["stats"].update(buys=7, sells=6, wins=4, pnl_usd=round(pnl, 2), pnl_sol=round(pnl / sol, 6))
    d["cash"] = int((10 + pnl - 2.0) / sol * 1e9)
    d["decisions"] += [{"at": now - 7000, "symbol": "RETARDIO", "text": "не купил: риск-фильтр: ликвидность $9.0K < $20K"},
                       {"at": now - 2400, "symbol": "GOAT", "text": "купил за ⭐ 7xKX…gAsU (MOODENG, PNUT)"}]
    rs["positions"]["m1"] = {"mint": "m1", "symbol": "GOAT", "source": "copy", "key": "x|m1",
                             "wallet": "7xKX…gAsU (MOODENG, PNUT)", "opened": now - 2400, "sol_in": 13_100_000,
                             "raw": 1, "usd_in": 2.0, "sig": "s", "value": 14_650_000, "sell_fails": 0,
                             "sell_why": None}
    rs["closed"] += [{"symbol": "PNUT", "mint": "p", "pnl_sol": 0.0042, "pnl_usd": 0.64, "pct": 32.1,
                      "why": "копия: кошелёк продал", "opened": now - 9000, "closed": now - 5000, "source": "copy"},
                     {"symbol": "CHILLGUY", "mint": "c", "pnl_sol": -0.0055, "pnl_usd": -0.84, "pct": -41.9,
                      "why": "стоп −40%", "opened": now - 20000, "closed": now - 15000, "source": "copy"}]
    rs["stats"].update(buys=3, sells=2, wins=1, pnl_sol=-0.0013, pnl_usd=-0.2, rent_back_sol=0.004)
    rs["decisions"] += [{"at": now - 15500, "symbol": "CHILLGUY", "text": "купил за ⭐ 7xKX…gAsU (MOODENG, PNUT)"},
                        {"at": now - 7000, "symbol": "RETARDIO", "text": "не купил: риск-фильтр: ликвидность $9.0K < $20K"},
                        {"at": now - 2400, "symbol": "GOAT", "text": "купил за ⭐ 7xKX…gAsU (MOODENG, PNUT)"}]

    # 🎓 трейдеры: стартовый список, пара разобранных роликов, прогнозы и советы (без сети)
    mn = b.mentors
    mn.meme = b.meme
    mn.gemini.key = "AIzaPREVIEW"
    mn.gemini.model = "gemini-flash-latest"
    mn.seed()
    ms = mn.st
    srcs = ms["sources"]
    cow, ban, decu = srcs["yt:UCRvqjQPSeaWn-uEx-w0XOIg"], srcs["yt:UCN9Nj4tjXbVTLYWN0EKly_Q"], srcs["tw:decu"]
    cow.update(status="star", videos=11, calls=14, rets=[6.1, 3.2, -2.0, 8.4, 4.4, -1.1, 5.0, 2.7, 7.3, 1.9], hype=[2, 1, 3])
    ban.update(status="muted", videos=9, calls=17, sponsored=7, rets=[-8.0, 3.1, -12.4, -6.6, 2.0, -9.9, -4.1, -7.5],
               hype=[8, 9, 7])
    decu.update(calls=6, rets=[41.0, -31.2, 12.5])
    mn.twitch.connected = True
    mn.twitch.channels = {s["ident"] for s in mn.active("twitch")}
    ms["videos"] += [
        {"id": "v1", "source": ban["id"], "author": "Crypto Banter", "title": "ALTSEASON IS HERE!!!", "url": "https://youtu.be/x",
         "published": now - 9000, "at": now - 8000, "method": "gemini", "summary": "Ведущий уверен в скором альтсезоне и "
         "советует докупать альткоины; аргументов почти нет, много эмоций.", "market_view": "bullish", "calls": [{}],
         "tips": [], "sponsored": True, "sponsor_note": "биржа с бонусом за депозит", "hype": 9, "call_ids": []},
        {"id": "v2", "source": cow["id"], "author": "Benjamin Cowen", "title": "Bitcoin: The Liquidity Problem",
         "url": "https://youtu.be/y", "published": now - 5000, "at": now - 4000, "method": "gemini",
         "summary": "BTC упирается в снижение ликвидности: автор ждёт боковик и откат альтов к BTC, советует не "
         "входить на всё сразу и держать стоп.", "market_view": "bearish", "calls": [{}, {}], "tips": [],
         "sponsored": False, "sponsor_note": "", "hype": 2, "call_ids": []}]
    ms["calls"] += [
        {"id": "c1", "source": cow["id"], "asset": "ETH", "mint": "", "key": "ETH", "direction": "short", "at": now - 4000,
         "horizon_h": 72, "path": [0.0, -1.2, -2.4], "done": False, "ret": None, "price0": 3100.0, "kind": "cex"},
        {"id": "c2", "source": ban["id"], "asset": "SOL", "mint": "", "key": "SOL", "direction": "long", "at": now - 200000,
         "horizon_h": 48, "path": [0.0, -3.0, -5.1], "done": True, "ret": -5.5, "price0": 160.0, "kind": "cex"},
        {"id": "c3", "source": decu["id"], "asset": "GOAT", "mint": "d1", "key": "d1", "direction": "long", "at": now - 3000,
         "horizon_h": 24, "path": [0.0, 18.0, 12.4], "done": False, "ret": None, "price0": 0.01, "kind": "sol"}]
    ms["tips"] += [
        {"text": "Не входить в токен, если он уже вырос на 30% за час", "rule": {"param": "max_chase_pct", "value": 30},
         "source": cow["id"], "author": "Benjamin Cowen", "at": now - 4000,
         "verdict": {"state": "rejected", "text": "не подтвердился: 30% дал бы +12.0% (22 сделки) против +21.0% (19)"}},
        {"text": "Стоп −20% на мемкоинах", "rule": {"param": "stop_pct", "value": 20}, "source": decu["id"],
         "author": "decu", "at": now - 2000,
         "verdict": {"state": "accepted", "text": "принят: стоп −20% дал бы +71.0% вместо +58.2% на 31 копиях"}}]
    ms["budget"].update(day=time.strftime("%Y-%m-%d", time.gmtime(now)), videos=4)
    mn._log("✅ Принял совет decu: стоп −20%. На 31 копиях было бы +71.0% вместо +58.2%.", now - 2000)
    # 🔭 разведчик: двое на испытании, один отсеян фейк-фильтром
    sco = mn.scout
    sco.st["on"] = False  # превью без сети
    for kind, ident, name, via, upd in (
            ("youtube", "UCxxxxxxxxxxxxxxxxxxxx01", "DataDash", "Gemini + Google", dict(videos=3, calls=4, rets=[3.1, -1.2, 4.0])),
            ("twitch", "trencher", "Trencher", "Twitch · Crypto", dict(calls=2))):
        s = mn._add(kind, ident, name, note="🔭 " + via, user=False)
        sco._mark(s, via, 1, now - 3 * 86400, kind)
        s.update(upd)
    sco.st.update(found=7, kept=1, dropped=3)
    mn.candles_of = lambda coin, since: asyncio.sleep(0, [])  # кнопка «📜 История» видна
    hist = {"n": 7, "wins": 5, "avg": 2.4, "shrunk": 1.4, "bull_share": 0.71, "rows": [], "method": "Gemini", "at": now}
    srcs["yt:UCxxxxxxxxxxxxxxxxxxxx01"]["history"] = hist
    cow["history"] = {**hist, "n": 9, "wins": 6, "avg": 3.1, "shrunk": 2.0}
    ban["history"] = {**hist, "n": 11, "wins": 3, "avg": -4.0, "shrunk": -2.8, "bull_share": 1.0}
    sco.st["via_stats"] = {"Gemini + Google": {"taken": 4, "kept": 1, "dropped": 2},
                           "поиск YouTube": {"taken": 3, "kept": 0, "dropped": 1}}
    sco._reject("yt:UCbad", "Crypto Millionaire 100x", "фейк-фильтр: 1.2M подписчиков, а смотрят ~800 — похоже на накрутку",
                now - 86400)
    sco._reject("tw:slots", "SlotsKing", "гемблинг/раздачи в названии стрима", now - 80000)
    # 🏋️ тренировка: 5 стратегий с демо-сделками
    tst = mn.train.st
    demo_rets = {"all": [4.1, -7.9, 2.2, -8.1, 9.6, -3.0, 1.2, -8.0, 5.5, -2.4, 0.8],
                 "trusted": [4.1, 2.2, 9.6, -3.0, 5.5, 1.2, -2.4, 6.3, 3.9],
                 "consensus": [9.6, 5.5, -8.0, 3.9], "clean": [4.1, 2.2, -8.1, 1.2, -2.4, 0.8, -7.9, 2.0],
                 "confident": [9.6, -8.0, 5.5, 6.3, -2.4]}
    for name, rets in demo_rets.items():
        v = tst["variants"][name]
        cash = 10.0
        for k, r in enumerate(rets):
            usd = round(cash * 0.25, 2)
            cash += usd * r / 100
            v["closed"].append({"asset": ["SOL", "ETH", "GOAT", "LINK", "SUI"][k % 5], "author": ["Benjamin Cowen", "decu",
                                "Coin Bureau"][k % 3], "kind": "sol" if k % 5 == 2 else "cex", "pct": r,
                                "usd": round(usd * r / 100, 4), "why": "stop" if r < -7.5 else "tp" if r > 9 else "time",
                                "at": now - 400000 + k * 30000, "closed": now - 380000 + k * 30000})
        v.update(cash=round(cash, 4), n=len(rets), wins=sum(r > 0 for r in rets), rets=rets, pnl=round(cash - 10, 4))
    tst["active"] = "trusted"
    tst["ideas"] = 23
    tst["rules"]["cex"] = {"stop": 0.08, "tp": 0.1}
    tst["track"]["c1"] = {"kind": "cex", "asset": "ETH", "mint": "", "price0": 3100.0, "at": now - 4000,
                          "until": now + 200000, "path": [0.0, -1.2, 2.4]}
    tv = tst["variants"]["trusted"]
    tv["cash"] -= 2.6
    tv["positions"]["c1"] = {"id": "c1", "key": "ETH", "kind": "cex", "asset": "ETH", "mint": "", "author": "Benjamin Cowen",
                             "at": now - 4000, "usd": 2.6, "until": now + 200000, "stop": 0.08, "tp": 0.1, "now": 2.4}
    mn.train._lesson("🏋️ Урок по выходам (монеты: стоп −8%, тейк нет → стоп −8%, тейк +10%). На 16 идеях с полным путём "
                     "цены так было бы +31.2% вместо +18.4%.", now - 30000)
    mn.train._lesson("🏋️ Лучшая стратегия на демо — «Проверенные авторы»: 9 сделок, в плюс 7, в среднем +1.9% за сделку "
                     "(с поправкой на везение). Теперь сигналы — только по ней.", now - 9000)

    # 🎥 стримы вживую: decu в эфире, итог прошлого эфира, привычки и «цена ошибок» по проверенным входам
    lv = mn.live
    lv.st["budget"].update(day=time.strftime("%Y-%m-%d", time.gmtime(now)), n=37)
    lv.st["total"] = 212
    lv.st["sessions"]["decu"] = {
        "login": "decu", "name": "decu", "stream_id": "1", "title": "SOL trenches — 100 → 1000", "started": now - 5400,
        "opened": now - 5300, "last_at": now - 120, "n": 17, "mode": "video", "why": "",
        "obs": [{"at": now - 120, "summary": "Держит лонг GOAT в +12%, подтянул стоп в безубыток и объяснил чату, "
                                                "где будет фиксировать половину.", "asset": "GOAT", "action": "move_stop",
                 "position": {"side": "long"}}],
        "actions": [{"at": now - 3900, "action": "enter_long", "asset": "WIF", "said": ""},
                    {"at": now - 3000, "action": "exit_loss", "asset": "WIF", "said": ""},
                    {"at": now - 1500, "action": "enter_long", "asset": "GOAT", "said": ""},
                    {"at": now - 120, "action": "move_stop", "asset": "GOAT", "said": ""}],
        "plus": {"stop_set": 2, "cut_loss": 1}, "miss": {"fomo_chase": 1}, "disc": [5, 6, 7, 7], "lessons": [],
        "acted": {}, "counted": {}}
    decu["live"] = {"sessions": 6, "obs": 118, "minutes": 1260, "entries": 21,
                    "plus": {"stop_set": 9, "cut_loss": 6, "took_profit": 4}, "miss": {"fomo_chase": 7, "oversize": 4,
                                                                                       "averaging_down": 3},
                    "disc": [6, 5, 7, 6, 6, 4, 7]}
    trencher = srcs["tw:trencher"]
    trencher["live"] = {"sessions": 2, "obs": 31, "minutes": 380, "entries": 9, "plus": {"explained": 2},
                        "miss": {"no_stop": 6, "overleverage": 4, "revenge": 2}, "disc": [3, 2, 4, 3]}
    lv.st["history"].append({"login": "trencher", "name": "Trencher", "title": "", "at": now - 20000, "minutes": 190,
                             "n": 31, "entries": 9, "exits": 7, "wins": 2, "losses": 5, "assets": ["BONK", "WIF"],
                             "plus": {"explained": 2}, "miss": {"no_stop": 6, "revenge": 2}, "disc": 3.1,
                             "lessons": ["После двух убытков подряд — пауза, а не сделка вдвое больше"]})
    for i, (tags, r) in enumerate([(["no_stop"], -6.1), (["no_stop", "overleverage"], -9.4), (["no_stop"], -2.2),
                                   (["no_stop", "fomo_chase"], -7.0), (["fomo_chase"], -3.3), (["fomo_chase"], 1.5),
                                   (["fomo_chase"], -4.0), (["stop_set"], 3.1), (["stop_set"], -1.0),
                                   (["stop_set", "explained"], 4.4), (["stop_set"], 2.2)]):
        ms["calls"].append({"id": f"lv{i}", "source": decu["id"], "asset": "SOL", "mint": "", "key": f"lv{i}",
                            "direction": "long", "at": now - 300000, "horizon_h": 24, "path": [r], "done": True,
                            "ret": r, "price0": 150.0, "kind": "cex", "live": True, "tags": tags})

    # 🔎 мелкие детали на экране и 🪞 повтор на демо
    lv.st["sessions"]["decu"]["details"] = [
        {"at": now - 900, "type": "insider_chat", "text": "в углу экрана Telegram-чат «alpha»: кинули GOAT и адрес",
         "who": "alpha calls", "asset": "GOAT", "contract": ""},
        {"at": now - 400, "type": "wallet_tracker", "text": "Cielo: кошелёк «whale 3» купил GOAT на 40 SOL",
         "who": "Cielo", "asset": "GOAT", "contract": ""},
        {"at": now - 200, "type": "token_stats", "text": "панель Axiom: топ-10 31%, dev 0%, бандлы 4%",
         "who": "", "asset": "GOAT", "contract": ""}]
    mr = lv.mirror.st
    for name, rets in (("exact", [-31.0, 12.5, -18.2, 22.0, -9.5]), ("smart", [12.5, 22.0, -6.0])):
        bk = mr["books"][name]
        cash = 10.0
        for r in rets:
            usd = round(cash * 0.2, 2)
            cash += usd * r / 100
        bk.update(cash=round(cash, 4), n=len(rets), wins=sum(r > 0 for r in rets), rets=rets, pnl=round(cash - 10, 4))
        mr["by"].setdefault("decu", {"name": "decu", "exact": [], "smart": []})[name] = rets
    mr["books"]["smart"]["positions"]["m9"] = {"id": "m9", "login": "decu", "name": "decu", "key": "GOAT", "asset": "GOAT",
                                              "kind": "sol", "side": 1, "usd": 2.2, "units": 1, "price0": 1, "now": 8.4,
                                              "path": [0, 8.4], "stop": 0.3, "tp": None, "at": now - 1500,
                                              "until": now + 80000, "late": None, "adds": 0, "tags": []}
    mr["books"]["smart"]["cash"] -= 2.2
    mr["skipped"] = 2
    # ⚡ пульс рынка: стратегии на бумаге и выводы
    pl = b.pulse.st
    for name, rets in (("safe", [14.0, -35.0, 22.0, 8.0, -12.0, 41.0, -35.0, 18.0, 6.0, 25.0, -9.0]),
                       ("all", [-35.0, -35.0, 12.0, -35.0, 60.0, -35.0, -20.0, 8.0, -35.0, 15.0, -35.0]),
                       ("strict", [22.0, 9.0, -14.0, 31.0])):
        bk = pl["books"][name]
        cash = 10.0
        for r in rets:
            cash += round(cash * 0.2, 2) * r / 100
        bk.update(cash=round(cash, 4), n=len(rets), wins=sum(r > 0 for r in rets), rets=rets, pnl=round(cash - 10, 4))
    pl["active"] = "safe"
    pl["avoid"] = ["bundler:hi"]
    pl["scans"] = 64
    import random as _r
    rr = _r.Random(3)
    for k in range(40):
        bad = k % 3 == 0
        path = [0.0, rr.uniform(-60, -30) if bad else rr.uniform(-20, 70)]
        pl["done"].append({"mint": f"d{k}", "symbol": f"T{k}", "at": now - 200000, "path": path, "marks": {}, "max": max(path),
                           "f": {"bundler": 0.45 if bad else 0.05, "top10": 0.6 if bad else 0.2, "src": ["trend"]}})
    for k, sym in enumerate(("GOAT", "CHILLGUY", "PNUT")):
        pl["picks"][f"p{k}"] = {"id": f"p{k}", "mint": f"p{k}", "symbol": sym, "at": now - 900 * (k + 1), "price0": 1,
                                "f": {"src": ["stretch", "smart"] if k == 0 else ["trend"]}, "path": [0.0, 12.0 - k * 9],
                                "marks": {}, "max": 18.0, "who": "", "until": now + 80000}

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
    # 💰 Профит-центр: тестовые сделки автопилота за 6 дней (доказанный плюс — видно план денег)
    for i in range(18):
        cost = 5.0
        pnl = cost * ((0.9 if i % 4 else -0.5) + rnd.uniform(-0.2, 0.2)) / 100
        b.trader.state.log.insert(0, TradeRecord(now - (18 - i) * 8 * 3600, "bybit", "sell", "SOL/USDT", False, 0.03,
                                                 "SOL", cost + pnl, "USDT", cost + pnl, pnl_usd=pnl,
                                                 source="scalp").__dict__)
    b.trader.state.day_trades = 3
    b.trader.state.day_pnl_usd = 0.042
    b.trader.state.day = b.trader._today()
    return b


async def main():
    from tests import fake_market_api as fm

    b = build()
    port = int(os.getenv("PORT", "8080"))
    fm.wire(b, f"http://127.0.0.1:{port}/fake")  # мемкоины на ненастоящих данных
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
    from bot import feed  # 📡 лента и 🧠 штурман на ненастоящих данных — чтобы видеть дизайн
    for ago_min, text in [
        (400, "🐋 Кит 7xKX…gAsU купил MOODENG на 2.1 SOL — копирую на демо"),
        (310, "♾ 🔁 перезапустил «пульс рынка GMGN/Axiom» (завис)"),
        (240, "⚙️ Треугольник USDT → ETH → BTC → USDT: +0.034 USDT (тест)"),
        (150, "🧪 Демо мемкоинов: продал PNUT +18.4% по тейку"),
        (95, "⚠️ Ключ BingX без белого списка IP — истечёт через 9 дней"),
        (40, "🤖 Автопилот: купил SOL на $5 (Откат), продажа по +0.9% стоит на бирже"),
        (12, "🧠 Консилиум 3 ИИ: «ждать» — включать деньги рано (12 ч)"),
        (3, "🎓 Benjamin Cowen: падение — BTC упирается в снижение ликвидности"),
    ]:
        feed.add(b, text, at=now - ago_min * 60)
    b._brief = {"text": "Сейчас: бот учится на демо — 18 сделок автопилота, в среднем +0.6% за сделку.\n"
                        "Риск: ключ BingX без белого списка IP истечёт через 9 дней.\n"
                        "Шаг: привяжи IP к ключу BingX, остальное работает само — /doctor",
                "at": now - 240, "model": "deepseek-v4-flash (предпросмотр)", "error": None}
    b.ai.client.endpoints = b.ai.client.endpoints or [object()]  # предпросмотр: «ИИ подключён»
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
