"""💸 Торговля мемкоинами в Telegram: /meme, кнопки, подтверждения и защита от утечки ключа."""

from __future__ import annotations

import html
import logging
import re
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .memetrade import SETUP_TEXT, SIZES, SwapError
from .solana import WalletError, valid_address

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

ADDR_IN_TEXT = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

CONFIRM_TEXT = """\
⚠️ <b>Включить реальное автокопирование?</b>

Бот будет <b>сам покупать мемкоины за SOL</b> с кошелька «Бот», когда ⭐-кошелёк покупает и токен проходит
риск-фильтр, и сам продавать: когда кошелёк продаст, по стопу −{stop:.0%} или через 48 ч.

Сумма сделки ${size:g} · не больше {max_open} позиций · запас {reserve:g} SOL на комиссии ·
дневной лимит убытка ${loss:g} · пауза 12 ч после 3 убытков подряд.

Честно: мемкоины чаще всего падают, копия всегда хуже оригинала (мы покупаем позже кошелька).
Прошлые успехи ⭐ не гарантируют будущих. Клади на «Бот» только то, что не жалко потерять."""

LEAK_TEXT = """\
🚨 <b>Похоже, это приватный ключ или сид-фраза — я удалил сообщение.</b>

Считай этот ключ засвеченным: он прошёл через сервера Telegram. Сделай так:
1. В Phantom создай новый аккаунт и переведи туда всё с засвеченного.
2. Ключ нового аккаунта вставляй <b>только</b> в Render → Environment → SOLANA_TRADER_KEY.
Никогда не отправляй ключ или 12 слов в чат, ИИ, «поддержке» — настоящая поддержка их не спрашивает."""


def keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    mt = b.meme
    if not mt.configured:
        rows = [[InlineKeyboardButton(text="🔄 Проверить подключение", callback_data="mt:show")],
                [InlineKeyboardButton(text="🐋 Копитрейдинг (без денег)", callback_data="cp:show")]]
        return InlineKeyboardMarkup(inline_keyboard=rows)
    st = mt.st
    rows = [[InlineKeyboardButton(text="⏸ Выключить автокопирование" if mt.on else "🟢 Включить автокопирование",
                                  callback_data="mt:off" if mt.on else "mt:on")],
            [InlineKeyboardButton(text=("✓ " if st["size_usd"] == s else "") + f"${s:g}", callback_data=f"mt:size:{s:g}")
             for s in SIZES],
            [InlineKeyboardButton(text="Кого: только ⭐" if st["scope"] == "star" else "Кого: ⭐ + 👀 хорошие",
                                  callback_data="mt:scope"),
             InlineKeyboardButton(text=f"Позиций до {st['max_open']}", callback_data="mt:maxopen")]]
    for mint, p in list(st["positions"].items())[:6]:
        rows.append([InlineKeyboardButton(text=f"💸 Продать {p['symbol']}", callback_data=f"mt:s:{mint}")])
    if st["positions"]:
        rows.append([InlineKeyboardButton(text="🧯 Продать всё", callback_data="mt:sa")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="mt:show"),
                 InlineKeyboardButton(text="❔ Как это работает", callback_data="mt:help")])
    url = b.app_url()
    if url:
        from aiogram.types import WebAppInfo
        rows.append([InlineKeyboardButton(text="📱 В приложении", web_app=WebAppInfo(url=url + "?open=copy"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


HOW_TEXT = """\
💸 <b>Как бот торгует мемкоинами</b>

1. <b>Кого копирует.</b> Только кошельки, которые сначала заработали ⭐ в копитрейдинге без денег: от 5 копий
   и в среднем от +2% на копию с учётом опоздания и комиссий (/copy). Можно расширить до «👀 хороших» —
   кошельков в плюсе по своей истории, но ещё без ⭐ (рискованнее).
2. <b>Перед покупкой</b>: риск-фильтр копитрейдинга (ликвидность, RugCheck, возраст, переплата) и проверка
   «туда-обратно» через Jupiter: если токен нельзя продать — это ловушка, бот не покупает; если вход+выход
   дороже 6% — тоже нет.
3. <b>Покупка</b> через Jupiter (лучший маршрут по всем DEX), на сумму из настроек.
4. <b>Продажа</b>: кошелёк продал · стоп −40% по котировке Jupiter · пул слили · 48 часов. Пустой счёт
   токена бот закрывает и возвращает залог ~0,002 SOL.
5. <b>Тормоза</b>: до 2 позиций, запас SOL на комиссии, дневной лимит убытка, пауза 12 ч после 3 убытков подряд.

Вручную: <code>/meme buy АДРЕС_ТОКЕНА 2</code> — купить на $2 (адрес — из DexScreener/GMGN), 
<code>/meme sell СИМВОЛ</code> — продать, <code>/meme sell all</code> — продать всё.
Все сделки видны в Phantom → аккаунт «Бот» и на Solscan."""


async def send_events(b: ArbitrageBot, events: list[dict]) -> None:
    from .copy_ui import send_events as send
    await send(b, events)


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    async def owner_only(uid: int | None, chat_id: int) -> bool:
        if b.owner_id is None or uid != b.owner_id:
            await b._send(chat_id, "⛔ Торговать может только владелец бота.")
            return False
        return True

    async def show(chat_id: int) -> None:
        await b.meme.balance(max_age=20)
        await b._send(chat_id, b.meme.text(), keyboard(b))

    async def do_buy(chat_id: int, mint: str, usd: float) -> None:
        mt = b.meme
        await b._send(chat_id, f"⏳ Проверяю токен и покупаю на ${usd:g}… (до 30 секунд)")
        symbol = ""
        try:
            pair = (await b.copy._pairs([mint])).get(mint)
            symbol = pair.symbol if pair else ""
        except Exception:
            pass
        try:
            e = await mt.buy(mint, usd, source="manual", symbol=symbol)
        except (SwapError, WalletError) as err:
            await b._send(chat_id, f"⚠️ Не купил: {html.escape(str(err))[:300]}")
            return
        b.save_copy()
        await send_events(b, [e])

    async def do_sell(chat_id: int, mint: str) -> None:
        if mint not in b.meme.st["positions"]:
            await b._send(chat_id, "Этой позиции уже нет.", keyboard(b))
            return
        await b._send(chat_id, "⏳ Продаю…")
        e = await b.meme.sell(mint, "вручную")
        b.save_copy()
        if e:
            await send_events(b, [e])
        await b._backup_now()

    @dp.message(Command("meme", "memes", "solbot"))
    async def cmd_meme(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        args = (command.args or "").split()
        if not args:
            await show(message.chat.id)
            return
        uid = message.from_user.id if message.from_user else None
        if not await owner_only(uid, message.chat.id):
            return
        cmd = args[0].lower()
        if cmd in ("buy", "купить") and len(args) >= 2:
            m = ADDR_IN_TEXT.search(args[1])
            mint = m.group(0) if m else ""
            if not valid_address(mint):
                await message.answer("⚠️ Нужен адрес токена (mint) — скопируй его в DexScreener или GMGN.")
                return
            try:
                usd = float(args[2].replace(",", ".").lstrip("$")) if len(args) >= 3 else b.meme.st["size_usd"]
            except ValueError:
                usd = b.meme.st["size_usd"]
            await do_buy(message.chat.id, mint, usd)
        elif cmd in ("sell", "продать") and len(args) >= 2:
            if args[1].lower() in ("all", "всё", "все"):
                await message.answer("⏳ Продаю всё…")
                events = await b.meme.sell_all("продать всё")
                b.save_copy()
                await send_events(b, events or [{"text": "Позиций нет.", "buttons": []}])
                return
            mint = b.meme.find(args[1])
            if not mint:
                await message.answer("Такой позиции нет. Список: /meme")
                return
            await do_sell(message.chat.id, mint)
        elif cmd in ("on", "off"):
            if cmd == "on":
                await b._send(message.chat.id, confirm_text(b), confirm_kb())
            else:
                b.meme.st["on"] = False
                b.save_copy()
                await show(message.chat.id)
        elif cmd == "size" and len(args) >= 2:
            try:
                b.meme.st["size_usd"] = max(0.5, min(float(args[1].replace(",", ".").lstrip("$")), 50.0))
            except ValueError:
                pass
            b.save_copy()
            await show(message.chat.id)
        else:
            await b._send(message.chat.id, HOW_TEXT if b.meme.configured else SETUP_TEXT, keyboard(b))

    @dp.callback_query(F.data.startswith("mt:"))
    async def cb_meme(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        parts = cb.data.split(":")
        action = parts[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        await cb.answer()
        mt = b.meme
        if action == "show":
            await show(chat_id)
            return
        if action == "help":
            await b._send(chat_id, HOW_TEXT if mt.configured else SETUP_TEXT, keyboard(b))
            return
        if not await owner_only(cb.from_user.id, chat_id):
            return
        if not mt.configured:
            await b._send(chat_id, mt.text(), keyboard(b))
            return
        if action == "on":
            await b._send(chat_id, confirm_text(b), confirm_kb())
        elif action == "on2":
            mt.st["on"] = True
            note = ""
            if not b.copy.on:
                b.copy.st["on"] = True
                note = "\n🐋 Копитрейдинг тоже включил: без него нет ⭐-кошельков."
            b.save_copy()
            stars = sum(w["status"] == "star" for w in b.copy.st["wallets"].values())
            await b._send(chat_id, "🟢 <b>Реальное автокопирование включено.</b>" + note
                          + ("" if stars else "\nПока ⭐-кошельков нет: они появятся, когда копии без денег докажут "
                             "прибыль (от 5 сделок, обычно несколько дней). До этого бот не купит ничего — так и задумано."),
                          keyboard(b))
            await b._backup_now()
        elif action == "off":
            mt.st["on"] = False
            b.save_copy()
            await b._send(chat_id, "⏸ Автокопирование выключено. Открытые позиции бот продолжает вести (стоп, "
                                   "продажа вслед за кошельком). Продать сразу — кнопками ниже.", keyboard(b))
        elif action == "size" and len(parts) > 2:
            mt.st["size_usd"] = float(parts[2])
            b.save_copy()
            await show(chat_id)
        elif action == "scope":
            mt.st["scope"] = "good" if mt.st["scope"] == "star" else "star"
            b.save_copy()
            await b._send(chat_id, "Копирую только ⭐ (проверенные копиями без денег)." if mt.st["scope"] == "star" else
                          "Копирую ⭐ и 👀 кошельки в плюсе по своей истории. ⚠️ Это рискованнее: их копии ещё не "
                          "доказали прибыль.", keyboard(b))
        elif action == "maxopen":
            mt.st["max_open"] = {1: 2, 2: 3, 3: 1}.get(mt.st["max_open"], 2)
            b.save_copy()
            await show(chat_id)
        elif action == "s" and len(parts) > 2:
            await do_sell(chat_id, parts[2])
        elif action == "sa":
            await b._send(chat_id, f"🧯 Продать все позиции ({len(mt.st['positions'])}) прямо сейчас?",
                          InlineKeyboardMarkup(inline_keyboard=[[
                              InlineKeyboardButton(text="✅ Да, продать всё", callback_data="mt:sa2"),
                              InlineKeyboardButton(text="Отмена", callback_data="mt:show")]]))
        elif action == "sa2":
            await b._send(chat_id, "⏳ Продаю всё…")
            events = await mt.sell_all("продать всё")
            b.save_copy()
            await send_events(b, events or [{"text": "Позиций нет.", "buttons": []}])
        elif action == "b" and len(parts) > 2:
            await do_buy(chat_id, parts[2], mt.st["size_usd"])


def confirm_text(b: ArbitrageBot) -> str:
    st = b.meme.st
    return CONFIRM_TEXT.format(stop=st["stop_pct"], size=st["size_usd"], max_open=st["max_open"],
                               reserve=st["reserve_sol"], loss=st["daily_loss_usd"])


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, торговать реальными SOL", callback_data="mt:on2")],
        [InlineKeyboardButton(text="Отмена", callback_data="mt:show")]])


async def guard_secret(b: ArbitrageBot, message: Message) -> None:
    """Человек прислал ключ/сид-фразу: удаляем и объясняем. ИИ это сообщение не увидит."""
    try:
        await message.delete()
    except Exception as e:
        log.info("Не смог удалить сообщение с ключом: %s", e)
    await b._send(message.chat.id, LEAK_TEXT)
