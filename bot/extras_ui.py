"""Полезные мелочи: ценовые алерты, сравнение цены по биржам, кошелёк Phantom, проверка всего."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .alerts import AlertError, alerts_text, build_alert, fired_text, fmt_price, parse_alert
from .doctor import check_ai, doctor_text, run_doctor
from .solana import WalletError, short

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)
PRICE_TIMEOUT = 15


async def _ticker(b: ArbitrageBot, name: str, coin: str) -> dict | None:
    ex = b.scanner.exchanges.get(name)
    if ex is None:
        return None
    symbol = f"{coin}/{b.settings.quote}"
    try:
        await asyncio.wait_for(b.scanner._ensure_markets(name, ex), PRICE_TIMEOUT)
        if symbol not in (ex.markets or {}):
            return None
        return await asyncio.wait_for(ex.fetch_ticker(symbol), PRICE_TIMEOUT)
    except Exception as e:
        log.debug("Цена %s на %s: %s", symbol, name, e)
        return None


def _mid(t: dict | None) -> float | None:
    if not t:
        return None
    bid, ask, last = t.get("bid"), t.get("ask"), t.get("last")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return float(last or bid or ask or 0) or None


async def price_on(b: ArbitrageBot, name: str, coin: str) -> float | None:
    if coin in ("USDT", "USDC") and coin == b.settings.quote:
        return 1.0
    return _mid(await _ticker(b, name, coin))


async def prices_everywhere(b: ArbitrageBot, coin: str) -> list[tuple[str, dict]]:
    names = list(b.scanner.exchanges)
    tickers = await asyncio.gather(*(_ticker(b, n, coin) for n in names))
    return [(n, t) for n, t in zip(names, tickers) if _mid(t)]


async def public_prices(b: ArbitrageBot, coins: list[str]) -> dict[str, float]:
    """Цены в $ для списка монет: с первой биржи, где монета торгуется (для кошелька Phantom)."""
    out = {c: 1.0 for c in coins if c in ("USDT", "USDC")}
    order = list(b.scanner.exchanges)
    alias = {"MSOL": None, "JITOSOL": None}  # ликвид-стейкинг: на спотовых биржах обычно нет

    async def one(coin: str) -> None:
        if coin in out or alias.get(coin, coin) is None:
            return
        for name in order[:3]:
            price = await price_on(b, name, coin)
            if price:
                out[coin] = price
                return

    await asyncio.gather(*(one(c) for c in coins[:20]))
    return out


async def public_candles(b: ArbitrageBot, coin: str, since_ms: int) -> list[list[float]]:
    """Дневные свечи [ts_ms, open, high, low, close, vol] с первой биржи, где есть монета (для 📜 истории прогнозов)."""
    symbol = f"{coin}/{b.settings.quote}"
    for name in list(b.scanner.exchanges)[:3]:
        ex = b.scanner.exchanges[name]
        try:
            await asyncio.wait_for(b.scanner._ensure_markets(name, ex), PRICE_TIMEOUT)
            if symbol not in (ex.markets or {}):
                continue
            rows = await asyncio.wait_for(ex.fetch_ohlcv(symbol, "1d", since=int(since_ms), limit=200), PRICE_TIMEOUT)
            if rows:
                return [list(map(float, r[:6])) for r in rows if r and r[1] and r[4]]
        except Exception as e:
            log.debug("Свечи %s на %s: %s", symbol, name, e)
    return []


async def first_exchange_with(b: ArbitrageBot, coin: str, preferred: str | None = None) -> tuple[str, float] | None:
    names = [preferred] if preferred else list(b.scanner.exchanges)
    for name in names:
        price = await price_on(b, name, coin)
        if price:
            return name, price
    return None


def price_text(b: ArbitrageBot, coin: str, rows: list[tuple[str, dict]]) -> str:
    if not rows:
        return f"Не нашёл {html.escape(coin)}/{b.settings.quote} ни на одной бирже из списка."
    rows = sorted(rows, key=lambda r: _mid(r[1]))
    lines = [f"💲 <b>{html.escape(coin)}/{b.settings.quote}</b> на биржах"]
    for name, t in rows:
        bid, ask = t.get("bid"), t.get("ask")
        chg = t.get("percentage")
        chg_txt = f" · {float(chg):+.2f}% за сутки" if chg is not None else ""
        book = f" (купить {fmt_price(float(ask))} / продать {fmt_price(float(bid))})" if bid and ask else ""
        lines.append(f"• {html.escape(b.scanner.display_name(name))}: <b>{fmt_price(_mid(t))}</b>{book}{chg_txt}")
    if len(rows) >= 2:
        cheap, dear = rows[0], rows[-1]
        ask = float(cheap[1].get("ask") or _mid(cheap[1]))
        bid = float(dear[1].get("bid") or _mid(dear[1]))
        gap = (bid / ask - 1) * 100
        verdict = ("перевод между биржами съест такую разницу комиссиями" if gap < 1.5
                   else "разница заметная, проверь сеть и комиссию вывода, прежде чем переводить")
        lines.append(f"\nКупить на {b.scanner.display_name(cheap[0])} и продать на {b.scanner.display_name(dear[0])}: "
                     f"{gap:+.2f}% до комиссий. {verdict}.")
    return "\n".join(lines)


def alerts_keyboard(items) -> InlineKeyboardMarkup | None:
    if not items:
        return None
    rows = [[InlineKeyboardButton(text=f"✖ {a.symbol_text}", callback_data=f"al:del:{a.id}")] for a in items[:20]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    @dp.message(Command("price", "p"))
    async def cmd_price(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        coin = (command.args or "").strip().upper().split("/")[0]
        if not coin:
            await message.answer("Пример: <code>/price TON</code>: цена монеты на всех биржах сразу")
            return
        rows = await prices_everywhere(b, coin)
        await b._answer(message, price_text(b, coin, rows))

    @dp.message(Command("alert"))
    async def cmd_alert(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        args = (command.args or "").strip()
        chat_id = message.chat.id
        if not args:
            items = b.alerts.for_chat(chat_id)
            await b._answer(message, alerts_text(items, b.scanner.display_name), alerts_keyboard(items))
            return
        try:
            spec = parse_alert(args, list(b.scanner.exchanges))
            found = await first_exchange_with(b, spec.coin, spec.exchange)
            if not found:
                where = b.scanner.display_name(spec.exchange) if spec.exchange else "биржах из списка"
                raise AlertError(f"не нашёл {spec.coin}/{b.settings.quote} на {where}")
            name, current = found
            alert = build_alert(spec, current, name, chat_id)
            b.alerts.add(alert)
        except AlertError as e:
            await message.answer(f"❌ {html.escape(str(e))}")
            return
        await b.save_extras()
        await message.answer(
            f"🔔 Готово: напишу, когда <b>{alert.symbol_text}</b> на {b.scanner.display_name(name)}.\n"
            f"Сейчас {fmt_price(current)}. Все алерты: /alerts"
        )

    @dp.message(Command("alerts"))
    async def cmd_alerts(message: Message) -> None:
        if not b._allowed(message):
            return
        items = b.alerts.for_chat(message.chat.id)
        await b._answer(message, alerts_text(items, b.scanner.display_name), alerts_keyboard(items))

    @dp.callback_query(lambda c: (c.data or "").startswith("al:del:"))
    async def cb_alert_del(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        removed = b.alerts.remove(cb.data.split(":", 2)[2], chat_id)
        await b.save_extras()
        await cb.answer("Удалил" if removed else "Уже нет")
        items = b.alerts.for_chat(chat_id)
        if cb.message:
            try:
                await cb.message.edit_text(alerts_text(items, b.scanner.display_name), reply_markup=alerts_keyboard(items))
            except Exception:
                pass

    @dp.message(Command("phantom", "wallet"))
    async def cmd_phantom(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        args = (command.args or "").strip()
        if args and len(args.split()) >= 10:
            # кто-то прислал сид-фразу: не сохраняем, удаляем сообщение и предупреждаем
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(
                "🚨 Похоже, это сид-фраза. Я её НЕ сохранил и попытался удалить сообщение.\n"
                "Если она где-то засветилась, создай в Phantom новый кошелёк и переведи туда всё. "
                "Боту нужен только публичный адрес: /phantom"
            )
            return
        uid = message.from_user.id if message.from_user else None
        if args and b.owner_id is not None and not b.is_owner(uid):
            await message.answer("Менять кошелёк может только владелец бота.")
            return
        if args.lower() in ("off", "выкл", "удалить", "нет"):
            b.phantom.set_address("")
            await b.save_extras()
            await message.answer("👻 Кошелёк Phantom отключён.")
            return
        if args:
            try:
                b.phantom.set_address(args)
            except WalletError as e:
                await message.answer(f"❌ {html.escape(str(e))}")
                return
            await b.save_extras()
            await message.answer(f"👻 Кошелёк <code>{short(args)}</code> подключён (только просмотр). Загружаю баланс…")
        snap = await b.phantom_snapshot(force=bool(args))
        await b._answer(message, b.phantom.text(snap))

    async def run_check(chat_id: int, only_ai: bool = False) -> None:
        wait = await b.bot.send_message(chat_id, "🤖 Задаю короткий вопрос каждому ИИ… (до 30 секунд)" if only_ai else
                                        "🩺 Проверяю биржи, ключи, все ИИ, автомат и сервис… (до 40 секунд)")
        try:
            text = doctor_text(await check_ai(b), title="🤖 Проверка ИИ") if only_ai else doctor_text(await run_doctor(b))
        except Exception as e:
            log.exception("doctor")
            text = f"❌ Проверка упала: {html.escape(type(e).__name__)}: {html.escape(str(e)[:200])}"
        try:
            await wait.delete()
        except Exception:
            pass
        await b._send(chat_id, text, doctor_markup())

    @dp.message(Command("doctor", "check", "проверка"))
    async def cmd_doctor(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        arg = (command.args or "").strip().lower()
        await run_check(message.chat.id, only_ai=arg in ("ai", "ии", "иишки"))

    @dp.callback_query(F.data.startswith("dr:"))
    async def cb_doctor(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer("Нет доступа", show_alert=True)
            return
        await cb.answer("Проверяю…")
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        await run_check(chat_id, only_ai=cb.data == "dr:ai")


def doctor_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Проверить всё ещё раз", callback_data="dr:all"),
         InlineKeyboardButton(text="🤖 Только ИИ", callback_data="dr:ai")],
        [InlineKeyboardButton(text="➕ Добавить бесплатные ИИ", callback_data="cn:keys")],
    ])


async def check_alerts_once(b: ArbitrageBot) -> int:
    """Один проход: присылает сработавшие алерты. Возвращает, сколько сработало."""
    if not b.alerts.items:
        return 0
    fired = await b.alerts.check(lambda ex, coin: price_on(b, ex, coin))
    for alert, price in fired:
        await b._send(alert.chat_id, fired_text(alert, price, b.scanner.display_name(alert.exchange)))
    if fired:
        await b.save_extras()
    return len(fired)


async def alerts_loop(b: ArbitrageBot, every: float = 30) -> None:
    """Проверяет ценовые алерты и присылает сработавшие."""
    await asyncio.sleep(10)
    while True:
        try:
            await check_alerts_once(b)
        except Exception:
            log.exception("Ошибка проверки алертов")
        await asyncio.sleep(every)
