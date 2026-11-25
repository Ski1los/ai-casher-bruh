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
        "/news on|off — весь модуль (выключенный не ставит щит и паузы)\n"
        "/newstrade — 🤖 торговля по новостям (демо, потом реал)")


def keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.news.st
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить сейчас", callback_data="nw:refresh"),
         InlineKeyboardButton(text="📋 Все за сутки", callback_data="nw:all")],
        [InlineKeyboardButton(text="🧠 Чему научился", callback_data="nw:learn"),
         InlineKeyboardButton(text="📡 Источники", callback_data="nw:src")],
        [InlineKeyboardButton(text="🤖 Торговля по новостям", callback_data="nx:panel")],
        [InlineKeyboardButton(text="🔔 Уведомления: вкл" if st["alerts"] else "🔕 Уведомления: выкл",
                              callback_data="nw:alerts")],
    ])


TRADE_HELP = ("🤖 <b>Торговля по новостям</b>\n"
              "/newstrade — состояние, открытые сделки, экзамен на демо\n"
              "/newstrade on|off — включить или выключить\n"
              "/newstrade real — реальные сделки (только для типов новостей, сдавших экзамен на демо)\n"
              "/newstrade demo — вернуть всё в демо\n"
              "/newstrade sell — продать все открытые сделки по новостям\n"
              "/newstrade reset — начать демо заново ($10)")

REAL_WARNING = ("💵 <b>Включить реальные сделки по новостям?</b>\n\n"
                "Вживую бот будет покупать только по тем типам новостей, которые сдали экзамен на демо, и только "
                "в пределах лимитов (/set max_trade, дневной убыток). Остальные типы продолжат учиться на демо.\n\n"
                "Риски: новость доходит до бота за минуты, цена может развернуться; стоп −2.5% при резком движении "
                "может исполниться хуже. Две реальные сделки подряд в минус — бот сам вернётся в демо. /stop — тоже.")


def trade_kb(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.newstrade.st
    rows = [[InlineKeyboardButton(text="⏸ Выключить" if st["on"] else "▶️ Включить",
                                  callback_data="nx:off" if st["on"] else "nx:on"),
             InlineKeyboardButton(text="🧪 Вернуть демо" if st["real"] else "💵 Реальные сделки",
                                  callback_data="nx:demo" if st["real"] else "nx:real")]]
    if st["open"]:
        rows.append([InlineKeyboardButton(text="💰 Продать всё сейчас", callback_data="nx:sell")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="nx:panel"),
                 InlineKeyboardButton(text="📰 Новости", callback_data="nx:news")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, включить", callback_data="nx:real2"),
        InlineKeyboardButton(text="Отмена", callback_data="nx:panel")]])


async def trade_action(b: ArbitrageBot, action: str, uid: int | None) -> tuple[str, InlineKeyboardMarkup | None]:
    """Общая логика команд и кнопок торговли по новостям. Возвращает (текст, клавиатура)."""
    from .users_ui import is_main_owner

    nt = b.newstrade
    st = nt.st
    if action in ("real", "real2") and b.owner_id is not None and not is_main_owner(b, uid):
        return "Это может только главный владелец бота: реальные деньги включает только он.", None
    if action in ("on", "off", "demo", "sell", "reset") and b.owner_id is not None and not b.is_owner(uid):
        return "Это может только владелец бота.", None
    if action in ("on", "off"):
        st["on"] = action == "on"
        nt._changed()
        return (("▶️ Торговля по новостям включена: " + nt.mode_text()) if st["on"] else
                "⏸ Торговля по новостям выключена (открытые сделки ведутся до цели или стопа)"), trade_kb(b)
    if action == "real":
        why = nt.real_problem()
        if why:
            return f"💵 Пока нельзя: {why}.", trade_kb(b)
        return REAL_WARNING, real_kb()
    if action == "real2":
        why = nt.real_problem()
        if why:
            return f"💵 Пока нельзя: {why}.", trade_kb(b)
        st["real"] = True
        st["reverted"] = ""
        nt._changed()
        passed = [c for c in news_cats() if nt.graduated(c)]
        return ("💵 <b>Реальные сделки по новостям включены</b> для: " + ", ".join(news_cats()[c] for c in passed)
                + ". Остальные типы — на демо."), trade_kb(b)
    if action == "demo":
        st["real"] = False
        nt._changed()
        return "🧪 Торговля по новостям — снова демо", trade_kb(b)
    if action == "sell":
        msgs = await nt.sell_all()
        return ("\n\n".join(msgs) or "Открытых сделок по новостям нет."), trade_kb(b)
    if action == "reset":
        nt.reset_demo()
        return f"🧪 Демо торговли по новостям начато заново: ${nt.equity():.2f}", trade_kb(b)
    return nt.panel_text(), trade_kb(b)


def news_cats() -> dict:
    from .news import CATS
    return {k: v[1] for k, v in CATS.items()}


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

    @dp.message(Command("newstrade", "newsbot", "nt_trade"))
    async def cmd_newstrade(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        uid = message.from_user.id if message.from_user else None
        arg = (command.args or "").strip().lower().split()
        action = {"on": "on", "off": "off", "real": "real", "реал": "real", "demo": "demo", "демо": "demo",
                  "sell": "sell", "reset": "reset", "": "panel"}.get(arg[0] if arg else "", "help")
        if action == "help":
            await b._send(message.chat.id, TRADE_HELP)
            return
        text, kb = await trade_action(b, action, uid)
        await b._send(message.chat.id, text, kb)

    @dp.callback_query(F.data.startswith("nx:"))
    async def cb_newstrade(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        await cb.answer()
        if action == "news":
            await b._send(chat_id, b.news.panel_text(), keyboard(b))
            return
        text, kb = await trade_action(b, action, cb.from_user.id)
        await b._send(chat_id, text, kb)

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
