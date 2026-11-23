"""💰 /profit в Telegram: Профит-центр, план денег, Страж прибыли и совет ИИ."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .profit import PLAN_AMOUNTS, ROUTES

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

CHECK_SEC = 10 * 60

AI_QUESTION = (
    "Посмотри раздел profit (Профит-центр) и всё остальное. Как мне получить максимум прибыли при минимальном риске "
    "прямо сейчас? Коротко, по пунктам: 1) какие способы доказали плюс по фактам, а какие нет (не путай демо и "
    "реальные деньги); 2) куда и сколько положить из моей суммы; 3) какие настройки поменять (предложи кнопками); "
    "4) что включить или выключить. Не обещай доходность, опирайся только на цифры."
)

HOW = """💰 <b>Как работает Профит-центр</b>

• Бот собирает в одну таблицу результаты <b>всех</b> способов заработка: автопилот (реальные и тестовые сделки), \
лабораторию, копитрейдинг, мемкоины (демо и реальные), тренировку на идеях трейдеров, арбитраж.
• Для демо вычитает поправку на реальность: −0,1% за сделку на бирже и −2% на мемкоинах (демо не знает очередей \
и проскальзывания).
• Вердикт честный: «✅ доказан плюс» — только если от 15 сделок и даже при невезении (средняя минус разброс) \
сделка в плюсе. Иначе это может быть просто удача.
• <b>План денег</b>: доли по «четверти Келли» — известная формула размера ставки, урезанная в 4 раза ради \
безопасности, плюс минимальные ордера бирж. Что не нашло доказанного применения — в Earn под процент.
• <b>🛡 Страж прибыли</b>: если реальные деньги доказанно теряют на последних 8–15 сделках — пауза новых покупок \
на 3 дня (открытые позиции бот ведёт). Если демо копитрейдинга доказало минус — выключает его сигналы. \
Всё возвращается кнопкой.
• Когда что-то доказало плюс, бот напишет сам, а когда настоящие сделки подтвердили плюс на 20 сделках — \
подскажет, что можно добавить денег (не больше чем в 2 раза за раз).

Честно: никакая формула не превращает $5 в миллион. Максимум профита — это когда деньги работают только там, \
где плюс доказан, и не горят там, где доказан минус."""


def keyboard(b: ArbitrageBot, amount: float | None = None) -> InlineKeyboardMarkup:
    st = b.profit.st
    amount = amount or st["amount"]
    rows = [[InlineKeyboardButton(text=("• " if float(a) == float(amount) else "") + f"План ${a}",
                                  callback_data=f"pf:plan:{a}") for a in PLAN_AMOUNTS]]
    paused = [r for r, m in st["paused"].items() if m.get("until", 0) > time.time()]
    for r in paused:
        rows.append([InlineKeyboardButton(text=f"▶️ Снять паузу: {ROUTES[r][0][:28]}", callback_data=f"pf:resume:{r}")])
    if st["muted"].get("copy"):
        rows.append([InlineKeyboardButton(text="🔔 Вернуть сигналы копитрейдинга", callback_data="pf:unmute:copy")])
    rows.append([InlineKeyboardButton(text="🧠 Совет ИИ: как заработать больше", callback_data="pf:ai")])
    rows.append([InlineKeyboardButton(text=f"🛡 Страж: {'вкл' if st['guard'] else 'выкл'}", callback_data="pf:guard"),
                 InlineKeyboardButton(text="🔄 Обновить", callback_data="pf:show"),
                 InlineKeyboardButton(text="❔ Как это", callback_data="pf:help")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show(b: ArbitrageBot, chat_id: int, amount: float | None = None) -> None:
    await b._send(chat_id, b.profit.text(amount), keyboard(b, amount))


async def profit_tick(b: ArbitrageBot) -> None:
    if b.owner_id is None:
        return
    before = b.copy.changes
    try:
        events = b.profit.check()
    except Exception:
        log.exception("Профит-центр")
        events = []
    from .copy_ui import send_events
    await send_events(b, events)
    if b.copy.changes != before:
        b.save_copy()
        b.save_trading()
        if events:
            await b._backup_now()


async def profit_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(90)
    while True:
        try:
            await profit_tick(b)
        except Exception:
            log.exception("Ошибка Профит-центра")
        await asyncio.sleep(CHECK_SEC)


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    async def owner_only(uid: int | None, chat_id: int) -> bool:
        if b.owner_id is None or uid != b.owner_id:
            await b._send(chat_id, "⛔ Это может менять только владелец бота.")
            return False
        return True

    @dp.message(Command("profit", "profits", "pnl", "профит"))
    async def cmd_profit(message: Message, command: CommandObject) -> None:
        if not b._allowed_user(message.from_user.id if message.from_user else None):
            return
        arg = (command.args or "").strip().replace("$", "").replace(",", ".")
        amount = None
        try:
            amount = float(arg) if arg else None
        except ValueError:
            amount = None
        if amount is not None and not (1 <= amount <= 1_000_000):
            amount = None
        await show(b, message.chat.id, amount)

    @dp.callback_query(F.data.startswith("pf:"))
    async def cb_profit(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        parts = cb.data.split(":", 2)
        action = parts[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        await cb.answer()
        p = b.profit
        if action == "show":
            await show(b, chat_id)
        elif action == "help":
            await b._send(chat_id, HOW, keyboard(b))
        elif action == "plan" and len(parts) > 2:
            amount = float(parts[2])
            if b.owner_id is not None and cb.from_user.id == b.owner_id:
                p.st["amount"] = amount
                p._changed()
                b.save_copy()
            await show(b, chat_id, amount)
        elif action == "ai":
            await b._ai_respond(chat_id, lambda: b.ai.ask(chat_id, AI_QUESTION, b.ai_context()))
        elif not await owner_only(cb.from_user.id, chat_id):
            return
        elif action == "guard":
            p.st["guard"] = not p.st["guard"]
            p._changed()
            b.save_copy()
            await b._send(chat_id, "🛡 Страж прибыли включён: буду ставить на паузу то, что доказанно теряет."
                          if p.st["guard"] else "🛡 Страж прибыли выключен. Паузы после серий убытков в самих "
                          "режимах (щит автопилота, лимиты мемкоинов) продолжают работать.")
            await show(b, chat_id)
        elif action == "resume" and len(parts) > 2 and parts[2] in ROUTES:
            await b._send(chat_id, p.resume(parts[2]))
            b.save_copy()
            b.save_trading()
            await show(b, chat_id)
        elif action == "unmute" and len(parts) > 2:
            await b._send(chat_id, p.unmute(parts[2]))
            b.save_copy()
            await show(b, chat_id)


__all__ = ["register", "profit_loop", "profit_tick", "keyboard", "show", "AI_QUESTION"]
