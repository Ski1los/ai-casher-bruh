"""Демо-счёт: торгуем виртуальными деньгами по реальным ценам бирж.

Что учитывается, чтобы не было «фейковой» прибыли:
  * торговые комиссии обеих бирж (taker);
  * реальная глубина стакана: покупаем и продаём по заявкам, а не по одной «красивой» цене;
  * минимальная сумма и количество ордера на бирже;
  * комиссия за вывод монеты (берётся с биржи, если она её отдаёт публично, иначе из настроек);
  * статус ввода/вывода, если биржа его сообщает;
  * время перевода: монета продаётся по цене, которая будет через DEMO_TRANSFER_MIN минут,
    а не по той, что была в момент сигнала;
  * деньги физически лежат на одной бирже: после продажи на бирже B
    следующая покупка возможна только на бирже B.

Вся прибыль реинвестируется: следующая сделка идёт на весь баланс (сложный процент).
"""

from __future__ import annotations

import html
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings
from .scanner import Opportunity, Scanner, ScanResult, fmt_price, short_error, simulate_buy, simulate_sell

log = logging.getLogger(__name__)

DEMO_FILE = Path(__file__).resolve().parent.parent / "data" / "demo.json"
MAX_CANDIDATES_PER_SCAN = 5  # сколько связок за скан проверять по стаканам (бережём лимиты API)

# Причины отказа: код -> человеческое описание
REASONS = {
    "location": "деньги на другой бирже",
    "wallet": "закрыт вывод или ввод монеты",
    "min_order": "баланс меньше минимального ордера",
    "fees": "комиссия вывода съедает прибыль",
    "depth": "после проверки стакана прибыли нет",
    "error": "ошибка запроса к бирже",
}


@dataclass
class DemoTrade:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    opened_at: float
    cost_usd: float  # сколько потратили
    buy_price: float  # средняя цена покупки
    coins_bought: float  # после торговой комиссии
    withdraw_fee_coins: float
    withdraw_fee_known: bool  # True = комиссию сообщила биржа, False = взяли из настроек
    coins_arrived: float  # дошло до второй биржи
    expected_usd: float  # сколько ожидали получить в момент входа
    signal_percent: float  # что обещал сигнал
    closed_at: float | None = None
    received_usd: float | None = None
    sell_price: float | None = None

    @property
    def profit_usd(self) -> float:
        return (self.received_usd or 0) - self.cost_usd

    @property
    def profit_percent(self) -> float:
        return self.profit_usd / self.cost_usd * 100 if self.cost_usd else 0.0


@dataclass
class DemoAccount:
    active: bool = False
    chat_id: int | None = None
    start_balance: float = 0.0
    balance: float = 0.0  # свободные USDT
    location: str | None = None  # на какой бирже лежат USDT (None = ещё не выбрали)
    created_at: float = 0.0
    open_trade: DemoTrade | None = None
    trades: list[DemoTrade] = field(default_factory=list)
    rejects: dict[str, int] = field(default_factory=dict)
    scans: int = 0
    best_rejected: str | None = None  # пример лучшей связки, которая не прошла
    best_rejected_percent: float = 0.0

    @property
    def equity(self) -> float:
        """Сколько всего денег, считая монету в пути по цене покупки."""
        return self.balance + (self.open_trade.cost_usd if self.open_trade else 0.0)


# ---------------------------------------------------------------------------
# Сохранение в файл
# ---------------------------------------------------------------------------


def load_account(path: Path = DEMO_FILE) -> DemoAccount:
    if not path.exists():
        return DemoAccount()
    try:
        raw = json.loads(path.read_text("utf-8"))
        open_trade = raw.pop("open_trade", None)
        trades = raw.pop("trades", [])
        acc = DemoAccount(**raw)
        acc.open_trade = DemoTrade(**open_trade) if open_trade else None
        acc.trades = [DemoTrade(**t) for t in trades]
        return acc
    except (ValueError, TypeError) as e:
        log.warning("Не удалось прочитать %s (%s), начинаю заново", path, e)
        return DemoAccount()


def save_account(acc: DemoAccount, path: Path = DEMO_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(acc), ensure_ascii=False, indent=1), "utf-8")


# ---------------------------------------------------------------------------
# Движок
# ---------------------------------------------------------------------------


def _limit(market: dict, kind: str) -> float:
    value = ((market.get("limits") or {}).get(kind) or {}).get("min")
    return float(value) if value else 0.0


class DemoEngine:
    def __init__(self, scanner: Scanner, settings: Settings, path: Path = DEMO_FILE):
        self.scanner = scanner
        self.settings = settings
        self.path = path
        self.acc = load_account(path)
        self._counted: set[str] = set()  # чтобы одна связка не считалась в отказах много раз

    @property
    def active(self) -> bool:
        return self.acc.active

    def start(self, amount: float, chat_id: int | None = None) -> None:
        self.acc = DemoAccount(
            active=True, chat_id=chat_id, start_balance=amount, balance=amount, created_at=time.time()
        )
        self._counted.clear()
        save_account(self.acc, self.path)

    def stop(self) -> None:
        self.acc.active = False
        save_account(self.acc, self.path)

    def _reject(self, opp: Opportunity, reason: str, note: str | None = None) -> None:
        tag = f"{opp.symbol}|{opp.buy.exchange}|{opp.sell.exchange}|{reason}"
        if tag not in self._counted:
            self._counted.add(tag)
            self.acc.rejects[reason] = self.acc.rejects.get(reason, 0) + 1
        if note and reason in ("fees", "depth", "min_order") and opp.net_percent > self.acc.best_rejected_percent:
            self.acc.best_rejected_percent = opp.net_percent
            self.acc.best_rejected = note

    # --- шаг 1: закрыть сделку, если монета «доехала» -----------------------

    async def _try_close(self) -> list[str]:
        t = self.acc.open_trade
        if t is None or time.time() - t.opened_at < self.settings.demo_transfer_min * 60:
            return []
        try:
            book = await self.scanner.order_book(t.sell_exchange, t.symbol)
        except Exception as e:
            log.warning("Демо: не смог получить стакан для закрытия: %s", e)
            return []
        bids = book.get("bids") or []
        fee = float(self.scanner.market(t.sell_exchange, t.symbol).get("taker") or 0.001)
        received, left = simulate_sell(bids, t.coins_arrived, fee)
        if left > 0 and bids:  # стакан тонкий: остаток продаём по худшей видимой цене
            received += left * float(bids[-1][0]) * (1 - fee)

        t.closed_at = time.time()
        t.received_usd = received
        t.sell_price = received / (t.coins_arrived * (1 - fee)) if t.coins_arrived else 0
        self.acc.balance = received
        self.acc.location = t.sell_exchange
        self.acc.trades.append(t)
        self.acc.open_trade = None
        save_account(self.acc, self.path)

        icon = "✅" if t.profit_usd >= 0 else "🔻"
        total = (self.acc.balance / self.acc.start_balance - 1) * 100
        return [
            f"{icon} <b>Демо: сделка закрыта</b> {html.escape(t.symbol)}\n"
            f"Продано на {html.escape(self.scanner.display_name(t.sell_exchange))} по {fmt_price(t.sell_price)}\n"
            f"Результат: {t.profit_usd:+.4f}$ ({t.profit_percent:+.2f}%), "
            f"сигнал обещал {t.signal_percent:+.2f}%\n"
            f"Баланс: <b>${self.acc.balance:.4f}</b> (всего {total:+.2f}% от старта)"
        ]

    # --- шаг 2: открыть новую сделку ----------------------------------------

    async def _evaluate(self, opp: Opportunity) -> DemoTrade | None:
        s = self.settings
        balance = self.acc.balance
        base = opp.symbol.split("/")[0]
        buy_ex, sell_ex = opp.buy.exchange, opp.sell.exchange

        if self.acc.location and buy_ex != self.acc.location:
            self._reject(opp, "location")
            return None

        if self.scanner.can_withdraw(buy_ex, base) is False or self.scanner.can_deposit(sell_ex, base) is False:
            self._reject(opp, "wallet")
            return None

        buy_market = self.scanner.market(buy_ex, opp.symbol)
        sell_market = self.scanner.market(sell_ex, opp.symbol)
        min_cost = max(_limit(buy_market, "cost"), _limit(buy_market, "amount") * opp.buy.ask)
        if balance < min_cost:
            self._reject(opp, "min_order", f"{opp.symbol}: мин. ордер ${min_cost:.2f}, а баланс ${balance:.2f}")
            return None

        fee_coins = self.scanner.withdraw_fee_coins(buy_ex, base)
        fee_known = fee_coins is not None
        if fee_coins is None:
            fee_coins = s.demo_withdraw_fee_usd / opp.buy.ask
        fee_usd = fee_coins * opp.buy.ask

        # Быстрая оценка без запросов к бирже
        expected_profit = balance * opp.net_percent / 100 - fee_usd
        if expected_profit < balance * s.min_profit_percent / 100:
            self._reject(
                opp,
                "fees",
                f"{opp.symbol} {self.scanner.display_name(buy_ex)}→{self.scanner.display_name(sell_ex)}: "
                f"спред +{opp.net_percent:.2f}% (${balance * opp.net_percent / 100:.3f}), "
                f"а вывод стоит ${fee_usd:.3f} = {fee_usd / balance * 100:.1f}% от баланса",
            )
            return None

        # Точная проверка по стаканам
        try:
            buy_book = await self.scanner.order_book(buy_ex, opp.symbol)
            sell_book = await self.scanner.order_book(sell_ex, opp.symbol)
        except Exception as e:
            self._reject(opp, "error")
            log.info("Демо: %s", short_error(e))
            return None

        spent, coins = simulate_buy(buy_book.get("asks") or [], balance, opp.buy.taker_fee)
        arrived = coins - fee_coins
        if spent < balance * 0.999 or arrived <= 0:
            self._reject(opp, "depth")
            return None
        if arrived < _limit(sell_market, "amount") or arrived * opp.sell.bid < _limit(sell_market, "cost"):
            self._reject(opp, "min_order", f"{opp.symbol}: после вывода монет меньше минимального ордера на продажу")
            return None
        received, left = simulate_sell(sell_book.get("bids") or [], arrived, opp.sell.taker_fee)
        profit_pct = (received / spent - 1) * 100
        if left > 0 or profit_pct < s.min_profit_percent:
            self._reject(opp, "depth", f"{opp.symbol}: по стакану выходит {profit_pct:+.2f}%")
            return None

        return DemoTrade(
            symbol=opp.symbol,
            buy_exchange=buy_ex,
            sell_exchange=sell_ex,
            opened_at=time.time(),
            cost_usd=spent,
            buy_price=spent / (coins / (1 - opp.buy.taker_fee)),
            coins_bought=coins,
            withdraw_fee_coins=fee_coins,
            withdraw_fee_known=fee_known,
            coins_arrived=arrived,
            expected_usd=received,
            signal_percent=profit_pct,
        )

    async def _try_open(self, result: ScanResult) -> list[str]:
        checked = 0
        for opp in result.opportunities:
            # Лишние запросы к бирже делаем только для первых нескольких кандидатов
            needs_network = not (self.acc.location and opp.buy.exchange != self.acc.location)
            if needs_network and checked >= MAX_CANDIDATES_PER_SCAN:
                break
            trade = await self._evaluate(opp)
            checked += needs_network
            if trade is None:
                continue
            self.acc.balance -= trade.cost_usd
            self.acc.location = trade.buy_exchange
            self.acc.open_trade = trade
            save_account(self.acc, self.path)
            fee_src = "с биржи" if trade.withdraw_fee_known else "оценка из настроек"
            return [
                f"🛒 <b>Демо: сделка открыта</b> {html.escape(trade.symbol)}\n"
                f"Куплено на {html.escape(self.scanner.display_name(trade.buy_exchange))} "
                f"на ${trade.cost_usd:.4f} по {fmt_price(trade.buy_price)}\n"
                f"Вывод: −{trade.withdraw_fee_coins:.6g} монет ({fee_src})\n"
                f"Перевод на {html.escape(self.scanner.display_name(trade.sell_exchange))}, "
                f"продажа через {self.settings.demo_transfer_min:g} мин по цене на тот момент\n"
                f"Ожидаемо: {trade.signal_percent:+.2f}%"
            ]
        save_account(self.acc, self.path)
        return []

    async def process(self, result: ScanResult) -> list[str]:
        """Вызывается после каждого скана. Возвращает сообщения о событиях."""
        if not self.acc.active:
            return []
        self.acc.scans += 1
        events = await self._try_close()
        if self.acc.open_trade is None:
            events += await self._try_open(result)
        return events

    # --- отчёты -------------------------------------------------------------

    def status_text(self) -> str:
        a = self.acc
        if not a.start_balance:
            return "Демо-счёт не создан. Запусти: <code>/demo_start 5</code>"
        hours = (time.time() - a.created_at) / 3600
        closed = a.trades
        wins = sum(1 for t in closed if t.profit_usd > 0)
        total_pct = (a.equity / a.start_balance - 1) * 100
        where = html.escape(self.scanner.display_name(a.location)) if a.location else "ещё не выбрана (любая)"
        lines = [
            f"<b>Демо-счёт</b> {'🟢 работает' if a.active else '⏸ остановлен'}",
            f"Старт: ${a.start_balance:.2f}, {hours:.1f} ч назад",
            f"Сейчас: <b>${a.equity:.4f}</b> ({total_pct:+.2f}%)",
            f"Деньги на бирже: {where}",
            f"Сделок закрыто: {len(closed)} (в плюс: {wins}, в минус: {len(closed) - wins})",
            f"Сканирований: {a.scans}",
        ]
        if a.open_trade:
            t = a.open_trade
            left = max(0, self.settings.demo_transfer_min * 60 - (time.time() - t.opened_at)) / 60
            lines.append(
                f"\n⏳ В пути: {html.escape(t.symbol)} → {html.escape(self.scanner.display_name(t.sell_exchange))}, "
                f"продажа через ~{left:.0f} мин"
            )
        if a.rejects:
            lines.append("\n<b>Почему связки отклонены</b> (уникальные):")
            for code, count in sorted(a.rejects.items(), key=lambda x: -x[1]):
                lines.append(f"• {REASONS.get(code, code)}: {count}")
        if a.best_rejected:
            lines.append(f"\nЛучшая отклонённая: <i>{html.escape(a.best_rejected)}</i>")
        if a.active and not closed and a.scans >= 30:
            lines.append(
                "\n💡 Сделок нет, а сканов уже много. Значит, при таком балансе комиссии "
                "съедают весь спред. Сравни: <code>/demo_start 100</code>"
            )
        return "\n".join(lines)

    def history_text(self, limit: int = 10) -> str:
        if not self.acc.trades:
            return "Закрытых демо-сделок пока нет."
        lines = ["<b>Последние демо-сделки</b>"]
        for t in self.acc.trades[-limit:][::-1]:
            when = time.strftime("%d.%m %H:%M", time.localtime(t.closed_at or t.opened_at))
            lines.append(
                f"{when} {html.escape(t.symbol)} "
                f"{html.escape(self.scanner.display_name(t.buy_exchange))}→"
                f"{html.escape(self.scanner.display_name(t.sell_exchange))}: "
                f"{t.profit_usd:+.4f}$ ({t.profit_percent:+.2f}%, обещано {t.signal_percent:+.2f}%)"
            )
        return "\n".join(lines)
