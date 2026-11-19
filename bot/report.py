"""Статистика работы за период и ежедневный отчёт."""

from __future__ import annotations

import html
import time
from collections import Counter
from dataclasses import dataclass, field

from .scanner import NearMiss, ScanResult

TELEGRAM_LIMIT = 4000  # у Telegram лимит 4096 символов, оставляем запас


@dataclass
class ScanStats:
    started_at: float = field(default_factory=time.time)  # когда запущен бот
    period_start: float = field(default_factory=time.time)  # начало текущего периода отчёта
    scans: int = 0
    failed_scans: int = 0
    total_duration: float = 0.0
    exchange_errors: Counter = field(default_factory=Counter)
    cross_keys: set = field(default_factory=set)
    tri_keys: set = field(default_factory=set)
    best_cross: tuple[float, str] | None = None  # (% по стакану, описание)
    best_tri: tuple[float, str] | None = None
    near_cross: NearMiss | None = None  # лучший вариант периода, даже если убыточный
    near_tri: NearMiss | None = None

    def record(self, result: ScanResult, name=lambda x: x) -> None:
        self.scans += 1
        self.total_duration += result.duration_sec
        for ex in result.errors:
            self.exchange_errors[ex] += 1
        if result.near_cross and (self.near_cross is None or result.near_cross.net_percent > self.near_cross.net_percent):
            self.near_cross = result.near_cross
        if result.near_tri and (self.near_tri is None or result.near_tri.net_percent > self.near_tri.net_percent):
            self.near_tri = result.near_tri
        for o in result.opportunities:
            self.cross_keys.add(o.key)
            if o.depth and o.depth.filled:
                pct = o.depth.profit_percent
                if self.best_cross is None or pct > self.best_cross[0]:
                    self.best_cross = (pct, f"{o.symbol} {name(o.buy.exchange)}→{name(o.sell.exchange)}")
        for t in result.triangles:
            self.tri_keys.add(t.key)
            if t.depth and t.depth.filled:
                pct = t.depth.profit_percent
                if self.best_tri is None or pct > self.best_tri[0]:
                    self.best_tri = (pct, f"{t.path_text} на {name(t.exchange)}")

    def record_failure(self) -> None:
        self.failed_scans += 1

    def reset_period(self) -> None:
        started = self.started_at
        self.__init__()
        self.started_at = started

    def text(self, exchanges_total: int, trade_amount: float) -> str:
        hours = (time.time() - self.period_start) / 3600
        uptime_h = (time.time() - self.started_at) / 3600
        avg = self.total_duration / self.scans if self.scans else 0
        lines = [
            f"Работает без перезапуска: {uptime_h:.1f} ч",
            f"За период ({hours:.1f} ч): сканов {self.scans}, неудачных {self.failed_scans}, "
            f"в среднем {avg:.1f} с на скан",
            f"Уникальных связок: межбиржевых {len(self.cross_keys)}, треугольных {len(self.tri_keys)}",
        ]
        if self.best_cross:
            lines.append(
                f"Лучшая межбиржевая по стакану на ${trade_amount:g}: "
                f"<b>{self.best_cross[0]:+.2f}%</b> ({html.escape(self.best_cross[1])}, без учёта вывода)"
            )
        if self.best_tri:
            lines.append(
                f"Лучший треугольник по стакану на ${trade_amount:g}: "
                f"<b>{self.best_tri[0]:+.3f}%</b> ({html.escape(self.best_tri[1])})"
            )
        if self.near_tri and not self.best_tri:
            lines.append(
                f"Ближе всего к прибыли треугольник: {self.near_tri.net_percent:+.3f}% после комиссий "
                f"({self.near_tri.gross_percent:+.3f}% до), {html.escape(self.near_tri.text)}"
            )
        if self.exchange_errors and self.scans:
            bad = [
                f"{html.escape(ex)} {cnt * 100 // max(self.scans, 1)}%"
                for ex, cnt in self.exchange_errors.most_common()
                if cnt / self.scans >= 0.2
            ]
            if bad:
                lines.append("⚠️ Часто не отвечают: " + ", ".join(bad) + " (можно убрать из EXCHANGES)")
        return "\n".join(lines)


def seconds_until(hour: int, utc_offset_hours: float, now: float | None = None) -> float:
    """Сколько секунд до ближайшего hour:00 по местному времени (UTC + offset)."""
    now = time.time() if now is None else now
    local = now + utc_offset_hours * 3600
    day_start = local - (local % 86400)
    target = day_start + hour * 3600
    if target <= local:
        target += 86400
    return target - local


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Режет длинный текст на части по границам блоков, не ломая HTML-теги внутри блока."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        pieces = [block]
        if len(block) > limit:  # очень длинный блок режем по строкам
            pieces, buf = [], ""
            for line in block.split("\n"):
                line = line[:limit]
                if buf and len(buf) + 1 + len(line) > limit:
                    pieces.append(buf)
                    buf = line
                else:
                    buf = f"{buf}\n{line}" if buf else line
            if buf:
                pieces.append(buf)
        for piece in pieces:
            if current and len(current) + 2 + len(piece) > limit:
                parts.append(current)
                current = piece
            else:
                current = f"{current}\n\n{piece}" if current else piece
    if current:
        parts.append(current)
    return parts
