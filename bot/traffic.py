"""📶 Трафик (v48.1): сколько бот качает из интернета и как уложиться в бесплатные 5 ГБ Render в месяц.

Почему это важно. Render на бесплатном тарифе считает ВЕСЬ трафик, который сервис гоняет по интернету (запросы
к биржам, новостям, Solana, ИИ, бэкапы в Telegram, ответы приложения), и даёт 5 ГБ на месяц на весь workspace.
Кончились — сервис спит до 1-го числа. Поэтому бот сам:

1. 📏 Меряет. Настоящий счётчик — сетевой интерфейс контейнера (/proc/net/dev: всё, что пришло и ушло, — так же,
   как видит Render, а то и строже: считаем оба направления). Плюс разбивка по сайтам (api.bybit.com,
   cointelegraph.com…) — чтобы было видно, кто ест трафик (/traffic).
2. 🧮 Планирует. Бюджет на месяц (TRAFFIC_BUDGET_GB, по умолчанию 4 ГБ — запас до 5). Каждые пару минут бот
   прикидывает: если тратить так же до конца месяца, уложимся? Если нет — включает эконом-режим.
3. 🐢 Экономит. Тяжёлые циклы (сканер цен, новости, кошельки Solana, пульс, эфиры) ждут дольше: ×2, ×4, ×8.
   Самое тяжёлое (видео эфиров, разведчик) выключается первым. Ведение открытых сделок (стопы, цели) замедляется
   не больше чем вдвое — деньги важнее трафика.
4. 🛑 Последний рубеж: потрачено ≥ бюджета — работают только Telegram, приложение и ведение открытых сделок.

Состояние — в copy.st["traffic"] (сохраняется в бэкап, переживает перезапуски).
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import aiohttp

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

GB = 1024 ** 3
MB = 1024 ** 2
DEFAULT_BUDGET_GB = 4.0
HOSTS_KEEP = 40
# (порог: прогноз на месяц / бюджет, уровень). Прогноз = потрачено / прошедшая доля месяца.
LEVELS = ((0.85, 0), (1.1, 1), (1.6, 2))
LEVEL_FACTOR = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16}
LEVEL_NAME = {0: "обычный", 1: "эконом ×2", 2: "сильная экономия ×4", 3: "минимум ×8", 4: "стоп: бюджет исчерпан"}
# что выключается на каком уровне (включительно)
HEAVY_OFF = {"live_video": 1, "scout": 2, "live": 2, "mentors": 3, "pulse": 3, "copy_discover": 2, "news_low": 2,
             "opps": 3, "radar": 3, "mirror": 3}

_meter: Traffic | None = None  # один на процесс: к нему стучатся все aiohttp-сессии


def allow(feature: str) -> bool:
    """Для модулей без ссылки на бота: можно ли сейчас тяжёлую функцию (нет счётчика — можно)."""
    return _meter is None or _meter.allow(feature)


def note_ws(host: str, n: int) -> None:
    """Байты из веб-сокета (их трассировка aiohttp не видит)."""
    if _meter is not None:
        _meter.note(host, n)


def factor(cap: int | None = None) -> int:
    f = 1 if _meter is None else _meter.factor()
    return min(f, cap) if cap else f


def _month(now: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(now))


def month_fraction(now: float) -> float:
    """Какая доля календарного месяца (UTC) прошла. Не меньше одних суток — чтобы первый день не пугал."""
    t = time.gmtime(now)
    start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0))
    span = calendar.monthrange(t.tm_year, t.tm_mon)[1] * 86400
    return min(1.0, max(86400.0 / span, (now - start) / span))


def read_netdev(path: str = "/proc/net/dev") -> int | None:
    """Сколько байт прошло через сетевые интерфейсы контейнера (кроме lo), в обе стороны. None — не Linux."""
    try:
        with open(path, encoding="ascii") as f:
            lines = f.readlines()[2:]
    except OSError:
        return None
    total = 0
    for line in lines:
        name, _, rest = line.partition(":")
        if name.strip() == "lo" or not rest:
            continue
        parts = rest.split()
        if len(parts) >= 9:
            total += int(parts[0]) + int(parts[8])
    return total


def gb(v: float) -> str:
    return f"{v / GB:.2f} ГБ" if v >= 0.1 * GB else f"{v / MB:.0f} МБ"


class Traffic:
    def __init__(self, b: ArbitrageBot | None = None, netdev: str = "/proc/net/dev") -> None:
        self.b = b
        self.netdev = netdev
        self._local: dict = {}
        self._session_hosts: dict[str, int] = {}  # с последнего сохранения
        self._session_bytes = 0
        self._last_raw = read_netdev(netdev)
        self._last_level: int | None = None
        self.budget = self._budget_env()

    @staticmethod
    def _budget_env() -> float:
        try:
            v = float(os.getenv("TRAFFIC_BUDGET_GB", "") or DEFAULT_BUDGET_GB)
        except ValueError:
            v = DEFAULT_BUDGET_GB
        return max(0.2, min(v, 10_000.0)) * GB

    # --- состояние --------------------------------------------------------------------------------

    @property
    def st(self) -> dict:
        if self.b is not None and getattr(self.b, "copy", None) is not None:
            cs = self.b.copy.st
            st = cs.get("traffic")
            if not isinstance(st, dict):
                st = cs["traffic"] = {}
        else:
            st = self._local
        st.setdefault("month", "")
        st.setdefault("used", 0)
        st.setdefault("hosts", {})
        st.setdefault("days", {})
        st.setdefault("budget_gb", None)  # выставлено командой /traffic budget (иначе из TRAFFIC_BUDGET_GB)
        st.setdefault("prev", None)       # итог прошлого месяца
        return st

    def budget_bytes(self) -> float:
        v = self.st.get("budget_gb")
        return float(v) * GB if v else self.budget

    # --- учёт -------------------------------------------------------------------------------------

    def note(self, host: str, n: int) -> None:
        """Байты по сайту (из трассировки aiohttp). Только для разбивки «кто ест»."""
        if n <= 0:
            return
        self._session_hosts[host] = self._session_hosts.get(host, 0) + n
        self._session_bytes += n

    def update(self, now: float | None = None) -> int:
        """Перенести накопленное в месячный счёт. Возвращает прирост байт."""
        now = time.time() if now is None else now
        st = self.st
        month = _month(now)
        if st["month"] != month:
            if st["month"]:
                st["prev"] = {"month": st["month"], "used": st["used"]}
            st.update(month=month, used=0, hosts={}, days={})
        raw = read_netdev(self.netdev)
        if raw is not None and self._last_raw is not None and raw >= self._last_raw:
            delta = raw - self._last_raw
        elif raw is not None and self._last_raw is not None:
            delta = raw  # счётчик сбросился (новый контейнер)
        else:
            delta = self._session_bytes  # не Linux: только то, что видно через aiohttp
        self._last_raw = raw
        hosts = st["hosts"]
        for h, n in self._session_hosts.items():
            hosts[h] = hosts.get(h, 0) + n
        if len(hosts) > HOSTS_KEEP:
            st["hosts"] = dict(sorted(hosts.items(), key=lambda x: -x[1])[:HOSTS_KEEP])
        self._session_hosts = {}
        self._session_bytes = 0
        st["used"] = int(st["used"] + delta)
        day = time.strftime("%d", time.gmtime(now))
        st["days"][day] = int(st["days"].get(day, 0) + delta)
        return delta

    # --- план -------------------------------------------------------------------------------------

    def projected(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return self.st["used"] / month_fraction(now)

    def level(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        budget = self.budget_bytes()
        used = self.st["used"]
        if used >= budget:
            return 4
        if used >= 0.92 * budget:
            return 3
        ratio = self.projected(now) / budget
        for limit, lvl in LEVELS:
            if ratio < limit:
                return lvl
        return 3

    def factor(self, now: float | None = None) -> int:
        return LEVEL_FACTOR[self.level(now)]

    def allow(self, feature: str, now: float | None = None) -> bool:
        """Можно ли сейчас тяжёлую функцию (видео эфиров, разведчик…)."""
        off_at = HEAVY_OFF.get(feature)
        return off_at is None or self.level(now) < off_at

    async def sleep(self, base: float, cap: int | None = None) -> None:
        """asyncio.sleep с учётом экономии: base × множитель (cap — не больше чем во столько раз)."""
        f = self.factor()
        await asyncio.sleep(base * (min(f, cap) if cap else f))

    def per_day_left(self, now: float | None = None) -> float:
        """Сколько байт в среднем можно тратить в оставшиеся дни месяца."""
        now = time.time() if now is None else now
        t = time.gmtime(now)
        days_in = calendar.monthrange(t.tm_year, t.tm_mon)[1]
        left_days = max(1.0, days_in - t.tm_mday + 1 - (t.tm_hour * 3600 + t.tm_min * 60) / 86400)
        return max(0.0, self.budget_bytes() - self.st["used"]) / left_days

    # --- тексты -----------------------------------------------------------------------------------

    def status_dict(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        st = self.st
        lvl = self.level(now)
        hosts = sorted(st["hosts"].items(), key=lambda x: -x[1])
        seen = sum(n for _, n in hosts) or 1
        return {"month": st["month"], "used": st["used"], "budget": self.budget_bytes(), "projected": self.projected(now),
                "level": lvl, "level_name": LEVEL_NAME[lvl], "factor": LEVEL_FACTOR[lvl],
                "per_day_left": self.per_day_left(now),
                "hosts": [{"host": h, "share": round(n / seen, 3), "bytes": n} for h, n in hosts[:8]],
                "days": st["days"], "prev": st.get("prev")}

    def text(self, now: float | None = None) -> str:
        d = self.status_dict(now)
        lines = ["📶 <b>Трафик за месяц</b> (Render бесплатно даёт 5 ГБ, дальше сервис спит до 1-го числа)",
                 f"Потрачено: <b>{gb(d['used'])}</b> из бюджета {gb(d['budget'])}",
                 f"Прогноз на месяц при таком темпе: {gb(d['projected'])}",
                 f"Режим: <b>{d['level_name']}</b>"
                 + ("" if d["level"] == 0 else " — тяжёлые циклы реже, видео эфиров выключено"),
                 f"Можно тратить в среднем: {gb(d['per_day_left'])} в сутки до конца месяца"]
        if d["hosts"]:
            lines.append("\n<b>Кто ест трафик</b> (доли запросов бота):")
            for h in d["hosts"]:
                lines.append(f"• {h['host']}: {h['share'] * 100:.0f}%")
        if d.get("prev"):
            lines.append(f"\nПрошлый месяц ({d['prev']['month']}): {gb(d['prev']['used'])}")
        lines.append("\n<i>Цифра бота может отличаться от Render на 10–20%. Если в Render (Dashboard → Billing) "
                     "больше — напиши /traffic used 3.2 (сколько там ГБ), бот подстроится. Бюджет: /traffic budget 4</i>")
        return "\n".join(lines)

    def level_change_text(self, now: float | None = None) -> str | None:
        """Сообщение, когда режим экономии сменился (None — не менялся)."""
        lvl = self.level(now)
        prev, self._last_level = self._last_level, lvl
        if prev is None or prev == lvl:
            return None
        d = self.status_dict(now)
        if lvl > prev:
            return (f"📶 <b>Экономлю трафик: {d['level_name']}</b>\nПотрачено {gb(d['used'])} из {gb(d['budget'])}, "
                    f"при таком темпе за месяц выйдет {gb(d['projected'])}. Сканер, новости и кошельки проверяю реже, "
                    "тяжёлое (видео эфиров, разведчик) выключаю, открытые сделки веду как обычно. Подробно: /traffic")
        return f"📶 Трафик в норме, режим: {d['level_name']}. Подробно: /traffic"


# --- трассировка aiohttp: кто сколько качает ---------------------------------------------------------

def _host(url) -> str:
    try:
        return urlsplit(str(url)).hostname or "?"
    except Exception:
        return "?"


async def _on_chunk(session, ctx, params) -> None:
    m = _meter
    if m is not None:
        m.note(getattr(ctx, "host", "?"), len(params.chunk))


async def _on_request_start(session, ctx, params) -> None:
    ctx.host = _host(params.url)
    m = _meter
    if m is not None:
        m.note(ctx.host, 400 + len(str(params.url)))  # заголовки запроса и ответа, грубо


async def _on_request_chunk(session, ctx, params) -> None:
    m = _meter
    if m is not None:
        m.note(getattr(ctx, "host", "?"), len(params.chunk))


def trace_config() -> aiohttp.TraceConfig:
    tc = aiohttp.TraceConfig()
    tc.on_request_start.append(_on_request_start)
    tc.on_request_chunk_sent.append(_on_request_chunk)
    tc.on_response_chunk_received.append(_on_chunk)
    return tc


def install(meter: Traffic) -> None:
    """Все aiohttp-сессии процесса (ccxt, aiogram, новости, Solana, ИИ) сообщают свои байты счётчику."""
    global _meter
    _meter = meter
    cls = aiohttp.ClientSession
    if getattr(cls, "_traffic_patched", False):
        return
    orig = cls.__init__

    def __init__(self, *args, **kwargs):
        tcs = list(kwargs.get("trace_configs") or [])
        tcs.append(trace_config())
        kwargs["trace_configs"] = tcs
        orig(self, *args, **kwargs)

    cls.__init__ = __init__
    cls._traffic_patched = True


async def traffic_loop(b: ArbitrageBot) -> None:
    """Раз в 2 минуты: учесть трафик, при смене режима — сообщить владельцу, сохранить отложенный бэкап."""
    from .telegram_bot import BACKUP_MIN_GAP_SEC
    while True:
        await asyncio.sleep(120)
        b._beats["traffic"] = time.time()
        try:
            b.traffic.update()
            text = b.traffic.level_change_text()
            if text:
                await b.notify(text)
            if getattr(b, "_backup_pending", False) and time.time() - b._last_backup >= BACKUP_MIN_GAP_SEC:
                await b._backup_now()
        except Exception:
            log.exception("Учёт трафика")


def set_used(meter: Traffic, gb_used: float, now: float | None = None) -> None:
    """Подстроиться под цифру из Render (Dashboard → Billing): сколько ГБ уже потрачено в этом месяце."""
    meter.update(now)
    meter.st["used"] = int(max(0.0, gb_used) * GB)


def register(b: ArbitrageBot) -> None:
    from aiogram.filters import Command, CommandObject
    from aiogram.types import Message

    @b.dp.message(Command("traffic", "трафик"))
    async def cmd_traffic(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        uid = message.from_user.id if message.from_user else None
        args = (command.args or "").replace(",", ".").split()
        tr = b.traffic
        if args and args[0] in ("used", "budget"):
            if b.owner_id is not None and not b.is_owner(uid):
                await b._send(message.chat.id, "Это может только владелец бота.")
                return
            try:
                val = float(args[1])
            except (IndexError, ValueError):
                await b._send(message.chat.id, "Напиши число в ГБ, например: /traffic used 3.2 или /traffic budget 4")
                return
            if args[0] == "used":
                set_used(tr, val)
            else:
                if not 0.2 <= val <= 10_000:
                    await b._send(message.chat.id, "Бюджет — от 0.2 до 10000 ГБ")
                    return
                tr.st["budget_gb"] = val
            b.copy.changes += 1
            await b._backup_now()
        else:
            tr.update()
        await b._send(message.chat.id, tr.text())
