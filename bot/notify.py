"""🔔 Уведомления всем пользователям бота, а не только владельцу.

Раньше всё, что бот присылает сам (сделки автомата, мемкоины, копитрейдинг, пульс рынка, эфиры, проблемы с ключами,
новые версии), шло только главному владельцу. Теперь — всем, у кого есть доступ: владелец, приглашённые по ссылке
(/users) и записанные в ALLOWED_USER_IDS. Каждый может выключить уведомления себе (/notify), владелец — любому (/users).

Если действие сделал один пользователь (например, включил реальный режим в приложении), остальные видят, кто это был:
«👤 Вася: 🔴 Реальный режим включён…».

Только владельцу по-прежнему идут: резервные копии (это файлы-хранилище) и «кто-то присоединился по ссылке».

v41: 🌙 «тихие часы» (23:00–08:00 по времени отчётов, REPORT_UTC_OFFSET) — по желанию каждого: ночью обычные
события копятся и утром приходят одним сообщением «Пока ты спал». Важное приходит сразу всегда: настоящие деньги,
предупреждения, проблемы. Каждое уведомление пишется и в 📡 ленту событий (bot/feed.py).
"""

from __future__ import annotations

import contextvars
import html
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import feed

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

# кто сейчас действует (приложение ставит это на время запроса) — чтобы остальные видели, кто что сделал
ACTOR: contextvars.ContextVar[int | None] = contextvars.ContextVar("notify_actor", default=None)

QUIET = (23, 8)        # 🌙 тихие часы: с 23:00 до 08:00
HELD_MAX = 40          # сколько событий копим за ночь на человека
URGENT_MARKS = ("🔴", "🚨", "⛔", "❌", "⚠️", "⏸", "🔑", "📥", "💵")
URGENT_WORDS = ("реальн", "настоящ", "live", "phantom", "стоп-лосс", "две копии", "убыт")


def is_urgent(text: str) -> bool:
    """Важное (деньги, проблемы) приходит сразу даже в тихие часы."""
    head = (text or "").lstrip()
    low = head.lower()
    return head.startswith(URGENT_MARKS) or any(m in head[:80] for m in ("⚠️", "🚨", "⛔")) \
        or any(w in low for w in URGENT_WORDS)


def quiet_now(b: ArbitrageBot, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    hour = time.gmtime(now + float(getattr(b.settings, "report_utc_offset", 0) or 0) * 3600).tm_hour
    start, end = QUIET
    return hour >= start or hour < end


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


def _held(b: ArbitrageBot) -> dict[int, list[tuple[float, str]]]:
    if not isinstance(getattr(b, "_held", None), dict):
        b._held = {}
    return b._held


async def notify(b: ArbitrageBot, text: str, markup: InlineKeyboardMarkup | None = None,
                 actor: int | None = None, skip_actor: bool = False, urgent: bool | None = None) -> int:
    """Отправить уведомление всем, кто его не выключил. Возвращает, скольким отправлено (сразу)."""
    actor = ACTOR.get() if actor is None else actor
    people = everyone(b)
    who = name_of(b, actor) if actor is not None and actor in people and len(people) > 1 else None
    try:
        feed.add(b, f"{text} · 👤 {who}" if who else text, kind=feed.kind_of(text))
    except Exception as e:  # лента — не повод потерять уведомление
        log.debug("feed: %s", e)
    hold = not (is_urgent(text) if urgent is None else urgent) and quiet_now(b)
    quiet = getattr(b, "quiet", set())
    sent = 0
    for uid in recipients(b):
        if skip_actor and uid == actor:
            continue
        body = text
        if actor is not None and uid != actor and actor in people:
            body = f"👤 <b>{html.escape(name_of(b, actor))}</b>: {text}"
        if hold and uid in quiet:
            box = _held(b).setdefault(uid, [])
            box.append((time.time(), feed.plain(body)))
            del box[:-HELD_MAX]
            continue
        try:
            await b._send(uid, body, markup)
            sent += 1
        except Exception as e:  # пользователь удалил чат с ботом и т.п. — остальным всё равно отправляем
            log.info("Уведомление для %s не ушло: %s", uid, e)
    return sent


async def flush_quiet(b: ArbitrageBot, now: float | None = None, force_uid: int | None = None) -> int:
    """Утром (или когда человек выключил тихие часы) — всё отложенное одним сообщением."""
    held = _held(b)
    if not held or (force_uid is None and quiet_now(b, now)):
        return 0
    n = 0
    for uid in [force_uid] if force_uid is not None else list(held):
        box = held.pop(uid, None)
        if not box:
            continue
        lines = [f"🌙 <b>Пока ты спал</b> — {len(box)} событ{'ие' if len(box) == 1 else 'ий'}:"]
        for _at, t in box[-20:]:
            lines.append(f"• {html.escape(t[:200])}")
        if len(box) > 20:
            lines.insert(1, "(показываю последние 20, всё — в ленте: /feed)")
        try:
            await b._send(uid, "\n".join(lines)[:4000])
            n += 1
        except Exception as e:
            log.info("Утренняя сводка для %s не ушла: %s", uid, e)
    return n


def set_quiet(b: ArbitrageBot, uid: int, on: bool) -> None:
    if not isinstance(getattr(b, "quiet", None), set):
        b.quiet = set()
    if on:
        b.quiet.add(uid)
    else:
        b.quiet.discard(uid)


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
    q = uid in getattr(b, "quiet", set())
    lines.append(f"🌙 Тихие часы {QUIET[0]:02d}:00–{QUIET[1]:02d}:00: " + (
        "включены — ночью обычные события копятся и утром приходят одним сообщением. Деньги и проблемы — сразу."
        if q else "выключены (уведомления приходят и ночью)."))
    lines.append("📡 Всё, что бот сделал сам, — в ленте: /feed")
    return "\n".join(lines)


def keyboard(b: ArbitrageBot, uid: int) -> InlineKeyboardMarkup:
    on = uid not in getattr(b, "muted", set())
    q = uid in getattr(b, "quiet", set())
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔕 Выключить мне" if on else "🔔 Включить мне",
                              callback_data="nt:off" if on else "nt:on")],
        [InlineKeyboardButton(text="☀️ Тихие часы: выключить" if q else "🌙 Тихие часы ночью",
                              callback_data="nt:q0" if q else "nt:q1")]])


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
        elif arg in ("quiet", "night", "ночь", "тихо"):
            set_quiet(b, uid, uid not in getattr(b, "quiet", set()))
            await b.save_extras()
        await message.answer(status_text(b, uid), reply_markup=keyboard(b, uid))

    @dp.callback_query(F.data.startswith("nt:"))
    async def cb_notify(cb: CallbackQuery) -> None:
        uid = cb.from_user.id
        if not b._allowed_user(uid) or uid not in everyone(b):
            await cb.answer("Нет доступа", show_alert=True)
            return
        if cb.data in ("nt:q0", "nt:q1"):
            set_quiet(b, uid, cb.data == "nt:q1")
            await b.save_extras()
            await cb.answer("🌙 Тихие часы включены" if cb.data == "nt:q1" else "Тихие часы выключены")
            if cb.data == "nt:q0":
                await flush_quiet(b, force_uid=uid)
        else:
            set_muted(b, uid, cb.data == "nt:off")
            await b.save_extras()
            await cb.answer("Уведомления выключены" if cb.data == "nt:off" else "Уведомления включены")
        if cb.message:
            try:
                await cb.message.edit_text(status_text(b, uid), reply_markup=keyboard(b, uid))
            except Exception as e:
                log.debug("notify edit: %s", e)


__all__ = ["ACTOR", "notify", "recipients", "everyone", "set_muted", "set_quiet", "flush_quiet", "quiet_now",
           "is_urgent", "status_text", "keyboard", "register", "name_of"]
