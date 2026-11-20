"""Ценовые алерты: «сообщи, когда TON будет ниже 5$» или «когда SOL вырастет на 5%».

Алерт одноразовый: сработал → пришло сообщение → алерт удалён. Цены берутся с биржи публично,
ключи не нужны. Хранятся вместе с настройками (файл + резервная копия в Telegram).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

MAX_ALERTS = 30  # на одного пользователя

BELOW = {"<", "<=", "ниже", "меньше", "below", "under", "до"}
ABOVE = {">", ">=", "выше", "больше", "above", "over"}


class AlertError(Exception):
    """Понятная ошибка в тексте алерта."""


@dataclass
class PriceAlert:
    id: str
    coin: str
    op: str  # "<" — сработать, когда цена опустится до price; ">" — поднимется до price
    price: float
    exchange: str
    chat_id: int
    created_at: float
    start_price: float | None = None  # цена в момент создания (чтобы показать, сколько прошло)

    @property
    def symbol_text(self) -> str:
        return f"{self.coin} {'≤' if self.op == '<' else '≥'} {fmt_price(self.price)}"

    def hit(self, price: float) -> bool:
        return price <= self.price if self.op == "<" else price >= self.price


@dataclass
class AlertSpec:
    """Разобранная команда до того, как стала алертом (цена ещё может быть в процентах)."""

    coin: str
    op: str | None
    price: float | None
    pct: float | None
    exchange: str | None


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"${p:,.2f}".replace(",", " ")
    if p >= 1:
        return f"${p:.4f}".rstrip("0").rstrip(".")
    return f"${p:.8g}"


_NUM = re.compile(r"^([+-]?)\$?(\d+(?:[.,]\d+)?)\$?(%?)$")


def parse_alert(text: str, exchanges: list[str] | None = None) -> AlertSpec:
    """«TON < 5», «ton ниже 5 bybit», «BTC 100000», «SOL +5%», «sol -3%» → AlertSpec."""
    raw = text.replace("$", " ").replace("<=", " < ").replace(">=", " > ")
    raw = raw.replace("<", " < ").replace(">", " > ")
    parts = raw.split()
    if not parts:
        raise AlertError("пример: /alert TON < 5")
    coin = parts[0].upper().split("/")[0]
    if not re.fullmatch(r"[A-Z0-9]{1,15}", coin):
        raise AlertError(f"«{parts[0]}» не похоже на монету. Пример: /alert TON < 5")
    op = price = pct = exchange = None
    known = {e.lower() for e in (exchanges or [])}
    for p in parts[1:]:
        low = p.lower()
        if low in BELOW:
            op = "<"
        elif low in ABOVE:
            op = ">"
        elif low in known:
            exchange = low
        elif m := _NUM.match(p.replace(",", ".")):
            sign, num, is_pct = m.groups()
            value = float(num.replace(",", "."))
            if is_pct:
                if value <= 0 or value >= 100:
                    raise AlertError("процент от 0 до 100, например +5%")
                pct = -value if sign == "-" else value
                if not sign and op is None:
                    raise AlertError("для процента укажи направление: +5% (рост) или -5% (падение)")
            else:
                if sign:
                    raise AlertError("цена пишется без знака. Для изменения в процентах добавь %: SOL -5%")
                if value <= 0:
                    raise AlertError("цена должна быть больше нуля")
                price = value
        else:
            raise AlertError(f"не понял «{p}». Пример: /alert TON < 5 или /alert SOL +5%")
    if price is None and pct is None:
        raise AlertError("укажи цену или процент. Пример: /alert TON < 5")
    if pct is not None and op is not None and pct > 0:
        pct = -pct if op == "<" else pct
    return AlertSpec(coin, op, price, pct, exchange)


def build_alert(spec: AlertSpec, current: float, exchange: str, chat_id: int) -> PriceAlert:
    """Превращает команду в алерт: проценты считаются от текущей цены, направление угадывается."""
    if spec.pct is not None:
        price = current * (1 + spec.pct / 100)
        op = ">" if spec.pct > 0 else "<"
    else:
        price = spec.price
        op = spec.op or ("<" if price < current else ">")
    if op == "<" and current <= price:
        raise AlertError(f"{spec.coin} уже стоит {fmt_price(current)}, это не выше {fmt_price(price)}")
    if op == ">" and current >= price:
        raise AlertError(f"{spec.coin} уже стоит {fmt_price(current)}, это не ниже {fmt_price(price)}")
    return PriceAlert(id=secrets.token_hex(3), coin=spec.coin, op=op, price=float(price), exchange=exchange,
                      chat_id=chat_id, created_at=time.time(), start_price=current)


class AlertBook:
    def __init__(self) -> None:
        self.items: list[PriceAlert] = []

    def for_chat(self, chat_id: int) -> list[PriceAlert]:
        return [a for a in self.items if a.chat_id == chat_id]

    def add(self, alert: PriceAlert) -> None:
        if len(self.for_chat(alert.chat_id)) >= MAX_ALERTS:
            raise AlertError(f"не больше {MAX_ALERTS} алертов. Удали лишние: /alerts")
        self.items.append(alert)

    def remove(self, alert_id: str, chat_id: int | None = None) -> PriceAlert | None:
        for a in self.items:
            if a.id == alert_id and (chat_id is None or a.chat_id == chat_id):
                self.items.remove(a)
                return a
        return None

    def export(self) -> list[dict]:
        return [asdict(a) for a in self.items]

    def restore(self, raw) -> None:
        self.items = []
        for d in raw or []:
            try:
                self.items.append(PriceAlert(**{k: d[k] for k in PriceAlert.__dataclass_fields__ if k in d}))
            except (TypeError, KeyError):
                continue

    def move_from_removed(self, exchanges: list[str]) -> int:
        """Алерты на биржах, которых больше нет в боте, переносит на первую оставшуюся."""
        if not exchanges:
            return 0
        moved = 0
        for a in self.items:
            if a.exchange not in exchanges:
                a.exchange = exchanges[0]
                moved += 1
        return moved

    async def check(self, price_of) -> list[tuple[PriceAlert, float]]:
        """price_of(exchange, coin) → цена. Возвращает сработавшие алерты (и удаляет их)."""
        pairs = sorted({(a.exchange, a.coin) for a in self.items})
        if not pairs:
            return []

        async def one(pair):
            try:
                return pair, await price_of(*pair)
            except Exception as e:
                log.debug("Цена %s для алерта: %s", pair, e)
                return pair, None

        prices = dict(await asyncio.gather(*(one(p) for p in pairs)))
        fired = []
        for a in list(self.items):
            price = prices.get((a.exchange, a.coin))
            if price and a.hit(price):
                self.items.remove(a)
                fired.append((a, price))
        return fired


def fired_text(a: PriceAlert, price: float, exchange_title: str) -> str:
    word = "опустилась до" if a.op == "<" else "поднялась до"
    move = ""
    if a.start_price:
        change = (price / a.start_price - 1) * 100
        move = f"\nС момента создания: {change:+.2f}% (было {fmt_price(a.start_price)})"
    return (f"🔔 <b>{a.coin}</b>: цена {word} {fmt_price(a.price)}\n"
            f"Сейчас <b>{fmt_price(price)}</b> на {exchange_title}{move}")


def alerts_text(items: list[PriceAlert], title_of) -> str:
    if not items:
        return ("🔔 Алертов нет.\n\nПримеры:\n<code>/alert TON &lt; 5</code>: сообщу, когда TON будет ниже $5\n"
                "<code>/alert BTC &gt; 100000</code>: когда BTC поднимется выше $100 000\n"
                "<code>/alert SOL +5%</code> / <code>/alert SOL -3%</code>: рост или падение от текущей цены\n"
                "Можно указать биржу: <code>/alert TON &lt; 5 bingx</code>")
    lines = ["🔔 <b>Твои алерты</b>"]
    for a in items:
        lines.append(f"• {a.symbol_text} на {title_of(a.exchange)}")
    lines.append("\nАлерт срабатывает один раз. Проверка каждые 30 секунд.")
    return "\n".join(lines)
