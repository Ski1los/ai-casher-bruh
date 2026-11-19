"""Точная аналитика без ИИ: комиссии, безубыточность, качество исполнения, узкие места.

Всё здесь считается из реальных данных бота. ИИ-помощник получает эти цифры
как факты и не придумывает свои. Команда /analysis работает даже без ключа ИИ.
"""

from __future__ import annotations

import html
import statistics
from dataclasses import dataclass, field

from .config import Settings
from .demo import REASONS, DemoAccount
from .demo_manager import account_label

DEFAULT_TAKER = 0.001


@dataclass
class Suggestion:
    """Рекомендация, которую можно применить кнопкой."""

    key: str  # имя из EDITABLE, например "tri_min"
    value: float
    reason: str


@dataclass
class Analysis:
    fees: dict[str, float] = field(default_factory=dict)  # биржа -> типичная taker-комиссия (доля)
    fee_overridden: set[str] = field(default_factory=set)
    tri_break_even: dict[str, float] = field(default_factory=dict)  # биржа -> % безубыточности цикла
    cross_break_even: tuple[str, str, float] | None = None  # самая дешёвая пара бирж и её %
    cross_by_amount: dict[float, float] = field(default_factory=dict)  # сумма -> % с учётом вывода
    accounts: list[dict] = field(default_factory=list)
    bottlenecks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "fees_taker_percent": {k: round(v * 100, 4) for k, v in self.fees.items()},
            "fees_set_manually": sorted(self.fee_overridden),
            "triangle_break_even_percent": {k: round(v, 3) for k, v in self.tri_break_even.items()},
            "cross_cheapest_pair": (
                {"buy": self.cross_break_even[0], "sell": self.cross_break_even[1],
                 "break_even_percent_without_withdraw": round(self.cross_break_even[2], 3)}
                if self.cross_break_even else None
            ),
            "cross_break_even_percent_by_amount_incl_withdraw": {
                f"${k:g}": round(v, 2) for k, v in self.cross_by_amount.items()
            },
            "demo_accounts": self.accounts,
            "bottlenecks": self.bottlenecks,
            "notes": self.notes,
            "rule_based_suggestions": [
                {"set": s.key, "value": s.value, "why": s.reason} for s in self.suggestions
            ],
        }


# ---------------------------------------------------------------------------
# Комиссии
# ---------------------------------------------------------------------------


def exchange_fees(scanner, quote: str = "USDT") -> dict[str, float]:
    """Медианная taker-комиссия спотовых пар к USDT по каждой бирже (из данных ccxt)."""
    out: dict[str, float] = {}
    for name, ex in scanner.exchanges.items():
        markets = getattr(ex, "markets", None) or {}
        fees = [
            float(m["taker"])
            for m in markets.values()
            if m.get("spot") and m.get("quote") == quote and m.get("taker") is not None
        ]
        if fees:
            out[name] = statistics.median(fees)
    return out


def triangle_break_even(fee: float) -> float:
    """Сколько % должен дать цикл из трёх обменов до комиссий, чтобы выйти в ноль."""
    return (1 / (1 - fee) ** 3 - 1) * 100


def cross_break_even(fee_buy: float, fee_sell: float) -> float:
    return (1 / ((1 - fee_buy) * (1 - fee_sell)) - 1) * 100


# ---------------------------------------------------------------------------
# Демо-счета
# ---------------------------------------------------------------------------


def account_stats(acc: DemoAccount) -> dict:
    trades = acc.trades
    wins = [t for t in trades if t.profit_usd > 0]
    d = {
        "account": account_label(acc),
        "strategy": acc.strategy,
        "start_usd": acc.start_balance,
        "equity_usd": round(acc.equity, 4),
        "pnl_percent": round((acc.equity / acc.start_balance - 1) * 100, 3) if acc.start_balance else 0,
        "trades": len(trades),
        "win_rate_percent": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "scans": acc.scans,
        "rejects": dict(sorted(acc.rejects.items(), key=lambda x: -x[1])),
        "best_rejected": (acc.best_rejected or "")[:160] or None,
    }
    if trades:
        expected = [t.signal_percent for t in trades]
        actual = [t.profit_percent for t in trades]
        d["avg_expected_percent"] = round(statistics.mean(expected), 4)
        d["avg_actual_percent"] = round(statistics.mean(actual), 4)
        d["avg_execution_loss_percent"] = round(statistics.mean(e - a for e, a in zip(expected, actual)), 4)
        d["best_trade_percent"] = round(max(actual), 4)
        d["worst_trade_percent"] = round(min(actual), 4)
        d["last_trades"] = [
            {"what": t.symbol, "expected": round(t.signal_percent, 3), "actual": round(t.profit_percent, 3)}
            for t in trades[-3:]
        ]
    return d


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------


def analyze(scanner, settings: Settings, demo, stats=None) -> Analysis:
    a = Analysis()
    a.fees = exchange_fees(scanner, settings.quote)
    a.fee_overridden = {ex for ex in a.fees if ex in settings.fee_overrides}

    # Безубыточность
    for ex, fee in a.fees.items():
        a.tri_break_even[ex] = triangle_break_even(fee)
    if len(a.fees) >= 2:
        pairs = [
            (x, y, cross_break_even(a.fees[x], a.fees[y])) for x in a.fees for y in a.fees if x != y
        ]
        a.cross_break_even = min(pairs, key=lambda p: p[2])
    base_cross = a.cross_break_even[2] if a.cross_break_even else cross_break_even(DEFAULT_TAKER, DEFAULT_TAKER)
    amounts = sorted({acc.start_balance for acc in demo.accounts.values()}) or list(settings.demo_amounts)
    for amount in amounts:
        a.cross_by_amount[amount] = base_cross + settings.demo_withdraw_fee_usd / amount * 100

    # Демо
    for acc in sorted(demo.accounts.values(), key=lambda x: (x.strategy, x.start_balance)):
        a.accounts.append(account_stats(acc))

    _diagnose(a, settings, demo, stats, getattr(scanner, "display_name", lambda x: x))
    return a


def _diagnose(a: Analysis, s: Settings, demo, stats, name=lambda x: x) -> None:
    accounts = list(demo.accounts.values())

    # 1. Где треугольники дешевле всего
    if a.tri_break_even:
        cheapest = min(a.tri_break_even, key=a.tri_break_even.get)
        priciest = max(a.tri_break_even, key=a.tri_break_even.get)
        lo, hi = a.tri_break_even[cheapest], a.tri_break_even[priciest]
        if hi - lo > 0.01:
            a.notes.append(
                f"Треугольник дешевле всего на {name(cheapest)}: цикл должен дать ≥{lo:.2f}% до комиссий, "
                f"на {name(priciest)} уже ≥{hi:.2f}%. При прочих равных ищи сделки на {name(cheapest)}."
            )
        else:
            a.notes.append(f"Треугольник на любой бирже окупается от ≈{lo:.2f}% до комиссий (3 × taker).")
        if s.tri_min_profit_percent < 0.05:
            a.notes.append("Порог tri_min очень низкий: будут сделки, которые съест проскальзывание.")

    # 2. Межбиржевой и плата за вывод
    for amount, be in sorted(a.cross_by_amount.items()):
        if be > 3:
            a.notes.append(
                f"Межбиржевой на ${amount:g}: чтобы выйти в ноль, спред должен быть ≥{be:.1f}% "
                f"(вывод ${s.demo_withdraw_fee_usd:g} = {s.demo_withdraw_fee_usd / amount * 100:.1f}% суммы). "
                "Такие спреды почти всегда фейковые."
            )

    # 3. Узкие места по каждому счёту
    for acc in accounts:
        if not acc.rejects:
            continue
        top_code, top_count = max(acc.rejects.items(), key=lambda x: x[1])
        total = sum(acc.rejects.values())
        if total >= 5 and top_count / total >= 0.5:
            a.bottlenecks.append(
                f"{account_label(acc)}: {top_count * 100 // total}% отказов, причина: {REASONS.get(top_code, top_code)}"
            )

    # 4. Качество исполнения треугольников → поднять порог
    tri_trades = [t for acc in accounts if acc.strategy == "tri" for t in acc.trades]
    if len(tri_trades) >= 3:
        loss = statistics.mean(t.signal_percent - t.profit_percent for t in tri_trades)
        actual = statistics.mean(t.profit_percent for t in tri_trades)
        if actual < 0 and loss > 0:
            new_min = round(min(5.0, max(s.tri_min_profit_percent, 0) + loss), 2)
            if new_min > s.tri_min_profit_percent:
                a.suggestions.append(Suggestion(
                    "tri_min", new_min,
                    f"треугольники в среднем теряют {loss:.3f}% между обнаружением и исполнением "
                    f"(средний итог {actual:+.3f}%), входить стоит только с запасом",
                ))

    # 5. Межбиржевые сделки: плата за перевод из настроек, но спред съедается в пути
    cross_trades = [t for acc in accounts if acc.strategy == "cross" for t in acc.trades]
    if len(cross_trades) >= 3:
        actual = statistics.mean(t.profit_percent for t in cross_trades)
        if actual < 0:
            new_min = round(min(20.0, s.min_profit_percent + abs(actual) + 0.2), 2)
            a.suggestions.append(Suggestion(
                "min_profit", new_min,
                f"межбиржевые сделки в среднем {actual:+.2f}%: цена уходит, пока монета едет",
            ))

    # 6. «Почти получилось»: насколько рынок близок к прибыли
    if stats is not None and stats.scans >= 10:
        nt, nc = stats.near_tri, stats.near_cross
        if nt is not None and not stats.tri_keys:
            if 0 < nt.net_percent < s.tri_min_profit_percent and not tri_trades:
                # были циклы в плюсе после комиссий, но порог их отсёк: демо проверит, реальны ли они
                new_min = round(max(0.03, nt.net_percent * 0.8), 3)
                a.suggestions.append(Suggestion(
                    "tri_min", new_min,
                    f"лучший треугольник периода дал {nt.net_percent:+.3f}% после комиссий, "
                    f"но порог {s.tri_min_profit_percent:g}% его отсёк; демо проверит, держится ли прибыль",
                ))
            elif nt.net_percent <= 0:
                a.notes.append(
                    f"Лучший треугольник периода: {nt.gross_percent:+.3f}% до комиссий и "
                    f"{nt.net_percent:+.3f}% после ({nt.text}). Сейчас всю разницу съедают комиссии."
                )
        if nc is not None and nc.net_percent < s.min_profit_percent and not stats.cross_keys:
            a.notes.append(
                f"Лучшая межбиржевая пара периода: {nc.net_percent:+.2f}% после торговых комиссий, "
                f"ещё без платы за вывод ({nc.text})."
            )

    # 7. Биржи, которые часто не отвечают
    if stats is not None and stats.scans >= 10:
        for ex, cnt in stats.exchange_errors.items():
            if cnt / stats.scans >= 0.3:
                a.notes.append(
                    f"{name(ex)} не отвечает в {cnt * 100 // stats.scans}% сканов: лучше убрать её из EXCHANGES."
                )

    # 8. Перспективные счета
    for acc in accounts:
        st = account_stats(acc)
        if st["trades"] >= 10 and (st["win_rate_percent"] or 0) >= 60 and st["pnl_percent"] > 0:
            a.notes.append(
                f"{account_label(acc)} в плюсе ({st['pnl_percent']:+.2f}%, {st['trades']} сделок, "
                f"{st['win_rate_percent']}% успешных). Прежде чем рисковать деньгами, понаблюдай ещё несколько дней."
            )

    # убираем дубли рекомендаций по одному ключу (оставляем первую)
    seen: set[str] = set()
    a.suggestions = [x for x in a.suggestions if not (x.key in seen or seen.add(x.key))]


# ---------------------------------------------------------------------------
# Текст для Telegram
# ---------------------------------------------------------------------------


def fees_text(a: Analysis, scanner, settings: Settings) -> str:
    """Только комиссии и безубыточность (команда /fees)."""
    return "\n\n".join(_fee_blocks(a, scanner, settings))


def _fee_blocks(a: Analysis, scanner, settings: Settings) -> list[str]:
    name = scanner.display_name
    parts: list[str] = []

    if a.fees:
        lines = ["💸 <b>Комиссии</b> (taker, спот)"]
        for ex, fee in sorted(a.fees.items(), key=lambda x: x[1]):
            mark = " ✏️ вручную" if ex in a.fee_overridden else ""
            lines.append(
                f"  {html.escape(name(ex))}: {fee * 100:.3f}% · треугольник окупается от "
                f"{a.tri_break_even[ex]:.2f}%{mark}"
            )
        if a.cross_break_even:
            x, y, be = a.cross_break_even
            lines.append(
                f"  Межбиржевой: дешевле всего {html.escape(name(x))}→{html.escape(name(y))}, "
                f"нужно ≥{be:.2f}% + плата за вывод"
            )
        lines.append(
            "  <i>Это стандартные ставки из ccxt. Если у тебя другая (VIP, скидка за токен биржи), "
            "задай: <code>/fee bybit 0.08</code> (в процентах), вернуть: <code>/fee bybit reset</code></i>"
        )
        parts.append("\n".join(lines))
    else:
        parts.append("💸 Комиссии появятся после первого успешного скана: /scan")

    if a.cross_by_amount:
        lines = [f"⚖️ <b>Межбиржевой: безубыточность с учётом вывода</b> (≈${settings.demo_withdraw_fee_usd:g})"]
        for amount, be in sorted(a.cross_by_amount.items()):
            lines.append(f"  ${amount:g}: спред ≥ {be:.2f}%")
        parts.append("\n".join(lines))
    return parts


def analysis_text(a: Analysis, scanner, settings: Settings) -> str:
    parts = ["📊 <b>Анализ</b>", *_fee_blocks(a, scanner, settings)]

    if a.accounts:
        lines = ["🧪 <b>Демо: ожидание и реальность</b>"]
        for st in a.accounts:
            line = f"  {html.escape(st['account'])}: {st['pnl_percent']:+.2f}%, сделок {st['trades']}"
            if st["trades"]:
                line += (
                    f", ждали {st['avg_expected_percent']:+.3f}% → вышло {st['avg_actual_percent']:+.3f}%"
                    f" (успешных {st['win_rate_percent']}%)"
                )
            lines.append(line)
        parts.append("\n".join(lines))

    if a.bottlenecks:
        parts.append("🚧 <b>Узкие места</b>\n" + "\n".join(f"  • {html.escape(b)}" for b in a.bottlenecks))
    if a.notes:
        parts.append("📝 <b>Выводы</b>\n" + "\n".join(f"  • {html.escape(n)}" for n in a.notes))
    if a.suggestions:
        parts.append(
            "💡 <b>Рекомендации</b> (применяются кнопкой ниже)\n"
            + "\n".join(f"  • <code>{x.key} = {x.value:g}</code>: {html.escape(x.reason)}" for x in a.suggestions)
        )
    elif a.accounts:
        parts.append("💡 Пока нечего менять: данных мало или всё в норме. Подробнее спроси ИИ: /ai")
    return "\n\n".join(parts)
