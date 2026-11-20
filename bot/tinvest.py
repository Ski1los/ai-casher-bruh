"""Т-Инвестиции (Т-Банк): портфель брокерского счёта в режиме «только чтение».

Нужен токен T-Invest API с правом «только чтение» (создаётся в приложении/на сайте Т-Банка:
Инвестиции → Настройки → Токены T-Invest API). Токен кладётся в Render → Environment как TINVEST_TOKEN.
Бот через него только смотрит портфель: заявки не выставляет, деньги не выводит.

API: REST-шлюз https://invest-public-api.tbank.ru/rest/ (методы UsersService/GetAccounts и
OperationsService/GetPortfolio). Сертификат сервера выпущен Russian Trusted Root CA (НУЦ Минцифры),
которого нет в стандартных хранилищах сертификатов. Поэтому корневой сертификат лежит в bot/certs/
и добавляется ТОЛЬКО для запросов к T-Invest (на остальные соединения бота не влияет).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import ssl
import time
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

API = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
SANDBOX_API = "https://sandbox-invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
CA_FILE = Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"
CACHE_SEC = 60
TIMEOUT = aiohttp.ClientTimeout(total=20)

TYPE_TITLES = {"share": "Акции", "bond": "Облигации", "etf": "Фонды", "currency": "Валюта", "futures": "Фьючерсы",
               "option": "Опционы", "sp": "Структурные ноты"}
ACCOUNT_TYPES = {"ACCOUNT_TYPE_TINKOFF": "брокерский", "ACCOUNT_TYPE_TINKOFF_IIS": "ИИС",
                 "ACCOUNT_TYPE_INVEST_BOX": "инвесткопилка", "ACCOUNT_TYPE_INVEST_FUND": "фондовый"}
CURRENCY_SIGNS = {"rub": "₽", "usd": "$", "eur": "€", "cny": "¥"}


class TInvestError(Exception):
    """Понятная ошибка T-Invest API."""


def token_from_env(env=None) -> str:
    env = os.environ if env is None else env
    for key in ("TINVEST_TOKEN", "TINKOFF_TOKEN", "TBANK_TOKEN", "INVEST_TOKEN"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return ""


def ssl_context() -> ssl.SSLContext:
    """Обычные корневые сертификаты + Russian Trusted Root CA (только для этого клиента)."""
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except Exception:  # certifi приходит вместе с ccxt; без него остаются системные сертификаты
        pass
    if CA_FILE.exists():
        ctx.load_verify_locations(cafile=str(CA_FILE))
    return ctx


def money(v) -> float:
    """MoneyValue / Quotation ({"units": "12", "nano": 500000000}) → 12.5."""
    if not isinstance(v, dict):
        return 0.0
    try:
        return int(v.get("units") or 0) + int(v.get("nano") or 0) / 1e9
    except (TypeError, ValueError):
        return 0.0


def currency(v) -> str:
    return str((v or {}).get("currency") or "rub").lower() if isinstance(v, dict) else "rub"


def sign(cur: str) -> str:
    return CURRENCY_SIGNS.get(cur.lower(), cur.upper())


def parse_portfolio(data: dict) -> dict:
    positions = []
    for p in data.get("positions") or []:
        qty = money(p.get("quantity"))
        price = money(p.get("currentPrice"))
        avg = money(p.get("averagePositionPrice"))
        cur = currency(p.get("currentPrice"))
        value = qty * price
        yield_abs = money(p.get("expectedYield"))
        cost = qty * avg
        positions.append({
            "ticker": str(p.get("ticker") or p.get("figi") or "?"),
            "type": str(p.get("instrumentType") or ""),
            "quantity": qty, "price": price, "avg_price": avg, "currency": cur, "value": value,
            "yield": yield_abs, "yield_pct": (yield_abs / cost * 100) if cost else None,
            "daily_yield": money(p.get("dailyYield")) if p.get("dailyYield") else None,
            "blocked": bool(p.get("blocked")),
        })
    positions.sort(key=lambda x: -abs(x["value"]))
    totals = {}
    for key, kind in (("totalAmountShares", "share"), ("totalAmountBonds", "bond"), ("totalAmountEtf", "etf"),
                      ("totalAmountCurrencies", "currency"), ("totalAmountFutures", "futures"),
                      ("totalAmountOptions", "option"), ("totalAmountSp", "sp")):
        v = money(data.get(key))
        if v:
            totals[kind] = v
    total = money(data.get("totalAmountPortfolio")) or sum(totals.values())
    return {
        "total": total, "currency": currency(data.get("totalAmountPortfolio")),
        "yield_pct": money(data.get("expectedYield")),
        "daily_yield": money(data.get("dailyYield")) if data.get("dailyYield") else None,
        "daily_yield_pct": money(data.get("dailyYieldRelative")) if data.get("dailyYieldRelative") else None,
        "by_type": totals, "positions": positions,
    }


class TInvestClient:
    def __init__(self, token: str = "", base: str = API, ssl_ctx: ssl.SSLContext | bool | None = None) -> None:
        self.token = token.strip()
        self.base = base
        self._ssl = ssl_ctx
        self._session: aiohttp.ClientSession | None = None
        self.snapshot: dict | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, env=None) -> TInvestClient:
        env = os.environ if env is None else env
        sandbox = (env.get("TINVEST_SANDBOX") or "").strip().lower() in ("1", "true", "yes")
        return cls(token_from_env(env), SANDBOX_API if sandbox else API)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, method: str, body: dict) -> dict:
        if self._session is None or self._session.closed:
            if self._ssl is None:
                self._ssl = ssl_context()
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl))
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                   "x-app-name": "ai-casher-bruh"}
        try:
            async with self._session.post(self.base + method, json=body, headers=headers, timeout=TIMEOUT) as r:
                data = await r.json(content_type=None)
                status = r.status
        except aiohttp.ClientConnectorCertificateError:
            raise TInvestError("не удалось проверить сертификат Т-Банка (Russian Trusted Root CA)") from None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise TInvestError(f"нет связи с T-Invest API: {type(e).__name__}") from None
        except ValueError:
            raise TInvestError("T-Invest API прислал непонятный ответ") from None
        if status >= 400:
            msg = str((data or {}).get("message") or "") if isinstance(data, dict) else ""
            if status in (401, 403) or "authentication" in msg.lower() or "unauthenticated" in msg.lower():
                raise TInvestError("токен не подходит или отозван: создай новый (только чтение) и замени TINVEST_TOKEN")
            if status == 429:
                raise TInvestError("T-Invest просит подождать (лимит запросов)")
            raise TInvestError(f"T-Invest API: ошибка {status} {msg[:120]}".strip())
        return data if isinstance(data, dict) else {}

    async def accounts(self) -> list[dict]:
        data = await self._post("UsersService/GetAccounts", {})
        out = []
        for a in data.get("accounts") or []:
            status = str(a.get("status") or "")
            if status and status != "ACCOUNT_STATUS_OPEN":
                continue
            out.append({"id": str(a.get("id") or ""), "name": str(a.get("name") or "Счёт"),
                        "type": ACCOUNT_TYPES.get(str(a.get("type") or ""), ""),
                        "access": str(a.get("accessLevel") or "")})
        return [a for a in out if a["id"]]

    async def portfolio(self, account_id: str) -> dict:
        data = await self._post("OperationsService/GetPortfolio", {"accountId": account_id, "currency": "RUB"})
        return parse_portfolio(data)

    async def get(self, force: bool = False) -> dict | None:
        """Снимок всех открытых счетов: {accounts: [...], total, updated_at, error?}."""
        if not self.configured:
            return None
        async with self._lock:
            snap = self.snapshot
            if snap and not force and time.time() - snap["updated_at"] < CACHE_SEC:
                return snap
            try:
                accounts = await self.accounts()
                ports = await asyncio.gather(*(self.portfolio(a["id"]) for a in accounts), return_exceptions=True)
                items = []
                for a, p in zip(accounts, ports):
                    if isinstance(p, BaseException):
                        items.append({**a, "error": str(p) if isinstance(p, TInvestError) else type(p).__name__})
                    else:
                        items.append({**a, **p})
                self.snapshot = {"accounts": items, "updated_at": time.time(),
                                 "total": sum(x.get("total") or 0 for x in items if x.get("currency", "rub") == "rub"),
                                 "read_only": all("READ_ONLY" in (a["access"] or "READ_ONLY") for a in accounts)}
            except TInvestError as e:
                self.snapshot = {"accounts": [], "updated_at": time.time(), "total": 0, "error": str(e)}
            except Exception as e:
                log.exception("T-Invest")
                self.snapshot = {"accounts": [], "updated_at": time.time(), "total": 0,
                                 "error": f"{type(e).__name__}: {str(e)[:120]}"}
            return self.snapshot

    def text(self, snap: dict | None) -> str:
        if not self.configured:
            return TINVEST_HELP
        if not snap:
            return "🏦 Т-Инвестиции: загружаю…"
        if snap.get("error"):
            return f"🏦 <b>Т-Инвестиции</b>\n❌ {html.escape(snap['error'])}\n\nКак подключить заново: /tinvest help"
        lines = ["🏦 <b>Т-Инвестиции</b> (только просмотр)"]
        if not snap.get("read_only", True):
            lines.append("⚠️ У токена есть право торговли. Для бота хватит «только чтение»: создай такой и замени TINVEST_TOKEN")
        if not snap["accounts"]:
            lines.append("Открытых счетов не нашёл.")
        for a in snap["accounts"]:
            title = html.escape(a["name"] + (f" ({a['type']})" if a.get("type") else ""))
            if a.get("error"):
                lines.append(f"\n<b>{title}</b>: ❌ {html.escape(a['error'])}")
                continue
            s = sign(a.get("currency", "rub"))
            day = ""
            if a.get("daily_yield") is not None:
                pct = f" ({a['daily_yield_pct']:+.2f}%)" if a.get("daily_yield_pct") is not None else ""
                day = f" · за день {a['daily_yield']:+,.2f} {s}{pct}".replace(",", " ")
            lines.append(f"\n<b>{title}</b>: {a['total']:,.2f} {s}".replace(",", " ")
                         + f" · доходность {a['yield_pct']:+.2f}%{day}")
            if a.get("by_type"):
                lines.append("  " + " · ".join(f"{TYPE_TITLES.get(k, k)} {v:,.0f} {s}".replace(",", " ")
                                               for k, v in a["by_type"].items()))
            for p in a["positions"][:12]:
                ps = sign(p["currency"])
                y = f" · {p['yield']:+,.2f} {ps}".replace(",", " ") if p["yield"] else ""
                yp = f" ({p['yield_pct']:+.1f}%)" if p.get("yield_pct") is not None and p["yield"] else ""
                lines.append(f"  {html.escape(p['ticker'])}: {p['quantity']:g} шт × {p['price']:,.2f} {ps} "
                             f"= {p['value']:,.0f} {ps}{y}{yp}".replace(",", " "))
            if len(a["positions"]) > 12:
                lines.append(f"  … и ещё {len(a['positions']) - 12}")
        if len(snap["accounts"]) > 1:
            lines.append(f"\n<b>Всего</b> ≈ {snap['total']:,.2f} ₽".replace(",", " "))
        return "\n".join(lines)


TINVEST_HELP = """\
🏦 <b>Т-Инвестиции: портфель в боте (только просмотр)</b>

1. Открой приложение Т-Банк → Инвестиции → ⚙️ Настройки → «Токены T-Invest API»
   (или сайт tbank.ru/invest/settings/api/).
2. «Создать токен» → доступ <b>«Только чтение»</b> → все счета → скопируй токен (он покажется один раз).
3. Render → твой сервис → Environment → Add: <code>TINVEST_TOKEN</code> = токен → Save. Бот перезапустится.
4. Напиши /tinvest: бот покажет счета, бумаги, доходность за день и всё время.

🔒 Токен НЕ присылай в чат, только в Render. «Только чтение» не даёт выставлять заявки и выводить деньги.
Почему без торговли: арбитраж на Мосбирже для маленьких сумм не окупает комиссии, а риск ошибки велик.
Бот здесь как удобная сводка рядом с криптой."""
