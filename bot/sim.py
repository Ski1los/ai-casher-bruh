"""Симуляция сделок по стаканам: общие функции для всех стратегий."""

from __future__ import annotations

from dataclasses import dataclass


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


def simulate_buy(asks: list[list[float]], amount_usd: float, fee: float) -> tuple[float, float]:
    """Покупка на amount_usd по стакану asks. Возвращает (потрачено $, получено монет после комиссии)."""
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
    return spent, coins * (1 - fee)


def simulate_sell(bids: list[list[float]], coins: float, fee: float) -> tuple[float, float]:
    """Продажа монет по стакану bids. Возвращает (получено $ после комиссии, непроданный остаток монет)."""
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
    return received * (1 - fee), max(to_sell, 0.0)
