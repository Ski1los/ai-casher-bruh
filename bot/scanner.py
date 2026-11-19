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
from .sim import DepthResult, simulate_buy, simulate_sell  # noqa: F401 (реэкспорт)
from .triangular import TriOpportunity, find_triangles, simulate_path

log = logging.getLogger(__name__)

DEFAULT_TAKER_FEE = 0.001  # 0.1%, если биржа не сообщила комиссию
MARKETS_RELOAD_SEC = 3600  # раз в час обновляем список пар

# Грузим только спот: без фьючерсов и опционов меньше памяти (важно для бесплатного
# хостинга с 512 МБ) и меньше запросов к бирже. А defaultType="spot" важен для Bybit:
# по умолчанию он отдаёт тикеры фьючерсов, и спотовые цены просто не попадали бы в поиск.
SPOT_ONLY_OPTIONS: dict[str, dict] = {
    "bybit": {"fetchMarkets": {"types": ["spot"]}},
    "okx": {"fetchMarkets": {"types": ["spot"]}},
    "gate": {"fetchMarkets": {"types": ["spot"]}},
    "kucoin": {"fetchMarkets": {"types": ["spot"]}},
    "bitget": {"fetchMarkets": {"types": ["spot"]}},
    "htx": {"fetchMarkets": {"types": {"spot": True, "linear": False, "inverse": False}}},
}


def make_exchange(name: str):
    """Создаёт биржу ccxt, настроенную только на спот. None, если такой биржи нет."""
    cls = getattr(ccxt, name, None)
    if cls is None:
        return None
    options = {"defaultType": "spot", **SPOT_ONLY_OPTIONS.get(name, {})}
    ex = cls({"enableRateLimit": True, "timeout": 20_000, "options": options})
    # MEXC игнорирует опцию типов и всегда грузит ещё и фьючерсы, отключаем напрямую
    if name == "mexc" and hasattr(ex, "fetch_spot_markets"):
        ex.fetch_markets = ex.fetch_spot_markets
    return ex


DEPTH_CHECK_TOP = 10  # для скольких лучших вариантов проверять стакан
TRI_DEPTH_CHECK_TOP = 5  # то же для треугольников (по 3 стакана на каждый)


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
    triangles: list[TriOpportunity] = field(default_factory=list)
    exchanges_ok: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    symbols_compared: int = 0
    duration_sec: float = 0.0
    finished_at: float = 0.0
    # «Почти получилось»: лучший вариант скана, даже если он ниже порога или в минусе.
    # Показывает, насколько рынок близок к прибыли и сколько съедают комиссии.
    near_cross: NearMiss | None = None
    near_tri: NearMiss | None = None


@dataclass
class NearMiss:
    net_percent: float  # после торговых комиссий
    gross_percent: float  # до комиссий
    text: str  # «PEPE/USDT bybit→okx» или «USDT → A → B → USDT на okx»


# ---------------------------------------------------------------------------
# Чистая логика (без сети). Её легко тестировать.
# ---------------------------------------------------------------------------


def net_percent(ask: float, bid: float, fee_buy: float, fee_sell: float) -> float:
    """Прибыль в % после комиссий: купили по ask, продали по bid."""
    return ((bid / ask) * (1 - fee_buy) * (1 - fee_sell) - 1) * 100


def find_opportunities(
    quotes: dict[str, dict[str, Quote]],
    settings: Settings,
    near: list | None = None,
) -> tuple[list[Opportunity], int]:
    """Ищет лучшие связки «купить здесь, продать там».

    quotes: {биржа: {символ: Quote}}
    Возвращает (список возможностей по убыванию прибыли, сколько пар сравнили).
    near: если передан список, в него кладётся лучший вариант скана (NearMiss), даже убыточный.
    """
    best_any: NearMiss | None = None
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
                if near is not None and (best_any is None or net > best_any.net_percent):
                    best_any = NearMiss(net, gross, f"{symbol} {buy.exchange}→{sell.exchange}")
                if net < settings.min_profit_percent:
                    continue
                if best is None or net > best.net_percent:
                    best = Opportunity(symbol, buy, sell, gross, net)
        if best:
            result.append(best)

    result.sort(key=lambda o: o.net_percent, reverse=True)
    if near is not None and best_any is not None:
        near.append(best_any)
    return result, compared


def simulate_trade(
    asks: list[list[float]],
    bids: list[list[float]],
    amount_usd: float,
    fee_buy: float,
    fee_sell: float,
    transfer_fee_coins: float = 0.0,
) -> DepthResult:
    """Проходит по стаканам: покупает на amount_usd по asks, продаёт по bids.

    asks/bids: [[цена, количество], ...], лучшие цены первыми (как отдаёт ccxt).
    transfer_fee_coins: сколько монет съест перевод между биржами.
    """
    spent, coins = simulate_buy(asks, amount_usd, fee_buy)
    coins = max(coins - transfer_fee_coins, 0.0)
    received, left = simulate_sell(bids, coins, fee_sell)
    filled = spent >= amount_usd - 1e-9 and left <= 1e-12
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
            ex = make_exchange(name)
            if ex is None:
                log.error("Биржа %r не найдена в ccxt, пропускаю", name)
                continue
            self.exchanges[name] = ex

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
            self.apply_fee_overrides(name)

    def apply_fee_overrides(self, name: str | None = None) -> None:
        """Подставляет свои комиссии (FEE_OVERRIDES или /fee) во все пары биржи.

        Исходная комиссия ccxt запоминается, чтобы её можно было вернуть.
        Все расчёты (сканер, треугольники, демо) берут комиссию из market["taker"],
        так что замена действует везде сразу.
        """
        names = [name] if name else list(self.exchanges)
        for n in names:
            ex = self.exchanges.get(n)
            if ex is None:
                continue
            override = self.settings.fee_overrides.get(n)
            for m in (getattr(ex, "markets", None) or {}).values():
                if "_ccxt_taker" not in m:
                    m["_ccxt_taker"] = m.get("taker")
                m["taker"] = override if override is not None else m["_ccxt_taker"]

    async def _fetch_spot_tickers(self, name: str, ex: ccxt.Exchange) -> dict[str, dict]:
        """Все спотовые тикеры биржи (любые котируемые валюты) одним запросом."""
        await self._ensure_markets(name, ex)
        if not ex.has.get("fetchTickers"):
            raise RuntimeError("биржа не умеет отдавать все тикеры разом")
        tickers = await ex.fetch_tickers()
        markets = ex.markets or {}
        return {
            s: t
            for s, t in tickers.items()
            if (m := markets.get(s)) and m.get("spot") and m.get("active") is not False
        }

    def _quotes_from_tickers(self, name: str, tickers: dict[str, dict]) -> dict[str, Quote]:
        """Котировки пар к основной валюте (USDT) для межбиржевого сравнения."""
        markets = self.exchanges[name].markets or {}
        out: dict[str, Quote] = {}
        for symbol, t in tickers.items():
            market = markets.get(symbol) or {}
            if market.get("quote") != self.settings.quote:
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

    async def order_book(self, exchange_id: str, symbol: str) -> dict:
        return await self._order_book(self.exchanges[exchange_id], symbol)

    def market(self, exchange_id: str, symbol: str) -> dict:
        ex = self.exchanges.get(exchange_id)
        return (ex.markets or {}).get(symbol, {}) if ex else {}

    def currency(self, exchange_id: str, code: str) -> dict:
        """Инфо о монете: статус ввода/вывода и комиссии вывода.

        Многие биржи отдают это только с API-ключом, тогда вернётся пустой dict.
        """
        ex = self.exchanges.get(exchange_id)
        return (getattr(ex, "currencies", None) or {}).get(code) or {} if ex else {}

    def withdraw_fee_coins(self, exchange_id: str, code: str) -> float | None:
        """Самая дешёвая комиссия вывода монеты (в монетах) среди открытых сетей. None = неизвестно."""
        cur = self.currency(exchange_id, code)
        fees = []
        for net in (cur.get("networks") or {}).values():
            if net.get("withdraw") is False:
                continue
            if net.get("fee") is not None:
                fees.append(float(net["fee"]))
        if fees:
            return min(fees)
        return float(cur["fee"]) if cur.get("fee") is not None else None

    def can_withdraw(self, exchange_id: str, code: str) -> bool | None:
        return self.currency(exchange_id, code).get("withdraw")

    def can_deposit(self, exchange_id: str, code: str) -> bool | None:
        return self.currency(exchange_id, code).get("deposit")

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

    async def order_books(self, exchange_id: str, symbols: list[str]) -> list[dict]:
        return list(await asyncio.gather(*(self.order_book(exchange_id, s) for s in symbols)))

    async def _check_tri_depth(self, opp: TriOpportunity) -> None:
        try:
            books = await self.order_books(opp.exchange, [leg.symbol for leg in opp.legs])
            opp.depth = simulate_path(books, opp.legs, self.settings.trade_amount_usd)
        except Exception as e:
            opp.depth_error = short_error(e)

    # --- главный метод ------------------------------------------------------

    async def scan(self) -> ScanResult:
        """Один полный проход: котировки со всех бирж → поиск связок → проверка стаканов."""
        async with self._lock:  # не даём запустить два скана одновременно
            started = time.time()
            result = ScanResult()

            names = list(self.exchanges)
            fetched = await asyncio.gather(
                *(self._fetch_spot_tickers(n, self.exchanges[n]) for n in names),
                return_exceptions=True,
            )
            quotes: dict[str, dict[str, Quote]] = {}
            near_tri: list = []
            for name, data in zip(names, fetched):
                if isinstance(data, BaseException):
                    result.errors[name] = short_error(data)
                    log.warning("%s: %s", name, data)
                    continue
                result.exchanges_ok.append(name)
                quotes[name] = self._quotes_from_tickers(name, data)
                if self.settings.triangular_enabled:
                    result.triangles += find_triangles(
                        name,
                        self.exchanges[name].markets or {},
                        data,
                        start=self.settings.quote,
                        min_profit_percent=self.settings.tri_min_profit_percent,
                        max_profit_percent=self.settings.tri_max_profit_percent,
                        min_volume_usd=self.settings.min_volume_usd,
                        near=near_tri,
                    )

            near_cross: list = []
            result.opportunities, result.symbols_compared = find_opportunities(quotes, self.settings, near_cross)
            result.near_cross = near_cross[0] if near_cross else None
            if near_tri:
                best = max(near_tri, key=lambda x: x.net_percent)
                result.near_tri = NearMiss(best.net_percent, best.gross_percent, f"{best.path_text} на {best.exchange}")
            result.triangles.sort(key=lambda o: o.net_percent, reverse=True)

            await asyncio.gather(
                *(self._check_depth(o) for o in result.opportunities[:DEPTH_CHECK_TOP]),
                *(self._check_tri_depth(o) for o in result.triangles[:TRI_DEPTH_CHECK_TOP]),
            )

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
