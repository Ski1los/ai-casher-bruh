"""Реальная торговля через API бирж: ручные ордера, исполнение треугольников, автоторговля.

Безопасность прежде всего:
  * ключи берутся ТОЛЬКО из переменных окружения (Render → Environment), в чат и в код не попадают;
  * ключу нужны только права на спотовую торговлю, БЕЗ права на вывод: даже если ключ утечёт,
    деньги нельзя вывести с биржи;
  * по умолчанию включён ТЕСТОВЫЙ режим: всё считается по реальным стаканам и реальному балансу,
    но ордера на биржу не отправляются. Реальный режим включается отдельно кнопкой;
  * лимиты: максимум на сделку, максимум сделок в сутки, лимит дневного убытка автоторговли;
  * перед каждым ордером цена перепроверяется по свежему стакану;
  * /stop: выключает автоторговлю и реальный режим и отменяет открытые ордера.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field

import ccxt.async_support as ccxt

from .config import Settings
from .sim import simulate_buy, simulate_sell
from .triangular import TriOpportunity, path_min_amount, path_min_order_ok, simulate_path

log = logging.getLogger(__name__)

KEY_PAGES = {
    "bybit": "https://www.bybit.com/app/user/api-management",
    "bingx": "https://bingx.com/en/account/api",
}
STABLES = {"USDT", "USDC", "FDUSD", "DAI", "TUSD", "USDD", "PYUSD"}
TRADE_LOG_KEEP = 200
PENDING_KEEP = 30
PENDING_TTL_SEC = 300  # предпросмотр ордера действует 5 минут
TRI_REPEAT_SEC = 600  # один и тот же цикл автоторговля повторяет не чаще раза в 10 минут
AUTO_TRY_MAX = 3  # сколько лучших циклов за один скан проверять свежими стаканами
AUTO_SKIP_REPEAT_SEC = 90  # пропущенный цикл (ушла цена, мало денег) снова пробуем через 1,5 минуты
AUTO_REASONS_KEEP = 6
AUTO_MIN_FREE_USD = 5.0  # меньше этого на торговом счёте циклы почти всегда упираются в минимальный ордер
FREE_CACHE_SEC = 300  # как часто автоторговля перечитывает свободный баланс
FUNDING_CACHE_SEC = 600  # как часто проверять, не лежат ли деньги на счёте Funding
ACCOUNTS_CACHE_SEC = 60  # обзор всех счетов биржи (где лежат деньги)

# BingX: названия счетов из /openApi/account/v1/allAccountBalance — так, как они называются в приложении
BINGX_ACCOUNTS = {
    "sopt": "Спот/Фонд", "spot": "Спот", "fund": "Фонд (Fund)", "funding": "Фонд (Fund)",
    "stdFutures": "Стандартные фьючерсы", "USDTMPerp": "Бессрочные фьючерсы USDT-M",
    "coinMPerp": "Бессрочные фьючерсы Coin-M", "copyTrading": "Копитрейдинг", "grid": "Торговые боты (Grid)",
    "eran": "Накопления (Wealth)", "earn": "Накопления (Wealth)", "c2c": "P2P (C2C)",
}
HISTORY_EVERY_SEC = 15 * 60  # как часто записывать стоимость портфеля для графика
HISTORY_KEEP = 1000  # ≈ 10 дней по 15 минут


class TradeError(Exception):
    """Понятная пользователю ошибка торговли."""


def human_error(e: BaseException) -> str:
    """ccxt-исключение → объяснение по-русски."""
    text = str(e)[:200]
    if isinstance(e, TradeError):
        return str(e)
    # порядок важен: подклассы раньше родителей
    if isinstance(e, ccxt.InsufficientFunds):
        return "недостаточно средств на бирже"
    if isinstance(e, ccxt.PermissionDenied):
        return "у ключа нет нужных прав (включи «Спотовая торговля» / Spot Trade) или IP сервера не в белом списке"
    if isinstance(e, ccxt.AccountSuspended):
        return "аккаунт на бирже ограничен"
    if isinstance(e, ccxt.AuthenticationError):
        return "ключ не подходит: проверь API key, secret и passphrase (и что ключ не удалён)"
    if isinstance(e, ccxt.OrderNotFound):
        return "ордер не найден (уже исполнен или отменён)"
    if isinstance(e, ccxt.InvalidOrder):
        return f"биржа отклонила ордер: {text}"
    if isinstance(e, ccxt.RateLimitExceeded):
        return "слишком много запросов, биржа просит подождать"
    if isinstance(e, ccxt.InvalidNonce):
        return "время запроса не совпало с биржей, попробуй ещё раз"
    if is_geo_block(e):
        return ("биржа не пускает сервер из его страны (ошибка 403 «block access from your country»). "
                "Проверь /doctor: там подсказка, как это обойти")
    if isinstance(e, (ccxt.NetworkError, asyncio.TimeoutError)):
        return f"биржа не отвечает или не пускает сервер: {text}"
    return f"{type(e).__name__}: {text}"


def is_geo_block(e: BaseException) -> bool:
    """Биржа (через CloudFront) блокирует страну сервера, а не ключ и не запрос."""
    text = str(e).lower()
    return "block access from your country" in text or ("cloudfront" in text and "403" in text)


KEY_INFO_EXCHANGES = ("bybit", "bingx")
KEY_WARN_DAYS = 14  # за сколько дней предупреждать, что ключ скоро истечёт


def parse_bybit_key(r: dict, now: float | None = None) -> dict:
    """Ответ Bybit /v5/user/query-api → права и срок действия ключа."""
    now = time.time() if now is None else now
    perms = r.get("permissions") or {}
    ips = [ip for ip in (r.get("ips") or []) if ip]
    out = {
        "withdraw": "Withdraw" in (perms.get("Wallet") or []),
        "spot_trade": "SpotTrade" in (perms.get("Spot") or []),
        "read_only": str(r.get("readOnly")) == "1",
        "ip_bound": bool(ips) and ips != ["*"],
        "unified": str(r.get("uta") or r.get("unified") or "0") not in ("0", "False", "false"),
    }
    expired = str(r.get("expiredAt") or "").strip()
    if expired:
        try:
            out["expires_at"] = dt.datetime.fromisoformat(expired.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    if "expires_at" not in out:
        try:
            days = float(r.get("deadlineDay"))
            if days >= 0:
                out["expires_at"] = now + days * 86400
        except (TypeError, ValueError):
            pass
    return out


BINGX_PERMS = {1: "spot_trade", 2: "read", 3: "futures", 4: "transfer", 5: "withdraw", 7: "sub_transfer"}


def parse_bingx_key(data) -> dict:
    """Ответ BingX /openApi/v1/account/apiPermissions → права ключа.

    Формат: {"apiKey", "permissions": [1, 2, ...], "ipAddresses": [...], "note"}, где
    1 — спот, 2 — чтение, 3 — фьючерсы, 4 — переводы, 5 — ВЫВОД, 7 — переводы субаккаунтов.
    """
    data = data if isinstance(data, dict) else {}
    out: dict = {}
    for p in data.get("permissions") or []:
        try:
            name = BINGX_PERMS.get(int(p))
        except (TypeError, ValueError):
            continue
        if name:
            out[name] = True
    ips = [ip for ip in (data.get("ipAddresses") or []) if ip]
    out["ip_bound"] = bool(ips)
    out["inactive_rule"] = not ips  # ключ без IP BingX удаляет после 14 дней без запросов
    return out


def new_auto_stats() -> dict:
    return {"since": time.time(), "checks": 0, "candidates": 0, "attempts": 0, "executed": 0, "pnl": 0.0,
            "best": None, "reasons": {}}


def key_trade_problem(name: str, info: dict | None) -> str | None:
    """Почему ключ не может торговать (по правам, которые сообщила биржа). None — всё в порядке или неизвестно."""
    if not info or info.get("error"):
        return None
    if name == "bybit":
        if info.get("read_only"):
            return ("ключ «только чтение» (Read-Only): баланс виден, ордера биржа отклонит. "
                    "bybit.com → API → Edit → Read-Write + Spot Trade (без Withdraw)")
        if "spot_trade" in info and not info["spot_trade"]:
            return "у ключа нет права Spot Trade: включи «Спот» в настройках ключа на bybit.com"
    if name == "bingx" and ("spot_trade" in info or "read" in info) and not info.get("spot_trade"):
        return "у ключа нет права «Спотовая торговля»: включи его в настройках API на bingx.com"
    return None


def days_left(info: dict, now: float | None = None) -> float | None:
    exp = info.get("expires_at")
    if not exp:
        return None
    return (exp - (time.time() if now is None else now)) / 86400


def credentials_from_env(names, env=None) -> dict[str, dict]:
    """Ключи вида BYBIT_API_KEY / BYBIT_API_SECRET."""
    env = os.environ if env is None else env
    out: dict[str, dict] = {}
    for name in names:
        up = name.upper()
        key = (env.get(f"{up}_API_KEY") or "").strip()
        secret = (env.get(f"{up}_API_SECRET") or "").strip()
        if not key or not secret:
            continue
        out[name] = {"apiKey": key, "secret": secret}
    return out


def parse_amount(spec: str, available: float | None) -> float:
    """«10», «0,5», «all»/«всё», «50%» → число."""
    s = str(spec).strip().lower().replace(",", ".").lstrip("$")
    if s in ("all", "max", "всё", "все", "весь", "вся"):
        if available is None:
            raise TradeError("не знаю баланс, укажи количество числом")
        return available
    if s.endswith("%"):
        pct = float(s[:-1])
        if not 0 < pct <= 100:
            raise TradeError("процент от 0 до 100")
        if available is None:
            raise TradeError("не знаю баланс, укажи количество числом")
        return available * pct / 100
    try:
        value = float(s)
    except ValueError:
        raise TradeError(f"«{spec}» не похоже на число") from None
    if value <= 0:
        raise TradeError("количество должно быть больше нуля")
    return value


def fmt_amount(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1000:
        return f"{x:,.2f}".replace(",", " ")
    if abs(x) >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    return f"{x:.8g}"


# ---------------------------------------------------------------------------
# Структуры
# ---------------------------------------------------------------------------


@dataclass
class OrderPlan:
    """Предпросмотр ордера: что будет, если нажать «Подтвердить»."""

    exchange: str
    symbol: str
    side: str  # buy / sell
    type: str  # market / limit
    spent: float  # сколько отдаём
    spent_ccy: str
    est_got: float  # сколько получим после комиссии (оценка по стакану)
    got_ccy: str
    est_price: float  # средняя цена исполнения
    fee_rate: float
    usd_value: float
    price: float | None = None  # цена лимитного ордера
    source: str = "manual"  # manual / ai / auto
    warnings: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    created_at: float = field(default_factory=time.time)

    @property
    def base(self) -> str:
        return self.symbol.split("/")[0]


@dataclass
class TradeRecord:
    at: float
    exchange: str
    kind: str  # buy / sell / limit / triangle
    what: str  # пара или путь цикла
    live: bool
    spent: float
    spent_ccy: str
    got: float
    got_ccy: str
    usd_value: float
    pnl_usd: float | None = None  # для треугольника: USDT в конце минус в начале
    source: str = "manual"
    ok: bool = True
    note: str = ""
    order_id: str | None = None


@dataclass
class TradingState:
    live: bool = False  # False = тестовый режим, ордера не отправляются
    auto: bool = False  # автоторговля треугольниками
    day: str = ""
    day_pnl_usd: float = 0.0  # результат автоторговли/треугольников за сутки (только реальный режим)
    day_trades: int = 0  # реальных сделок бота за сутки
    paused_reason: str | None = None
    log: list[dict] = field(default_factory=list)
    history: list[list[float]] = field(default_factory=list)  # [[время, стоимость портфеля $], ...]
    scalp: dict = field(default_factory=dict)  # режим $5 (bot/scalper.py): включён ли, позиция, итоги


@dataclass
class TriExecution:
    executed: bool
    live: bool
    opp: TriOpportunity
    amount: float = 0.0
    final: float = 0.0
    reason: str = ""  # почему не исполнили / что пошло не так
    steps: list[str] = field(default_factory=list)
    code: str = ""  # машинная причина пропуска: no_balance | min_order | limits | books | moved
    stuck: str | None = None  # что осталось не в USDT после сбоя

    @property
    def pnl(self) -> float:
        return self.final - self.amount

    def text(self, name) -> str:
        title = f"{html.escape(self.opp.path_text)} на {html.escape(name(self.opp.exchange))}"
        mode = "" if self.live else "🧪 ТЕСТ (ордера не отправлялись): "
        if not self.executed:
            return f"⏭ {mode}{title}: не исполнял, {html.escape(self.reason)}"
        icon = "✅" if self.pnl >= 0 else "🔻"
        pct = self.pnl / self.amount * 100 if self.amount else 0
        lines = [
            f"{icon} {mode}<b>Треугольник исполнен</b>: {title}",
            f"${self.amount:.4f} → ${self.final:.4f} = <b>{self.pnl:+.4f}$</b> ({pct:+.3f}%)",
        ]
        lines += [f"   {html.escape(s)}" for s in self.steps]
        if self.reason:
            lines.append(f"⚠️ {html.escape(self.reason)}")
        if self.stuck:
            lines.append(f"⚠️ Осталось не в USDT: {html.escape(self.stuck)}. Продать: /sell")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Трейдер
# ---------------------------------------------------------------------------


class Trader:
    def __init__(self, scanner, settings: Settings, env=None):
        self.scanner = scanner
        self.settings = settings
        self.state = TradingState()
        self.creds = credentials_from_env(scanner.exchanges, env)
        self.key_status: dict[str, str] = {}  # биржа -> "ok" или текст ошибки
        self.key_info: dict[str, dict] = {}  # права и срок действия ключа (где биржа это сообщает)
        self.pending: OrderedDict[str, OrderPlan] = OrderedDict()
        self.recent_tri: dict[str, float] = {}  # ключ цикла -> до какого времени его не повторять
        self.auto_status: dict = {}  # что автоторговля сделала на последнем скане и почему
        self.auto_stats: dict = new_auto_stats()  # сводка с момента включения / последнего отчёта
        self.free_cache: dict[str, tuple[float, float]] = {}  # биржа -> (когда, свободно USDT)
        self.funding_cache: dict[str, tuple[float, float]] = {}  # биржа -> (когда, $ в стейблах на Funding)
        self.accounts_cache: dict[str, tuple[float, dict[str, float]]] = {}  # биржа -> (когда, {счёт: $})
        self.balance_cache: dict[str, tuple[float, list]] = {}
        self._lock = asyncio.Lock()  # одна операция с деньгами за раз
        for name, cred in self.creds.items():
            self._apply_creds(name, cred)

    # --- ключи --------------------------------------------------------------

    def _apply_creds(self, name: str, cred: dict) -> None:
        ex = self.scanner.exchanges[name]
        for k, v in cred.items():
            setattr(ex, k, v)
        options = getattr(ex, "options", None)
        if name == "bybit" and isinstance(options, dict):
            # Bybit без этого флага отказывается отдавать статус ордера
            options.setdefault("fetchOrder", {})["acknowledged"] = True

    @property
    def working(self) -> list[str]:
        """Биржи с проверенными рабочими ключами (или ещё не проверенными)."""
        return [n for n in self.creds if self.key_status.get(n, "ok") == "ok"]

    def trade_block_reason(self, name: str) -> str | None:
        """Почему на этой бирже бот сейчас не может торговать сам (None — может)."""
        status = self.key_status.get(name, "ok")
        if status != "ok":
            return f"ключ не работает: {status}"
        if self.state.live:  # в тесте ордера не уходят, права ключа не важны
            return key_trade_problem(name, self.key_info.get(name))
        return None

    async def check_keys(self) -> dict[str, str]:
        async def one(name: str) -> tuple[str, str]:
            ex = self.scanner.exchanges[name]
            try:
                await self.scanner._ensure_markets(name, ex)
                await ex.fetch_balance()
                return name, "ok"
            except Exception as e:
                return name, human_error(e)

        results = await asyncio.gather(*(one(n) for n in self.creds))
        self.key_status = dict(results)
        return self.key_status

    async def inspect_keys(self) -> dict[str, dict]:
        """Права и срок действия ключей там, где биржа это сообщает (Bybit, BingX)."""

        async def one(name: str) -> tuple[str, dict]:
            ex = self.scanner.exchanges[name]
            try:
                if name == "bybit":
                    res = await ex.privateGetV5UserQueryApi()
                    return name, parse_bybit_key((res or {}).get("result") or {})
                if name == "bingx":
                    res = await ex.accountV1PrivateGetAccountApiPermissions()
                    return name, parse_bingx_key((res or {}).get("data"))
            except Exception as e:
                return name, {"error": human_error(e)}
            return name, {}

        names = [n for n in self.working if n in KEY_INFO_EXCHANGES]
        for name, info in await asyncio.gather(*(one(n) for n in names)):
            info["checked_at"] = time.time()
            self.key_info[name] = info
        return self.key_info

    def key_warnings(self, now: float | None = None) -> list[str]:
        """Короткие предупреждения о ключах: скоро истекает, включён вывод."""
        now = time.time() if now is None else now
        out = []
        for name, info in self.key_info.items():
            title = self.scanner.display_name(name)
            days = days_left(info, now)
            if days is not None and days <= KEY_WARN_DAYS:
                out.append(f"ключ {title} перестанет работать через {max(days, 0):.0f} дн. "
                           f"Создай новый и замени в Render (как в /keys)")
            if info.get("withdraw"):
                out.append(f"у ключа {title} включено право вывода: создай ключ без Withdraw")
        return out

    def _ex(self, name: str):
        if name not in self.creds:
            raise TradeError(
                f"для {self.scanner.display_name(name)} нет API-ключа. Как подключить: /keys"
            )
        if self.key_status.get(name, "ok") != "ok":
            raise TradeError(f"ключ {self.scanner.display_name(name)} не работает: {self.key_status[name]}")
        return self.scanner.exchanges[name]

    def resolve_exchange(self, raw: str) -> str:
        raw = raw.strip().lower()
        aliases = {self.scanner.display_name(n).lower(): n for n in self.scanner.exchanges}
        aliases.update({"байбит": "bybit", "буббит": "bybit", "bybit.com": "bybit",
                        "бингх": "bingx", "бингикс": "bingx", "бинг": "bingx", "bing x": "bingx", "бинкс": "bingx",
                        "бингекс": "bingx", "bingx.com": "bingx"})
        name = aliases.get(raw, raw)
        if name not in self.scanner.exchanges:
            raise TradeError(f"не знаю биржу «{raw}». Есть: {', '.join(self.scanner.exchanges)}")
        return name

    # --- сутки и лимиты -------------------------------------------------------

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + self.settings.report_utc_offset * 3600))

    def _roll_day(self) -> None:
        today = self._today()
        if self.state.day != today:
            self.state.day = today
            self.state.day_pnl_usd = 0.0
            self.state.day_trades = 0

    def _check_limits(self, usd: float, is_buy: bool, source: str) -> None:
        self._roll_day()
        s = self.settings  # в тесте лимиты тоже проверяем, чтобы тест был честным
        if self.state.day_trades >= s.daily_trades:
            raise TradeError(f"лимит {s.daily_trades:g} сделок за сутки исчерпан (/set daily_trades)")
        if is_buy and usd > s.max_trade_usd * 1.0001:
            raise TradeError(f"${usd:.2f} больше лимита ${s.max_trade_usd:g} на сделку (/set max_trade)")
        if source == "auto" and self.state.day_pnl_usd <= -s.daily_loss_usd:
            raise TradeError(f"автоторговля на паузе: дневной убыток достиг ${s.daily_loss_usd:g}")

    def _record(self, rec: TradeRecord) -> None:
        # Неуспешный цикл тоже мог совершить сделки и получить убыток при возврате.
        if rec.live and (rec.ok or rec.kind == "triangle"):
            self._roll_day()
            self.state.day_trades += 1
            if rec.pnl_usd is not None:
                self.state.day_pnl_usd += rec.pnl_usd
                if self.state.auto and self.state.day_pnl_usd <= -self.settings.daily_loss_usd:
                    self.state.auto = False
                    self.state.paused_reason = (
                        f"дневной убыток {self.state.day_pnl_usd:.2f}$ достиг лимита ${self.settings.daily_loss_usd:g}"
                    )
        self.state.log.append(asdict(rec))
        del self.state.log[:-TRADE_LOG_KEEP]

    # --- сохранение ---------------------------------------------------------

    def export(self) -> dict:
        return asdict(self.state)

    def restore(self, raw: dict) -> None:
        known = {f for f in TradingState.__dataclass_fields__}
        self.state = TradingState(**{k: v for k, v in (raw or {}).items() if k in known})

    # --- баланс -------------------------------------------------------------

    async def _fetch_balance(self, name: str) -> dict:
        return await self._ex(name).fetch_balance()

    async def free(self, name: str, ccy: str) -> float:
        bal = await self._fetch_balance(name)
        return float((bal.get("free") or {}).get(ccy) or 0)

    def min_start_usd(self, name: str) -> float:
        """Сколько USDT нужно на торговом счёте, чтобы цикл из трёх шагов прошёл минимальный ордер биржи.

        Bybit с января 2025 принимает ордера через API от 5 USDT. После первого шага сумма чуть
        уменьшается на комиссию, поэтому ровно $5 на третьем шаге уже меньше минимума.
        """
        m = self.scanner.market(name, f"BTC/{self.settings.quote}") or {}
        min_cost = float(((m.get("limits") or {}).get("cost") or {}).get("min") or 0)
        fee = float(m.get("taker") or 0.001)
        need = min_cost / (1 - fee) ** 3 * 1.02 if min_cost else 0.0  # +2% на движение цены за секунды цикла
        return round(max(need, AUTO_MIN_FREE_USD), 2)

    async def funding_usd(self, name: str, now: float | None = None) -> float | None:
        """Сколько $ в стейблкоинах лежит на счёте Funding (бот им не торгует). None, если у биржи его нет."""
        now = time.time() if now is None else now
        cached = self.funding_cache.get(name)
        if cached and now - cached[0] < FUNDING_CACHE_SEC:
            return cached[1]
        ex = self.scanner.exchanges.get(name)
        accounts = (getattr(ex, "options", None) or {}).get("accountsByType") or {}
        if "funding" not in accounts or str(accounts["funding"]).lower() == str(accounts.get("spot", "spot")).lower():
            return None
        try:
            bal = await ex.fetch_balance({"type": "funding"})
        except Exception as e:
            log.info("Баланс Funding %s: %s", name, e)
            return None
        total = bal.get("total") or {}
        usd = sum(float(total.get(c) or 0) for c in STABLES)
        self.funding_cache[name] = (now, usd)
        return usd

    async def other_accounts(self, name: str, now: float | None = None) -> dict[str, float]:
        """{название счёта: $} — деньги на счетах биржи, кроме торгового (бот ими не торгует).

        BingX с 2025 года держит спот на отдельном «Спот»-счёте: деньги на «Фонде», фьючерсах,
        копитрейдинге или в grid-ботах API спота не показывает, и человеку кажется, что бот «не видит» денег."""
        now = time.time() if now is None else now
        cached = self.accounts_cache.get(name)
        if cached and now - cached[0] < ACCOUNTS_CACHE_SEC:
            return cached[1]
        out: dict[str, float] = {}
        ex = self.scanner.exchanges.get(name)
        if name == "bingx" and hasattr(ex, "accountV1PrivateGetAllAccountBalance"):
            try:
                res = await ex.accountV1PrivateGetAllAccountBalance()
                for row in (res or {}).get("data") or []:
                    acc = str(row.get("accountType") or "")
                    usd = float(row.get("usdtBalance") or 0)
                    if acc in ("spot",) or usd < 0.01:
                        continue
                    out[BINGX_ACCOUNTS.get(acc, acc)] = out.get(BINGX_ACCOUNTS.get(acc, acc), 0) + usd
            except Exception as e:
                log.info("Счета %s: %s", name, e)
        funding = await self.funding_usd(name, now)
        if funding and funding >= 0.01:
            label = "Фонд (Fund)" if name == "bingx" else "Funding"
            # «Спот/Фонд» у BingX = спот + фонд; если фонд прочитан точно, показываем его отдельно
            out.pop("Спот/Фонд", None)
            out[label] = funding
        elif "Спот/Фонд" in out:
            try:
                spot = sum(r[3] or 0 for r in await self.balances(name))
            except Exception:
                spot = None
            rest = out.pop("Спот/Фонд") - (spot or 0)
            if spot is not None and rest >= 0.5:
                out["Фонд (Fund)"] = rest
        self.accounts_cache[name] = (now, out)
        return out

    def where_money_text(self, name: str, accounts: dict[str, float], spot_usd: float | None = None) -> str:
        """Понятная подсказка: где лежат деньги и как перевести их на торговый счёт."""
        big = {k: v for k, v in accounts.items() if v >= 0.5}
        if not big:
            return ""
        title = self.scanner.display_name(name)
        where = ", ".join(f"«{k}» ≈ ${v:.2f}" for k, v in sorted(big.items(), key=lambda kv: -kv[1]))
        target = "Спот" if name == "bingx" else "Unified Trading" if name == "bybit" else "торговый (спот)"
        first = max(big, key=big.get)
        if first in ("Торговые боты (Grid)", "Копитрейдинг", "Накопления (Wealth)", "P2P (C2C)"):
            return (f"на {title} деньги лежат в разделе {where}, а бот торгует только со счёта «{target}». "
                    f"Сначала останови/закрой это в приложении {title} (деньги вернутся на «Фонд»), "
                    f"потом Активы → Перевести → с «Фонд» на «{target}»")
        return (f"на {title} деньги лежат на другом счёте: {where}. Бот торгует только со счёта «{target}»"
                + (f" (там сейчас ≈ ${spot_usd:.2f})" if spot_usd is not None else "")
                + f". Переведи бесплатно: приложение {title} → Активы → Перевести → с «{first}» на «{target}»")

    async def usd_prices(self, name: str, coins: list[str]) -> dict[str, float]:
        prices = {c: 1.0 for c in coins if c in STABLES}
        need = [c for c in coins if c not in prices]
        if not need:
            return prices
        quote = self.settings.quote
        ex = self.scanner.exchanges[name]

        async def one(c: str) -> None:
            symbol = f"{c}/{quote}"
            if symbol not in (ex.markets or {}):
                return
            try:
                t = await ex.fetch_ticker(symbol)
                price = t.get("bid") or t.get("last")
                if price:
                    prices[c] = float(price)
            except Exception:
                pass

        await asyncio.gather(*(one(c) for c in need[:15]))
        return prices

    async def balances(self, name: str) -> list[tuple[str, float, float, float | None]]:
        """[(монета, свободно, всего, ≈$)] по убыванию стоимости."""
        bal = await self._fetch_balance(name)
        free = bal.get("free") or {}
        items = [(c, float(free.get(c) or 0), float(t)) for c, t in (bal.get("total") or {}).items() if t and float(t) > 0]
        prices = await self.usd_prices(name, [c for c, _, _ in items])
        out = [(c, f, t, t * prices[c] if c in prices else None) for c, f, t in items]
        out.sort(key=lambda x: -(x[3] or 0))
        self.balance_cache[name] = (time.time(), out)
        return out

    async def all_balances_text(self) -> str:
        if not self.creds:
            return "Ключи бирж не подключены. Как подключить: /keys"
        results = await asyncio.gather(*(self.balances(n) for n in self.working), return_exceptions=True)
        parts = []
        total = 0.0
        for name, res in zip(self.working, results):
            title = html.escape(self.scanner.display_name(name))
            if isinstance(res, BaseException):
                parts.append(f"<b>{title}</b>: ❌ {html.escape(human_error(res))}")
                continue
            lines = [f"<b>{title}</b>"]
            shown = [r for r in res if (r[3] or 0) >= 0.01 or r[3] is None][:15]
            for c, f, t, usd in shown:
                usd_txt = f" ≈ ${usd:.2f}" if usd is not None else ""
                locked = f" (в ордерах {fmt_amount(t - f)})" if t - f > 1e-12 else ""
                lines.append(f"  {html.escape(c)}: {fmt_amount(t)}{usd_txt}{locked}")
                total += usd or 0
            free_q = next((f for c, f, _, _ in res if c == self.settings.quote), 0.0)
            lines.append(f"  <i>свободно для сделок: {free_q:.2f} {self.settings.quote}</i>")
            try:
                elsewhere = await self.other_accounts(name)
            except Exception:
                elsewhere = {}
            hint = self.where_money_text(name, elsewhere, sum(r[3] or 0 for r in res))
            if hint:
                lines.append("  💡 " + html.escape(hint))
            elif not shown:
                lines.append("  пусто. Если деньги на бирже есть: переведи их внутри биржи на торговый (спот) счёт")
            dust = len(res) - len(shown)
            if dust > 0:
                lines.append(f"  <i>и ещё {dust} монет меньше $0.01</i>")
            parts.append("\n".join(lines))
        for name in self.creds:
            if name not in self.working:
                parts.append(f"<b>{html.escape(self.scanner.display_name(name))}</b>: ❌ {html.escape(self.key_status[name])}")
        return "💰 <b>Балансы</b>\n\n" + "\n\n".join(parts) + f"\n\nВсего ≈ <b>${total:.2f}</b>"

    async def portfolio(self) -> dict:
        """Балансы всех бирж с ключами + общая стоимость. Заодно пишет точку в историю для графика."""
        names = self.working
        results = await asyncio.gather(*(self.balances(n) for n in names), return_exceptions=True)
        exchanges, total, ok = [], 0.0, False
        for name, res in zip(names, results):
            item = {"exchange": name, "name": self.scanner.display_name(name)}
            if isinstance(res, BaseException):
                item["error"] = human_error(res)
            else:
                ok = True
                coins = [{"coin": c, "free": f, "total": t, "usd": u} for c, f, t, u in res]
                item["coins"] = coins
                item["total_usd"] = sum(c["usd"] or 0 for c in coins)
                total += item["total_usd"]
                try:
                    elsewhere = await self.other_accounts(name)
                except Exception:
                    elsewhere = {}
                if elsewhere:
                    item["other_accounts"] = {k: round(v, 2) for k, v in elsewhere.items()}
                    item["hint"] = self.where_money_text(name, elsewhere, item["total_usd"])
            exchanges.append(item)
        for name in self.creds:
            if name not in names:
                exchanges.append({"exchange": name, "name": self.scanner.display_name(name),
                                  "error": self.key_status.get(name, "ключ не работает")})
        if ok:
            self.record_equity(total)
        return {"total_usd": total, "exchanges": exchanges, "updated_at": time.time()}

    def record_equity(self, total: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        hist = self.state.history
        if len(hist) >= 2 and now - hist[-2][0] < HISTORY_EVERY_SEC:
            hist[-1] = [now, round(total, 4)]  # последняя точка «плавает», остальные не чаще раза в 15 минут
        else:
            hist.append([now, round(total, 4)])
        del hist[:-HISTORY_KEEP]

    def balances_for_ai(self) -> dict:
        """Последние известные балансы (без запросов к бирже) для контекста ИИ."""
        out = {}
        for name, (at, rows) in self.balance_cache.items():
            out[name] = {
                "minutes_ago": round((time.time() - at) / 60, 1),
                "coins": {c: {"amount": round(t, 8), "usd": round(u, 2) if u is not None else None} for c, _, t, u in rows[:12]},
            }
            other = (self.accounts_cache.get(name) or (0, {}))[1]
            if other:
                out[name]["money_on_other_accounts_bot_cannot_trade"] = {k: round(v, 2) for k, v in other.items()}
        return out

    # --- предпросмотр ордера ------------------------------------------------

    def _market(self, name: str, symbol: str) -> dict:
        m = self.scanner.market(name, symbol)
        if not m:
            raise TradeError(f"на {self.scanner.display_name(name)} нет пары {symbol}")
        if m.get("active") is False:
            raise TradeError(f"пара {symbol} на {self.scanner.display_name(name)} сейчас не торгуется")
        return m

    @staticmethod
    def _limits(m: dict) -> tuple[float, float]:
        limits = m.get("limits") or {}
        return (
            float((limits.get("amount") or {}).get("min") or 0),
            float((limits.get("cost") or {}).get("min") or 0),
        )

    async def plan_market(self, name: str, side: str, coin: str, amount_spec: str, source: str = "manual") -> OrderPlan:
        """Покупка за USDT (amount_spec в USDT) или продажа монеты (amount_spec в монетах)."""
        ex = self._ex(name)
        await self.scanner._ensure_markets(name, ex)
        quote = self.settings.quote
        coin = coin.upper().strip()
        if coin == quote:
            raise TradeError(f"{quote} и так валюта счёта. Укажи монету, например BTC")
        symbol = f"{coin}/{quote}"
        m = self._market(name, symbol)
        fee = float(m.get("taker") or 0.001)
        bal = await self._fetch_balance(name)
        free = bal.get("free") or {}
        book = await self.scanner.order_book(name, symbol)
        min_amount, min_cost = self._limits(m)
        warnings: list[str] = []

        if side == "buy":
            available = float(free.get(quote) or 0)
            cost = parse_amount(amount_spec, min(available, self.settings.max_trade_usd))
            self._check_funds(name, cost, available, quote, warnings)
            self._check_limits(cost, True, source)
            spent, coins = simulate_buy(book.get("asks") or [], cost, fee)
            if coins <= 0:
                raise TradeError("стакан пустой")
            if spent < cost * 0.999:
                warnings.append(f"в стакане хватает только на {spent:.2f} {quote}")
            price = spent / (coins / (1 - fee))
            if cost < min_cost or coins < min_amount:
                raise TradeError(f"меньше минимального ордера биржи (мин. ≈{max(min_cost, min_amount * price):.2f} {quote})")
            plan = OrderPlan(name, symbol, "buy", "market", cost, quote, coins, coin, price, fee, cost, source=source, warnings=warnings)
        else:
            available = float(free.get(coin) or 0)
            amount = parse_amount(amount_spec, available)
            self._check_funds(name, amount, available, coin, warnings)
            received, left = simulate_sell(book.get("bids") or [], amount, fee)
            if received <= 0:
                raise TradeError("стакан пустой")
            if left > 0:
                warnings.append(f"в стакане не хватает покупателей на {fmt_amount(left)} {coin}")
            price = received / ((amount - left) * (1 - fee))
            if amount < min_amount or amount * price < min_cost:
                raise TradeError(f"меньше минимального ордера биржи (мин. ≈{max(min_cost, min_amount * price):.2f} {quote})")
            self._check_limits(received, False, source)
            plan = OrderPlan(name, symbol, "sell", "market", amount, coin, received, quote, price, fee, received,
                             source=source, warnings=warnings)
        return self._remember(plan)

    async def plan_limit(self, name: str, side: str, coin: str, amount_spec: str, price: float, source: str = "manual") -> OrderPlan:
        """Лимитный ордер: покупка на amount_spec USDT по цене price или продажа монет по цене price."""
        ex = self._ex(name)
        await self.scanner._ensure_markets(name, ex)
        quote = self.settings.quote
        coin = coin.upper().strip()
        symbol = f"{coin}/{quote}"
        m = self._market(name, symbol)
        if price <= 0:
            raise TradeError("цена должна быть больше нуля")
        fee = float(m.get("maker") or m.get("taker") or 0.001)
        bal = await self._fetch_balance(name)
        free = bal.get("free") or {}
        book = await self.scanner.order_book(name, symbol)
        best_bid = float((book.get("bids") or [[0]])[0][0])
        best_ask = float((book.get("asks") or [[0]])[0][0])
        min_amount, min_cost = self._limits(m)
        warnings = []
        if side == "buy":
            available = float(free.get(quote) or 0)
            cost = parse_amount(amount_spec, min(available, self.settings.max_trade_usd))
            self._check_funds(name, cost, available, quote, warnings)
            self._check_limits(cost, True, source)
            amount = cost / price
            if best_ask and price >= best_ask:
                warnings.append(f"цена выше лучшей продажи {best_ask:.8g}: исполнится сразу как рыночный")
            elif best_ask and price < best_ask * 0.5:
                warnings.append("цена вдвое ниже рынка: может не исполниться никогда")
            got, spent, got_ccy, spent_ccy, usd = amount * (1 - fee), cost, coin, quote, cost
        else:
            available = float(free.get(coin) or 0)
            amount = parse_amount(amount_spec, available)
            self._check_funds(name, amount, available, coin, warnings)
            if best_bid and price <= best_bid:
                warnings.append(f"цена ниже лучшей покупки {best_bid:.8g}: исполнится сразу как рыночный")
            self._check_limits(amount * price, False, source)
            got, spent, got_ccy, spent_ccy, usd = amount * price * (1 - fee), amount, quote, coin, amount * price
        if amount < min_amount or amount * price < min_cost:
            raise TradeError(f"меньше минимального ордера биржи (мин. ≈{max(min_cost, min_amount * price):.2f} {quote})")
        plan = OrderPlan(name, symbol, side, "limit", spent, spent_ccy, got, got_ccy, price, fee, usd,
                         price=price, source=source, warnings=warnings)
        return self._remember(plan)

    def _check_funds(self, name: str, need: float, available: float, ccy: str, warnings: list[str]) -> None:
        """В реальном режиме не хватает денег = ошибка. В тесте можно потренироваться и без денег на бирже."""
        if need <= 0:
            raise TradeError(f"на {self.scanner.display_name(name)} нет свободных {ccy}")
        if need <= available * 1.0001:
            return
        text = f"на {self.scanner.display_name(name)} свободно {fmt_amount(available)} {ccy}, а нужно {fmt_amount(need)}"
        if self.state.live:
            raise TradeError(text)
        warnings.append(text + " (в тесте можно, в реальном режиме ордер не пройдёт)")

    def _remember(self, plan: OrderPlan) -> OrderPlan:
        self.pending[plan.id] = plan
        while len(self.pending) > PENDING_KEEP:
            self.pending.popitem(last=False)
        return plan

    def get_plan(self, plan_id: str) -> OrderPlan:
        plan = self.pending.get(plan_id)
        if plan is None:
            raise TradeError("этот предпросмотр уже использован или устарел, создай ордер заново")
        if time.time() - plan.created_at > PENDING_TTL_SEC:
            self.pending.pop(plan_id, None)
            raise TradeError("предпросмотр устарел (больше 5 минут), цены изменились. Создай ордер заново")
        return plan

    def plan_text(self, plan: OrderPlan) -> str:
        name = html.escape(self.scanner.display_name(plan.exchange))
        mode = "🔴 РЕАЛЬНЫЙ ордер" if self.state.live else "🧪 ТЕСТ (ордер не будет отправлен)"
        action = {"buy": "Покупка", "sell": "Продажа"}[plan.side]
        kind = "по рынку" if plan.type == "market" else f"лимитная по {plan.price:.8g}"
        who = {"ai": " · предложил ИИ", "auto": " · автоторговля"}.get(plan.source, "")
        lines = [
            f"<b>{action} {html.escape(plan.symbol)} на {name}</b> ({kind}){who}",
            f"Отдаёшь: <b>{fmt_amount(plan.spent)} {html.escape(plan.spent_ccy)}</b>",
            f"Получишь ≈ <b>{fmt_amount(plan.est_got)} {html.escape(plan.got_ccy)}</b> (после комиссии {plan.fee_rate * 100:.3g}%)",
            f"Средняя цена ≈ {plan.est_price:.8g}",
        ]
        if plan.type == "market":
            lines.append(f"Перед отправкой проверка стакана: допустимое ухудшение {self.settings.max_slippage_percent:g}%. "
                         "После отправки рыночный ордер может исполниться по худшей цене; это не гарантия.")
        lines += [f"⚠️ {html.escape(w)}" for w in plan.warnings]
        lines.append(mode)
        return "\n".join(lines)

    # --- исполнение ордера --------------------------------------------------

    async def execute_plan(self, plan_id: str) -> TradeRecord:
        plan = self.get_plan(plan_id)
        async with self._lock:
            self.pending.pop(plan_id, None)
            ex = self._ex(plan.exchange)
            self._check_limits(plan.usd_value, plan.side == "buy", plan.source)
            if plan.type == "market":
                await self._recheck_price(plan)
            if not self.state.live:
                rec = TradeRecord(time.time(), plan.exchange, plan.side if plan.type == "market" else "limit",
                                  plan.symbol, False, plan.spent, plan.spent_ccy, plan.est_got, plan.got_ccy,
                                  plan.usd_value, source=plan.source, note="тест: ордер не отправлялся")
                self._record(rec)
                return rec
            try:
                if plan.type == "limit":
                    amount = plan.spent / plan.price if plan.side == "buy" else plan.spent
                    order = await ex.create_order(
                        plan.symbol, "limit", plan.side, self._amount(ex, plan.symbol, amount), self._price(ex, plan.symbol, plan.price)
                    )
                    rec = TradeRecord(time.time(), plan.exchange, "limit", plan.symbol, True, plan.spent, plan.spent_ccy,
                                      0.0, plan.got_ccy, plan.usd_value, source=plan.source,
                                      note=f"лимитный {plan.side} по {plan.price:.8g} выставлен", order_id=str(order.get("id")))
                    self._record(rec)
                    return rec
                if plan.side == "buy":
                    order = await ex.create_market_buy_order_with_cost(plan.symbol, self._cost(ex, plan.symbol, plan.spent))
                else:
                    order = await ex.create_market_sell_order(plan.symbol, self._amount(ex, plan.symbol, plan.spent))
            except Exception as e:
                raise TradeError(human_error(e)) from e
            spent, got = await self._settle(ex, plan.exchange, order, plan.symbol, plan.side, plan.fee_rate)
            rec = TradeRecord(time.time(), plan.exchange, plan.side, plan.symbol, True, spent, plan.spent_ccy, got,
                              plan.got_ccy, plan.usd_value, source=plan.source, order_id=str(order.get("id")))
            self._record(rec)
            return rec

    async def _recheck_price(self, plan: OrderPlan) -> None:
        book = await self.scanner.order_book(plan.exchange, plan.symbol)
        slip = self.settings.max_slippage_percent / 100
        if plan.side == "buy":
            spent, coins = simulate_buy(book.get("asks") or [], plan.spent, plan.fee_rate)
            if coins <= 0:
                raise TradeError("стакан пустой, ордер не отправлен")
            if spent < plan.spent * (1 - 1e-9):
                raise TradeError("недостаточно глубины стакана для покупки, ордер не отправлен")
            price = spent / (coins / (1 - plan.fee_rate))
            if price > plan.est_price * (1 + slip):
                raise TradeError(f"цена выросла на {(price / plan.est_price - 1) * 100:.2f}%, ордер не отправлен")
        else:
            received, left = simulate_sell(book.get("bids") or [], plan.spent, plan.fee_rate)
            if received <= 0:
                raise TradeError("стакан пустой, ордер не отправлен")
            if left > plan.spent * 1e-9:
                raise TradeError("недостаточно глубины стакана для продажи, ордер не отправлен")
            price = received / ((plan.spent - left) * (1 - plan.fee_rate))
            if price < plan.est_price * (1 - slip):
                raise TradeError(f"цена упала на {(1 - price / plan.est_price) * 100:.2f}%, ордер не отправлен")

    @staticmethod
    def _amount(ex, symbol: str, amount: float) -> float:
        try:
            return float(ex.amount_to_precision(symbol, amount))
        except Exception:
            return amount

    @staticmethod
    def _price(ex, symbol: str, price: float) -> float:
        try:
            return float(ex.price_to_precision(symbol, price))
        except Exception:
            return price

    @staticmethod
    def _cost(ex, symbol: str, cost: float) -> float:
        try:
            return float(ex.cost_to_precision(symbol, cost))
        except Exception:
            return cost

    async def _fetch_order(self, ex, oid: str, symbol: str) -> dict | None:
        try:
            return await ex.fetch_order(oid, symbol)
        except Exception as e:
            log.info("fetch_order %s: %s", oid, e)
            for method in ("fetch_closed_order", "fetch_open_order"):
                fn = getattr(ex, method, None)
                if fn is None:
                    continue
                try:
                    return await fn(oid, symbol)
                except Exception:
                    continue
        return None

    async def _settle(self, ex, name: str, order: dict, symbol: str, side: str, fee_rate: float) -> tuple[float, float]:
        """Дожидается исполнения рыночного ордера. Возвращает (сколько отдали, сколько получили после комиссии)."""
        o = order or {}
        oid = o.get("id")
        for attempt in range(5):
            if o.get("filled") and (o.get("status") in ("closed", "canceled", "expired") or attempt >= 2):
                break
            if not oid:
                break
            await asyncio.sleep(0.25 * (attempt + 1))
            o = await self._fetch_order(ex, oid, symbol) or o
        base, quote = symbol.split("/")
        filled = float(o.get("filled") or 0)
        cost = float(o.get("cost") or 0) or filled * float(o.get("average") or o.get("price") or 0)
        if side == "buy":
            spent, got, got_ccy = cost, filled, base
        else:
            spent, got, got_ccy = filled, cost, quote
        fees = [f for f in (o.get("fees") or [o.get("fee")]) if f and f.get("cost") is not None]
        if fees:
            got -= sum(float(f["cost"]) for f in fees if f.get("currency") == got_ccy)
        else:
            got *= 1 - fee_rate
        if filled <= 0:
            # биржа не подтвердила исполнение: не гадаем, а просим проверить вручную
            raise TradeError(f"не удалось подтвердить исполнение ордера {oid}: проверь /orders и баланс /balance")
        return spent, max(got, 0.0)

    # --- открытые ордера ------------------------------------------------------

    async def open_orders(self, name: str) -> list[dict]:
        ex = self._ex(name)
        try:
            return await ex.fetch_open_orders()
        except Exception as e:
            raise TradeError(human_error(e)) from e

    async def cancel(self, name: str, order_id: str, symbol: str) -> None:
        ex = self._ex(name)
        try:
            await ex.cancel_order(order_id, symbol)
        except Exception as e:
            raise TradeError(human_error(e)) from e

    async def panic(self) -> str:
        """Стоп-кран: выключить автоторговлю и реальный режим, отменить все открытые ордера."""
        hook = getattr(self, "on_panic", None)
        if hook is not None:  # ♾ полный автомат: снять разрешение на деньги, сам ничего не включать
            try:
                hook()
            except Exception:
                log.exception("on_panic")
        self.state.auto = False
        scalp_pos = bool(self.state.scalp.get("positions") or self.state.scalp.get("pos"))
        if self.state.scalp:
            self.state.scalp["on"] = False
        was_live = self.state.live
        self.state.live = False
        self.pending.clear()
        lines = ["🛑 <b>Стоп</b>: автоторговля и режим $5 выключены, режим переключён на тестовый."]
        for name in self.working:
            title = html.escape(self.scanner.display_name(name))
            try:
                orders = await self.open_orders(name)
            except TradeError as e:
                lines.append(f"{title}: не смог получить ордера ({html.escape(str(e))})")
                continue
            cancelled, failed = 0, 0
            for o in orders:
                try:
                    await self.cancel(name, o["id"], o["symbol"])
                    cancelled += 1
                except TradeError:
                    failed += 1
            if orders:
                lines.append(f"{title}: отменено ордеров {cancelled}" + (f", не удалось {failed}" if failed else ""))
        if was_live:
            lines.append("Монеты на балансе не продавались. Посмотреть: /balance, продать: /sell")
        if scalp_pos:
            lines.append("У режима $5 была открыта позиция: её продажа по цели отменена. Включишь режим снова (/scalp on) — "
                         "бот выставит продажу заново.")
        return "\n".join(lines)

    # --- треугольник --------------------------------------------------------

    async def execute_triangle(self, opp: TriOpportunity, amount: float | None = None, source: str = "manual",
                               min_net: float | None = None) -> TriExecution:
        name = opp.exchange
        start = opp.legs[0].from_ccy
        need = self.settings.tri_min_profit_percent if min_net is None else min_net
        async with self._lock:
            ex = self._ex(name)
            free = await self.free(name, start)
            cap = self.settings.max_trade_usd if amount is None else min(amount, self.settings.max_trade_usd)
            amount = min(cap, free * 0.998)
            note = ""
            if not self.state.live and amount < cap * 0.5:
                note = f"на бирже {free:.2f} {start}, тест посчитан на ${cap:g}"
                amount = cap
            res = TriExecution(False, self.state.live, opp, amount)
            if amount <= 0:
                res.reason = f"на {self.scanner.display_name(name)} нет свободных {start} (торговый счёт пуст)"
                res.code = "no_balance"
                return res
            try:
                self._check_limits(amount, True, source)
            except TradeError as e:
                res.reason, res.code = str(e), "limits"
                return res
            markets = {leg.symbol: self.scanner.market(name, leg.symbol) for leg in opp.legs}
            if not path_min_order_ok(opp.legs, amount, markets):
                need_amount = path_min_amount(opp.legs, markets) * 1.02
                where = f"свободно {free:.2f} {start}" if self.state.live else f"лимит ${cap:g}"
                res.reason = (f"${amount:.2f} меньше минимального ордера биржи: для этого цикла нужно от "
                              f"${need_amount:.2f} ({where})")
                res.code = "min_order"
                return res
            try:
                books = await self.scanner.order_books(name, [leg.symbol for leg in opp.legs])
            except Exception as e:
                res.reason, res.code = f"не получил стаканы: {human_error(e)}", "books"
                return res
            sim = simulate_path(books, opp.legs, amount)
            if not sim.filled or sim.profit_percent < need:
                res.reason = f"на свежих стаканах {sim.profit_percent:+.3f}% (нужно ≥{need:g}%), возможность ушла"
                res.code = "moved"
                return res

            if not self.state.live:
                res.executed, res.final = True, sim.received_usd
                res.steps = [f"ожидаемо по стакану: {sim.profit_percent:+.3f}%"]
                res.reason = note
                self._record(TradeRecord(time.time(), name, "triangle", opp.path_text, False, amount, start,
                                         sim.received_usd, start, amount, pnl_usd=res.pnl, source=source,
                                         note="тест: ордера не отправлялись"))
                return res

            holding_ccy, holding = start, amount
            try:
                for i, leg in enumerate(opp.legs):
                    if leg.side == "buy":
                        order = await ex.create_market_buy_order_with_cost(leg.symbol, self._cost(ex, leg.symbol, holding))
                    else:
                        order = await ex.create_market_sell_order(leg.symbol, self._amount(ex, leg.symbol, holding))
                    spent, got = await self._settle(ex, name, order, leg.symbol, leg.side, leg.fee)
                    if i < len(opp.legs) - 1:
                        # не продаём больше, чем реально лежит (другие монеты пользователя не трогаем)
                        got = min(got, await self.free(name, leg.to_ccy))
                    res.steps.append(f"{i + 1}) {fmt_amount(spent)} {leg.from_ccy} → {fmt_amount(got)} {leg.to_ccy}")
                    holding_ccy, holding = leg.to_ccy, got
            except Exception as e:
                res.reason = f"шаг {len(res.steps) + 1} не прошёл: {human_error(e)}"
                if holding_ccy != start:
                    holding = await self._unwind(ex, name, holding_ccy, holding, start, res)
                    if res.stuck is None:
                        holding_ccy = start
            res.executed = True
            if holding_ccy == start:
                res.final = holding
            else:  # монета застряла: оцениваем по текущей цене, чтобы дневной убыток считался честно
                price = (await self.usd_prices(name, [holding_ccy])).get(holding_ccy, 0.0)
                res.final = holding * price
                res.reason += f" (остаток оценён ≈${res.final:.4f})"
            self._record(TradeRecord(time.time(), name, "triangle", opp.path_text, True, amount, start, res.final, start,
                                     amount, pnl_usd=res.pnl, source=source, ok=not res.reason, note=res.reason))
            return res

    async def _unwind(self, ex, name: str, ccy: str, amount: float, start: str, res: TriExecution) -> float:
        """После сбоя продаём застрявшую монету обратно в USDT, чтобы не держать риск."""
        symbol = f"{ccy}/{start}"
        try:
            if not self.scanner.market(name, symbol):
                raise TradeError(f"нет пары {symbol}")
            amount = min(amount, await self.free(name, ccy))
            order = await ex.create_market_sell_order(symbol, self._amount(ex, symbol, amount))
            _, got = await self._settle(ex, name, order, symbol, "sell", float(self.scanner.market(name, symbol).get("taker") or 0.001))
            res.steps.append(f"возврат: {fmt_amount(amount)} {ccy} → {fmt_amount(got)} {start}")
            return got
        except Exception as e:
            res.stuck = f"{fmt_amount(amount)} {ccy} ({human_error(e)})"
            return amount

    # --- автоторговля -------------------------------------------------------

    def _auto_estimate(self, opp: TriOpportunity) -> float:
        """Оценка прибыли цикла для отбора кандидатов (точная проверка будет по свежим стаканам).

        Проверка стакана сканером сделана на trade_amount_usd ($100). Если эта сумма не больше той,
        с которой торгуем, её результат честный; если больше (а торгуем мы $10), большой стакан
        занижает прибыль, и берём оценку по лучшим ценам.
        """
        d = opp.depth
        if d is not None and d.filled and d.amount_usd <= self.settings.max_trade_usd * 1.01:
            return d.profit_percent
        return opp.net_percent

    async def auto_step(self, result) -> list[TriExecution]:
        """Вызывается после каждого скана. Пробует до AUTO_TRY_MAX лучших циклов, исполняет не больше одного.

        Возвращает только исполненные циклы (пропуски не шлём в чат каждый скан, а пишем в auto_status:
        их видно в /why, /trade, /doctor и в приложении, плюс раз в несколько часов приходит сводка).
        """
        if not self.state.auto:
            return []
        now = time.time()
        s = self.settings
        need = s.auto_min_profit_percent
        self.recent_tri = {k: t for k, t in self.recent_tri.items() if t > now}
        triangles = list(getattr(result, "triangles", None) or [])
        status: dict = {"at": now, "live": self.state.live, "need": need, "triangles": len(triangles),
                        "candidates": 0, "tried": [], "blocked": {}, "reason": "", "code": "", "executed": False,
                        "best": None}
        if triangles:
            top = max(triangles, key=lambda o: o.net_percent)
            status["best"] = {"net": round(top.net_percent, 4), "path": top.path_text, "exchange": top.exchange}
        else:
            near = getattr(result, "near_tri", None)
            if near is not None:
                status["best"] = {"net": round(near.net_percent, 4), "path": near.text, "exchange": ""}

        usable = []
        for name in self.creds:
            why = self.trade_block_reason(name)
            if why:
                status["blocked"][name] = why
            else:
                usable.append(name)
        executions: list[TriExecution] = []
        if usable:  # и в тесте: сразу видно, что на торговом счёте пусто, ещё до реального режима
            status["free"] = await self._free_quote(usable, now)
            status["low_money"] = {n: v for n, v in status["free"].items()
                                   if v is not None and v < self.min_start_usd(n)}
            status["need_usd"] = {n: self.min_start_usd(n) for n in usable}
            funding = {}
            for n in status["low_money"]:
                f = await self.funding_usd(n, now)
                if f and f >= 1:
                    funding[n] = round(f, 2)
            if funding:
                status["funding"] = funding
        if not self.creds:
            status["reason"], status["code"] = "нет API-ключей бирж (/keys)", "no_keys"
        elif not usable:
            status["reason"], status["code"] = "ни на одной бирже с ключом бот сейчас не может торговать", "blocked"
        elif not s.triangular_enabled:
            status["reason"], status["code"] = "треугольный арбитраж выключен (TRIANGULAR=0)", "disabled"
        else:
            self._roll_day()
            if self.state.day_trades >= s.daily_trades:
                status["reason"], status["code"] = f"лимит {s.daily_trades:g} сделок за сутки исчерпан", "limits"
            else:
                cands = [o for o in triangles if o.exchange in usable and "|".join(o.key) not in self.recent_tri
                         and self._auto_estimate(o) >= need]
                cands.sort(key=self._auto_estimate, reverse=True)
                status["candidates"] = len(cands)
                if not cands:
                    best = status["best"]
                    best_txt = f", лучший сейчас {best['net']:+.3f}%" if best else ""
                    status["reason"] = f"нет циклов с прибылью ≥{need:g}% после комиссий{best_txt}"
                    status["code"] = "no_candidates"
                skip_ex: set[str] = set()
                for opp in cands[:AUTO_TRY_MAX]:
                    if opp.exchange in skip_ex:
                        continue
                    key = "|".join(opp.key)
                    res = await self.execute_triangle(opp, source="auto", min_net=need)
                    status["tried"].append({"path": opp.path_text, "exchange": opp.exchange,
                                            "estimate": round(self._auto_estimate(opp), 4),
                                            "executed": res.executed, "reason": res.reason, "code": res.code})
                    if res.executed:
                        self.recent_tri[key] = now + TRI_REPEAT_SEC
                        status["executed"], status["reason"], status["code"] = True, res.reason, ""
                        executions.append(res)
                        break
                    self.recent_tri[key] = now + AUTO_SKIP_REPEAT_SEC
                    status["reason"], status["code"] = res.reason, res.code
                    if res.code == "limits":
                        break
                    if res.code in ("no_balance", "min_order"):
                        skip_ex.add(opp.exchange)  # другие циклы на этой бирже упрутся в то же самое
        self.auto_status = status
        self._update_auto_stats(status, executions)
        return executions

    async def _free_quote(self, names: list[str], now: float) -> dict[str, float | None]:
        """Свободные USDT на биржах (кэш 5 минут, чтобы не дёргать биржу каждый скан)."""
        quote = self.settings.quote

        async def one(name: str) -> None:
            cached = self.free_cache.get(name)
            if cached and now - cached[0] < FREE_CACHE_SEC:
                return
            try:
                self.free_cache[name] = (now, await self.free(name, quote))
            except Exception as e:
                log.info("Баланс %s для автоторговли: %s", name, e)

        await asyncio.gather(*(one(n) for n in names))
        return {n: (self.free_cache[n][1] if n in self.free_cache else None) for n in names}

    def _update_auto_stats(self, status: dict, executions: list[TriExecution]) -> None:
        st = self.auto_stats
        st["checks"] += 1
        st["candidates"] += status["candidates"]
        st["attempts"] += len(status["tried"])
        st["executed"] += len(executions)
        st["pnl"] += sum(r.pnl for r in executions)
        best = status.get("best")
        if best and (st["best"] is None or best["net"] > st["best"]["net"]):
            st["best"] = dict(best)
        if not status["executed"] and status["code"]:
            reasons = st["reasons"]
            reasons[status["code"]] = {"n": (reasons.get(status["code"]) or {}).get("n", 0) + 1, "text": status["reason"]}

    def reset_auto_stats(self) -> None:
        self.auto_stats = new_auto_stats()
        self.auto_status = {}

    def auto_problem(self) -> tuple[str, str] | None:
        """Проблема, из-за которой автоторговля в принципе не сможет торговать (не «рынок спокоен»).

        Возвращает (код, текст) для разового предупреждения владельцу, или None.
        """
        st = self.auto_status
        if not self.state.auto or not st:
            return None
        if st.get("code") in ("blocked", "no_keys", "disabled"):
            details = "; ".join(f"{self.scanner.display_name(n)}: {why}" for n, why in st.get("blocked", {}).items())
            return st["code"], st["reason"] + (f". {details}" if details else "")
        tried = st.get("tried") or []
        money = [t for t in tried if t["code"] in ("no_balance", "min_order")]
        if self.state.live and tried and len(money) == len(tried):
            return "money", money[0]["reason"]
        low, free = st.get("low_money") or {}, st.get("free") or {}
        if self.state.live and low and len(low) == len([v for v in free.values() if v is not None]):
            if st.get("funding"):
                return "funding", self._funding_text(st["funding"])
            parts = ", ".join(f"{self.scanner.display_name(n)} {v:.2f} {self.settings.quote} (нужно от "
                              f"${self.min_start_usd(n):.2f})" for n, v in low.items())
            return "money", (f"на торговом счёте мало денег: {parts}. Биржа принимает ордер через API только от "
                             f"минимума (у Bybit $5) на КАЖДЫЙ из трёх шагов цикла, поэтому с такой суммой цикл не пройдёт")
        return None

    def auto_digest(self) -> str:
        """Сводка автоторговли за период (присылается владельцу, если сделок не было)."""
        st = self.auto_stats
        hours = max((time.time() - st["since"]) / 3600, 0.01)
        mode = "🔴 реально" if self.state.live else "🧪 тест"
        lines = [f"⚙️ <b>Автоторговля: сводка за {hours:.1f} ч</b> ({mode})",
                 f"Сканов: {st['checks']} · подходящих циклов: {st['candidates']} · проверено стаканами: "
                 f"{st['attempts']} · исполнено: <b>{st['executed']}</b>"]
        if st["executed"]:
            lines.append(f"Результат: {st['pnl']:+.4f}$")
        if st["best"]:
            b = st["best"]
            where = f" на {html.escape(self.scanner.display_name(b['exchange']))}" if b.get("exchange") else ""
            lines.append(f"Лучший замеченный цикл: {b['net']:+.3f}% {html.escape(b['path'])}{where} "
                         f"(порог {self.settings.auto_min_profit_percent:g}%)")
        lines += self._reason_lines(st["reasons"])
        lines.append(self._auto_hint())
        lines.append("Подробно: /why")
        return "\n".join(x for x in lines if x)

    @staticmethod
    def _reason_lines(reasons: dict) -> list[str]:
        if not reasons:
            return []
        top = sorted(reasons.items(), key=lambda kv: -kv[1]["n"])[:AUTO_REASONS_KEEP]
        return ["Почему не было сделок:"] + [f"  • {html.escape(v['text'])} ×{v['n']}" for _, v in top]

    def _funding_text(self, funding: dict) -> str:
        where = {"bybit": "Bybit → Активы → Перевод → со счёта Funding на Unified Trading",
                 "bingx": "BingX → Активы → Перевод → со счёта Fund на Spot"}
        return "; ".join(
            f"на {self.scanner.display_name(n)} ${v:.2f} лежат на счёте Funding, а бот торгует только с торгового счёта. "
            f"Переведи (бесплатно, 10 секунд): {where.get(n, 'Активы → Перевод → с Funding на торговый счёт')}"
            for n, v in funding.items())

    def _auto_hint(self) -> str:
        st, s = self.auto_status, self.settings
        code = st.get("code") if st else ""
        codes = {t["code"] for t in (st.get("tried") or [])} if st else set()
        if code in ("blocked", "no_keys"):
            return "💡 Исправь ключ (см. выше), затем /doctor: там должно быть ✅."
        if st and st.get("funding"):
            return "💡 Деньги не на том счёте: " + self._funding_text(st["funding"]) + "."
        if st and st.get("low_money") and len(st["low_money"]) == len([v for v in (st.get("free") or {}).values() if v is not None]):
            codes.add("min_order")
        if "min_order" in codes or "no_balance" in codes or code in ("min_order", "no_balance"):
            need = max((st.get("need_usd") or {}).values(), default=AUTO_MIN_FREE_USD) if st else AUTO_MIN_FREE_USD
            return (f"💡 На торговом счёте слишком мало. Bybit принимает ордера через API от $5 на каждый из трёх шагов "
                    f"цикла, поэтому нужно минимум ${need:.2f}, а с запасом на движение цены $10–20. Пополни торговый "
                    f"(Unified/Spot) счёт или оставь /test.")
        if code == "limits":
            return "💡 Сработал дневной лимит. Завтра счётчик обнулится, или поменяй /set daily_trades."
        if code in ("no_candidates", "moved"):
            return (f"💡 Это нормально: циклы ≥{s.auto_min_profit_percent:g}% после комиссий бывают редко и живут секунды. "
                    f"Бот ничего не покупает, пока нет прибыли, и это защищает деньги. Порог можно снизить: "
                    f"<code>/set auto_min 0.15</code> (риск проскальзывания выше).")
        return ""

    def auto_status_text(self) -> str:
        """Полный ответ на «почему автоторговля ничего не делает» (/why)."""
        s, state, st = self.settings, self.state, self.auto_status
        mode = "🔴 реально" if state.live else "🧪 тест (ордера не отправляются)"
        lines = [f"⚙️ <b>Автоторговля</b>: {'🟢 включена' if state.auto else '⏸ выключена'} · {mode}"]
        if not state.auto:
            if state.paused_reason:
                lines.append(f"⚠️ Остановлена: {html.escape(state.paused_reason)}")
            lines.append("Включить: /autotrade on (в тесте можно безопасно посмотреть, что бы она делала)")
        lines.append(f"Условие: цикл ≥{s.auto_min_profit_percent:g}% после комиссий по свежему стакану, "
                     f"до ${s.max_trade_usd:g} за цикл, скан каждые {s.scan_interval_sec:g} с")
        for name in self.creds:
            why = self.trade_block_reason(name) or key_trade_problem(name, self.key_info.get(name))
            title = html.escape(self.scanner.display_name(name))
            if why:
                prefix = "⛔" if self.trade_block_reason(name) else "⚠️ (в тесте не мешает)"
                lines.append(f"{prefix} {title}: {html.escape(why)}")
            else:
                lines.append(f"✅ {title}: может торговать")
        if not st:
            lines.append("\nЕщё не было ни одной проверки после включения. Подожди один скан.")
            return "\n".join(lines)
        for name, value in (st.get("free") or {}).items():
            if value is not None:
                need = self.min_start_usd(name)
                warn = f" ⚠️ мало: для цикла нужно от ${need:.2f}" if value < need else ""
                lines.append(f"💵 {html.escape(self.scanner.display_name(name))}: на торговом счёте {value:.2f} {s.quote}{warn}")
        for name, value in (st.get("funding") or {}).items():
            lines.append(f"📥 {html.escape(self.scanner.display_name(name))}: на счёте Funding ${value:.2f} (ими бот не торгует)")
        ago = max(0, time.time() - st["at"])
        lines.append(f"\n<b>Последняя проверка</b> {ago:.0f} с назад: циклов найдено {st['triangles']}, "
                     f"подходящих {st['candidates']}")
        if st.get("best"):
            b = st["best"]
            where = f" ({html.escape(self.scanner.display_name(b['exchange']))})" if b.get("exchange") else ""
            lines.append(f"Лучший сейчас: {b['net']:+.3f}% {html.escape(b['path'])}{where}")
        for t in st.get("tried") or []:
            icon = "✅" if t["executed"] else "⏭"
            lines.append(f"{icon} {html.escape(t['path'])} на {html.escape(self.scanner.display_name(t['exchange']))} "
                         f"(оценка {t['estimate']:+.3f}%): {html.escape(t['reason'] or 'исполнен')}")
        if st.get("reason") and not st.get("executed"):
            lines.append(f"<b>Почему нет сделки:</b> {html.escape(st['reason'])}")
        a = self.auto_stats
        hours = (time.time() - a["since"]) / 3600
        lines.append(f"\nЗа {hours:.1f} ч: сканов {a['checks']}, проверено стаканами {a['attempts']}, исполнено {a['executed']}"
                     + (f" ({a['pnl']:+.4f}$)" if a["executed"] else ""))
        if a.get("best") and a["checks"] >= 10:
            pb = a["best"]
            lines.append(f"Лучший цикл за это время: {pb['net']:+.3f}% (нужно ≥{s.auto_min_profit_percent:g}%)"
                         + (". Ни разу не дотянул до порога: дело в рынке, а не в боте" if pb["net"] < s.auto_min_profit_percent else ""))
        hint = self._auto_hint()
        if hint:
            lines.append(hint)
        return "\n".join(lines)

    def auto_status_dict(self) -> dict:
        st = dict(self.auto_status or {})
        st["stats"] = dict(self.auto_stats)
        st["blocked_now"] = {n: self.trade_block_reason(n) for n in self.creds if self.trade_block_reason(n)}
        st["key_problems"] = {n: p for n in self.creds if (p := key_trade_problem(n, self.key_info.get(n)))}
        hint = self._auto_hint()
        st["hint"] = re.sub(r"<[^>]+>", "", hint)
        return st

    # --- тексты -------------------------------------------------------------

    def panel_text(self) -> str:
        s, st = self.settings, self.state
        self._roll_day()
        mode = "🔴 <b>РЕАЛЬНЫЙ</b> (ордера уходят на биржу)" if st.live else "🧪 <b>ТЕСТ</b> (всё считается, ордера не отправляются)"
        lines = ["💼 <b>Торговля</b>", f"Режим: {mode}"]
        if not self.creds:
            lines.append("\nКлючи бирж не подключены. Инструкция: /keys")
        else:
            for n in self.creds:
                status = self.key_status.get(n)
                icon = "✅" if status == "ok" else ("❔ не проверен" if status is None else f"❌ {status}")
                lines.append(f"  {html.escape(self.scanner.display_name(n))}: {html.escape(icon)}")
        sc = st.scalp or {}
        held = [p["symbol"].split("/")[0] for p in (sc.get("positions") or {}).values() if p]
        lines.append(f"🎯 Автопилот (режим $5): {'🟢 включён' if sc.get('on') else '⏸ выключен'}"
                     + (f", держу {html.escape(', '.join(held))}" if held else "") + " · /scalp")
        lines.append(
            f"Автоторговля треугольниками: {'🟢 включена' if st.auto else '⏸ выключена'} "
            f"(циклы от {s.auto_min_profit_percent:g}% по стакану)"
        )
        if st.paused_reason and not st.auto:
            lines.append(f"⚠️ Остановлена: {html.escape(st.paused_reason)}")
        if st.auto and self.auto_status:
            a = self.auto_status
            what = "исполнил цикл" if a.get("executed") else (a.get("reason") or "жду подходящий цикл")
            lines.append(f"  Последняя проверка: {html.escape(what)} · подробно /why")
        for n in self.creds:
            why = self.trade_block_reason(n)
            if why and self.key_status.get(n, "ok") == "ok":
                lines.append(f"  ⛔ {html.escape(self.scanner.display_name(n))}: {html.escape(why)}")
        lines.append(
            f"Сегодня: сделок {st.day_trades} из {s.daily_trades:g}, результат циклов {st.day_pnl_usd:+.4f}$ "
            f"(лимит убытка ${s.daily_loss_usd:g})"
        )
        lines.append(f"Лимит на сделку: ${s.max_trade_usd:g} · допустимое ухудшение цены {s.max_slippage_percent:g}%")
        lines.append(
            "\n<code>/buy bybit BTC 5</code> — купить BTC на 5 USDT\n"
            "<code>/sell bybit BTC all</code> — продать (число, all или 50%)\n"
            "<code>/limit bybit buy BTC 5 60000</code> — лимитный ордер\n"
            "Или просто напиши ИИ: «купи эфира на 5 долларов на байбите»"
        )
        return "\n".join(lines)

    def log_text(self, limit: int = 15) -> str:
        if not self.state.log:
            return "Сделок через бота пока не было."
        lines = ["<b>Сделки через бота</b>"]
        for r in reversed(self.state.log[-limit:]):
            when = time.strftime("%d.%m %H:%M", time.gmtime(r["at"] + self.settings.report_utc_offset * 3600))
            mode = "" if r["live"] else "🧪"
            src = {"ai": "🤖", "auto": "⚙️", "scalp": "🎯"}.get(r.get("source"), "")
            icon = "✅" if r.get("ok", True) else "⚠️"
            body = (
                f"{fmt_amount(r['spent'])} {r['spent_ccy']} → {fmt_amount(r['got'])} {r['got_ccy']}"
                if r["kind"] != "limit" else html.escape(r.get("note") or "")
            )
            pnl = f" = {r['pnl_usd']:+.4f}$" if r.get("pnl_usd") is not None else ""
            lines.append(
                f"{when} {icon}{mode}{src} {html.escape(self.scanner.display_name(r['exchange']))} "
                f"{html.escape(r['what'])}: {html.escape(body)}{pnl}"
            )
        lines.append("\n🧪 тест · 🤖 по предложению ИИ · ⚙️ автоторговля · 🎯 режим $5")
        return "\n".join(lines)

    def record_text(self, rec: TradeRecord) -> str:
        name = html.escape(self.scanner.display_name(rec.exchange))
        if not rec.live:
            head = "🧪 <b>Тест выполнен</b> (ордер не отправлялся)"
        elif rec.kind == "limit":
            head = "📌 <b>Лимитный ордер выставлен</b>"
        else:
            head = "✅ <b>Ордер исполнен</b>"
        lines = [f"{head}: {html.escape(rec.what)} на {name}"]
        if rec.kind == "limit" and rec.live:
            lines.append(html.escape(rec.note))
            lines.append("Открытые ордера и отмена: /orders")
        else:
            lines.append(f"Отдал {fmt_amount(rec.spent)} {html.escape(rec.spent_ccy)} → получил {fmt_amount(rec.got)} {html.escape(rec.got_ccy)}")
        if rec.order_id:
            lines.append(f"ID ордера: <code>{html.escape(rec.order_id)}</code>")
        return "\n".join(lines)
