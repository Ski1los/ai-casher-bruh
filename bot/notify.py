"""🔔 Уведомления всем пользователям бота, а не только владельцу.

Раньше всё, что бот присылает сам (сделки автомата, мемкоины, копитрейдинг, пульс рынка, эфиры, проблемы с ключами,
новые версии), шло только главному владельцу. Теперь — всем, у кого есть доступ: владелец, приглашённые по ссылке
(/users) и записанные в ALLOWED_USER_IDS. Каждый может выключить уведомления себе (/notify), владелец — любому (/users).

Если действие сделал один пользователь (например, включил реальный режим в приложении), остальные видят, кто это был:
«👤 Вася: 🔴 Реальный режим включён…».

Только владельцу по-прежнему идут: резервные копии (это файлы-хранилище) и «кто-то присоединился по ссылке».
"""

from __future__ import annotations

import contextvars
import html
import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

# кто сейчас действует (приложение ставит это на время запроса) — чтобы остальные видели, кто что сделал
ACTOR: contextvars.ContextVar[int | None] = contextvars.ContextVar("notify_actor", default=None)


def everyone(b: ArbitrageBot) -> list[int]:
    """Все пользователи с доступом: владелец первым, потом ALLOWED_USER_IDS, потом приглашённые."""
    if b.owner_id is None:
        return []
    out = [b.owner_id]
    for uid in sorted(b.env_ids):
        if uid not in out:
            out.append(uid)
    for uid in sorted(getattr(b, "users", {}), key=lambda k: b.users[k].get("added", 0)):
        if uid.lstrip("-").isdigit() and int(uid) not in out:
            out.append(int(uid))
    return out


def recipients(b: ArbitrageBot) -> list[int]:
    muted = getattr(b, "muted", set())
    return [uid for uid in everyone(b) if uid not in muted]


def name_of(b: ArbitrageBot, uid: int) -> str:
    if uid == b.owner_id:
        return "владелец"
    u = getattr(b, "users", {}).get(str(uid)) or {}
    return u.get("name") or str(uid)


async def notify(b: ArbitrageBot, text: str, markup: InlineKeyboardMarkup | None = None,
                 actor: int | None = None, skip_actor: bool = False) -> int:
    """Отправить уведомление всем, кто его не выключил. Возвращает, скольким отправлено."""
    actor = ACTOR.get() if actor is None else actor
    sent = 0
    for uid in recipients(b):
        if skip_actor and uid == actor:
            continue
        body = text
        if actor is not None and uid != actor and actor in everyone(b):
            body = f"👤 <b>{html.escape(name_of(b, actor))}</b>: {text}"
        try:
            await b._send(uid, body, markup)
            sent += 1
        except Exception as e:  # пользователь удалил чат с ботом и т.п. — остальным всё равно отправляем
            log.info("Уведомление для %s не ушло: %s", uid, e)
    return sent


def set_muted(b: ArbitrageBot, uid: int, muted: bool) -> None:
    if not hasattr(b, "muted"):
        b.muted = set()
    if muted:
        b.muted.add(uid)
    else:
        b.muted.discard(uid)


def status_text(b: ArbitrageBot, uid: int) -> str:
    on = uid not in getattr(b, "muted", set())
    who = ", ".join(html.escape(name_of(b, u)) for u in recipients(b)) or "никто"
    lines = [f"🔔 <b>Уведомления</b>: {'включены' if on else '🔕 выключены'}",
             "Сделки автомата, мемкоины, копитрейдинг, пульс рынка, эфиры трейдеров, проблемы с ключами, "
             "новые версии и ежедневный отчёт.",
             f"Сейчас получают: {who}."]
    if uid == b.owner_id and not on:
        lines.append("⚠️ Ты главный владелец: без уведомлений не узнаешь о проблемах с ключами и сделках.")
    return "\n".join(lines)


def keyboard(b: ArbitrageBot, uid: int) -> InlineKeyboardMarkup:
    on = uid not in getattr(b, "muted", set())
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔕 Выключить мне" if on else "🔔 Включить мне",
                             callback_data="nt:off" if on else "nt:on")]])


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    @dp.message(Command("notify", "уведомления", "notifications"))
    async def cmd_notify(message: Message) -> None:
        if not b._allowed(message) or message.from_user is None:
            return
        uid = message.from_user.id
        arg = (message.text or "").split(maxsplit=1)[1:]
        arg = arg[0].strip().lower() if arg else ""
        if uid not in everyone(b):
            await message.answer("🔔 Уведомления приходят пользователям бота. Попроси владельца пригласить тебя: /users")
            return
        if arg in ("on", "вкл", "off", "выкл"):
            set_muted(b, uid, arg in ("off", "выкл"))
            await b.save_extras()
        await message.answer(status_text(b, uid), reply_markup=keyboard(b, uid))

    @dp.callback_query(F.data.startswith("nt:"))
    async def cb_notify(cb: CallbackQuery) -> None:
        uid = cb.from_user.id
        if not b._allowed_user(uid) or uid not in everyone(b):
            await cb.answer("Нет доступа", show_alert=True)
            return
        set_muted(b, uid, cb.data == "nt:off")
        await b.save_extras()
        await cb.answer("Уведомления выключены" if cb.data == "nt:off" else "Уведомления включены")
        if cb.message:
            try:
                await cb.message.edit_text(status_text(b, uid), reply_markup=keyboard(b, uid))
            except Exception as e:
                log.debug("notify edit: %s", e)


__all__ = ["ACTOR", "notify", "recipients", "everyone", "set_muted", "status_text", "keyboard", "register", "name_of"]
