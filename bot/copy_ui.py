"""🐋 Копитрейдинг в Telegram: /copy, кнопки, фоновый цикл и сигналы."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .copytrade import STEP_SEC
from .solana import WalletError

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()  # держим ссылки на фоновые задачи, иначе сборщик мусора может их оборвать


def _spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task

COPY_HELP = """\
🐋 <b>Копитрейдинг: как пользоваться</b>

<b>Включить</b> — кнопка «🟢 Включить». Бот сам найдёт кошельки: возьмёт токены Solana, которые сильно
выросли, и посмотрит, кто купил их рано. Ботов-снайперов отсеет по их истории.

<b>Добавить свой кошелёк</b> (например, из GMGN → Top Traders, Axiom или DexScreener → Top traders):
<code>/copy add АДРЕС имя</code>
<b>Убрать</b>: <code>/copy del АДРЕС</code> · <b>Подробно о кошельке</b>: <code>/copy w АДРЕС</code>

<b>Как бот решает, кому верить</b>
1. Каждая покупка кошелька повторяется <b>без денег</b>: по цене в момент, когда бот её увидел (с реальной
   задержкой), с комиссией Phantom 0,85% и проскальзыванием 1%. Выход — когда кошелёк продаёт, стоп −40%
   или через 48 ч.
2. ⭐ — от 5 копий и в среднем от +2% на копию (с поправкой на везение). Покупки ⭐-кошельков приходят
   сигналом с кнопкой «Купить в Phantom».
3. 🔴 — 4 убытка подряд или −40% по копиям: бот выгоняет кошелёк и ищет новый.
4. Фильтр рисков (ликвидность, RugCheck, возраст токена, переплата к цене кошелька, рост за час) сам
   сдвигает пороги, если сделки за порогом теряли. Каждое изменение — «урок» с цифрами.

💸 <b>Торговать реально</b>: /meme. Бот покупает и продаёт сам с ОТДЕЛЬНОГО кошелька «Бот» (новый аккаунт
в Phantom с небольшой суммой), ключ которого лежит только в Render. Основной кошелёк и сид-фраза не нужны.
Мемкоины — самый рискованный рынок: заходи только суммой, которую не жалко потерять."""


def keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    ct = b.copy
    rows = [[InlineKeyboardButton(text="⏸ Выключить" if ct.on else "🟢 Включить", callback_data="cp:off" if ct.on else "cp:on"),
             InlineKeyboardButton(text=f"🔔 Сигналы: {'вкл' if ct.st['signals'] else 'выкл'}", callback_data="cp:sig")],
            [InlineKeyboardButton(text="🔎 Найти кошельки сейчас", callback_data="cp:find"),
             InlineKeyboardButton(text="🧠 Уроки", callback_data="cp:lessons")],
            [InlineKeyboardButton(text="💸 Торговать реально" + (" 🟢" if b.meme.on else ""), callback_data="mt:show")],
            [InlineKeyboardButton(text="❔ Как это работает", callback_data="cp:help"),
             InlineKeyboardButton(text="🔄 Обновить", callback_data="cp:show")]]
    url = b.app_url()
    if url:
        from aiogram.types import WebAppInfo
        rows.append([InlineKeyboardButton(text="📱 В приложении", web_app=WebAppInfo(url=url + "?open=copy"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lessons_text(b: ArbitrageBot) -> str:
    st = b.copy.st
    lines = ["🧠 <b>Чему бот научился</b>"]
    f = st["filter"]
    liq = f"{f['min_liq']:,.0f}".replace(",", " ")
    lines.append(f"Фильтр сейчас: ликвидность ≥ ${liq}, RugCheck ≤ {f['max_rug']:g}, возраст ≥ "
                 f"{f['min_age_h']:g} ч, переплата ≤ {f['max_chase']:.0%}, рост за час ≤ {f['max_h1']:g}%")
    if st["mistakes"]:
        from .copytrade import MISTAKES
        lines.append("\n<b>Ошибки в убыточных копиях</b>:")
        for k, v in sorted(st["mistakes"].items(), key=lambda x: -x[1])[:6]:
            lines.append(f"• {MISTAKES.get(k, k)} — {v}")
    if st["lessons"]:
        lines.append("\n<b>Уроки</b>:")
        for x in st["lessons"][-10:][::-1]:
            lines.append(f"• {time.strftime('%d.%m %H:%M', time.gmtime(x['at'] + 3 * 3600))} — {html.escape(x['text'])}")
    else:
        lines.append("\nУроков пока нет: фильтр начинает учиться после 20 закрытых копий.")
    return "\n".join(lines)


def event_markup(e: dict) -> InlineKeyboardMarkup | None:
    """Кнопки события: ссылка или действие в боте («cb:mt:s:…» → callback)."""
    if not e.get("buttons"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=u[3:]) if u.startswith("cb:") else InlineKeyboardButton(text=t, url=u)]
        for t, u in e["buttons"]])


async def send_events(b: ArbitrageBot, events: list[dict]) -> None:
    if b.owner_id is None:
        return
    for e in events:
        held = b.meme.st["positions"] if b.meme.configured else b.meme.demo["positions"]
        if e.get("mint") and e.get("signal") is not False and e["mint"] not in held and not any(
                u.startswith("cb:") for _, u in e.get("buttons", [])):
            btn = ((f"💸 Купить ботом ${b.meme.st['size_usd']:g}", f"cb:mt:b:{e['mint']}") if b.meme.configured
                   else (f"🧪 Купить на демо ${b.meme.st['size_usd']:g}", f"cb:mt:db:{e['mint']}"))
            e = {**e, "buttons": [btn] + list(e.get("buttons", []))}
        await b.notify(e["text"], event_markup(e))


async def find_now(b: ArbitrageBot, chat_id: int) -> None:
    await b._send(chat_id, "🔎 Ищу кошельки: выросшие токены → ранние покупатели → проверка их истории. "
                           "Это займёт до пары минут…")
    try:
        report = await b.copy.discover(force=True)
    except Exception as e:
        await b._send(chat_id, f"⚠️ Поиск не удался: {html.escape(str(e))[:300]}")
        return
    b.save_copy()
    text = "🔎 <b>Поиск кошельков</b>\n" + ("\n".join(html.escape(r) for r in report) if report else
                                         "Сейчас нет подходящих выросших токенов (нужно +50% за сутки, ликвидность "
                                         "от $30k, возраст 2–96 ч). Попробую сам через 2 часа.")
    await b._send(chat_id, text, keyboard(b))


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    async def owner_only(uid: int | None, chat_id: int) -> bool:
        if not b.is_owner(uid):
            await b._send(chat_id, "⛔ Управлять копитрейдингом может только владелец бота.")
            return False
        return True

    @dp.message(Command("copy", "wallets", "smart"))
    async def cmd_copy(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        uid = message.from_user.id if message.from_user else None
        args = (command.args or "").split()
        if not args:
            await b._send(message.chat.id, b.copy.text(), keyboard(b))
            return
        if not await owner_only(uid, message.chat.id):
            return
        cmd = args[0].lower()
        if cmd in ("add", "+") and len(args) >= 2:
            await message.answer("🐋 Добавляю и разбираю историю кошелька…")
            try:
                await b.copy.add_wallet(args[1], " ".join(args[2:])[:30])
            except WalletError as e:
                await message.answer(f"⚠️ {e}")
                return
            b.save_copy()
            await b._send(message.chat.id, b.copy.wallet_text(args[1]) + (
                "" if b.copy.on else "\n\nКопитрейдинг выключен — включи кнопкой, чтобы бот начал копировать."), keyboard(b))
        elif cmd in ("del", "rm", "-") and len(args) >= 2:
            ok = b.copy.remove_wallet(args[1])
            b.save_copy()
            await message.answer("🗑 Убрал." if ok else "Такого кошелька нет в списке.")
        elif cmd in ("w", "info") and len(args) >= 2:
            await b._send(message.chat.id, b.copy.wallet_text(args[1]), keyboard(b))
        elif cmd in ("on", "off"):
            b.copy.st["on"] = cmd == "on"
            b.save_copy()
            await b._send(message.chat.id, b.copy.text(), keyboard(b))
            if cmd == "on" and not b.copy.st["wallets"]:
                _spawn(find_now(b, message.chat.id))
        elif cmd == "find":
            await find_now(b, message.chat.id)
        else:
            await b._send(message.chat.id, COPY_HELP)

    @dp.callback_query(F.data.startswith("cp:"))
    async def cb_copy(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id
        await cb.answer()
        if action == "show":
            await b._send(chat_id, b.copy.text(), keyboard(b))
        elif action == "help":
            await b._send(chat_id, COPY_HELP, keyboard(b))
        elif action == "lessons":
            await b._send(chat_id, lessons_text(b), keyboard(b))
        elif not await owner_only(cb.from_user.id, chat_id):
            return
        elif action in ("on", "off"):
            b.copy.st["on"] = action == "on"
            b.save_copy()
            await b._send(chat_id, ("🟢 Копитрейдинг включён. Проверяю кошельки каждые 30 секунд." if action == "on"
                                    else "⏸ Копитрейдинг выключен.") + "\n\n" + b.copy.text(), keyboard(b))
            if action == "on" and not b.copy.st["wallets"]:
                _spawn(find_now(b, chat_id))
        elif action == "sig":
            b.copy.st["signals"] = not b.copy.st["signals"]
            b.save_copy()
            await b._send(chat_id, f"🔔 Сигналы ⭐-кошельков {'включены' if b.copy.st['signals'] else 'выключены'}.",
                          keyboard(b))
        elif action == "find":
            await find_now(b, chat_id)


DEMO_INTRO = """\
🧪 <b>Запустил демо-счёт мемкоинов</b> — ${usd:g} виртуальных.
Пока настоящий кошелёк не подключён и не пополнен, бот торгует ими по тем же правилам, что и реальными: \
копирует лучшие кошельки (⭐), проверяет токен (ловушки, ликвидность, стоимость входа-выхода по настоящей котировке \
Jupiter), ставит стоп и сам продаёт.
🧠 На этих сделках и на всех копиях он учится: подбирает стоп и тейк-профит, замеряет реальные комиссии, решает, \
каких кошельков копировать. Каждое изменение — сообщением «Урок».
Честно: демо не платит за промах цены при реальном исполнении, поэтому живые сделки обычно хуже на 1–3%. \
Первые ⭐ кошельки появятся через 1–3 дня наблюдения. Копитрейдинг (без денег) включил — без него нет сигналов."""


async def copy_tick(b: ArbitrageBot) -> None:
    """Один шаг: копии, реальные сделки (если подключён кошелёк бота), поиск новых кошельков раз в 2 часа,
    сохранение. Открытые реальные позиции ведём, даже если копитрейдинг выключен."""
    ct, mt = b.copy, b.meme
    if b.owner_id is None:
        return
    events: list[dict] = []
    if mt.start_demo_if_new():  # первый запуск: демо-счёт учится, пока не подключён настоящий кошелёк
        events.append({"text": DEMO_INTRO.format(usd=mt.demo["start_usd"]),
                       "buttons": [("🧪 Демо-счёт", "cb:mt:show"), ("🐋 Копитрейдинг", "cb:cp:show")]})
    if not ct.on and not mt.st["positions"] and not mt.demo["positions"]:
        await send_events(b, events)
        return
    if ct.on:
        events += await ct.step()
        try:
            events += await mt.after_copy_step()
        except Exception as e:
            log.exception("Сделки мемкоинов после шага копитрейдинга")
            mt.last_error = str(e)[:200]
    try:
        events += await mt.guard()
    except Exception as e:
        log.exception("Сторож реальных позиций")
        mt.last_error = str(e)[:200]
    if ct.on:
        try:
            report = await ct.discover()
            added = [r for r in report if r.startswith("➕")]
            if added:
                events.append({"text": "🔎 Нашёл новые кошельки:\n" + "\n".join(html.escape(r) for r in added),
                               "buttons": []})
        except Exception as e:
            log.info("Поиск кошельков: %s", e)
    await send_events(b, events)
    if ct.changes != b._copy_saved:
        b._copy_saved = ct.changes
        b.save_copy()
        if events or time.time() - b._last_backup > 30 * 60:
            await b._backup_now()


async def copy_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(25)
    while True:
        try:
            await copy_tick(b)
        except Exception:
            log.exception("Ошибка копитрейдинга")
        await asyncio.sleep(getattr(b.copy, "step_sec", STEP_SEC))


def short_line(b: ArbitrageBot) -> str:
    """Строка для ежедневного отчёта (копии без денег + реальные сделки)."""
    real = b.meme.short_line()
    line = _copy_line(b)
    return "\n".join(x for x in (line, real) if x)


def _copy_line(b: ArbitrageBot) -> str:
    ct = b.copy
    if not ct.on and not ct.st["wallets"]:
        return ""
    s = ct.summary()
    stars = [w for w in ct.st["wallets"].values() if w["status"] == "star"]
    return (f"🐋 <b>Копитрейдинг</b>: {'🟢' if ct.on else '⏸'} кошельков {len(ct.active())}, ⭐ {len(stars)}"
            + (f" ({', '.join(html.escape(w['name']) for w in stars[:3])})" if stars else "")
            + (f". Копии без денег (прошли фильтр): {s['taken']}, {s['taken_wins']} в плюс, {s['taken_sum']:+.1f}%"
               if s["taken"] else "") + " — /copy")


