"""🔁 Переводы монет между своими биржами (Bybit ⇄ BingX): сначала анализ, потом вывод.

Как это устроено (безопасность прежде всего):
* **Только между твоими биржами.** Адрес получателя бот берёт сам — через API второй биржи (твой адрес пополнения).
  Вписать чужой адрес нельзя ни командой, ни через ИИ, ни в приложении.
* **Выключено, пока главный владелец не разрешит** (/transfer → «🔓 Разрешить», два подтверждения).
* **Лимиты**: не больше $30 за перевод и $100 в сутки (меняется: /transfer limit 50 150), считаются в $.
* **Перед каждым выводом — анализ** (свежие данные бирж, кэш 3 минуты):
  ключи и право вывода → открыт ли вывод монеты на бирже-источнике и ввод на бирже-получателе (ловит блокировки
  вроде Terra, техработы сетей, делистинг) → общие сети, где открыто и то и другое → самая дешёвая сеть →
  минимум вывода, комиссия (не больше 5% суммы) → хватает ли монет → адрес пополнения на второй бирже → лимиты.
  Перед нажатием «Перевести» анализ делается ещё раз: если за минуту что-то закрылось, вывода не будет.
* **Слежение**: бот проверяет, ушёл ли вывод и дошёл ли депозит, а дошедшее сам переводит на торговый счёт
  (Bybit: Funding → Единый, BingX: Fund → Spot), если ключу это разрешено. Пишет в чат на каждом шаге.
* **⚖️ Автобаланс** (по желанию): если автопилоту/автоторговле на одной бирже не хватает USDT, а на другой лишние —
  бот сам переводит недостающее (в пределах лимитов, не чаще раза в 12 ч, комиссия до 3%).

Перед покупкой бот тоже смотрит статус монеты (Trader.coin_wall): автоторговля не покупает монеты, вывод которых
закрыт биржей, а в ручной заявке появляется предупреждение «купить можно, вывести — нет».
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .trading import STABLES, TradeError, human_error

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

CUR_CACHE_SEC = 180
PLAN_TTL_SEC = 300
MAX_FEE_PCT = 5.0        # комиссия больше 5% суммы — не переводим
WARN_FEE_PCT = 2.0
AUTO_FEE_PCT = 3.0
AUTO_EVERY_SEC = 12 * 3600
AUTO_CHECK_SEC = 600
TRACK_SEC = 120
SLOW_SEC = 6 * 3600      # дольше — пишем, что перевод задерживается
KEEP_ITEMS = 25
DEFAULTS = {"allowed": False, "auto": False, "max_usd": 30.0, "day_usd": 100.0, "day": "", "day_sent": 0.0,
            "items": [], "last_auto": 0.0}
LIMIT_MAX = 100_000.0

# одинаковые сети у разных бирж называются по-разному
_NET_ALIASES = {
    "ERC20": "ERC20", "ETH": "ERC20", "ETHEREUM": "ERC20",
    "TRC20": "TRC20", "TRX": "TRC20", "TRON": "TRC20",
    "BEP20": "BEP20", "BSC": "BEP20", "BNB": "BEP20", "BEP20BSC": "BEP20", "BNBSMARTCHAIN": "BEP20",
    "SOL": "SOL", "SOLANA": "SOL", "SPL": "SOL",
    "ARB": "ARB", "ARBI": "ARB", "ARBONE": "ARB", "ARBITRUM": "ARB", "ARBITRUMONE": "ARB", "ARBEVM": "ARB",
    "OP": "OP", "OPTIMISM": "OP", "OPETH": "OP", "OPTIMISMETH": "OP",
    "MATIC": "POLYGON", "POLYGON": "POLYGON", "POL": "POLYGON", "POLYGONPOS": "POLYGON",
    "AVAXC": "AVAXC", "CAVAX": "AVAXC", "AVAXCCHAIN": "AVAXC", "AVALANCHECCHAIN": "AVAXC",
    "TON": "TON", "TONCOIN": "TON", "BASE": "BASE", "BASEEVM": "BASE",
    "APT": "APT", "APTOS": "APT", "SUI": "SUI", "NEAR": "NEAR", "KAVAEVM": "KAVAEVM",
    "BTC": "BTC", "BITCOIN": "BTC", "LTC": "LTC", "DOGE": "DOGE", "XRP": "XRP", "ADA": "ADA", "DOT": "DOT",
    "LUNA": "LUNA", "TERRA": "LUNA", "LUNC": "LUNC", "TERRACLASSIC": "LUNC",
}


def canon_net(name: str | None) -> str:
    key = re.sub(r"[^A-Z0-9]", "", str(name or "").upper())
    return _NET_ALIASES.get(key, key)


def mask(addr: str) -> str:
    return addr if len(addr) <= 12 else f"{addr[:6]}…{addr[-4:]}"


def fmt(x: float) -> str:
    return f"{x:.8f}".rstrip("0").rstrip(".") if x < 1 else f"{x:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


@dataclass
class TransferPlan:
    id: str
    coin: str
    amount: float
    src: str
    dst: str
    checks: list[tuple[str, str]] = field(default_factory=list)  # (ok|warn|bad, текст)
    network: str = ""          # общее имя сети (TRC20, SOL…)
    src_net: str = ""          # как сеть называется у биржи-источника (ключ в ccxt)
    dst_net: str = ""
    fee: float | None = None
    usd: float | None = None
    address: str = ""
    tag: str | None = None
    created_at: float = field(default_factory=time.time)
    source: str = "manual"

    @property
    def ok(self) -> bool:
        return bool(self.network and self.address) and all(s != "bad" for s, _ in self.checks)

    def add(self, status: str, text: str) -> None:
        self.checks.append((status, text))

    def as_dict(self, name) -> dict:
        return {"id": self.id, "coin": self.coin, "amount": self.amount, "src": self.src, "dst": self.dst,
                "src_name": name(self.src), "dst_name": name(self.dst), "network": self.network, "fee": self.fee,
                "usd": None if self.usd is None else round(self.usd, 2), "address": mask(self.address) if self.address else "",
                "ok": self.ok, "checks": [{"status": s, "text": t} for s, t in self.checks]}


class Transfers:
    def __init__(self, b: ArbitrageBot) -> None:
        self.b = b
        self.plans: OrderedDict[str, TransferPlan] = OrderedDict()
        self._cur: dict[tuple[str, str], tuple[float, dict]] = {}
        self._addr: dict[tuple[str, str, str], tuple[str, str | None]] = {}
        self._lock = asyncio.Lock()

    # --- состояние ---------------------------------------------------------------------------------

    @property
    def st(self) -> dict:
        st = self.b.trader.state.transfers
        if not isinstance(st, dict):
            st = self.b.trader.state.transfers = {}
        for k, v in DEFAULTS.items():
            st.setdefault(k, list(v) if isinstance(v, list) else v)
        today = self.b.trader._today()
        if st["day"] != today:
            st["day"], st["day_sent"] = today, 0.0
        return st

    def name(self, ex: str) -> str:
        return self.b.scanner.display_name(ex)

    def other(self, ex: str) -> str | None:
        rest = [n for n in self.b.scanner.exchanges if n != ex]
        return rest[0] if len(rest) == 1 else None

    def can_withdraw_key(self, ex: str) -> bool | None:
        info = self.b.trader.key_info.get(ex) or {}
        if not info or info.get("error"):
            return None
        return bool(info.get("withdraw"))

    # --- данные бирж -----------------------------------------------------------------------------------

    async def currency(self, ex_name: str, coin: str, force: bool = False) -> dict:
        """Свежий статус монеты на бирже: {active, deposit, withdraw, nets: {сеть: {...}}} (кэш 3 мин)."""
        key = (ex_name, coin)
        hit = self._cur.get(key)
        if hit and not force and time.time() - hit[0] < CUR_CACHE_SEC:
            return hit[1]
        ex = self.b.trader._ex(ex_name)
        await self.b.scanner._ensure_markets(ex_name, ex)
        allc = await ex.fetch_currencies()
        raw = (allc or {}).get(coin)
        out: dict = {"found": raw is not None}
        if raw:
            out.update({"active": raw.get("active"), "deposit": raw.get("deposit"), "withdraw": raw.get("withdraw")})
            nets = {}
            for code, n in (raw.get("networks") or {}).items():
                if not isinstance(n, dict):
                    continue
                lim = ((n.get("limits") or {}).get("withdraw") or {})
                nets[canon_net(n.get("network") or code)] = {
                    "key": code, "deposit": n.get("deposit"), "withdraw": n.get("withdraw"),
                    "active": n.get("active"), "fee": n.get("fee"), "min": lim.get("min"),
                }
            out["nets"] = nets
        # обновляем и то, что видит остальной бот (Trader.coin_wall), — только эту монету
        cache = getattr(ex, "currencies", None)
        if isinstance(cache, dict) and raw:
            raw = {k: v for k, v in raw.items() if k != "info"}
            raw["networks"] = {k: {kk: vv for kk, vv in v.items() if kk != "info"}
                               for k, v in (raw.get("networks") or {}).items() if isinstance(v, dict)}
            cache[coin] = raw
        self._cur[key] = (time.time(), out)
        if len(self._cur) > 60:
            self._cur.pop(next(iter(self._cur)))
        return out

    async def price_usd(self, ex_name: str, coin: str) -> float | None:
        if coin in STABLES:
            return 1.0
        try:
            t = await self.b.scanner.exchanges[ex_name].fetch_ticker(f"{coin}/{self.b.settings.quote}")
            p = t.get("last") or t.get("bid")
            return float(p) if p else None
        except Exception as e:
            log.info("Цена %s на %s: %s", coin, ex_name, e)
            return None

    async def free_coin(self, ex_name: str, coin: str) -> float:
        ex = self.b.trader._ex(ex_name)
        bal = await ex.fetch_balance()
        free = float((bal.get("free") or {}).get(coin) or 0)
        if ex_name == "bybit":  # Bybit выводит и с Funding (accountType FUND,UTA)
            try:
                fb = await ex.fetch_balance({"type": "funding"})
                free += float((fb.get("free") or {}).get(coin) or 0)
            except Exception as e:
                log.debug("Funding Bybit: %s", e)
        return free

    async def deposit_address(self, ex_name: str, coin: str, net_key: str) -> tuple[str, str | None]:
        key = (ex_name, coin, net_key)
        if key in self._addr:
            return self._addr[key]
        ex = self.b.trader._ex(ex_name)
        res = await ex.fetch_deposit_address(coin, {"network": net_key})
        if not (res or {}).get("address") and hasattr(ex, "fetch_deposit_addresses_by_network"):
            # BingX отдаёт адреса по всем сетям сразу и ищет нужную по точному имени — ищем сами, по общему имени
            allnets = await ex.fetch_deposit_addresses_by_network(coin)
            res = next((v for k, v in (allnets or {}).items()
                        if isinstance(v, dict) and canon_net(v.get("network") or k) == canon_net(net_key)), None)
        res = res or {}
        addr = str(res.get("address") or "").strip()
        if not addr:
            raise TradeError("биржа не выдала адрес пополнения")
        tag = res.get("tag") or None
        self._addr[key] = (addr, tag)
        return addr, tag

    # --- анализ ----------------------------------------------------------------------------------------------

    def status_text(self, coin: str, ex_name: str, cur: dict) -> str:
        nets = cur.get("nets") or {}
        if not cur.get("found"):
            return f"{self.name(ex_name)}: такой монеты нет"
        parts = [f"{n} {'⬆️' if v.get('withdraw') else '⛔'}вывод {'⬇️' if v.get('deposit') else '⛔'}ввод"
                 for n, v in sorted(nets.items())]
        return f"{self.name(ex_name)}: " + ("; ".join(parts) if parts else "сети не указаны")

    async def analyze(self, coin: str, amount: float | str, src: str, dst: str | None = None,
                      source: str = "manual", force: bool = False) -> TransferPlan:
        coin = coin.upper().strip()
        dst = dst or self.other(src) or ""
        plan = TransferPlan(secrets.token_hex(4), coin, 0.0, src, dst, source=source)
        st = self.st
        t = self.b.trader
        if src == dst or dst not in self.b.scanner.exchanges:
            plan.add("bad", "нужны две разные биржи: откуда и куда")
            return plan
        for ex_name, role in ((src, "откуда"), (dst, "куда")):
            if ex_name not in t.creds:
                plan.add("bad", f"{self.name(ex_name)} ({role}): нет API-ключа — /keys")
            elif t.key_status.get(ex_name, "ok") != "ok":
                plan.add("bad", f"{self.name(ex_name)} ({role}): ключ не работает — {t.key_status[ex_name]}")
        if any(s == "bad" for s, _ in plan.checks):
            return plan
        perm = self.can_withdraw_key(src)
        if perm is False:
            plan.add("bad", f"у ключа {self.name(src)} нет права вывода (Withdraw). Как включить безопасно: /transfer help")
        elif perm is None:
            plan.add("warn", f"права ключа {self.name(src)} неизвестны — проверю при выводе")
        else:
            plan.add("ok", f"ключ {self.name(src)}: вывод разрешён")
        try:
            cs, cd = await asyncio.gather(self.currency(src, coin, force), self.currency(dst, coin, force))
        except Exception as e:
            plan.add("bad", f"биржа не отдала статус монеты: {human_error(e)}")
            return plan
        if not cs.get("found") or not cd.get("found"):
            plan.add("bad", f"{coin} нет на {self.name(src if not cs.get('found') else dst)}")
            return plan
        if cs.get("active") is False or cs.get("withdraw") is False:
            plan.add("bad", f"вывод {coin} с {self.name(src)} сейчас закрыт самой биржей (техработы сети, блокировка "
                            f"монеты или делистинг — как было с Terra). Вывести нельзя, пока биржа не откроет")
        else:
            plan.add("ok", f"вывод {coin} с {self.name(src)} открыт")
        if cd.get("active") is False or cd.get("deposit") is False:
            plan.add("bad", f"ввод {coin} на {self.name(dst)} сейчас закрыт биржей — монеты могут зависнуть")
        else:
            plan.add("ok", f"ввод {coin} на {self.name(dst)} открыт")
        ns, nd = cs.get("nets") or {}, cd.get("nets") or {}
        common = [n for n in ns if n in nd]
        good = [n for n in common if ns[n].get("withdraw") is not False and ns[n].get("active") is not False
                and nd[n].get("deposit") is not False and nd[n].get("active") is not False]
        if not common:
            plan.add("bad", f"у бирж нет общей сети для {coin}: {self.name(src)} — {', '.join(sorted(ns)) or '?'}, "
                            f"{self.name(dst)} — {', '.join(sorted(nd)) or '?'}")
            return plan
        if not good:
            closed = ", ".join(f"{n} ({'вывод закрыт' if ns[n].get('withdraw') is False else 'ввод закрыт'})"
                               for n in common)
            plan.add("bad", f"все общие сети сейчас закрыты: {closed}")
            return plan
        good.sort(key=lambda n: (ns[n].get("fee") is None, float(ns[n].get("fee") or 0)))
        net = good[0]
        plan.network, plan.src_net, plan.dst_net = net, ns[net]["key"], nd[net]["key"]
        plan.fee = None if ns[net].get("fee") is None else float(ns[net]["fee"])
        others = [n for n in good[1:4]]
        plan.add("ok", f"сеть {net}" + (f" (комиссия {fmt(plan.fee)} {coin})" if plan.fee is not None else
                                         " (комиссию биржа не сообщила)") + (f"; запасные: {', '.join(others)}" if others else ""))
        try:
            free = await self.free_coin(src, coin)
        except Exception as e:
            plan.add("bad", f"не смог узнать баланс {coin} на {self.name(src)}: {human_error(e)}")
            return plan
        fee = plan.fee or 0.0
        if isinstance(amount, str):
            amt = max(0.0, free - fee) if amount.lower() in ("all", "всё", "все", "max") else 0.0
        else:
            amt = float(amount)
        plan.amount = float(f"{amt:.8f}")
        if plan.amount <= 0:
            plan.add("bad", f"на {self.name(src)} нет свободных {coin} для вывода" if free <= fee else "сумма должна быть больше 0")
            return plan
        if free + 1e-12 < plan.amount + fee:
            plan.add("bad", f"на {self.name(src)} свободно {fmt(free)} {coin}, а нужно {fmt(plan.amount + fee)} "
                            f"с комиссией (монеты в ордерах или на другом счёте не считаются)")
        else:
            plan.add("ok", f"на {self.name(src)} свободно {fmt(free)} {coin}")
        mn = ns[net].get("min")
        if mn and plan.amount < float(mn):
            plan.add("bad", f"меньше минимума вывода {fmt(float(mn))} {coin} в сети {net}")
        if fee:
            pct = fee / plan.amount * 100
            cap = AUTO_FEE_PCT if source == "auto" else MAX_FEE_PCT
            if pct > cap:
                plan.add("bad", f"комиссия {pct:.1f}% суммы — дороже {cap:g}%, переводить невыгодно")
            elif pct > WARN_FEE_PCT:
                plan.add("warn", f"комиссия {pct:.1f}% суммы — заметно")
        price = await self.price_usd(src, coin)
        plan.usd = None if price is None else plan.amount * price
        if plan.usd is None:
            plan.add("warn", "не знаю цену монеты в $ — лимит посчитать не могу")
            plan.add("bad", "переводить можно монеты, у которых есть пара к USDT (чтобы проверить лимит)")
        else:
            if plan.usd > st["max_usd"] + 1e-9:
                plan.add("bad", f"${plan.usd:.2f} больше лимита ${st['max_usd']:g} за перевод (/transfer limit)")
            elif st["day_sent"] + plan.usd > st["day_usd"] + 1e-9:
                plan.add("bad", f"лимит ${st['day_usd']:g} в сутки: сегодня уже ${st['day_sent']:.2f} (/transfer limit)")
            else:
                plan.add("ok", f"≈${plan.usd:.2f} — в пределах лимитов (${st['max_usd']:g} за раз, ${st['day_usd']:g} в сутки)")
        risk = (self.b.scalper.st.get("risk") or {}).get(coin)
        if risk and float(risk[0]) > time.time():
            plan.add("warn", f"по {coin} есть тревожные новости: {risk[1]}")
        try:
            plan.address, plan.tag = await self.deposit_address(dst, coin, plan.dst_net)
            plan.add("ok", f"адрес пополнения {self.name(dst)} в сети {net}: {mask(plan.address)}"
                           + (" + мемо" if plan.tag else ""))
        except Exception as e:
            plan.add("bad", f"{self.name(dst)} не выдала адрес пополнения {coin} в сети {net}: {human_error(e)}")
        if src == "bybit":
            plan.add("warn", "Bybit выводит через API только на адреса из адресной книги: один раз добавь "
                             + (f"{plan.address}" + (f" (мемо {plan.tag})" if plan.tag else "") if plan.address
                                else "адрес BingX") + f" ({plan.network or 'сеть'}) в Активы → Вывод → Управление адресами")
        if not st["allowed"]:
            plan.add("bad", "переводы ещё не разрешены — /transfer → «🔓 Разрешить переводы»")
        return plan

    def keep(self, plan: TransferPlan) -> None:
        now = time.time()
        for k in [k for k, p in self.plans.items() if now - p.created_at > PLAN_TTL_SEC]:
            self.plans.pop(k, None)
        self.plans[plan.id] = plan
        while len(self.plans) > 20:
            self.plans.popitem(last=False)

    # --- вывод -------------------------------------------------------------------------------------------------

    async def execute(self, plan_id: str, actor: int | None = None) -> dict:
        old = self.plans.pop(plan_id, None)
        if old is None or time.time() - old.created_at > PLAN_TTL_SEC:
            raise TradeError("проверка устарела — запусти /transfer заново")
        async with self._lock:
            plan = await self.analyze(old.coin, old.amount, old.src, old.dst, source=old.source, force=True)
            if not plan.ok:
                bad = "; ".join(t for s, t in plan.checks if s == "bad")
                raise TradeError(f"перед выводом проверил ещё раз — нельзя: {bad}")
            ex = self.b.trader._ex(plan.src)
            params: dict = {"network": plan.src_net}
            if plan.tag:
                params["tag"] = plan.tag
            if plan.src == "bybit":
                params["accountType"] = "FUND,UTA"
            try:
                res = await ex.withdraw(plan.coin, plan.amount, plan.address, params)
            except Exception as e:
                raise TradeError(withdraw_error(plan, e)) from None
            st = self.st
            st["day_sent"] = round(st["day_sent"] + (plan.usd or 0), 4)
            item = {"id": str((res or {}).get("id") or ""), "at": time.time(), "coin": plan.coin, "amount": plan.amount,
                    "src": plan.src, "dst": plan.dst, "network": plan.network, "fee": plan.fee, "usd": plan.usd,
                    "status": "sent", "source": plan.source, "by": actor, "txid": None}
            st["items"].append(item)
            del st["items"][:-KEEP_ITEMS]
            self.b.save_trading()
        return item

    # --- слежение --------------------------------------------------------------------------------------------

    async def track(self, now: float | None = None) -> list[str]:
        """Ушёл ли вывод, дошёл ли депозит; дошедшее — на торговый счёт. Возвращает сообщения для чата."""
        now = time.time() if now is None else now
        msgs: list[str] = []
        for it in self.st["items"]:
            if it["status"] not in ("sent", "out"):
                continue
            title = f"{fmt(it['amount'])} {it['coin']} {self.name(it['src'])} → {self.name(it['dst'])}"
            try:
                if it["status"] == "sent":
                    wd = await self.b.trader._ex(it["src"]).fetch_withdrawals(it["coin"], int(it["at"] * 1000) - 120_000)
                    mine = next((w for w in wd or [] if str(w.get("id")) == it["id"]), None)
                    if mine:
                        it["txid"] = mine.get("txid") or it.get("txid")
                        s = mine.get("status")
                        if s in ("failed", "canceled"):
                            it["status"] = "failed"
                            msgs.append(f"❌ <b>Вывод не прошёл</b>: {html.escape(title)} — биржа отменила "
                                        f"({html.escape(str(s))}). Монеты вернутся на {html.escape(self.name(it['src']))}")
                            continue
                        if s == "ok":
                            it["status"] = "out"
                if it["status"] == "out" or now - it["at"] > 600:
                    dep = await self.b.trader._ex(it["dst"]).fetch_deposits(it["coin"], int(it["at"] * 1000) - 120_000)
                    low = it["amount"] - (it.get("fee") or 0) - 1e-9
                    got = next((d for d in dep or [] if d.get("status") == "ok"
                                and low <= float(d.get("amount") or 0) <= it["amount"] * 1.001 + 1e-9), None)
                    if got:
                        it["status"] = "done"
                        it["got"] = float(got.get("amount") or 0)
                        note = await self.to_trading(it["dst"], it["coin"], it["got"])
                        msgs.append(f"✅ <b>Перевод дошёл</b>: {html.escape(title)} — зачислено {fmt(it['got'])} "
                                    f"{it['coin']} за {max(1, round((now - it['at']) / 60))} мин.{note}")
                        continue
                if now - it["at"] > SLOW_SEC and not it.get("slow_told"):
                    it["slow_told"] = True
                    msgs.append(f"⏳ <b>Перевод идёт дольше 6 часов</b>: {html.escape(title)}. Проверь историю выводов "
                                f"на {html.escape(self.name(it['src']))}" + (f" (txid {html.escape(str(it['txid'])[:20])}…)"
                                                                            if it.get("txid") else ""))
            except Exception as e:
                log.info("Слежение за переводом %s: %s", title, e)
        if msgs:
            self.b.save_trading()
        return msgs

    async def to_trading(self, ex_name: str, coin: str, amount: float) -> str:
        """Депозит часто падает на Funding/Fund — переносим на торговый счёт, чтобы бот мог им торговать."""
        to = {"bybit": "unified", "bingx": "spot"}.get(ex_name)
        if not to:
            return ""
        ex = self.b.trader._ex(ex_name)
        try:
            fb = await ex.fetch_balance({"type": "funding"})
            on_fund = float((fb.get("free") or {}).get(coin) or 0)
        except Exception as e:
            log.info("Funding %s: %s", ex_name, e)
            return ""
        move = min(on_fund, amount)
        if move <= 0:
            return ""
        try:
            await ex.transfer(coin, move, "funding", to)
            return f"\nПеренёс {fmt(move)} {coin} с Funding на торговый счёт — можно торговать."
        except Exception as e:
            return (f"\n⚠️ Монеты на Funding: перенести на торговый счёт не смог ({html.escape(human_error(e))[:120]}). "
                    "Сделай вручную: Активы → Перевод, или дай ключу право «Transfer».")

    # --- ⚖️ автобаланс --------------------------------------------------------------------------------------

    async def auto_step(self, now: float | None = None) -> TransferPlan | None:
        """Нехватка USDT для автоторговли на одной бирже и лишние на другой → перевод (если разрешено)."""
        now = time.time() if now is None else now
        st, t, b = self.st, self.b.trader, self.b
        if not (st["allowed"] and st["auto"]) or now - st["last_auto"] < AUTO_EVERY_SEC:
            return None
        if not (b.scalper.on or t.state.auto) or len(t.working) < 2:
            return None
        if any(it["status"] in ("sent", "out") for it in st["items"]):
            return None  # прошлый перевод ещё в пути — сначала дождёмся его
        quote = b.settings.quote
        need = max(10.0, float(b.settings.max_trade_usd))
        free = {}
        for n in t.working[:2]:
            try:
                free[n] = await t.free(n, quote)
            except Exception as e:
                log.info("Автобаланс: баланс %s: %s", n, e)
                return None
        poor = min(free, key=free.get)
        rich = max(free, key=free.get)
        if poor == rich or free[poor] >= need * 0.5 or free[rich] < need * 1.5:
            return None
        amount = round(min(need - free[poor], free[rich] - need, st["max_usd"]), 2)
        if amount < 5:
            return None
        st["last_auto"] = now  # даже если не вышло — не долбим биржи каждые 10 минут
        plan = await self.analyze(quote, amount, rich, poor, source="auto")
        if not plan.ok:
            log.info("Автобаланс: нельзя — %s", "; ".join(x for s, x in plan.checks if s == "bad"))
            return plan
        self.keep(plan)
        await self.execute(plan.id)
        return plan

    # --- текст ----------------------------------------------------------------------------------------------

    def plan_text(self, plan: TransferPlan) -> str:
        icon = {"ok": "✅", "warn": "⚠️", "bad": "⛔"}
        head = (f"🔁 <b>Перевод {fmt(plan.amount)} {html.escape(plan.coin)}</b> "
                f"{html.escape(self.name(plan.src))} → {html.escape(self.name(plan.dst))}") if plan.amount else \
            f"🔁 <b>Перевод {html.escape(plan.coin)}</b> {html.escape(self.name(plan.src))} → {html.escape(self.name(plan.dst))}"
        lines = [head, "<b>Анализ перед выводом:</b>"]
        for s, t in plan.checks:
            t = html.escape(t)
            if plan.address and plan.address in t:  # полный адрес — нажатием копируется
                t = t.replace(html.escape(plan.address), f"<code>{html.escape(plan.address)}</code>")
            lines.append(f"{icon[s]} {t}")
        if plan.ok:
            got = plan.amount - (plan.fee or 0)
            lines.append(f"\nПридёт ≈ <b>{fmt(got)}–{fmt(plan.amount)} {html.escape(plan.coin)}</b> "
                         f"(биржи по-разному берут комиссию). Обычно 2–30 минут. Проверка действует 5 минут.")
        else:
            lines.append("\n⛔ <b>Переводить нельзя</b> — причины выше.")
        return "\n".join(lines)

    def panel_text(self) -> str:
        st, t = self.st, self.b.trader
        lines = ["🔁 <b>Переводы между биржами</b>",
                 "Бот сам выводит монеты с одной твоей биржи на другую — только на твой адрес пополнения, который "
                 "он берёт через API. Перед каждым выводом — анализ: открыт ли вывод/ввод, сеть, комиссия, лимиты.",
                 "",
                 f"Разрешено: {'🔓 да' if st['allowed'] else '🔒 нет'} · ⚖️ автобаланс: {'вкл' if st['auto'] else 'выкл'}",
                 f"Лимиты: ${st['max_usd']:g} за перевод, ${st['day_usd']:g} в сутки (сегодня ${st['day_sent']:.2f})"]
        for n in t.creds:
            perm = self.can_withdraw_key(n)
            lines.append(f"• ключ {html.escape(self.name(n))}: вывод "
                         + ("✅ разрешён" if perm else "⛔ не разрешён — /transfer help" if perm is False else "❔ не проверен"))
        items = self.st["items"][-5:][::-1]
        if items:
            lines.append("\n<b>Последние переводы:</b>")
            mark = {"sent": "⏳", "out": "🚚", "done": "✅", "failed": "❌"}
            for it in items:
                lines.append(f"{mark.get(it['status'], '•')} {fmt(it['amount'])} {html.escape(it['coin'])} "
                             f"{html.escape(self.name(it['src']))} → {html.escape(self.name(it['dst']))} · {it['network']}"
                             + (" · ⚖️ авто" if it.get("source") == "auto" else ""))
        lines += ["", "<b>Как пользоваться:</b>",
                  "<code>/transfer USDT 20 bybit bingx</code> — проверить и перевести",
                  "<code>/transfer USDT all bingx bybit</code> — всё, что свободно",
                  "<code>/transfer check LUNA</code> — открыт ли вывод/ввод монеты на обеих биржах",
                  "<code>/transfer limit 30 100</code> — лимиты за раз и в сутки",
                  "<code>/transfer ip</code> — IP сервера для белого списка ключа",
                  "<code>/transfer help</code> — как дать ключу право вывода безопасно"]
        return "\n".join(lines)


HELP_TEXT = (
    "🔑 <b>Как дать ключу право вывода — безопасно</b>\n\n"
    "<b>1. IP сервера.</b> Биржи дают вывод только ключам с привязкой к IP. Render → сервис бота → кнопка "
    "<b>Connect</b> → вкладка <b>Outbound</b>: там адреса, с которых бот ходит в интернет. Впиши их все "
    "через запятую. Какой адрес сейчас — покажет <code>/transfer ip</code>.\n\n"
    "<b>2. Bybit</b>: API → ключ → Edit → галочки <b>Withdraw</b> и <b>Transfer</b> (раздел Assets/Wallet), "
    "IP из шага 1. Потом Активы → Вывод → <b>Управление адресами</b> → добавь адрес пополнения BingX "
    "(бот покажет его в проверке) и включи «только адреса из списка».\n\n"
    "<b>3. BingX</b>: API management → ключ → <b>Withdraw</b> и <b>Transfer</b>, белый список IP из шага 1. "
    "В адресах вывода добавь адрес пополнения Bybit.\n\n"
    "<b>Честно о защите.</b> Бесплатные IP Render общие для сервисов в регионе, так что привязка к IP — "
    "защита средняя. Главная — <b>адресная книга</b>: даже с украденным ключом вывести можно только на твою "
    "вторую биржу. Поэтому не пропускай шаги с адресами. Render менять не надо — новые права бот увидит "
    "при проверке ключей (раз в час или /doctor)."
)


async def outbound_ip() -> str | None:
    """С какого IP бот ходит в интернет (для белого списка ключа). None — не удалось узнать."""
    import aiohttp

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s, s.get(url) as r:
                ip = (await r.text()).strip()
                if re.fullmatch(r"[0-9a-fA-F.:]{7,45}", ip):
                    return ip
        except Exception as e:
            log.info("IP через %s: %s", url, e)
    return None

ALLOW_WARNING = (
    "⚠️ <b>Разрешить боту выводить монеты?</b>\n\n"
    "• Только между твоими биржами: адрес получателя бот берёт через API второй биржи, чужой вписать нельзя.\n"
    "• Перед каждым выводом — анализ; если вывод или ввод монеты закрыт — перевода не будет.\n"
    "• Лимиты: ${max_usd:g} за перевод, ${day_usd:g} в сутки.\n"
    "• Ошибочная сеть или мемо могут потерять монеты — бот выбирает сеть только из тех, что открыты на ОБЕИХ биржах, "
    "и сам подставляет мемо, но гарантию даёт биржа, а не бот.\n\n"
    "Запретить можно в любой момент: /transfer → «🔒 Запретить», или /stop."
)


def withdraw_error(plan: TransferPlan, e: BaseException) -> str:
    text = str(e).lower()
    base = human_error(e)
    if "whitelist" in text or "address book" in text or "131002" in text or "not in" in text and "address" in text:
        return (f"биржа не пускает вывод на этот адрес: добавь {mask(plan.address)} в адресную книгу "
                f"{plan.src} (Активы → Вывод → Управление адресами). ({base})")
    if "permission" in text or "10005" in text or "not allowed" in text:
        return f"у ключа нет права вывода или не привязан IP — /transfer help. ({base})"
    if "insufficient" in text or "balance" in text:
        return f"не хватает монет с учётом комиссии. ({base})"
    if "suspend" in text or "disabled" in text or "maintenance" in text:
        return f"биржа закрыла вывод этой монеты/сети прямо сейчас. ({base})"
    return base


# --- парсинг команды ----------------------------------------------------------------------------------------

_AMOUNT = re.compile(r"^\d+(?:[.,]\d+)?$")


def parse_args(b: ArbitrageBot, args: list[str]) -> tuple[str, float | str, str, str | None]:
    """«USDT 20 bybit bingx», «20 usdt с bybit на bingx», «usdt all bingx» → (монета, сумма, откуда, куда)."""
    coin, amount, exs = None, None, []
    for a in args:
        low = a.lower().strip(",.")
        if low in ("с", "на", "from", "to", "->", "→"):
            continue
        if _AMOUNT.match(low):
            amount = float(low.replace(",", "."))
            continue
        if low in ("all", "всё", "все", "max"):
            amount = "all"
            continue
        try:
            exs.append(b.trader.resolve_exchange(low))
            continue
        except TradeError:
            pass
        coin = a.upper()
    if not coin or amount is None or not exs:
        raise TradeError("напиши так: /transfer USDT 20 bybit bingx (монета, сумма, откуда, куда)")
    return coin, amount, exs[0], exs[1] if len(exs) > 1 else None


def panel_kb(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.transfers.st
    rows = [[InlineKeyboardButton(text="🔒 Запретить переводы" if st["allowed"] else "🔓 Разрешить переводы",
                                  callback_data="xf:deny" if st["allowed"] else "xf:allow")],
            [InlineKeyboardButton(text=f"⚖️ Автобаланс: {'выключить' if st['auto'] else 'включить'}",
                                  callback_data="xf:auto")],
            [InlineKeyboardButton(text="🔑 Как дать ключу право вывода", callback_data="xf:help")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_kb(plan: TransferPlan) -> InlineKeyboardMarkup | None:
    if not plan.ok:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Перевести", callback_data=f"xf:go:{plan.id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"xf:no:{plan.id}")]])


async def transfers_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(90)
    last_auto = 0.0
    while True:
        try:
            if b.transfers.st["items"]:
                for text in await b.transfers.track():
                    await b.notify(text, urgent=True)
            now = time.time()
            if now - last_auto >= AUTO_CHECK_SEC:
                last_auto = now
                plan = await b.transfers.auto_step(now)
                if plan is not None and plan.ok:
                    await b.notify("⚖️ <b>Автобаланс</b>: " + b.transfers.plan_text(plan).split("\n", 1)[0]
                                   + f"\nНа {html.escape(b.transfers.name(plan.dst))} не хватало USDT для автоторговли. "
                                     "Сообщу, когда дойдёт. Выключить: /transfer", urgent=True)
        except Exception:
            log.exception("Переводы: цикл")
        b._beats["transfers"] = time.time()
        await asyncio.sleep(TRACK_SEC)


def register(b: ArbitrageBot) -> None:
    from .users_ui import is_main_owner

    dp = b.dp
    tf = b.transfers

    @dp.message(Command("transfer", "перевод", "перевести", "transfers"))
    async def cmd_transfer(message: Message) -> None:
        if not b._allowed(message) or message.from_user is None:
            return
        uid = message.from_user.id
        if not b.is_owner(uid):
            await message.answer("⛔ Переводы доступны только владельцу бота.")
            return
        args = (message.text or "").split()[1:]
        if not args:
            await b._answer(message, tf.panel_text(), panel_kb(b))
            return
        head = args[0].lower()
        if head in ("help", "помощь"):
            await b._answer(message, HELP_TEXT)
            return
        if head in ("ip", "айпи"):
            ip = await outbound_ip()
            await message.answer(
                (f"🌐 Сейчас бот выходит в интернет с IP <code>{html.escape(ip)}</code>.\n" if ip else
                 "🌐 Не смог узнать IP (сайты проверки не ответили).\n")
                + "Render может использовать несколько адресов: полный список — Render → сервис → Connect → Outbound. "
                  "Впиши все в белый список ключа.")
            return
        if head in ("limit", "лимит", "limits"):
            if not is_main_owner(b, uid):
                await message.answer("Лимиты переводов меняет только главный владелец.")
                return
            try:
                mx = float(args[1].replace(",", "."))
                day = float(args[2].replace(",", ".")) if len(args) > 2 else max(mx, tf.st["day_usd"])
                if not (1 <= mx <= LIMIT_MAX and mx <= day <= LIMIT_MAX):
                    raise ValueError
            except (IndexError, ValueError):
                await message.answer("Напиши так: /transfer limit 30 100 (за раз и в сутки, в $)")
                return
            tf.st["max_usd"], tf.st["day_usd"] = mx, day
            b.save_trading()
            await b._backup_now()
            await message.answer(f"✅ Лимиты переводов: ${mx:g} за раз, ${day:g} в сутки.")
            return
        if head in ("check", "проверка", "статус"):
            if len(args) < 2:
                await message.answer("Напиши так: /transfer check USDT")
                return
            coin = args[1].upper()
            await message.answer(f"🔍 Смотрю статус {html.escape(coin)} на биржах…")
            lines = [f"🔍 <b>{html.escape(coin)}: ввод и вывод на биржах</b>"]
            for n in b.trader.creds:
                try:
                    cur = await tf.currency(n, coin, force=True)
                    glob = ("⛔ вывод закрыт биржей" if cur.get("withdraw") is False else "⬆️ вывод открыт") + " · " + \
                           ("⛔ ввод закрыт" if cur.get("deposit") is False else "⬇️ ввод открыт")
                    lines.append(f"\n<b>{html.escape(tf.name(n))}</b>: {glob if cur.get('found') else 'монеты нет'}")
                    for net, v in sorted((cur.get("nets") or {}).items()):
                        lines.append(f"• {html.escape(net)}: вывод {'✅' if v.get('withdraw') else '⛔'} · ввод "
                                     f"{'✅' if v.get('deposit') else '⛔'}"
                                     + (f" · комиссия {fmt(float(v['fee']))}" if v.get("fee") is not None else ""))
                except Exception as e:
                    lines.append(f"\n<b>{html.escape(tf.name(n))}</b>: не смог узнать — {html.escape(human_error(e))}")
            if not b.trader.creds:
                lines.append("Нужен API-ключ хотя бы одной биржи: /keys")
            await b._answer(message, "\n".join(lines))
            return
        try:
            coin, amount, src, dst = parse_args(b, args)
        except TradeError as e:
            await message.answer(f"❌ {html.escape(str(e))}")
            return
        await message.answer("🔍 Анализирую: статус монеты, сети, комиссии, баланс, адрес…")
        plan = await tf.analyze(coin, amount, src, dst)
        if plan.ok:
            tf.keep(plan)
        await b._answer(message, tf.plan_text(plan), plan_kb(plan))

    @dp.callback_query(F.data.startswith("xf:"))
    async def cb_transfer(cb: CallbackQuery) -> None:
        uid = cb.from_user.id
        if not b.is_owner(uid):
            await cb.answer("Только владелец", show_alert=True)
            return
        parts = cb.data.split(":")
        action = parts[1]
        chat = cb.message.chat.id if cb.message else uid
        if action in ("allow", "allow2", "deny") and not is_main_owner(b, uid):
            await cb.answer("Разрешать переводы может только главный владелец", show_alert=True)
            return
        if action == "allow":
            await cb.answer()
            st = tf.st
            await b._send(chat, ALLOW_WARNING.format(max_usd=st["max_usd"], day_usd=st["day_usd"]),
                          InlineKeyboardMarkup(inline_keyboard=[[
                              InlineKeyboardButton(text="✅ Да, разрешаю", callback_data="xf:allow2"),
                              InlineKeyboardButton(text="Отмена", callback_data="xf:cancel")]]))
            return
        if action in ("allow2", "deny"):
            tf.st["allowed"] = action == "allow2"
            if action == "deny":
                tf.st["auto"] = False
            b.save_trading()
            await b._backup_now()
            await cb.answer("Переводы разрешены" if tf.st["allowed"] else "Переводы запрещены")
            await b._send(chat, tf.panel_text(), panel_kb(b))
            await b.notify(("🔓 Переводы между биржами разрешены" if tf.st["allowed"] else
                            "🔒 Переводы между биржами запрещены") + " (/transfer)", actor=uid, skip_actor=True,
                           urgent=True)
            return
        if action == "auto":
            if not tf.st["allowed"] and not tf.st["auto"]:
                await cb.answer("Сначала разрешите переводы", show_alert=True)
                return
            tf.st["auto"] = not tf.st["auto"]
            b.save_trading()
            await cb.answer("⚖️ Автобаланс включён" if tf.st["auto"] else "Автобаланс выключен")
            await b._send(chat, tf.panel_text(), panel_kb(b))
            return
        if action == "help":
            await cb.answer()
            await b._send(chat, HELP_TEXT)
            return
        if action in ("cancel", "no"):
            if len(parts) > 2:
                tf.plans.pop(parts[2], None)
            await cb.answer("Отменено")
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            return
        if action == "go" and len(parts) > 2:
            await cb.answer("Проверяю ещё раз и вывожу…")
            if cb.message:
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            try:
                it = await tf.execute(parts[2], actor=uid)
            except TradeError as e:
                await b._send(chat, f"❌ Не перевёл: {html.escape(str(e))}")
                return
            text = (f"🚀 <b>Вывод отправлен</b>: {fmt(it['amount'])} {html.escape(it['coin'])} "
                    f"{html.escape(tf.name(it['src']))} → {html.escape(tf.name(it['dst']))}, сеть {html.escape(it['network'])}. "
                    "Сообщу, когда дойдёт (обычно 2–30 минут).")
            await b._send(chat, text)
            await b.notify(text, actor=uid, skip_actor=True, urgent=True)
            await b._backup_now()
            return
        await cb.answer()
