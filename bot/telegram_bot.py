"""Telegram-интерфейс: команды и автоматическая рассылка сигналов."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from . import copy_ui, extras_ui, meme_ui, market_ui, mentors_ui, opps_ui, profit_ui, trading_ui, updater
from .copytrade import CopyTrader
from .memetrade import MemeTrader
from .mentors import Mentors
from .profit import ProfitCenter
from .solkey import looks_like_secret
from .ai import NO_AI_TEXT, AIAssistant, AIError, Answer, Proposal, validate_setting
from .alerts import AlertBook
from .analytics import analysis_text, analyze, fees_text
from .cloud import (BOT_VERSION, STATUS, TelegramBackup, cloud_port, public_url, self_ping_loop, set_status, start_health_server,
                    instance_text, version_text)
from .config import BOUNDS, EDITABLE, RISK_KEYS, Settings
from .demo import DEMO_FILE
from .demo_manager import DemoManager
from .memory import free_memory, freeze_startup, memory_line
from .opportunities import OpportunityHub, for_ai
from .dex import DexClient
from .report import ScanStats, seconds_until, split_message
from .scalper import STEP_SEC as SCALP_STEP_SEC
from .scalper import Scalper
from .scanner import Opportunity, ScanResult, Scanner, fmt_price
from .solana import PhantomWatcher
from .tinvest import TInvestClient
from .trading import Trader, days_left, key_trade_problem, new_auto_stats
from .triangular import TriOpportunity
from .webapp import WebApp

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
SETTINGS_FILE = DATA_DIR / "settings.json"  # изменения из /set, /fee и кнопок (переживают перезапуск)
TRADING_FILE = DATA_DIR / "trading.json"  # режим торговли, автоторговля, журнал сделок (без ключей!)
COPY_FILE = DATA_DIR / "copytrade.json"  # копитрейдинг: кошельки, копии без денег, уроки
EXPLAIN_KEEP = 30  # для скольких последних сообщений с сигналами помнить данные для кнопки «Разбор»
MAX_IN_MESSAGE = 10  # сколько связок показывать в одном сообщении
MAX_TRI_IN_MESSAGE = 5  # сколько треугольников показывать
BACKUP_EVERY_SEC = 30 * 60  # как часто сохранять статистику демо в Telegram
CLEANUP_EVERY_SEC = 10 * 60  # как часто убирать устаревшие записи и возвращать память системе
KEYS_WATCH_SEC = 12 * 3600  # как часто перепроверять ключи бирж (права, срок действия)
AUTO_WARN_SEC = 6 * 3600  # об одной и той же проблеме автоторговли напоминаем не чаще раза в 6 часов
AUTO_FIRST_REPORT_SEC = 3600  # первая сводка автоторговли через час после включения
DUPLICATE_WARN_SEC = 6 * 3600  # о второй копии бота напоминаем не чаще раза в 6 часов


class ConflictWatch(logging.Handler):
    """Замечает, что тем же токеном пользуется вторая копия бота (Telegram: «Conflict: terminated by other getUpdates»).

    Две копии = два бота торгуют одними ключами: двойные покупки. Так бывает, если в Render два сервиса
    с одним TELEGRAM_BOT_TOKEN. Короткий конфликт при обновлении (старая копия ещё не выключилась)
    за дубль не считаем: нужно несколько конфликтов, растянутых больше чем на 3 минуты.
    """

    WINDOW = 15 * 60
    SPAN = 3 * 60

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.hits: list[float] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if "Conflict" in msg or "TelegramConflictError" in msg:
            self.note()

    def note(self, now: float | None = None) -> None:
        self.hits.append(time.time() if now is None else now)
        del self.hits[:-30]

    def active(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        recent = [h for h in self.hits if now - h < self.WINDOW]
        return len(recent) >= 3 and recent[-1] - recent[0] >= self.SPAN

_KEYWORDS = r"(ключ|api|апи|прав|разреш|галоч|торгов)"
WITHDRAW_Q = re.compile(rf"(вывод|withdraw).*{_KEYWORDS}|{_KEYWORDS}.*(вывод|withdraw)", re.I | re.S)
WITHDRAW_ANSWER = """\
🔒 <b>Право «Вывод» боту НЕ нужно</b> — ни для теста, ни для реальной торговли. Не включай его.

Бот покупает и продаёт <b>внутри биржи</b>: деньги никуда не уходят. Для этого хватает двух галочек:
✅ <b>Чтение</b> · ✅ <b>Спот торговля</b>
Всё остальное (Вывод, Universal Transfer, фьючерсы, субаккаунты, P2P) — выключено. Доступ по IP — «Неограниченный».

Почему это важно: ключ хранится на сервере. Если он когда-нибудь утечёт, без права вывода его владелец \
сможет только торговать, а забрать деньги — нет. Поэтому биржи и требуют IP для вывода.

Если бот где-то пишет про ключ, пришли скриншот этого сообщения — разберу. Проверить ключ: /setup или /keys"""

DUPLICATE_TEXT = """\
⚠️ <b>Работают две копии бота</b> с одним токеном.

Скорее всего, в Render два сервиса (например, <code>ai-casher-bot</code> и <code>ai-casher-bot-mvjv</code>). \
Чем это плохо:
• две копии торгуют одними ключами: покупки могут удвоиться, поэтому новые сделки сейчас на паузе;
• Telegram отдаёт сообщения то одной копии, то другой: бот «тупит» и не отвечает;
• бесплатно у Render 750 часов в месяц на всех, а два сервиса тратят 1440. Примерно к середине месяца \
оба выключатся до 1-го числа.

Как проверить: отправь /status несколько раз. Если версия то одна, то другая (например v12 и v16) — \
работает старая копия.

Что сделать:
1) Если в Render два сервиса: открой лишний → Settings → в самом низу <b>Delete Web Service</b>. \
Оставь тот, где в Environment есть ключи бирж.
2) Если лишнего сервиса не видно (он в другом аккаунте Render или на другом хостинге) — отключи старую копию \
сменой токена: @BotFather → /mybots → бот → <b>API Token</b> → <b>Revoke current token</b> → скопируй новый → \
Render → твой сервис → Environment → <code>TELEGRAM_BOT_TOKEN</code> → вставь → Save Changes. \
Старая копия сразу перестанет работать, где бы она ни была. Бот, чат и бэкап остаются те же.
Как только останется одна копия, торговля продолжится сама."""

HELP_TEXT = """\
<b>Арбитражный сканер</b> 🤖

Ищу две вещи:
• <b>межбиржевой арбитраж</b>: где купить дешевле на одной бирже и продать дороже на другой;
• <b>треугольный арбитраж</b>: цикл из трёх обменов внутри одной биржи \
(USDT → A → B → USDT), без переводов между биржами.
Могу и торговать: кнопками, командами, через ИИ или автоматически (нужен API-ключ биржи, /keys).

<b>💡 Заработок с маленькой суммы</b>
/profit — 💰 <b>профит</b>: что реально зарабатывает, план денег
/autopilot — 🤖 <b>бот торгует сам</b>: одна кнопка, дальше без тебя
/earn — как заработать сейчас: акции Bybit (Launchpool, аирдропы), проценты, план на $5
/scalp — состояние автопилота (режим $5) · /lab — лаборатория: проверка стратегий вживую
/copy — 🐋 копитрейдинг кошельков Solana · /meme — 💸 мемкоины (🧪 демо)
/mentors — 🎓 трейдеры · /scout — 🔭 сам ищу их · /train — 🏋️ демо · /news

<b>Сигналы</b>
/scan — просканировать прямо сейчас
/auto_on — присылать новые связки автоматически
/auto_off — выключить автосигналы

<b>Демо-счета</b> (виртуальные деньги, реальные стаканы и комиссии)
/demo_start — запустить демо на $5, $50 и $500 по обеим стратегиям
/demo_start 5 — только на $5
/demo — сводка по всем счетам
/demo 5 — подробно по счетам на $5: сделки и почему связки отклонены
/demo_history — последние сделки
/demo_stop — остановить демо

<b>Торговля</b> 💼 (сначала в тестовом режиме: ордера не отправляются)
/setup — 🚀 <b>с чего начать</b>: проверка ключей, денег и стратегии, следующий шаг кнопкой
/trade — панель: балансы, ордера, режим, автоторговля
/keys — как подключить биржу (ключ без права вывода)
/balance — балансы на биржах
<code>/buy bybit BTC 5</code> — купить BTC на 5 USDT
<code>/sell bybit BTC all</code> — продать (число, all или 50%)
<code>/limit bybit buy BTC 5 60000</code> — лимитный ордер
/orders — открытые ордера и отмена
/live — включить реальные ордера, /test — вернуть тест
/autopilot — 🤖 <b>автопилот</b>: режим $5 + «Монеты в дело», сам выбирает монету и стратегию (<code>/autopilot off</code>)
/scalp — 🎯 <b>режим $5</b>: состояние и проверка на истории (<code>/scalp on</code>/<code>off</code>)
/autotrade on|off — автоторговля треугольниками (нужно от ~$10–20)
/why — 🔍 что автоторговля делает и почему нет сделок
/trades — журнал сделок
/stop — 🛑 стоп-кран: всё выключить и отменить ордера
Под треугольниками есть кнопка «⚡ Исполнить». Каждый ордер исполняется только после твоего подтверждения.

<b>ИИ-помощник и трейдер</b> 🧠
/ai — полный разбор: комиссии, демо, что поменять
/ai вопрос — спросить, например <code>/ai почему на $5 нет сделок?</code>
Можно просто писать вопрос обычным сообщением, а также просить: «купи эфира на 5$ на байбите».
Под сигналами есть кнопка «🤖 Разбор». Советы по настройкам применяются только твоей кнопкой.

<b>Анализ без ИИ</b>
/analysis — комиссии, безубыточность, узкие места, рекомендации
/fees — комиссии бирж; <code>/fee bybit 0.08</code> — задать свою ставку в %

<b>Полезное</b> ✨
/doctor — 🩺 проверить всё: биржи, ключи, сроки ключей, приложение, ИИ
<code>/price TON</code> — цена монеты на Bybit и BingX и разница между ними
<code>/alert TON &lt; 5</code> — сообщить, когда цена дойдёт до уровня (или <code>/alert SOL +5%</code>)
/alerts — мои алерты (удаление кнопками)
<code>/phantom АДРЕС</code> — 👻 баланс кошелька Phantom (только просмотр, сид-фразу не нужно!)
/clean — 🧹 освободить память

<b>Мемкоины и брокер</b> 🦎
<code>/dex BONK</code> или <code>/dex АДРЕС</code> — токен с DexScreener: цена, ликвидность, риски, ссылки GMGN/Axiom
<code>/rug АДРЕС</code> — 🛡 проверка токена Solana на скам (RugCheck)
/hot — тренды Solana · <code>/radar on</code> — присылать новые токены, прошедшие фильтры
/tinvest — 🏦 портфель Т-Инвестиций (токен «только чтение»)

<b>Прочее</b>
/report — отчёт о работе прямо сейчас (он же приходит каждый день)
/status — последний скан и состояние бота
/settings — настройки, /set — изменить, например <code>/set tri_min 0.1</code>
/myid — твой Telegram ID

⚠️ Прибыль не гарантирована: треугольники живут доли секунды, их забирают профессиональные боты. \
Торгуй только деньгами, которые не жалко потерять."""


class SubscriberStore:
    """Хранит список чатов с включёнными автосигналами в data/subscribers.json."""

    def __init__(self, path: Path = SUBSCRIBERS_FILE):
        self.path = path
        self.chats: set[int] = set()
        if path.exists():
            try:
                self.chats = set(json.loads(path.read_text("utf-8")))
            except (ValueError, TypeError):
                log.warning("Не удалось прочитать %s, начинаю с пустого списка", path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.chats)), "utf-8")

    def add(self, chat_id: int) -> None:
        self.chats.add(chat_id)
        self._save()

    def remove(self, chat_id: int) -> None:
        self.chats.discard(chat_id)
        self._save()

    def replace(self, chats: list[int]) -> None:
        self.chats = set(chats)
        self._save()


# ---------------------------------------------------------------------------
# Форматирование сообщений
# ---------------------------------------------------------------------------


def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}k"
    return f"${v:.0f}"


def withdraw_usd(o: Opportunity, scanner: Scanner, fallback_usd: float) -> tuple[float, bool]:
    """Сколько $ стоит перевести монету с биржи покупки. (сумма, True = сообщила биржа)."""
    coins = scanner.withdraw_fee_coins(o.buy.exchange, o.symbol.split("/")[0])
    if coins is None:
        return fallback_usd, False
    return coins * o.buy.ask, True


def format_opportunity(o: Opportunity, scanner: Scanner, wd_fallback_usd: float = 0.5) -> str:
    buy_name = html.escape(scanner.display_name(o.buy.exchange))
    sell_name = html.escape(scanner.display_name(o.sell.exchange))
    lines = [
        f"🟢 <b>{html.escape(o.symbol)}</b>  +{o.net_percent:.2f}% чистыми "
        f"(спред {o.gross_percent:.2f}%)",
        f"   Купить: <b>{buy_name}</b> по {fmt_price(o.buy.ask)}",
        f"   Продать: <b>{sell_name}</b> по {fmt_price(o.sell.bid)}",
        f"   Объём 24ч: {fmt_usd(o.buy.volume_usd)} / {fmt_usd(o.sell.volume_usd)}",
    ]
    if o.depth:
        d = o.depth
        sign = "+" if d.profit_usd >= 0 else "−"
        line = f"   Стакан на {fmt_usd(d.amount_usd)}: {sign}${abs(d.profit_usd):.2f} ({d.profit_percent:+.2f}%)"
        if not d.filled:
            line += " ⚠️ глубины не хватает"
        lines.append(line)
        wd, known = withdraw_usd(o, scanner, wd_fallback_usd)
        net = d.profit_usd - wd
        icon = "✅" if net > 0 else "❌"
        lines.append(
            f"   После вывода монеты (≈${wd:.2f}, {'с биржи' if known else 'оценка'}): "
            f"{icon} {'+' if net >= 0 else '−'}${abs(net):.2f}"
        )
    elif o.depth_error:
        lines.append("   Стакан: не удалось проверить")
    return "\n".join(lines)


def format_triangle(o: TriOpportunity, scanner: Scanner) -> str:
    lines = [
        f"🔺 <b>{html.escape(o.path_text)}</b>  +{o.net_percent:.3f}% чистыми",
        f"   Биржа: <b>{html.escape(scanner.display_name(o.exchange))}</b>",
    ]
    for i, leg in enumerate(o.legs, 1):
        action = "купить" if leg.side == "buy" else "продать"
        lines.append(f"   {i}) {action} {html.escape(leg.symbol)} по {fmt_price(leg.price)}")
    lines.append(f"   Мин. объём пары за 24ч: {fmt_usd(min(leg.volume_usd for leg in o.legs))}")
    if o.depth:
        d = o.depth
        sign = "+" if d.profit_usd >= 0 else "−"
        line = f"   Стакан на {fmt_usd(d.amount_usd)}: {sign}${abs(d.profit_usd):.3f} ({d.profit_percent:+.3f}%)"
        if not d.filled:
            line += " ⚠️ глубины не хватает"
        lines.append(line)
    elif o.depth_error:
        lines.append("   Стакан: не удалось проверить")
    return "\n".join(lines)


def format_scan(result: ScanResult, scanner: Scanner, settings: Settings) -> str:
    parts: list[str] = []
    if result.opportunities:
        parts.append(
            f"<b>Межбиржевые связки: {len(result.opportunities)}</b> (показываю до {MAX_IN_MESSAGE})"
        )
        parts += [
            format_opportunity(o, scanner, settings.demo_withdraw_fee_usd)
            for o in result.opportunities[:MAX_IN_MESSAGE]
        ]
    else:
        parts.append(
            f"Межбиржевых связок от {settings.min_profit_percent:g}% не найдено. "
            "Крупные спреды бывают редко, это нормально."
        )
    if settings.triangular_enabled:
        if result.triangles:
            parts.append(
                f"<b>Треугольники: {len(result.triangles)}</b> (показываю до {MAX_TRI_IN_MESSAGE})"
            )
            parts += [format_triangle(t, scanner) for t in result.triangles[:MAX_TRI_IN_MESSAGE]]
        else:
            parts.append(f"Треугольников от {settings.tri_min_profit_percent:g}% не найдено.")
    parts.append(format_footer(result, scanner))
    return "\n\n".join(parts)


def format_footer(result: ScanResult, scanner: Scanner) -> str:
    ok = ", ".join(scanner.display_name(n) for n in result.exchanges_ok) or "нет"
    text = (
        f"<i>Биржи: {html.escape(ok)}. Сравнено пар: {result.symbols_compared}. "
        f"Время: {result.duration_sec:.1f} с.</i>"
    )
    if result.errors:
        errs = "\n".join(
            f"• {html.escape(scanner.display_name(n))}: {html.escape(e)}" for n, e in result.errors.items()
        )
        text += f"\n<i>Не ответили:</i>\n{errs}"
    return text


def format_settings(settings: Settings, scanner: Scanner) -> str:
    lines = ["<b>Настройки</b>"]
    for key, (attr, desc) in EDITABLE.items():
        lines.append(f"<code>{key}</code> = {float(getattr(settings, attr)):g}  ({desc})")
    exchanges = ", ".join(scanner.display_name(n) for n in scanner.exchanges)
    lines.append(f"\nБиржи: {html.escape(exchanges)}")
    lines.append(f"Валюта: {settings.quote}")
    lines.append(f"Треугольный арбитраж: {'включён' if settings.triangular_enabled else 'выключен'}")
    lines.append("Демо по умолчанию: " + ", ".join(f"${a:g}" for a in settings.demo_amounts))
    lines.append(f"Ежедневный отчёт: {settings.report_hour}:00 (UTC{settings.report_utc_offset:+g})")
    if settings.fee_overrides:
        fees = ", ".join(f"{html.escape(scanner.display_name(k))} {v * 100:g}%" for k, v in settings.fee_overrides.items())
        lines.append(f"Свои комиссии: {fees}")
    lines.append("\nИзменить: <code>/set min_profit 1</code>")
    lines.append("Лимиты торговли (max_trade, daily_loss, daily_trades, auto_min, slippage) меняешь только ты, ИИ их не трогает.")
    lines.append("<i>Изменения сохраняются и переживают перезапуск. Биржи и валюта меняются "
                 "в настройках Render (EXCHANGES, QUOTE).</i>")
    return "\n".join(lines)


def _depth_dict(d) -> dict | None:
    if d is None:
        return None
    return {"amount_usd": d.amount_usd, "profit_usd": round(d.profit_usd, 4),
            "profit_pct": round(d.profit_percent, 4), "filled": d.filled}


def opportunity_dict(o: Opportunity, scanner: Scanner) -> dict:
    base = o.symbol.split("/")[0]
    wd = scanner.withdraw_fee_coins(o.buy.exchange, base)
    return {
        "type": "cross", "symbol": o.symbol, "buy_on": o.buy.exchange, "sell_on": o.sell.exchange,
        "buy_ask": o.buy.ask, "sell_bid": o.sell.bid,
        "spread_pct_before_fees": round(o.gross_percent, 4),
        "net_pct_after_trade_fees": round(o.net_percent, 4),
        "taker_fee_pct": [round(o.buy.taker_fee * 100, 4), round(o.sell.taker_fee * 100, 4)],
        "volume24h_usd": [round(o.buy.volume_usd), round(o.sell.volume_usd)],
        "withdraw_fee_usd": round(wd * o.buy.ask, 4) if wd is not None else "неизвестна",
        "withdraw_open": scanner.can_withdraw(o.buy.exchange, base),
        "deposit_open": scanner.can_deposit(o.sell.exchange, base),
        "order_book_check": _depth_dict(o.depth) or o.depth_error,
    }


def triangle_dict(t: TriOpportunity) -> dict:
    return {
        "type": "triangle", "exchange": t.exchange, "path": t.path_text,
        "legs": [f"{leg.side} {leg.symbol} @ {leg.price:.10g}" for leg in t.legs],
        "pct_before_fees": round(t.gross_percent, 4), "net_pct_after_3_fees": round(t.net_percent, 4),
        "taker_fee_pct": round(t.legs[0].fee * 100, 4),
        "min_volume24h_usd": round(min(leg.volume_usd for leg in t.legs)),
        "order_book_check": _depth_dict(t.depth) or t.depth_error,
    }


def proposals_text(proposals: list[Proposal], settings: Settings) -> str:
    lines = ["💡 <b>Предлагаю изменить</b> (нажми кнопку, чтобы применить):"]
    for p in proposals:
        attr, desc = EDITABLE[p.key]
        why = f": {html.escape(p.reason)}" if p.reason else ""
        lines.append(f"• {desc}: {float(getattr(settings, attr)):g} → <b>{p.value:g}</b>{why}")
    return "\n".join(lines)


def proposals_keyboard(proposals: list[Proposal]) -> InlineKeyboardMarkup | None:
    if not proposals:
        return None
    rows = [
        [InlineKeyboardButton(text=f"✅ {p.key} = {p.value:g}", callback_data=f"set:{p.key}:{p.value:g}")]
        for p in proposals
    ]
    rows.append([InlineKeyboardButton(text="✖️ Ничего не менять", callback_data="dismiss")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _strip_tags(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


# ---------------------------------------------------------------------------
# Сам бот
# ---------------------------------------------------------------------------


class ArbitrageBot:
    def __init__(self, token: str, settings: Settings, allowed_ids: list[int] | set[int]):
        self.settings = settings
        owner = next(iter(allowed_ids), None) if isinstance(allowed_ids, list) else None
        self.allowed_ids = set(allowed_ids)
        self.scanner = Scanner(settings)
        self.subscribers = SubscriberStore()
        self.sent_alerts: dict[tuple[str, str, str], tuple[float, float]] = {}
        self.demo = DemoManager(self.scanner, settings)
        self.stats = ScanStats()
        self.owner_id = owner
        self.ai = AIAssistant.from_env()
        self._explain_items: OrderedDict[str, dict] = OrderedDict()
        self._explain_seq = 0
        self._changed: dict[str, float] = {}  # что меняли через /set и кнопки
        self._fee_changes: dict[str, float | None] = {}  # что меняли через /fee (None = сброс)
        self.alerts = AlertBook()  # ценовые алерты (/alert)
        self.phantom = PhantomWatcher(os.getenv("PHANTOM_ADDRESS", "") or os.getenv("SOLANA_ADDRESS", ""))
        self.dex = DexClient()  # мемкоины: DexScreener + RugCheck (публичные API, без ключей)
        self.tinvest = TInvestClient.from_env()  # Т-Инвестиции: портфель (только чтение, TINVEST_TOKEN)
        self.radar_chats: set[int] = set()  # кому слать радар мемкоинов (/radar on)
        self.radar_seen: dict[str, float] = {}  # о каких токенах радар уже писал
        self.opps = OpportunityHub()  # «Как заработать сейчас»: акции Bybit, Earn, новости (/earn)
        self.opps_notify = True  # писать владельцу о новых акциях Bybit
        self.opps_last: dict | None = None
        self._key_warned: dict[str, float] = {}  # о чём уже предупреждали (чтобы не спамить)
        self._auto_warned: dict[str, float] = {}  # проблемы автоторговли, о которых уже написали
        self._auto_reports = 0  # сколько сводок автоторговли отправлено с момента включения
        self._last_cleanup = time.time()
        self.settings_file = SETTINGS_FILE
        if self.settings_file.exists():
            try:
                self._apply_saved_settings(json.loads(self.settings_file.read_text("utf-8")))
            except (ValueError, TypeError) as e:
                log.warning("Не удалось прочитать %s: %s", self.settings_file, e)

        # Торговля: ключи только из переменных окружения
        self.trader = Trader(self.scanner, settings)
        self.conflicts = ConflictWatch()
        self.scalper = Scalper(self.trader, self.duplicate_running)  # режим $5 (bot/scalper.py)
        self._duplicate_warned = 0.0
        self.trading_file = TRADING_FILE
        self.tri_refs: dict[str, TriOpportunity] = {}  # кнопки «⚡ Исполнить» под сигналами
        self.order_refs: dict[str, tuple[str, str, str]] = {}  # кнопки отмены ордеров
        if self.trading_file.exists():
            try:
                self.trader.restore(json.loads(self.trading_file.read_text("utf-8")))
            except (ValueError, TypeError) as e:
                log.warning("Не удалось прочитать %s: %s", self.trading_file, e)
        self.copy = CopyTrader(self.dex)  # 🐋 копитрейдинг (bot/copytrade.py)
        self.copy_file = COPY_FILE
        self._copy_saved = 0
        if self.copy_file.exists():
            try:
                self.copy.restore(json.loads(self.copy_file.read_text("utf-8")))
            except (ValueError, TypeError) as e:
                log.warning("Не удалось прочитать %s: %s", self.copy_file, e)
        # 💸 реальные сделки с мемкоинами: отдельный кошелёк бота из SOLANA_TRADER_KEY (bot/memetrade.py).
        # Состояние лежит внутри копитрейдинга (copy.st["real"]) — сохраняется и бэкапится вместе с ним.
        self.meme = MemeTrader(self.copy)
        if self.meme.key_error:
            log.warning("%s", self.meme.key_error)
        elif self.meme.configured:
            log.info("Кошелёк бота для мемкоинов: %s", self.meme.address)
        # 🎓 учёба у трейдеров YouTube/Twitch (bot/mentors.py); состояние — в copy.st["mentors"]
        self.mentors = Mentors(self.copy, self.meme, self.ai.client, price_of=self._public_prices,
                               candles_of=lambda coin, since: extras_ui.public_candles(self, coin, since))
        # 💰 Профит-центр: результаты всех способов по фактам, план денег, Страж прибыли (bot/profit.py);
        # состояние — в copy.st["profit"]
        self.profit = ProfitCenter(self)

        self.bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.webapp = WebApp(self, token)  # Telegram Mini App: /app на том же сервере, что и /health
        self.dp = Dispatcher()
        self._register_handlers()

        # Бэкап в Telegram работает, только если известен владелец (ALLOWED_USER_IDS)
        self.backup = TelegramBackup(self.bot, owner) if owner else None
        self._last_backup = 0.0
        self._lab_saved = 0  # сколько бумажных сделок лаборатории уже сохранено

    # --- резервная копия ----------------------------------------------------

    def _state(self) -> dict:
        return {
            "version": 4,
            "subscribers": sorted(self.subscribers.chats),
            "demo": self.demo.export(),
            "settings": self._settings_state(),
            "trading": self.trader.export(),
            "copy": self.copy.export(),
            "bot_version": BOT_VERSION,
        }

    def save_copy(self) -> None:
        try:
            self.copy_file.parent.mkdir(parents=True, exist_ok=True)
            self.copy_file.write_text(json.dumps(self.copy.export(), ensure_ascii=False, separators=(",", ":")), "utf-8")
        except OSError as e:
            log.warning("Не смог сохранить копитрейдинг: %s", e)

    def save_trading(self) -> None:
        try:
            self.trading_file.parent.mkdir(parents=True, exist_ok=True)
            self.trading_file.write_text(json.dumps(self.trader.export(), ensure_ascii=False, separators=(",", ":")), "utf-8")
        except OSError as e:
            log.warning("Не смог сохранить состояние торговли: %s", e)

    # --- сохранение настроек ------------------------------------------------

    def _settings_state(self) -> dict:
        return {"values": dict(self._changed), "fees": dict(self._fee_changes),
                "alerts": self.alerts.export(), "phantom": self.phantom.address,
                "radar": sorted(self.radar_chats), "opps_notify": self.opps_notify}

    def _apply_saved_settings(self, raw: dict) -> None:
        for key, value in (raw.get("values") or {}).items():
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if validate_setting(key, value) is None:
                setattr(self.settings, EDITABLE[key][0], value)
                self._changed[key] = value
        for ex, fee in (raw.get("fees") or {}).items():
            if self.scanner.exchanges and ex not in self.scanner.exchanges:
                continue  # биржу убрали из бота: её комиссия больше не нужна
            self._fee_changes[ex] = fee
            if fee is None:
                self.settings.fee_overrides.pop(ex, None)
            elif 0 <= float(fee) < 0.05:
                self.settings.fee_overrides[ex] = float(fee)
        self.scanner.apply_fee_overrides()
        if "alerts" in raw:
            self.alerts.restore(raw.get("alerts"))
            self.alerts.move_from_removed(list(self.scanner.exchanges))
        if raw.get("phantom") is not None:
            try:
                self.phantom.set_address(raw.get("phantom") or "")
            except Exception:
                pass
        if isinstance(raw.get("opps_notify"), bool):
            self.opps_notify = raw["opps_notify"]
        if isinstance(raw.get("radar"), list):
            self.radar_chats = {int(c) for c in raw["radar"] if str(c).lstrip("-").isdigit()}

    async def save_extras(self) -> None:
        """Алерты и адрес кошелька: в файл и в резервную копию."""
        self._save_settings()
        try:
            await self._backup_now()
        except Exception as e:
            log.warning("Бэкап не удался: %s", e)

    async def _public_prices(self, coins: list[str]) -> dict[str, float]:
        """Цены монет в $ с Bybit/BingX (для проверки прогнозов трейдеров)."""
        return await extras_ui.public_prices(self, coins)

    async def phantom_snapshot(self, force: bool = False) -> dict | None:
        return await self.phantom.get(lambda coins: extras_ui.public_prices(self, coins), force=force,
                                      mint_price_fn=lambda mints: self.dex.token_prices("solana", mints))

    def _save_settings(self) -> None:
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings_file.write_text(json.dumps(self._settings_state()), "utf-8")
        except OSError as e:
            log.warning("Не смог сохранить настройки: %s", e)

    def apply_setting(self, key: str, value: float) -> str | None:
        """Меняет настройку. Возвращает текст ошибки или None."""
        err = validate_setting(key, value)
        if err:
            return err
        setattr(self.settings, EDITABLE[key][0], value)
        self._changed[key] = value
        self._save_settings()
        return None

    def set_fee(self, exchange: str, fee: float | None) -> None:
        if fee is None:
            self.settings.fee_overrides.pop(exchange, None)
        else:
            self.settings.fee_overrides[exchange] = fee
        self._fee_changes[exchange] = fee
        self.scanner.apply_fee_overrides(exchange)
        self._save_settings()

    async def _backup_now(self) -> None:
        if self.backup is None:
            return
        self._last_backup = time.time()
        await self.backup.save(self._state())

    async def _restore_backup(self) -> str | None:
        """Подтягивает состояние из Telegram. Возвращает: "fresh" (бэкапа нет — новая установка),
        "updated" (версия бота сменилась), None (обычный перезапуск или бэкап выключен)."""
        if self.backup is None:
            log.warning("ALLOWED_USER_IDS пустой: резервная копия в Telegram выключена")
            return None
        try:
            state = await self.backup.restore()
        except Exception as e:
            log.warning("Бэкап не прочитан: %s", e)
            return None
        if not state:
            return "fresh"
        result = "updated" if str(state.get("bot_version") or "") != BOT_VERSION else None
        if state.get("settings") and not self.settings_file.exists():
            self._apply_saved_settings(state["settings"])
            self._save_settings()
        if state.get("trading") and not self.trading_file.exists():
            self.trader.restore(state["trading"])
            self.save_trading()
        if state.get("copy") and not self.copy_file.exists():
            self.copy.restore(state["copy"])
            self.save_copy()
        if DEMO_FILE.exists():
            log.info("Бэкап найден, но есть локальные данные. Использую локальные")
            return result
        if state.get("demo"):
            self.demo.restore(state["demo"])
        self.subscribers.replace(state.get("subscribers") or [])
        log.info("Состояние восстановлено из Telegram (демо-счетов: %d)", len(self.demo.accounts))
        return result

    async def _hello(self, kind: str | None) -> None:
        """Первое сообщение владельцу после установки или обновления: что дальше нажать."""
        if self.owner_id is None or kind is None:
            return
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Проверить готовность",
                                                                              callback_data="su:again")]])
        if kind == "fresh":
            text = (f"👋 <b>Бот установлен и работает</b> ({html.escape(version_text())}).\n\n"
                    "Дальше всё по кнопке ниже (или /setup): бот проверит ключи бирж, деньги на торговом счёте "
                    "и стратегию и подскажет следующий шаг.\n"
                    "С суммой от $5 торгует <b>режим $5</b> (/scalp). Сначала всё работает в тесте, "
                    "реальные деньги — только после твоего подтверждения.")
        else:
            text = (f"🆕 <b>Бот обновлён</b> до {html.escape(version_text())}. Настройки и история на месте.\n"
                    "Проверить, всё ли готово к торговле: /setup")
        await self._send(self.owner_id, text, markup)
        await self._backup_now()  # запомнить версию, чтобы не здороваться при каждом перезапуске

    # --- отправка -----------------------------------------------------------

    async def _send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        parts = split_message(text)
        for i, part in enumerate(parts):
            kb = markup if i == len(parts) - 1 else None
            try:
                try:
                    await self.bot.send_message(chat_id, part, reply_markup=kb)
                except TelegramBadRequest:  # сломанная разметка: отправляем как обычный текст
                    await self.bot.send_message(chat_id, _strip_tags(part), reply_markup=kb, parse_mode=None)
            except Exception as e:
                log.warning("Не смог отправить в чат %s: %s", chat_id, e)

    async def _answer(self, message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        parts = split_message(text)
        for i, part in enumerate(parts):
            kb = markup if i == len(parts) - 1 else None
            try:
                await message.answer(part, reply_markup=kb)
            except TelegramBadRequest:
                await message.answer(_strip_tags(part), reply_markup=kb, parse_mode=None)

    # --- ИИ -----------------------------------------------------------------

    def ai_context(self) -> dict:
        """Всё, что ИИ должен знать о текущем положении дел (компактно)."""
        s = self.settings
        a = analyze(self.scanner, s, self.demo, self.stats)
        local = time.gmtime(time.time() + s.report_utc_offset * 3600)
        st = self.stats
        period = {
            "hours": round((time.time() - st.period_start) / 3600, 2),
            "scans": st.scans, "failed_scans": st.failed_scans,
            "unique_cross_signals": len(st.cross_keys), "unique_triangles": len(st.tri_keys),
            "best_cross_by_book_pct": st.best_cross, "best_triangle_by_book_pct": st.best_tri,
            "closest_to_profit_cross": asdict(st.near_cross) if st.near_cross else None,
            "closest_to_profit_triangle": asdict(st.near_tri) if st.near_tri else None,
            "exchange_fail_percent": {
                ex: round(cnt * 100 / st.scans) for ex, cnt in st.exchange_errors.items()
            } if st.scans else {},
        }
        r = self.scanner.last_result
        last = None
        if r is not None:
            last = {
                "minutes_ago": round((time.time() - r.finished_at) / 60, 1),
                "exchanges_ok": r.exchanges_ok, "errors": r.errors,
                "pairs_compared": r.symbols_compared,
                "cross_found": len(r.opportunities), "triangles_found": len(r.triangles),
                "top_cross": [opportunity_dict(o, self.scanner) for o in r.opportunities[:4]],
                "top_triangles": [triangle_dict(t) for t in r.triangles[:4]],
                "closest_cross": asdict(r.near_cross) if r.near_cross else None,
                "closest_triangle": asdict(r.near_tri) if r.near_tri else None,
            }
        return {
            "now_local": time.strftime("%Y-%m-%d %H:%M", local),
            "settings": {
                **{k: float(getattr(s, attr)) for k, (attr, _) in EDITABLE.items()},
                "exchanges": list(self.scanner.exchanges), "quote": s.quote,
                "triangular_enabled": s.triangular_enabled,
            },
            "auto_signals_on": bool(self.subscribers.chats),
            "demo_running": self.demo.active,
            "demo_hours": round((time.time() - self.demo.created_at) / 3600, 1) if self.demo.created_at else 0,
            "analysis": a.to_dict(),
            "period": period,
            "last_scan": last,
            "trading": self._trading_context(),
            "extras": self._extras_context(),
            "earning_opportunities_now": for_ai(self.opps_last),
            "profit_center": self._profit_context(),
        }

    def _extras_context(self) -> dict:
        """Кошелёк Phantom (только просмотр), ценовые алерты и состояние ключей (без секретов)."""
        snap = self.phantom.snapshot
        phantom = None
        if self.phantom.address:
            phantom = {"connected": True, "read_only": True, "error": (snap or {}).get("error"),
                       "total_usd": round((snap or {}).get("total_usd") or 0, 2),
                       "assets": [{"coin": a["coin"], "amount": round(a["amount"], 6),
                                   "usd": round(a["usd"], 2) if a["usd"] is not None else None}
                                  for a in (snap or {}).get("assets", [])[:10]]}
        keys = {}
        for name, info in self.trader.key_info.items():
            days = days_left(info)
            if days is not None:
                expiry = f"истекает через {days:.0f} дн." + (" — скоро, нужен новый ключ" if days <= 14 else " — всё нормально")
            elif name == "bingx":
                expiry = "не истекает, пока бот им пользуется (BingX удаляет ключ только после 14 дней без запросов)"
            else:
                expiry = "биржа срок не сообщила"
            keys[name] = {
                "expiry": expiry,
                # формулировки однозначные: модели неправильно понимали withdraw_enabled=false как «надо включить»
                "withdraw_permission": ("ВКЛЮЧЁН — опасно, скажи создать ключ без вывода" if info.get("withdraw")
                                        else "выключен — ПРАВИЛЬНО, боту вывод не нужен, ничего не советуй"),
                "spot_trade_permission": ("есть" if info.get("spot_trade") else "НЕТ — нужно включить «Спот торговля»")
                if "spot_trade" in info else "неизвестно",
            }
        return {
            "phantom_wallet": phantom or {"connected": False, "how": "/phantom АДРЕС (только публичный адрес)"},
            "price_alerts": [a.symbol_text + f" ({a.exchange})" for a in self.alerts.items[:15]],
            "api_keys": keys,
            "useful_commands": ["/doctor", "/why", "/price COIN", "/alert COIN < PRICE", "/phantom", "/dex", "/rug",
                                "/hot", "/radar", "/tinvest", "/mentors", "/train", "/profit"],
            "tinvest": self._tinvest_context(),
            "memecoin_radar_on": bool(self.radar_chats),
            "copytrading_solana": self._copy_context(),
            "traders_youtube_twitch": self.mentors.ai_context(),
        }

    def _profit_context(self) -> dict:
        try:
            return self.profit.ai_context()
        except Exception as e:  # отчёт для ИИ не должен падать из-за одной части
            log.warning("Профит-центр для ИИ: %s", e)
            return {}

    def _copy_context(self) -> dict:
        """Копитрейдинг для ИИ: коротко, без лишних цифр (ИИ видит итоги, уроки и ошибки)."""
        d = self.copy.status_dict()
        return {"on": d["on"], "how": "/copy: бот копирует сделки кошельков Solana без денег, ⭐ = сигналы с кнопкой Phantom",
                "summary": d["summary"], "filter": d["filter"], "mistakes": d["mistakes"][:4],
                "lessons": [x["text"] for x in d["lessons"][:3]],
                "wallets": [{k: w[k] for k in ("name", "status", "copies", "sum", "grade")} for w in d["wallets"][:8]],
                "real_trading": self._meme_context()}

    def _meme_context(self) -> dict:
        mt = self.meme
        dd = mt.demo_dict()
        lrn = mt.status_dict()["learned"]
        demo = {"on": dd["on"], "equity_usd": dd["equity_usd"], "start_usd": dd["start_usd"],
                "total_pct": dd["total_pct"], "stats": dd["stats"], "verdict": dd["ready"]["text"],
                "positions": [{k: p[k] for k in ("symbol", "usd_in", "pct")} for p in dd["positions"]],
                "learned_rules": {"stop_pct": mt.st["stop_pct"], "tp_pct": mt.st["tp_pct"],
                                  "roundtrip_cost_pct": lrn["cost_pct"], "copy_good_wallets": lrn["good_ok"]}}
        if not mt.configured:
            return {"connected": False, "how": "/meme: отдельный аккаунт «Бот» в Phantom, его приватный ключ — ТОЛЬКО в "
                                               "Render → Environment → SOLANA_TRADER_KEY; никогда не в чат и не ИИ",
                    "error": mt.key_error, "demo_account": demo}
        d = mt.status_dict()
        return {"connected": True, "demo_account": demo, "auto_on": d["on"], "balance_sol": d["balance_sol"], "size_usd": d["size_usd"],
                "scope": d["scope"], "positions": [{k: p[k] for k in ("symbol", "usd_in", "pct", "source")}
                                                   for p in d["positions"]],
                "stats": d["stats"], "today_usd": d["day_pnl_usd"],
                "last_decisions": [x["symbol"] + ": " + x["text"] for x in d["decisions"][:4]]}

    def _tinvest_context(self) -> dict:
        if not self.tinvest.configured:
            return {"connected": False, "how": "/tinvest (токен «только чтение» в Render как TINVEST_TOKEN)"}
        snap = self.tinvest.snapshot or {}
        return {"connected": True, "read_only_view": True, "error": snap.get("error"),
                "total_rub": round(snap.get("total") or 0, 2),
                "accounts": [{"name": a.get("name"), "total": round(a.get("total") or 0, 2),
                              "yield_pct": round(a.get("yield_pct") or 0, 2),
                              "top": [{"ticker": p["ticker"], "value": round(p["value"], 2),
                                       "yield_pct": round(p["yield_pct"], 2) if p.get("yield_pct") is not None else None}
                                      for p in (a.get("positions") or [])[:8]]}
                             for a in snap.get("accounts", [])[:5]]}

    def _auto_context(self) -> dict:
        st = self.trader.auto_status_dict()
        return {k: st.get(k) for k in ("at", "need", "triangles", "candidates", "tried", "reason", "best", "free",
                                       "low_money", "need_usd", "funding", "blocked_now", "key_problems", "hint") if st.get(k) not in (None, {}, [])} | {
            "stats": {k: st["stats"][k] for k in ("checks", "attempts", "executed", "best")}}

    def duplicate_running(self) -> bool:
        """Работает ли вторая копия бота с тем же токеном (по конфликтам Telegram)."""
        return self.conflicts.active()

    def _trading_context(self) -> dict:
        t, s = self.trader, self.settings
        sc = self.scalper.status_dict()
        return {
            "mode": "live" if t.state.live else "test",
            "scalp_mode_5usd": {k: sc[k] for k in ("on", "use_coins", "reasons", "paper", "stats", "positions", "top",
                                                          "lab", "shield")},
            "second_bot_copy_running": self.duplicate_running(),
            "autotrade_on": t.state.auto,
            "autotrade_paused_reason": t.state.paused_reason,
            "autotrade_status": self._auto_context(),
            "exchanges_with_keys": t.working,
            "key_errors": {k: v for k, v in t.key_status.items() if v != "ok"},
            "max_trade_usd": s.max_trade_usd,
            "daily_trades_limit": s.daily_trades,
            "daily_loss_limit_usd": s.daily_loss_usd,
            "autotrade_min_profit_pct": s.auto_min_profit_percent,
            "today": {"trades": t.state.day_trades, "pnl_usd": round(t.state.day_pnl_usd, 4)},
            "balances": t.balances_for_ai(),
            "recent_trades": [
                {k: r.get(k) for k in ("exchange", "kind", "what", "live", "spent", "spent_ccy", "got", "got_ccy", "pnl_usd", "source", "note")}
                for r in t.state.log[-5:]
            ],
        }

    async def _refresh_balances(self) -> None:
        """Перед вопросом ИИ обновляем балансы (если давно не обновляли), чтобы он видел реальные цифры."""
        stale = [n for n in self.trader.working if time.time() - self.trader.balance_cache.get(n, (0, []))[0] > 60]
        if not stale:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*(self.trader.balances(n) for n in stale),
                                                  *(self.trader.other_accounts(n) for n in stale),
                                                  return_exceptions=True), 8)
        except asyncio.TimeoutError:
            pass

    def _remember_explain(self, items: dict) -> list[list[InlineKeyboardButton]]:
        if not self.ai.configured or not items.get("signals"):
            return []
        self._explain_seq += 1
        key = str(self._explain_seq)
        self._explain_items[key] = items
        while len(self._explain_items) > EXPLAIN_KEEP:
            self._explain_items.popitem(last=False)
        return [[InlineKeyboardButton(text="🤖 Разбор ИИ: стоит ли пробовать?", callback_data=f"ai_x:{key}")]]

    def _signal_markup(self, cross: list[Opportunity], tri: list[TriOpportunity],
                       trade_buttons: bool = True) -> InlineKeyboardMarkup | None:
        """Кнопки под сигналами: разбор ИИ и «⚡ Исполнить» для треугольников на биржах с ключами."""
        signals = [opportunity_dict(o, self.scanner) for o in cross[:3]] + [triangle_dict(t) for t in tri[:3]]
        rows = self._remember_explain({"signals": signals})
        if trade_buttons:
            rows += trading_ui.tri_rows(self, tri)
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None

    async def _ai_respond(self, chat_id: int, make_answer, wait: Message | None = None) -> None:
        """Запускает запрос к ИИ и показывает ответ с кнопками-предложениями."""
        if not self.ai.configured:
            await self._send(chat_id, NO_AI_TEXT)
            return
        if wait is None:
            wait = await self.bot.send_message(chat_id, "🤖 Анализирую данные, обычно 5–20 секунд…")
        try:
            await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:
            pass
        await self._refresh_balances()
        try:
            answer: Answer = await make_answer()
        except AIError as e:
            await wait.edit_text(f"🤖 Не получилось: {html.escape(str(e))[:600]}\n\nБез ИИ: /analysis")
            return
        except Exception as e:
            log.exception("Ошибка ИИ")
            await wait.edit_text(f"🤖 Ошибка: {html.escape(type(e).__name__)}. Без ИИ: /analysis")
            return
        text = answer.html
        if answer.proposals:
            text += "\n\n" + proposals_text(answer.proposals, self.settings)
        kb = proposals_keyboard(answer.proposals)
        if answer.orders:
            if self.owner_id is not None and chat_id == self.owner_id:
                orders_text, rows = await trading_ui.plans_from_intents(self, answer.orders)
                text += "\n\n" + orders_text
                if rows:
                    kb = InlineKeyboardMarkup(inline_keyboard=rows + (kb.inline_keyboard if kb else []))
            else:
                text += "\n\n⛔ Ордера через ИИ может подтверждать только владелец бота."
        parts = split_message(text)
        try:
            await wait.edit_text(parts[0], reply_markup=kb if len(parts) == 1 else None)
        except TelegramBadRequest:
            await wait.edit_text(_strip_tags(parts[0])[:4000], reply_markup=kb if len(parts) == 1 else None, parse_mode=None)
        if len(parts) > 1:
            await self._send(chat_id, "\n\n".join(parts[1:]), kb)

    async def _scan(self) -> ScanResult:
        try:
            result = await self.scanner.scan()
        except Exception:
            self.stats.record_failure()
            raise
        self.stats.record(result, self.scanner.display_name)
        return result

    def report_text(self) -> str:
        parts = [
            "📊 <b>Отчёт</b>",
            self.stats.text(len(self.scanner.exchanges), self.settings.trade_amount_usd),
        ]
        if self.demo.accounts:
            parts.append(self.demo.summary_text())
        else:
            parts.append("Демо-счета не запущены: /demo_start")
        if self.trader.creds:
            st = self.trader.state
            self.trader._roll_day()
            parts.append(
                f"💼 Торговля: режим {'🔴 реальный' if st.live else '🧪 тест'}, автоторговля "
                f"{'🟢 вкл' if st.auto else '⏸ выкл'}, сегодня сделок {st.day_trades}, "
                f"результат циклов {st.day_pnl_usd:+.4f}$. Подробно: /trades"
            )
        pilot = self.scalper.report_text()
        if pilot:
            parts.append(pilot)
        copy_line = copy_ui.short_line(self)
        if copy_line:
            parts.append(copy_line)
        mentors_line = self.mentors.short_line()
        if mentors_line:
            parts.append(mentors_line)
        train_line = self.mentors.train.short_line()
        if train_line:
            parts.append(train_line)
        try:
            profit_line = self.profit.short_line()
        except Exception as e:
            log.warning("Профит-центр в отчёте: %s", e)
            profit_line = ""
        if profit_line:
            parts.append(profit_line)
        parts.append("Комиссии, узкие места и рекомендации: /analysis" + (" · мнение ИИ: /ai" if self.ai.configured else ""))
        return "\n\n".join(parts)

    async def _ai_daily_review(self) -> tuple[str, InlineKeyboardMarkup | None]:
        """Мнение ИИ для ежедневного отчёта. Пустая строка, если ИИ нет или он недоступен."""
        if not self.ai.configured:
            return "", None
        try:
            answer = await self.ai.review(self.ai_context(), cache_sec=0)
        except Exception as e:
            log.warning("ИИ в ежедневном отчёте: %s", e)
            return f"🤖 ИИ сейчас недоступен: {html.escape(str(e))[:200]}", None
        text = "🤖 <b>Мнение ИИ</b>\n" + answer.html
        if answer.proposals:
            text += "\n\n" + proposals_text(answer.proposals, self.settings)
        return text, proposals_keyboard(answer.proposals)

    def _report_recipients(self) -> set[int]:
        chats = set()
        if self.owner_id:
            chats.add(self.owner_id)
        if self.demo.chat_id:
            chats.add(self.demo.chat_id)
        return chats

    async def _daily_report_loop(self) -> None:
        while True:
            await asyncio.sleep(seconds_until(self.settings.report_hour, self.settings.report_utc_offset))
            try:
                text = self.report_text()
                ai_text, kb = await self._ai_daily_review()
                for chat_id in self._report_recipients():
                    await self._send(chat_id, text)
                    if ai_text:
                        await self._send(chat_id, ai_text, kb)
                self.stats.reset_period()
            except Exception:
                log.exception("Ошибка ежедневного отчёта")
            await asyncio.sleep(90)

    # --- доступ -------------------------------------------------------------

    def _allowed(self, message: Message) -> bool:
        if not self.allowed_ids:
            return True
        return message.from_user is not None and message.from_user.id in self.allowed_ids

    def _allowed_user(self, user_id: int | None) -> bool:
        return not self.allowed_ids or user_id in self.allowed_ids

    # --- команды ------------------------------------------------------------

    def _register_handlers(self) -> None:
        dp = self.dp

        @dp.message(Command("update", "version"))
        async def cmd_update(message: Message) -> None:
            if not self._allowed(message):
                return
            text, markup = await self.update_text()
            await self._answer(message, text, markup)

        @dp.callback_query(F.data == "up:go")
        async def cb_update(cb: CallbackQuery) -> None:
            if self.owner_id is None or cb.from_user.id != self.owner_id:
                await cb.answer("Только владелец", show_alert=True)
                return
            hook = updater.hook_url()
            if not hook:
                await cb.answer("Нет RENDER_DEPLOY_HOOK_URL: /update", show_alert=True)
                return
            await cb.answer()
            async with aiohttp.ClientSession() as session:
                ok, msg = await updater.trigger(session, hook)
            if ok and updater.UPDATE.get("latest"):
                updater.UPDATE["triggered"][updater.UPDATE["latest"]] = time.time()
            await self._send(cb.message.chat.id, ("✅ " + msg + ". Через 3–5 минут бот перезапустится и напишет «🆕 Бот обновлён»."
                                                  if ok else "❌ " + html.escape(msg)))

        @dp.message(Command("myid"))
        async def cmd_myid(message: Message) -> None:
            uid = message.from_user.id if message.from_user else "?"
            await message.answer(f"Твой Telegram ID: <code>{uid}</code>")

        @dp.message(Command("start", "help"))
        async def cmd_start(message: Message) -> None:
            if not self._allowed(message):
                await message.answer("⛔ Доступ закрыт. Твой ID: /myid")
                return
            rows = list((self.app_markup() or InlineKeyboardMarkup(inline_keyboard=[])).inline_keyboard)
            rows.insert(0, [InlineKeyboardButton(text="💡 Как заработать сейчас (акции, проценты, новости)",
                                                 callback_data="op:show")])
            if message.from_user and message.from_user.id == self.owner_id:
                rows.insert(0, [InlineKeyboardButton(text="🚀 С чего начать: готовность к торговле", callback_data="su:again")])
            await message.answer(HELP_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

        @dp.message(Command("app"))
        async def cmd_app(message: Message) -> None:
            if not self._allowed(message):
                return
            markup = self.app_markup()
            if markup is None:
                await message.answer(
                    "📱 Приложение работает, когда бот запущен в облаке (Render): Telegram открывает "
                    "Mini App только по HTTPS-адресу. На компьютере укажи PUBLIC_URL с https-адресом."
                )
                return
            await message.answer(
                "📱 <b>AI Casher</b>: портфель, графики, рынок, сигналы, демо и ИИ в одном окне.\n"
                "Ещё его можно открыть кнопкой «App» слева от поля ввода.",
                reply_markup=markup,
            )

        @dp.message(Command("scan"))
        async def cmd_scan(message: Message) -> None:
            if not self._allowed(message):
                return
            wait = await message.answer("🔎 Сканирую биржи, обычно это 5–30 секунд…")
            try:
                result = await self._scan()
            except Exception as e:
                log.exception("Ошибка сканирования")
                await wait.edit_text(f"Ошибка сканирования: {html.escape(str(e))[:300]}")
                return
            parts = split_message(format_scan(result, self.scanner, self.settings))
            is_owner = message.from_user is not None and message.from_user.id == self.owner_id
            kb = self._signal_markup(result.opportunities, result.triangles, trade_buttons=is_owner)
            await wait.edit_text(parts[0], reply_markup=kb if len(parts) == 1 else None)
            for i, part in enumerate(parts[1:], 2):
                await message.answer(part, reply_markup=kb if i == len(parts) else None)

        @dp.message(Command("clean", "memory", "gc"))
        async def cmd_clean(message: Message) -> None:
            if not self._allowed(message):
                return
            r = self.cleanup(everything=True)
            freed = r["before"] - r["after"]
            await message.answer(
                "🧹 <b>Память почищена</b>\n"
                f"Было {r['before']:.0f} МБ, стало {r['after']:.0f} МБ"
                + (f" (освобождено {freed:.0f} МБ)" if freed >= 1 else "") + "\n"
                f"Сброшено кэшей и устаревших записей: {r['removed']}\n"
                f"Сейчас: {memory_line()} бесплатного Render.\n\n"
                "Настройки, алерты, демо и журнал сделок не тронуты. Бот и сам убирается каждые 10 минут.\n"
                "Почти всю память (~230 МБ) занимают сами библиотеки (Telegram, биржи), это нормально."
            )

        @dp.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            if not self._allowed(message):
                return
            result = self.scanner.last_result
            if result is None:
                await message.answer(f"Ещё не сканировал. Запусти /scan\nВерсия бота: {version_text()}\n"
                                     f"Где работает: {html.escape(instance_text())}")
                return
            ago = int(time.time() - result.finished_at)
            auto = "включены ✅" if message.chat.id in self.subscribers.chats else "выключены"
            auto += "\nБэкап в Telegram: " + ("включён 💾" if self.backup else "выключен (заполни ALLOWED_USER_IDS)")
            auto += "\nИИ-помощник: " + (
                f"подключён ({html.escape(self.ai.client.endpoints[0].title)}) 🧠" if self.ai.configured else "не подключён (/ai)"
            )
            await message.answer(
                f"Последний скан: {ago} с назад. Межбиржевых связок: {len(result.opportunities)}, "
                f"треугольников: {len(result.triangles)}.\n"
                f"Автосигналы в этом чате: {auto}\nВерсия бота: {version_text()}\n"
                f"Где работает: {html.escape(instance_text())}\n\n" + format_footer(result, self.scanner)
            )

        @dp.message(Command("auto_on"))
        async def cmd_auto_on(message: Message) -> None:
            if not self._allowed(message):
                return
            self.subscribers.add(message.chat.id)
            await self._backup_now()
            await message.answer(
                f"✅ Автосигналы включены. Сканирую каждые {self.settings.scan_interval_sec:g} с "
                f"и пишу о новых связках: межбиржевых от {self.settings.min_profit_percent:g}%"
                + (f", треугольниках от {self.settings.tri_min_profit_percent:g}%" if self.settings.triangular_enabled else "")
                + " (только если прибыль подтверждается стаканом)."
            )

        @dp.message(Command("auto_off"))
        async def cmd_auto_off(message: Message) -> None:
            if not self._allowed(message):
                return
            self.subscribers.remove(message.chat.id)
            await self._backup_now()
            await message.answer("🔕 Автосигналы выключены.")

        @dp.message(Command("settings"))
        async def cmd_settings(message: Message) -> None:
            if not self._allowed(message):
                return
            await message.answer(format_settings(self.settings, self.scanner))

        @dp.message(Command("demo_start"))
        async def cmd_demo_start(message: Message, command: CommandObject) -> None:
            if not self._allowed(message):
                return
            raw = (command.args or "").replace("$", " ").replace(";", " ").split()
            try:
                amounts = [float(x.replace(",", ".")) for x in raw] or list(self.settings.demo_amounts)
            except ValueError:
                await message.answer("Формат: <code>/demo_start</code> или <code>/demo_start 5 50 500</code>")
                return
            amounts = sorted(set(amounts))
            if len(amounts) > 5 or not all(1 <= a <= 1_000_000 for a in amounts):
                await message.answer("До 5 сумм, каждая от 1 до 1 000 000 $")
                return
            labels = self.demo.start(amounts, message.chat.id)
            await self._backup_now()
            await self._answer(
                message,
                "🧪 <b>Демо запущено</b>: " + ", ".join(labels) + "\n\n"
                f"Каждые {self.settings.scan_interval_sec:g} с бот ищет связки и «торгует» виртуальными "
                "деньгами по реальным стаканам. Учитывается всё: комиссии, минимальные ордера, "
                f"плата за вывод и {self.settings.demo_transfer_min:g} мин перевода (межбиржевой), "
                "повторный запрос стаканов перед исполнением (треугольный). Прибыль реинвестируется.\n\n"
                "Сделки придут сюда. Сводка: /demo · каждый день в "
                f"{self.settings.report_hour}:00 придёт отчёт.",
            )

        @dp.message(Command("demo"))
        async def cmd_demo(message: Message, command: CommandObject) -> None:
            if not self._allowed(message):
                return
            arg = (command.args or "").strip().lstrip("$").replace(",", ".")
            if arg:
                try:
                    await self._answer(message, self.demo.details_text(float(arg)))
                except ValueError:
                    await message.answer("Формат: <code>/demo</code> или <code>/demo 5</code>")
                return
            await self._answer(message, self.demo.summary_text())

        @dp.message(Command("demo_history"))
        async def cmd_demo_history(message: Message) -> None:
            if not self._allowed(message):
                return
            await self._answer(message, self.demo.history_text())

        @dp.message(Command("demo_stop"))
        async def cmd_demo_stop(message: Message) -> None:
            if not self._allowed(message):
                return
            self.demo.stop()
            await self._backup_now()
            await self._answer(message, "⏸ Демо остановлено. Итог:\n\n" + self.demo.summary_text())

        @dp.message(Command("report"))
        async def cmd_report(message: Message) -> None:
            if not self._allowed(message):
                return
            await self._answer(message, self.report_text())

        @dp.message(Command("set"))
        async def cmd_set(message: Message, command: CommandObject) -> None:
            if not self._allowed(message):
                return
            args = (command.args or "").split()
            if len(args) != 2 or args[0] not in EDITABLE:
                names = "\n".join(
                    f"<code>{k}</code>: {desc} ({BOUNDS[k][0]:g}…{BOUNDS[k][1]:g})" for k, (_, desc) in EDITABLE.items()
                )
                await message.answer(f"Формат: <code>/set имя значение</code>\n\n{names}")
                return
            try:
                value = float(args[1].replace(",", "."))
            except ValueError:
                await message.answer("Значение должно быть числом, например <code>0.5</code>")
                return
            attr, desc = EDITABLE[args[0]]
            if args[0] in RISK_KEYS and (message.from_user is None or message.from_user.id != self.owner_id):
                await message.answer("⛔ Лимиты торговли меняет только владелец бота.")
                return
            err = self.apply_setting(args[0], value)
            if err:
                await message.answer(f"Нельзя: {desc}, {err}.")
                return
            await self._backup_now()
            await message.answer(f"Готово: {desc} = <b>{value:g}</b> (сохранено)")

        # --- анализ и комиссии ------------------------------------------------

        @dp.message(Command("analysis"))
        async def cmd_analysis(message: Message) -> None:
            if not self._allowed(message):
                return
            a = analyze(self.scanner, self.settings, self.demo, self.stats)
            proposals = [Proposal(x.key, x.value, x.reason) for x in a.suggestions]
            await self._answer(message, analysis_text(a, self.scanner, self.settings), proposals_keyboard(proposals))

        @dp.message(Command("fees"))
        async def cmd_fees(message: Message) -> None:
            if not self._allowed(message):
                return
            a = analyze(self.scanner, self.settings, self.demo, self.stats)
            await self._answer(message, fees_text(a, self.scanner, self.settings))

        @dp.message(Command("fee"))
        async def cmd_fee(message: Message, command: CommandObject) -> None:
            if not self._allowed(message):
                return
            args = (command.args or "").lower().split()
            names = ", ".join(f"<code>{n}</code>" for n in self.scanner.exchanges)
            # можно писать и id ccxt (bingx), и название биржи (BingX)
            aliases = {self.scanner.display_name(n).lower(): n for n in self.scanner.exchanges}
            if args:
                args[0] = aliases.get(args[0], args[0])
            if len(args) != 2 or args[0] not in self.scanner.exchanges:
                await message.answer(
                    "Своя торговая комиссия (taker) биржи в процентах:\n"
                    "<code>/fee bybit 0.08</code> — задать\n<code>/fee bybit reset</code> — вернуть стандартную\n"
                    f"Биржи: {names}\nТекущие ставки: /fees"
                )
                return
            ex = args[0]
            if args[1] in ("reset", "сброс", "default"):
                self.set_fee(ex, None)
                await self._backup_now()
                await message.answer(f"Комиссия {html.escape(self.scanner.display_name(ex))}: стандартная из ccxt.")
                return
            try:
                pct = float(args[1].rstrip("%").replace(",", "."))
            except ValueError:
                await message.answer("Процент должен быть числом, например <code>0.08</code>")
                return
            if not 0 <= pct < 5:
                await message.answer("Комиссия от 0 до 5%. Обычно это 0.05–0.2%.")
                return
            self.set_fee(ex, pct / 100)
            await self._backup_now()
            await message.answer(
                f"Готово: комиссия {html.escape(self.scanner.display_name(ex))} = <b>{pct:g}%</b>. "
                "Учитывается в сигналах, треугольниках и демо (сохранено)."
            )

        # --- ИИ -----------------------------------------------------------------

        @dp.message(Command("ai"))
        async def cmd_ai(message: Message, command: CommandObject) -> None:
            if not self._allowed(message):
                return
            question = (command.args or "").strip()
            chat_id = message.chat.id
            if question:
                await self._ai_respond(chat_id, lambda: self.ai.ask(chat_id, question, self.ai_context()))
            else:
                await self._ai_respond(chat_id, lambda: self.ai.review(self.ai_context()))

        @dp.message(Command("ai_status"))
        async def cmd_ai_status(message: Message) -> None:
            if not self._allowed(message):
                return
            await self._answer(message, self.ai.status_text() if self.ai.configured else NO_AI_TEXT)

        @dp.message(Command("ai_reset"))
        async def cmd_ai_reset(message: Message) -> None:
            if not self._allowed(message):
                return
            self.ai.reset(message.chat.id)
            await message.answer("🧹 ИИ забыл историю этого чата.")

        # --- кнопки -------------------------------------------------------------

        @dp.callback_query(F.data.startswith("set:"))
        async def cb_set(cb: CallbackQuery) -> None:
            if not self._allowed_user(cb.from_user.id):
                await cb.answer("Нет доступа", show_alert=True)
                return
            try:
                _, key, raw = cb.data.split(":", 2)
                value = float(raw)
            except ValueError:
                await cb.answer("Неверная кнопка", show_alert=True)
                return
            if key in RISK_KEYS:  # лимиты риска меняются только командой /set, не кнопкой от ИИ
                await cb.answer("Лимиты риска меняй сам командой /set", show_alert=True)
                return
            old = float(getattr(self.settings, EDITABLE[key][0])) if key in EDITABLE else None
            err = self.apply_setting(key, value)
            if err:
                await cb.answer(f"Не применено: {err}", show_alert=True)
                return
            await self._backup_now()
            await cb.answer("Применено ✅")
            # убираем нажатую кнопку, остальные предложения остаются
            if cb.message is not None and getattr(cb.message, "reply_markup", None):
                rows = [
                    r for r in cb.message.reply_markup.inline_keyboard
                    if not any(b.callback_data == cb.data for b in r)
                ]
                left = [r for r in rows if any((b.callback_data or "").startswith("set:") for b in r)]
                try:
                    await cb.message.edit_reply_markup(
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if left else None
                    )
                except TelegramBadRequest:
                    pass
            chat_id = cb.message.chat.id if cb.message else cb.from_user.id
            await self._send(chat_id, f"✅ {EDITABLE[key][1]}: {old:g} → <b>{value:g}</b> (сохранено)")

        @dp.callback_query(F.data == "dismiss")
        async def cb_dismiss(cb: CallbackQuery) -> None:
            await cb.answer("Оставил как есть")
            if cb.message is not None and self._allowed_user(cb.from_user.id):
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except TelegramBadRequest:
                    pass

        @dp.callback_query(F.data.startswith("ai_x:"))
        async def cb_explain(cb: CallbackQuery) -> None:
            if not self._allowed_user(cb.from_user.id):
                await cb.answer("Нет доступа", show_alert=True)
                return
            items = self._explain_items.get(cb.data.split(":", 1)[1])
            if items is None:
                await cb.answer("Сигнал устарел, запусти /scan заново", show_alert=True)
                return
            await cb.answer("Анализирую…")
            chat_id = cb.message.chat.id if cb.message else cb.from_user.id
            await self._ai_respond(chat_id, lambda: self.ai.explain(items, self.ai_context()))

        # --- торговля и полезные мелочи (до обработчика обычного текста) ----------
        trading_ui.register(self)
        extras_ui.register(self)
        market_ui.register(self)
        opps_ui.register(self)
        copy_ui.register(self)
        meme_ui.register(self)
        mentors_ui.register(self)
        profit_ui.register(self)

        # --- обычный текст = вопрос ИИ (регистрируется последним) ----------------

        @dp.message(F.text)
        async def on_text(message: Message) -> None:
            if not self._allowed(message):
                return
            text = (message.text or "").strip()
            if looks_like_secret(text):  # ключ/сид-фраза в чате: удалить, предупредить, ИИ не отдавать
                await meme_ui.guard_secret(self, message)
                return
            if text.startswith("/"):
                await message.answer("Не знаю такой команды. Список: /help")
                return
            offer = mentors_ui.link_offer(self, text)
            if offer:  # ссылка на YouTube/Twitch: предложить разобрать ролик или следить за каналом
                await message.answer(offer[0], reply_markup=offer[1])
                return
            if WITHDRAW_Q.search(text):  # частый опасный вопрос: отвечаем без ИИ, твёрдо и одинаково
                await self._answer(message, WITHDRAW_ANSWER)
                return
            if not self.ai.configured:
                await message.answer(
                    "Я понимаю команды из /help. Чтобы задавать вопросы обычным текстом, "
                    "подключи бесплатный ИИ: /ai"
                )
                return
            chat_id = message.chat.id
            await self._ai_respond(chat_id, lambda: self.ai.ask(chat_id, text, self.ai_context()))

    # --- автоматический режим ----------------------------------------------

    def _should_alert(self, o: Opportunity | TriOpportunity) -> bool:
        threshold = (
            self.settings.tri_min_profit_percent
            if isinstance(o, TriOpportunity)
            else self.settings.min_profit_percent
        )
        # Сигналим только о том, что подтверждено стаканом
        if o.depth is None or not o.depth.filled or o.depth.profit_percent < threshold:
            return False
        # Межбиржевой: и только если прибыль остаётся после платы за вывод монеты
        if isinstance(o, Opportunity):
            wd, _ = withdraw_usd(o, self.scanner, self.settings.demo_withdraw_fee_usd)
            if o.depth.profit_usd - wd <= 0:
                return False
        prev = self.sent_alerts.get(o.key)
        if prev is None:
            return True
        sent_at, sent_profit = prev
        cooldown_passed = time.time() - sent_at > self.settings.alert_cooldown_min * 60
        grew = o.net_percent - sent_profit >= 1.0  # прибыль выросла на 1 п.п. и больше
        return cooldown_passed or grew

    async def _run_demo(self, result: ScanResult) -> None:
        try:
            events = await self.demo.process(result)
        except Exception:
            log.exception("Ошибка демо-счёта")
            return
        if events or (self.demo.active and time.time() - self._last_backup > BACKUP_EVERY_SEC):
            await self._backup_now()
        chat_id = self.demo.chat_id
        for text in events:
            if chat_id is None:
                log.info(text)
            else:
                await self._send(chat_id, text)

    async def _run_autotrade(self, result: ScanResult) -> None:
        if not self.trader.state.auto or self.owner_id is None:
            return
        if self.duplicate_running():  # вторая копия бота торгует теми же ключами: не удваиваем сделки
            self.trader.auto_status = {**(self.trader.auto_status or {}), "at": time.time(),
                                       "reason": "работают две копии бота, автоторговля на паузе (/doctor)"}
            return
        try:
            executions = await self.trader.auto_step(result)
        except Exception:
            log.exception("Ошибка автоторговли")
            return
        for res in executions:  # пропуски не шлём каждый скан: они в /why и в сводке
            await self._send(self.owner_id, "⚙️ " + res.text(self.scanner.display_name))
        if executions:
            if not self.trader.state.auto and self.trader.state.paused_reason:
                await self._send(self.owner_id, f"⏸ <b>Автоторговля остановлена</b>: {html.escape(self.trader.state.paused_reason)}.\n"
                                                "Включить снова: /autotrade on")
            self.save_trading()
            await self._backup_now()
        await self._auto_notify()

    async def _auto_notify(self, now: float | None = None) -> None:
        """Разовое предупреждение, если автоторговля в принципе не может торговать, и периодическая сводка."""
        t = self.trader
        if not t.state.auto or self.owner_id is None:
            return
        now = time.time() if now is None else now
        problem = t.auto_problem()
        if problem:
            code, text = problem
            if now - self._auto_warned.get(code, 0) > AUTO_WARN_SEC:
                self._auto_warned[code] = now
                hint = t._auto_hint()
                await self._send(self.owner_id, "⚠️ <b>Автоторговля включена, но торговать не может</b>\n"
                                                f"{html.escape(text)}\n{hint}\nПодробно: /why · проверка: /doctor")
        else:
            self._auto_warned.clear()  # проблему исправили: если вернётся, снова предупредим
        hours = self.settings.auto_report_hours
        if hours <= 0:
            return
        period = min(AUTO_FIRST_REPORT_SEC, hours * 3600) if self._auto_reports == 0 else hours * 3600
        if now - t.auto_stats["since"] >= period:
            text = t.auto_digest()
            self._auto_reports += 1
            t.auto_stats = new_auto_stats()
            await self._send(self.owner_id, text + "\nСводку реже/выключить: <code>/set auto_report 24</code> или 0")

    async def _send_alerts(self, result: ScanResult) -> None:
        fresh_cross = [o for o in result.opportunities if self._should_alert(o)][:MAX_IN_MESSAGE]
        fresh_tri = [o for o in result.triangles if self._should_alert(o)][:MAX_TRI_IN_MESSAGE]
        if not (fresh_cross or fresh_tri) or not self.subscribers.chats:
            return
        blocks = ["🔔 <b>Новые связки</b>"]
        blocks += [format_opportunity(o, self.scanner, self.settings.demo_withdraw_fee_usd) for o in fresh_cross]
        blocks += [format_triangle(o, self.scanner) for o in fresh_tri]
        for o in [*fresh_cross, *fresh_tri]:
            self.sent_alerts[o.key] = (time.time(), o.net_percent)
        text = "\n\n".join(blocks)
        for chat_id in list(self.subscribers.chats):
            kb = self._signal_markup(fresh_cross, fresh_tri, trade_buttons=chat_id == self.owner_id)
            await self._send(chat_id, text, kb)

    def cleanup(self, everything: bool = False) -> dict:
        """Уборка памяти: устаревшие записи и кэши, затем сборка мусора.

        Периодически (everything=False) убирает только то, что точно больше не нужно.
        /clean (everything=True) сбрасывает и все кэши: они сами наполнятся при следующем запросе.
        Данные (настройки, алерты, демо, журнал сделок) не трогает.
        """
        now = time.time()
        keep = max(self.settings.alert_cooldown_min * 60, 3600)
        removed = 0
        for k in [k for k, (at, _) in self.sent_alerts.items() if now - at > keep]:
            del self.sent_alerts[k]
            removed += 1
        for d in (self._key_warned, self._auto_warned):
            for k in [k for k, at in d.items() if isinstance(at, (int, float)) and now - at > 7 * 86400]:
                del d[k]
                removed += 1
        removed += self.webapp.trim_caches(everything)
        if everything:
            removed += self.dex.clear_cache()
            removed += len(self._explain_items)
            self._explain_items.clear()
        before, after = free_memory()
        self._last_cleanup = now
        return {"removed": removed, "before": before, "after": after}

    async def _auto_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            if time.time() - self._last_cleanup > CLEANUP_EVERY_SEC:
                try:
                    r = self.cleanup()
                    log.info("Уборка памяти: %.0f → %.0f МБ, убрано записей: %d", r["before"], r["after"], r["removed"])
                except Exception:
                    log.exception("Ошибка уборки памяти")
            if self.subscribers.chats or self.demo.active or self.trader.state.auto:
                try:
                    result = await self._scan()
                    await self._run_autotrade(result)  # первым: каждая секунда задержки съедает прибыль
                    await self._run_demo(result)
                    await self._send_alerts(result)
                except Exception:
                    log.exception("Ошибка в автосканировании")
            await asyncio.sleep(self.settings.scan_interval_sec)

    async def _warn_duplicate(self) -> None:
        if not self.duplicate_running() or self.owner_id is None:
            return
        if time.time() - self._duplicate_warned < DUPLICATE_WARN_SEC:
            return
        self._duplicate_warned = time.time()
        await self._send(self.owner_id, DUPLICATE_TEXT)

    async def _scalp_loop(self) -> None:
        """Режим $5: каждые 30 секунд следит за позицией или ищет вход."""
        await asyncio.sleep(10)
        while True:
            try:
                await self._warn_duplicate()
                await self._scalp_tick()
            except Exception:
                log.exception("Ошибка режима $5")
            await asyncio.sleep(SCALP_STEP_SEC)

    async def _scalp_tick(self) -> None:
        """Один шаг автопилота: торговля (если включён) или обучение лаборатории (если выключен)."""
        events = []
        if self.scalper.on and self.owner_id is not None:
            events = await self.scalper.step()
            for text in events:
                await self._send(self.owner_id, text)
        elif self.owner_id is not None and self.trader.working:
            await self.scalper.learn()
        if events:
            self.save_trading()
            await self._backup_now()
            self._lab_saved = self.scalper.lab_changes
        elif (self.scalper.lab_changes != self._lab_saved
              and time.time() - self._last_backup > BACKUP_EVERY_SEC):
            # результаты лаборатории — это опыт бота: на Render файлы стираются при перезапуске,
            # поэтому не реже раза в полчаса отправляем их в бэкап в Telegram
            self._lab_saved = self.scalper.lab_changes
            self.save_trading()
            await self._backup_now()

    # --- приложение ---------------------------------------------------------

    @staticmethod
    def app_url() -> str | None:
        url = public_url()
        return f"{url.rstrip('/')}/app" if url and url.startswith("https://") else None

    def app_markup(self) -> InlineKeyboardMarkup | None:
        url = self.app_url()
        if not url:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Открыть приложение", web_app=WebAppInfo(url=url))]])

    async def _equity_loop(self) -> None:
        """Раз в 15 минут записывает стоимость портфеля, чтобы в приложении был график баланса."""
        await asyncio.sleep(20)
        while True:
            if self.trader.working:
                try:
                    await self.trader.portfolio()
                    self.save_trading()
                except Exception:
                    log.exception("Не удалось записать стоимость портфеля")
            await asyncio.sleep(15 * 60)

    # --- запуск -------------------------------------------------------------

    async def _check_keys_on_start(self) -> None:
        status = await self.trader.check_keys()
        try:
            await self.trader.inspect_keys()  # права ключа: «только чтение» не даст автоторговле торговать
        except Exception:
            log.exception("Не удалось прочитать права ключей")
        bad = {k: v for k, v in status.items() if v != "ok"}
        if self.trader.state.live:
            for name, info in self.trader.key_info.items():
                problem = key_trade_problem(name, info)
                if problem and name not in bad:
                    bad[name] = problem
        log.info("Ключи бирж: %s", {k: ("ok" if v == "ok" else "ошибка") for k, v in status.items()})
        if bad and self.owner_id:
            lines = ["⚠️ <b>Проблема с ключами бирж</b>"]
            lines += [f"{html.escape(self.scanner.display_name(k))}: {html.escape(v)}" for k, v in bad.items()]
            if self.trader.state.auto:
                lines.append("Автоторговля на этих биржах не работает, пока ключ не исправлен.")
            await self._send(self.owner_id, "\n".join(lines))

    async def _keys_watch_loop(self) -> None:
        """Раз в 12 часов перепроверяет ключи: не сломался ли, не истекает ли (Bybit), нет ли права вывода."""
        await asyncio.sleep(60)
        while True:
            try:
                before = dict(self.trader.key_status)
                status = await self.trader.check_keys()
                await self.trader.inspect_keys()
                notes = []
                for name, st in status.items():
                    if st != "ok" and before.get(name, "ok") == "ok":
                        notes.append(f"{self.scanner.display_name(name)}: ключ перестал работать: {st}")
                    elif st == "ok" and before.get(name, "ok") != "ok":
                        notes.append(f"{self.scanner.display_name(name)}: ключ снова работает ✅")
                for w in self.trader.key_warnings():
                    if time.time() - self._key_warned.get(w, 0) > 3 * 86400:  # не чаще раза в 3 дня
                        self._key_warned[w] = time.time()
                        notes.append(w)
                if notes and self.owner_id:
                    lines = ["🔑 <b>Ключи бирж</b>"] + [f"• {html.escape(n)}" for n in notes]
                    await self._send(self.owner_id, "\n".join(lines + ["Подробно: /doctor"]))
            except Exception:
                log.exception("Ошибка проверки ключей")
            await asyncio.sleep(KEYS_WATCH_SEC)

    async def update_text(self) -> tuple[str, InlineKeyboardMarkup | None]:
        """/update: какая версия стоит, какая последняя и как обновиться."""
        async with aiohttp.ClientSession() as session:
            latest = await updater.check(session)
        lines = [f"⬆️ <b>Обновления</b>\nСейчас работает: <b>{html.escape(version_text())}</b>"]
        if latest is None:
            lines.append(f"Последнюю версию узнать не получилось ({html.escape(updater.UPDATE.get('error') or 'нет связи')}).")
        else:
            lines.append(f"Последняя версия в репозитории: <b>v{latest}</b>")
        problem = updater.hook_problem()
        if problem:
            lines.append("⚠️ " + html.escape(problem))
        markup = None
        if updater.is_newer(latest):
            if updater.hook_url():
                lines.append("Нажми кнопку — Render поставит новую версию за 3–5 минут. Настройки и история сохранятся.")
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text=f"🔄 Обновить до v{latest}", callback_data="up:go")]])
            else:
                lines += ["", updater.MANUAL_STEPS, "", updater.HOOK_STEPS]
        elif latest is not None:
            lines.append("✅ Стоит последняя версия." + ("" if updater.hook_url() else "\n\n" + updater.HOOK_STEPS))
        STATUS["update"] = updater.status_line()
        return "\n".join(lines), markup

    async def _update_loop(self) -> None:
        """Раз в 30 минут: есть ли новая версия. Есть deploy hook — обновляемся сами, нет — говорим, что нажать."""
        await asyncio.sleep(90)
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    latest = await updater.check(session)
                    STATUS["update"] = updater.status_line()
                    if updater.is_newer(latest):
                        await self._on_new_version(session, latest)
                except Exception:
                    log.exception("Ошибка проверки обновлений")
                await asyncio.sleep(updater.CHECK_SEC)

    async def _on_new_version(self, session, latest: str) -> None:
        hook = updater.hook_url()
        if hook and updater.should_trigger(latest):
            updater.UPDATE["triggered"][latest] = time.time()
            ok, msg = await updater.trigger(session, hook)
            log.info("Самообновление до v%s: %s", latest, msg)
            if self.owner_id:
                await self._send(self.owner_id, (f"⬆️ Нашёл новую версию <b>v{latest}</b> (сейчас v{BOT_VERSION}). "
                                                 + ("Обновляюсь сам, это 3–5 минут. Сделки и настройки сохранятся."
                                                    if ok else f"Сам обновиться не смог: {html.escape(msg)}.\n\n"
                                                    + updater.MANUAL_STEPS)))
        elif not hook and updater.should_notify(latest) and self.owner_id:
            updater.UPDATE["notified"][latest] = time.time()
            await self._send(self.owner_id, (f"⬆️ <b>Вышла новая версия бота v{latest}</b>, а работает v{BOT_VERSION}: "
                                             "Render сам не обновляет этот сервис.\n\n"
                                             + updater.MANUAL_STEPS + "\n\n" + updater.HOOK_STEPS))

    async def _get_me_with_retry(self, tries: int = 8):
        """Связь с Telegram при старте иногда пропадает на секунды: не падаем сразу, пробуем ещё."""
        from aiogram.exceptions import TelegramNetworkError, TelegramServerError

        for attempt in range(tries):
            try:
                return await self.bot.get_me()
            except (TelegramNetworkError, TelegramServerError) as e:
                if attempt == tries - 1:
                    raise
                set_status("starting", "Нет связи с Telegram, пробую снова…")
                log.warning("Нет связи с Telegram (%s), попытка %d из %d", e, attempt + 1, tries)
                await asyncio.sleep(min(5 * (attempt + 1), 30))

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        runner = None
        aiogram_log = logging.getLogger("aiogram.dispatcher")
        aiogram_log.addHandler(self.conflicts)
        port = cloud_port() or int(os.getenv("WEBAPP_PORT", "0") or 0)
        if port:  # запущены в облаке: сразу открываем порт, чтобы хостинг видел, что сервис жив
            runner = await start_health_server(port, self.webapp.setup)
            if public_url():
                tasks.append(asyncio.create_task(self_ping_loop(public_url())))

        try:
            me = await self._get_me_with_retry()
            set_status("running", bot=me.username)
            await self.bot.set_my_commands(
                [
                    BotCommand(command="scan", description="Просканировать сейчас"),
                    BotCommand(command="auto_on", description="Включить автосигналы"),
                    BotCommand(command="auto_off", description="Выключить автосигналы"),
                    BotCommand(command="demo_start", description="Демо на $5, $50, $500"),
                    BotCommand(command="demo", description="Демо: сводка по счетам"),
                    BotCommand(command="demo_history", description="Демо: последние сделки"),
                    BotCommand(command="demo_stop", description="Остановить демо"),
                    BotCommand(command="app", description="📱 Приложение: графики и торговля"),
                    BotCommand(command="trade", description="💼 Торговля: панель"),
                    BotCommand(command="balance", description="Балансы на биржах"),
                    BotCommand(command="orders", description="Открытые ордера"),
                    BotCommand(command="stop", description="🛑 Остановить торговлю"),
                    BotCommand(command="ai", description="🧠 ИИ: разбор и вопросы"),
                    BotCommand(command="doctor", description="🩺 Проверить всё"),
                    BotCommand(command="clean", description="🧹 Освободить память"),
                    BotCommand(command="price", description="💲 Цена на Bybit и BingX"),
                    BotCommand(command="alert", description="🔔 Ценовой алерт"),
                    BotCommand(command="alerts", description="Мои алерты"),
                    BotCommand(command="phantom", description="👻 Кошелёк Phantom (просмотр)"),
                    BotCommand(command="setup", description="🚀 Готовность к торговле: что осталось"),
                    BotCommand(command="profit", description="💰 Профит: что реально зарабатывает, план денег"),
                    BotCommand(command="autopilot", description="🤖 Автопилот: бот торгует сам"),
                    BotCommand(command="scalp", description="🎯 Режим $5: состояние и сделки"),
                    BotCommand(command="lab", description="🧪 Лаборатория: стратегии вживую"),
                    BotCommand(command="copy", description="🐋 Копитрейдинг: умные кошельки"),
                    BotCommand(command="meme", description="💸 Мемкоины: бот торгует сам"),
                    BotCommand(command="mentors", description="🎓 Учусь у трейдеров YouTube и Twitch"),
                    BotCommand(command="train", description="🏋️ Тренировка: сам торгую выученным на демо"),
                    BotCommand(command="scout", description="🔭 Разведчик: сам ищу прибыльных трейдеров"),
                    BotCommand(command="earn", description="💡 Как заработать сейчас: акции, проценты"),
                    BotCommand(command="news", description="📰 Новости крипты"),
                    BotCommand(command="why", description="🔍 Автоторговля: почему нет сделок"),
                    BotCommand(command="dex", description="🦎 Мемкоин: поиск и проверка"),
                    BotCommand(command="hot", description="🔥 Тренды Solana (DexScreener)"),
                    BotCommand(command="rug", description="🛡 Проверка токена на скам"),
                    BotCommand(command="radar", description="📡 Радар новых мемкоинов"),
                    BotCommand(command="tinvest", description="🏦 Т-Инвестиции: портфель"),
                    BotCommand(command="analysis", description="Анализ: комиссии и узкие места"),
                    BotCommand(command="fees", description="Комиссии бирж"),
                    BotCommand(command="report", description="Отчёт о работе"),
                    BotCommand(command="status", description="Последний скан"),
                    BotCommand(command="settings", description="Настройки"),
                    BotCommand(command="update", description="⬆️ Версия и обновление"),
                    BotCommand(command="help", description="Помощь"),
                ]
            )
            log.info("Бот @%s запущен. Открой его в Telegram и нажми Start.", me.username)
            if not self.allowed_ids:
                log.warning("ALLOWED_USER_IDS пустой: ботом может пользоваться кто угодно")

            hello = await self._restore_backup()
            tasks.append(asyncio.create_task(self._hello(hello)))
            if self.trader.creds:
                tasks.append(asyncio.create_task(self._check_keys_on_start()))
                tasks.append(asyncio.create_task(self._keys_watch_loop()))
            tasks.append(asyncio.create_task(extras_ui.alerts_loop(self)))
            tasks.append(asyncio.create_task(market_ui.radar_loop(self)))
            tasks.append(asyncio.create_task(opps_ui.opps_loop(self)))
            tasks.append(asyncio.create_task(copy_ui.copy_loop(self)))
            tasks.append(asyncio.create_task(mentors_ui.mentors_loop(self)))
            tasks.append(asyncio.create_task(profit_ui.profit_loop(self)))
            tasks.append(asyncio.create_task(self._auto_loop()))
            tasks.append(asyncio.create_task(self._scalp_loop()))
            tasks.append(asyncio.create_task(self._equity_loop()))
            if self.app_url():
                try:
                    await self.bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(text="App", web_app=WebAppInfo(url=self.app_url()))
                    )
                    log.info("Mini App: %s", self.app_url())
                except Exception as e:
                    log.warning("Не удалось поставить кнопку приложения: %s", e)
            tasks.append(asyncio.create_task(self._daily_report_loop()))
            if cloud_port() or os.getenv("RENDER"):
                tasks.append(asyncio.create_task(self._update_loop()))
            log.info("Память после запуска: %s · заморожено объектов старта: %d", memory_line(), freeze_startup())
            await self.dp.start_polling(self.bot)
        finally:
            aiogram_log.removeHandler(self.conflicts)
            for t in tasks:
                t.cancel()
            if runner:
                await runner.cleanup()
            await self.scanner.close()
            await self.opps.close()
            await self.ai.client.close()
            await self.phantom.close()
            await self.dex.close()
            await self.copy.rpc.close()
            await self.meme.jup.close()
            await self.mentors.close()
            await self.tinvest.close()
            await self.bot.session.close()
