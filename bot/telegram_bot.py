"""Telegram-интерфейс: команды и автоматическая рассылка сигналов."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, Message

from .cloud import TelegramBackup, cloud_port, public_url, self_ping_loop, start_health_server
from .config import EDITABLE, Settings
from .demo import DEMO_FILE
from .demo_manager import DemoManager
from .report import ScanStats, seconds_until, split_message
from .scanner import Opportunity, ScanResult, Scanner, fmt_price
from .triangular import TriOpportunity

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
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


def format_opportunity(o: Opportunity, scanner: Scanner) -> str:
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
        parts += [format_opportunity(o, scanner) for o in result.opportunities[:MAX_IN_MESSAGE]]
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
    lines.append("\nИзменить: <code>/set min_profit 1</code>")
    lines.append("<i>Биржи и валюта меняются в файле .env (нужен перезапуск).</i>")
    return "\n".join(lines)


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

        self.bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self._register_handlers()

        # Бэкап в Telegram работает, только если известен владелец (ALLOWED_USER_IDS)
        self.backup = TelegramBackup(self.bot, owner) if owner else None
        self._last_backup = 0.0

    # --- резервная копия ----------------------------------------------------

    def _state(self) -> dict:
        return {"version": 2, "subscribers": sorted(self.subscribers.chats), "demo": self.demo.export()}

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
        if DEMO_FILE.exists():
            log.info("Бэкап найден, но есть локальные данные. Использую локальные")
            return
        if state.get("demo"):
            self.demo.restore(state["demo"])
        self.subscribers.replace(state.get("subscribers") or [])
        log.info("Состояние восстановлено из Telegram (демо-счетов: %d)", len(self.demo.accounts))

    # --- отправка -----------------------------------------------------------

    async def _send(self, chat_id: int, text: str) -> None:
        for part in split_message(text):
            try:
                await self.bot.send_message(chat_id, part)
            except Exception as e:
                log.warning("Не смог отправить в чат %s: %s", chat_id, e)

    async def _answer(self, message: Message, text: str) -> None:
        for part in split_message(text):
            await message.answer(part)

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
        return "\n\n".join(parts)

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
                for chat_id in self._report_recipients():
                    await self._send(chat_id, text)
                self.stats.reset_period()
            except Exception:
                log.exception("Ошибка ежедневного отчёта")
            await asyncio.sleep(90)

    # --- доступ -------------------------------------------------------------

    def _allowed(self, message: Message) -> bool:
        if not self.allowed_ids:
            return True
        return message.from_user is not None and message.from_user.id in self.allowed_ids

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
            await wait.edit_text(parts[0])
            for part in parts[1:]:
                await message.answer(part)

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
                names = ", ".join(f"<code>{k}</code>" for k in EDITABLE)
                await message.answer(f"Формат: <code>/set имя значение</code>\nИмена: {names}")
                return
            try:
                value = float(args[1].replace(",", "."))
            except ValueError:
                await message.answer("Значение должно быть числом, например <code>0.5</code>")
                return
            if value < 0 or (args[0] == "interval" and value < 10):
                await message.answer("Слишком маленькое значение (интервал не меньше 10 с).")
                return
            attr, desc = EDITABLE[args[0]]
            setattr(self.settings, attr, value)
            await message.answer(f"Готово: {desc} = <b>{value:g}</b>")

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
        blocks += [format_opportunity(o, self.scanner) for o in fresh_cross]
        blocks += [format_triangle(o, self.scanner) for o in fresh_tri]
        for o in [*fresh_cross, *fresh_tri]:
            self.sent_alerts[o.key] = (time.time(), o.net_percent)
        text = "\n\n".join(blocks)
        for chat_id in list(self.subscribers.chats):
            await self._send(chat_id, text)

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
            await self.bot.session.close()
