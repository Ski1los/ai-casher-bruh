"""Сканер межбиржевого арбитража.

Идея простая: одна и та же монета на разных биржах стоит немного по-разному.
Если на бирже A её можно КУПИТЬ (цена ask) дешевле, чем на бирже B ПРОДАТЬ
(цена bid), и разница больше комиссий, то это потенциальная возможность.

Сканер использует только ПУБЛИЧНЫЕ данные бирж, так что API-ключи не нужны,
и бот ничего не покупает сам. Он только ищет и сообщает.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field

import ccxt.async_support as ccxt

from .config import Settings

log = logging.getLogger(__name__)

DEFAULT_TAKER_FEE = 0.001  # 0.1%, если биржа не сообщила комиссию
MARKETS_RELOAD_SEC = 3600  # раз в час обновляем список пар
DEPTH_CHECK_TOP = 10  # для скольких лучших вариантов проверять стакан


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------


@dataclass
class Quote:
    """Котировка одной пары на одной бирже."""

    exchange: str
    symbol: str
    bid: float  # по этой цене можно ПРОДАТЬ
    ask: float  # по этой цене можно КУПИТЬ
    volume_usd: float  # объём торгов за 24ч
    taker_fee: float  # комиссия за рыночную сделку (0.001 = 0.1%)


@dataclass
class DepthResult:
    """Результат проверки по стакану: что будет, если реально вложить N долларов."""

    amount_usd: float
    received_usd: float
    filled: bool  # хватило ли глубины стакана на всю сумму

    @property
    def profit_usd(self) -> float:
        return self.received_usd - self.amount_usd

    @property
    def profit_percent(self) -> float:
        return self.profit_usd / self.amount_usd * 100 if self.amount_usd else 0.0


@dataclass
class Opportunity:
    symbol: str
    buy: Quote  # где покупаем
    sell: Quote  # где продаём
    gross_percent: float  # разница цен без комиссий
    net_percent: float  # после торговых комиссий
    depth: DepthResult | None = None
    depth_error: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.symbol, self.buy.exchange, self.sell.exchange)


@dataclass
class ScanResult:
    opportunities: list[Opportunity] = field(default_factory=list)
    exchanges_ok: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    symbols_compared: int = 0
    duration_sec: float = 0.0
    finished_at: float = 0.0


# ---------------------------------------------------------------------------
# Чистая логика (без сети). Её легко тестировать.
# ---------------------------------------------------------------------------


def net_percent(ask: float, bid: float, fee_buy: float, fee_sell: float) -> float:
    """Прибыль в % после комиссий: купили по ask, продали по bid."""
    return ((bid / ask) * (1 - fee_buy) * (1 - fee_sell) - 1) * 100


def find_opportunities(
    quotes: dict[str, dict[str, Quote]],
    settings: Settings,
) -> tuple[list[Opportunity], int]:
    """Ищет лучшие связки «купить здесь, продать там».

    quotes: {биржа: {символ: Quote}}
    Возвращает (список возможностей по убыванию прибыли, сколько пар сравнили).
    """
    by_symbol: dict[str, list[Quote]] = {}
    for exchange_quotes in quotes.values():
        for q in exchange_quotes.values():
            if q.bid <= 0 or q.ask <= 0 or q.volume_usd < settings.min_volume_usd:
                continue
            by_symbol.setdefault(q.symbol, []).append(q)

    result: list[Opportunity] = []
    compared = 0
    for symbol, qs in by_symbol.items():
        if len(qs) < 2:
            continue
        compared += 1
        best: Opportunity | None = None
        for buy in qs:
            for sell in qs:
                if buy.exchange == sell.exchange:
                    continue
                gross = (sell.bid / buy.ask - 1) * 100
                if gross > settings.max_profit_percent:
                    continue  # подозрительно много, скорее всего это разные монеты
                net = net_percent(buy.ask, sell.bid, buy.taker_fee, sell.taker_fee)
                if net < settings.min_profit_percent:
                    continue
                if best is None or net > best.net_percent:
                    best = Opportunity(symbol, buy, sell, gross, net)
        if best:
            result.append(best)

    result.sort(key=lambda o: o.net_percent, reverse=True)
    return result, compared


def simulate_trade(
    asks: list[list[float]],
    bids: list[list[float]],
    amount_usd: float,
    fee_buy: float,
    fee_sell: float,
) -> DepthResult:
    """Проходит по стаканам: покупает на amount_usd по asks, продаёт по bids.

    asks/bids: [[цена, количество], ...], лучшие цены первыми (как отдаёт ccxt).
    """
    # 1) Покупаем монету на бирже A
    spent = 0.0
    coins = 0.0
    for level in asks:
        price, qty = float(level[0]), float(level[1])
        if price <= 0 or qty <= 0:
            continue
        can_spend = min(amount_usd - spent, price * qty)
        coins += can_spend / price
        spent += can_spend
        if spent >= amount_usd - 1e-9:
            break
    coins *= 1 - fee_buy
    filled = spent >= amount_usd - 1e-9

    # 2) Продаём эти монеты на бирже B
    to_sell = coins
    received = 0.0
    for level in bids:
        price, qty = float(level[0]), float(level[1])
        if price <= 0 or qty <= 0:
            continue
        sell_qty = min(to_sell, qty)
        received += sell_qty * price
        to_sell -= sell_qty
        if to_sell <= 1e-12:
            break
    received *= 1 - fee_sell
    if to_sell > 1e-12:
        filled = False

    # Если купить на всю сумму не вышло, считаем прибыль от реально потраченного
    return DepthResult(amount_usd=spent, received_usd=received, filled=filled)


# ---------------------------------------------------------------------------
# Работа с биржами через ccxt
# ---------------------------------------------------------------------------


class Scanner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.exchanges: dict[str, ccxt.Exchange] = {}
        self._markets_loaded_at: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.last_result: ScanResult | None = None

        for name in settings.exchanges:
            cls = getattr(ccxt, name, None)
            if cls is None:
                log.error("Биржа %r не найдена в ccxt, пропускаю", name)
                continue
            self.exchanges[name] = cls({"enableRateLimit": True, "timeout": 20_000})

    def display_name(self, exchange_id: str) -> str:
        ex = self.exchanges.get(exchange_id)
        return getattr(ex, "name", None) or exchange_id

    async def close(self) -> None:
        await asyncio.gather(*(ex.close() for ex in self.exchanges.values()), return_exceptions=True)

    # --- загрузка котировок -------------------------------------------------

    async def _ensure_markets(self, name: str, ex: ccxt.Exchange) -> None:
        loaded_at = self._markets_loaded_at.get(name, 0)
        if time.time() - loaded_at > MARKETS_RELOAD_SEC:
            await ex.load_markets(reload=loaded_at > 0)
            self._markets_loaded_at[name] = time.time()

    async def _fetch_quotes(self, name: str, ex: ccxt.Exchange) -> dict[str, Quote]:
        await self._ensure_markets(name, ex)
        if not ex.has.get("fetchTickers"):
            raise RuntimeError("биржа не умеет отдавать все тикеры разом")

        # Берём только спотовые пары нужной валюты
        quote_ccy = self.settings.quote
        spot = {
            s: m
            for s, m in ex.markets.items()
            if m.get("spot") and m.get("quote") == quote_ccy and m.get("active") is not False
        }
        tickers = await ex.fetch_tickers()

        out: dict[str, Quote] = {}
        for symbol, t in tickers.items():
            market = spot.get(symbol)
            if market is None:
                continue
            bid, ask = t.get("bid"), t.get("ask")
            if not bid or not ask:
                continue
            volume = t.get("quoteVolume")
            if not volume and t.get("baseVolume") and t.get("last"):
                volume = t["baseVolume"] * t["last"]
            out[symbol] = Quote(
                exchange=name,
                symbol=symbol,
                bid=float(bid),
                ask=float(ask),
                volume_usd=float(volume or 0),
                taker_fee=float(market.get("taker") or DEFAULT_TAKER_FEE),
            )
        return out

    # --- проверка стакана ---------------------------------------------------

    async def _order_book(self, ex: ccxt.Exchange, symbol: str) -> dict:
        try:
            return await ex.fetch_order_book(symbol, limit=20)
        except Exception:
            # некоторые биржи принимают только определённые limit, пробуем без него
            return await ex.fetch_order_book(symbol)

    async def _check_depth(self, opp: Opportunity) -> None:
        try:
            buy_book, sell_book = await asyncio.gather(
                self._order_book(self.exchanges[opp.buy.exchange], opp.symbol),
                self._order_book(self.exchanges[opp.sell.exchange], opp.symbol),
            )
            opp.depth = simulate_trade(
                buy_book.get("asks") or [],
                sell_book.get("bids") or [],
                self.settings.trade_amount_usd,
                opp.buy.taker_fee,
                opp.sell.taker_fee,
            )
        except Exception as e:  # сеть, лимиты и т.п.
            opp.depth_error = short_error(e)

    # --- главный метод ------------------------------------------------------

    async def scan(self) -> ScanResult:
        """Один полный проход: котировки со всех бирж → поиск связок → проверка стаканов."""
        async with self._lock:  # не даём запустить два скана одновременно
            started = time.time()
            result = ScanResult()

            names = list(self.exchanges)
            fetched = await asyncio.gather(
                *(self._fetch_quotes(n, self.exchanges[n]) for n in names),
                return_exceptions=True,
            )
            quotes: dict[str, dict[str, Quote]] = {}
            for name, data in zip(names, fetched):
                if isinstance(data, BaseException):
                    result.errors[name] = short_error(data)
                    log.warning("%s: %s", name, data)
                else:
                    quotes[name] = data
                    result.exchanges_ok.append(name)

            result.opportunities, result.symbols_compared = find_opportunities(quotes, self.settings)

            top = result.opportunities[:DEPTH_CHECK_TOP]
            await asyncio.gather(*(self._check_depth(o) for o in top))

            result.duration_sec = time.time() - started
            result.finished_at = time.time()
            self.last_result = result
            return result


def fmt_price(p: float, significant: int = 6) -> str:
    """Цена без экспоненты: 0.0000123 вместо 1.23e-05."""
    if p <= 0:
        return "0"
    decimals = max(0, significant - 1 - math.floor(math.log10(p)))
    text = f"{p:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def short_error(e: BaseException) -> str:
    text = str(e).strip().replace("\n", " ") or type(e).__name__
    return f"{type(e).__name__}: {text[:150]}"
