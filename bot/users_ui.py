"""👥 Пользователи бота: приглашение по одноразовой ссылке, список, удаление.

Владелец (первый ID в ALLOWED_USER_IDS) нажимает «➕ Пригласить» — бот даёт ссылку вида
t.me/<бот>?start=inv_<код>. Друг открывает её, нажимает «Запустить» и получает ПОЛНЫЙ доступ, как у владельца
(включая настоящую торговлю). Ссылка одноразовая и живёт 24 часа. Управлять списком может только главный владелец.

Список хранится в настройках бота (файл + резервная копия в Telegram), так что переживает перезапуски Render.
Те, кто записан в ALLOWED_USER_IDS, остаются всегда: их можно убрать только в Render.
"""

from __future__ import annotations

import html
import logging
import secrets
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

INVITE_SEC = 24 * 3600   # ссылка живёт сутки
MAX_INVITES = 5          # одновременно действующих ссылок
MAX_USERS = 20


def new_users_state() -> dict:
    return {"users": {}, "invites": {}}


def export(b: ArbitrageBot) -> dict:
    return {"users": dict(b.users), "invites": dict(b.invites)}


def restore(b: ArbitrageBot, raw) -> None:
    """Из сохранённых настроек: добавленные пользователи снова получают доступ."""
    if not isinstance(raw, dict):
        return
    users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
    b.users = {str(k): v for k, v in users.items() if str(k).lstrip("-").isdigit() and isinstance(v, dict)}
    now = time.time()
    invites = raw.get("invites") if isinstance(raw.get("invites"), dict) else {}
    b.invites = {k: float(v) for k, v in invites.items() if isinstance(v, (int, float)) and v > now}
    if b.owner_id is not None:  # бот с владельцем: добавленные снова получают доступ (открытый бот не трогаем)
        for uid in b.users:
            b.allowed_ids.add(int(uid))


def is_main_owner(b: ArbitrageBot, uid: int | None) -> bool:
    return b.owner_id is not None and uid == b.owner_id


def new_invite(b: ArbitrageBot, now: float | None = None) -> str:
    now = time.time() if now is None else now
    b.invites = {k: v for k, v in b.invites.items() if v > now}
    while len(b.invites) >= MAX_INVITES:  # самую старую ссылку гасим
        b.invites.pop(min(b.invites, key=b.invites.get))
    code = secrets.token_urlsafe(9)  # 12 символов A-Z a-z 0-9 _ - — подходят для ?start=
    b.invites[code] = now + INVITE_SEC
    return code


def add_user(b: ArbitrageBot, uid: int, name: str, how: str, now: float | None = None) -> None:
    b.users[str(uid)] = {"name": (name or str(uid))[:60], "added": time.time() if now is None else now, "how": how}
    b.allowed_ids.add(uid)


def remove_user(b: ArbitrageBot, uid: int) -> bool:
    if b.users.pop(str(uid), None) is None:
        return False
    if uid not in b.env_ids:  # из ALLOWED_USER_IDS убрать нельзя — только в Render
        b.allowed_ids.discard(uid)
        # и рассылки ему больше не идут: сигналы и радар мемкоинов
        subs = getattr(b, "subscribers", None)
        if subs is not None and uid in getattr(subs, "chats", ()):
            subs.remove(uid)
        getattr(b, "radar_chats", set()).discard(uid)
    return True


def accept_invite(b: ArbitrageBot, code: str, uid: int, name: str, now: float | None = None) -> str:
    """Проверить код и добавить. Возвращает: ok / already / owner / bad / full."""
    now = time.time() if now is None else now
    if b.owner_id is None:  # бот открыт для всех: приглашения не нужны (и не должны его закрыть)
        return "bad"
    if is_main_owner(b, uid):
        return "owner"
    exp = b.invites.get(code)
    if exp is None or exp <= now:
        b.invites.pop(code, None)
        return "bad"
    if b.is_owner(uid):
        return "already"  # ссылку не тратим
    if len(b.users) >= MAX_USERS:
        return "full"
    del b.invites[code]
    add_user(b, uid, name, "ссылка", now)
    return "ok"


def users_text(b: ArbitrageBot) -> str:
    lines = ["👥 <b>Пользователи бота</b>",
             f"👑 Владелец: <code>{b.owner_id}</code> (ты)"]
    env_extra = sorted(b.env_ids - {b.owner_id} - {int(u) for u in b.users})
    for uid in env_extra:
        lines.append(f"• <code>{uid}</code> — из ALLOWED_USER_IDS в Render (убрать можно только там)")
    for uid, u in sorted(b.users.items(), key=lambda kv: kv[1].get("added", 0)):
        when = time.strftime("%d.%m.%Y", time.gmtime(u.get("added") or 0))
        lines.append(f"• <b>{html.escape(u.get('name') or uid)}</b> (<code>{uid}</code>), с {when}, {u.get('how', '')}")
    if not env_extra and not b.users:
        lines.append("Кроме тебя — никого.")
    active = [v for v in b.invites.values() if v > time.time()]
    if active:
        lines.append(f"🔗 Действующих ссылок-приглашений: {len(active)}")
    lines.append("\n⚠️ Приглашённый получает <b>полный доступ, как у тебя</b>: сигналы, ИИ, настройки, демо и "
                 "<b>настоящая торговля твоими деньгами</b> на Bybit/BingX и в кошельке бота. Ключи бирж он не видит "
                 "(они только в Render), выводить деньги бот не умеет. Добавлять и убирать людей можешь только ты.")
    return "\n".join(lines)


def users_kb(b: ArbitrageBot) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Пригласить (одноразовая ссылка)", callback_data="us:inv")]]
    for uid, u in sorted(b.users.items(), key=lambda kv: kv[1].get("added", 0)):
        rows.append([InlineKeyboardButton(text=f"➖ Убрать {(u.get('name') or uid)[:30]}", callback_data=f"us:ask:{uid}")])
    if b.invites:
        rows.append([InlineKeyboardButton(text="🚫 Отменить все ссылки", callback_data="us:clear")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _bot_username(b: ArbitrageBot) -> str:
    if not getattr(b, "_username", None):
        try:
            me = await b.bot.get_me()
            b._username = getattr(me, "username", "") or ""
        except Exception as e:  # без имени бота дадим команду вместо ссылки
            log.info("get_me: %s", e)
            return ""
    return b._username


async def _save(b: ArbitrageBot) -> None:
    await b.save_extras()


async def handle_start_invite(b: ArbitrageBot, message: Message, args: str | None) -> bool:
    """/start inv_<код>. True — сообщение обработано (дальше обычный /start не нужен или уже показан)."""
    if not args or not args.startswith("inv_") or message.from_user is None:
        return False
    user = message.from_user
    name = user.full_name or (f"@{user.username}" if user.username else str(user.id))
    if user.username:
        name = f"{name} (@{user.username})" if user.full_name else f"@{user.username}"
    res = accept_invite(b, args[4:], user.id, name)
    if res == "owner":
        await message.answer("Это твоя ссылка-приглашение 🙂 Отправь её другу: он откроет её и получит доступ.")
        return False
    if res == "bad":
        await message.answer("⛔ Ссылка-приглашение недействительна: её уже использовали или прошли 24 часа. "
                             "Попроси владельца бота прислать новую.")
        return True
    if res == "full":
        await message.answer(f"⛔ В боте уже {MAX_USERS} пользователей. Попроси владельца убрать кого-нибудь.")
        return True
    if res == "already":
        await message.answer("✅ У тебя уже есть доступ к боту.")
        return False
    await _save(b)
    log.info("Новый пользователь %s (%s) по приглашению", user.id, name)
    await message.answer("✅ <b>Добро пожаловать!</b> Владелец пригласил тебя в бота, доступ полный.\n"
                         "Бот работает с настоящими деньгами владельца — будь внимателен с кнопками торговли "
                         "(перед реальной сделкой бот всегда переспрашивает).")
    if b.owner_id is not None:
        try:
            await b._send(b.owner_id, f"👥 <b>{html.escape(name)}</b> (<code>{user.id}</code>) присоединился по "
                                      "твоей ссылке и получил полный доступ. Убрать: /users")
        except Exception as e:
            log.info("Не смог сообщить владельцу о новом пользователе: %s", e)
    return False  # дальше — обычное приветствие с меню


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    @dp.message(Command("users", "invite", "пользователи"))
    async def cmd_users(message: Message) -> None:
        if not b._allowed(message):
            return
        uid = message.from_user.id if message.from_user else None
        if b.owner_id is None:
            await message.answer("👥 Сначала укажи себя владельцем: Render → Environment → <code>ALLOWED_USER_IDS</code> = "
                                 "твой ID (узнать: /myid). Сейчас бот открыт для всех.")
            return
        if not is_main_owner(b, uid):
            await message.answer("👥 Добавлять и убирать пользователей может только главный владелец бота.")
            return
        await message.answer(users_text(b), reply_markup=users_kb(b))

    @dp.callback_query(F.data.startswith("us:"))
    async def cb_users(cb: CallbackQuery) -> None:
        if not is_main_owner(b, cb.from_user.id):
            await cb.answer("Только главный владелец", show_alert=True)
            return
        parts = (cb.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "inv":
            code = new_invite(b)
            await _save(b)
            name = await _bot_username(b)
            link = f"https://t.me/{name}?start=inv_{code}" if name else f"/start inv_{code}"
            await cb.answer("Ссылка готова")
            await b._send(cb.from_user.id,
                          "🔗 <b>Ссылка-приглашение</b> (одноразовая, действует 24 часа):\n"
                          f"{html.escape(link)}\n\n"
                          "Отправь её другу. Он откроет ссылку → нажмёт «Запустить» → получит <b>полный доступ</b>, "
                          "как у тебя, включая настоящую торговлю. Я сообщу, когда он присоединится.\n"
                          "Передумал — /users → «🚫 Отменить все ссылки».")
            return
        if action == "clear":
            b.invites.clear()
            await _save(b)
            await cb.answer("Все ссылки отменены")
        elif action == "ask" and len(parts) > 2 and parts[2] in b.users:
            u = b.users[parts[2]]
            await cb.answer()
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да, убрать", callback_data=f"us:del:{parts[2]}"),
                InlineKeyboardButton(text="Отмена", callback_data="us:show")]])
            if cb.message:
                await cb.message.edit_text(f"Убрать <b>{html.escape(u.get('name') or parts[2])}</b> из бота? "
                                           "Он сразу потеряет доступ.", reply_markup=kb)
            return
        elif action == "del" and len(parts) > 2 and parts[2].lstrip("-").isdigit():
            uid = int(parts[2])
            name = (b.users.get(parts[2]) or {}).get("name") or parts[2]
            if remove_user(b, uid):
                await _save(b)
                await cb.answer("Убрал")
                try:
                    await b._send(uid, "👥 Владелец закрыл тебе доступ к боту.")
                except Exception:
                    log.info("Не смог сообщить %s об удалении", name)
            else:
                await cb.answer("Его уже нет в списке")
        else:
            await cb.answer()
        if cb.message:
            try:
                await cb.message.edit_text(users_text(b), reply_markup=users_kb(b))
            except Exception as e:  # «message is not modified» и т.п.
                log.debug("users edit: %s", e)


__all__ = ["register", "handle_start_invite", "accept_invite", "new_invite", "add_user", "remove_user",
           "restore", "export", "users_text", "INVITE_SEC", "MAX_INVITES", "MAX_USERS"]
