"""Треугольный арбитраж внутри ОДНОЙ биржи.

Пример цикла: USDT → PEPE → BTC → USDT
  1) покупаем PEPE за USDT   (пара PEPE/USDT, по цене ask)
  2) продаём PEPE за BTC      (пара PEPE/BTC,  по цене bid)
  3) продаём BTC за USDT      (пара BTC/USDT,  по цене bid)
Если в конце USDT стало больше, чем в начале (с учётом трёх комиссий), это возможность.

Главный плюс для маленьких сумм: деньги не покидают биржу,
поэтому нет комиссии за вывод и не нужно ждать перевода.
"""

from __future__ import annotations

from dataclasses import dataclass

from .sim import DepthResult, simulate_buy, simulate_sell

DEFAULT_TAKER_FEE = 0.001


@dataclass
class TriLeg:
    symbol: str  # торговая пара, например "PEPE/BTC"
    side: str  # "buy" = отдаём quote, получаем base; "sell" = отдаём base, получаем quote
    price: float  # ask для buy, bid для sell
    fee: float
    volume_usd: float
    from_ccy: str
    to_ccy: str

    def rate(self) -> float:
        """Сколько единиц to_ccy получаем за 1 единицу from_ccy после комиссии."""
        gross = 1 / self.price if self.side == "buy" else self.price
        return gross * (1 - self.fee)


@dataclass
class TriOpportunity:
    exchange: str
    legs: list[TriLeg]
    gross_percent: float
    net_percent: float
    depth: DepthResult | None = None
    depth_error: str | None = None

    @property
    def path(self) -> list[str]:
        return [self.legs[0].from_ccy] + [leg.to_ccy for leg in self.legs]

    @property
    def path_text(self) -> str:
        return " → ".join(self.path)

    @property
    def key(self) -> tuple[str, str, str]:
        return ("tri", self.exchange, self.path_text)


def usd_prices(tickers: dict[str, dict], quote: str = "USDT") -> dict[str, float]:
    """Цена каждой монеты в USDT по средней цене пары X/USDT."""
    prices = {quote: 1.0}
    for symbol, t in tickers.items():
        base, _, q = symbol.partition("/")
        bid, ask = t.get("bid"), t.get("ask")
        if not bid or not ask:
            continue
        mid = (float(bid) + float(ask)) / 2
        if q == quote:
            prices.setdefault(base, mid)
        elif base == quote and mid > 0:
            prices.setdefault(q, 1 / mid)
    return prices


def _volume_usd(t: dict, quote_ccy: str, prices: dict[str, float]) -> float:
    vol = t.get("quoteVolume")
    if not vol and t.get("baseVolume") and t.get("last"):
        vol = float(t["baseVolume"]) * float(t["last"])
    return float(vol or 0) * prices.get(quote_ccy, 0.0)


def find_triangles(
    exchange: str,
    markets: dict[str, dict],
    tickers: dict[str, dict],
    start: str = "USDT",
    min_profit_percent: float = 0.2,
    max_profit_percent: float = 5.0,
    min_volume_usd: float = 50_000,
    limit: int = 50,
    near: list | None = None,
) -> list[TriOpportunity]:
    """Ищет все циклы start → A → B → start с прибылью в заданном диапазоне.

    markets: рынки биржи из ccxt (нужны base/quote/spot/active/taker)
    tickers: {символ: тикер ccxt} — только спот
    near: если передан список, в него кладётся лучший цикл биржи (даже убыточный)
    """
    best_any: TriOpportunity | None = None
    prices = usd_prices(tickers, start)

    # Граф обменов: edges[откуда][куда] = TriLeg
    edges: dict[str, dict[str, TriLeg]] = {}
    for symbol, t in tickers.items():
        m = markets.get(symbol)
        if not m or not m.get("spot") or m.get("active") is False:
            continue
        bid, ask = t.get("bid"), t.get("ask")
        if not bid or not ask or float(bid) <= 0 or float(ask) <= 0 or float(bid) > float(ask):
            continue
        base, quote = m.get("base"), m.get("quote")
        if not base or not quote:
            continue
        vol = _volume_usd(t, quote, prices)
        if vol < min_volume_usd:
            continue
        fee = float(m.get("taker") or DEFAULT_TAKER_FEE)
        edges.setdefault(quote, {})[base] = TriLeg(symbol, "buy", float(ask), fee, vol, quote, base)
        edges.setdefault(base, {})[quote] = TriLeg(symbol, "sell", float(bid), fee, vol, base, quote)

    result: list[TriOpportunity] = []
    for a, leg1 in edges.get(start, {}).items():
        for b, leg2 in edges.get(a, {}).items():
            if b == start:
                continue
            leg3 = edges.get(b, {}).get(start)
            if leg3 is None:
                continue
            gross = 1.0
            for leg in (leg1, leg2, leg3):
                gross *= (1 / leg.price) if leg.side == "buy" else leg.price
            net = leg1.rate() * leg2.rate() * leg3.rate()
            gross_pct, net_pct = (gross - 1) * 100, (net - 1) * 100
            if (
                near is not None
                and gross_pct <= max_profit_percent
                and (best_any is None or net_pct > best_any.net_percent)
            ):
                best_any = TriOpportunity(exchange, [leg1, leg2, leg3], gross_pct, net_pct)
            if net_pct < min_profit_percent or gross_pct > max_profit_percent:
                continue
            result.append(TriOpportunity(exchange, [leg1, leg2, leg3], gross_pct, net_pct))

    result.sort(key=lambda o: o.net_percent, reverse=True)
    if near is not None and best_any is not None:
        near.append(best_any)
    return result[:limit]


def simulate_path(books: list[dict], legs: list[TriLeg], amount: float) -> DepthResult:
    """Проводит сумму по трём стаканам подряд. amount в стартовой валюте (USDT).

    Если на первом шаге стакана не хватило, непотраченные USDT остаются на счёте
    и прибавляются к результату. Если не хватило на 2-м или 3-м шаге, застрявшие
    монеты считаются потерянными (пессимистичная оценка: в жизни их пришлось бы
    продавать по худшей цене).
    """
    current = amount
    unspent_start = 0.0
    filled = True
    for i, (leg, book) in enumerate(zip(legs, books)):
        if leg.side == "buy":
            spent, out = simulate_buy(book.get("asks") or [], current, leg.fee)
            left = current - spent
        else:
            out, left = simulate_sell(book.get("bids") or [], current, leg.fee)
        if left > current * 1e-9:
            filled = False
            if i == 0:
                unspent_start = left
        current = out
    return DepthResult(amount_usd=amount, received_usd=current + unspent_start, filled=filled)


def leg_min_order_ok(leg: TriLeg, amount_in: float, market: dict) -> bool:
    """Проходит ли нога по минимальному ордеру. amount_in в валюте from_ccy."""
    limits = market.get("limits") or {}
    min_cost = float((limits.get("cost") or {}).get("min") or 0)
    min_amount = float((limits.get("amount") or {}).get("min") or 0)
    if leg.side == "buy":
        cost, base_amount = amount_in, amount_in / leg.price
    else:
        cost, base_amount = amount_in * leg.price, amount_in
    return cost >= min_cost and base_amount >= min_amount


def path_min_order_ok(legs: list[TriLeg], amount: float, markets: dict[str, dict]) -> bool:
    current = amount
    for leg in legs:
        if not leg_min_order_ok(leg, current, markets.get(leg.symbol, {})):
            return False
        current *= leg.rate()
    return True


def path_min_amount(legs: list[TriLeg], markets: dict[str, dict]) -> float:
    """Минимальная стартовая сумма (в валюте первой ноги), с которой проходят ВСЕ ноги цикла.

    Биржа не примет ордер меньше min cost (обычно $1–5) или min amount. Считаем, сколько нужно
    на входе, чтобы каждая нога была не меньше своего минимума, и берём самое строгое.
    """
    need, factor = 0.0, 1.0  # factor: сколько from_ccy текущей ноги приходится на 1 единицу старта
    for leg in legs:
        limits = (markets.get(leg.symbol) or {}).get("limits") or {}
        min_cost = float((limits.get("cost") or {}).get("min") or 0)
        min_amount = float((limits.get("amount") or {}).get("min") or 0)
        if leg.side == "buy":  # отдаём quote
            min_in = max(min_cost, min_amount * leg.price)
        else:  # отдаём base
            min_in = max(min_amount, min_cost / leg.price if leg.price else 0)
        if factor > 0:
            need = max(need, min_in / factor)
        factor *= leg.rate()
    return need
