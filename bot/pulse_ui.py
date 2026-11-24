"""⚡ /pulse в Telegram: пульс рынка (GMGN + Axiom Pulse), стратегии-фильтры на бумаге, выводы и сигналы."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .pulse import BUCKETS, HOLD_H, PRESETS

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

TICK_SEC = 60

HOW = f"""⚡ <b>Как работает Пульс рынка</b>

<b>Что смотрит</b> (раз в 3 минуты):
• <b>GMGN</b> — тренды за 5 минут, «окопы» (новые токены, почти завершившие bonding curve, только что вышедшие \
на DEX — это ровно три колонки Pulse в Axiom: New Pairs, Final Stretch, Migrated), сделки умных кошельков и KOL, \
свежие коллы. Нужен бесплатный ключ: gmgn.ai/ai → API key → Render → Environment → <code>GMGN_API_KEY</code>. \
Ключ только читает рынок — к деньгам доступа нет.
• <b>DexScreener</b> — что сейчас продвигают (работает и без ключа) + проверка RugCheck.
• <b>🎥 Стримы</b> — токен мелькнул у стримера (инсайд-чат, трекер кошельков, колл) или стример им торгует.

<b>Что делает</b>: запоминает цену и все признаки токена (ликвидность, возраст, холдеры, топ-10, разработчик, \
снайперы, бандлы, инсайдеры, риск rug, умные деньги) и ведёт его {HOLD_H} часа. {len(PRESETS)} стратегий-фильтров \
(как пресеты в Axiom/GMGN) торгуют кандидатами на бумажных $10 со стопом, тейком, комиссиями и сетью Solana.

<b>Как учится</b>: по закрытым токенам считает, сколько в среднем приносит каждый признак ({len(BUCKETS)} признаков \
по 3 корзины + источник). Что доказанно теряет — бот сам вносит в стоп-лист, на нём торгует «выученный фильтр». \
Стоп и тейк подбирает по путям цены. Лучшая стратегия (только если плюс не похож на везение) присылает сигналы.

<b>Axiom</b>: открытого API у Axiom нет, а входить в твой аккаунт боту нельзя — поэтому его Pulse бот повторяет \
через GMGN (данные те же, они с блокчейна) и видит Axiom на экранах стримеров.

Честно: большинство мемкоинов падает. Пульс показывает на цифрах, что работает, а что нет. Настоящие деньги не \
трогает — итог виден в 💰 /profit."""


def keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.pulse.st
    rows = [
        [InlineKeyboardButton(text="🧠 Чему научился", callback_data="pu:learn"),
         InlineKeyboardButton(text="📋 Кандидаты", callback_data="pu:picks")],
        [InlineKeyboardButton(text="⏸ Выключить" if st["on"] else "🟢 Включить", callback_data="pu:toggle"),
         InlineKeyboardButton(text=("🔔 Сигналы: да" if st["notify"] else "🔕 Сигналы: нет"), callback_data="pu:notify")],
        [InlineKeyboardButton(text="🔄 Найти сейчас", callback_data="pu:scan"),
         InlineKeyboardButton(text="❔ Как работает", callback_data="pu:help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def learn_text(b: ArbitrageBot) -> str:
    p = b.pulse
    ins = p.insights()
    lines = ["🧠 <b>Чему Пульс научился на рынке</b> — средний итог токенов с признаком (стоп/тейк и комиссии учтены)"]
    if not ins:
        lines.append(f"\nПока мало закрытых токенов: признак показываю от 8 штук, каждого веду {HOLD_H} ч. "
                     f"Сейчас веду {len(p.st['picks'])}, закрыто {len(p.st['done'])}.")
        return "\n".join(lines)
    icons = {"proven": "✅", "promising": "🟡", "flat": "⚪", "losing": "🔴", "data": "⏳"}
    for x in sorted(ins, key=lambda x: -x["avg"])[:24]:
        lines.append(f"{icons.get(x['verdict'], '•')} {html.escape(x['label'])}: {x['n']} шт., "
                     f"<b>{x['avg']:+.0f}%</b>, в плюс {x['win']}%")
    if p.st["avoid"]:
        from .pulse import bucket_label
        lines.append("\n🚫 <b>Стоп-лист</b> (доказанный минус от 15 токенов): "
                     + "; ".join(html.escape(bucket_label(x)) for x in p.st["avoid"]))
    lines.append("\n✅ доказан плюс · 🟡 плюс, но может быть везение · ⚪ около нуля · 🔴 доказанный минус")
    return "\n".join(lines)


def picks_text(b: ArbitrageBot) -> str:
    from .pulse import SRC_TEXT
    st = b.pulse.st
    picks = sorted(st["picks"].values(), key=lambda p: -p["at"])[:15]
    lines = [f"📋 <b>Кандидаты Пульса</b> — веду {len(st['picks'])}, каждого {HOLD_H} ч"]
    if not picks:
        lines.append("Пока пусто — первый поиск через пару минут после запуска.")
    now = time.time()
    for p in picks:
        f = p["f"]
        src = ", ".join(SRC_TEXT.get(s, s) for s in f.get("src") or [])
        extra = []
        for k, label in (("top10", "топ-10"), ("bundler", "бандлы"), ("insider", "инсайд."), ("dev", "dev")):
            if f.get(k) is not None:
                extra.append(f"{label} {f[k]:.0%}")
        if f.get("smart") or f.get("smart_buys"):
            extra.append(f"умных {int(max(f.get('smart') or 0, f.get('smart_buys') or 0))}")
        lines.append(f"• <b>{html.escape(p['symbol'])}</b> ({html.escape(src)}) {int((now - p['at']) // 60)} мин назад: "
                     f"сейчас <b>{p['path'][-1]:+.0f}%</b>, макс. {p['max']:+.0f}%"
                     + (f"\n   {' · '.join(extra)}" if extra else ""))
    return "\n".join(lines)


async def show(b: ArbitrageBot, chat_id: int) -> None:
    await b._send(chat_id, b.pulse.text(), keyboard(b))


async def pulse_tick(b: ArbitrageBot) -> None:
    if b.owner_id is None:
        return
    p = b.pulse
    before = b.copy.changes
    try:
        events = await p.step()
    except Exception as e:
        log.exception("Пульс рынка")
        p.st["last_error"] = str(e)[:200]
        events = []
    if events:
        from .copy_ui import send_events
        await send_events(b, events)
    if b.copy.changes != before:
        b.save_copy()


async def pulse_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(60)
    while True:
        b._beats["pulse"] = time.time()
        try:
            await pulse_tick(b)
        except Exception:
            log.exception("Ошибка пульса рынка")
        await asyncio.sleep(TICK_SEC)


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    @dp.message(Command("pulse", "gmgn", "axiom", "пульс"))
    async def cmd_pulse(message: Message) -> None:
        if not b._allowed_user(message.from_user.id if message.from_user else None):
            return
        await show(b, message.chat.id)

    @dp.callback_query(F.data.startswith("pu:"))
    async def cb_pulse(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        owner = b.is_owner(cb.from_user.id)
        p = b.pulse
        if action in ("toggle", "notify", "scan") and not owner:
            await cb.answer("Это может менять только владелец бота", show_alert=True)
            return
        await cb.answer()
        if action == "show":
            await show(b, chat_id)
        elif action == "help":
            await b._send(chat_id, HOW, keyboard(b))
        elif action == "learn":
            await b._send(chat_id, learn_text(b), keyboard(b))
        elif action == "picks":
            await b._send(chat_id, picks_text(b), keyboard(b))
        elif action == "toggle":
            p.st["on"] = not p.st["on"]
            p._changed()
            b.save_copy()
            await show(b, chat_id)
        elif action == "notify":
            p.st["notify"] = not p.st["notify"]
            p._changed()
            b.save_copy()
            await show(b, chat_id)
        elif action == "scan":
            if p.busy:
                await b._send(chat_id, "⏳ Уже ищу, подожди минуту.")
                return
            await b._send(chat_id, "⚡ Смотрю GMGN и DexScreener…")
            p.busy = True
            try:
                events = await p.scan()
            except Exception as e:
                log.info("Пульс: %s", e)
                events = []
                p.st["last_error"] = str(e)[:200]
            finally:
                p.busy = False
            b.save_copy()
            if events:
                from .copy_ui import send_events
                await send_events(b, events)
            await b._send(chat_id, picks_text(b), keyboard(b))


__all__ = ["register", "pulse_loop", "pulse_tick", "keyboard", "HOW", "learn_text", "picks_text"]
