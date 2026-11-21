"""Режим $5 (автопилот): одна сделка за раз на каждой бирже. Работает с суммой от минимального ордера биржи (≈$5).

Зачем отдельный режим. Треугольник — это три ордера подряд, и Bybit требует от $5 на КАЖДЫЙ:
с $5 на счёте после первого шага (минус комиссия) третий ордер уже меньше минимума. Здесь ордеров
всего два: купить и продать, поэтому $5 хватает.

Как работает (простыми словами):
  1. Раз в 6 часов бот берёт свечи по 5 минут за последние ~3,5 дня по 13 крупным монетам и для каждой
     перебирает 3 стратегии: «Откат» (резко ниже средней, RSI низкий), «Отскок» (разворот от нижней
     полосы Боллинджера) и «Тренд» (откат к средней в растущем рынке). Вариант выбирается на первых ~2/3
     истории и проверяется на последней трети, которую он «не видел» (walk-forward). Торгуются только
     монеты, где вариант в плюсе и не в минусе на новых данных — так меньше самообмана.
  2. Ждёт сигнала своей стратегии. Обвал (−3% за час, −8% за сутки) — не сигнал. Если BTC упал больше
     чем на 2% за час, новых покупок нет вообще («тормоз рынка»).
  3. Покупает на все свободные USDT (не больше /set max_trade) — так прибыль сразу идёт в оборот
     (сложный процент).
  4. Сразу ставит на бирже лимитную продажу на +0,6…1,3% (цель выбирается по проверке на истории,
     комиссии уже учтены). Ордер стоит на бирже, даже если бот перезапустится.
  5. Если за 24 часа цель не достигнута, переставляет продажу в безубыток.
  6. Стоп-лосс (-6%) срабатывает, только если позиция стоит больше минимального ордера биржи:
     Bybit не даст продать монеты дешевле $5. С ровно $5 бот при просадке ждёт возврата цены.

«Монеты в дело» (по желанию): если USDT мало, а на спот-счёте лежит монета дороже минимального ордера
(например, SOL), бот продаёт её по рынку в USDT, чтобы торговать этими деньгами.

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
SIGNAL_CANDLES = 500  # ≈1,7 суток свечей: средние (в т.ч. длинная на 200 свечей) и фильтр обвала как на истории
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
TOP_COINS = 5  # сколько лучших по проверке монет смотреть на каждом шаге
TRAIN_SHARE = 0.65  # на этой доле истории выбираем вариант, на остальной — проверяем его «вслепую»
# Порог «монета годится». Проверено на 300 случайных графиках (чистый шум, заработать на нём нельзя):
# без порога шум проходит проверку в 41% случаев, с порогом «≥6 сделок и ≥+2%» — в 20%, а настоящие откаты
# проходят всё равно. Строже (≥8 сделок, ≥+3%) шум проходит в 8%, но бот может сутками стоять без сделок.
MIN_TRADES = 6
MIN_NET_PCT = 2.0
BB_LEN, BB_K = 20, 2.0  # полосы Боллинджера (стратегия «Отскок»)
BOUNCE_RSI_MAX = 40.0
TREND_FAST, TREND_SLOW = 20, 200  # короткая и длинная средние (стратегия «Тренд»)
TREND_RSI_MIN, TREND_RSI_MAX = 40.0, 62.0
MARKET_DROP_1H = -0.02  # BTC упал больше чем на 2% за час — новых покупок нет: весь рынок падает
MARKET_CACHE_SEC = 60
IDLE_RETRY_SEC = 3600  # «Монеты в дело»: если продажа монеты не прошла, следующая попытка через час
# Лаборатория: каждая монета со своей стратегией «торгует» на бумаге по живым свечам по тем же правилам,
# что и проверка на истории. Стратегия, которая вживую в минусе, к реальной торговле не допускается.
LAB_MIN_TRADES = 3  # с какого числа бумажных сделок верить результату лаборатории
LAB_WINDOW = 20  # судим по последним 20 сделкам: стратегия может «исправиться»
# Самообучение: лаборатория проверяет все 3 стратегии каждой монеты. Стратегия, которая вживую стабильно
# в плюсе, допускается к реальным деньгам, даже если история выбрала другую (или монета не прошла отбор):
LAB_PROMOTE_TRADES = 6  # не меньше 6 бумажных сделок…
LAB_PROMOTE_PCT = 1.5  # …в сумме не меньше +1,5% (то есть стоп-лоссы перекрыты прибылью)…
LAB_PROMOTE_WIN = 0.6  # …и не меньше 60% сделок в плюс; на истории эта стратегия по монете — не в минусе
# Щит
COIN_COOLDOWN_SEC = 24 * 3600  # после стоп-лосса по монете — сутки её не покупать
LOSS_STREAK = 2  # столько убыточных сделок подряд на бирже…
STREAK_PAUSE_SEC = 12 * 3600  # …и новые покупки на этой бирже на паузе на 12 часов
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


STRATEGIES = {  # код -> (название, как объяснить новичку)
    "dip": ("Откат", "резкий откат ниже средней при низком RSI"),
    "bounce": ("Отскок", "цена ушла под нижнюю полосу Боллинджера и развернулась вверх"),
    "trend": ("Тренд", "откат к короткой средней в растущем рынке"),
}


@dataclass
class Params:
    tp: float = TP_GRID[1]
    rsi_buy: float = 30.0
    strategy: str = "dip"

    def text(self) -> str:
        name = STRATEGIES.get(self.strategy, (self.strategy,))[0]
        extra = f", RSI < {self.rsi_buy:g}" if self.strategy == "dip" else ""
        return f"{name}: цель +{self.tp * 100:.1f}%{extra}"


def variants() -> list[Params]:
    """Сетка вариантов: 12 штук. Маленькая нарочно — чем больше вариантов, тем легче «подогнать» под историю."""
    out = []
    for tp in TP_GRID:
        out += [Params(tp, r, "dip") for r in RSI_GRID]
        out += [Params(tp, BOUNCE_RSI_MAX, "bounce"), Params(tp, TREND_RSI_MAX, "trend")]
    return out


def bollinger_low(values: list[float], n: int = BB_LEN, k: float = BB_K) -> tuple[list[float], list[float]]:
    """(средняя, нижняя полоса) скользящим окном n."""
    mid, low = [], []
    s = s2 = 0.0
    for i, v in enumerate(values):
        s += v
        s2 += v * v
        if i >= n:
            old = values[i - n]
            s -= old
            s2 -= old * old
        w = min(i + 1, n)
        m = s / w
        sd = math.sqrt(max(s2 / w - m * m, 0.0))
        mid.append(m)
        low.append(m - k * sd)
    return mid, low


def indicators(closes: list[float]) -> dict[str, list[float]]:
    mid, low = bollinger_low(closes)
    return {"ema": ema(closes, EMA_LEN), "rsi": rsi(closes), "fast": ema(closes, TREND_FAST),
            "slow": ema(closes, TREND_SLOW), "bb_mid": mid, "bb_low": low}


def _no_crash(closes: list[float], i: int) -> bool:
    c = closes[i]
    if i >= 12 and c / closes[i - 12] - 1 < CRASH_1H:
        return False
    day = i - 288
    return not (day >= 0 and c / closes[day] - 1 < CRASH_24H)


def signal_at(closes: list[float], ind: dict[str, list[float]], i: int, p: Params) -> bool:
    """Покупать ли на закрытии свечи i по стратегии p.strategy. Обвал (−3% за час, −8% за сутки) — никогда."""
    if i < EMA_LEN or i >= len(closes):
        return False
    c, r = closes[i], ind["rsi"][i]
    if p.strategy == "bounce":
        low, mid = ind["bb_low"], ind["bb_mid"]
        ok = (closes[i - 1] < low[i - 1] and c > low[i] and c > closes[i - 1] and r < p.rsi_buy
              and c < mid[i] * (1 - DEV_BELOW_EMA))  # до средней ещё есть куда расти
    elif p.strategy == "trend":
        fast, mid, slow = ind["fast"], ind["ema"], ind["slow"]
        ok = (i >= TREND_SLOW and fast[i] > mid[i] > slow[i] and mid[i] > mid[i - 12]  # рынок растёт
              and closes[i - 1] <= fast[i - 1] and c > fast[i]  # откатились к средней и пошли вверх
              and TREND_RSI_MIN <= r <= p.rsi_buy)
    else:
        ok = r < p.rsi_buy and c <= ind["ema"][i] * (1 - DEV_BELOW_EMA)
    return ok and _no_crash(closes, i)


def trade_return(entry: float, exit_: float, fee: float) -> float:
    """Доходность сделки после двух комиссий: купили по entry, продали по exit_."""
    return (1 - fee) ** 2 * exit_ / entry - 1


def target_price(entry: float, tp: float, fee: float) -> float:
    """Цена продажи, при которой после двух комиссий остаётся +tp."""
    return entry * (1 + tp) / (1 - fee) ** 2


def backtest(candles: list[list[float]], p: Params, fee: float, start: int = 0,
             ind: dict[str, list[float]] | None = None) -> dict:
    """Проверка на истории. candles: [[время, open, high, low, close, объём], ...].

    Осторожные допущения: покупка по открытию следующей свечи чуть хуже цены, продажа по цели
    только если high выше цели; если в одной свече задеты и стоп, и цель — считаем, что стоп.
    start: входить только начиная с этой свечи (индикаторы считаются по всей истории) —
    так проверяем вариант на данных, которых он «не видел» при выборе.
    """
    opens = [float(c[1]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    closes = [float(c[4]) for c in candles]
    ind = ind or indicators(closes)
    n = len(candles)
    equity, trades, wins, worst, hold = 1.0, 0, 0, 0.0, 0
    open_ret = None
    i = max(EMA_LEN, start)
    max_hold = MAX_HOLD_SEC // CANDLE_SEC
    while i < n - 1:
        if not signal_at(closes, ind, i, p):
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
        "days": round((n - start) * CANDLE_SEC / 86400, 1), "tp": p.tp, "rsi_buy": p.rsi_buy,
        "strategy": p.strategy,
    }


def best_params(candles: list[list[float]], fee: float) -> dict | None:
    """Выбор стратегии с проверкой на «новых» данных (walk-forward).

    1. Первые ~65% истории: перебираем 12 вариантов (3 стратегии × цели) и берём лучший.
    2. Оставшиеся ~35% этот вариант не видел. Если и там не в минус, а вся история в плюсе —
       монета годится (ok=True). Так отсекаем варианты, которые просто «подогнались» под прошлое.
    """
    if len(candles) < EMA_LEN + 50:
        return None
    split = int(len(candles) * TRAIN_SHARE)
    train_c = candles[:split]
    train_ind = indicators([float(c[4]) for c in train_c])
    tried = [(p, backtest(train_c, p, fee, ind=train_ind)) for p in variants()]
    tried = [x for x in tried if x[1]["trades"] >= 2]
    if not tried:
        return None
    p, train = max(tried, key=lambda x: (x[1]["net_pct"], x[1]["trades"]))
    full_ind = indicators([float(c[4]) for c in candles])
    full = backtest(candles, p, fee, ind=full_ind)
    test = backtest(candles, p, fee, start=split, ind=full_ind)
    full.update(train_pct=train["net_pct"], test_pct=test["net_pct"], test_trades=test["trades"],
                test_days=test["days"])
    # запасные стратегии для лаборатории: лучший вариант каждой из остальных стратегий
    alts = []
    for code in STRATEGIES:
        own = [x for x in tried if x[0].strategy == code and code != p.strategy]
        if own:
            ap, atrain = max(own, key=lambda x: (x[1]["net_pct"], x[1]["trades"]))
            afull = backtest(candles, ap, fee, ind=full_ind)
            alts.append({"strategy": code, "tp": ap.tp, "rsi_buy": ap.rsi_buy, "train_pct": atrain["net_pct"],
                         "net_pct": afull["net_pct"], "trades": afull["trades"]})
    full["alts"] = alts
    full["ok"] = (train["net_pct"] > 0 and test["net_pct"] >= 0
                  and full["trades"] >= MIN_TRADES and full["net_pct"] >= MIN_NET_PCT)
    return full


def is_good(r: dict) -> bool:
    return bool(r.get("ok", r.get("net_pct", 0) > 0))


# ---------------------------------------------------------------------------
# Живая торговля
# ---------------------------------------------------------------------------


def new_scalp_state() -> dict:
    # positions: биржа -> позиция (на каждой бирже своя, деньги разных бирж работают одновременно)
    # paper: биржа -> тестовый баланс
    # use_coins: «Монеты в дело» — продавать лежащие без дела монеты в USDT, чтобы ими торговал режим $5
    return {"on": False, "positions": {}, "paper": {}, "stats": {"cycles": 0, "wins": 0, "pnl": 0.0, "since": 0.0},
            "events": [], "last_buy_candle": {}, "use_coins": False, "convert_tried": {},
            # lab: бумажные сделки по живым свечам; blocked: монеты на паузе после стоп-лосса;
            # risk: монеты с делистингом или плохими новостями; streak/pause: серия убытков по биржам
            "lab": {"open": {}, "stats": {}}, "blocked": {}, "risk": {}, "streak": {}, "pause": {}}


class Scalper:
    def __init__(self, trader: Trader, duplicate_fn: Callable[[], bool] | None = None):
        self.trader = trader
        self.duplicate_fn = duplicate_fn or (lambda: False)
        self.ranking: dict[str, list[dict]] = {}  # биржа -> [результаты проверки по монетам, лучшие сверху]
        self.ranked_at: dict[str, float] = {}
        self.reasons: dict[str, str] = {}  # биржа -> что режим делал на последнем шаге и почему
        self._cur: str | None = None  # биржа, с которой сейчас работает шаг
        self._paper_virtual: set[str] = set()  # биржи, где тест идёт на виртуальных $5 (реальных было меньше)
        self._market: dict[str, tuple[float, float | None]] = {}  # биржа -> (когда, изменение BTC за час)
        self._sig: dict[tuple[str, str], tuple[int, list]] = {}  # (биржа, пара) -> (номер 5-минутки, свечи)
        self._lab_period: dict[str, int] = {}  # биржа -> номер 5-минутки, на которой лаборатория уже отработала
        self.lab_changes = 0  # растёт с каждой закрытой бумажной сделкой: пора сохранить в бэкап
        self.last_step = 0.0

    @property
    def last_reason(self) -> str:
        items = [(n, r) for n, r in self.reasons.items() if r]
        if len(items) == 1:
            return items[0][1]
        return "; ".join(f"{self._name(n) if n != '_' else 'общее'}: {r}" for n, r in items)

    @last_reason.setter
    def last_reason(self, text: str) -> None:
        self.reasons[self._cur or "_"] = text

    @property
    def positions(self) -> dict[str, dict]:
        return self.st["positions"]

    @property
    def pos(self) -> dict | None:
        """Позиция на текущей бирже шага."""
        return self.positions.get(self._cur or "")

    @pos.setter
    def pos(self, value: dict | None) -> None:
        if value is None:
            self.positions.pop(self._cur or "", None)
        else:
            self.positions[value["exchange"]] = value

    # --- состояние ----------------------------------------------------------

    @property
    def st(self) -> dict:
        raw = self.trader.state.scalp
        if not raw:
            raw.update(new_scalp_state())
        for k, v in new_scalp_state().items():
            raw.setdefault(k, v)
        old = raw.pop("pos", None)  # версия до v14: одна позиция и один тестовый баланс
        if old:
            raw["positions"][old["exchange"]] = old
        raw.pop("paper_usd", None)  # старый тестовый баланс начнётся заново: теперь он у каждой биржи свой
        return raw

    @property
    def on(self) -> bool:
        return bool(self.st["on"])

    def _event(self, text: str) -> None:
        if self._cur and len(self.exchanges()) > 1:
            text = f"{self._name(self._cur)}: {text}"
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

    def exchanges(self) -> list[str]:
        """Все биржи с рабочими ключами: режим $5 торгует на каждой своими деньгами (Bybit первым)."""
        working = list(self.trader.working)
        return sorted(working, key=lambda n: (n != "bybit", n))

    def exchange(self) -> str | None:
        """Главная биржа (для проверки на истории и подсказок): Bybit, если есть ключ, иначе первая рабочая."""
        ex = self.exchanges()
        return ex[0] if ex else None

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
        results.sort(key=lambda r: (not is_good(r), -r.get("test_pct", 0), -r["net_pct"]))
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
                 "Для каждой монеты бот перебрал 3 стратегии (Откат, Отскок, Тренд) на первых ~2/3 истории "
                 "и проверил лучшую на последней трети, которую она «не видела».",
                 "<i>Монета [стратегия]: сделок (в плюс) · итог · на новых данных · худшая просадка · цель</i>"]
        for r in rows[:10]:
            icon = "🟢" if is_good(r) else "🔴"
            open_part = f", открыта {r['open_pct']:+.2f}%" if r["open_pct"] is not None else ""
            strat = STRATEGIES.get(r.get("strategy", "dip"), ("?",))[0]
            test = f" · новые {r['test_pct']:+.2f}%" if r.get("test_pct") is not None else ""
            lines.append(f"{icon} {html.escape(r['symbol'].split('/')[0])} [{strat}]: {r['trades']} ({r['wins']}) · "
                         f"<b>{r['net_pct']:+.2f}%</b>{open_part}{test} · {r['worst_pct']:.1f}% · +{r['tp'] * 100:.1f}%")
        good = [r for r in rows if is_good(r)]
        lines.append("")
        if good:
            lines.append(f"Бот торгует только 🟢 монеты (сейчас {len(good)}): от {MIN_TRADES} сделок и от "
                         f"+{MIN_NET_PCT:g}% на всей истории и не в минусе на новых данных. Это прошлое, а не обещание: на другом рынке результат будет другим.")
        else:
            lines.append("Сейчас ни одна монета не прошла проверку: бот ждёт и не покупает. Это правильно — "
                         "лучше стоять, чем торговать в минус. Проверка повторяется каждые 6 часов.")
        return "\n".join(lines)

    # --- шаг -----------------------------------------------------------------

    async def step(self, now: float | None = None) -> list[str]:
        """Один шаг: следим за позицией или ищем вход. Возвращает сообщения для владельца."""
        now = time.time() if now is None else now
        self.last_step = now
        if not self.on:
            return []
        names = self.exchanges()
        # позиции на биржах, ключ которых сейчас не работает, тоже не теряем из виду
        names += [n for n in self.positions if n not in names and n in self.trader.scanner.exchanges]
        self.reasons = {n: r for n, r in self.reasons.items() if n in names}
        if not names:
            self._cur = None
            self.last_reason = "нет рабочего ключа биржи (/keys)"
            return []
        self.reasons.pop("_", None)
        out: list[str] = []
        for name in names:
            out += await self._step_one(name, now)
        return out

    async def learn(self) -> None:
        """Автопилот выключен, но ключ есть: лаборатория всё равно учится (без денег и без ордеров),
        чтобы к включению уже было видно, какие стратегии работают вживую."""
        for name in self.exchanges():
            try:
                await self.rank(name)
                await self.lab_step(name)
            except Exception as e:
                log.info("Лаборатория (обучение) %s: %s", name, e)

    async def _step_one(self, name: str, now: float) -> list[str]:
        self._cur = name
        if not self.pos:
            try:  # проверка на истории — до блокировки: она долгая, а ручные ордера ждать не должны
                await self.rank(name)
            except Exception as e:
                log.info("Проверка на истории: %s", e)
        try:
            await self.lab_step(name)
        except Exception as e:
            log.info("Лаборатория %s: %s", name, e)
        async with self.trader._lock:
            self._cur = name
            try:
                if self.pos:
                    return await self._manage(now)
                if self.duplicate_fn():
                    self.last_reason = ("работают две копии бота: новые покупки на паузе, "
                                        "удали лишний сервис в Render (/doctor)")
                    return []
                return await self._look_for_entry(name, now)
            except TradeError as e:
                self.last_reason = str(e)
            except Exception as e:
                log.exception("Ошибка режима $5 (%s)", name)
                self.last_reason = human_error(e)
        return []

    async def _free_usd(self, name: str) -> float:
        paper = self.st["paper"]
        if self.trader.state.live:
            return await self.trader.free(name, self.trader.settings.quote)
        # тест: начинаем с реального свободного баланса (или виртуальных $5, если пусто). Пока тестовых
        # сделок не было, сверяемся с биржей снова: деньги могли прийти позже или баланс не прочитался
        if paper.get(name) is None or (name in self._paper_virtual and not self.positions.get(name)):
            try:
                real = await self.trader.free(name, self.trader.settings.quote)
            except Exception:
                return float(paper.get(name) or 5.0)  # не прочитали — не запоминаем виртуальные $5 навсегда
            if real >= 5:
                paper[name] = round(real, 4)
                self._paper_virtual.discard(name)
            elif paper.get(name) is None:
                paper[name] = 5.0
                self._paper_virtual.add(name)
        return float(paper[name])

    async def _look_for_entry(self, name: str, now: float) -> list[str]:
        t = self.trader
        quote = t.settings.quote
        free = await self._free_usd(name)
        if self.st["use_coins"] and free < self._min_cost(name, f"BTC/{quote}"):
            converted = await self._convert_idle(name)
            if converted:  # деньги пришли: покупать будем на следующем шаге, по свежему балансу
                return converted
        cost = math.floor(min(free, t.settings.max_trade_usd) * 100 + 1e-6) / 100
        pause = float(self.st["pause"].get(name) or 0)
        if pause > now:
            self.last_reason = (f"щит: {LOSS_STREAK} убытка подряд — новые покупки на паузе до "
                                f"{self._clock(pause)}. Рынок сейчас против стратегии, лучше переждать")
            return []
        ranking = await self.rank(name)
        good, held_back = self.candidates(name, now)
        if not good:
            if held_back:
                self.last_reason = "жду: монеты, прошедшие проверку, сейчас под защитой (" + "; ".join(held_back[:3]) + ")"
            else:
                self.last_reason = ("жду: ни одна монета сейчас не прошла проверку на истории (3 стратегии)" if ranking
                                    else "биржа не отдала свечи, попробую позже")
            return []
        min_need = min(self._min_cost(name, r["symbol"]) for r, _, _ in good)
        if free < min_need:
            await self._fresh_balances(name)
            idle = self._idle_coins_text(name)
            if idle and not self.st["use_coins"]:
                self.last_reason = (f"на счёте {free:.2f} {quote}, а деньги лежат в монетах ({idle}). Включи «Монеты "
                                    "в дело» (/autopilot) — бот сам продаст их в USDT и пустит в торговлю")
                return []
            if idle and not t.state.live:
                self.last_reason = (f"тест: {free:.2f} {quote} на счёте. В реальном режиме «Монеты в дело» продаст "
                                    f"{idle} в USDT и начнёт торговать")
                return []
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
        for r, p, src in good:
            symbol = r["symbol"]
            candles = await self.signal_candles(name, symbol)
            if len(candles) < EMA_LEN + 15:
                continue
            closes = [float(c[4]) for c in candles[:-1]]  # последняя свеча ещё не закрылась
            i = len(closes) - 1
            candle_ts = float(candles[-2][0])
            if self.st["last_buy_candle"].get(f"{name}:{symbol}") == candle_ts:
                continue
            if not signal_at(closes, indicators(closes), i, p):
                continue
            drop = await self.market_drop(name)
            if drop is not None and drop < MARKET_DROP_1H:
                self.last_reason = (f"тормоз рынка: BTC {drop * 100:+.1f}% за час — весь рынок падает, "
                                    f"сигнал по {symbol.split('/')[0]} пропускаю")
                return []
            ticker = await t.scanner.exchanges[name].fetch_ticker(symbol)
            bid, ask = float(ticker.get("bid") or 0), float(ticker.get("ask") or 0)
            if not bid or not ask or (ask - bid) / bid > MAX_SPREAD:
                continue
            if not self._sellable(name, symbol, cost, ask):
                skipped.append(symbol.split("/")[0])
                continue
            self.st["last_buy_candle"][f"{name}:{symbol}"] = candle_ts
            return await self._buy(name, symbol, cost, ask, p, now, src)
        names = ", ".join(f"{r['symbol'].split('/')[0]} ({STRATEGIES.get(p.strategy, ('?',))[0]}"
                          f"{' ⭐' if src == 'lab' else ''})" for r, p, src in good)
        self.last_reason = f"жду сигнала: слежу за {names}, ${cost:.2f} готовы к покупке"
        if held_back:
            self.last_reason += ". Под защитой: " + "; ".join(held_back[:3])
        if skipped:
            self.last_reason += (f". Был откат на {', '.join(skipped)}, но с ${cost:.2f} продажу потом не пропустит "
                                 "минимум биржи (крупный шаг количества): нужно чуть больше денег")
        return []

    async def _buy(self, name: str, symbol: str, cost: float, ask: float, p: Params, now: float,
                   src: str = "history") -> list[str]:
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
            self.st["paper"][name] = round(float(self.st["paper"].get(name) or 0) - cost, 6)
        entry = spent / got * (1 - fee)  # цена до комиссии: комиссию считаем в target_price
        pos = {"exchange": name, "symbol": symbol, "amount": got, "cost": spent, "entry": entry,
               "tp": target_price(entry, p.tp, fee), "tp_pct": p.tp, "fee": fee, "opened_at": now,
               "order_id": None, "live": live, "breakeven": False, "strategy": p.strategy}
        self.pos = pos
        t._record(TradeRecord(now, name, "buy", symbol, live, spent, t.settings.quote, got, coin, spent,
                              source="scalp", order_id=oid,
                              note=f"режим $5: {p.text()}" + ("" if live else " (тест)")))
        mode = "" if live else "🧪 ТЕСТ: "
        tp_line = f"Продажа выставлена по {pos['tp']:.6g} (+{p.tp * 100:.1f}% после комиссий)."
        try:
            await self._place_tp(pos)
        except TradeError as e:
            tp_line = f"⚠️ {html.escape(str(e))}"
        why = STRATEGIES.get(p.strategy, ("", ""))
        text = (f"🎯 {mode}<b>Режим $5: купил {html.escape(coin)}</b> на {html.escape(self._name(name))}\n"
                f"${spent:.2f} → {got:.8g} {html.escape(coin)} по ≈{entry:.6g}\n"
                f"Сигнал «{why[0]}»: {why[1]}.\n"
                + (self._proof_line(name, symbol, p.strategy) if src == "lab" else "") + tp_line)
        self._event(f"купил {coin} на ${spent:.2f}")
        self.last_reason = f"держу {coin}, жду цели {pos['tp']:.6g}"
        return [text]

    # --- свечи для сигналов --------------------------------------------------

    async def signal_candles(self, name: str, symbol: str) -> list[list[float]]:
        """Свечи для сигнала. Сигнал меняется только когда закрывается 5-минутная свеча, поэтому качаем
        один раз на свечу (а не каждые 30 с). Кэш верим, только если биржа уже отдала текущую свечу."""
        period = int(time.time() // CANDLE_SEC)
        cached = self._sig.get((name, symbol))
        if cached and cached[0] == period:
            return cached[1]
        candles = await self._candles(name, symbol, SIGNAL_CANDLES)
        if candles and float(candles[-1][0]) >= period * CANDLE_SEC * 1000:
            self._sig[(name, symbol)] = (period, candles)
        return candles

    # --- лаборатория: проверка стратегий вживую без денег -----------------------

    @property
    def lab(self) -> dict:
        lab = self.st["lab"]
        lab.setdefault("open", {})
        lab.setdefault("stats", {})
        return lab

    @staticmethod
    def lab_key(name: str, symbol: str, strategy: str) -> str:
        return f"{name}|{symbol}|{strategy}"

    def lab_verdict(self, name: str, symbol: str, strategy: str) -> tuple[str, dict | None]:
        """«new» — мало бумажных сделок, «ok» — вживую не в минусе, «bad» — вживую в минусе."""
        s = self.lab["stats"].get(self.lab_key(name, symbol, strategy))
        if not s or len(s["rets"]) < LAB_MIN_TRADES:
            return "new", s
        return ("bad" if s["sum"] < 0 else "ok"), s

    def _lab_record(self, key: str, ret_pct: float, now: float) -> None:
        s = self.lab["stats"].setdefault(key, {"rets": [], "sum": 0.0, "n": 0, "wins": 0, "since": now})
        s["rets"].append(round(ret_pct, 4))
        del s["rets"][:-LAB_WINDOW]
        s["sum"] = round(sum(s["rets"]), 4)
        s["n"] += 1
        s["wins"] += ret_pct > 0
        s["last"] = now
        self.lab_changes += 1

    def slots(self, name: str) -> list[tuple[dict, Params, bool]]:
        """Все стратегии, которые проверяет лаборатория: (результат монеты, параметры, это главная стратегия?)."""
        out = []
        for r in self.ranking.get(name) or []:
            out.append((r, Params(r["tp"], r["rsi_buy"], r.get("strategy", "dip")), True))
            for a in r.get("alts") or []:
                out.append((r, Params(a["tp"], a["rsi_buy"], a["strategy"]), False))
        return out

    def promoted(self, name: str, symbol: str, alt: dict) -> dict | None:
        """Запасная стратегия доказала себя вживую — её статистика лаборатории, иначе None."""
        s = self.lab["stats"].get(self.lab_key(name, symbol, alt["strategy"]))
        if not s or len(s["rets"]) < LAB_PROMOTE_TRADES or alt.get("net_pct", -1) < 0:
            return None
        wins = sum(x > 0 for x in s["rets"])
        return s if s["sum"] >= LAB_PROMOTE_PCT and wins >= LAB_PROMOTE_WIN * len(s["rets"]) else None

    def candidates(self, name: str, now: float) -> tuple[list[tuple[dict, Params, str]], list[str]]:
        """Что можно покупать за реальные деньги: по одной стратегии на монету, лучшие сверху.
        Возвращает ([(результат монеты, параметры, откуда)], [почему другие монеты под защитой])."""
        picked, held_back = [], []
        for r in self.ranking.get(name) or []:
            coin = r["symbol"].split("/")[0]
            options = []  # (ранг, -сумма в лаборатории, параметры, откуда)
            main = Params(r["tp"], r["rsi_buy"], r.get("strategy", "dip"))
            verdict, s = self.lab_verdict(name, r["symbol"], main.strategy)
            if is_good(r) and verdict == "bad":
                held_back.append(f"{coin} — в лаборатории вживую {s['sum']:+.2f}% за {len(s['rets'])} сделок")
            elif is_good(r):
                options.append((0 if verdict == "ok" else 1, -(s or {}).get("sum", 0), main, "history"))
            for a in r.get("alts") or []:
                ps = self.promoted(name, r["symbol"], a)
                if ps:
                    options.append((0, -ps["sum"], Params(a["tp"], a["rsi_buy"], a["strategy"]), "lab"))
            if not options:
                continue
            why = self.shield_reason(coin, now)
            if why:
                held_back.append(f"{coin} — {why}")
                continue
            picked.append(min(options, key=lambda o: o[:2]) + (r,))
        picked.sort(key=lambda o: o[:2])
        return [(r, prm, src) for _, _, prm, src, r in picked[:TOP_COINS]], held_back

    async def lab_step(self, name: str, now: float | None = None) -> None:
        """Раз в 5-минутку: ведём бумажные позиции и открываем новые по сигналам всех проверенных монет.
        Правила те же, что в проверке на истории: вход по открытию следующей свечи, цель по high, стоп по low,
        через сутки цель опускается до безубытка. Так видно, работает ли выбор стратегии на живом рынке."""
        now = time.time() if now is None else now
        period = int(now // CANDLE_SEC)
        if self._lab_period.get(name) == period:
            return
        ranking = self.ranking.get(name) or []
        if not ranking:
            return
        self._lab_period[name] = period
        lab = self.lab
        fees = {r["symbol"]: self._fee(name, r["symbol"]) for r in ranking}
        # 1) открытые бумажные позиции (в т.ч. по стратегиям, которые после пересчёта уже не выбраны)
        for key, pos in list(lab["open"].items()):
            ex_name, symbol, _ = key.split("|")
            if ex_name != name:
                continue
            candles = await self.signal_candles(name, symbol)
            for c in candles[:-1]:
                ts = float(c[0])
                if ts <= pos["checked"]:
                    continue
                pos["checked"] = ts
                target = pos["tp"] if (ts - pos["entry_ts"]) / 1000 < MAX_HOLD_SEC else min(pos["tp"], pos["be"])
                exit_ = None
                if float(c[3]) <= pos["stop"]:
                    exit_ = pos["stop"] * (1 - SLIPPAGE)
                elif float(c[2]) > target:
                    exit_ = target
                if exit_ is not None:
                    self._lab_record(key, trade_return(pos["entry"], exit_, pos["fee"]) * 100, now)
                    del lab["open"][key]
                    break
        # 2) новые сигналы — по всем трём стратегиям каждой монеты
        ind_cache: dict[str, tuple[list, list[float], dict]] = {}
        for r, p, _ in self.slots(name):
            symbol = r["symbol"]
            key = self.lab_key(name, symbol, p.strategy)
            if key in lab["open"]:
                continue
            if symbol not in ind_cache:
                candles = await self.signal_candles(name, symbol)
                closes = [float(c[4]) for c in candles[:-1]]
                ind_cache[symbol] = (candles, closes, indicators(closes) if len(candles) >= EMA_LEN + 15 else {})
            candles, closes, ind = ind_cache[symbol]
            if not ind:
                continue
            candle_ts = float(candles[-2][0])
            if lab["stats"].get(key, {}).get("signal_ts") == candle_ts:
                continue
            if not signal_at(closes, ind, len(closes) - 1, p):
                continue
            fee = fees.get(symbol, 0.001)
            entry = float(candles[-1][1]) * (1 + SLIPPAGE)
            lab["open"][key] = {"entry": entry, "tp": target_price(entry, p.tp, fee),
                                "be": target_price(entry, BREAKEVEN_EXTRA, fee), "stop": entry * (1 - STOP_PCT),
                                "fee": fee, "entry_ts": float(candles[-1][0]), "checked": candle_ts, "opened": now}
            lab["stats"].setdefault(key, {"rets": [], "sum": 0.0, "n": 0, "wins": 0, "since": now})["signal_ts"] = candle_ts

    def _proof_line(self, name: str, symbol: str, strategy: str) -> str:
        s = self.lab["stats"].get(self.lab_key(name, symbol, strategy)) or {"rets": [], "sum": 0}
        wins = sum(x > 0 for x in s["rets"])
        return (f"⭐ Стратегию выбрала лаборатория: вживую без денег {len(s['rets'])} сделок, {wins} в плюс, "
                f"{s['sum']:+.2f}%.\n")

    def lab_rows(self, name: str | None = None) -> list[dict]:
        name = name or self.exchange() or ""
        out = []
        alts = {(r["symbol"], a["strategy"]): a for r in self.ranking.get(name) or [] for a in r.get("alts") or []}
        for r, p, main in self.slots(name):
            key = self.lab_key(name, r["symbol"], p.strategy)
            verdict, s = self.lab_verdict(name, r["symbol"], p.strategy)
            is_open = key in self.lab["open"]
            if not main and not s and not is_open:
                continue  # запасные стратегии без сделок не показываем, чтобы не засорять список
            promoted = not main and self.promoted(name, r["symbol"], alts[(r["symbol"], p.strategy)]) is not None
            out.append({"symbol": r["symbol"], "strategy": p.strategy, "verdict": verdict, "main": main,
                        "promoted": promoted, "trades": len(s["rets"]) if s else 0,
                        "wins": sum(x > 0 for x in s["rets"]) if s else 0,
                        "sum_pct": s["sum"] if s else 0.0, "open": is_open, "passed": is_good(r) if main else promoted})
        return out

    def lab_text(self, name: str | None = None) -> str:
        name = name or self.exchange()
        if not name:
            return "🧪 Лаборатория: нужен рабочий ключ биржи (/keys)."
        rows = self.lab_rows(name)
        lines = [f"🧪 <b>Лаборатория</b>: {html.escape(self._name(name))}",
                 "Все 3 стратегии каждой монеты «торгуют» без денег по живым свечам — по тем же правилам, что "
                 "и проверка на истории. Если стратегия вживую в минусе (от "
                 f"{LAB_MIN_TRADES} сделок), реальные деньги на неё не идут. Судим по последним {LAB_WINDOW} сделкам.",
                 f"🧠 <b>Самообучение</b>: стратегия, которая вживую дала от {LAB_PROMOTE_TRADES} сделок, "
                 f"≥ {LAB_PROMOTE_PCT:+g}% и ≥ {LAB_PROMOTE_WIN:.0%} в плюс, получает ⭐ и торгует за реальные деньги — "
                 "даже если история выбрала другую.", ""]
        if not rows:
            lines.append("Пока пусто: проверка на истории ещё не готова (включи автопилот: /autopilot).")
        icon = {"ok": "🟢", "bad": "🔴", "new": "⚪️"}
        for x in rows:
            strat = STRATEGIES.get(x["strategy"], ("?",))[0]
            res = (f"{x['trades']} сделок ({x['wins']} в плюс), <b>{x['sum_pct']:+.2f}%</b>" if x["trades"]
                   else "сделок ещё не было")
            tail = ("" if x["passed"] else " · <i>проверку на истории не прошла</i>") if x["main"] else (
                " · ⭐ <b>доказала себя вживую — торгую</b>" if x["promoted"] else " · <i>запасная</i>")
            lines.append(f"{icon[x['verdict']]} {html.escape(x['symbol'].split('/')[0])} [{strat}]: {res}"
                         + (" · сейчас в бумажной сделке" if x["open"] else "") + tail)
        lines.append("\n🟢 вживую не в минусе · ⚪️ мало данных (торговать можно, если прошла проверку на истории) · "
                     "🔴 вживую в минусе — не торгую")
        return "\n".join(lines)

    # --- щит ---------------------------------------------------------------------

    def _clock(self, ts: float) -> str:
        return time.strftime("%d.%m %H:%M", time.gmtime(ts + self.trader.settings.report_utc_offset * 3600))

    def set_risk(self, risk: dict[str, tuple[float, str]]) -> None:
        """Монеты с делистингом или плохими новостями (из bot/opportunities.risk_coins)."""
        self.st["risk"] = {c: [float(until), str(why)] for c, (until, why) in risk.items()}

    def shield_reason(self, coin: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        for src in ("risk", "blocked"):
            item = self.st[src].get(coin)
            if item and float(item[0]) > now:
                return f"{item[1]} (до {self._clock(float(item[0]))})"
        return None

    def shield_rows(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        out = []
        for src in ("risk", "blocked"):
            for coin, (until, why) in sorted(self.st[src].items()):
                if float(until) > now:
                    out.append({"coin": coin, "until": float(until), "reason": why, "kind": src})
        for name, until in self.st["pause"].items():
            if float(until) > now:
                out.append({"coin": "*", "exchange": name, "until": float(until), "kind": "pause",
                            "reason": f"{LOSS_STREAK} убытка подряд на {self._name(name)}: пауза новых покупок"})
        return out

    def _after_close(self, name: str, coin: str, pnl: float, why: str, now: float) -> str:
        """Щит после сделки: монета на паузу после стоп-лосса, биржа на паузу после серии убытков."""
        st = self.st
        for src in ("blocked", "risk"):  # заодно чистим истёкшее, чтобы состояние не разрасталось
            st[src] = {c: v for c, v in st[src].items() if float(v[0]) > now}
        if pnl >= 0:
            st["streak"][name] = 0
            return ""
        if why == "продажа вручную":  # твоё решение, а не ошибка стратегии: щит не включаем
            return ""
        note = ""
        if why == "стоп-лосс":
            st["blocked"][coin] = [now + COIN_COOLDOWN_SEC, "был стоп-лосс"]
            note = f"\n🛡 {html.escape(coin)} не покупаю сутки: после стоп-лосса монете нужно успокоиться."
        st["streak"][name] = int(st["streak"].get(name) or 0) + 1
        if st["streak"][name] >= LOSS_STREAK:
            st["streak"][name] = 0
            st["pause"][name] = now + STREAK_PAUSE_SEC
            note += (f"\n🛡 {LOSS_STREAK} убытка подряд: новые покупки на {html.escape(self._name(name))} "
                     f"на паузе до {self._clock(now + STREAK_PAUSE_SEC)}. Открытые продажи остаются.")
        return note

    # --- тормоз рынка и «Монеты в дело» ---------------------------------------

    async def market_drop(self, name: str, now: float | None = None) -> float | None:
        """Изменение BTC за последний час на этой бирже (None — не узнали: тогда не мешаем торговле)."""
        now = time.time() if now is None else now
        cached = self._market.get(name)
        if cached and now - cached[0] < MARKET_CACHE_SEC:
            return cached[1]
        symbol = f"BTC/{self.trader.settings.quote}"
        drop = None
        if self.trader.scanner.market(name, symbol):
            candles = await self._candles(name, symbol, 14)
            closes = [float(c[4]) for c in candles]
            if len(closes) >= 13 and closes[-13] > 0:
                drop = closes[-1] / closes[-13] - 1
        self._market[name] = (now, drop)
        return drop

    def _idle_rows(self, name: str) -> list[tuple[str, float, float]]:
        """Монеты на спот-счёте, которые можно продать в USDT: [(монета, свободно, ≈$)] из последнего баланса."""
        at_rows = self.trader.balance_cache.get(name)
        if not at_rows:
            return []
        quote = self.trader.settings.quote
        busy = {p["symbol"].split("/")[0] for p in self.positions.values() if p and p.get("exchange") == name}
        out = []
        for coin, free, total, usd in at_rows[1]:
            if coin == quote or coin in busy or not usd or not total or free <= 0:
                continue
            symbol = f"{coin}/{quote}"
            m = self.trader.scanner.market(name, symbol)
            if not m or m.get("active") is False:
                continue
            price = usd / total
            if free * price >= self._min_cost(name, symbol, price) * 1.03:
                out.append((coin, free, free * price))
        return out

    async def _fresh_balances(self, name: str, max_age: float = 300) -> None:
        cached = self.trader.balance_cache.get(name)
        if cached and time.time() - cached[0] < max_age:
            return
        try:
            await self.trader.balances(name)
        except Exception as e:
            log.info("Баланс %s: %s", name, e)

    def _idle_coins_text(self, name: str) -> str:
        return ", ".join(f"{html.escape(c)} ≈${usd:.2f}" for c, _, usd in self._idle_rows(name))

    async def _convert_idle(self, name: str) -> list[str]:
        """«Монеты в дело»: продать по рынку монеты, которые лежат без дела, чтобы режим $5 торговал этими деньгами.
        Только в реальном режиме, только больше минимального ордера, и не ту монету, что в открытой сделке."""
        t = self.trader
        await self._fresh_balances(name)
        if not t.state.live:
            return []
        quote = t.settings.quote
        tried = self.st["convert_tried"]
        out = []
        for coin, free, usd in self._idle_rows(name):
            key = f"{name}:{coin}"
            if time.time() - float(tried.get(key) or 0) < IDLE_RETRY_SEC:
                continue
            tried[key] = time.time()
            symbol = f"{coin}/{quote}"
            ex = t._ex(name)
            try:
                amount = t._amount(ex, symbol, free)
                order = await ex.create_market_sell_order(symbol, amount)
                _, got = await t._settle(ex, name, order, symbol, "sell", self._fee(name, symbol))
            except Exception as e:
                self._event(f"не смог продать {coin} в {quote}: {human_error(e)}")
                out.append(f"⚠️ Автопилот: не смог продать {html.escape(coin)} в {quote} на "
                           f"{html.escape(self._name(name))}: {html.escape(human_error(e))}. Попробую через час.")
                continue
            tried.pop(key, None)
            t._record(TradeRecord(time.time(), name, "sell", symbol, True, amount, coin, got, quote, got,
                                  source="scalp", order_id=str(order.get("id")),
                                  note="автопилот: монета без дела → USDT для режима $5"))
            self._event(f"продал {coin} → {got:.2f} {quote}: деньги в дело")
            out.append(f"🪙 <b>Автопилот: {html.escape(coin)} → {got:.2f} {quote}</b> на {html.escape(self._name(name))}\n"
                       "Монета лежала без дела, теперь эти деньги торгует режим $5. "
                       "Выключить: /scalp → «Монеты в дело».")
        if out:
            t.balance_cache.pop(name, None)
            self.last_reason = "деньги в USDT, ищу сигнал"
        return out

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
        pos = self.pos
        t = self.trader
        name, symbol = pos["exchange"], pos["symbol"]
        coin = symbol.split("/")[0]
        if pos["live"] != t.state.live and not pos["live"]:
            # тестовую позицию в реальном режиме не продолжаем: закрываем её как тест
            self.pos = None
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

    async def _stop(self, bid: float, now: float, why: str = "стоп-лосс") -> list[str]:
        pos = self.pos
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
        return self._closed(got, why, now)

    def _closed(self, got: float, why: str, now: float) -> list[str]:
        pos = self.pos
        t = self.trader
        coin = pos["symbol"].split("/")[0]
        pnl = got - pos["cost"]
        stats = self.st["stats"]
        stats["cycles"] += 1
        stats["wins"] += pnl > 0
        stats["pnl"] = round(stats["pnl"] + pnl, 6)
        if not pos["live"]:
            ex_name = pos["exchange"]
            self.st["paper"][ex_name] = round(float(self.st["paper"].get(ex_name) or 0) + got, 6)
        t._record(TradeRecord(now, pos["exchange"], "sell", pos["symbol"], pos["live"], pos["amount"], coin, got,
                              t.settings.quote, got, pnl_usd=pnl, source="scalp",
                              note=f"режим $5: {why}" + ("" if pos["live"] else " (тест)")))
        self.positions.pop(pos["exchange"], None)
        shield_note = self._after_close(pos["exchange"], coin, pnl, why, now)
        hours = (now - pos["opened_at"]) / 3600
        icon = "✅" if pnl >= 0 else "🔻"
        mode = "" if pos["live"] else "🧪 ТЕСТ: "
        self._event(f"продал {coin}: {pnl:+.4f}$ ({why})")
        self.last_reason = "продал, ищу следующий откат"
        return [f"{icon} {mode}<b>Режим $5: продал {html.escape(coin)}</b> на {html.escape(self._name(pos['exchange']))} "
                f"({why}) через {hours:.1f} ч\n"
                f"${pos['cost']:.4f} → ${got:.4f} = <b>{pnl:+.4f}$</b> ({pnl / pos['cost'] * 100:+.2f}%)\n"
                f"Всего в режиме $5: {stats['cycles']} сделок, {stats['pnl']:+.4f}$" + shield_note]

    async def sell_now(self, name: str | None = None) -> str:
        """Кнопка «Продать сейчас»: снять цель и продать по рынку (если позиция больше минимума биржи)."""
        names = [name] if name else list(self.positions)
        if not names or not any(self.positions.get(n) for n in names):
            return "Позиций нет: продавать нечего."
        out = []
        async with self.trader._lock:
            for n in names:
                self._cur = n
                pos = self.pos
                if not pos:
                    continue
                t = self.trader
                title = self._name(n)
                try:
                    ticker = await t.scanner.exchanges[n].fetch_ticker(pos["symbol"])
                    bid = float(ticker.get("bid") or 0)
                    if pos["live"] and pos["amount"] * bid < self._min_cost(n, pos["symbol"], bid):
                        out.append(f"{html.escape(title)}: позиция стоит ${pos['amount'] * bid:.2f}, это меньше "
                                   "минимального ордера биржи. Продать по рынку нельзя: продажа по цели остаётся стоять.")
                        continue
                    res = await self._stop(bid, time.time(), "продажа вручную")
                    out.append(res[0].replace("стоп-лосс", "продажа вручную"))
                except TradeError as e:
                    out.append(f"{html.escape(title)}: ❌ {html.escape(str(e))}")
        return "\n\n".join(out)

    # --- тексты -------------------------------------------------------------

    def _pos_line(self, pos: dict) -> str:
        coin = pos["symbol"].split("/")[0]
        hours = (time.time() - pos["opened_at"]) / 3600
        be = " (безубыток)" if pos["breakeven"] else ""
        strat = STRATEGIES.get(pos.get("strategy") or "dip", ("",))[0]
        return (f"<b>{fmt(pos['amount'])} {html.escape(coin)}</b> за ${pos['cost']:.2f} [{strat}], держу {hours:.1f} ч, "
                f"продажа по {pos['tp']:.6g}{be}")

    def status_text(self) -> str:
        st, t = self.st, self.trader
        live = t.state.live
        lines = ["🎯 <b>Режим $5 (автопилот)</b>",
                 "Сам выбирает монету и стратегию (Откат, Отскок или Тренд — что прошло проверку на истории), "
                 "покупает на все свободные USDT и сразу ставит продажу на +0,6…1,3% после комиссий. "
                 "Торгует на каждой бирже с ключом своими деньгами одновременно."]
        state = ("🟢 включён" if st["on"] else "⚪️ выключен") + (", 🔴 реальные деньги" if live else ", 🧪 тест")
        lines.append(f"Состояние: <b>{state}</b>")
        lines.append("🪙 Монеты в дело: " + ("<b>вкл</b> — лежащие монеты бот продаёт в USDT и торгует ими"
                                             if st["use_coins"] else "выкл (монеты на счёте не трогаю)"))
        names = self.exchanges() + [n for n in self.positions if n not in self.exchanges()]
        for n in names:
            lines.append(f"\n<b>{html.escape(self._name(n))}</b>")
            real = self._real_line(n)
            if real:
                lines.append(f"  на бирже: {real}")
            if not live and st["paper"].get(n) is not None:
                lines.append(f"  тестовый баланс: ${float(st['paper'][n]):.2f}"
                             + (" (виртуальные: на бирже меньше $5)" if n in self._paper_virtual else ""))
            if self.positions.get(n):
                lines.append("  позиция: " + self._pos_line(self.positions[n]))
            if self.reasons.get(n):
                lines.append(f"  сейчас: {html.escape(self.reasons[n])}")
        if self.reasons.get("_"):
            lines.append(f"Сейчас: {html.escape(self.reasons['_'])}")
        lab = self.lab_rows()
        tested = [x for x in lab if x["verdict"] != "new"]
        if lab:
            good = sum(x["verdict"] == "ok" for x in tested)
            stars = sum(x["promoted"] for x in lab)
            lines.append(f"\n🧪 Лаборатория (проверка вживую без денег): проверено {len(tested)} стратегий, "
                         f"не в минусе {good}, в минусе {len(tested) - good}"
                         + (f", ⭐ доказали себя и торгуют: {stars}" if stars else "") + " — подробно /lab")
        shield = self.shield_rows()
        if shield:
            lines.append("🛡 Щит: " + "; ".join(
                html.escape(f"{x['coin'] if x['coin'] != '*' else self._name(x['exchange'])} — {x['reason'][:70]}")
                for x in shield[:4]))
        s = st["stats"]
        if s["cycles"]:
            lines.append(f"\nИтог с включения: {s['cycles']} сделок, в плюс {s['wins']}, <b>{s['pnl']:+.4f}$</b>")
        if st["events"]:
            lines.append("\nПоследнее:")
            for at, text in st["events"][-4:]:
                lines.append(f"• {time.strftime('%d.%m %H:%M', time.gmtime(at + t.settings.report_utc_offset * 3600))} "
                             f"{html.escape(text)}")
        lines.append("\nЧестно: прибыль не гарантирована. На падении деньги могут зависнуть в монете, "
                     "пока цена не вернётся. Ставь только то, что не жалко.")
        return "\n".join(lines)

    def report_text(self) -> str:
        """Коротко для ежедневного отчёта (пусто, если автопилот никогда не включали)."""
        st = self.st
        s = st["stats"]
        if not st["on"] and not s["cycles"] and not self.positions:
            return ""
        lines = [f"🤖 <b>Автопилот</b>: {'🟢 работает' if st['on'] else '⏸ выключен'}, "
                 f"{'🔴 реальные деньги' if self.trader.state.live else '🧪 тест'}"]
        if s["cycles"]:
            lines.append(f"Сделок {s['cycles']}, в плюс {s['wins']}, итог <b>{s['pnl']:+.4f}$</b>")
        for pos in self.positions.values():
            lines.append("В сделке: " + self._pos_line(pos))
        lab = [x for x in self.lab_rows() if x["verdict"] != "new"]
        if lab:
            best = max(lab, key=lambda x: x["sum_pct"])
            lines.append(f"🧪 Лаборатория: проверено вживую {len(lab)} стратегий, не в минусе "
                         f"{sum(x['verdict'] == 'ok' for x in lab)}; лучшая — {html.escape(best['symbol'].split('/')[0])} "
                         f"{best['sum_pct']:+.2f}% (/lab)")
        shield = self.shield_rows()
        if shield:
            lines.append(f"🛡 Щит: под защитой {len(shield)} (" + ", ".join(
                html.escape(x["coin"] if x["coin"] != "*" else "пауза биржи") for x in shield[:5]) + ")")
        if st["on"] and self.last_reason:
            lines.append("Сейчас: " + html.escape(self.last_reason[:200]))
        return "\n".join(lines)

    def _real_line(self, name: str) -> str:
        """Реальный баланс торгового счёта из последнего чтения (без запроса к бирже)."""
        at_rows = self.trader.balance_cache.get(name)
        if not at_rows:
            return ""
        rows = at_rows[1]
        quote = self.trader.settings.quote
        free = next((f for c, f, _, _ in rows if c == quote), 0.0)
        total = sum(u or 0 for _, _, _, u in rows)
        return f"свободно {free:.2f} {quote}, всего ≈ ${total:.2f}"

    def status_dict(self) -> dict:
        st = self.st
        keys = ("exchange", "symbol", "amount", "cost", "entry", "tp", "opened_at", "breakeven", "strategy")
        positions = [{k: p.get(k) for k in keys} for p in self.positions.values() if p]
        paper = {n: v for n, v in st["paper"].items() if v is not None}
        return {"on": st["on"], "live": self.trader.state.live, "reason": self.last_reason,
                "use_coins": bool(st["use_coins"]),
                "lab": self.lab_rows(), "shield": self.shield_rows(),
                "reasons": {n: r for n, r in self.reasons.items() if r},
                "real": {n: self._real_line(n) for n in self.exchanges() if self._real_line(n)},
                "paper": paper, "paper_usd": round(sum(paper.values()), 4) if paper else None,
                "stats": dict(st["stats"]), "positions": positions, "pos": positions[0] if positions else None,
                "top": [{**{k: r.get(k) for k in ("symbol", "trades", "wins", "net_pct", "worst_pct", "tp",
                                                   "strategy", "test_pct")},
                         "ok": is_good(r), "strategy_name": STRATEGIES.get(r.get("strategy") or "dip", ("?",))[0]}
                        for r in (self.ranking.get(self.exchange() or "") or [])[:5]]}


def fmt(x: float) -> str:
    return f"{x:.8g}"
