"""Telegram: /news — 📰 новости, их влияние на рынок и чему бот на них научился (bot/news.py)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

HELP = ("📰 <b>Команды новостей</b>\n"
        "/news — главное, настроение рынка, щит и пауза\n"
        "/news all — все события за сутки с замерами цены\n"
        "/news learn — чему бот научился (какие новости реально двигают цену)\n"
        "/news src — какие источники работают\n"
        "/news alerts on|off — уведомления о важных новостях\n"
        "/news on|off — весь модуль (выключенный не ставит щит и паузы)")


def keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.news.st
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить сейчас", callback_data="nw:refresh"),
         InlineKeyboardButton(text="📋 Все за сутки", callback_data="nw:all")],
        [InlineKeyboardButton(text="🧠 Чему научился", callback_data="nw:learn"),
         InlineKeyboardButton(text="📡 Источники", callback_data="nw:src")],
        [InlineKeyboardButton(text="🔔 Уведомления: вкл" if st["alerts"] else "🔕 Уведомления: выкл",
                              callback_data="nw:alerts")],
    ])


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    def owner_ok(uid: int | None) -> bool:
        return b.owner_id is None or b.is_owner(uid)

    @dp.message(Command("news", "novosti", "новости"))
    async def cmd_news(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        uid = message.from_user.id if message.from_user else None
        arg = (command.args or "").strip().lower().split()
        nw = b.news
        if not arg:
            await b._send(message.chat.id, nw.panel_text(), keyboard(b))
        elif arg[0] in ("all", "все"):
            await b._send(message.chat.id, nw.all_text(), keyboard(b))
        elif arg[0] in ("learn", "обучение", "stats"):
            await b._send(message.chat.id, nw.learn_text())
        elif arg[0] in ("src", "sources", "источники"):
            await b._send(message.chat.id, nw.src_text())
        elif arg[0] in ("alerts", "notify", "уведомления") and len(arg) > 1 and arg[1] in ("on", "off"):
            if not owner_ok(uid):
                await message.answer("Менять может только владелец.")
                return
            nw.st["alerts"] = arg[1] == "on"
            nw._changed()
            await message.answer("🔔 Уведомления о важных новостях включены" if nw.st["alerts"]
                                 else "🔕 Уведомления о новостях выключены (щит и паузы работают)")
        elif arg[0] in ("on", "off"):
            if not owner_ok(uid):
                await message.answer("Включать и выключать может только владелец.")
                return
            nw.st["on"] = arg[0] == "on"
            nw._changed()
            nw.apply()
            await message.answer("📰 Новости включены: бот читает источники, учится и защищает сделки" if nw.st["on"]
                                 else "⏸ Новости выключены: щит монет и паузы по новостям сняты")
        else:
            await b._send(message.chat.id, HELP)

    @dp.callback_query(F.data.startswith("nw:"))
    async def cb_news(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        nw = b.news
        if action == "alerts":
            if not owner_ok(cb.from_user.id):
                await cb.answer("Только владелец", show_alert=True)
                return
            nw.st["alerts"] = not nw.st["alerts"]
            nw._changed()
            await cb.answer("Уведомления " + ("включены 🔔" if nw.st["alerts"] else "выключены 🔕"))
            try:
                await cb.message.edit_reply_markup(reply_markup=keyboard(b))
            except Exception:
                pass
            return
        if action == "refresh":
            await cb.answer("Читаю все источники… до 30 секунд")
            try:
                msgs = await nw.tick(force=True)
            except Exception as e:
                log.warning("Новости, обновление: %s", e)
                msgs = []
            for text in msgs:
                await b.notify(text)
            await b._send(chat_id, nw.panel_text(), keyboard(b))
            return
        await cb.answer()
        if action == "all":
            await b._send(chat_id, nw.all_text(), keyboard(b))
        elif action == "learn":
            await b._send(chat_id, nw.learn_text())
        elif action == "src":
            await b._send(chat_id, nw.src_text())
