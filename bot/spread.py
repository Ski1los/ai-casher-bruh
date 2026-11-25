"""⚖️ Спред-арбитраж между биржами: купить монету там, где она дешевле, перевезти и продать там, где дороже.

Как это работает (по шагам, без магии):
1. Сканер каждую минуту сравнивает цены всех общих монет Bybit и BingX (сотни пар).
2. Для лучших расхождений бот делает **полный анализ ДО покупки**:
   ключи → открыт ли вывод монеты на бирже покупки и ввод на бирже продажи (ловит блокировки вроде Terra,
   техработы сетей, делистинг) → общая открытая сеть и её комиссия → быстрая ли сеть (≤ 15 мин) → стаканы
   обеих бирж на ТВОЮ сумму (а не «красивая» цена) → минимальные ордера и минимум вывода → одна ли это монета
   (контракт; при огромном спреде без подтверждения — отказ) → прибыль после ВСЕХ комиссий: покупка, вывод,
   продажа → адрес в адресной книге (Bybit сообщает это через API).
3. Только если всё зелёное и прибыль ≥ порога (по умолчанию 1% и $0.03): покупка → вывод на адрес пополнения
   второй биржи (его берёт сам бот) → ждёт зачисления → продаёт.
4. Если после покупки вывод не прошёл — продаёт монеты обратно на той же бирже (убыток — комиссии, ~0.2–0.5%).
   Если пока монеты ехали, цена упала — ждёт безубытка до 2 ч, но продаёт сразу при просадке 3% (стоп).

Режим: 🧪 ТЕСТ (по умолчанию, даже если остальной бот торгует по-настоящему) — сделки «на бумаге» по реальным
стаканам, со временем перевода и настоящими комиссиями, деньги не двигаются; 💵 РЕАЛЬНЫЙ — настоящие ордера и
выводы: нужны /live, разрешённые переводы (/transfer) и отдельное подтверждение /spread real. До 3 сделок разными монетами одновременно (/spread runs), лимиты: сделка ≤ max_trade, перевод ≤ лимита переводов, дневной убыток.

Честно: на $5 таких сделок мало — комиссия вывода монеты (часто $0.1–0.5) съедает 2–10% суммы, а большие спреды
обычно бывают там, где биржа закрыла ввод/вывод (бот это видит и не лезет). Тест покажет реальную картину.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .sim import simulate_buy, simulate_sell
from .trading import TradeError, TradeRecord, human_error
from .transfers import canon_net, floor_step, fmt, mask

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

# Сколько обычно идёт перевод между биржами по сети (вывод + подтверждения + зачисление), минуты. С запасом.
NET_MIN = {
    "APT": 3, "SUI": 3, "SOL": 3, "TRC20": 4, "XLM": 2, "XRP": 3, "TON": 4, "ALGO": 3, "NEAR": 3, "BEP20": 4,
    "ARB": 6, "OP": 6, "BASE": 6, "AVAXC": 4, "POLYGON": 10, "ATOM": 4, "INJ": 3, "SEI": 3, "HBAR": 3, "EOS": 3,
    "DOT": 6, "KAS": 5, "TIA": 4, "OSMO": 4, "CELO": 4, "FTM": 4, "SONIC": 4, "ZKSYNCERA": 8, "LINEA": 8,
    "ERC20": 12, "ADA": 12, "LTC": 30, "DOGE": 40, "BCH": 60, "ETC": 60, "BTC": 60,
}
MAX_NET_MIN = 15          # дольше — цена успеет уйти
SUSPECT_PCT = 3.0         # такой спред без подтверждения «это одна монета» — подозрителен
HOLD_SEC = 2 * 3600       # монеты доехали, а продать в плюс нельзя — ждём безубытка не дольше
STOP_PCT = 3.0            # просадка от безубытка, при которой продаём сразу
SLOW_SEC = 3 * 3600
CANDIDATES = 6            # сколько связок за скан проверяем полностью (бережём лимиты бирж)
MAX_RUNS = 3              # сколько сделок (разных монет) держим одновременно по умолчанию; /spread runs 1–5
MAX_RUNS_CAP = 5
MIN_VOL_USD = 100_000     # оборот за сутки на КАЖДОЙ бирже: на тонких монетах большой спред — обычно ловушка
STALE_SEC = 12 * 3600     # спред держится дольше — значит, перевезти монеты на деле нельзя (иначе его бы закрыли)
STALE_PCT = 2.0
SEEN_GAP_SEC = 900        # пропал из скана дольше — «висящий» спред считаем заново
VOL_SIGMA = 2.0           # прибыль должна быть больше обычного хода цены за время перевода (2σ ≈ 95% случаев)
PAPER_STUCK_SEC = HOLD_SEC + 3600        # тест: доехали, а продать не вышло 3 ч — в жизни монеты застряли бы
LIVE_SENT_MAX_SEC = 24 * 3600            # реально: перевод идёт дольше суток
LIVE_SELL_MAX_SEC = HOLD_SEC + 6 * 3600  # реально: монеты на месте, а продать не вышло 8 ч
STRUCT = {"wallet", "suspect", "market", "net", "stale"}  # проблема монеты, а не рынка — копим «штрафы»
STRIKES = 2               # столько структурных отказов за сутки → карантин
BAN_STEPS = (86400, 3 * 86400, 7 * 86400)  # карантин растёт: 1 день → 3 дня → 7 дней
BAN_RUN_SEC = 7 * 86400   # сделка застряла — монета в карантин на неделю
# известно проблемные: спред по ним «висит» днями, потому что ввод/вывод сети годами то закрыт, то на техработах.
# Снять: /spread unban LUNC
SEED_BAD = {
    "LUNC": "Terra Classic: ввод/вывод на биржах часто закрыт или на техработах — спред «висит» днями, перевезти нельзя",
    "USTC": "Terra Classic (бывший UST): ввод/вывод часто закрыт, цена без опоры — спред «висит» днями",
}
RUNS_KEEP = 30
CHECKS_KEEP = 8
BOOK_CACHE_SEC = 600
ACTIVE = ("sent", "arrived", "back")
DEFAULTS = {"on": False, "paper": True, "min_pct": 1.0, "min_usd": 0.03, "max_usd": 10.0, "runs": [], "checks": [],
            "reasons": {}, "cool": {}, "told": {}, "totals": {"test": [0, 0.0], "live": [0, 0.0]},
            "max_runs": MAX_RUNS, "bad": {}, "strikes": {}, "seeded": False, "stuck": 0}
REASONS = {
    "fees": "после всех комиссий прибыль меньше порога",
    "wallet": "вывод/ввод монеты закрыт или нет общей сети",
    "net": "сеть медленная или её время неизвестно",
    "book": "в стакане мало заявок на эту сумму",
    "min": "меньше минимального ордера или вывода",
    "money": "мало свободных USDT на бирже покупки",
    "addr": "адреса нет в адресной книге биржи",
    "suspect": "не подтверждено, что это одна и та же монета",
    "limits": "лимиты сделок или переводов",
    "keys": "ключи, права или переводы не разрешены",
    "market": "пара не торгуется",
    "error": "ошибка запроса к бирже",
    "stale": "спред держится больше 12 ч — перевезти монеты на деле нельзя",
    "volatile": "цена за время перевода обычно уходит больше прибыли",
}
COOL_SEC = {"fees": 300, "book": 300, "error": 300, "money": 900, "keys": 900, "limits": 1800, "min": 1800,
            "wallet": 1800, "addr": 1800, "market": 3600, "net": 6 * 3600, "suspect": 12 * 3600, "stale": 6 * 3600,
            "volatile": 1800}
TELL_ADDR_SEC = 6 * 3600  # «добавь адрес в книгу» по одной связке — не чаще


@dataclass
class SpreadCheck:
    """Полный анализ одной связки «купить на src → перевезти → продать на dst»."""

    coin: str
    src: str
    dst: str
    symbol: str
    gross: float = 0.0                 # спред по тикерам, %
    checks: list[tuple[str, str]] = field(default_factory=list)
    reason: str = ""                   # код отказа; пусто — можно торговать
    usd: float = 0.0                   # потратим USDT
    coins: float = 0.0                 # купим монет (после торговой комиссии)
    send: float = 0.0                  # отправим (монеты минус комиссия вывода)
    fee: float = 0.0                   # комиссия вывода, монет
    fee_usd: float = 0.0
    network: str = ""
    minutes: float = 0.0
    received: float = 0.0              # получим USDT после продажи
    address: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason

    @property
    def profit(self) -> float:
        return self.received - self.usd

    @property
    def pct(self) -> float:
        return self.profit / self.usd * 100 if self.usd else 0.0

    def add(self, status: str, text: str) -> None:
        self.checks.append((status, text))

    def no(self, code: str, text: str) -> SpreadCheck:
        self.add("bad", text)
        self.reason = self.reason or code
        return self


def plan_reason(texts: list[str]) -> str:
    """Код причины по тексту отказа анализа перевода (bot/transfers.py)."""
    joined = " ".join(texts).lower()
    for code, words in (("keys", ("ключ", "не разрешены", "права")), ("wallet", ("закрыт", "общей сети", "нет на ")),
                        ("limits", ("лимит",)), ("min", ("минимума",)), ("fees", ("комиссия",)),
                        ("addr", ("адрес",))):
        if any(w in joined for w in words):
            return code
    return "error"


class Spread:
    def __init__(self, b: ArbitrageBot) -> None:
        self.b = b
        self._book: dict[tuple[str, str], tuple[float, list[dict] | None]] = {}
        self._busy = asyncio.Lock()
        self._seen: dict[str, list[float]] = {}  # связка → [впервые, последний раз] в сканах подряд
        self.skipped: dict[str, int] = {}        # что отсеяно до полного анализа на последнем скане

    # --- состояние ---------------------------------------------------------------------------------

    @property
    def st(self) -> dict:
        st = self.b.trader.state.spread
        if not isinstance(st, dict):
            st = self.b.trader.state.spread = {}
        for k, v in DEFAULTS.items():
            if k not in st:
                st[k] = ({kk: list(vv) for kk, vv in v.items()} if k == "totals" else
                         type(v)(v) if isinstance(v, (list, dict)) else v)
        if not st["seeded"]:  # один раз: снятый руками карантин больше не возвращаем
            st["seeded"] = True
            for coin, why in SEED_BAD.items():
                st["bad"].setdefault(coin, {"until": time.time() + 30 * 86400, "why": why, "n": 1, "auto": False})
        return st

    # --- карантин проблемных монет -----------------------------------------------------------------

    def bad(self, coin: str, now: float | None = None) -> dict | None:
        """Монета в карантине? (запись с причиной и сроком) — такие связки бот даже не проверяет."""
        now = time.time() if now is None else now
        x = self.st["bad"].get(coin)
        if x and float(x.get("until") or 0) > now:
            return x
        return None

    def ban(self, coin: str, sec: float, why: str, now: float | None = None, auto: bool = True) -> dict:
        now = time.time() if now is None else now
        bad = self.st["bad"]
        old = bad.get(coin) or {}
        x = {"until": max(now + sec, float(old.get("until") or 0)), "why": why[:200], "n": int(old.get("n") or 0) + 1,
             "auto": auto, "at": now}
        bad[coin] = x
        self.st["strikes"].pop(coin, None)
        if len(bad) > 60:  # память чистая: истёкшие и самые старые — прочь
            for k in sorted(bad, key=lambda k: float(bad[k].get("until") or 0))[:len(bad) - 60]:
                bad.pop(k, None)
        return x

    def unban(self, coin: str) -> bool:
        self.st["strikes"].pop(coin, None)
        return self.st["bad"].pop(coin, None) is not None

    def strike(self, coin: str, why: str, now: float | None = None) -> dict | None:
        """Структурный отказ (вывод закрыт, разные токены, «висящий» спред…). STRIKES за сутки → карантин."""
        now = time.time() if now is None else now
        sts = self.st["strikes"]
        hits = [t for t in sts.get(coin, []) if now - float(t) < 86400] + [now]
        sts[coin] = hits[-STRIKES:]
        if len(hits) < STRIKES:
            return None
        n = int((self.st["bad"].get(coin) or {}).get("n") or 0)
        return self.ban(coin, BAN_STEPS[min(n, len(BAN_STEPS) - 1)], why, now)

    def bad_rows(self, now: float | None = None) -> list[tuple[str, dict]]:
        now = time.time() if now is None else now
        return sorted(((c, x) for c, x in self.st["bad"].items() if float(x.get("until") or 0) > now),
                      key=lambda cx: -float(cx[1].get("until") or 0))

    @property
    def max_runs(self) -> int:
        try:
            return max(1, min(MAX_RUNS_CAP, int(self.st["max_runs"])))
        except (TypeError, ValueError):
            return MAX_RUNS

    def name(self, ex: str) -> str:
        return self.b.scanner.display_name(ex)

    def when(self, ts: float) -> str:
        off = float(getattr(self.b.settings, "report_utc_offset", 3) or 0)
        return time.strftime("%d.%m %H:%M", time.gmtime(ts + off * 3600))

    @property
    def live(self) -> bool:
        """Реальные сделки — только если торговля в реальном режиме (/live) И спред переключён на реальные
        (/spread real, с подтверждением). Иначе — тест на бумаге, даже когда остальной бот торгует по-настоящему."""
        return bool(self.b.trader.state.live) and not self.st["paper"]

    def active(self) -> list[dict]:
        return [r for r in self.st["runs"] if r.get("stage") in ACTIVE]

    @property
    def wants_scan(self) -> bool:
        return bool(self.st["on"] or self.active())

    def problems(self) -> list[str]:
        """Что мешает работать прямо сейчас (пусто — готов)."""
        t = self.b.trader
        out = []
        have = [n for n in self.b.scanner.exchanges if n in t.creds and t.key_status.get(n, "ok") == "ok"]
        if len(have) < 2:
            out.append("нужны рабочие API-ключи обеих бирж (/keys)")
        if self.live and not self.b.transfers.st["allowed"]:
            out.append("в реальном режиме нужны разрешённые переводы: /transfer → «🔓 Разрешить переводы»")
        t._roll_day()
        if self.live and t.state.day_pnl_usd <= -self.b.settings.daily_loss_usd:
            out.append(f"дневной убыток достиг лимита ${self.b.settings.daily_loss_usd:g} — продолжу завтра")
        return out

    # --- анализ ------------------------------------------------------------------------------------

    def candidates(self, result, now: float | None = None, track: bool = True) -> list:
        """Связки для полного анализа. До него отсеиваем то, что проверять бессмысленно: монеты в карантине,
        тонкие рынки (оборот < $100k за сутки на одной из бирж) и спреды, которые «висят» больше 12 ч."""
        quote = self.b.settings.quote
        t = self.b.trader
        now = time.time() if now is None else now
        busy = {r["coin"] for r in self.active()}
        cool = self.st["cool"]
        skipped: dict[str, int] = {}
        out = []
        for o in getattr(result, "opportunities", None) or []:
            coin, q = (o.symbol.split("/") + [""])[:2]
            src, dst = o.buy.exchange, o.sell.exchange
            if q != quote or coin in busy or src not in t.creds or dst not in t.creds:
                continue
            key = f"{coin}:{src}:{dst}"
            if track:
                seen = self._seen.get(key)
                if seen is None or now - seen[1] > SEEN_GAP_SEC:
                    seen = self._seen[key] = [now, now]
                seen[1] = now
                if now - seen[0] >= STALE_SEC and o.gross_percent >= STALE_PCT and not self.bad(coin, now):
                    hours = (now - seen[0]) / 3600
                    self.ban(coin, BAN_STEPS[0], f"спред {o.gross_percent:.1f}% держится {hours:.0f} ч — на деле "
                                                 "перевезти монеты нельзя (иначе его бы давно закрыли)", now)
                    self.st["reasons"]["stale"] = int(self.st["reasons"].get("stale", 0)) + 1
            if self.bad(coin, now):
                skipped["bad"] = skipped.get("bad", 0) + 1
                continue
            vol = min(float(getattr(o.buy, "volume_usd", 0) or 0), float(getattr(o.sell, "volume_usd", 0) or 0))
            if vol < MIN_VOL_USD:
                skipped["volume"] = skipped.get("volume", 0) + 1
                continue
            if cool.get(key, 0) > now:
                skipped["cool"] = skipped.get("cool", 0) + 1
                continue
            out.append(o)
        if track:
            self.skipped = skipped
            for k in [k for k, v in self._seen.items() if now - v[1] > SEEN_GAP_SEC]:
                self._seen.pop(k, None)
        out.sort(key=lambda o: o.net_percent, reverse=True)
        return out

    async def _address_book(self, src: str, coin: str) -> list[dict] | None:
        key = (src, coin)
        hit = self._book.get(key)
        if hit and time.time() - hit[0] < BOOK_CACHE_SEC:
            return hit[1]
        rows = await self.b.transfers.address_book(src, coin)
        self._book[key] = (time.time(), rows)
        if len(self._book) > 40:
            self._book.pop(next(iter(self._book)))
        return rows

    async def evaluate(self, symbol: str, src: str, dst: str, gross: float = 0.0) -> SpreadCheck:
        b, t, tf = self.b, self.b.trader, self.b.transfers
        st = self.st
        coin, quote = symbol.split("/")
        ev = SpreadCheck(coin, src, dst, symbol, gross)
        bad = self.bad(coin)
        if bad:
            ev.add("warn", f"{coin} в карантине до {self.when(float(bad['until']))}: {bad.get('why', '')} — сам не беру "
                           f"(снять: /spread unban {coin})")
        for n in (src, dst):
            if n not in t.creds:
                return ev.no("keys", f"нет API-ключа {self.name(n)} — /keys")
            if t.key_status.get(n, "ok") != "ok":
                return ev.no("keys", f"ключ {self.name(n)} не работает: {t.key_status[n]}")
        wall = t.coin_wall(src, coin)
        if wall:
            return ev.no("wallet", wall)
        ma, mb = b.scanner.market(src, symbol), b.scanner.market(dst, symbol)
        if not ma or not mb or ma.get("active") is False or mb.get("active") is False:
            return ev.no("market", f"пара {symbol} не торгуется на одной из бирж")
        min_amt_a, min_cost_a = t._limits(ma)
        min_amt_b, min_cost_b = t._limits(mb)
        fee_a, fee_b = float(ma.get("taker") or 0.001), float(mb.get("taker") or 0.001)
        # 1. деньги и сумма сделки
        try:
            free = await t.free(src, quote)
        except Exception as e:
            return ev.no("error", f"баланс {self.name(src)}: {human_error(e)}")
        cap = min(float(st["max_usd"]), float(b.settings.max_trade_usd), float(tf.st["max_usd"]))
        usd = math.floor(min(cap, free * 0.995) * 100) / 100
        need = max(min_cost_a, min_cost_b * 1.05, 1.0)
        if usd < need:
            return ev.no("money", f"на {self.name(src)} свободно {free:.2f} {quote}, для сделки нужно от {need:.2f} "
                                  f"(минимальные ордера обеих бирж)")
        # 2. стаканы на нашу сумму
        try:
            book_a, book_b = await asyncio.gather(b.scanner.order_book(src, symbol), b.scanner.order_book(dst, symbol))
        except Exception as e:
            return ev.no("error", f"стаканы: {human_error(e)}")
        spent, coins = simulate_buy(book_a.get("asks") or [], usd, fee_a)
        if coins <= 0 or spent < usd * 0.999:
            return ev.no("book", f"в стакане {self.name(src)} мало продавцов на ${usd:.2f}")
        if coins < min_amt_a:
            return ev.no("min", f"меньше минимального ордера {self.name(src)}")
        ev.usd, ev.coins = spent, coins
        ev.add("ok", f"купить {fmt(coins)} {coin} на {self.name(src)} за ${spent:.2f} (по стакану)")
        # 3. перевоз: статус монеты, сеть, комиссия, минимум, адрес — тот же анализ, что перед любым выводом
        try:
            plan = await tf.analyze(coin, "all", src, dst, source="spread", assume_free=coins, paper=not self.live)
        except Exception as e:
            return ev.no("error", f"анализ перевода: {human_error(e)}")
        bad = [x for s, x in plan.checks if s == "bad"]
        for x in bad:
            ev.add("bad", x)
        for s, x in plan.checks:
            if s == "warn" and "цену монеты" not in x:
                ev.add("warn", x)
        if bad or not plan.network:
            ev.reason = plan_reason(bad) if bad else "wallet"
            return ev
        ev.network, ev.fee, ev.send, ev.address = plan.network, float(plan.fee or 0.0), plan.amount, plan.address
        price = spent / coins if coins else 0.0
        ev.fee_usd = ev.fee * price
        ev.add("ok", f"вывод {coin} с {self.name(src)} и ввод на {self.name(dst)} открыты, сеть {ev.network}, комиссия "
                     f"{fmt(ev.fee)} {coin} (≈${ev.fee_usd:.3f})")
        minutes = NET_MIN.get(canon_net(ev.network))
        if minutes is None:
            return ev.no("net", f"сеть {ev.network}: не знаю, сколько идёт перевод — рисковать не буду")
        if minutes > MAX_NET_MIN:
            return ev.no("net", f"сеть {ev.network} медленная (~{minutes} мин): цена успеет уйти")
        ev.minutes = minutes
        # 4. одна ли это монета (у разных бирж бывают разные токены с одним тикером)
        try:
            cs, cd = await asyncio.gather(tf.currency(src, coin), tf.currency(dst, coin))
            ca = ((cs.get("nets") or {}).get(ev.network) or {}).get("contract")
            cb = ((cd.get("nets") or {}).get(ev.network) or {}).get("contract")
        except Exception:
            ca = cb = None
        if ca and cb and ca.lower() != cb.lower():
            return ev.no("suspect", f"контракты {coin} на биржах разные ({mask(ca)} и {mask(cb)}) — это разные токены "
                                    "с одним названием, переводить нельзя")
        if ca and cb:
            ev.add("ok", "контракт токена на обеих биржах совпадает")
        elif gross >= SUSPECT_PCT:
            return ev.no("suspect", f"спред {gross:.1f}% подозрительно большой, а подтвердить, что на биржах одна и та же "
                                    f"монета (контракт), нельзя")
        # 5. продажа на второй бирже
        received, left = simulate_sell(book_b.get("bids") or [], ev.send, fee_b)
        if left > ev.send * 1e-6:
            return ev.no("book", f"в стакане {self.name(dst)} мало покупателей на {fmt(ev.send)} {coin}")
        if ev.send < min_amt_b or received < min_cost_b:
            return ev.no("min", f"после комиссии вывода продажа на {self.name(dst)} меньше минимального ордера")
        ev.received = received
        ev.add("ok" if ev.profit > 0 else "bad",
               f"продать {fmt(ev.send)} {coin} на {self.name(dst)} → ${received:.2f}: итог {ev.profit:+.3f}$ "
               f"({ev.pct:+.2f}%) после ВСЕХ комиссий, перевод ~{minutes:g} мин")
        if ev.pct < float(st["min_pct"]) or ev.profit < float(st["min_usd"]):
            ev.reason = "fees"
            ev.add("bad", f"порог: от {float(st['min_pct']):g}% и ${float(st['min_usd']):g} — не беру")
            return ev
        # 5б. риск хода цены, пока монеты едут: обычное движение за `minutes` минут должно быть меньше прибыли
        move = await self._typical_move(dst, symbol, minutes)
        if move is None:
            ev.add("info", "ход цены за время перевода не проверил (биржа не отдала свечи)")
        elif move >= ev.pct:
            return ev.no("volatile", f"за ~{minutes:g} мин цена {coin} обычно уходит на ±{move:.2f}% — больше прибыли "
                                     f"{ev.pct:.2f}%: высокий риск продать в минус")
        else:
            ev.add("ok", f"ход цены за ~{minutes:g} мин обычно ±{move:.2f}% — меньше прибыли {ev.pct:.2f}%")
        # 6. адресная книга (последней: если только она мешает — скажем, какой адрес добавить)
        if src == "bybit":
            if not self.live:
                ev.add("info", f"для реальной сделки адрес {ev.address} ({ev.network}) должен быть в адресной книге Bybit")
            else:
                try:
                    rows = await self._address_book(src, coin)
                except Exception as e:
                    return ev.no("addr", f"адресную книгу {self.name(src)} прочитать не смог: {human_error(e)}")
                if rows is not None:
                    mine = [x for x in rows if str(x.get("address") or "").strip().lower() == ev.address.lower()
                            and (not x.get("chain") or canon_net(x.get("chain")) == canon_net(ev.network))]
                    if not mine:
                        return ev.no("addr", f"адреса {ev.address} ({ev.network}) нет в адресной книге Bybit — Bybit "
                                             "выводит через API только туда. Активы → Вывод → Управление адресами")
                    if all(str(x.get("status")) == "1" for x in mine):
                        return ev.no("addr", "адрес в книге Bybit новый — вывод на него разрешат через 24 ч")
                    ev.add("ok", "адрес есть в адресной книге Bybit")
        return ev

    async def _typical_move(self, ex_name: str, symbol: str, minutes: float) -> float | None:
        """Обычный ход цены за `minutes` минут, % (2σ по минутным свечам за последний час). None — свечей нет."""
        ex = self.b.scanner.exchanges.get(ex_name)
        if ex is None or not hasattr(ex, "fetch_ohlcv"):
            return None
        try:
            rows = await ex.fetch_ohlcv(symbol, "1m", limit=61)
        except Exception:
            return None
        closes = [float(r[4]) for r in rows or [] if r and len(r) > 4 and r[4]]
        if len(closes) < 20:
            return None
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        return VOL_SIGMA * sd * math.sqrt(max(1.0, minutes)) * 100

    def _note(self, ev: SpreadCheck, now: float | None = None) -> None:
        now = time.time() if now is None else now
        st = self.st
        st["checks"].append({"at": now, "coin": ev.coin, "src": ev.src, "dst": ev.dst, "gross": round(ev.gross, 3),
                             "pct": round(ev.pct, 3) if ev.usd and ev.received else None, "reason": ev.reason,
                             "why": next((x for s, x in reversed(ev.checks) if s == "bad"), "")[:160]})
        del st["checks"][:-CHECKS_KEEP]
        if ev.reason:
            st["reasons"][ev.reason] = int(st["reasons"].get(ev.reason, 0)) + 1
            st["cool"][f"{ev.coin}:{ev.src}:{ev.dst}"] = now + COOL_SEC.get(ev.reason, 600)
            if ev.reason in STRUCT:
                why = next((x for s, x in reversed(ev.checks) if s == "bad"), REASONS.get(ev.reason, ev.reason))
                self.strike(ev.coin, why, now)
        for k in [k for k, v in st["cool"].items() if v < now]:
            st["cool"].pop(k, None)

    # --- сделка ------------------------------------------------------------------------------------

    def _new_run(self, ev: SpreadCheck, mode: str) -> dict:
        now = time.time()
        return {"id": secrets.token_hex(3), "mode": mode, "coin": ev.coin, "src": ev.src, "dst": ev.dst,
                "symbol": ev.symbol, "network": ev.network, "minutes": ev.minutes, "stage": "sent", "opened": now,
                "spent": round(ev.usd, 6), "bought": ev.coins, "sent": ev.send, "fee": ev.fee,
                "exp_usd": round(ev.received, 6), "exp_pct": round(ev.pct, 3), "gross": round(ev.gross, 3),
                "eta": now + ev.minutes * 60, "item": None, "item_at": None, "got": None, "arrived_at": None,
                "received": None, "pnl": None, "closed": None, "note": ""}

    async def open(self, ev: SpreadCheck) -> dict:
        """Открыть сделку по готовому анализу: в тесте — на бумаге, в реальном режиме — покупка и вывод."""
        if not self.live:
            run = self._new_run(ev, "test")
            self._keep(run)
            return run
        t, tf = self.b.trader, self.b.transfers
        ex = t._ex(ev.src)
        m = self.b.scanner.market(ev.src, ev.symbol)
        fee_a = float(m.get("taker") or 0.001)
        async with t._lock:
            t._check_limits(ev.usd, True, "auto")
            book = await self.b.scanner.order_book(ev.src, ev.symbol)  # цена за секунды анализа не ушла?
            _, coins = simulate_buy(book.get("asks") or [], ev.usd, fee_a)
            slip = self.b.settings.max_slippage_percent / 100
            if coins < ev.coins * (1 - slip):
                raise TradeError(f"цена {ev.coin} на {self.name(ev.src)} выросла, пока проверял — не покупаю")
            try:
                order = await ex.create_market_buy_order_with_cost(ev.symbol, t._cost(ex, ev.symbol, ev.usd))
            except Exception as e:
                raise TradeError(human_error(e)) from e
            spent, got = await t._settle(ex, ev.src, order, ev.symbol, "buy", fee_a)
            t._record(TradeRecord(time.time(), ev.src, "buy", ev.symbol, True, spent, self.b.settings.quote, got, ev.coin,
                                  spent, source="spread", note=f"⚖️ спред: купил для перевоза на {self.name(ev.dst)}",
                                  order_id=str((order or {}).get("id"))))
        run = self._new_run(ev, "live")
        run.update(spent=round(spent, 6), bought=got, stage="bought")
        self._keep(run)
        try:
            cur = await tf.currency(ev.src, ev.coin)
            prec = ((cur.get("nets") or {}).get(ev.network) or {}).get("precision")
            amount = floor_step(min(got, await tf.free_coin(ev.src, ev.coin)) - ev.fee, prec)
            plan = await tf.analyze(ev.coin, amount, ev.src, ev.dst, source="spread", force=True)
            if not plan.ok:
                raise TradeError("; ".join(x for s, x in plan.checks if s == "bad"))
            tf.keep(plan)
            item = await tf.execute(plan.id)
        except Exception as e:
            run["note"] = f"вывод не прошёл: {e if isinstance(e, TradeError) else human_error(e)}"[:300]
            run["stage"] = "back"
            if "адрес" in run["note"].lower() or "address" in run["note"].lower():
                self.st["cool"][f"{ev.coin}:{ev.src}:{ev.dst}"] = time.time() + COOL_SEC["addr"]
            self.b.save_trading()
            return run
        run.update(stage="sent", sent=item["amount"], fee=item.get("fee") or ev.fee, item=item.get("id") or None,
                   item_at=item.get("at"), network=item.get("network") or ev.network, eta=time.time() + ev.minutes * 60)
        self.b.save_trading()
        return run

    def _keep(self, run: dict) -> None:
        runs = self.st["runs"]
        runs.append(run)
        if len(runs) > RUNS_KEEP:  # старые завершённые убираем, активные — никогда
            done = [r for r in runs if r.get("stage") not in ACTIVE]
            for r in done[:len(runs) - RUNS_KEEP]:
                runs.remove(r)

    def _item(self, run: dict) -> dict | None:
        for it in self.b.transfers.st["items"]:
            if run.get("item") and str(it.get("id")) == str(run["item"]):
                return it
            if run.get("item_at") and it.get("at") == run["item_at"] and it.get("coin") == run["coin"]:
                return it
        return None

    @staticmethod
    def decide(run: dict, est: float, now: float) -> str | None:
        """Продавать ли доехавшие монеты: причина продажи или None (ждать)."""
        be = float(run["spent"])
        if est >= be:
            return "в плюс" if est > be * 1.0005 else "в ноль"
        if est <= be * (1 - STOP_PCT / 100):
            return f"стоп: цена ушла больше чем на {STOP_PCT:g}%"
        if now - float(run.get("arrived_at") or now) >= HOLD_SEC:
            return "ждал безубытка 2 ч"
        return None

    def _overdue(self, run: dict, now: float) -> str | None:
        """Сделка застряла и ждать дальше бессмысленно? Текст причины или None."""
        stage, live = run["stage"], run["mode"] == "live"
        since = float(run.get("arrived_at") or run["opened"])
        if not live:
            if stage in ("arrived", "back") and now - since > PAPER_STUCK_SEC:
                return ("тест: монеты «доехали», но продать их не вышло 3 ч (нет покупателей или пара не торгуется) — "
                        "в реальности они бы застряли")
            return None
        if stage == "sent" and now - float(run["opened"]) > LIVE_SENT_MAX_SEC:
            return "перевод идёт больше суток — проверь /transfer и баланс бирж; бот больше не ждёт"
        if stage in ("arrived", "back") and now - since > LIVE_SELL_MAX_SEC:
            where = run["dst"] if stage == "arrived" else run["src"]
            return (f"монеты на {self.name(where)}, но продать их не вышло 8 ч — продай вручную: "
                    f"/sell {where} {run['coin']} all")
        return None

    async def _sell(self, run: dict, where: str, qty: float) -> float:
        """Реальная продажа монет сделки на бирже where. Возвращает полученные USDT."""
        t = self.b.trader
        ex = t._ex(where)
        m = self.b.scanner.market(where, run["symbol"])
        fee = float(m.get("taker") or 0.001)
        async with t._lock:
            try:
                order = await ex.create_market_sell_order(run["symbol"], t._amount(ex, run["symbol"], qty))
            except Exception as e:
                raise TradeError(human_error(e)) from e
            _, got = await t._settle(ex, where, order, run["symbol"], "sell", fee)
        return got

    def _finish(self, run: dict, received: float, why: str, now: float) -> str:
        quote = self.b.settings.quote
        note = "продал обратно на бирже покупки — вывод не прошёл" if why == "back" else f"продал {why}"
        run.update(received=round(received, 6), pnl=round(received - float(run["spent"]), 6), closed=now, stage="done",
                   why=note)
        if why == "back":  # вывод не прошёл уже после покупки — монета проблемная
            self.ban(run["coin"], BAN_STEPS[1], "вывод не прошёл после покупки: " + str(run.get("note") or "")[:120], now)
        elif why.startswith("стоп"):
            self.strike(run["coin"], f"стоп-лосс: цена ушла больше чем на {STOP_PCT:g}%, пока монеты ехали", now)
        tot = self.st["totals"].setdefault(run["mode"], [0, 0.0])
        tot[0] += 1
        tot[1] = round(tot[1] + run["pnl"], 6)
        live = run["mode"] == "live"
        self.b.trader._record(TradeRecord(now, run["dst"] if why != "back" else run["src"], "spread",
                                          f"{run['coin']} {run['src']}→{run['dst']}", live, float(run["spent"]), quote,
                                          received, quote, float(run["spent"]), pnl_usd=run["pnl"], source="spread",
                                          note=note))
        if live and self.b.trader.state.day_pnl_usd <= -self.b.settings.daily_loss_usd and self.st["on"]:
            self.st["on"] = False
            run["note"] = (run.get("note") or "") + " · дневной лимит убытка: спред выключен до /spread on"
        return self.run_text(run)

    async def step(self, now: float | None = None) -> list[str]:
        """Ведёт открытые сделки: доехали ли монеты, продать ли. Возвращает сообщения для чата."""
        now = time.time() if now is None else now
        msgs: list[str] = []
        t = self.b.trader
        for run in self.active():
            title = f"{run['coin']} {self.name(run['src'])} → {self.name(run['dst'])}"
            live = run["mode"] == "live"
            try:
                if live and not t.state.live:  # 🛑 стоп-кран: сами не продаём, говорим, где монеты
                    if not run.get("told_stop"):
                        run["told_stop"] = True
                        where = run["src"] if run["stage"] in ("back",) else run["dst"]
                        msgs.append(f"⏸ <b>Спред на паузе</b> (реальный режим выключен): {html.escape(title)}. "
                                    f"Монеты {html.escape(run['coin'])} — на {html.escape(self.name(where))}"
                                    + (" (ещё в пути)" if run["stage"] == "sent" else "")
                                    + f". Продать вручную: <code>/sell {where} {html.escape(run['coin'])} all</code>")
                    continue
                stuck = self._overdue(run, now)
                if stuck:  # застрявшая сделка не держит слот вечно (раньше могла висеть днями)
                    run.update(stage="stuck", closed=now, note=stuck)
                    self.st["stuck"] = int(self.st.get("stuck") or 0) + 1
                    self.ban(run["coin"], BAN_RUN_SEC, f"сделка застряла: {stuck}", now)
                    msgs.append(f"⚠️ Спред {html.escape(title)}: {html.escape(stuck)}. {html.escape(run['coin'])} — в "
                                "карантин на 7 дней, место освобождено для новых сделок")
                    continue
                if run["stage"] == "sent":
                    if not live:
                        if now >= float(run["eta"]):
                            run.update(stage="arrived", got=run["sent"], arrived_at=now)
                    else:
                        it = self._item(run)
                        if it is None:
                            run.update(stage="stuck", note="перевод потерялся из журнала — проверь баланс", closed=now)
                            msgs.append(f"⚠️ Спред {html.escape(title)}: не нашёл перевод в журнале. Проверь /balance")
                            continue
                        if it.get("status") == "done":
                            run.update(stage="arrived", got=float(it.get("got") or run["sent"]), arrived_at=now)
                        elif it.get("status") == "failed":
                            run.update(stage="back", note="биржа отменила вывод — монеты вернулись")
                        elif now - float(run["opened"]) > SLOW_SEC and not run.get("slow_told"):
                            run["slow_told"] = True
                            msgs.append(f"⏳ Спред {html.escape(title)}: перевод идёт дольше 3 ч. Слежу дальше (/transfer)")
                if run["stage"] == "arrived":
                    dst = run["dst"]
                    m = self.b.scanner.market(dst, run["symbol"])
                    fee = float(m.get("taker") or 0.001)
                    qty = float(run["got"])
                    if live:
                        free = await t.free(dst, run["coin"])
                        if free < qty * 0.98:
                            note = await self.b.transfers.to_trading(dst, run["coin"], qty)
                            free = await t.free(dst, run["coin"])
                            if free < qty * 0.98:
                                if not run.get("fund_told"):
                                    run["fund_told"] = True
                                    msgs.append(f"⚠️ Спред {html.escape(title)}: монеты дошли, но не на торговом счёте "
                                                f"(свободно {fmt(free)}).{note} Продам, как только появятся")
                                continue
                        qty = min(qty, free)
                    book = await self.b.scanner.order_book(dst, run["symbol"])
                    est, left = simulate_sell(book.get("bids") or [], qty, fee)
                    if left > qty * 1e-6:
                        continue  # покупателей в стакане сейчас мало — подождём
                    if est < t._limits(m)[1] * 0.98:
                        run.update(stage="stuck", closed=now, note=f"{fmt(qty)} {run['coin']} на {self.name(dst)} "
                                                                   "меньше минимального ордера — продай вручную")
                        msgs.append(f"⚠️ Спред {html.escape(title)}: {html.escape(run['note'])}")
                        continue
                    why = self.decide(run, est, now)
                    if why is None:
                        continue
                    received = await self._sell(run, dst, qty) if live else est
                    msgs.append(self._finish(run, received, why, now))
                elif run["stage"] == "back":
                    src = run["src"]
                    m = self.b.scanner.market(src, run["symbol"])
                    qty = float(run["bought"])
                    if live:
                        qty = min(qty, await self.b.transfers.free_coin(src, run["coin"]))
                        if src == "bybit":
                            await self.b.transfers.to_trading(src, run["coin"], qty)
                        qty = min(qty, await t.free(src, run["coin"]))
                    if qty <= 0:
                        continue
                    received = await self._sell(run, src, qty)
                    msgs.append(self._finish(run, received, "back", now))
            except TradeError as e:
                if not run.get("err_told"):
                    run["err_told"] = True
                    msgs.append(f"⚠️ Спред {html.escape(title)}: {html.escape(str(e))}. Повторю на следующем скане")
            except Exception as e:
                log.info("Спред %s: %s", title, e)
        return msgs

    async def process(self, result, now: float | None = None) -> list[str]:
        """Каждый скан: ведём открытые сделки и, если есть свободные места (до max_runs разных монет), ищем новые."""
        if self._busy.locked():
            return []
        async with self._busy:
            msgs = await self.step(now)
            st = self.st
            cands = self.candidates(result)  # и для учёта «висящих» спредов — даже когда новых сделок не ищем
            if not st["on"] or len(self.active()) >= self.max_runs or self.problems():
                if msgs:
                    self.b.save_trading()
                return msgs
            if self.live and self.b.duplicate_running():
                return msgs
            told = st["told"]
            for o in cands[:CANDIDATES]:
                if len(self.active()) >= self.max_runs:
                    break
                if o.symbol.split("/")[0] in {r["coin"] for r in self.active()}:
                    continue
                ev = await self.evaluate(o.symbol, o.buy.exchange, o.sell.exchange, o.gross_percent)
                self._note(ev)
                if ev.reason == "addr" and ev.received > ev.usd:
                    key = f"{ev.coin}:{ev.src}:{ev.dst}"
                    if time.time() - float(told.get(key, 0)) > TELL_ADDR_SEC:
                        told[key] = time.time()
                        msgs.append(f"📒 <b>Спред {html.escape(ev.coin)} {ev.pct:+.2f}%</b> "
                                    f"({html.escape(self.name(ev.src))} → {html.escape(self.name(ev.dst))}) прошёл все "
                                    f"проверки, кроме адресной книги. Добавь в книгу {html.escape(self.name(ev.src))} "
                                    f"адрес <code>{html.escape(ev.address)}</code> (сеть {html.escape(ev.network)}) — "
                                    "и такие сделки бот будет брать сам.")
                if not ev.ok:
                    continue
                try:
                    run = await self.open(ev)
                except TradeError as e:
                    st["cool"][f"{ev.coin}:{ev.src}:{ev.dst}"] = time.time() + 1800
                    if time.time() - float(told.get("open_err", 0)) > 3600:
                        told["open_err"] = time.time()
                        msgs.append(f"⚠️ Спред {html.escape(ev.coin)}: не открыл — {html.escape(str(e))}")
                    break
                if run["mode"] == "live":  # тест на бумаге — одно сообщение с итогом, без лишнего шума
                    msgs.append(self.open_text(run))
            for k in [k for k, v in told.items() if time.time() - float(v) > 86400]:
                told.pop(k, None)
            self.b.save_trading()
            return msgs

    # --- тексты ------------------------------------------------------------------------------------

    def mode_word(self, mode: str) -> str:
        return "🧪 ТЕСТ (на бумаге)" if mode == "test" else "💵"

    def open_text(self, run: dict) -> str:
        coin = html.escape(run["coin"])
        head = "🧪 <b>Спред (ТЕСТ, на бумаге)</b>" if run["mode"] == "test" else "⚖️ <b>Спред: сделка открыта</b>"
        lines = [f"{head}: {coin} {html.escape(self.name(run['src']))} → {html.escape(self.name(run['dst']))}",
                 f"Купил {fmt(float(run['bought']))} {coin} за ${float(run['spent']):.2f} на "
                 f"{html.escape(self.name(run['src']))} (там дешевле на {float(run['gross']):.2f}%)"]
        if run["stage"] == "back":
            lines.append(f"⚠️ {html.escape(run['note'])}. Продаю обратно на {html.escape(self.name(run['src']))}")
        else:
            lines.append(f"Перевожу {fmt(float(run['sent']))} {coin} по сети {html.escape(run['network'])} "
                         f"(~{float(run['minutes']):g} мин), потом продам на {html.escape(self.name(run['dst']))}")
            lines.append(f"Ожидаю ${float(run['exp_usd']):.2f}: <b>{float(run['exp_usd']) - float(run['spent']):+.3f}$</b> "
                         f"({float(run['exp_pct']):+.2f}%) после всех комиссий")
        return "\n".join(lines)

    def run_text(self, run: dict) -> str:
        coin = html.escape(run["coin"])
        pnl = float(run.get("pnl") or 0)
        icon = "✅" if pnl >= 0 else "🔻"
        mode = "🧪 ТЕСТ: " if run["mode"] == "test" else ""
        mins = max(1, round((float(run.get("closed") or time.time()) - float(run["opened"])) / 60))
        title = (f"{coin} {html.escape(self.name(run['src']))} → {html.escape(self.name(run['dst']))}"
                 if run.get("arrived_at") else f"{coin} на {html.escape(self.name(run['src']))} (вернул обратно)")
        pct = pnl / float(run["spent"]) * 100 if float(run["spent"]) else 0.0
        line = (f"{icon} {mode}<b>Спред завершён</b>: {title}\n${float(run['spent']):.2f} → ${float(run['received']):.2f} = "
                f"<b>{pnl:+.3f}$</b> ({pct:+.2f}%) за {mins} мин · ожидал {float(run['exp_pct']):+.2f}%")
        if run.get("why"):
            line += f"\nПродал: {html.escape(run['why'].removeprefix('продал '))}"
        if run.get("note"):
            line += f"\n{html.escape(run['note'])}"
        return line

    def run_line(self, run: dict) -> str:
        coin = html.escape(run["coin"])
        path = f"{coin} {html.escape(self.name(run['src']))}→{html.escape(self.name(run['dst']))}"
        mode = "🧪" if run["mode"] == "test" else "💵"
        if run["stage"] == "done":
            pnl = float(run.get("pnl") or 0)
            return f"{mode} {'✅' if pnl >= 0 else '🔻'} {path}: {pnl:+.3f}$ ({float(run['spent']):.2f}$)"
        stage = {"sent": "монеты в пути", "arrived": "доехали, жду цену продажи", "back": "продаю обратно",
                 "bought": "куплено, вывожу", "stuck": "⚠️ " + str(run.get("note") or "застряло")}.get(run["stage"],
                                                                                                     run["stage"])
        left = max(0, round((float(run.get("eta") or 0) - time.time()) / 60))
        return f"{mode} ⏳ {path}: {stage}" + (f" (~{left} мин)" if run["stage"] == "sent" and left else "")

    def check_text(self, ev: SpreadCheck) -> str:
        icon = {"ok": "✅", "warn": "⚠️", "bad": "⛔", "info": "ℹ️"}
        head = (f"<b>{html.escape(ev.coin)}</b> {html.escape(self.name(ev.src))} → {html.escape(self.name(ev.dst))}"
                + (f" · спред {ev.gross:.2f}%" if ev.gross else ""))
        verdict = ("🟢 беру" if ev.ok else f"⛔ {html.escape(REASONS.get(ev.reason, ev.reason))}")
        lines = [f"{head} — {verdict}"]
        for s, x in ev.checks[-6:]:
            x = html.escape(x)
            if ev.address and html.escape(ev.address) in x:
                x = x.replace(html.escape(ev.address), f"<code>{html.escape(ev.address)}</code>")
            lines.append(f"{icon.get(s, '•')} {x}")
        return "\n".join(lines)

    def panel_text(self) -> str:
        st = self.st
        s = self.b.settings
        mode = "💵 <b>РЕАЛЬНЫЙ</b>: настоящие ордера и выводы" if self.live else \
            "🧪 <b>ТЕСТ</b>: сделки на бумаге по реальным стаканам, деньги не двигаются"
        if not self.live and not st["paper"]:
            mode += " (спред переключён на реальные, но торговля в тестовом режиме — /live)"
        lines = ["⚖️ <b>Спред-арбитраж между биржами</b>",
                 "Монета на одной бирже дешевле, чем на другой → бот покупает там, переводит монеты и продаёт там, "
                 "где дороже. Перед покупкой проверяет всё: вывод/ввод, сеть, комиссии, стаканы, одна ли это монета.",
                 "",
                 f"Статус: {'▶️ <b>работает</b>' if st['on'] else '⏸ выключен'} · режим {mode}",
                 f"Условия: прибыль после ВСЕХ комиссий от {float(st['min_pct']):g}% и ${float(st['min_usd']):g} · "
                 f"сделка до ${min(float(st['max_usd']), float(s.max_trade_usd)):g} · перевод не дольше {MAX_NET_MIN} мин · "
                 f"до {self.max_runs} сделок одновременно (разные монеты)"]
        probs = self.problems()
        if probs:
            lines += ["", "⛔ <b>Мешает</b>:"] + [f"• {html.escape(p)}" for p in probs]
        act = self.active()
        if act:
            lines += ["", "<b>Сейчас</b>:"] + [self.run_line(r) for r in act]
        tot = st["totals"]
        lines.append("")
        lines.append(f"Итоги: 🧪 тест — {int(tot.get('test', [0])[0])} сделок, {float(tot.get('test', [0, 0])[1]):+.3f}$ · "
                     f"💵 реально — {int(tot.get('live', [0])[0])}, {float(tot.get('live', [0, 0])[1]):+.3f}$")
        if st["checks"]:
            lines.append("\n<b>Последние проверки</b>:")
            for c in st["checks"][-5:]:
                verdict = "🟢 взял" if not c["reason"] else "⛔ " + REASONS.get(c["reason"], c["reason"])
                pct = f", итог {c['pct']:+.2f}%" if c.get("pct") is not None else ""
                lines.append(f"• {html.escape(c['coin'])} {html.escape(self.name(c['src']))}→"
                             f"{html.escape(self.name(c['dst']))}: спред {c['gross']:.2f}%{pct} — {html.escape(verdict)}")
        reasons = sorted(st["reasons"].items(), key=lambda kv: -kv[1])[:4]
        if reasons:
            lines.append("Чаще всего отказ: " + "; ".join(f"{html.escape(REASONS.get(k, k))} ({v})" for k, v in reasons))
        sk = self.skipped
        if sk:
            parts = [f"{t} {sk[k]}" for k, t in (("bad", "в карантине"), ("volume", "тонкий рынок"),
                                                  ("cool", "пауза после отказа")) if sk.get(k)]
            lines.append("Отсеял до анализа на последнем скане: " + ", ".join(parts))
        bad = self.bad_rows()
        if bad:
            lines.append("\n🚫 <b>Карантин</b> (проблемные монеты — не проверяю и не беру):")
            for coin, x in bad[:6]:
                lines.append(f"• <b>{html.escape(coin)}</b> до {self.when(float(x['until']))}: {html.escape(x.get('why', ''))}")
            if len(bad) > 6:
                lines.append(f"…и ещё {len(bad) - 6}: <code>/spread bad</code>")
            lines.append("Снять: <code>/spread unban LUNC</code> · добавить: <code>/spread ban МОНЕТА</code>")
        res = self.b.scanner.last_result
        near = getattr(res, "near_cross", None) if res else None
        if near:
            nb = self.bad(near.text.split("/")[0])
            lines.append(f"Лучший спред последнего скана: {html.escape(near.text)} — {near.gross_percent:+.2f}% по ценам, "
                         f"{near.net_percent:+.2f}% после торговых комиссий (без комиссии вывода)"
                         + (" — 🚫 в карантине, не беру" if nb else ""))
        lines.append("\n<code>/spread check</code> — проверить сейчас · <code>/spread check SOL</code> — монету · "
                     "<code>/spread min 1.5</code> · <code>/spread max 10</code> · <code>/spread runs 3</code> — сделок "
                     "одновременно · <code>/spread addr</code> — адреса · <code>/spread bad</code> — карантин · "
                     "<code>/spread real</code> / <code>/spread test</code> — реальные сделки или тест")
        return "\n".join(lines)

    def ev_dict(self, ev: SpreadCheck) -> dict:
        return {"coin": ev.coin, "src": self.name(ev.src), "dst": self.name(ev.dst), "gross": round(ev.gross, 3),
                "ok": ev.ok, "reason": REASONS.get(ev.reason, ev.reason) if ev.reason else "",
                "usd": round(ev.usd, 2), "profit": round(ev.profit, 4) if ev.received else None,
                "pct": round(ev.pct, 3) if ev.received else None, "network": ev.network,
                "checks": [{"status": s, "text": x} for s, x in ev.checks]}

    def status_dict(self) -> dict:
        """Для приложения (Mini App): то же, что в /spread."""
        st = self.st
        res = self.b.scanner.last_result
        near = getattr(res, "near_cross", None) if res else None
        runs = [r for r in st["runs"] if r.get("stage") in ("done", "stuck")][-6:][::-1]
        bad = [{"coin": c, "until": float(x["until"]), "why": x.get("why", ""), "auto": bool(x.get("auto", True))}
               for c, x in self.bad_rows()]
        return {"on": bool(st["on"]), "live": self.live, "paper": bool(st["paper"]),
                "max_runs": self.max_runs, "bad": bad[:20], "skipped": dict(self.skipped),
                "stuck": int(st.get("stuck") or 0),
                "trading_live": bool(self.b.trader.state.live), "min_pct": st["min_pct"], "min_usd": st["min_usd"],
                "max_usd": min(float(st["max_usd"]), float(self.b.settings.max_trade_usd)), "max_net_min": MAX_NET_MIN,
                "problems": self.problems(), "active": [re.sub(r"<[^>]+>", "", self.run_line(r)) for r in self.active()],
                "runs": [re.sub(r"<[^>]+>", "", self.run_line(r)) for r in runs],
                "totals": {k: {"n": int(v[0]), "pnl": round(float(v[1]), 4)} for k, v in st["totals"].items()},
                "checks": [{"coin": c["coin"], "src": self.name(c["src"]), "dst": self.name(c["dst"]), "gross": c["gross"],
                            "pct": c.get("pct"), "reason": REASONS.get(c["reason"], c["reason"]) if c["reason"] else ""}
                           for c in st["checks"][-5:][::-1]],
                "near": {"text": near.text, "gross": round(near.gross_percent, 3), "net": round(near.net_percent, 3)}
                if near else None}

    def history_text(self) -> str:
        runs = [r for r in self.st["runs"] if r.get("stage") in ("done", "stuck")][-12:]
        if not runs:
            return "⚖️ Сделок по спреду ещё не было. Включить: /spread on"
        return "⚖️ <b>Сделки по спреду</b>\n" + "\n".join(self.run_line(r) for r in reversed(runs))

    def bad_text(self) -> str:
        rows = self.bad_rows()
        if not rows:
            return ("🚫 Карантин пуст. Монета попадает сюда сама, если: вывод/ввод закрыт или это разные токены 2 раза "
                    "за сутки, спред «висит» больше 12 ч, сделка застряла или вывод не прошёл. Вручную: /spread ban LUNC")
        lines = ["🚫 <b>Карантин спреда</b> — эти монеты бот не проверяет и не берёт:"]
        for coin, x in rows:
            who = "" if x.get("auto", True) else " (вручную/заранее)"
            lines.append(f"• <b>{html.escape(coin)}</b> до {self.when(float(x['until']))}{who}: {html.escape(x.get('why', ''))}")
        lines.append("\nСрок растёт с каждым разом: 1 день → 3 дня → 7 дней. Снять: <code>/spread unban МОНЕТА</code>")
        return "\n".join(lines)

    async def addr_text(self) -> str:
        """Какие адреса добавить в адресные книги, чтобы бот мог перевозить монеты (один раз)."""
        tf = self.b.transfers
        names = [n for n in self.b.scanner.exchanges if n in self.b.trader.creds]
        if len(names) < 2:
            return "Нужны API-ключи обеих бирж: /keys"
        evm = {"BEP20", "ARB", "OP", "BASE", "POLYGON", "AVAXC", "ERC20"}
        lines = ["📒 <b>Адреса для адресных книг</b> — один раз, чтобы бот мог перевозить монеты сам"]
        for src, dst in ((names[0], names[1]), (names[1], names[0])):
            lines.append(f"\n<b>В книгу {html.escape(self.name(src))}</b> (адреса пополнения {html.escape(self.name(dst))}):")
            try:
                cur = await tf.currency(dst, "USDT")
            except Exception as e:
                lines.append(f"не смог получить сети: {html.escape(human_error(e))}")
                continue
            nets = [n for n in sorted(cur.get("nets") or {}, key=lambda n: NET_MIN.get(n, 99))
                    if NET_MIN.get(n, 99) <= MAX_NET_MIN and (cur["nets"][n].get("deposit") is not False)][:6]
            for n in nets:
                try:
                    addr, tag = await tf.deposit_address(dst, "USDT", cur["nets"][n]["key"])
                except Exception as e:
                    lines.append(f"• {html.escape(n)}: {html.escape(human_error(e))[:80]}")
                    continue
                kind = " — EVM: добавь как <b>универсальный адрес</b>, подойдёт всем токенам сети" if n in evm else ""
                lines.append(f"• {html.escape(n)}: <code>{html.escape(addr)}</code>"
                             + (f" мемо <code>{html.escape(str(tag))}</code>" if tag else "") + kind)
        lines.append("\nBybit: Активы → Вывод → Управление адресами. BingX: Активы → Вывод → Адресная книга "
                     "(белый список). Для сетей не-EVM биржи часто требуют запись на каждую монету — когда бот найдёт "
                     "выгодный спред, но адреса не будет в книге, он пришлёт точный адрес и сеть. Перед каждым выводом "
                     "бот всё равно берёт адрес у биржи заново и сверяет.")
        return "\n".join(lines)


def panel_kb(b: ArbitrageBot) -> InlineKeyboardMarkup:
    on = b.spread.st["on"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏸ Выключить" if on else "▶️ Включить", callback_data="sp:off" if on else "sp:on")],
        [InlineKeyboardButton(text="🔍 Проверить сейчас", callback_data="sp:check"),
         InlineKeyboardButton(text="📜 Сделки", callback_data="sp:hist")],
        [InlineKeyboardButton(text="📒 Адреса для книги", callback_data="sp:addr"),
         InlineKeyboardButton(text="🧪 Вернуть тест" if not b.spread.st["paper"] else "💵 Реальные сделки",
                              callback_data="sp:paper" if not b.spread.st["paper"] else "sp:real")],
    ])


LIVE_WARNING = (
    "⚠️ <b>Перевести спред на РЕАЛЬНЫЕ сделки?</b>\n"
    "Бот будет сам покупать монеты на одной бирже, выводить их на твою вторую биржу и продавать там. "
    "Только после полного анализа и только если прибыль после всех комиссий ≥ порога.\n"
    "Риски честно: пока монеты едут (2–15 мин), цена может уйти; биржа может задержать вывод. "
    "Защита: проблемные монеты в карантине, не больше 3 сделок разными монетами одновременно (/spread runs), лимиты "
    "сделки и переводов, стоп −3%, дневной лимит убытка."
)


def real_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, реальные сделки", callback_data="sp:real2"),
        InlineKeyboardButton(text="Отмена", callback_data="sp:cancel")]])


async def check_now(b: ArbitrageBot, coin: str | None = None) -> str:
    evs, empty = await check_evs(b, coin)
    if empty:
        return empty
    sp = b.spread
    mode = "реальный режим" if sp.live else "тест: деньги не двигаются"
    parts = [f"⚖️ <b>Проверка спреда</b> ({mode})"] + [sp.check_text(e) for e in evs]
    if not any(e.ok for e in evs):
        parts.append("Ни одна связка не прошла все проверки — бот ничего бы не купил. Так и задумано: лучше пропустить, "
                     "чем потерять на комиссиях.")
    text = "\n\n".join(parts)
    return text if len(text) <= 4000 else text[:3990] + "…"


async def check_evs(b: ArbitrageBot, coin: str | None = None) -> tuple[list[SpreadCheck], str]:
    """Полный анализ лучших связок сейчас (или одной монеты). Ничего не покупает. (анализы, текст «нечего проверять»)."""
    sp = b.spread
    names = [n for n in b.scanner.exchanges if n in b.trader.creds]
    if len(names) < 2:
        return [], "⚖️ Нужны API-ключи обеих бирж: /keys"
    evs: list[SpreadCheck] = []
    if coin:
        sym = f"{coin}/{b.settings.quote}"
        for src, dst in ((names[0], names[1]), (names[1], names[0])):
            gross = 0.0
            try:
                ta, tb_ = await asyncio.gather(b.scanner.exchanges[src].fetch_ticker(sym),
                                               b.scanner.exchanges[dst].fetch_ticker(sym))
                if ta.get("ask") and tb_.get("bid"):
                    gross = (float(tb_["bid"]) / float(ta["ask"]) - 1) * 100
            except Exception:
                return [], f"⚖️ Пары {html.escape(sym)} нет на одной из бирж"
            evs.append(await sp.evaluate(sym, src, dst, gross))
        evs.sort(key=lambda e: e.gross, reverse=True)
        evs = evs[:1]
    else:
        res = b.scanner.last_result
        if res is None or time.time() - res.finished_at > 90:
            res = await b._scan()
        old = sp.st["cool"]
        sp.st["cool"] = {}  # проверка по кнопке — без пауз после прошлых отказов
        try:
            cands = sp.candidates(res, track=False)[:4]
        finally:
            sp.st["cool"] = old
        for o in cands:
            evs.append(await sp.evaluate(o.symbol, o.buy.exchange, o.sell.exchange, o.gross_percent))
        if not evs:
            near = getattr(res, "near_cross", None)
            return [], ("⚖️ <b>Сейчас выгодных расхождений нет</b>: ни одна общая монета не дешевле на одной бирже настолько, "
                    f"чтобы покрыть торговые комиссии (+{b.settings.min_profit_percent:g}%)."
                    + (f"\nЛучшее: {html.escape(near.text)} — {near.net_percent:+.2f}% после торговых комиссий."
                       if near else "")
                    + "\nЭто нормально: Bybit и BingX — крупные биржи, цены у них почти одинаковые. Бот проверяет каждую "
                      "минуту и возьмёт сделку, когда появится.")
    return evs, ""


def real_problem(b: ArbitrageBot) -> str | None:
    """Почему нельзя перейти на реальные сделки (None — можно)."""
    if not b.trader.state.live:
        return "сначала включи реальный режим торговли: /live (сейчас вся торговля в тесте)"
    if not b.transfers.st["allowed"]:
        return "спреду нужно перевозить монеты: сначала /transfer → «🔓 Разрешить переводы»"
    return None


async def set_paper(b: ArbitrageBot, paper: bool) -> str:
    sp = b.spread
    if not paper:
        why = real_problem(b)
        if why:
            return f"⛔ Пока нельзя: {why}."
    sp.st["paper"] = paper
    b.save_trading()
    await b._backup_now()
    if paper:
        return "🧪 Спред вернулся в тест: сделки на бумаге, деньги не двигаются." + (
            " Открытую реальную сделку бот доведёт до конца." if any(r["mode"] == "live" for r in sp.active()) else "")
    return ("💵 <b>Спред торгует по-настоящему</b>" + (": бот проверяет каждую минуту." if sp.st["on"] else
                                                       ". Он сейчас выключен — нажми «▶️ Включить».")
            + f" Сделка до ${min(float(sp.st['max_usd']), float(b.settings.max_trade_usd)):g}, "
              f"до {sp.max_runs} одновременно.")


async def set_on(b: ArbitrageBot, on: bool) -> str:
    sp = b.spread
    if on and sp.live and not b.transfers.st["allowed"]:
        return "⛔ В реальном режиме спреду нужно перевозить монеты: сначала /transfer → «🔓 Разрешить переводы»."
    sp.st["on"] = on
    b.save_trading()
    await b._backup_now()
    if not on:
        return "⏸ Спред выключен. Открытую сделку бот доведёт до конца." if sp.active() else "⏸ Спред выключен."
    probs = sp.problems()
    return ("▶️ <b>Спред включён</b> (" + ("💵 реальный режим" if sp.live else "🧪 тест на бумаге") + "). "
            "Проверяю каждую минуту, о сделках напишу." + ("\nПока мешает: " + "; ".join(probs) if probs else ""))


def register(b: ArbitrageBot) -> None:
    from .users_ui import is_main_owner

    dp = b.dp

    @dp.message(Command("spread", "спред", "arb", "арбитраж"))
    async def cmd_spread(message: Message) -> None:
        if not b._allowed(message) or message.from_user is None:
            return
        uid = message.from_user.id
        if not b.is_owner(uid):
            await message.answer("⛔ Спред-арбитраж доступен только владельцу бота.")
            return
        sp = b.spread
        args = (message.text or "").split()[1:]
        head = args[0].lower() if args else ""
        if not head:
            await b._answer(message, sp.panel_text(), panel_kb(b))
            return
        if head in ("on", "off", "вкл", "выкл", "start", "stop"):
            on = head in ("on", "вкл", "start")
            if not is_main_owner(b, uid):
                await message.answer("Включает и выключает спред только главный владелец.")
                return
            await b._answer(message, await set_on(b, on))
            return
        if head in ("real", "live", "реально", "реальные", "paper", "test", "тест", "бумага"):
            paper = head in ("paper", "test", "тест", "бумага")
            if not is_main_owner(b, uid):
                await message.answer("Режим спреда меняет только главный владелец.")
                return
            why = None if paper else real_problem(b)
            if why:
                await message.answer(f"⛔ Пока нельзя: {why}.")
                return
            if not paper and sp.st["paper"]:
                await b._answer(message, LIVE_WARNING, real_kb())
                return
            await b._answer(message, await set_paper(b, paper))
            return
        if head in ("check", "проверка", "проверить", "scan"):
            coin = args[1].upper() if len(args) > 1 else None
            if coin and not re.fullmatch(r"[A-Z0-9]{1,15}", coin):
                await message.answer("Напиши так: /spread check SOL")
                return
            await message.answer("🔍 Проверяю спреды: стаканы, вывод/ввод, сети, комиссии… (деньги не двигаются)")
            try:
                text = await check_now(b, coin)
            except Exception as e:
                log.exception("Проверка спреда")
                text = f"❌ Проверка прервалась: {html.escape(human_error(e))}"
            await b._answer(message, text)
            return
        if head in ("min", "max", "мин", "макс"):
            if not is_main_owner(b, uid):
                await message.answer("Условия спреда меняет только главный владелец.")
                return
            try:
                v = float(args[1].replace(",", "."))
                ok = 0.3 <= v <= 20 if head in ("min", "мин") else 1 <= v <= 1000
                if not ok:
                    raise ValueError
            except (IndexError, ValueError):
                await message.answer("Напиши так: /spread min 1.5 (порог прибыли 0.3–20%) или /spread max 10 (сумма $1–1000)")
                return
            key = "min_pct" if head in ("min", "мин") else "max_usd"
            sp.st[key] = v
            b.save_trading()
            await message.answer(f"✅ {'Порог прибыли' if key == 'min_pct' else 'Сумма сделки до'}: "
                                 f"{v:g}{'%' if key == 'min_pct' else '$'}"
                                 + (" — ниже 0.8% комиссии и движение цены часто съедают всё" if key == "min_pct" and v < 0.8
                                    else ""))
            return
        if head in ("bad", "карантин", "ban", "unban", "бан", "разбан", "runs", "сделок"):
            if head in ("bad", "карантин") and len(args) < 2:
                await b._answer(message, sp.bad_text())
                return
            if not is_main_owner(b, uid):
                await message.answer("Карантин и число сделок меняет только главный владелец.")
                return
            if head in ("runs", "сделок"):
                try:
                    v = int(args[1])
                    if not 1 <= v <= MAX_RUNS_CAP:
                        raise ValueError
                except (IndexError, ValueError):
                    await message.answer(f"Напиши так: /spread runs 3 (сколько сделок разными монетами одновременно, "
                                         f"1–{MAX_RUNS_CAP})")
                    return
                sp.st["max_runs"] = v
                b.save_trading()
                await message.answer(f"✅ Спред держит до {v} сделок одновременно (каждая — своя монета). В реальном режиме "
                                     "каждая сделка — в пределах лимитов сделки, переводов и дневного убытка.")
                return
            coin = args[1].upper() if len(args) > 1 else ""
            if not re.fullmatch(r"[A-Z0-9]{1,15}", coin):
                await message.answer("Напиши так: /spread ban LUNC или /spread unban LUNC")
                return
            if head in ("unban", "разбан"):
                ok = sp.unban(coin)
                b.save_trading()
                await message.answer(f"✅ {coin} вышла из карантина: бот снова будет её проверять (все проверки — как обычно)."
                                     if ok else f"{coin} не в карантине.")
                return
            try:
                days = float(args[2].replace(",", ".")) if len(args) > 2 else 30.0
                if not 0.04 <= days <= 365:
                    raise ValueError
            except ValueError:
                await message.answer("Срок — в днях, от 0.04 (1 ч) до 365: /spread ban LUNC 30")
                return
            sp.ban(coin, days * 86400, "добавлено вручную", auto=False)
            b.save_trading()
            await message.answer(f"🚫 {coin} в карантине на {days:g} дн.: спред по ней бот не проверяет и не берёт.")
            return
        if head in ("addr", "адреса", "address", "book"):
            await message.answer("📒 Беру адреса пополнения у бирж…")
            await b._answer(message, await sp.addr_text())
            return
        if head in ("history", "сделки", "log"):
            await b._answer(message, sp.history_text())
            return
        await b._answer(message, sp.panel_text(), panel_kb(b))

    @dp.callback_query(F.data.startswith("sp:"))
    async def cb_spread(cb: CallbackQuery) -> None:
        uid = cb.from_user.id
        if not b.is_owner(uid):
            await cb.answer("Только владелец", show_alert=True)
            return
        sp = b.spread
        action = cb.data.split(":")[1]
        chat = cb.message.chat.id if cb.message else uid
        if action in ("on", "off", "real", "real2", "paper") and not is_main_owner(b, uid):
            await cb.answer("Это меняет только главный владелец", show_alert=True)
            return
        if action in ("on", "off"):
            text = await set_on(b, action == "on")
            await cb.answer("Готово")
            await b._send(chat, text)
            await b._send(chat, sp.panel_text(), panel_kb(b))
            return
        if action == "real":
            why = real_problem(b)
            if why:
                await cb.answer(f"Пока нельзя: {why}"[:190], show_alert=True)
                return
            await cb.answer()
            await b._send(chat, LIVE_WARNING, real_kb())
            return
        if action in ("real2", "paper"):
            text = await set_paper(b, action == "paper")
            await cb.answer("Готово")
            await b._send(chat, text)
            await b._send(chat, sp.panel_text(), panel_kb(b))
            if action == "real2" and not sp.st["paper"]:
                await b.notify("💵 Спред-арбитраж переведён на реальные сделки (/spread)", actor=uid, skip_actor=True,
                               urgent=True)
            return
        if action == "check":
            await cb.answer("Проверяю… деньги не двигаются")
            try:
                text = await check_now(b)
            except Exception as e:
                log.exception("Проверка спреда")
                text = f"❌ Проверка прервалась: {html.escape(human_error(e))}"
            await b._send(chat, text)
            return
        if action == "hist":
            await cb.answer()
            await b._send(chat, sp.history_text())
            return
        if action == "addr":
            await cb.answer("Беру адреса у бирж…")
            await b._send(chat, await sp.addr_text())
            return
        await cb.answer("Отменено" if action == "cancel" else "")
