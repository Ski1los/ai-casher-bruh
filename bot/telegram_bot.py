"""Telegram-интерфейс: команды и автоматическая рассылка сигналов."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .ai import NO_AI_TEXT, AIAssistant, AIError, Answer, Proposal, validate_setting
from .analytics import analysis_text, analyze, fees_text
from .cloud import TelegramBackup, cloud_port, public_url, self_ping_loop, start_health_server
from .config import BOUNDS, EDITABLE, Settings
from .demo import DEMO_FILE
from .demo_manager import DemoManager
from .report import ScanStats, seconds_until, split_message
from .scanner import Opportunity, ScanResult, Scanner, fmt_price
from .triangular import TriOpportunity

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
SETTINGS_FILE = DATA_DIR / "settings.json"  # изменения из /set, /fee и кнопок (переживают перезапуск)
EXPLAIN_KEEP = 30  # для скольких последних сообщений с сигналами помнить данные для кнопки «Разбор»
MAX_IN_MESSAGE = 10  # сколько связок показывать в одном сообщении
MAX_TRI_IN_MESSAGE = 5  # сколько треугольников показывать
BACKUP_EVERY_SEC = 30 * 60  # как часто сохранять статистику демо в Telegram

HELP_TEXT = """\
<b>Арбитражный сканер</b> 🤖

Ищу две вещи:
• <b>межбиржевой арбитраж</b>: где купить дешевле на одной бирже и продать дороже на другой;
• <b>треугольный арбитраж</b>: цикл из трёх обменов внутри одной биржи \
(USDT → A → B → USDT), без переводов между биржами.
Сам я реальными деньгами не торгую, только сигналы и демо-счета.

<b>Сигналы</b>
/scan — просканировать прямо сейчас
/auto_on — присылать новые связки автоматически
/auto_off — выключить автосигналы

<b>Демо-счета</b> (виртуальные деньги, реальные стаканы и комиссии)
/demo_start — запустить демо на $5, $50 и $500 по обеим стратегиям
/demo_start 5 — только на $5 (можно перечислить суммы: <code>/demo_start 5 20</code>)
/demo — сводка по всем счетам
/demo 5 — подробно по счетам на $5: сделки и почему связки отклонены
/demo_history — последние сделки
/demo_stop — остановить демо

<b>ИИ-помощник</b> 🧠
/ai — полный разбор: комиссии, демо, что поменять
/ai вопрос — спросить, например <code>/ai почему на $5 нет сделок?</code>
Можно просто писать вопрос обычным сообщением.
Под сигналами есть кнопка «🤖 Разбор». Советы по настройкам применяются только твоей кнопкой.

<b>Анализ без ИИ</b>
/analysis — комиссии, безубыточность, узкие места, рекомендации
/fees — комиссии бирж; <code>/fee bybit 0.08</code> — задать свою ставку в %

<b>Прочее</b>
/report — отчёт о работе прямо сейчас (он же приходит каждый день)
/status — последний скан и состояние бота
/settings — настройки, /set — изменить, например <code>/set tri_min 0.1</code>
/myid — твой Telegram ID

⚠️ Перед реальной сделкой проверь вручную, что ввод/вывод монеты открыт, \
и помни: пока ты вручную делаешь три обмена, треугольник обычно уже исчез."""


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
        self.settings_file = SETTINGS_FILE
        if self.settings_file.exists():
            try:
                self._apply_saved_settings(json.loads(self.settings_file.read_text("utf-8")))
            except (ValueError, TypeError) as e:
                log.warning("Не удалось прочитать %s: %s", self.settings_file, e)

        self.bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self._register_handlers()

        # Бэкап в Telegram работает, только если известен владелец (ALLOWED_USER_IDS)
        self.backup = TelegramBackup(self.bot, owner) if owner else None
        self._last_backup = 0.0

    # --- резервная копия ----------------------------------------------------

    def _state(self) -> dict:
        return {
            "version": 3,
            "subscribers": sorted(self.subscribers.chats),
            "demo": self.demo.export(),
            "settings": self._settings_state(),
        }

    # --- сохранение настроек ------------------------------------------------

    def _settings_state(self) -> dict:
        return {"values": dict(self._changed), "fees": dict(self._fee_changes)}

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
            self._fee_changes[ex] = fee
            if fee is None:
                self.settings.fee_overrides.pop(ex, None)
            elif 0 <= float(fee) < 0.05:
                self.settings.fee_overrides[ex] = float(fee)
        self.scanner.apply_fee_overrides()

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

    async def _restore_backup(self) -> None:
        if self.backup is None:
            log.warning("ALLOWED_USER_IDS пустой: резервная копия в Telegram выключена")
            return
        state = await self.backup.restore()
        if not state:
            return
        if state.get("settings") and not self.settings_file.exists():
            self._apply_saved_settings(state["settings"])
            self._save_settings()
        if DEMO_FILE.exists():
            log.info("Бэкап найден, но есть локальные данные. Использую локальные")
            return
        if state.get("demo"):
            self.demo.restore(state["demo"])
        self.subscribers.replace(state.get("subscribers") or [])
        log.info("Состояние восстановлено из Telegram (демо-счетов: %d)", len(self.demo.accounts))

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
        }

    def _remember_explain(self, items: dict) -> InlineKeyboardMarkup | None:
        if not self.ai.configured or not items.get("signals"):
            return None
        self._explain_seq += 1
        key = str(self._explain_seq)
        self._explain_items[key] = items
        while len(self._explain_items) > EXPLAIN_KEEP:
            self._explain_items.popitem(last=False)
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🤖 Разбор ИИ: стоит ли пробовать?", callback_data=f"ai_x:{key}")]]
        )

    def _explain_markup(self, cross: list[Opportunity], tri: list[TriOpportunity]) -> InlineKeyboardMarkup | None:
        signals = [opportunity_dict(o, self.scanner) for o in cross[:3]] + [triangle_dict(t) for t in tri[:3]]
        return self._remember_explain({"signals": signals})

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

        @dp.message(Command("myid"))
        async def cmd_myid(message: Message) -> None:
            uid = message.from_user.id if message.from_user else "?"
            await message.answer(f"Твой Telegram ID: <code>{uid}</code>")

        @dp.message(Command("start", "help"))
        async def cmd_start(message: Message) -> None:
            if not self._allowed(message):
                await message.answer("⛔ Доступ закрыт. Твой ID: /myid")
                return
            await message.answer(HELP_TEXT)

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
            kb = self._explain_markup(result.opportunities, result.triangles)
            await wait.edit_text(parts[0], reply_markup=kb if len(parts) == 1 else None)
            for i, part in enumerate(parts[1:], 2):
                await message.answer(part, reply_markup=kb if i == len(parts) else None)

        @dp.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            if not self._allowed(message):
                return
            result = self.scanner.last_result
            if result is None:
                await message.answer("Ещё не сканировал. Запусти /scan")
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
                f"Автосигналы в этом чате: {auto}\n\n" + format_footer(result, self.scanner)
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
            # можно писать и id ccxt (gate), и название биржи (Gate.io)
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

        # --- обычный текст = вопрос ИИ (регистрируется последним) ----------------

        @dp.message(F.text)
        async def on_text(message: Message) -> None:
            if not self._allowed(message):
                return
            text = (message.text or "").strip()
            if text.startswith("/"):
                await message.answer("Не знаю такой команды. Список: /help")
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
        kb = self._explain_markup(fresh_cross, fresh_tri)
        for chat_id in list(self.subscribers.chats):
            await self._send(chat_id, text, kb)

    async def _auto_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            if self.subscribers.chats or self.demo.active:
                try:
                    result = await self._scan()
                    await self._run_demo(result)
                    await self._send_alerts(result)
                except Exception:
                    log.exception("Ошибка в автосканировании")
            await asyncio.sleep(self.settings.scan_interval_sec)

    # --- запуск -------------------------------------------------------------

    async def run(self) -> None:
        tasks: list[asyncio.Task] = []
        runner = None
        port = cloud_port()
        if port:  # запущены в облаке: сразу открываем порт, чтобы хостинг видел, что сервис жив
            runner = await start_health_server(port)
            if public_url():
                tasks.append(asyncio.create_task(self_ping_loop(public_url())))

        try:
            me = await self.bot.get_me()
            await self.bot.set_my_commands(
                [
                    BotCommand(command="scan", description="Просканировать сейчас"),
                    BotCommand(command="auto_on", description="Включить автосигналы"),
                    BotCommand(command="auto_off", description="Выключить автосигналы"),
                    BotCommand(command="demo_start", description="Демо на $5, $50, $500"),
                    BotCommand(command="demo", description="Демо: сводка по счетам"),
                    BotCommand(command="demo_history", description="Демо: последние сделки"),
                    BotCommand(command="demo_stop", description="Остановить демо"),
                    BotCommand(command="ai", description="🧠 ИИ: разбор и вопросы"),
                    BotCommand(command="analysis", description="Анализ: комиссии и узкие места"),
                    BotCommand(command="fees", description="Комиссии бирж"),
                    BotCommand(command="report", description="Отчёт о работе"),
                    BotCommand(command="status", description="Последний скан"),
                    BotCommand(command="settings", description="Настройки"),
                    BotCommand(command="help", description="Помощь"),
                ]
            )
            log.info("Бот @%s запущен. Открой его в Telegram и нажми Start.", me.username)
            if not self.allowed_ids:
                log.warning("ALLOWED_USER_IDS пустой: ботом может пользоваться кто угодно")

            await self._restore_backup()
            tasks.append(asyncio.create_task(self._auto_loop()))
            tasks.append(asyncio.create_task(self._daily_report_loop()))
            await self.dp.start_polling(self.bot)
        finally:
            for t in tasks:
                t.cancel()
            if runner:
                await runner.cleanup()
            await self.scanner.close()
            await self.ai.client.close()
            await self.bot.session.close()
