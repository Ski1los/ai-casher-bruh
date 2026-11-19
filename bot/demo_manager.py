"""Несколько демо-счетов сразу: разные суммы × разные стратегии.

Например, при /demo_start 5 50 500 создаётся 6 счетов:
  Межбиржевой $5, $50, $500  — покупка на одной бирже, перевод, продажа на другой;
  Треугольный $5, $50, $500  — три обмена подряд внутри одной биржи.

Так за сутки видно, при какой сумме и какой стратегии есть реальный результат.

Как честно моделируется треугольник:
  1) по тикерам находим цикл с прибылью;
  2) берём стаканы и проверяем, что прибыль есть на реальных заявках («обнаружение»);
  3) ЗАНОВО запрашиваем стаканы и исполняем сделку по ним («исполнение»).
     Между шагами 2 и 3 проходит реальное время сетевых запросов, как у живого бота.
     Если за это время возможность исчезла, демо честно фиксирует убыток.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .demo import DEMO_FILE, REASONS, DemoAccount, DemoEngine, DemoTrade, account_from_dict
from .scanner import Scanner, ScanResult, short_error
from .triangular import TriOpportunity, path_min_order_ok, simulate_path

log = logging.getLogger(__name__)

STRATEGIES = {
    "cross": ("Межбиржевой", "покупка на одной бирже, перевод и продажа на другой"),
    "tri": ("Треугольный", "три обмена подряд внутри одной биржи"),
}
MAX_TRI_CANDIDATES = 3  # сколько треугольников за скан проверять по стаканам
TRI_REPEAT_COOLDOWN_SEC = 10 * 60  # один и тот же цикл на одном счёте не чаще раза в 10 минут
MAX_TRADES_KEPT = 300  # сколько сделок хранить на счёт (чтобы бэкап не разрастался)


def account_id(strategy: str, amount: float) -> str:
    return f"{strategy}-{amount:g}"


def account_label(acc: DemoAccount) -> str:
    return f"{STRATEGIES.get(acc.strategy, (acc.strategy,))[0]} ${acc.start_balance:g}"


class BookCache:
    """Стаканы на время одного прохода: несколько счетов не дёргают биржу за одним и тем же."""

    def __init__(self, scanner: Scanner):
        self.scanner = scanner
        self._cache: dict[tuple[str, str], dict] = {}

    async def get(self, exchange: str, symbol: str) -> dict:
        key = (exchange, symbol)
        if key not in self._cache:
            self._cache[key] = await self.scanner.order_book(exchange, symbol)
        return self._cache[key]

    async def get_many(self, exchange: str, symbols: list[str]) -> list[dict]:
        return list(await asyncio.gather(*(self.get(exchange, s) for s in symbols)))

    async def fresh_many(self, exchange: str, symbols: list[str]) -> list[dict]:
        books = await asyncio.gather(*(self.scanner.order_book(exchange, s) for s in symbols))
        for s, b in zip(symbols, books):
            self._cache[(exchange, s)] = b
        return list(books)


# ---------------------------------------------------------------------------
# Треугольные демо-счета
# ---------------------------------------------------------------------------


class TriDemoEngine:
    def __init__(self, scanner: Scanner, settings: Settings, accounts: list[DemoAccount]):
        self.scanner = scanner
        self.settings = settings
        self.accounts = accounts
        self._counted: set[str] = set()

    def _reject(self, acc: DemoAccount, opp: TriOpportunity, reason: str, note: str | None = None) -> None:
        tag = f"{acc.start_balance}|{opp.exchange}|{opp.path_text}|{reason}"
        if tag not in self._counted:
            self._counted.add(tag)
            acc.rejects[reason] = acc.rejects.get(reason, 0) + 1
        if note and opp.net_percent > acc.best_rejected_percent:
            acc.best_rejected_percent = opp.net_percent
            acc.best_rejected = note

    def _markets(self, opp: TriOpportunity) -> dict[str, dict]:
        return {leg.symbol: self.scanner.market(opp.exchange, leg.symbol) for leg in opp.legs}

    async def process(self, result: ScanResult, books: BookCache) -> list[str]:
        active = [a for a in self.accounts if a.active]
        if not active:
            return []
        now = time.time()
        for acc in active:
            acc.scans += 1
            acc.recent = {k: t for k, t in acc.recent.items() if now - t < TRI_REPEAT_COOLDOWN_SEC}

        events: list[str] = []
        checked = 0
        for opp in result.triangles:
            key = "|".join(opp.key)
            eligible: list[DemoAccount] = []
            for acc in active:
                if acc.location and acc.location != opp.exchange:
                    self._reject(acc, opp, "location")
                    continue
                if key in acc.recent:
                    continue
                if not path_min_order_ok(opp.legs, acc.balance, self._markets(opp)):
                    self._reject(
                        acc, opp, "min_order",
                        f"{opp.path_text} ({self.scanner.display_name(opp.exchange)}): "
                        f"${acc.balance:.2f} меньше минимального ордера одной из пар",
                    )
                    continue
                eligible.append(acc)
            if not eligible:
                continue
            if checked >= MAX_TRI_CANDIDATES:
                break
            checked += 1

            symbols = [leg.symbol for leg in opp.legs]
            try:
                detect = await books.get_many(opp.exchange, symbols)
            except Exception as e:
                log.info("Треугольник: %s", short_error(e))
                for acc in eligible:
                    self._reject(acc, opp, "error")
                continue

            entering: list[tuple[DemoAccount, float]] = []
            for acc in eligible:
                d = simulate_path(detect, opp.legs, acc.balance)
                if not d.filled or d.profit_percent < self.settings.tri_min_profit_percent:
                    self._reject(
                        acc, opp, "depth",
                        f"{opp.path_text} ({self.scanner.display_name(opp.exchange)}): по тикерам "
                        f"+{opp.net_percent:.2f}%, по стаканам на ${acc.balance:.2f} {d.profit_percent:+.2f}%",
                    )
                    continue
                entering.append((acc, d.profit_percent))
            if not entering:
                continue

            # Исполнение: стаканы запрашиваем заново — прошло реальное время
            try:
                execute = await books.fresh_many(opp.exchange, symbols)
            except Exception as e:
                log.info("Треугольник (исполнение): %s", short_error(e))
                continue

            lines = [
                f"🔺 <b>Демо: треугольник</b> на {html.escape(self.scanner.display_name(opp.exchange))}\n"
                f"{html.escape(opp.path_text)}"
            ]
            for acc, expected_pct in entering:
                d2 = simulate_path(execute, opp.legs, acc.balance)
                trade = DemoTrade(
                    symbol=opp.path_text,
                    buy_exchange=opp.exchange,
                    sell_exchange=opp.exchange,
                    opened_at=now,
                    cost_usd=acc.balance,
                    buy_price=0.0,
                    coins_bought=0.0,
                    withdraw_fee_coins=0.0,
                    withdraw_fee_known=True,
                    coins_arrived=0.0,
                    expected_usd=acc.balance * (1 + expected_pct / 100),
                    signal_percent=expected_pct,
                    closed_at=time.time(),
                    received_usd=d2.received_usd,
                    strategy="tri",
                )
                acc.balance = d2.received_usd
                acc.location = opp.exchange
                acc.trades.append(trade)
                acc.recent[key] = now
                icon = "✅" if trade.profit_usd >= 0 else "🔻"
                total = (acc.balance / acc.start_balance - 1) * 100
                lines.append(
                    f"{icon} ${acc.start_balance:g}: {trade.profit_usd:+.4f}$ ({trade.profit_percent:+.3f}%, "
                    f"ожидалось {expected_pct:+.3f}%) → баланс ${acc.balance:.4f} ({total:+.2f}%)"
                )
            events.append("\n".join(lines))
        return events


# ---------------------------------------------------------------------------
# Менеджер всех счетов
# ---------------------------------------------------------------------------


class DemoManager:
    def __init__(self, scanner: Scanner, settings: Settings, path: Path | None = DEMO_FILE):
        self.scanner = scanner
        self.settings = settings
        self.path = path
        self.accounts: dict[str, DemoAccount] = {}
        self.chat_id: int | None = None
        self.created_at: float = 0.0
        self._cross: dict[str, DemoEngine] = {}
        self._tri = TriDemoEngine(scanner, settings, [])
        if path is not None and path.exists():
            try:
                self._load_dict(json.loads(path.read_text("utf-8")))
            except (ValueError, TypeError) as e:
                log.warning("Не удалось прочитать %s (%s), начинаю заново", path, e)
        self._rebuild()

    # --- состояние ----------------------------------------------------------

    @property
    def active(self) -> bool:
        return any(a.active for a in self.accounts.values())

    def _rebuild(self) -> None:
        self._cross = {}
        for aid, acc in self.accounts.items():
            if acc.strategy == "cross":
                engine = DemoEngine(self.scanner, self.settings, path=None)
                engine.acc = acc
                self._cross[aid] = engine
        self._tri = TriDemoEngine(
            self.scanner, self.settings, [a for a in self.accounts.values() if a.strategy == "tri"]
        )

    def _load_dict(self, raw: dict) -> None:
        if "accounts" in raw:
            self.chat_id = raw.get("chat_id")
            self.created_at = raw.get("created_at") or 0.0
            self.accounts = {k: account_from_dict(v) for k, v in raw["accounts"].items()}
        elif "start_balance" in raw:  # формат первой версии: один межбиржевой счёт
            acc = account_from_dict(raw)
            self.chat_id = acc.chat_id
            self.created_at = acc.created_at
            self.accounts = {account_id("cross", acc.start_balance): acc} if acc.start_balance else {}

    def export(self) -> dict:
        for acc in self.accounts.values():
            if len(acc.trades) > MAX_TRADES_KEPT:
                acc.trades = acc.trades[-MAX_TRADES_KEPT:]
        return {
            "version": 2,
            "chat_id": self.chat_id,
            "created_at": self.created_at,
            "accounts": {k: asdict(v) for k, v in self.accounts.items()},
        }

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.export(), ensure_ascii=False), "utf-8")

    def restore(self, raw: dict) -> None:
        self._load_dict(raw)
        self._rebuild()
        self.save()

    def start(self, amounts: list[float], chat_id: int | None = None) -> list[str]:
        strategies = ["cross"] + (["tri"] if self.settings.triangular_enabled else [])
        now = time.time()
        self.chat_id = chat_id
        self.created_at = now
        self.accounts = {}
        for st in strategies:
            for amount in amounts:
                self.accounts[account_id(st, amount)] = DemoAccount(
                    active=True, chat_id=chat_id, start_balance=amount, balance=amount,
                    created_at=now, strategy=st,
                )
        self._rebuild()
        self.save()
        return [account_label(a) for a in self.accounts.values()]

    def stop(self) -> None:
        for acc in self.accounts.values():
            acc.active = False
        self.save()

    # --- работа -------------------------------------------------------------

    async def process(self, result: ScanResult) -> list[str]:
        if not self.active:
            return []
        books = BookCache(self.scanner)
        events: list[str] = []
        for engine in self._cross.values():
            if not engine.acc.active:
                continue
            engine.get_book = books.get
            for text in await engine.process(result):
                events.append(f"[{html.escape(account_label(engine.acc))}] {text}")
        events += await self._tri.process(result, books)
        self.save()
        return events

    # --- отчёты -------------------------------------------------------------

    def summary_text(self) -> str:
        if not self.accounts:
            return "Демо-счета не созданы. Запусти: <code>/demo_start</code>"
        hours = (time.time() - self.created_at) / 3600
        state = "🟢 работают" if self.active else "⏸ остановлены"
        lines = [f"🧪 <b>Демо-счета</b> {state} · {hours:.1f} ч"]
        scans = max((a.scans for a in self.accounts.values()), default=0)
        best: tuple[float, str] | None = None
        for st, (title, desc) in STRATEGIES.items():
            accs = [a for a in self.accounts.values() if a.strategy == st]
            if not accs:
                continue
            lines.append(f"\n<b>{title}</b> <i>({desc})</i>")
            for a in sorted(accs, key=lambda x: x.start_balance):
                pct = (a.equity / a.start_balance - 1) * 100 if a.start_balance else 0
                wins = sum(1 for t in a.trades if t.profit_usd > 0)
                extra = " ⏳ в пути" if a.open_trade else ""
                lines.append(
                    f"  ${a.start_balance:g} → <b>${a.equity:.4f}</b> ({pct:+.2f}%) · "
                    f"сделок {len(a.trades)} ({wins}✅/{len(a.trades) - wins}🔻){extra}"
                )
                if a.trades and (best is None or pct > best[0]):
                    best = (pct, account_label(a))
        lines.append(f"\nСканирований: {scans}")
        if best:
            lines.append(f"Лучший счёт: <b>{html.escape(best[1])}</b> ({best[0]:+.2f}%)")
        elif scans >= 30:
            lines.append(
                "💡 Сделок пока нет ни на одном счёте: все найденные связки съедают комиссии "
                "или не проходят по стаканам. Причины: <code>/demo 5</code>"
            )
        amounts = sorted({a.start_balance for a in self.accounts.values()})
        lines.append("Подробно: " + " · ".join(f"<code>/demo {x:g}</code>" for x in amounts))
        lines.append("История: /demo_history")
        return "\n".join(lines)

    def details_text(self, amount: float) -> str:
        accs = [a for a in self.accounts.values() if abs(a.start_balance - amount) < 1e-9]
        if not accs:
            amounts = ", ".join(f"{a.start_balance:g}" for a in self.accounts.values())
            return f"Нет демо-счетов на ${amount:g}. Есть: {amounts or 'никаких'}"
        parts = []
        for acc in sorted(accs, key=lambda a: a.strategy):
            parts.append(f"<b>{html.escape(account_label(acc))}</b>\n" + self._account_details(acc))
        return "\n\n".join(parts)

    def _account_details(self, a: DemoAccount) -> str:
        closed = a.trades
        wins = sum(1 for t in closed if t.profit_usd > 0)
        pct = (a.equity / a.start_balance - 1) * 100 if a.start_balance else 0
        where = html.escape(self.scanner.display_name(a.location)) if a.location else "ещё не выбрана"
        lines = [
            f"Сейчас: <b>${a.equity:.4f}</b> ({pct:+.2f}%)",
            f"Деньги на бирже: {where}",
            f"Сделок: {len(closed)} (✅ {wins} / 🔻 {len(closed) - wins})",
        ]
        if closed:
            exp = sum(t.signal_percent for t in closed) / len(closed)
            real = sum(t.profit_percent for t in closed) / len(closed)
            lines.append(f"Средняя сделка: ожидалось {exp:+.3f}%, вышло {real:+.3f}%")
        if a.open_trade:
            t = a.open_trade
            left = max(0, self.settings.demo_transfer_min * 60 - (time.time() - t.opened_at)) / 60
            lines.append(
                f"⏳ В пути: {html.escape(t.symbol)} → {html.escape(self.scanner.display_name(t.sell_exchange))}, "
                f"продажа через ~{left:.0f} мин"
            )
        if a.rejects:
            lines.append("Почему связки отклонены (уникальные):")
            for code, count in sorted(a.rejects.items(), key=lambda x: -x[1]):
                lines.append(f"  • {REASONS.get(code, code)}: {count}")
        if a.best_rejected:
            lines.append(f"Лучшая отклонённая: <i>{html.escape(a.best_rejected)}</i>")
        return "\n".join(lines)

    def history_text(self, limit: int = 15) -> str:
        rows = [(t, a) for a in self.accounts.values() for t in a.trades]
        if not rows:
            return "Закрытых демо-сделок пока нет."
        rows.sort(key=lambda r: r[0].closed_at or r[0].opened_at, reverse=True)
        lines = ["<b>Последние демо-сделки</b>"]
        for t, a in rows[:limit]:
            when = time.strftime("%d.%m %H:%M", time.gmtime((t.closed_at or t.opened_at) + self.settings.report_utc_offset * 3600))
            route = (
                html.escape(self.scanner.display_name(t.buy_exchange))
                if t.strategy == "tri"
                else f"{html.escape(self.scanner.display_name(t.buy_exchange))}→{html.escape(self.scanner.display_name(t.sell_exchange))}"
            )
            lines.append(
                f"{when} [{html.escape(account_label(a))}] {html.escape(t.symbol)} {route}: "
                f"{t.profit_usd:+.4f}$ ({t.profit_percent:+.3f}%, ожидалось {t.signal_percent:+.3f}%)"
            )
        return "\n".join(lines)
