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
from . import updater
from .cloud import version_text
from .trading import (KEY_PAGES, OrderPlan, TradeError, fmt_amount, human_error,
                      key_trade_problem)
from .scalper import STRATEGIES, is_good
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
   ❗ <b>Вывод (Withdraw) НЕ включай — он боту НЕ нужен, даже для реальной торговли.</b> \
Бот покупает и продаёт внутри биржи, деньги никуда не выводит. Без вывода даже украденный ключ не заберёт деньги.
   На <b>BingX</b> галочки такие: ✅ Чтение, ✅ Спот торговля; ⬜️ Бессрочная фьючерсная торговля, \
⬜️ Universal Transfer, ⬜️ Вывод, ⬜️ Управлять субаккаунтами, ⬜️ P2P. Доступ по IP: «Неограниченный».
   На <b>Bybit</b>: системный ключ → «Чтение и запись» → отметь торговлю (Unified Trading / Spot); Withdraw не трогай.
4. IP-ограничение не ставь: у бесплатного Render адрес сервера не твой личный. Особенности:
   • <b>Bybit</b>: ключ без IP живёт ~3 месяца. Бот покажет дату и напомнит за 2 недели (/doctor).
   • <b>BingX</b>: ключ без IP удаляется, если им 14 дней не пользовались. Бот обращается к бирже \
каждые 15 минут, так что, пока бот работает, ключ живой.
5. Скопируй ключ и секрет <b>сразу</b>: секрет биржа показывает один раз. Потерял — удали ключ и создай новый.
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
        [InlineKeyboardButton(text="🤖 Автопилот (режим $5)" + (" 🟢" if b.scalper.on else ""), callback_data="sc:show")],
        [InlineKeyboardButton(text="⏸ Выключить треугольники" if st.auto else "⚙️ Автоторговля треугольниками",
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

Право «Вывод» у ключа для этого <b>не нужно</b> и включать его не надо: хватает «Чтение + Спот торговля».

Честно: прибыль не гарантирована. Треугольники живут доли секунды, и в реальности часть сделок \
будет в минус из-за проскальзывания. Ставь только те деньги, которые не жалко потерять.
Остановить всё в любой момент: /stop"""


def scalp_keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    sc = b.scalper
    coins = sc.st["use_coins"]
    rows = [[InlineKeyboardButton(text="⏸ Выключить автопилот" if sc.on else "🤖 Включить автопилот",
                                  callback_data="sc:off" if sc.on else "ap:on")],
            [InlineKeyboardButton(text=("✅" if coins else "⬜️") + " Монеты в дело: продавать лежащие монеты в USDT",
                                  callback_data="sc:coins")],
            [InlineKeyboardButton(text="📊 Проверка на истории", callback_data="sc:bt"),
             InlineKeyboardButton(text="🧪 Лаборатория", callback_data="sc:lab")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="sc:show")]]
    if sc.positions:
        rows.append([InlineKeyboardButton(text="💸 Продать позиции сейчас", callback_data="sc:sell")])
    if not b.trader.state.live:
        rows.append([InlineKeyboardButton(text="🔴 Включить реальный режим", callback_data="tr:live")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def why_text(b: ArbitrageBot) -> str:
    sc = b.scalper
    head = ""
    if sc.on:
        head = (f"🎯 <b>Режим $5 включён</b>: {html.escape(sc.last_reason or 'запускаюсь')}. Подробно: /scalp\n\n")
    elif not b.trader.state.auto:
        head = "💡 На $5 треугольники не проходят минимум биржи. Для маленькой суммы есть режим $5: /scalp\n\n"
    if b.duplicate_running():
        head = "⚠️ <b>Работают две копии бота</b>: новые сделки на паузе. Удали лишний сервис в Render (/doctor).\n\n" + head
    return head + b.trader.auto_status_text() + f"\n\n<i>Версия бота: {version_text()}</i>"


async def scalp_preflight(b: ArbitrageBot) -> list[str]:
    """Что помешает режиму $5: ключ, права, деньги на торговом счёте, вторая копия бота."""
    t = b.trader
    names = b.scalper.exchanges()
    out: list[str] = []
    if not names:
        return ["нет рабочего ключа биржи: /keys"]
    try:
        await t.inspect_keys()
    except Exception:
        pass
    for name in names:
        title = b.scanner.display_name(name)
        problem = key_trade_problem(name, t.key_info.get(name))
        if problem:
            out.append(f"{title}: {problem}" + ("" if t.state.live else " (в тесте не мешает)"))
        if b.scalper.positions.get(name):
            continue  # деньги сейчас в монете — это нормально
        try:
            free = await t.free(name, b.settings.quote)
        except Exception as e:
            out.append(f"{title}: не смог прочитать баланс ({human_error(e)})")
            continue
        if free < 5:
            await b.scalper._fresh_balances(name, 0)
            idle = b.scalper._idle_coins_text(name)
            elsewhere = await t.other_accounts(name)
            if idle:
                if not b.scalper.st["use_coins"]:
                    out.append(f"{title}: USDT {free:.2f}, а деньги лежат в монетах ({idle}). Включи «Монеты в дело» "
                               "в /scalp — бот сам продаст их в USDT и будет ими торговать")
                elif not t.state.live:
                    out.append(f"{title}: в реальном режиме «Монеты в дело» продаст {idle} в USDT и пустит в торговлю")
                else:
                    out.append(f"{title}: «Монеты в дело» продаст {idle} в USDT на ближайшем шаге (до 30 с)")
            elif sum(elsewhere.values()) >= 1:
                out.append(t.where_money_text(name, elsewhere, free))
            else:
                out.append(f"{title}: на торговом счёте {free:.2f} {b.settings.quote}, а биржа принимает ордер от $5. "
                           "Пополни хотя бы до $5,5 (с запасом на округление), пока эта биржа будет ждать"
                           + ("" if t.state.live else " (в тесте бот возьмёт виртуальные $5)"))
    if b.duplicate_running():
        out.append("работают две копии бота: покупки на паузе, пока не удалишь лишний сервис в Render")
    return out


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


def perms_line(info: dict | None) -> str:
    """«(чтение ✅, спот ✅, вывод ⬜️ — так и надо)»: что включено у ключа, если биржа это сообщает."""
    if not info or info.get("error") or not any(k in info for k in ("spot_trade", "read", "read_only", "withdraw")):
        return ""
    parts = []
    if "spot_trade" in info:
        parts.append(f"спот {'✅' if info.get('spot_trade') else '❌'}")
    parts.append(f"вывод {'⚠️ включён — лучше выключить' if info.get('withdraw') else '⬜️ выключен, так и надо'}")
    return " (" + ", ".join(parts) + ")"


async def readiness(b: ArbitrageBot) -> tuple[str, InlineKeyboardMarkup]:
    """/setup: чек-лист «готов ли бот торговать» — по шагам, с кнопками для следующего шага."""
    t, sc, quote = b.trader, b.scalper, b.settings.quote
    lines = ["🚀 <b>Готовность к торговле</b>", ""]
    todo: list[str] = []
    rows: list[list[InlineKeyboardButton]] = []

    # 1. ключи
    if not t.creds:
        lines.append("1️⃣ ❌ <b>Ключи бирж не подключены.</b> Без них бот только показывает сигналы.")
        todo.append("подключить ключ Bybit или BingX (кнопка «Как подключить ключ»)")
    else:
        status = await t.check_keys()
        try:
            await t.inspect_keys()
        except Exception:
            pass
        for name, st in status.items():
            title = html.escape(b.scanner.display_name(name))
            problem = key_trade_problem(name, t.key_info.get(name)) if st == "ok" else None
            if st != "ok":
                lines.append(f"1️⃣ ❌ {title}: ключ не работает — {html.escape(st)}")
                todo.append(f"исправить ключ {title} (создай новый и замени в Render → Environment)")
            elif problem:
                lines.append(f"1️⃣ ⚠️ {title}: ключ работает, но {html.escape(problem)}")
                todo.append(f"включить право «Спот-торговля» у ключа {title}")
            else:
                lines.append(f"1️⃣ ✅ {title}: ключ работает, торговать может" + perms_line(t.key_info.get(name)))
    name = sc.exchange()

    async def _stuck_coin(ex_name: str, quote: str):
        """Монета ≥ $5, в которой лежат деньги вне режима $5: (монета, $, занята ли ордером)."""
        try:
            for c, f, total, usd in await t.balances(ex_name):
                if c != quote and usd and usd >= 5:
                    return c, usd, f < total * 0.9
        except Exception:
            pass
        return None

    async def _locked_quote(ex_name: str, quote: str) -> float:
        """Сколько USDT заморожено в открытых ордерах (не режима $5)."""
        try:
            for c, f, total, _ in await t.balances(ex_name):
                if c == quote:
                    return max(total - f, 0.0)
        except Exception:
            pass
        return 0.0

    # 2. деньги: режим $5 торгует на каждой бирже с ключом своими деньгами
    ready, low, tips = 0, [], []
    for ex_name in sc.exchanges():
        title = html.escape(b.scanner.display_name(ex_name))
        held = sc.positions.get(ex_name)
        if held:
            ready += 1
            lines.append(f"2️⃣ ✅ {title}: деньги в сделке ({html.escape(held['symbol'].split('/')[0])}), "
                         "продажа по цели стоит на бирже")
            continue
        try:
            free = await t.free(ex_name, quote)
        except Exception as e:
            lines.append(f"2️⃣ ⚠️ {title}: баланс не прочитан ({html.escape(human_error(e))})")
            continue
        if free >= 5:
            ready += 1
            lines.append(f"2️⃣ ✅ На торговом счёте {title}: <b>{free:.2f} {quote}</b> — режиму $5 хватает")
            continue
        elsewhere = await t.other_accounts(ex_name)
        hint = t.where_money_text(ex_name, elsewhere) if sum(elsewhere.values()) >= 1 else ""
        locked_q = await _locked_quote(ex_name, quote)
        if hint:
            where = ", ".join(f"«{html.escape(k)}» ${v:.2f}" for k, v in elsewhere.items() if v >= 0.5)
            lines.append(f"2️⃣ ❌ {title}: на торговом (спот) счёте {free:.2f} {quote}, а деньги лежат тут: <b>{where}</b>. "
                         "Бот торгует только со спот-счёта")
            todo.append(html.escape(hint))
        elif locked_q >= 5:
            lines.append(f"2️⃣ ❌ {title}: {locked_q:.2f} {quote} заняты открытым ордером, свободно {free:.2f}")
            todo.append(f"на {title} отменить старый ордер: /orders → ❌ (деньги вернутся на счёт)")
        else:
            coin = await _stuck_coin(ex_name, quote)
            if coin and sc.st["use_coins"] and not coin[2]:
                ready += 1
                lines.append(f"2️⃣ ✅ {title}: ≈${coin[1]:.2f} в {html.escape(coin[0])} — «Монеты в дело» включено, "
                             + ("бот сам продаст в USDT и начнёт торговать" if t.state.live
                                else "в реальном режиме бот сам продаст в USDT"))
            elif coin:
                c, usd, locked = coin
                lines.append(f"2️⃣ ❌ {title}: {free:.2f} {quote} свободно, а <b>≈${usd:.2f} лежит в {html.escape(c)}</b> "
                             "(купил не режим $5, он такую монету не ведёт)")
                tips.append(f"на {title} {html.escape(c)} занят ордером: сначала /orders → отменить, потом "
                            f"<code>/sell {ex_name} {html.escape(c)} all</code>" if locked
                            else f"на {title} пустить {html.escape(c)} в торговлю: кнопка «🤖 Включить автопилот» "
                            "(включит «Монеты в дело», бот сам продаст в USDT) или вручную "
                            f"<code>/sell {ex_name} {html.escape(c)} all</code>")
            else:
                lines.append(f"2️⃣ ➖ {title}: на торговом счёте {free:.2f} {quote} — меньше $5, эта биржа будет ждать")
                low.append(title)
    if not sc.exchanges():
        lines.append("2️⃣ ➖ Деньги проверю, когда заработает ключ")
    elif not ready:
        todo += tips
        if low and not tips:
            todo.append(f"пополнить торговый счёт ({', '.join(low)}) хотя бы до $5,5 USDT")
    else:
        lines += [f"   💡 {x}" for x in tips]

    # 3. проверка стратегии на истории
    if name:
        try:
            ranking = await sc.rank(name)
        except Exception:
            ranking = []
        good = [r for r in ranking if is_good(r)]
        if good:
            top = ", ".join(f"{html.escape(r['symbol'].split('/')[0])} {STRATEGIES.get(r.get('strategy') or 'dip', ('',))[0]} "
                            f"{r['net_pct']:+.1f}%" for r in good[:3])
            lines.append(f"3️⃣ ✅ Проверка на истории (3 стратегии, с проверкой на новых данных): прошли {len(good)} "
                         f"монет ({top})")
        elif ranking:
            lines.append("3️⃣ ⏳ Проверка на истории: сейчас ни одна монета не прошла — бот будет ждать, "
                         "это нормально и бережёт деньги")
        else:
            lines.append("3️⃣ ⚠️ Биржа пока не отдала свечи для проверки, попробую позже")

    # 4. режим $5 и реальный режим
    lines.append(f"4️⃣ {'✅' if sc.on else '⬜️'} Автопилот (режим $5): {'включён' if sc.on else 'выключен'}"
                 + (f" — {html.escape(sc.last_reason)}" if sc.on and sc.last_reason else ""))
    lines.append(f"5️⃣ {'🔴' if t.state.live else '🧪'} Режим: "
                 + ("<b>реальный</b> — ордера уходят на биржу" if t.state.live else "<b>тест</b> — ордера не отправляются"))
    if updater.outdated():
        lines.append(f"⚠️ {html.escape(updater.status_line())}")
        if not updater.hook_url():
            todo.append("обновить бота: /update (там кнопка и шаги на 1 минуту)")
    if b.duplicate_running():
        lines.append("⚠️ Работают две копии бота: удали лишний сервис в Render")
        todo.append("удалить лишний сервис в Render")

    lines.append("")
    if todo:
        lines.append("<b>Осталось сделать:</b>")
        lines += [f"• {x}" for x in todo]
    elif not sc.on:
        lines.append("✅ Всё готово. Нажми «🤖 Включить автопилот»: бот сам выберет монету и стратегию и будет торговать.")
    elif not t.state.live:
        lines.append("✅ Режим $5 работает в тесте. Когда увидишь, как он торгует, включи реальный режим.")
    else:
        lines.append("✅ <b>Бот торгует реально.</b> Состояние: /scalp · остановить всё: /stop")
    lines.append("\n<i>Честно: прибыль не гарантирована. Ставь только деньги, которые не жалко.</i>")

    if not t.creds or any(st != "ok" for st in t.key_status.values()):
        rows.append([InlineKeyboardButton(text="🔑 Как подключить ключ", callback_data="su:keys")])
    if name and not sc.on:
        rows.append([InlineKeyboardButton(text="🤖 Включить автопилот", callback_data="ap:on")])
    if name and sc.on and not t.state.live and not todo:
        rows.append([InlineKeyboardButton(text="🔴 Включить реальный режим", callback_data="tr:live")])
    rows.append([InlineKeyboardButton(text="📊 Проверка на истории", callback_data="sc:bt"),
                 InlineKeyboardButton(text="🔄 Проверить снова", callback_data="su:again")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


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
        if not b.is_owner(uid):
            await message.answer("⛔ Торговать может только владелец бота.")
            return False
        return True

    async def cb_owner(cb: CallbackQuery) -> bool:
        if not b.is_owner(cb.from_user.id):
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

    @dp.message(Command("setup", "ready", "go"))
    async def cmd_setup(message: Message) -> None:
        if not await owner_only(message):
            return
        wait = await message.answer("🔍 Проверяю ключи, деньги и стратегию…")
        text, markup = await readiness(b)
        try:
            await wait.delete()
        except Exception:
            pass
        await b._answer(message, text, markup)

    @dp.callback_query(F.data.startswith("su:"))
    async def cb_setup(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        action = cb.data.split(":", 1)[1]
        await cb.answer("Проверяю…" if action == "again" else None)
        if action == "keys":
            await b._send(chat_of(cb), await keys_text(b))
        else:
            text, markup = await readiness(b)
            await b._send(chat_of(cb), text, markup)

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
        if on and b.scalper.on:
            b.scalper.turn_off()
            await b._send(chat_id, "🎯 Режим $5 выключен: обе стратегии берут одни и те же USDT и мешали бы друг другу.")
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

    async def toggle_scalp(chat_id: int, on: bool | None = None) -> None:
        sc = b.scalper
        on = (not sc.on) if on is None else on
        if not on:
            sc.turn_off()
            await after_change()
            live_pos = any(p.get("live") for p in sc.positions.values())
            extra = ("\nОткрытые позиции остались: продажа по цели стоит на бирже. Продать сразу: кнопка в /scalp."
                     if live_pos else "")
            await b._send(chat_id, "⏸ Режим $5 выключен." + extra)
            return
        if not b.trader.working:
            await b._send(chat_id, "Для режима $5 нужен рабочий ключ биржи: /keys")
            return
        if b.trader.state.auto:
            b.trader.state.auto = False
            await b._send(chat_id, "⚙️ Автоторговлю треугольниками выключил: на $5 она не проходит минимум биржи "
                                   "(нужно $5 на каждый из трёх ордеров), а деньги теперь нужны режиму $5.")
        sc.turn_on()
        await after_change()
        warnings = await scalp_preflight(b)
        mode = ("🔴 <b>РЕАЛЬНО</b>, с настоящими деньгами" if b.trader.state.live
                else "🧪 в <b>ТЕСТЕ</b> на виртуальном балансе (ордера не уходят). Для реальной торговли: /live")
        await b._send(chat_id,
                      f"🎯 <b>Режим $5 включён</b>, {mode}.\n"
                      "1) Проверяю стратегию на истории по 13 крупным монетам (свечи за ~3 дня).\n"
                      "2) Жду отката и покупаю на все свободные USDT (не больше "
                      f"${b.settings.max_trade_usd:g}, <code>/set max_trade</code>).\n"
                      "3) Сразу ставлю продажу на бирже по цели +0,6…1,3% после комиссий. Прибыль идёт в оборот.\n"
                      "Сделка может длиться от минут до суток и дольше: откаты бывают не каждый час.\n"
                      + "".join(f"\n⚠️ {html.escape(w)}" for w in warnings)
                      + "\n\nСостояние: /scalp · остановить всё: /stop", scalp_keyboard(b))
        try:
            await sc.rank(sc.exchange() or "", force=False)
        except Exception as e:
            log.info("Проверка на истории: %s", e)

    async def scalp_backtest(chat_id: int) -> None:
        sc = b.scalper
        name = sc.exchange()
        if name is None:
            await b._send(chat_id, "Нужен рабочий ключ биржи: /keys")
            return
        await b._send(chat_id, "📊 Беру свечи за ~3 дня по 13 монетам и проверяю стратегию с комиссиями…")
        try:
            await sc.rank(name, force=True)
        except Exception as e:
            await b._send(chat_id, f"❌ Не получилось: {html.escape(human_error(e))}")
            return
        await b._send(chat_id, sc.backtest_text(name))

    async def autopilot(chat_id: int, on: bool = True) -> None:
        """Одна кнопка: режим $5 + «Монеты в дело». Реальные деньги — только после отдельного подтверждения."""
        sc = b.scalper
        if not on:
            await toggle_scalp(chat_id, False)
            return
        if not b.trader.working:
            await b._send(chat_id, "Для автопилота нужен рабочий ключ биржи (права «Чтение + Спот торговля»): /keys")
            return
        sc.st["use_coins"] = True
        if sc.on:
            await after_change()
            await b._refresh_balances()
            await b._send(chat_id, "🤖 Автопилот уже работает. «Монеты в дело» включено.\n\n" + sc.status_text(),
                          scalp_keyboard(b))
        else:
            await toggle_scalp(chat_id, True)
        if not b.trader.state.live:
            s = b.settings
            await b._send(chat_id, "🧪 Сейчас автопилот торгует <b>в тесте</b>: всё как по-настоящему, но ордера не уходят. "
                                   "Можно понаблюдать пару сделок, а можно сразу включить реальные деньги:\n\n"
                                   + LIVE_WARNING.format(max_trade=s.max_trade_usd, daily=s.daily_trades,
                                                         loss=s.daily_loss_usd), live_keyboard())

    @dp.message(Command("autopilot", "avtopilot", "pilot"))
    async def cmd_autopilot(message: Message, command: CommandObject) -> None:
        if not await owner_only(message):
            return
        arg = (command.args or "").strip().lower()
        await autopilot(message.chat.id, arg not in ("off", "0", "выкл", "нет"))

    @dp.callback_query(F.data.startswith("ap:"))
    async def cb_autopilot(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        await cb.answer()
        await autopilot(chat_of(cb), cb.data == "ap:on")

    @dp.message(Command("lab", "laboratory"))
    async def cmd_lab(message: Message) -> None:
        if await owner_only(message):
            await b._answer(message, b.scalper.lab_text(), scalp_keyboard(b))

    @dp.message(Command("scalp", "five", "dip"))
    async def cmd_scalp(message: Message, command: CommandObject) -> None:
        if not await owner_only(message):
            return
        arg = (command.args or "").strip().lower()
        if arg in ("on", "1", "вкл", "да"):
            await toggle_scalp(message.chat.id, True)
        elif arg in ("off", "0", "выкл", "нет"):
            await toggle_scalp(message.chat.id, False)
        elif arg in ("test", "bt", "история", "проверка"):
            await scalp_backtest(message.chat.id)
        else:
            await b._refresh_balances()
            await b._answer(message, b.scalper.status_text(), scalp_keyboard(b))

    @dp.callback_query(F.data.startswith("sc:"))
    async def cb_scalp(cb: CallbackQuery) -> None:
        if not await cb_owner(cb):
            return
        action = cb.data.split(":", 1)[1]
        chat_id = chat_of(cb)
        await cb.answer()
        if action == "on":
            await toggle_scalp(chat_id, True)
        elif action == "off":
            await toggle_scalp(chat_id, False)
        elif action == "bt":
            await scalp_backtest(chat_id)
        elif action == "lab":
            await b._send(chat_id, b.scalper.lab_text(), scalp_keyboard(b))
        elif action == "coins":
            st = b.scalper.st
            st["use_coins"] = not st["use_coins"]
            await after_change()
            await b._send(chat_id, ("🪙 <b>Монеты в дело: включено.</b> Если на счёте мало USDT, а монета (например, SOL) "
                                    "лежит без дела и стоит больше минимального ордера, бот продаст её по рынку в USDT "
                                    "и будет торговать этими деньгами. Только в реальном режиме; монету из открытой "
                                    "сделки не трогает." if st["use_coins"] else
                                    "🪙 Монеты в дело: выключено. Монеты на счёте бот не трогает."), scalp_keyboard(b))
        elif action == "sell":
            try:
                text = await b.scalper.sell_now()
            except TradeError as e:
                text = f"❌ {html.escape(str(e))}"
            except Exception as e:
                log.exception("Продажа позиции режима $5")
                text = f"❌ {html.escape(human_error(e))}"
            await after_change()
            await b._send(chat_id, text)
        else:
            await b._refresh_balances()
            await b._send(chat_id, b.scalper.status_text(), scalp_keyboard(b))

    @dp.message(Command("why", "autostatus"))
    async def cmd_why(message: Message) -> None:
        if await owner_only(message):
            await b._answer(message, why_text(b))

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
            await tell_others(message.from_user.id if message.from_user else None, "⛔ <b>Остановил всё</b> (/stop).")

    @dp.message(Command("trades"))
    async def cmd_trades(message: Message) -> None:
        if await owner_only(message):
            await b._answer(message, b.trader.log_text())

    # --- кнопки -------------------------------------------------------------

    async def tell_others(uid: int | None, text: str) -> None:
        """🔔 Действия с настоящими деньгами — видны и остальным пользователям бота: «👤 Вася: …»."""
        if uid is not None:
            await b.notify(text, actor=uid, skip_actor=True)

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
        if getattr(rec, "live", False):
            await tell_others(cb.from_user.id, b.trader.record_text(rec))

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
        if getattr(res, "live", False) and getattr(res, "executed", False):
            await tell_others(cb.from_user.id, res.text(b.scanner.display_name))

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
        await tell_others(cb.from_user.id, "🔴 <b>Реальный режим включён</b>: ордера уходят на биржу. "
                                           "Вернуть тест: /test · остановить всё: /stop")

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
            await b._send(chat_id, why_text(b))
        elif action == "keys":
            await b._send(chat_id, await keys_text(b))
        elif action == "stop":
            text = await b.trader.panic()
            await after_change()
            await b._send(chat_id, text)
