"""Команды для мемкоинов (DexScreener, RugCheck, радар) и Т-Инвестиций."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING

from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from .dex import DEX_HELP, SOL_ADDRESS_RE, DexError, Pair, best_pairs, is_address, pair_text
from .tinvest import TINVEST_HELP

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

RADAR_EVERY_SEC = 10 * 60
RADAR_SEEN_KEEP_SEC = 3 * 86400
RADAR_MAX_PER_MESSAGE = 3


def link_buttons(p: Pair) -> InlineKeyboardMarkup:
    from .dex import links

    lk = links(p)
    names = {"gmgn": "GMGN", "axiom": "Axiom", "dexscreener": "DexScreener"}
    row = [InlineKeyboardButton(text=names[k], url=lk[k]) for k in ("gmgn", "axiom", "dexscreener") if k in lk]
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def dex_lookup(b: ArbitrageBot, query: str) -> tuple[str, InlineKeyboardMarkup | None]:
    pairs = await b.dex.search(query)
    if is_address(query):
        pairs = [p for p in pairs if p.address.lower() == query.lower()] or pairs
    top = best_pairs(pairs, limit=1 if is_address(query) else 4)
    if not top:
        return f"Ничего не нашёл по «{html.escape(query)}» на DexScreener.", None
    first = top[0]
    rug = await b.dex.rug_safe(first.address) if first.chain == "solana" else None
    blocks = [pair_text(first, rug, detailed=True)]
    if len(top) > 1:
        blocks.append("<b>Ещё с таким названием</b> (проверь адрес! Скамеры копируют тикеры):")
        blocks += [pair_text(p, None, detailed=False) for p in top[1:]]
    return "🦎 " + "\n\n".join(blocks), link_buttons(first)


async def hot_text(b: ArbitrageBot) -> str:
    pairs = await b.dex.trending("solana", limit=8)
    if not pairs:
        return "Сейчас DexScreener не вернул трендовых токенов. Попробуй позже."
    items = [f"{i}. {pair_text(p, None, detailed=False)}" for i, p in enumerate(pairs, 1)]
    return ("🔥 <b>В тренде на Solana</b> (DexScreener: бусты и свежие профили)\n\n" + "\n\n".join(items)
            + "\n\n⚠️ «В тренде» часто значит «за продвижение заплатили». Перед покупкой: <code>/rug АДРЕС</code>")


async def rug_text(b: ArbitrageBot, mint: str) -> str:
    rug = await b.dex.rugcheck(mint)
    if rug is None:
        return "RugCheck не знает такой токен (или адрес не из сети Solana)."
    pairs = best_pairs(await b.dex.tokens("solana", [mint]), limit=1)
    head = f"🛡 <b>RugCheck</b> <code>{html.escape(mint)}</code>"
    if pairs:
        return head + "\n\n" + pair_text(pairs[0], rug, detailed=True)
    lines = [head, f"Риск: {rug['score']}/100 ({rug['label']})"]
    for r in rug["risks"][:8]:
        icon = "🔴" if r["level"] == "danger" else "🟡"
        lines.append(f"{icon} {html.escape(r['name'])}: {html.escape(r['description'])}")
    if not rug["risks"]:
        lines.append("Явных рисков RugCheck не нашёл (это не гарантия).")
    return "\n".join(lines)


def radar_item_text(p: Pair, rug: dict) -> str:
    return pair_text(p, rug, detailed=True)


async def radar_once(b: ArbitrageBot, now: float | None = None) -> int:
    """Один проход радара: шлёт подписчикам новые токены. Возвращает, сколько токенов отправлено."""
    if not b.radar_chats:
        return 0
    now = time.time() if now is None else now
    b.radar_seen = {k: t for k, t in b.radar_seen.items() if now - t < RADAR_SEEN_KEEP_SEC}
    found = [(p, r) for p, r in await b.dex.radar(now) if p.address not in b.radar_seen]
    found = found[:RADAR_MAX_PER_MESSAGE]
    if not found:
        return 0
    for p, _ in found:
        b.radar_seen[p.address] = now
    text = ("📡 <b>Радар мемкоинов</b>: новые токены Solana с ликвидностью, объёмом и нормальным RugCheck\n\n"
            + "\n\n".join(radar_item_text(p, r) for p, r in found)
            + "\n\n⚠️ Это фильтр, а не совет. Даже «зелёные» мемкоины часто падают на 50–90%. Выключить: /radar off")
    for chat_id in list(b.radar_chats):
        await b._send(chat_id, text, link_buttons(found[0][0]) if len(found) == 1 else None)
    return len(found)


async def radar_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await radar_once(b)
        except DexError as e:
            log.info("Радар: %s", e)
        except Exception:
            log.exception("Ошибка радара мемкоинов")
        await asyncio.sleep(RADAR_EVERY_SEC)


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    @dp.message(Command("dex", "token"))
    async def cmd_dex(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        query = (command.args or "").strip()
        if not query:
            await b._answer(message, DEX_HELP)
            return
        try:
            text, kb = await dex_lookup(b, query)
        except DexError as e:
            text, kb = f"❌ DexScreener: {html.escape(str(e))}", None
        await b._answer(message, text, kb)

    @dp.message(Command("hot", "trending"))
    async def cmd_hot(message: Message) -> None:
        if not b._allowed(message):
            return
        try:
            text = await hot_text(b)
        except DexError as e:
            text = f"❌ DexScreener: {html.escape(str(e))}"
        await b._answer(message, text)

    @dp.message(Command("rug", "safe"))
    async def cmd_rug(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        mint = (command.args or "").strip()
        if not SOL_ADDRESS_RE.match(mint):
            await message.answer("Пришли адрес токена Solana (mint), например из GMGN, Axiom или Phantom:\n"
                                 "<code>/rug АДРЕС</code>")
            return
        try:
            text = await rug_text(b, mint)
        except DexError as e:
            text = f"❌ RugCheck: {html.escape(str(e))}"
        await b._answer(message, text)

    @dp.message(Command("radar"))
    async def cmd_radar(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        arg = (command.args or "").strip().lower()
        chat_id = message.chat.id
        if arg in ("on", "вкл", "1", "да"):
            b.radar_chats.add(chat_id)
            await b.save_extras()
            await message.answer(
                "📡 Радар включён. Раз в 10 минут проверяю трендовые токены Solana и присылаю новые, если:\n"
                "• ликвидность ≥ $30K и объём за сутки ≥ $100K\n• возраст от 1 часа до 3 дней, есть продажи\n"
                "• RugCheck: риск ≤ 30/100 и нет «опасных» пунктов\n"
                "Выключить: /radar off")
        elif arg in ("off", "выкл", "0", "нет"):
            b.radar_chats.discard(chat_id)
            await b.save_extras()
            await message.answer("📡 Радар выключен.")
        else:
            on = chat_id in b.radar_chats
            await message.answer(f"📡 Радар мемкоинов: {'🟢 включён' if on else '⏸ выключен'}.\n"
                                 "<code>/radar on</code> · <code>/radar off</code> · разово: /hot")

    @dp.message(Command("tinvest", "ti", "broker", "tbank"))
    async def cmd_tinvest(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        uid = message.from_user.id if message.from_user else None
        if b.owner_id is not None and uid != b.owner_id:
            await message.answer("Портфель Т-Инвестиций видит только владелец бота.")
            return
        if (command.args or "").strip().lower() in ("help", "помощь", "как") or not b.tinvest.configured:
            await b._answer(message, TINVEST_HELP)
            return
        snap = await b.tinvest.get(force=True)
        await b._answer(message, b.tinvest.text(snap))
