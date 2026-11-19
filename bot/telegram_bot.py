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

from .config import EDITABLE, Settings
from .demo import DemoEngine
from .scanner import Opportunity, ScanResult, Scanner, fmt_price

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
MAX_IN_MESSAGE = 10  # сколько связок показывать в одном сообщении

HELP_TEXT = """\
<b>Арбитражный сканер</b> 🤖

Я сравниваю цены монет на разных биржах и ищу, где можно купить дешевле, \
а продать дороже. Сам я ничего не покупаю, только присылаю сигналы.

<b>Команды</b>
/scan — просканировать прямо сейчас
/auto_on — присылать сигналы автоматически
/auto_off — выключить автосигналы
/status — результат последнего сканирования
/settings — текущие настройки
/set — изменить настройку, например <code>/set min_profit 1</code>
/myid — показать твой Telegram ID
/help — это сообщение

<b>Демо-счёт</b> (виртуальные деньги, реальные цены и комиссии)
/demo_start 5 — запустить демо с $5 (можно любую сумму)
/demo — баланс, сделки и почему связки отклонены
/demo_history — последние сделки
/demo_stop — остановить демо

⚠️ <b>Перед сделкой всегда проверь вручную</b>, что на обеих биржах открыты \
ввод и вывод монеты и что это одна и та же монета в той же сети. \
Учитывай комиссию за вывод и время перевода: пока монета идёт, цена может уйти."""


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


def format_scan(result: ScanResult, scanner: Scanner, settings: Settings) -> str:
    parts: list[str] = []
    if result.opportunities:
        parts.append(f"Найдено связок: <b>{len(result.opportunities)}</b> (показываю до {MAX_IN_MESSAGE})")
        parts += [format_opportunity(o, scanner) for o in result.opportunities[:MAX_IN_MESSAGE]]
    else:
        parts.append(
            f"Связок с прибылью от {settings.min_profit_percent:g}% не найдено 🤷\n"
            "Это нормально: крупные спреды бывают редко. Попробуй уменьшить порог: "
            "<code>/set min_profit 0.3</code>"
        )
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
        lines.append(f"<code>{key}</code> = {getattr(settings, attr):g}  ({desc})")
    exchanges = ", ".join(scanner.display_name(n) for n in scanner.exchanges)
    lines.append(f"\nБиржи: {html.escape(exchanges)}")
    lines.append(f"Валюта: {settings.quote}")
    lines.append("\nИзменить: <code>/set min_profit 1</code>")
    lines.append("<i>Биржи и валюта меняются в файле .env (нужен перезапуск).</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Сам бот
# ---------------------------------------------------------------------------


class ArbitrageBot:
    def __init__(self, token: str, settings: Settings, allowed_ids: set[int]):
        self.settings = settings
        self.allowed_ids = allowed_ids
        self.scanner = Scanner(settings)
        self.subscribers = SubscriberStore()
        self.sent_alerts: dict[tuple[str, str, str], tuple[float, float]] = {}
        self.demo = DemoEngine(self.scanner, settings)

        self.bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self._register_handlers()

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
                result = await self.scanner.scan()
            except Exception as e:
                log.exception("Ошибка сканирования")
                await wait.edit_text(f"Ошибка сканирования: {html.escape(str(e))[:300]}")
                return
            await wait.edit_text(format_scan(result, self.scanner, self.settings))

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
            await message.answer(
                f"Последний скан: {ago} с назад, связок: {len(result.opportunities)}.\n"
                f"Автосигналы в этом чате: {auto}\n\n" + format_footer(result, self.scanner)
            )

        @dp.message(Command("auto_on"))
        async def cmd_auto_on(message: Message) -> None:
            if not self._allowed(message):
                return
            self.subscribers.add(message.chat.id)
            await message.answer(
                f"✅ Автосигналы включены. Сканирую каждые {self.settings.scan_interval_sec:g} с "
                f"и пишу, когда прибыль ≥ {self.settings.min_profit_percent:g}%."
            )

        @dp.message(Command("auto_off"))
        async def cmd_auto_off(message: Message) -> None:
            if not self._allowed(message):
                return
            self.subscribers.remove(message.chat.id)
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
            raw = (command.args or "5").strip().replace(",", ".").lstrip("$")
            try:
                amount = float(raw)
            except ValueError:
                await message.answer("Формат: <code>/demo_start 5</code>")
                return
            if not 1 <= amount <= 1_000_000:
                await message.answer("Сумма от 1 до 1 000 000 $")
                return
            self.demo.start(amount, message.chat.id)
            await message.answer(
                f"🧪 Демо-счёт запущен: <b>${amount:g}</b> виртуальных USDT.\n"
                f"Бот сканирует каждые {self.settings.scan_interval_sec:g} с и сам «торгует» по реальным "
                f"стаканам: с комиссиями, минимальными ордерами, платой за вывод и задержкой перевода "
                f"{self.settings.demo_transfer_min:g} мин.\n\n"
                "Сделки придут сюда сообщениями. Статистика: /demo"
            )

        @dp.message(Command("demo"))
        async def cmd_demo(message: Message) -> None:
            if not self._allowed(message):
                return
            await message.answer(self.demo.status_text())

        @dp.message(Command("demo_history"))
        async def cmd_demo_history(message: Message) -> None:
            if not self._allowed(message):
                return
            await message.answer(self.demo.history_text())

        @dp.message(Command("demo_stop"))
        async def cmd_demo_stop(message: Message) -> None:
            if not self._allowed(message):
                return
            self.demo.stop()
            await message.answer("⏸ Демо остановлено. Итог:\n\n" + self.demo.status_text())

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

    def _should_alert(self, o: Opportunity) -> bool:
        # Если стакан проверили и реальной прибыли не хватает, молчим
        if o.depth and o.depth.profit_percent < self.settings.min_profit_percent:
            return False
        prev = self.sent_alerts.get(o.key)
        if prev is None:
            return True
        sent_at, sent_profit = prev
        cooldown_passed = time.time() - sent_at > self.settings.alert_cooldown_min * 60
        grew = o.net_percent - sent_profit >= 1.0  # спред вырос на 1 п.п. и больше
        return cooldown_passed or grew

    async def _run_demo(self, result: ScanResult) -> None:
        try:
            events = await self.demo.process(result)
        except Exception:
            log.exception("Ошибка демо-счёта")
            return
        chat_id = self.demo.acc.chat_id
        for text in events:
            if chat_id is None:
                log.info(text)
                continue
            try:
                await self.bot.send_message(chat_id, text)
            except Exception as e:
                log.warning("Не смог отправить демо-событие: %s", e)

    async def _auto_loop(self) -> None:
        await asyncio.sleep(3)
        while True:
            if self.subscribers.chats or self.demo.active:
                try:
                    result = await self.scanner.scan()
                    await self._run_demo(result)
                    fresh = [o for o in result.opportunities if self._should_alert(o)]
                    if fresh and self.subscribers.chats:
                        text = "🔔 <b>Новые связки</b>\n\n" + "\n\n".join(
                            format_opportunity(o, self.scanner) for o in fresh[:MAX_IN_MESSAGE]
                        )
                        for o in fresh[:MAX_IN_MESSAGE]:
                            self.sent_alerts[o.key] = (time.time(), o.net_percent)
                        for chat_id in list(self.subscribers.chats):
                            try:
                                await self.bot.send_message(chat_id, text)
                            except Exception as e:
                                log.warning("Не смог отправить в чат %s: %s", chat_id, e)
                except Exception:
                    log.exception("Ошибка в автосканировании")
            await asyncio.sleep(self.settings.scan_interval_sec)

    # --- запуск -------------------------------------------------------------

    async def run(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="scan", description="Просканировать сейчас"),
                BotCommand(command="auto_on", description="Включить автосигналы"),
                BotCommand(command="auto_off", description="Выключить автосигналы"),
                BotCommand(command="demo_start", description="Демо-счёт: /demo_start 5"),
                BotCommand(command="demo", description="Демо: баланс и статистика"),
                BotCommand(command="demo_history", description="Демо: последние сделки"),
                BotCommand(command="demo_stop", description="Остановить демо"),
                BotCommand(command="status", description="Последний скан"),
                BotCommand(command="settings", description="Настройки"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        me = await self.bot.get_me()
        log.info("Бот @%s запущен. Открой его в Telegram и нажми Start.", me.username)
        if not self.allowed_ids:
            log.warning("ALLOWED_USER_IDS пустой: ботом может пользоваться кто угодно")

        auto_task = asyncio.create_task(self._auto_loop())
        try:
            await self.dp.start_polling(self.bot)
        finally:
            auto_task.cancel()
            await self.scanner.close()
            await self.bot.session.close()
