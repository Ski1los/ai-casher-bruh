"""🤖 Торговля по новостям (v48): новость → поиск токена → проверки → сделка. Сначала демо, потом по-настоящему.

Как это работает:
1. 📰 Модуль новостей (bot/news.py) нашёл свежую хорошую новость (листинг на крупной бирже, покупка в резерв,
   приток в ETF по монете…). Ожидаемый ход берётся из обучения: стартовая оценка, которую поправляют замеры цены.
2. 🔎 Поиск токена: тикеры и названия из заголовка, монеты, которые назвал ИИ, и поиск названия проекта в
   CoinGecko (бесплатно, без ключа). Подходит только монета с парой к USDT на Bybit или BingX.
3. ✅ Проверки перед покупкой — если хоть одна не прошла, бот не покупает и пишет почему:
   • новость свежая (до 30 мин) и по фактам такие новости дают рост ≥ 1.5% за 4 ч;
   • монета не под щитом (взлом, делистинг…), нет паузы (шторм, данные США);
   • оборот от $300 000 за сутки, разница цен покупки и продажи до 0.6%;
   • «не опоздали»: если с момента новости цена уже прошла 70% ожидаемого хода — не догоняем; если упала на
     1.5% — рынок новости не поверил, тоже не берём.
4. 💼 Сделка: цель — ожидаемый ход (1.5–8%), стоп −2.5%, после +1.5% стоп в безубыток, после +2% стоп тянется за
   ценой, через 4 ч продажа в любом случае. Плохая новость по монете — продажа сразу.
5. 🎓 Демо → реал. Каждая сделка учит бота: итоги по типам новостей. Вживую торгуются только типы, которые прошли
   экзамен на демо (≥ 8 сделок, в среднем ≥ +0.5% после комиссий, в плюс ≥ 50%), и только если владелец включил
   реальный режим (/newstrade real + подтверждение) и бот в /live. Остальные типы продолжают учиться на демо.
   Две реальные сделки подряд в минус или дневной лимит убытка — бот сам возвращается в демо. /stop — тоже.

Состояние — в copy.st["newstrade"] (сохраняется и бэкапится).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from typing import TYPE_CHECKING

import aiohttp

from .news import CATS, COIN_SKIP, STABLES
from .trading import TradeError, TradeRecord, human_error

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

FAST_SEC = 20                  # как часто проверяем открытые сделки
FRESH_SEC = 30 * 60
ENTRY_MIN = 1.5                # ожидаемый рост за 4 ч, %
MIN_VOL = 300_000
MAX_SPREAD = 0.6               # %
LATE_SHARE = 0.7               # прошло 70% ожидаемого хода — опоздали
DISBELIEF = -1.5               # упала после хорошей новости — рынок не поверил
TP_MIN, TP_MAX = 1.5, 8.0
SL_PCT = 2.5
BE_AT = 1.5
TRAIL_AT = 2.0
TRAIL_GAP = 1.2
HOLD_SEC = 4 * 3600
MAX_OPEN = 3
DAY_MAX = 15
DEMO_START = 10.0
REAL_CAP = 10.0
GRAD_N = 8
GRAD_AVG = 0.5
GRAD_WIN = 0.5
LOSS_STREAK = 2
CLOSED_KEEP = 60
SKIPS_KEEP = 12
CG_URL = "https://api.coingecko.com/api/v3/search"
CG_CACHE_SEC = 7 * 86400
CG_PER_TICK = 4
CG_MAX_RANK = 2000
NAME_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]{2,20}\b")
NAME_STOP = {
    "The", "And", "For", "With", "From", "Into", "After", "Amid", "Over", "Will", "Lists", "List", "Listing", "Launch",
    "Launches", "New", "Says", "Said", "Why", "How", "What", "When", "This", "That", "Its", "Has", "Have", "Price",
    "Token", "Tokens", "Coin", "Coins", "Crypto", "Market", "Markets", "Network", "Protocol", "Foundation", "Labs",
    "Exchange", "Trading", "Spot", "Futures", "Perpetual", "Support", "Adds", "Add", "Seed", "Tag", "Pairs", "Pair",
    "Binance", "Coinbase", "Upbit", "Bithumb", "OKX", "Bybit", "BingX", "Kraken", "Robinhood", "Gemini", "Bitget",
    "SEC", "CFTC", "Fed", "Federal", "Reserve", "Trump", "Musk", "Bitcoin", "Ethereum", "Ether", "ETF", "ETFs", "USD",
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "January", "February", "March",
    "April", "May", "June", "July", "August", "September", "October", "November", "December", "Today", "Week",
    "Report", "Data", "Fund", "Funds", "Inflows", "Record", "Buys", "Bought", "Treasury", "Million", "Billion",
    "Partnership", "Partners", "Integration", "Mainnet", "Upgrade", "Airdrop", "Wallet", "Bank", "Banks", "Firm",
    "Company", "Group", "Global", "Asia", "Korea", "Korean", "Japan", "China", "Europe", "America", "American",
}


def new_state() -> dict:
    return {"on": True, "real": False, "cash": DEMO_START, "open": [], "closed": [], "cats": {}, "streak": 0,
            "day": ["", 0], "skips": [], "found": 0, "checked": 0, "reverted": "", "done": []}


def name_candidates(title: str) -> list[str]:
    """Слова с большой буквы, похожие на название проекта («Walrus», «Hyperliquid»), — их ищем в CoinGecko."""
    out = []
    for w in NAME_RE.findall(title):
        if w in NAME_STOP or w.upper() == w and len(w) <= 5:
            continue  # тикеры (ВСЕ ЗАГЛАВНЫЕ) уже нашёл разбор новостей
        out.append(w)
    return list(dict.fromkeys(out))[:3]


class NewsTrader:
    def __init__(self, b: ArbitrageBot) -> None:
        self.b = b
        self._session: aiohttp.ClientSession | None = None
        self._cg: dict[str, tuple[float, str | None]] = {}  # название → (когда искали, тикер или None)
        self._lock = asyncio.Lock()

    # --- состояние ---------------------------------------------------------------------------------

    @property
    def st(self) -> dict:
        cs = self.b.copy.st
        st = cs.get("newstrade")
        if not isinstance(st, dict):
            st = cs["newstrade"] = {}
        for k, v in new_state().items():
            if k not in st:
                st[k] = type(v)(v) if isinstance(v, (list, dict)) else v
        return st

    def _changed(self) -> None:
        try:
            self.b.copy.changes += 1
        except AttributeError:
            pass

    def name(self, ex: str) -> str:
        return self.b.scanner.display_name(ex)

    def when(self, ts: float) -> str:
        return self.b.news.when(ts)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def on_panic(self) -> None:
        """/stop: реальные сделки по новостям выключаются; открытые позиции ведутся до стопа или цели."""
        if self.st["real"]:
            self.st["real"] = False
            self.st["reverted"] = "ты нажал /stop"
            self._changed()

    # --- экзамен: какие типы новостей можно торговать вживую ------------------------------------------

    def cat_stats(self, cat: str) -> tuple[int, float, float]:
        n, s, w = self.st["cats"].get(cat, [0, 0.0, 0])
        return int(n), (s / n if n else 0.0), (w / n if n else 0.0)

    def graduated(self, cat: str) -> bool:
        n, avg, win = self.cat_stats(cat)
        return n >= GRAD_N and avg >= GRAD_AVG and win >= GRAD_WIN

    def live_ready(self) -> str | None:
        """Почему реальные сделки сейчас невозможны (None — можно)."""
        t = self.b.trader
        if not self.st["real"]:
            return "реальный режим торговли по новостям выключен (/newstrade real)"
        if not t.state.live:
            return "бот в тестовом режиме (/live)"
        t._roll_day()
        if t.state.day_pnl_usd <= -self.b.settings.daily_loss_usd:
            return f"дневной убыток достиг лимита ${self.b.settings.daily_loss_usd:g}"
        return None

    def real_problem(self) -> str | None:
        """Что мешает включить реальный режим (для кнопки)."""
        t = self.b.trader
        if not any(n in t.creds and t.key_status.get(n, "ok") == "ok" for n in self.b.scanner.exchanges):
            return "нужен рабочий API-ключ Bybit или BingX (/keys)"
        if not t.state.live:
            return "сначала включи реальный режим бота: /live"
        if not any(self.graduated(c) for c in CATS):
            return (f"ни один тип новостей ещё не сдал экзамен на демо (≥ {GRAD_N} сделок, в среднем ≥ +{GRAD_AVG}% "
                    f"после комиссий, в плюс ≥ {GRAD_WIN * 100:.0f}%). Пусть демо поработает")
        return None

    # --- поиск токена --------------------------------------------------------------------------------

    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        return self._session

    async def coingecko(self, word: str, now: float) -> str | None:
        hit = self._cg.get(word.lower())
        if hit and now - hit[0] < CG_CACHE_SEC:
            return hit[1]
        headers = {}
        key = os.getenv("COINGECKO_API_KEY", "").strip()
        if key:
            headers["x-cg-demo-api-key"] = key
        found = None
        try:
            async with self._s().get(CG_URL, params={"query": word}, headers=headers) as r:
                if r.status != 200:
                    raise RuntimeError(f"ответ {r.status}")
                data = await r.json(content_type=None)
            for c in (data or {}).get("coins") or []:
                rank = c.get("market_cap_rank")
                if not rank or rank > CG_MAX_RANK:
                    continue
                if str(c.get("name") or "").lower() == word.lower() or str(c.get("symbol") or "").upper() == word.upper():
                    found = str(c.get("symbol") or "").upper() or None
                    break
        except Exception as e:
            log.info("CoinGecko «%s»: %s", word, e)
            return None  # не кэшируем ошибку сети
        self._cg[word.lower()] = (now, found)
        if len(self._cg) > 500:
            self._cg = dict(list(self._cg.items())[-300:])
        return found

    def markets_for(self, coin: str) -> list[str]:
        sym = f"{coin}/{self.b.settings.quote}"
        out = []
        for name, ex in self.b.scanner.exchanges.items():
            m = (getattr(ex, "markets", None) or {}).get(sym)
            if m and m.get("active") is not False and m.get("spot", True):
                out.append(name)
        return out

    async def find_tokens(self, e: dict, now: float, budget: list[int]) -> list[str]:
        """Монеты новости, которые можно купить на наших биржах: из разбора, ИИ и поиска названия в CoinGecko."""
        coins = [c for c in e["coins"] if c not in STABLES and c not in COIN_SKIP]
        if not coins and CATS.get(e["cat"], ("", "", 0, ""))[3] == "coin":  # «ФРС снизила ставку» — не про токен
            for word in name_candidates(e["title"]):
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                sym = await self.coingecko(word, now)
                if sym and sym not in STABLES and sym not in COIN_SKIP and sym not in coins:
                    coins.append(sym)
                    if sym not in e["coins"] and len(e["coins"]) < 4:
                        e["coins"].append(sym)  # пусть и модуль новостей мерит влияние на эту монету
        return [c for c in coins if self.markets_for(c)][:2]

    # --- проверки и вход -----------------------------------------------------------------------------

    def _skip(self, e: dict, coin: str, why: str, now: float) -> None:
        self.st["skips"].append({"at": now, "coin": coin, "why": why[:140], "title": e["title"][:90]})
        del self.st["skips"][:-SKIPS_KEEP]

    async def _ticker(self, ex_name: str, coin: str) -> dict | None:
        from .extras_ui import _ticker
        return await _ticker(self.b, ex_name, coin)

    async def _move_since(self, ex_name: str, coin: str, start: float, price: float) -> float | None:
        """Ход цены с момента новости, % (по 5-минутным свечам)."""
        ex = self.b.scanner.exchanges.get(ex_name)
        if ex is None:
            return None
        try:
            rows = await asyncio.wait_for(ex.fetch_ohlcv(f"{coin}/{self.b.settings.quote}", "5m", limit=48), 15)
        except Exception as e:
            log.info("Новости-торговля: свечи %s: %s", coin, e)
            return None
        rows = [r for r in rows or [] if r and r[1]]
        if not rows:
            return None
        start_ms = start * 1000
        before = [r for r in rows if r[0] <= start_ms]
        base = float(before[-1][1]) if before else float(rows[0][1])
        return (price / base - 1) * 100 if base else None

    async def check(self, e: dict, coin: str, now: float) -> tuple[dict | None, str]:
        """Все проверки перед покупкой. (план, почему нет)."""
        nw = self.b.news
        val = nw.est(e["cat"], "4h")[0] if e["cat"] in CATS else 0.0
        start = min(e["t"], e["seen"])
        if now - start > FRESH_SEC:
            return None, f"новость старше {FRESH_SEC // 60} мин — цена уже отреагировала"
        if val < ENTRY_MIN:
            return None, f"по фактам такие новости дают {val:+.1f}% за 4 ч — меньше порога +{ENTRY_MIN}%"
        shield = self.b.scalper.shield_reason(coin, now) if hasattr(self.b.scalper, "shield_reason") else None
        if shield:
            return None, f"под щитом: {shield}"
        pause = nw.pause(now)
        if pause:
            return None, f"пауза: {pause[1]}"
        if any(p["coin"] == coin for p in self.st["open"]):
            return None, "по этой монете уже есть сделка"
        best = None
        for ex_name in self.markets_for(coin):
            t = await self._ticker(ex_name, coin)
            if not t or not t.get("bid") or not t.get("ask"):
                continue
            vol = float(t.get("quoteVolume") or 0) or float(t.get("baseVolume") or 0) * float(t.get("last") or 0)
            if best is None or vol > best[1]:
                best = (ex_name, vol, float(t["bid"]), float(t["ask"]))
        if best is None:
            return None, "биржи не отдали цену"
        ex_name, vol, bid, ask = best
        if vol < MIN_VOL:
            return None, f"оборот ${vol:,.0f} за сутки — меньше ${MIN_VOL:,.0f}: тонкий рынок".replace(",", " ")
        spread = (ask / bid - 1) * 100
        if spread > MAX_SPREAD:
            return None, f"разница цен покупки и продажи {spread:.2f}% — больше {MAX_SPREAD}%"
        moved = await self._move_since(ex_name, coin, start, (bid + ask) / 2)
        if moved is None:
            return None, "нет свечей — не могу проверить, не опоздали ли"
        if moved >= max(1.0, val * LATE_SHARE):
            return None, f"опоздали: с момента новости уже {moved:+.1f}% из ожидаемых {val:+.1f}%"
        if moved <= DISBELIEF:
            return None, f"рынок не поверил: после новости {moved:+.1f}%"
        tp = max(TP_MIN, min(TP_MAX, val - max(0.0, moved)))
        return {"ex": ex_name, "bid": bid, "ask": ask, "vol": vol, "est": val, "moved": moved, "tp": tp}, ""

    def _size(self, live: bool, free: float) -> float:
        s = self.b.settings
        if live:
            return round(min(free, s.max_trade_usd, REAL_CAP), 2)
        st = self.st
        return round(min(float(st["cash"]), s.max_trade_usd, max(2.0, DEMO_START / MAX_OPEN)), 2)

    async def open(self, e: dict, coin: str, plan: dict, now: float) -> str:
        st = self.st
        t = self.b.trader
        ex_name = plan["ex"]
        symbol = f"{coin}/{self.b.settings.quote}"
        fee = self.b.scalper._fee(ex_name, symbol)
        live = (self.live_ready() is None and self.graduated(e["cat"]) and ex_name in t.creds
                and t.key_status.get(ex_name, "ok") == "ok")
        free = await t.free(ex_name, self.b.settings.quote) if live else 0.0
        cost = self._size(live, free)
        min_cost = self.b.scalper._min_cost(ex_name, symbol, plan["ask"])
        if cost < max(1.0, min_cost * 1.05):
            raise TradeError(f"на сделку ${cost:.2f}, а биржа принимает от ${min_cost:.2f}"
                             + ("" if live else " (демо-деньги кончились: /newstrade reset)"))
        oid = None
        if live:
            t._check_limits(cost, True, "auto")
            ex = t._ex(ex_name)
            try:
                order = await ex.create_market_buy_order_with_cost(symbol, t._cost(ex, symbol, cost))
            except Exception as err:
                raise TradeError(f"покупка {coin} не прошла: {human_error(err)}") from err
            spent, got = await t._settle(ex, ex_name, order, symbol, "buy", fee)
            oid = str(order.get("id"))
        else:
            spent, got = cost, cost / plan["ask"] * (1 - fee)
            st["cash"] = round(float(st["cash"]) - cost, 6)
        entry = spent / got if got else plan["ask"]
        pos = {"id": f"{e['id']}:{coin}", "event": e["id"], "coin": coin, "symbol": symbol, "ex": ex_name,
               "cat": e["cat"], "title": (e.get("ru") or e["title"])[:120], "live": live, "cost": spent, "amount": got,
               "entry": entry, "fee": fee, "opened": now, "tp": entry * (1 + plan["tp"] / 100),
               "sl": entry * (1 - SL_PCT / 100), "peak": entry, "be": False, "tp_pct": plan["tp"]}
        st["open"].append(pos)
        st["day"][1] += 1
        t._record(TradeRecord(now, ex_name, "buy", symbol, live, spent, self.b.settings.quote, got, coin, spent,
                              source="news", order_id=oid,
                              note=f"новость: {CATS[e['cat']][1]}" + ("" if live else " (демо)")))
        self._changed()
        mode = "💵 " if live else "🧪 ДЕМО: "
        return (f"🤖 {mode}<b>Купил {html.escape(coin)} по новости</b> на {html.escape(self.name(ex_name))}\n"
                f"{CATS[e['cat']][0]} {html.escape(pos['title'])}\n"
                f"${spent:.2f} по ≈{entry:.6g} · цель +{plan['tp']:.1f}% · стоп −{SL_PCT}% · максимум 4 ч\n"
                f"<i>Почему: такие новости дают в среднем {plan['est']:+.1f}% за 4 ч, после новости прошло "
                f"{plan['moved']:+.1f}%, оборот ${plan['vol'] / 1e6:.1f} млн</i>")

    async def process(self, new: list[dict], now: float | None = None) -> list[str]:
        """Свежие новости из модуля новостей → поиск токенов → проверки → сделки."""
        now = time.time() if now is None else now
        st = self.st
        if not st["on"] or not self.b.news.st["on"]:
            return []
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if st["day"][0] != day:
            st["day"] = [day, 0]
        msgs: list[str] = []
        budget = [CG_PER_TICK]
        async with self._lock:
            for e in new:
                if e.get("old") or e["cat"] not in CATS or self.b.news.est(e["cat"], "4h")[0] < ENTRY_MIN:
                    continue  # плохие и слабые новости не торгуем (плохие защищает щит)
                if e["id"] in st["done"]:
                    continue  # ту же историю (её пересказ в другом источнике) второй раз не торгуем
                st["done"].append(e["id"])
                del st["done"][:-200]
                coins = await self.find_tokens(e, now, budget)
                if not coins:
                    continue
                st["found"] += len(coins)
                for coin in coins:
                    if len(st["open"]) >= MAX_OPEN:
                        self._skip(e, coin, f"уже открыто {MAX_OPEN} сделки по новостям", now)
                        break
                    if st["day"][1] >= DAY_MAX:
                        self._skip(e, coin, f"лимит {DAY_MAX} сделок по новостям в сутки", now)
                        break
                    st["checked"] += 1
                    try:
                        plan, why = await self.check(e, coin, now)
                        if plan is None:
                            self._skip(e, coin, why, now)
                            continue
                        msgs.append(await self.open(e, coin, plan, now))
                    except TradeError as err:
                        self._skip(e, coin, str(err), now)
                    except Exception as err:
                        log.exception("Торговля по новостям, %s", coin)
                        self._skip(e, coin, f"ошибка: {type(err).__name__}", now)
            self._changed()
        return msgs

    # --- ведение сделок ------------------------------------------------------------------------------

    async def manage(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        st = self.st
        if not st["open"]:
            return []
        msgs: list[str] = []
        async with self._lock:
            risk = self.b.news.risk(now)
            for pos in list(st["open"]):
                try:
                    t = await self._ticker(pos["ex"], pos["coin"])
                    bid = float((t or {}).get("bid") or 0)
                    if not bid:
                        continue
                    pos["peak"] = max(pos["peak"], bid)
                    gain = (bid / pos["entry"] - 1) * 100
                    if not pos["be"] and gain >= BE_AT:
                        pos["be"] = True
                        pos["sl"] = max(pos["sl"], pos["entry"] * (1 + 2 * pos["fee"]))
                    if (pos["peak"] / pos["entry"] - 1) * 100 >= TRAIL_AT:
                        pos["sl"] = max(pos["sl"], pos["peak"] * (1 - TRAIL_GAP / 100))
                    why = ("цель" if bid >= pos["tp"] else "стоп" if bid <= pos["sl"] else
                           "4 ч прошло" if now - pos["opened"] >= HOLD_SEC else
                           "плохая новость по монете" if pos["coin"] in risk else "")
                    if why == "стоп" and pos["be"]:
                        why = "защита прибыли" if bid > pos["entry"] * (1 + 2 * pos["fee"]) else "безубыток"
                    if why:
                        msgs.append(await self._close(pos, bid, why, now))
                except TradeError as err:
                    pos["fails"] = pos.get("fails", 0) + 1
                    if pos["fails"] in (1, 15) or pos["fails"] % 180 == 0:  # не спамить каждые 20 с
                        msgs.append(f"⚠️ Торговля по новостям: {html.escape(pos['coin'])} — {html.escape(str(err))}. "
                                    "Пробую снова каждые 20 с; продать вручную: /newstrade sell")
                except Exception:
                    log.exception("Торговля по новостям: ведение %s", pos.get("coin"))
            self._changed()
        return msgs

    async def _close(self, pos: dict, bid: float, why: str, now: float) -> str:
        st = self.st
        t = self.b.trader
        if pos["live"]:
            ex = t._ex(pos["ex"])
            have = await t.free(pos["ex"], pos["coin"])
            amount = min(pos["amount"], have) if have else pos["amount"]
            try:
                order = await ex.create_market_sell_order(pos["symbol"], t._amount(ex, pos["symbol"], amount))
            except Exception as err:
                raise TradeError(f"продажа не прошла: {human_error(err)}") from err
            _, got = await t._settle(ex, pos["ex"], order, pos["symbol"], "sell", pos["fee"])
        else:
            got = pos["amount"] * bid * (1 - pos["fee"])
            st["cash"] = round(float(st["cash"]) + got, 6)
        pnl = got - pos["cost"]
        pct = pnl / pos["cost"] * 100 if pos["cost"] else 0.0
        st["open"] = [p for p in st["open"] if p["id"] != pos["id"]]
        st["closed"].append({**{k: pos[k] for k in ("coin", "ex", "cat", "title", "live", "cost", "opened")},
                             "got": round(got, 6), "pnl": round(pnl, 6), "pct": round(pct, 3), "why": why,
                             "closed": now})
        del st["closed"][:-CLOSED_KEEP]
        if not pos["live"]:  # экзамен сдаётся только на демо: одинаковые условия для всех типов новостей
            n, s, w = st["cats"].get(pos["cat"], [0, 0.0, 0])
            st["cats"][pos["cat"]] = [n + 1, round(s + pct, 4), w + (1 if pct > 0 else 0)]
        t._record(TradeRecord(now, pos["ex"], "sell", pos["symbol"], pos["live"], pos["amount"], pos["coin"], got,
                              self.b.settings.quote, got, pnl_usd=pnl, source="news",
                              note=f"новость: {why}" + ("" if pos["live"] else " (демо)")))
        note = ""
        if pos["live"]:
            st["streak"] = st["streak"] + 1 if pnl < 0 else 0
            reason = None
            t._roll_day()
            if st["streak"] >= LOSS_STREAK:
                reason = f"{LOSS_STREAK} реальные сделки подряд в минус"
            elif t.state.day_pnl_usd <= -self.b.settings.daily_loss_usd:
                reason = f"дневной убыток достиг лимита ${self.b.settings.daily_loss_usd:g}"
            if reason:
                st["streak"] = 0
                if st["real"]:
                    st["real"] = False
                    st["reverted"] = reason
                    note = (f"\n🛡 Вернул торговлю по новостям в демо: {html.escape(reason)}. "
                            "Включить снова: /newstrade real")
        icon = "✅" if pnl >= 0 else "🔻"
        mode = "💵 " if pos["live"] else "🧪 ДЕМО: "
        hours = (now - pos["opened"]) / 3600
        return (f"{icon} {mode}<b>Продал {html.escape(pos['coin'])}</b> ({why}) через {hours:.1f} ч: "
                f"${pos['cost']:.2f} → ${got:.2f} = <b>{pnl:+.2f}$</b> ({pct:+.2f}%)\n"
                f"<i>{html.escape(pos['title'][:100])}</i>" + note)

    async def sell_all(self, why: str = "вручную") -> list[str]:
        out = []
        async with self._lock:
            for pos in list(self.st["open"]):
                t = await self._ticker(pos["ex"], pos["coin"])
                bid = float((t or {}).get("bid") or 0) or pos["entry"]
                try:
                    out.append(await self._close(pos, bid, why, time.time()))
                except TradeError as err:
                    out.append(f"⚠️ {html.escape(pos['coin'])}: {html.escape(str(err))}")
            self._changed()
        return out

    def reset_demo(self) -> None:
        st = self.st
        st["cash"] = DEMO_START
        st["open"] = [p for p in st["open"] if p["live"]]
        st["closed"] = [c for c in st["closed"] if c["live"]]
        st["cats"] = {}
        self._changed()

    # --- тексты --------------------------------------------------------------------------------------

    def totals(self, live: bool) -> tuple[int, int, float]:
        rows = [c for c in self.st["closed"] if bool(c["live"]) == live]
        return len(rows), sum(1 for c in rows if c["pnl"] > 0), sum(c["pnl"] for c in rows)

    def equity(self) -> float:
        st = self.st
        return float(st["cash"]) + sum(p["cost"] for p in st["open"] if not p["live"])

    def mode_text(self) -> str:
        st = self.st
        if not st["on"]:
            return "⏸ выключена"
        if st["real"]:
            why = self.live_ready()
            return ("💵 <b>реальный режим</b>: вживую — только типы новостей, сдавшие экзамен, остальные на демо"
                    if why is None else f"💵 реальный режим включён, но сейчас демо: {why}")
        return "🧪 <b>демо</b>: виртуальные деньги, настоящие цены"

    def panel_text(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        st = self.st
        n, w, pnl = self.totals(False)
        lines = ["🤖 <b>Торговля по новостям</b>",
                 "Хорошая свежая новость → бот ищет токен на Bybit/BingX → проверяет, не опоздали ли → покупает с целью, "
                 "стопом и выходом через 4 ч.",
                 "", f"Режим: {self.mode_text()}",
                 f"🧪 Демо-счёт: <b>${self.equity():.2f}</b> из ${DEMO_START:g} · сделок {n}, в плюс {w}, итог {pnl:+.2f}$"]
        rn, rw, rpnl = self.totals(True)
        if rn or st["real"]:
            lines.append(f"💵 Реальные: сделок {rn}, в плюс {rw}, итог {rpnl:+.2f}$")
        if st.get("reverted") and not st["real"]:
            lines.append(f"🛡 Последний возврат в демо: {html.escape(st['reverted'])}")
        if st["open"]:
            lines.append("\n<b>Сейчас открыто:</b>")
            for p in st["open"]:
                lines.append(f"• {'💵' if p['live'] else '🧪'} <b>{html.escape(p['coin'])}</b> на {html.escape(self.name(p['ex']))}"
                             f": ${p['cost']:.2f} по {p['entry']:.6g}, цель {p['tp']:.6g}, стоп {p['sl']:.6g}, "
                             f"до {self.when(p['opened'] + HOLD_SEC)}")
        exam = [(c, *self.cat_stats(c)) for c in CATS if self.cat_stats(c)[0]]
        if exam:
            lines.append("\n🎓 <b>Экзамен на демо</b> (вживую — после ≥ "
                         f"{GRAD_N} сделок, в среднем ≥ +{GRAD_AVG}%, в плюс ≥ {GRAD_WIN * 100:.0f}%):")
            for c, cn, avg, win in sorted(exam, key=lambda x: -x[1])[:6]:
                mark = "✅ сдал" if self.graduated(c) else f"{cn}/{GRAD_N}"
                lines.append(f"• {CATS[c][0]} {CATS[c][1]}: {cn} сделок, в среднем {avg:+.2f}%, в плюс {win * 100:.0f}%"
                             f" — {mark}")
        if st["closed"]:
            lines.append("\n<b>Последние сделки:</b>")
            for c in st["closed"][-4:][::-1]:
                lines.append(f"• {'💵' if c['live'] else '🧪'} {html.escape(c['coin'])} {c['pct']:+.2f}% ({html.escape(c['why'])})")
        if st["skips"]:
            lines.append("\n<b>Почему не купил</b> (последние):")
            for s in st["skips"][-4:][::-1]:
                lines.append(f"• {html.escape(s['coin'])}: {html.escape(s['why'])}")
        lines.append(f"\nНайдено токенов в новостях: {st['found']}, проверено: {st['checked']}")
        lines.append("<i>Честно: новости доходят до бота за минуты, и часть движения к этому времени уже прошла. Поэтому "
                     "бот не догоняет цену, а вживую торгует только то, что доказало плюс на демо. Гарантий нет.</i>")
        return "\n".join(lines)

    def status_dict(self) -> dict:
        st = self.st
        n, w, pnl = self.totals(False)
        rn, rw, rpnl = self.totals(True)
        return {"on": st["on"], "real": st["real"], "live_now": self.live_ready() is None,
                "live_why": self.live_ready(), "mode": re.sub(r"<[^>]+>", "", self.mode_text()),
                "equity": round(self.equity(), 4), "start": DEMO_START,
                "demo": {"n": n, "wins": w, "pnl": round(pnl, 4)}, "real_totals": {"n": rn, "wins": rw, "pnl": round(rpnl, 4)},
                "open": [{"coin": p["coin"], "ex": self.name(p["ex"]), "live": p["live"], "cost": p["cost"],
                          "entry": p["entry"], "tp": p["tp"], "sl": p["sl"], "until": p["opened"] + HOLD_SEC,
                          "title": p["title"]} for p in st["open"]],
                "closed": [{"coin": c["coin"], "pct": c["pct"], "pnl": c["pnl"], "why": c["why"], "live": c["live"],
                            "closed": c["closed"]} for c in st["closed"][-6:][::-1]],
                "exam": [{"cat": c, "name": CATS[c][1], "icon": CATS[c][0], "n": self.cat_stats(c)[0],
                          "avg": round(self.cat_stats(c)[1], 3), "win": round(self.cat_stats(c)[2], 3),
                          "passed": self.graduated(c)} for c in CATS if self.cat_stats(c)[0]],
                "skips": [{"coin": s["coin"], "why": s["why"]} for s in st["skips"][-5:][::-1]],
                "found": st["found"], "checked": st["checked"], "reverted": st.get("reverted", ""),
                "grad": {"n": GRAD_N, "avg": GRAD_AVG, "win": GRAD_WIN}}


async def newstrade_loop(b: ArbitrageBot) -> None:
    """Быстрый цикл: открытые сделки по новостям проверяются каждые 20 с (новые ищет цикл новостей)."""
    await asyncio.sleep(30)
    while True:
        b._beats["newstrade"] = time.time()
        try:
            for text in await b.newstrade.manage():
                await b.notify(text)
        except Exception:
            log.exception("Ошибка торговли по новостям")
        await b.traffic.sleep(FAST_SEC, cap=2)  # открытые сделки — не реже чем раз в 40 с
