"""Режим $5: «ловец откатов» на одной монете. Работает с суммой от минимального ордера биржи (≈$5).

Зачем отдельный режим. Треугольник — это три ордера подряд, и Bybit требует от $5 на КАЖДЫЙ:
с $5 на счёте после первого шага (минус комиссия) третий ордер уже меньше минимума. Здесь ордеров
всего два: купить и продать, поэтому $5 хватает.

Как работает (простыми словами):
  1. Раз в несколько часов бот берёт свечи по 5 минут за последние ~3 дня по крупным монетам
     и проверяет на истории, где эта стратегия заработала бы после комиссий. Торгует только те монеты,
     где проверка в плюсе.
  2. Ждёт отката: цена резко ушла ниже средней (RSI низкий), но это не обвал.
  3. Покупает на все свободные USDT (не больше /set max_trade) — так прибыль сразу идёт в оборот
     (сложный процент).
  4. Сразу ставит на бирже лимитную продажу на +0,6…1,3% (цель выбирается по проверке на истории,
     комиссии уже учтены). Ордер стоит на бирже, даже если бот перезапустится.
  5. Если за 24 часа цель не достигнута, переставляет продажу в безубыток.
  6. Стоп-лосс (-6%) срабатывает, только если позиция стоит больше минимального ордера биржи:
     Bybit не даст продать монеты дешевле $5. С ровно $5 бот при просадке ждёт возврата цены.

Честно: это не гарантия прибыли. На падающем рынке деньги могут надолго «зависнуть» в монете.
Проверка на истории показывает, как было, а не как будет.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .trading import TradeError, TradeRecord, human_error

if TYPE_CHECKING:
    from .trading import Trader

log = logging.getLogger(__name__)

COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "TON", "SUI", "ADA", "LINK", "AVAX", "LTC", "TRX", "BNB")
TIMEFRAME = "5m"
CANDLE_SEC = 300
HISTORY_CANDLES = 1000  # ≈ 3,5 дня свечей по 5 минут: на этом проверяем стратегию
SIGNAL_CANDLES = 300  # сутки свечей: средняя и фильтр обвала за 24 ч считаются так же, как на истории
BACKTEST_EVERY_SEC = 6 * 3600
STEP_SEC = 30  # как часто проверять позицию и сигналы
MAX_SPREAD = 0.0015  # спред больше 0,15% съедает прибыль
SLIPPAGE = 0.0005  # в проверке на истории покупаем чуть хуже цены открытия свечи
TP_GRID = (0.006, 0.009, 0.013)  # цели продажи: +0,6% / +0,9% / +1,3% чистыми после комиссий
RSI_GRID = (25.0, 32.0)
EMA_LEN = 50
RSI_LEN = 14
DEV_BELOW_EMA = 0.004  # цена хотя бы на 0,4% ниже средней
CRASH_1H = -0.03  # за последний час упало больше чем на 3% — это обвал, не откат
CRASH_24H = -0.08
STOP_PCT = 0.06
MAX_HOLD_SEC = 24 * 3600
BREAKEVEN_EXTRA = 0.0005  # безубыток = комиссии + 0,05%
TOP_COINS = 4  # сколько лучших по проверке монет смотреть на каждом шаге
EVENTS_KEEP = 8


# ---------------------------------------------------------------------------
# Индикаторы и сама стратегия (чистые функции: одинаково работают на истории и вживую)
# ---------------------------------------------------------------------------


def ema(values: list[float], n: int) -> list[float]:
    if not values:
        return []
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def rsi(values: list[float], n: int = RSI_LEN) -> list[float]:
    """RSI по Уайлдеру. Первые n значений = 50 (данных ещё мало)."""
    out = [50.0] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    avg_g, avg_l = gains / n, losses / n
    for i in range(n, len(values)):
        if i > n:
            d = values[i] - values[i - 1]
            avg_g = (avg_g * (n - 1) + max(d, 0)) / n
            avg_l = (avg_l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


@dataclass
class Params:
    tp: float = TP_GRID[1]
    rsi_buy: float = 30.0

    def text(self) -> str:
        return f"цель +{self.tp * 100:.1f}%, RSI < {self.rsi_buy:g}"


def signal_at(closes: list[float], ema_v: list[float], rsi_v: list[float], i: int, p: Params) -> bool:
    """Покупать ли на закрытии свечи i: откат ниже средней при низком RSI, но не обвал."""
    if i < EMA_LEN or i >= len(closes):
        return False
    c = closes[i]
    if rsi_v[i] >= p.rsi_buy or c > ema_v[i] * (1 - DEV_BELOW_EMA):
        return False
    if c / closes[i - 12] - 1 < CRASH_1H:
        return False
    day = i - 288
    if day >= 0 and c / closes[day] - 1 < CRASH_24H:
        return False
    return True


def trade_return(entry: float, exit_: float, fee: float) -> float:
    """Доходность сделки после двух комиссий: купили по entry, продали по exit_."""
    return (1 - fee) ** 2 * exit_ / entry - 1


def target_price(entry: float, tp: float, fee: float) -> float:
    """Цена продажи, при которой после двух комиссий остаётся +tp."""
    return entry * (1 + tp) / (1 - fee) ** 2


def backtest(candles: list[list[float]], p: Params, fee: float) -> dict:
    """Проверка на истории. candles: [[время, open, high, low, close, объём], ...].

    Осторожные допущения: покупка по открытию следующей свечи чуть хуже цены, продажа по цели
    только если high выше цели; если в одной свече задеты и стоп, и цель — считаем, что стоп.
    """
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    ema_v, rsi_v = ema(closes, EMA_LEN), rsi(closes)
    n = len(candles)
    equity, trades, wins, worst, hold = 1.0, 0, 0, 0.0, 0
    open_ret = None
    i = EMA_LEN
    max_hold = MAX_HOLD_SEC // CANDLE_SEC
    while i < n - 1:
        if not signal_at(closes, ema_v, rsi_v, i, p):
            i += 1
            continue
        entry = opens[i + 1] * (1 + SLIPPAGE)
        tp_price = target_price(entry, p.tp, fee)
        be_price = target_price(entry, BREAKEVEN_EXTRA, fee)
        stop_price = entry * (1 - STOP_PCT)
        exit_, j = None, i + 1
        while j < n:
            worst = min(worst, lows[j] / entry - 1)
            target = tp_price if j - (i + 1) < max_hold else min(tp_price, be_price)
            if lows[j] <= stop_price:
                exit_ = stop_price * (1 - SLIPPAGE)
                break
            if highs[j] > target:
                exit_ = target
                break
            j += 1
        if exit_ is None:  # к концу данных позиция ещё открыта: считаем по последней цене
            open_ret = trade_return(entry, closes[-1], fee)
            equity *= 1 + open_ret
            hold += n - 1 - i
            break
        r = trade_return(entry, exit_, fee)
        equity *= 1 + r
        trades += 1
        wins += r > 0
        hold += j - i
        i = j + 1
    return {
        "trades": trades, "wins": wins, "net_pct": round((equity - 1) * 100, 3),
        "open_pct": round(open_ret * 100, 3) if open_ret is not None else None,
        "worst_pct": round(worst * 100, 2),
        "avg_hold_min": round(hold * CANDLE_SEC / 60 / max(trades + (open_ret is not None), 1)),
        "days": round(n * CANDLE_SEC / 86400, 1), "tp": p.tp, "rsi_buy": p.rsi_buy,
    }


def best_params(candles: list[list[float]], fee: float) -> dict | None:
    """Лучшие параметры из маленькой сетки (6 вариантов: чтобы не подгонять под историю)."""
    if len(candles) < EMA_LEN + 50:
        return None
    results = [backtest(candles, Params(tp, r), fee) for tp in TP_GRID for r in RSI_GRID]
    results = [x for x in results if x["trades"] >= 2]
    if not results:
        return None
    return max(results, key=lambda x: (x["net_pct"], x["trades"]))


# ---------------------------------------------------------------------------
# Живая торговля
# ---------------------------------------------------------------------------


def new_scalp_state() -> dict:
    return {"on": False, "pos": None, "paper_usd": None, "stats": {"cycles": 0, "wins": 0, "pnl": 0.0, "since": 0.0},
            "events": [], "last_buy_candle": {}}


class Scalper:
    def __init__(self, trader: Trader, duplicate_fn: Callable[[], bool] | None = None):
        self.trader = trader
        self.duplicate_fn = duplicate_fn or (lambda: False)
        self.ranking: dict[str, list[dict]] = {}  # биржа -> [результаты проверки по монетам, лучшие сверху]
        self.ranked_at: dict[str, float] = {}
        self.last_reason = ""  # что бот делал на последнем шаге и почему
        self.last_step = 0.0

    # --- состояние ----------------------------------------------------------

    @property
    def st(self) -> dict:
        raw = self.trader.state.scalp
        if not raw:
            raw.update(new_scalp_state())
        for k, v in new_scalp_state().items():
            raw.setdefault(k, v)
        return raw

    @property
    def on(self) -> bool:
        return bool(self.st["on"])

    def _event(self, text: str) -> None:
        ev = self.st["events"]
        ev.append([time.time(), text])
        del ev[:-EVENTS_KEEP]

    def _name(self, n: str) -> str:
        return self.trader.scanner.display_name(n)

    def _fee(self, name: str, symbol: str) -> float:
        m = self.trader.scanner.market(name, symbol) or {}
        return float(m.get("taker") or 0.001)

    def _min_cost(self, name: str, symbol: str, price: float | None = None) -> float:
        m = self.trader.scanner.market(name, symbol) or {}
        min_amount, min_cost = self.trader._limits(m)
        return max(min_cost, min_amount * (price or 0), 1.0 if not min_cost else 0)

    def _sellable(self, name: str, symbol: str, cost: float, price: float) -> bool:
        """Пройдёт ли потом продажа: после комиссии и округления количества вниз позиция должна остаться
        не меньше минимального ордера. У BTC шаг количества бывает ≈ $0,09, и с $5 продажа бы не прошла."""
        m = self.trader.scanner.market(name, symbol) or {}
        step = float((m.get("precision") or {}).get("amount") or 0)
        if step >= 1:  # биржа с точностью «знаков после запятой», а не шагом
            step = 10 ** -step
        fee = self._fee(name, symbol)
        coins_usd = cost * (1 - fee) - step * price  # сколько монет останется после комиссии и округления, в $ покупки
        lowest_sale = coins_usd * (1 + BREAKEVEN_EXTRA) / (1 - fee) ** 2  # самая низкая продажа — в безубыток
        return lowest_sale >= self._min_cost(name, symbol, price) * 1.001

    def exchange(self) -> str | None:
        """На какой бирже торговать: Bybit, если на ней есть ключ, иначе первая рабочая."""
        working = self.trader.working
        if not working:
            return None
        return "bybit" if "bybit" in working else working[0]

    def turn_on(self) -> None:
        st = self.st
        if not st["on"]:
            st["stats"] = {"cycles": 0, "wins": 0, "pnl": 0.0, "since": time.time()}
        st["on"] = True

    def turn_off(self) -> None:
        self.st["on"] = False

    # --- проверка на истории -----------------------------------------------

    async def _candles(self, name: str, symbol: str, limit: int) -> list[list[float]]:
        ex = self.trader.scanner.exchanges[name]
        try:
            return await ex.fetch_ohlcv(symbol, TIMEFRAME, limit=limit) or []
        except Exception as e:
            if limit > 500:
                try:
                    return await ex.fetch_ohlcv(symbol, TIMEFRAME, limit=500) or []
                except Exception:
                    pass
            log.info("Свечи %s %s: %s", name, symbol, e)
            return []

    def symbols(self, name: str) -> list[str]:
        quote = self.trader.settings.quote
        out = []
        for c in COINS:
            m = self.trader.scanner.market(name, f"{c}/{quote}")
            if m and m.get("active") is not False:
                out.append(f"{c}/{quote}")
        return out

    async def rank(self, name: str, force: bool = False) -> list[dict]:
        """Проверка стратегии на истории по всем монетам биржи. Лучшие сверху."""
        if not force and time.time() - self.ranked_at.get(name, 0) < BACKTEST_EVERY_SEC and name in self.ranking:
            return self.ranking[name]
        ex = self.trader.scanner.exchanges[name]
        await self.trader.scanner._ensure_markets(name, ex)
        sem = asyncio.Semaphore(4)

        async def one(symbol: str) -> dict | None:
            async with sem:
                candles = await self._candles(name, symbol, HISTORY_CANDLES)
            res = best_params(candles, self._fee(name, symbol))
            if res:
                res["symbol"] = symbol
            return res

        results = [r for r in await asyncio.gather(*(one(s) for s in self.symbols(name))) if r]
        results.sort(key=lambda r: -r["net_pct"])
        self.ranking[name] = results
        self.ranked_at[name] = time.time()
        return results

    def backtest_text(self, name: str) -> str:
        rows = self.ranking.get(name) or []
        title = html.escape(self._name(name))
        if not rows:
            return f"📊 Проверка на истории ({title}): нет данных. Биржа не отдала свечи, попробуй позже."
        days = rows[0]["days"]
        lines = [f"📊 <b>Проверка на истории</b>: {title}, свечи по 5 мин за {days:g} дн., комиссии учтены",
                 "<i>Монета: сделок (в плюс) · итог · худшая просадка · цель</i>"]
        for r in rows[:10]:
            icon = "🟢" if r["net_pct"] > 0 else "🔴"
            open_part = f", открыта {r['open_pct']:+.2f}%" if r["open_pct"] is not None else ""
            lines.append(f"{icon} {html.escape(r['symbol'].split('/')[0])}: {r['trades']} ({r['wins']}) · "
                         f"<b>{r['net_pct']:+.2f}%</b>{open_part} · {r['worst_pct']:.1f}% · +{r['tp'] * 100:.1f}%")
        good = [r for r in rows if r["net_pct"] > 0]
        lines.append("")
        if good:
            lines.append(f"Бот торгует только 🟢 монеты (сейчас {len(good)}). Это прошлое, а не обещание: "
                         "на другом рынке результат будет другим.")
        else:
            lines.append("Сейчас ни одна монета не в плюсе на истории: бот ждёт и не покупает. Это правильно — "
                         "лучше стоять, чем торговать в минус.")
        return "\n".join(lines)

    # --- шаг -----------------------------------------------------------------

    async def step(self, now: float | None = None) -> list[str]:
        """Один шаг: следим за позицией или ищем вход. Возвращает сообщения для владельца."""
        now = time.time() if now is None else now
        self.last_step = now
        if not self.on:
            return []
        name = self.exchange()
        if name is None:
            self.last_reason = "нет рабочего ключа биржи (/keys)"
            return []
        out: list[str] = []
        if not self.st["pos"]:
            try:  # проверка на истории — до блокировки: она долгая, а ручные ордера ждать не должны
                await self.rank(name)
            except Exception as e:
                log.info("Проверка на истории: %s", e)
        async with self.trader._lock:
            try:
                if self.st["pos"]:
                    out += await self._manage(now)
                else:
                    if self.duplicate_fn():
                        self.last_reason = ("работают две копии бота: новые покупки на паузе, "
                                            "удали лишний сервис в Render (/doctor)")
                        return out
                    out += await self._look_for_entry(name, now)
            except TradeError as e:
                self.last_reason = str(e)
            except Exception as e:
                log.exception("Ошибка режима $5")
                self.last_reason = human_error(e)
        return out

    async def _free_usd(self, name: str) -> float:
        st = self.st
        if self.trader.state.live:
            return await self.trader.free(name, self.trader.settings.quote)
        if st["paper_usd"] is None:  # тест: начинаем с реального свободного баланса (или $5, если пусто)
            try:
                real = await self.trader.free(name, self.trader.settings.quote)
            except Exception:
                real = 0.0
            st["paper_usd"] = round(real if real >= 5 else 5.0, 4)
        return float(st["paper_usd"])

    async def _look_for_entry(self, name: str, now: float) -> list[str]:
        t = self.trader
        quote = t.settings.quote
        free = await self._free_usd(name)
        cost = math.floor(min(free, t.settings.max_trade_usd) * 100 + 1e-6) / 100
        ranking = await self.rank(name)
        good = [r for r in ranking if r["net_pct"] > 0][:TOP_COINS]
        if not good:
            self.last_reason = ("жду: ни одна монета сейчас не в плюсе на проверке по истории" if ranking
                                else "биржа не отдала свечи, попробую позже")
            return []
        min_need = min(self._min_cost(name, r["symbol"]) for r in good)
        if free < min_need:
            self.last_reason = (f"мало денег на торговом счёте: {free:.2f} {quote}, биржа принимает ордер от "
                                f"${min_need:.2f}. Если деньги на Funding — переведи на торговый счёт")
            return []
        if cost < min_need:
            self.last_reason = (f"лимит на сделку ${t.settings.max_trade_usd:g} меньше минимального ордера биржи "
                                f"${min_need:.2f}: увеличь его, например <code>/set max_trade 10</code>")
            return []
        t._check_limits(cost, True, "auto")
        if t.state.day_trades + 2 > t.settings.daily_trades:
            raise TradeError(f"лимит {t.settings.daily_trades:g} сделок за сутки (/set daily_trades)")
        skipped: list[str] = []
        for r in good:
            symbol = r["symbol"]
            candles = await self._candles(name, symbol, SIGNAL_CANDLES)
            if len(candles) < EMA_LEN + 15:
                continue
            closes = [float(c[4]) for c in candles[:-1]]  # последняя свеча ещё не закрылась
            i = len(closes) - 1
            p = Params(r["tp"], r["rsi_buy"])
            candle_ts = float(candles[-2][0])
            if self.st["last_buy_candle"].get(symbol) == candle_ts:
                continue
            if not signal_at(closes, ema(closes, EMA_LEN), rsi(closes), i, p):
                continue
            ticker = await t.scanner.exchanges[name].fetch_ticker(symbol)
            bid, ask = float(ticker.get("bid") or 0), float(ticker.get("ask") or 0)
            if not bid or not ask or (ask - bid) / bid > MAX_SPREAD:
                continue
            if not self._sellable(name, symbol, cost, ask):
                skipped.append(symbol.split("/")[0])
                continue
            self.st["last_buy_candle"] = {symbol: candle_ts}
            return await self._buy(name, symbol, cost, ask, p, now)
        names = ", ".join(r["symbol"].split("/")[0] for r in good)
        self.last_reason = f"жду отката: слежу за {names} (${cost:.2f} готовы к покупке)"
        if skipped:
            self.last_reason += (f". Был откат на {', '.join(skipped)}, но с ${cost:.2f} продажу потом не пропустит "
                                 "минимум биржи (крупный шаг количества): нужно чуть больше денег")
        return []

    async def _buy(self, name: str, symbol: str, cost: float, ask: float, p: Params, now: float) -> list[str]:
        t = self.trader
        fee = self._fee(name, symbol)
        live = t.state.live
        coin = symbol.split("/")[0]
        if live:
            ex = t._ex(name)
            try:
                order = await ex.create_market_buy_order_with_cost(symbol, t._cost(ex, symbol, cost))
            except Exception as e:
                raise TradeError(f"покупка {coin} не прошла: {human_error(e)}") from e
            spent, got = await t._settle(ex, name, order, symbol, "buy", fee)
            oid = str(order.get("id"))
        else:
            spent, got, oid = cost, cost / ask * (1 - fee), None
            self.st["paper_usd"] = round(float(self.st["paper_usd"]) - cost, 6)
        entry = spent / got * (1 - fee)  # цена до комиссии: комиссию считаем в target_price
        pos = {"exchange": name, "symbol": symbol, "amount": got, "cost": spent, "entry": entry,
               "tp": target_price(entry, p.tp, fee), "tp_pct": p.tp, "fee": fee, "opened_at": now,
               "order_id": None, "live": live, "breakeven": False}
        self.st["pos"] = pos
        t._record(TradeRecord(now, name, "buy", symbol, live, spent, t.settings.quote, got, coin, spent,
                              source="scalp", order_id=oid,
                              note="режим $5: покупка на откате" + ("" if live else " (тест)")))
        mode = "" if live else "🧪 ТЕСТ: "
        tp_line = f"Продажа выставлена по {pos['tp']:.6g} (+{p.tp * 100:.1f}% после комиссий)."
        try:
            await self._place_tp(pos)
        except TradeError as e:
            tp_line = f"⚠️ {html.escape(str(e))}"
        text = (f"🎯 {mode}<b>Режим $5: купил {html.escape(coin)}</b> на {html.escape(self._name(name))}\n"
                f"${spent:.2f} → {got:.8g} {html.escape(coin)} по ≈{entry:.6g}\n" + tp_line)
        self._event(f"купил {coin} на ${spent:.2f}")
        self.last_reason = f"держу {coin}, жду цели {pos['tp']:.6g}"
        return [text]

    async def _place_tp(self, pos: dict) -> None:
        """Выставить (или переставить) лимитную продажу по цели. В тесте только запоминаем цену."""
        if not pos["live"]:
            pos["order_id"] = "paper"
            return
        t = self.trader
        ex = t._ex(pos["exchange"])
        coin = pos["symbol"].split("/")[0]
        try:
            have = await t.free(pos["exchange"], coin)
        except Exception:
            have = pos["amount"]
        amount = t._amount(ex, pos["symbol"], min(pos["amount"], have) if have > 0 else pos["amount"])
        price = t._price(ex, pos["symbol"], pos["tp"])
        try:
            order = await ex.create_order(pos["symbol"], "limit", "sell", amount, price)
        except Exception as e:
            pos["order_id"] = None
            self._event(f"не смог выставить продажу {coin}: {human_error(e)}")
            raise TradeError(f"не смог выставить продажу {coin}: {human_error(e)}. Попробую снова") from e
        pos["order_id"] = str(order.get("id"))
        pos["amount"] = amount

    async def _cancel_tp(self, pos: dict) -> None:
        if pos["live"] and pos.get("order_id"):
            try:
                await self.trader._ex(pos["exchange"]).cancel_order(pos["order_id"], pos["symbol"])
            except Exception as e:
                log.info("Отмена ордера %s: %s", pos["order_id"], e)
        pos["order_id"] = None

    async def _manage(self, now: float) -> list[str]:
        pos = self.st["pos"]
        t = self.trader
        name, symbol = pos["exchange"], pos["symbol"]
        coin = symbol.split("/")[0]
        if pos["live"] != t.state.live and not pos["live"]:
            # тестовую позицию в реальном режиме не продолжаем: закрываем её как тест
            self.st["pos"] = None
            self.last_reason = "тестовая позиция закрыта: включён реальный режим"
            return []
        if pos["live"] and not pos.get("order_id"):
            await self._place_tp(pos)
        if pos["live"]:
            o = await t._fetch_order(t._ex(name), pos["order_id"], symbol)
            status = (o or {}).get("status")
            if status == "closed":
                cost = float(o.get("cost") or 0) or float(o.get("filled") or 0) * float(o.get("average") or o.get("price") or 0)
                fees = [f for f in (o.get("fees") or [o.get("fee")]) if f and f.get("cost") is not None]
                fee_usd = sum(float(f["cost"]) for f in fees if f.get("currency") == t.settings.quote)
                got = cost - fee_usd if fees else cost * (1 - pos["fee"])
                return self._closed(got, "цель", now)
            if status in ("canceled", "cancelled", "expired", "rejected"):
                pos["order_id"] = None  # ордер сняли (например, вручную): выставим заново на следующем шаге
                self._event(f"продажа {coin} была отменена, выставляю заново")
                return []
        ticker = await t.scanner.exchanges[name].fetch_ticker(symbol)
        bid = float(ticker.get("bid") or ticker.get("last") or 0)
        if not pos["live"] and bid and bid >= pos["tp"]:
            return self._closed(pos["amount"] * pos["tp"] * (1 - pos["fee"]), "цель", now)
        if not pos["breakeven"] and now - pos["opened_at"] > MAX_HOLD_SEC:
            be = target_price(pos["entry"], BREAKEVEN_EXTRA, pos["fee"])
            if be < pos["tp"]:
                await self._cancel_tp(pos)
                pos["tp"], pos["breakeven"] = be, True
                await self._place_tp(pos)
                self._event(f"{coin}: сутки без цели, продажа переставлена в безубыток")
                return [f"⏳ Режим $5: {html.escape(coin)} держу больше суток. Переставил продажу в безубыток "
                        f"({be:.6g}), чтобы освободить деньги."]
        value = pos["amount"] * bid
        if bid and bid <= pos["entry"] * (1 - STOP_PCT):
            if value >= self._min_cost(name, symbol, bid) * 1.02:
                return await self._stop(bid, now)
            self.last_reason = (f"{coin} упал на {(1 - bid / pos['entry']) * 100:.1f}%, но продать нельзя: "
                                f"${value:.2f} меньше минимального ордера биржи. Жду возврата цены")
            return []
        change = (bid / pos["entry"] - 1) * 100 if bid else 0
        self.last_reason = f"держу {coin}: сейчас {change:+.2f}%, продажа стоит по {pos['tp']:.6g}"
        return []

    async def _stop(self, bid: float, now: float) -> list[str]:
        pos = self.st["pos"]
        t = self.trader
        if pos["live"]:
            await self._cancel_tp(pos)
            ex = t._ex(pos["exchange"])
            try:
                order = await ex.create_market_sell_order(pos["symbol"], t._amount(ex, pos["symbol"], pos["amount"]))
            except Exception as e:
                raise TradeError(f"стоп-лосс не прошёл: {human_error(e)}") from e
            _, got = await t._settle(ex, pos["exchange"], order, pos["symbol"], "sell", pos["fee"])
        else:
            got = pos["amount"] * bid * (1 - pos["fee"])
        return self._closed(got, "стоп-лосс", now)

    def _closed(self, got: float, why: str, now: float) -> list[str]:
        pos = self.st["pos"]
        t = self.trader
        coin = pos["symbol"].split("/")[0]
        pnl = got - pos["cost"]
        stats = self.st["stats"]
        stats["cycles"] += 1
        stats["wins"] += pnl > 0
        stats["pnl"] = round(stats["pnl"] + pnl, 6)
        if not pos["live"]:
            self.st["paper_usd"] = round(float(self.st["paper_usd"] or 0) + got, 6)
        t._record(TradeRecord(now, pos["exchange"], "sell", pos["symbol"], pos["live"], pos["amount"], coin, got,
                              t.settings.quote, got, pnl_usd=pnl, source="scalp",
                              note=f"режим $5: {why}" + ("" if pos["live"] else " (тест)")))
        self.st["pos"] = None
        hours = (now - pos["opened_at"]) / 3600
        icon = "✅" if pnl >= 0 else "🔻"
        mode = "" if pos["live"] else "🧪 ТЕСТ: "
        self._event(f"продал {coin}: {pnl:+.4f}$ ({why})")
        self.last_reason = "продал, ищу следующий откат"
        return [f"{icon} {mode}<b>Режим $5: продал {html.escape(coin)}</b> ({why}) через {hours:.1f} ч\n"
                f"${pos['cost']:.4f} → ${got:.4f} = <b>{pnl:+.4f}$</b> ({pnl / pos['cost'] * 100:+.2f}%)\n"
                f"Всего в режиме $5: {stats['cycles']} сделок, {stats['pnl']:+.4f}$"]

    async def sell_now(self) -> str:
        """Кнопка «Продать сейчас»: снять цель и продать по рынку (если позиция больше минимума биржи)."""
        async with self.trader._lock:
            pos = self.st["pos"]
            if not pos:
                return "Позиции нет: продавать нечего."
            t = self.trader
            ticker = await t.scanner.exchanges[pos["exchange"]].fetch_ticker(pos["symbol"])
            bid = float(ticker.get("bid") or 0)
            if pos["live"] and pos["amount"] * bid < self._min_cost(pos["exchange"], pos["symbol"], bid):
                return (f"Сейчас позиция стоит ${pos['amount'] * bid:.2f}, это меньше минимального ордера биржи. "
                        "Продать по рынку нельзя: ордер на продажу по цели остаётся стоять.")
            out = await self._stop(bid, time.time())
            return out[0].replace("стоп-лосс", "продажа вручную")

    # --- тексты -------------------------------------------------------------

    def status_text(self) -> str:
        st, t = self.st, self.trader
        live = t.state.live
        lines = ["🎯 <b>Режим $5: ловец откатов</b>",
                 "Покупает монету на откате на все свободные USDT и сразу ставит продажу на +0,6…1,3% после "
                 "комиссий. Два ордера вместо трёх, поэтому хватает $5."]
        state = ("🟢 включён" if st["on"] else "⚪️ выключен") + (", 🔴 реальные деньги" if live else ", 🧪 тест")
        lines.append(f"Состояние: <b>{state}</b>")
        name = self.exchange()
        if name:
            lines.append(f"Биржа: {html.escape(self._name(name))}")
        if not live and st["paper_usd"] is not None:
            lines.append(f"Тестовый баланс: ${float(st['paper_usd']):.2f}")
        pos = st["pos"]
        if pos:
            coin = pos["symbol"].split("/")[0]
            hours = (time.time() - pos["opened_at"]) / 3600
            be = " (безубыток)" if pos["breakeven"] else ""
            lines.append(f"Позиция: <b>{fmt(pos['amount'])} {html.escape(coin)}</b> за ${pos['cost']:.2f}, "
                         f"держу {hours:.1f} ч, продажа по {pos['tp']:.6g}{be}")
        s = st["stats"]
        if s["cycles"]:
            lines.append(f"Итог с включения: {s['cycles']} сделок, в плюс {s['wins']}, <b>{s['pnl']:+.4f}$</b>")
        if self.last_reason:
            lines.append(f"Сейчас: {html.escape(self.last_reason)}")
        if st["events"]:
            lines.append("\nПоследнее:")
            for at, text in st["events"][-4:]:
                lines.append(f"• {time.strftime('%d.%m %H:%M', time.gmtime(at + t.settings.report_utc_offset * 3600))} "
                             f"{html.escape(text)}")
        lines.append("\nЧестно: прибыль не гарантирована. На падении деньги могут зависнуть в монете, "
                     "пока цена не вернётся. Ставь только то, что не жалко.")
        return "\n".join(lines)

    def status_dict(self) -> dict:
        st = self.st
        pos = st["pos"]
        return {"on": st["on"], "live": self.trader.state.live, "reason": self.last_reason,
                "paper_usd": st["paper_usd"], "stats": dict(st["stats"]),
                "pos": {k: pos[k] for k in ("symbol", "amount", "cost", "entry", "tp", "opened_at", "breakeven")} if pos else None,
                "top": [{k: r[k] for k in ("symbol", "trades", "wins", "net_pct", "worst_pct", "tp")}
                        for r in (self.ranking.get(self.exchange() or "") or [])[:5]]}


def fmt(x: float) -> str:
    return f"{x:.8g}"
