"""Фейковая биржа с балансами, комиссиями, округлением и стаканами — как настоящая ccxt, но без сети."""

from __future__ import annotations

import math

import ccxt.async_support as ccxt


def mk_market(base, quote, taker=0.001, min_cost=1.0, min_amount=0.0):
    return {"spot": True, "active": True, "base": base, "quote": quote, "taker": taker, "maker": taker,
            "limits": {"cost": {"min": min_cost}, "amount": {"min": min_amount}}}


class FakeTradingExchange:
    has = {"fetchTickers": True}

    def __init__(self, name, markets, tickers, balances=None, depth=1e12, terse_orders=False):
        self.name = name
        self.markets = markets
        self.tickers = tickers  # {symbol: {"bid", "ask", "quoteVolume"}}
        self.currencies = {}
        self.options = {}
        self.balance = dict(balances or {})  # свободные средства
        self.locked: dict[str, float] = {}
        self.depth = depth
        self.terse_orders = terse_orders  # True = create_order возвращает только id (как Bybit/OKX)
        self.orders: dict[str, dict] = {}
        self.created: list[tuple] = []  # журнал отправленных ордеров
        self.fail_symbols: set[str] = set()
        self.apiKey = None
        self.secret = None
        self.password = None
        self.auth_error = False

    # --- публичное ----------------------------------------------------------

    async def load_markets(self, reload=False):
        return self.markets

    async def fetch_tickers(self, symbols=None):
        return self.tickers

    async def fetch_ticker(self, symbol):
        return self.tickers[symbol]

    async def fetch_order_book(self, symbol, limit=None):
        t = self.tickers[symbol]
        return {"asks": [[t["ask"], self.depth]], "bids": [[t["bid"], self.depth]]}

    async def close(self):
        pass

    # --- точность -----------------------------------------------------------

    @staticmethod
    def amount_to_precision(symbol, amount):
        return str(math.floor(amount * 1e6) / 1e6)

    @staticmethod
    def cost_to_precision(symbol, cost):
        return str(math.floor(cost * 1e8) / 1e8)

    @staticmethod
    def price_to_precision(symbol, price):
        return f"{price:.10g}"

    # --- приватное ----------------------------------------------------------

    def _auth(self):
        if self.auth_error:
            raise ccxt.AuthenticationError("Invalid API key")
        if not self.apiKey:
            raise ccxt.AuthenticationError("apiKey required")

    async def fetch_balance(self):
        self._auth()
        total = {c: self.balance.get(c, 0) + self.locked.get(c, 0) for c in set(self.balance) | set(self.locked)}
        return {"free": dict(self.balance), "used": dict(self.locked), "total": total}

    def _take(self, ccy, amount):
        have = self.balance.get(ccy, 0)
        if amount > have + 1e-12:
            raise ccxt.InsufficientFunds(f"need {amount} {ccy}, have {have}")
        self.balance[ccy] = have - amount

    def _give(self, ccy, amount):
        self.balance[ccy] = self.balance.get(ccy, 0) + amount

    def _new_order(self, order):
        oid = str(len(self.orders) + 1)
        order["id"] = oid
        self.orders[oid] = order
        return {"id": oid, "info": {}} if self.terse_orders else dict(order)

    async def create_market_buy_order_with_cost(self, symbol, cost, params=None):
        self._auth()
        self.created.append(("buy_cost", symbol, cost))
        if symbol in self.fail_symbols:
            raise ccxt.InvalidOrder(f"{symbol} order rejected")
        m = self.markets[symbol]
        price = self.tickers[symbol]["ask"]
        self._take(m["quote"], cost)
        filled = cost / price
        fee = filled * m["taker"]
        self._give(m["base"], filled - fee)
        return self._new_order({"symbol": symbol, "side": "buy", "type": "market", "status": "closed",
                                "filled": filled, "cost": cost, "average": price,
                                "fee": {"currency": m["base"], "cost": fee}})

    async def create_market_sell_order(self, symbol, amount, params=None):
        self._auth()
        self.created.append(("sell", symbol, amount))
        if symbol in self.fail_symbols:
            raise ccxt.InvalidOrder(f"{symbol} order rejected")
        m = self.markets[symbol]
        price = self.tickers[symbol]["bid"]
        self._take(m["base"], amount)
        cost = amount * price
        fee = cost * m["taker"]
        self._give(m["quote"], cost - fee)
        return self._new_order({"symbol": symbol, "side": "sell", "type": "market", "status": "closed",
                                "filled": amount, "cost": cost, "average": price,
                                "fee": {"currency": m["quote"], "cost": fee}})

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        self._auth()
        assert type == "limit"
        self.created.append(("limit", symbol, side, amount, price))
        m = self.markets[symbol]
        ccy, need = (m["quote"], amount * price) if side == "buy" else (m["base"], amount)
        self._take(ccy, need)
        self.locked[ccy] = self.locked.get(ccy, 0) + need
        return self._new_order({"symbol": symbol, "side": side, "type": "limit", "status": "open",
                                "amount": amount, "price": price, "filled": 0.0, "cost": 0.0, "_lock": (ccy, need)})

    async def fetch_order(self, oid, symbol=None, params=None):
        self._auth()
        return dict(self.orders[oid])

    async def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self._auth()
        return [dict(o) for o in self.orders.values() if o["status"] == "open"]

    async def cancel_order(self, oid, symbol=None, params=None):
        self._auth()
        o = self.orders.get(oid)
        if o is None or o["status"] != "open":
            raise ccxt.OrderNotFound(oid)
        o["status"] = "canceled"
        ccy, need = o["_lock"]
        self.locked[ccy] -= need
        self._give(ccy, need)
        return dict(o)


def tri_exchange(balances=None, **kw) -> FakeTradingExchange:
    """Биржа с прибыльным треугольником USDT → PEPE → BTC → USDT (≈ +2% до комиссий)."""
    markets = {"BTC/USDT": mk_market("BTC", "USDT"), "PEPE/USDT": mk_market("PEPE", "USDT"),
               "PEPE/BTC": mk_market("PEPE", "BTC", min_cost=0.00002)}
    tickers = {
        "BTC/USDT": {"bid": 50_000, "ask": 50_001, "quoteVolume": 1e7},
        "PEPE/USDT": {"bid": 0.0000099, "ask": 0.00001, "quoteVolume": 1e7},
        "PEPE/BTC": {"bid": 2.04e-10, "ask": 2.05e-10, "quoteVolume": 200},
    }
    return FakeTradingExchange("ExA", markets, tickers, balances if balances is not None else {"USDT": 100.0}, **kw)
