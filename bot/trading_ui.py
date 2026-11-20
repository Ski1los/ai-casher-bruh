"""Telegram-интерфейс торговли: команды, кнопки подтверждения, панель, ордера ИИ-агента.

Торговать может ТОЛЬКО владелец бота (первый ID в ALLOWED_USER_IDS).
"""

from __future__ import annotations

import html
import logging
import secrets
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .ai import OrderIntent
from .cloud import version_text
from .trading import (KEY_PAGES, OrderPlan, TradeError, fmt_amount, human_error,
                      key_trade_problem)
from .triangular import TriOpportunity

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

TRI_BUTTONS = 3  # сколько кнопок «Исполнить» под сигналами
REFS_KEEP = 60

KEYS_HELP = """\
🔑 <b>Как подключить биржу для торговли</b>

1. Открой управление API <b>на сайте</b> биржи (в браузере телефона: «версия для ПК»): {links}.
2. Создай <b>системный API-ключ</b> (System-generated / HMAC).
3. Права: включи только <b>Чтение + Спот-торговлю</b> (Read + Spot Trade).
   ❗ <b>Вывод (Withdraw) НЕ включай.</b> Тогда даже при утечке ключа деньги нельзя вывести.
4. IP-ограничение не ставь: у бесплатного Render адрес сервера не твой личный. Особенности:
   • <b>Bybit</b>: ключ без IP живёт ~3 месяца. Бот покажет дату и напомнит за 2 недели (/doctor).
   • <b>BingX</b>: ключ без IP удаляется, если им 14 дней не пользовались. Бот обращается к бирже \
каждые 15 минут, так что, пока бот работает, ключ живой.
5. Скопируй ключ и секрет.
6. Render → сервис <b>ai-casher-bot</b> → <b>Environment</b> → <b>Add Environment Variable</b>:
   <code>BYBIT_API_KEY</code> = ключ, <code>BYBIT_API_SECRET</code> = секрет
   <code>BINGX_API_KEY</code> = ключ, <code>BINGX_API_SECRET</code> = секрет
   → <b>Save Changes</b>. Бот перезапустится примерно за минуту.
7. Здесь отправь /keys (или /doctor для полной проверки): бот проверит ключ, права и покажет баланс.
8. Деньги для торговли должны лежать на <b>торговом</b> счёте: у Bybit это Unified Trading, у BingX Spot. \
Если пополнил «Funding / Fund», сделай внутренний перевод (бесплатно). /doctor подскажет, если деньги не там.

Ключи никогда не присылай в чат, только в Render.
Сначала бот работает в <b>тестовом режиме</b>: считает всё по реальному балансу и стаканам, но ордера не отправляет.
Реальные ордера включаются командой /live."""


def plan_keyboard(plan: OrderPlan, live: bool) -> InlineKeyboardMarkup:
    label = "🔴 Подтвердить (реально)" if live else "🧪 Выполнить тест"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"ordok:{plan.id}"),
        InlineKeyboardButton(text="✖️ Отмена", callback_data=f"ordno:{plan.id}"),
    ]])


def panel_keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.trader.state
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Балансы", callback_data="tr:bal"),
         InlineKeyboardButton(text="📋 Ордера", callback_data="tr:orders")],
        [InlineKeyboardButton(text="⏸ Выключить автоторговлю" if st.auto else "⚙️ Включить автоторговлю",
                              callback_data="tr:auto")],
        [InlineKeyboardButton(text="🧪 Вернуть тестовый режим" if st.live else "🔴 Включить реальный режим",
                              callback_data="tr:live")],
        [InlineKeyboardButton(text="🔍 Почему нет сделок", callback_data="tr:why")],
        [InlineKeyboardButton(text="📜 Сделки", callback_data="tr:log"),
         InlineKeyboardButton(text="🔑 Проверить ключи", callback_data="tr:keys")],
        [InlineKeyboardButton(text="🛑 СТОП: всё остановить", callback_data="tr:stop")],
    ])


LIVE_WARNING = """\
🔴 <b>Включить реальную торговлю?</b>

После этого ордера будут уходить на биржу с настоящими деньгами:
• лимит на одну покупку или цикл: <b>${max_trade:g}</b> (<code>/set max_trade</code>);
• не больше <b>{daily:g}</b> сделок в сутки;
• автоторговля встанет на паузу, если за сутки потеряет <b>${loss:g}</b>.

Честно: прибыль не гарантирована. Треугольники живут доли секунды, и в реальности часть сделок \
будет в минус из-за проскальзывания. Ставь только те деньги, которые не жалко потерять.
Остановить всё в любой момент: /stop"""


def live_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔴 Да, торговать реально", callback_data="live:yes"),
        InlineKeyboardButton(text="✖️ Нет", callback_data="dismiss"),
    ]])


def tri_rows(b: ArbitrageBot, triangles: list[TriOpportunity]) -> list[list[InlineKeyboardButton]]:
    """Кнопки «⚡ Исполнить» под треугольниками на биржах с рабочими ключами."""
    if b.owner_id is None:
        return []
    ok = set(b.trader.working)
    rows = []
    mode = "" if b.trader.state.live else " (тест)"
    for t in triangles:
        if t.exchange not in ok or not t.depth or not t.depth.filled:
            continue
        token = secrets.token_hex(4)
        b.tri_refs[token] = t
        path = " → ".join(t.path[1:-1])
        rows.append([InlineKeyboardButton(
            text=f"⚡ Исполнить {path} на ${b.settings.max_trade_usd:g}{mode}", callback_data=f"tx:{token}"
        )])
        if len(rows) >= TRI_BUTTONS:
            break
    while len(b.tri_refs) > REFS_KEEP:
        b.tri_refs.pop(next(iter(b.tri_refs)))
    return rows


async def plans_from_intents(b: ArbitrageBot, intents: list[OrderIntent]) -> tuple[str, list[list[InlineKeyboardButton]]]:
    """Ордера, которые предложил ИИ → предпросмотры с кнопками подтверждения."""
    blocks, rows = [], []
    for it in intents:
        try:
            name = b.trader.resolve_exchange(it.exchange)
            if it.price:
                plan = await b.trader.plan_limit(name, it.side, it.coin, it.amount, it.price, source="ai")
            else:
                plan = await b.trader.plan_market(name, it.side, it.coin, it.amount, source="ai")
        except TradeError as e:
            blocks.append(f"❌ Не смог подготовить ордер ({html.escape(it.side)} {html.escape(it.coin)}): {html.escape(str(e))}")
            continue
        except Exception as e:
            blocks.append(f"❌ Не смог подготовить ордер: {html.escape(human_error(e))}")
            continue
        text = b.trader.plan_text(plan)
        if it.reason:
            text += f"\n<i>Почему: {html.escape(it.reason)}</i>"
        blocks.append(text)
        rows += plan_keyboard(plan, b.trader.state.live).inline_keyboard
    return "\n\n".join(blocks), rows


async def keys_text(b: ArbitrageBot) -> str:
    links = ", ".join(f'<a href="{u}">{html.escape(b.scanner.display_name(n))}</a>'
                      for n, u in KEY_PAGES.items() if n in b.scanner.exchanges)
    if not b.trader.creds:
        return KEYS_HELP.format(links=links)
    status = await b.trader.check_keys()
    lines = ["🔑 <b>Проверка ключей</b>"]
    try:
        await b.trader.inspect_keys()
    except Exception:
        pass
    for name, st in status.items():
        icon = "✅ работает" if st == "ok" else f"❌ {st}"
        problem = key_trade_problem(name, b.trader.key_info.get(name)) if st == "ok" else None
        if problem:
            icon = f"⚠️ подключается, но торговать не сможет: {problem}"
        lines.append(f"{html.escape(b.scanner.display_name(name))}: {html.escape(icon)}")
    if any(st == "ok" for st in status.values()):
        lines.append("\nБалансы: /balance · панель торговли: /trade")
    for n in b.scanner.exchanges:
        if n not in b.trader.creds:
            lines.append(f"\n{html.escape(b.scanner.display_name(n))} не подключена: добавь <code>{n.upper()}_API_KEY</code> "
                         f"и <code>{n.upper()}_API_SECRET</code> в Render → Environment.")
    return "\n".join(lines)


async def preflight(b: ArbitrageBot) -> list[str]:
    """Что помешает автоторговле, ещё до первой сделки: права ключей и деньги на торговом счёте."""
    t = b.trader
    out: list[str] = []
    try:
        await t.inspect_keys()
    except Exception:
        pass
    for name in t.working:
        title = b.scanner.display_name(name)
        problem = key_trade_problem(name, t.key_info.get(name))
        if problem:
            out.append(f"{title}: {problem}" + ("" if t.state.live else " (в тесте не мешает, в реальном режиме ордера не пройдут)"))
        try:
            free = await t.free(name, b.settings.quote)
        except Exception:
            continue
        need = t.min_start_usd(name)
        if free < need:
            funding = await t.funding_usd(name)
            if funding and funding >= 1:
                out.append(t._funding_text({name: funding}))
            else:
                out.append(f"{title}: на торговом счёте {free:.2f} {b.settings.quote}, а для цикла нужно от ${need:.2f} "
                           f"(биржа принимает ордер через API от $5 на каждый из трёх шагов). Пополни до $10–20"
                           + ("" if t.state.live else " (в тесте не мешает)"))
    return out


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    async def owner_only(message: Message) -> bool:
        if not b._allowed(message):
            return False
        uid = message.from_user.id if message.from_user else None
        if b.owner_id is None:
            await message.answer("Торговля доступна только владельцу. Заполни ALLOWED_USER_IDS в Render.")
            return False
        if uid != b.owner_id:
            await message.answer("⛔ Торговать может только владелец бота.")
            return False
        return True

    async def cb_owner(cb: CallbackQuery) -> bool:
        if b.owner_id is None or cb.from_user.id != b.owner_id:
            await cb.answer("Торговать может только владелец", show_alert=True)
            return False
        return True

    def chat_of(cb: CallbackQuery) -> int:
        return cb.message.chat.id if cb.message else cb.from_user.id

    async def after_change() -> None:
        b.save_trading()
        await b._backup_now()

    async def show_plan(chat_id: int, make_plan) -> None:
        try:
            plan = await make_plan()
        except TradeError as e:
            await b._send(chat_id, f"❌ {html.escape(str(e))}")
            return
        except Exception as e:
            log.exception("Ошибка подготовки ордера")
            await b._send(chat_id, f"❌ {html.escape(human_error(e))}")
            return
        await b._send(chat_id, b.trader.plan_text(plan), plan_keyboard(plan, b.trader.state.live))

    # --- команды ------------------------------------------------------------

    @dp.message(Command("trade"))
    async def cmd_trade(message: Message) -> None:
        if await owner_only(message):
            await b._answer(message, b.trader.panel_text(), panel_keyboard(b))

    @dp.message(Command("keys"))
    async def cmd_keys(message: Message) -> None:
        if await owner_only(message):
            await b._answer(message, await keys_text(b))

    @dp.message(Command("balance"))
    async def cmd_balance(message: Message) -> None:
        if await owner_only(message):
            text = await b.trader.all_balances_text()
            if b.phantom.address:
                text += "\n\n" + b.phantom.text(await b.phantom_snapshot())
            await b._answer(message, text)

    @dp.message(Command("buy", "sell"))
    async def cmd_buy_sell(message: Message, command: CommandObject) -> None:
        if not await owner_only(message):
            return
        side = command.command.lower()
        args = (command.args or "").split()
        if len(args) != 3:
            example = "<code>/buy bybit BTC 5</code> — купить BTC на 5 USDT" if side == "buy" else \
                "<code>/sell bybit BTC all</code> — продать всё (или число, или <code>50%</code>)"
            await message.answer(f"Формат: <code>/{side} биржа МОНЕТА {'USDT' if side == 'buy' else 'сколько'}</code>\n{example}")
            return

        async def make():
            name = b.trader.resolve_exchange(args[0])
            return await b.trader.plan_market(name, side, args[1], args[2])

        await show_plan(message.chat.id, make)

    @dp.message(Command("limit"))
    async def cmd_limit(message: Message, command: CommandObject) -> None:
        if not await owner_only(message):
            return
        args = (command.args or "").split()
        if len(args) != 5 or args[1].lower() not in ("buy", "sell"):
            await message.answer(
                "Формат: <code>/limit биржа buy|sell МОНЕТА сколько цена</code>\n"
                "<code>/limit bybit buy BTC 5 60000</code> — купить BTC на 5 USDT по 60000\n"
                "<code>/limit bybit sell BTC all 70000</code> — продать всё по 70000"
            )
            return

        async def make():
            name = b.trader.resolve_exchange(args[0])
            try:
                price = float(args[4].replace(",", "."))
            except ValueError:
                raise TradeError("цена должна быть числом") from None
            return await b.trader.plan_limit(name, args[1].lower(), args[2], args[3], price)

        await show_plan(message.chat.id, make)

    async def orders_view(chat_id: int) -> None:
        if not b.trader.working:
            await b._send(chat_id, "Нет бирж с рабочими ключами: /keys")
            return
        lines, rows = ["📋 <b>Открытые ордера</b>"], []
        for name in b.trader.working:
            title = html.escape(b.scanner.display_name(name))
            try:
                orders = await b.trader.open_orders(name)
            except TradeError as e:
                lines.append(f"{title}: ❌ {html.escape(str(e))}")
                continue
            for o in orders[:10]:
                token = secrets.token_hex(4)
                b.order_refs[token] = (name, o["id"], o["symbol"])
                side = "покупка" if o.get("side") == "buy" else "продажа"
                lines.append(
                    f"{title}: {side} {html.escape(o['symbol'])} {fmt_amount(float(o.get('amount') or 0))} "
                    f"по {float(o.get('price') or 0):.8g}, исполнено {fmt_amount(float(o.get('filled') or 0))}"
                )
                rows.append([InlineKeyboardButton(text=f"✖️ Отменить {o['symbol']} ({title})", callback_data=f"cx:{token}")])
        while len(b.order_refs) > REFS_KEEP:
            b.order_refs.pop(next(iter(b.order_refs)))
        if len(lines) == 1:
            lines.append("Открытых ордеров нет.")
        await b._send(chat_id, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

    @dp.message(Command("orders"))
    async def cmd_orders(message: Message) -> None:
        if await owner_only(message):
            await orders_view(message.chat.id)

    @dp.message(Command("live"))
    async def cmd_live(message: Message) -> None:
        if not await owner_only(message):
            return
        if not b.trader.working:
            await b._answer(message, "Сначала подключи ключ биржи: /keys")
            return
        s = b.settings
        await b._answer(message, LIVE_WARNING.format(max_trade=s.max_trade_usd, daily=s.daily_trades, loss=s.daily_loss_usd),
                        live_keyboard())

    @dp.message(Command("test"))
    async def cmd_test(message: Message) -> None:
        if await owner_only(message):
            b.trader.state.live = False
            await after_change()
            await message.answer("🧪 Тестовый режим: ордера больше не отправляются на биржу.")

    async def toggle_auto(chat_id: int, on: bool | None = None) -> None:
        st = b.trader.state
        on = (not st.auto) if on is None else on
        if on and not b.trader.working:
            await b._send(chat_id, "Для автоторговли нужен рабочий ключ биржи: /keys")
            return
        if on and not b.settings.triangular_enabled:
            await b._send(chat_id, "Треугольный арбитраж выключен (TRIANGULAR=0), автоторговать нечем.")
            return
        was_on = st.auto
        st.auto = on
        if on:
            st.paused_reason = None
            if not was_on:
                b.trader.reset_auto_stats()
                b._auto_reports = 0
                b._auto_warned.clear()
        await after_change()
        if on:
            mode = ("🔴 РЕАЛЬНО, с настоящими деньгами" if st.live
                    else "🧪 в ТЕСТЕ: буду присылать, что исполнил бы, ордера не отправляются")
            warnings = await preflight(b)
            await b._send(
                chat_id,
                f"⚙️ Автоторговля включена, {mode}.\n"
                f"Беру треугольники на биржах с ключами ({', '.join(b.trader.working)}), если прибыль после комиссий "
                f"≥ {b.settings.auto_min_profit_percent:g}% (<code>/set auto_min</code>), до ${b.settings.max_trade_usd:g} за цикл. "
                f"Перед сделкой стаканы запрашиваются заново. Скан каждые {b.settings.scan_interval_sec:g} с.\n"
                "Пропуски не сыплются в чат: что происходит и почему, смотри в /why, "
                f"а сводка придёт через час.\n"
                + ("".join(f"\n⚠️ {html.escape(w)}" for w in warnings) + ("\n" if warnings else ""))
                + "Выключить: /autotrade off · остановить всё: /stop",
            )
        else:
            await b._send(chat_id, "⏸ Автоторговля выключена.")

    @dp.message(Command("why", "autostatus"))
    async def cmd_why(message: Message) -> None:
        if await owner_only(message):
            await b._answer(message, b.trader.auto_status_text() + f"\n\n<i>Версия бота: {version_text()}</i>")

    @dp.message(Command("autotrade"))
    async def cmd_autotrade(message: Message, command: CommandObject) -> None:
        if not await owner_only(message):
            return
        arg = (command.args or "").strip().lower()
        on = True if arg in ("on", "1", "вкл", "да") else False if arg in ("off", "0", "выкл", "нет") else None
        await toggle_auto(message.chat.id, on)

    @dp.message(Command("stop"))
    async def cmd_stop(message: Message) -> None:
        if await owner_only(message):
            text = await b.trader.panic()
            await after_change()
            await b._answer(message, text)

    @dp.message(Command("trades"))
    async def cmd_trades(message: Message) -> None:
        if await owner_only(message):
            await b._answer(message, b.trader.log_text())

    # --- кнопки -------------------------------------------------------------

    async def drop_buttons(cb: CallbackQuery) -> None:
        if cb.message is not None:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

    @dp.callback_query(F.data.startswith("ordok:"))
    async def cb_order_ok(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        await cb.answer("Выполняю…")
        await drop_buttons(cb)
        plan_id = cb.data.split(":", 1)[1]
        try:
            rec = await b.trader.execute_plan(plan_id)
        except TradeError as e:
            await b._send(chat_of(cb), f"❌ Не выполнено: {html.escape(str(e))}")
            return
        except Exception as e:
            log.exception("Ошибка исполнения ордера")
            await b._send(chat_of(cb), f"❌ Не выполнено: {html.escape(human_error(e))}")
            return
        await after_change()
        await b._send(chat_of(cb), b.trader.record_text(rec))

    @dp.callback_query(F.data.startswith("ordno:"))
    async def cb_order_no(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        b.trader.pending.pop(cb.data.split(":", 1)[1], None)
        await cb.answer("Отменено")
        await drop_buttons(cb)

    @dp.callback_query(F.data.startswith("tx:"))
    async def cb_tri(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        opp = b.tri_refs.pop(cb.data.split(":", 1)[1], None)
        if opp is None:
            await cb.answer("Сигнал устарел, запусти /scan", show_alert=True)
            return
        await cb.answer("Проверяю свежие стаканы…")
        try:
            res = await b.trader.execute_triangle(opp, source="manual")
        except TradeError as e:
            await b._send(chat_of(cb), f"❌ {html.escape(str(e))}")
            return
        await after_change()
        await b._send(chat_of(cb), res.text(b.scanner.display_name))

    @dp.callback_query(F.data.startswith("cx:"))
    async def cb_cancel(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        ref = b.order_refs.pop(cb.data.split(":", 1)[1], None)
        if ref is None:
            await cb.answer("Список устарел, открой /orders заново", show_alert=True)
            return
        try:
            await b.trader.cancel(*ref)
        except TradeError as e:
            await cb.answer(str(e)[:190], show_alert=True)
            return
        await cb.answer("Ордер отменён ✅")
        await b._send(chat_of(cb), f"✖️ Ордер {html.escape(ref[2])} на {html.escape(b.scanner.display_name(ref[0]))} отменён.")

    @dp.callback_query(F.data == "live:yes")
    async def cb_live(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        b.trader.state.live = True
        b.trader.pending.clear()  # старые тестовые предпросмотры не должны исполниться реально
        await after_change()
        await cb.answer("Реальный режим включён")
        await drop_buttons(cb)
        warnings = await preflight(b)
        await b._send(chat_of(cb), "🔴 <b>Реальный режим включён.</b> Ордера после подтверждения уходят на биржу.\n"
                                   + "".join(f"⚠️ {html.escape(w)}\n" for w in warnings)
                                   + "Вернуть тест: /test · остановить всё: /stop")

    @dp.callback_query(F.data.startswith("tr:"))
    async def cb_panel(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        action = cb.data.split(":", 1)[1]
        chat_id = chat_of(cb)
        await cb.answer()
        if action == "bal":
            await b._send(chat_id, await b.trader.all_balances_text())
        elif action == "orders":
            await orders_view(chat_id)
        elif action == "auto":
            await toggle_auto(chat_id)
        elif action == "live":
            if b.trader.state.live:
                b.trader.state.live = False
                await after_change()
                await b._send(chat_id, "🧪 Тестовый режим: ордера больше не отправляются на биржу.")
            elif not b.trader.working:
                await b._send(chat_id, "Сначала подключи ключ биржи: /keys")
            else:
                s = b.settings
                await b._send(chat_id, LIVE_WARNING.format(max_trade=s.max_trade_usd, daily=s.daily_trades,
                                                           loss=s.daily_loss_usd), live_keyboard())
        elif action == "log":
            await b._send(chat_id, b.trader.log_text())
        elif action == "why":
            await b._send(chat_id, b.trader.auto_status_text() + f"\n\n<i>Версия бота: {version_text()}</i>")
        elif action == "keys":
            await b._send(chat_id, await keys_text(b))
        elif action == "stop":
            text = await b.trader.panic()
            await after_change()
            await b._send(chat_id, text)
