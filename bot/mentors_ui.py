"""🎓 /mentors: трейдеры с YouTube и Twitch — список, добавление, разбор ролика, советы и проверка прогнозов."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .mentors import TICK_SEC, VIDEOS_PER_DAY, VIEWS, MentorError, parse_link
from .scout import fit

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

HOW_TEXT = """\
🎓 <b>Как бот учится у трейдеров</b>

<b>YouTube.</b> Бот следит за новыми роликами каналов из списка. Ролик «смотрит» Gemini — ИИ Google открывает \
видео по ссылке (звук и кадры, первые 20 минут) и выписывает: суть, прогнозы (монета, рост/падение, срок), \
практические советы, есть ли реклама и насколько это хайп. Без ключа Gemini бот читает только название и описание.

<b>Twitch.</b> Бот анонимно читает чаты memecoin-стримеров. Когда стример, модератор или бот канала пишет адрес \
токена Solana — значит, стример его торгует или показывает зрителям. Бот записывает цену в эту минуту.

<b>Главное — проверка.</b> Слова ничего не стоят, поэтому каждый прогноз бот проверяет ценой: запоминает, по \
какой цене мог бы войти, и смотрит, что стало через срок прогноза (мемкоин — как настоящая копия: стоп, \
тейк-профит, комиссии). У каждого трейдера копится счёт:
⭐ — советы окупаются (от 8 проверок): его прогнозы приходят сигналом, токены стримера — сделкой на 🧪 демо;
👀 — проверяю; 🔇 — теряют деньги или половина роликов с рекламой: сигналов нет, ИИ ему не доверяет.

<b>Советы с цифрами</b> («стоп −20%», «ликвидность от $50K») бот проверяет на своей истории копий и меняет \
правило, только если так было бы заметно выгоднее. Каждый принятый или отклонённый совет — с цифрами.

<b>ИИ</b> (/ai и вопросы в чат) видит выводы роликов, счёт каждого трейдера и общее мнение по монетам за неделю.

Честно: стримеры часто показывают токен, когда уже купили сами, — зрители покупают дороже. Реклама бирж \
и пресейлов встречается постоянно. Поэтому бот ничего не покупает за настоящие деньги «потому что сказал \
блогер»: только сигналы, демо и кнопка.

<b>Gemini бесплатно:</b> aistudio.google.com/apikey → Create API key → в Render → Environment → \
<code>GEMINI_API_KEY</code>. Из России сайт открывается через VPN, а сам бот на Render работает с ключом \
без VPN. Лимит — 8 часов YouTube-видео в день, бот тратит не больше 5."""


def keyboard(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.mentors.st
    rows = [[InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="mn:run"),
             InlineKeyboardButton(text="🔄 Обновить", callback_data="mn:show")],
            [InlineKeyboardButton(text="🏋️ Тренировка на демо", callback_data="tn:show"),
             InlineKeyboardButton(text="🔭 Разведчик", callback_data="mn:scout")],
            [InlineKeyboardButton(text="🧠 Советы", callback_data="mn:tips"),
             InlineKeyboardButton(text="📈 Прогнозы", callback_data="mn:calls"),
             InlineKeyboardButton(text="👥 Трейдеры", callback_data="mn:list")],
            [InlineKeyboardButton(text="🔔 Сообщать о роликах" if not st["notify"] else "🔕 Не сообщать о роликах",
                                  callback_data="mn:notify"),
             InlineKeyboardButton(text="⏸ Выключить" if st["on"] else "🟢 Включить", callback_data="mn:toggle")],
            [InlineKeyboardButton(text="🔌 Проверить Gemini", callback_data="mn:gcheck"),
             InlineKeyboardButton(text="❔ Как это работает", callback_data="mn:help")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scout_kb(b: ArbitrageBot) -> InlineKeyboardMarkup:
    on = b.mentors.scout.st["on"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Найти трейдеров сейчас", callback_data="mn:sfind")],
        [InlineKeyboardButton(text="🏆 Рейтинг", callback_data="mn:srank"),
         InlineKeyboardButton(text="📜 Проверить историю", callback_data="mn:shist")],
        [InlineKeyboardButton(text="⏸ Не искать самому" if on else "🟢 Искать самому", callback_data="mn:stoggle"),
         InlineKeyboardButton(text="🎓 Трейдеры", callback_data="mn:show")]])


async def scout_find(b: ArbitrageBot, chat_id: int) -> None:
    sc = b.mentors.scout
    if sc.busy:
        await b._send(chat_id, "⏳ Уже ищу, подожди пару минут.")
        return
    await b._send(chat_id, "🔭 Ищу трейдеров: Gemini + поиск Google, поиск YouTube, Twitch Crypto. Каждого проверяю "
                           "фейк-фильтром — это до 2 минут…")
    try:
        events = await sc.discover(force=True)
    except Exception as e:
        log.exception("Разведчик")
        await b._send(chat_id, f"⚠️ Поиск не удался: {html.escape(str(e))[:300]}", scout_kb(b))
        return
    b.mentors._changed()
    b.save_copy()
    if events:
        await send_events(b, events)
    else:
        why = sc.st["last_error"]
        await b._send(chat_id, "🔭 Новых подходящих трейдеров не нашёл" + (
            "" if not why else f" ({html.escape(why)[:200]})") + ". Все кандидаты отсеяны фейк-фильтром или уже "
            "знакомы, либо на испытании нет свободных мест. Попробую сам через сутки.", scout_kb(b))
    await b._send(chat_id, sc.text(), scout_kb(b))


async def scout_history(b: ArbitrageBot, chat_id: int) -> None:
    sc = b.mentors.scout
    if not b.mentors.candles_of:
        await b._send(chat_id, "📜 Нет доступа к ценам бирж — историю посчитать нечем.", scout_kb(b))
        return
    if sc.busy:
        await b._send(chat_id, "⏳ Разведчик занят поиском, попробуй через пару минут.")
        return
    n = len(b.mentors.active("youtube"))
    await b._send(chat_id, f"📜 Считаю прошлые прогнозы {n} каналов YouTube на настоящих свечах Bybit/BingX — "
                           "это до пары минут…")
    sc.busy = True
    try:
        done = await sc.refresh_history(force=True, limit=20)
    except Exception as e:
        log.exception("История прогнозов")
        await b._send(chat_id, f"⚠️ Не получилось: {html.escape(str(e))[:300]}", scout_kb(b))
        return
    finally:
        sc.busy = False
    b.save_copy()
    await b._send(chat_id, f"📜 Посчитал историю {done} из {n} каналов.\n\n" + sc.rank_text(), scout_kb(b))


async def gemini_check_text(b: ArbitrageBot) -> str:
    """Понятная проверка ключа Gemini: где найден, принял ли его Google, какая модель."""
    mt = b.mentors
    g = mt.gemini
    if not g.configured:
        why = mt.gemini_hint or "в Render нет переменной GEMINI_API_KEY с ключом"
        return ("🔌 <b>Gemini не подключён</b>: " + html.escape(why) + ".\n\n"
                "Как подключить: aistudio.google.com/apikey → Create API key → открой ключ → «Copy key» (полный ключ, "
                "начинается с <code>AQ.</code>) → Render → твой сервис → Environment → <code>GEMINI_API_KEY</code> → "
                "вставь → Save Changes. Render сам перезапустит бота, через 1–2 минуты нажми проверку ещё раз.")
    masked = f"{g.key[:5]}…{g.key[-4:]}" if len(g.key) > 12 else "…"
    try:
        model = await g.check()
    except Exception as e:
        return (f"🔌 Ключ найден ({html.escape(mt.gemini_env or '?')}: <code>{html.escape(masked)}</code>), но "
                f"<b>не работает</b>: {html.escape(str(e))[:300]}\n\nЧаще всего ключ скопирован не целиком: в AI "
                "Studio открой ключ и нажми «Copy key», вставь заново в Render.")
    ai = b.ai.client
    ai_line = ("ИИ-помощник тоже на Gemini ✅" if any(e.provider == "gemini" for e in ai.endpoints)
               else "ИИ-помощник работает через другой ключ")
    return (f"🔌 <b>Gemini подключён ✅</b> ({html.escape(mt.gemini_env or '?')}: <code>{html.escape(masked)}</code>)\n"
            f"Модель: {html.escape(model)}. Ролики YouTube бот будет смотреть сам — до {VIDEOS_PER_DAY} в день.\n"
            f"{ai_line}.")


def tips_text(b: ArbitrageBot) -> str:
    d = b.mentors.status_dict()
    if not d["tips"]:
        return "🧠 Советов пока нет — они появятся после разбора первых роликов."
    icon = {"accepted": "✅", "rejected": "❌", "same": "➖", "wait": "⏳"}
    lines = ["🧠 <b>Советы трейдеров</b> (с цифрами — проверены на истории копий)"]
    for t in d["tips"]:
        mark = icon.get(t["state"], "💬")
        lines.append(f"{mark} <b>{html.escape(t['author'] or '—')}</b>: {html.escape(t['text'])}"
                     + (f"\n   → {html.escape(t['verdict'])}" if t["verdict"] else ""))
    return "\n".join(lines)


def calls_text(b: ArbitrageBot) -> str:
    d = b.mentors.status_dict()
    if not d["recent_calls"]:
        return "📈 Прогнозов на проверке пока нет."
    lines = [f"📈 <b>Прогнозы трейдеров</b>: на проверке {d['calls_open']}, проверено {d['calls_done']}"]
    for c in d["recent_calls"]:
        arrow = {"long": "📈 рост", "short": "📉 падение", "avoid": "🚫 не покупать"}[c["direction"]]
        if c["done"]:
            res = f"итог <b>{c['ret']:+.1f}%</b> {'✅' if c['ret'] > 0 else '❌'}"
        else:
            left = max(0, c["at"] + c["horizon_h"] * 3600 - time.time()) / 3600
            res = (f"сейчас {c['now']:+.1f}% · " if c["now"] else "") + f"проверю через {left:.0f} ч"
        lines.append(f"• {html.escape(c['author'])}: {html.escape(c['asset'])} {arrow} — {res}")
    lines.append("\nМемкоины считаются как копия: со стопом, тейк-профитом и комиссиями. Монеты с бирж — по цене через "
                 "срок прогноза минус комиссии.")
    return "\n".join(lines)


def list_kb(b: ArbitrageBot) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗑 {s['name']}", callback_data=f"mn:del:{s['id']}")]
            for s in b.mentors.active()[:14]]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="mn:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def list_text(b: ArbitrageBot) -> str:
    d = b.mentors.status_dict()
    lines = ["👥 <b>Трейдеры</b> — нажми 🗑, чтобы перестать следить. Добавить: <code>/mentors add ссылка</code>"]
    for s in d["sources"]:
        icon = "🔭" if s["trial"] and s["status"] == "watch" else {"star": "⭐", "watch": "👀", "muted": "🔇"}[s["status"]]
        where = "▶️ YouTube" if s["kind"] == "youtube" else "📺 Twitch" + (" · 🟢 чат" if s["live_chat"] else "")
        lines.append(f"{icon} <b>{html.escape(s['name'])}</b> ({where}) — {html.escape(s['note'] or '')}"
                     + (f"\n   {html.escape(s['history'])}" if s["history"] else "")
                     + (f"\n   ⚠️ {html.escape(s['error'])}" if s["error"] else ""))
    return fit(lines)


async def send_events(b: ArbitrageBot, events: list[dict]) -> None:
    from .copy_ui import send_events as send
    await send(b, events)


async def analyze_link(b: ArbitrageBot, chat_id: int, video_id: str) -> None:
    mt = b.mentors
    await b._send(chat_id, "⏳ Смотрю ролик… (Gemini обычно отвечает за 30–90 секунд)")
    src = None
    try:
        cid, author = await mt.youtube.resolve("yt_video", video_id)
        src = next((s for s in mt.active("youtube") if s["ident"] == cid), None) or mt._add("youtube", cid, author)
    except Exception as e:
        log.info("Канал ролика: %s", e)
    video = {"id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}", "title": "", "description": "",
             "published": time.time(), "author": src["name"] if src else ""}
    if src:  # название и описание — из RSS канала (если ролик свежий)
        try:
            _, videos = await mt.youtube.feed(src["ident"])
            found = next((v for v in videos if v["id"] == video_id), None)
            if found:
                video.update(title=found["title"], description=found["description"], published=found["published"])
        except Exception as e:
            log.info("RSS для ролика: %s", e)
    try:
        rec = await mt.analyze_video(src, video)
    except Exception as e:
        await b._send(chat_id, f"⚠️ Не разобрал ролик: {html.escape(str(e))[:300]}")
        return
    if src:
        src["seen"] = (src["seen"] + [video_id])[-40:]
    b.save_copy()
    pending = mt.take_pending()  # сигналы тренировки по прогнозам из этого ролика
    e = mt.video_event(rec, src)
    if rec["tips"][1:]:
        e["text"] += "\nЕщё советы: " + "; ".join(html.escape(t["text"]) for t in rec["tips"][1:4])
    if src:
        e["text"] += f"\n\n▶️ Канал «{html.escape(src['name'])}» добавил в список — новые ролики разберу сам."
    await send_events(b, [e] + pending)


TRAIN_HOW = """\
🏋️ <b>Как бот тренируется</b>

Всё, чему бот научился у трейдеров, он сам проверяет сделками на <b>виртуальных деньгах</b>:
• каждый прогноз «вырастет» (монета с Bybit/BingX) и каждый токен, который стример показал в чате, — идея для сделки;
• одновременно торгуют 5 стратегий, у каждой своё демо. Они по-разному отбирают идеи: все подряд; только \
проверенные авторы; только если согласны 2+ трейдера; без рекламы и хайпа; только уверенные прогнозы;
• сделка — 25% счёта стратегии, со стопом, тейк-профитом, комиссиями и проскальзыванием; держит не дольше срока \
прогноза (мемкоин — сутки, монета — до недели).

<b>Чему учится сам:</b>
1) выбирает лучшую стратегию (от 8 сделок, средний результат с поправкой на везение) — только её идеи приходят \
тебе сигналом 🏋️. Ни одна не в плюсе — сигналов нет;
2) подбирает стоп и тейк-профит: цену бот отслеживает весь срок идеи, даже после выхода, поэтому видно, что было \
бы при другом стопе. Меняет по одному шагу и только если выгода от 5%.

Настоящие деньги тренировка не трогает. Честно: на демо нет реального проскальзывания и очередей, живые сделки \
обычно хуже на 1–3%."""


def train_kb(b: ArbitrageBot) -> InlineKeyboardMarkup:
    st = b.mentors.train.st
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="tn:show"),
         InlineKeyboardButton(text="📜 Сделки", callback_data="tn:trades")],
        [InlineKeyboardButton(text="🔕 Без сигналов" if st["signals"] else "🔔 Сигналы лучшей стратегии",
                              callback_data="tn:signals"),
         InlineKeyboardButton(text="🔕 Не писать о сделках" if st["notify"] else "📨 Писать о каждой сделке",
                              callback_data="tn:notify")],
        [InlineKeyboardButton(text="⏸ Пауза" if st["on"] else "🟢 Тренироваться", callback_data="tn:toggle"),
         InlineKeyboardButton(text="❔ Как это работает", callback_data="tn:help")],
        [InlineKeyboardButton(text=f"♻️ Заново ${x}", callback_data=f"tn:reset:{x}") for x in (5, 10, 20)],
        [InlineKeyboardButton(text="🎓 Трейдеры", callback_data="mn:show")]])


def train_trades_text(b: ArbitrageBot) -> str:
    d = b.mentors.train.status_dict()
    if not d["closed"]:
        return "📜 Закрытых демо-сделок пока нет. Первые закроются по стопу, тейку или когда выйдет срок прогноза."
    why = {"stop": "стоп", "tp": "тейк", "time": "срок"}
    who = f"«{html.escape(d['active_name'])}»" if d["active"] else "«Все прогнозы»"
    lines = [f"📜 <b>Последние демо-сделки</b> стратегии {who}"]
    for c in d["closed"]:
        lines.append(f"{'✅' if c['pct'] > 0 else '❌'} {html.escape(c['asset'])} ({html.escape(c['author'] or '—')}) — "
                     f"{why[c['why']]}, <b>{c['pct']:+.1f}%</b> (${c['usd']:+.2f})")
    return "\n".join(lines)


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    async def owner_only(uid: int | None, chat_id: int) -> bool:
        if b.owner_id is None or uid != b.owner_id:
            await b._send(chat_id, "⛔ Менять список может только владелец бота.")
            return False
        return True

    async def show(chat_id: int) -> None:
        if b.mentors.seed():  # открыли раньше первого шага — стартовый список сразу
            b.save_copy()
        await b._send(chat_id, b.mentors.text(), keyboard(b))

    async def add(chat_id: int, link: str) -> None:
        try:
            src, text = await b.mentors.add_link(link)
        except (MentorError, Exception) as e:
            await b._send(chat_id, f"⚠️ {html.escape(str(e))[:300]}")
            return
        if text.startswith("video:"):
            await analyze_link(b, chat_id, text[6:])
            return
        b.save_copy()
        await b._send(chat_id, text, keyboard(b))
        await b._backup_now()

    @dp.message(Command("mentors", "traders", "youtube", "twitch", "mentor"))
    async def cmd_mentors(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        args = (command.args or "").split(maxsplit=1)
        if not args:
            await show(message.chat.id)
            return
        uid = message.from_user.id if message.from_user else None
        if not await owner_only(uid, message.chat.id):
            return
        cmd, rest = args[0].lower(), (args[1] if len(args) > 1 else "")
        if cmd in ("scout", "разведчик", "find", "найти"):
            if rest in ("now", "сейчас") or cmd in ("find", "найти"):
                if b.owner_id is not None and message.from_user and message.from_user.id == b.owner_id:
                    await scout_find(b, message.chat.id)
                    return
            await b._send(message.chat.id, b.mentors.scout.text(), scout_kb(b))
        elif cmd in ("gemini", "check", "проверка"):
            await b._send(message.chat.id, await gemini_check_text(b), keyboard(b))
        elif cmd in ("add", "добавить", "video", "видео", "ролик") and rest:
            await add(message.chat.id, rest.strip())
        elif cmd in ("del", "удалить", "remove") and rest:
            src = b.mentors.find(rest)
            if not src:
                await message.answer("Такого трейдера в списке нет. Список: /mentors")
                return
            b.mentors.remove(src)
            b.save_copy()
            await message.answer(f"🗑 Больше не слежу за {html.escape(src['name'])}.")
        elif parse_link(command.args.strip()):  # ссылку не переводим в нижний регистр: id ролика чувствителен
            await add(message.chat.id, command.args.strip())
        else:
            await b._send(message.chat.id, HOW_TEXT, keyboard(b))

    @dp.message(Command("scout", "разведчик"))
    async def cmd_scout(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        uid = message.from_user.id if message.from_user else None
        arg = (command.args or "").strip().lower()
        if arg in ("now", "сейчас", "find", "найти") and uid == b.owner_id:
            await scout_find(b, message.chat.id)
            return
        if arg in ("history", "история") and uid == b.owner_id:
            await scout_history(b, message.chat.id)
            return
        if arg in ("rank", "top", "рейтинг", "топ"):
            await b._send(message.chat.id, b.mentors.scout.rank_text(), scout_kb(b))
            return
        await b._send(message.chat.id, b.mentors.scout.text(), scout_kb(b))

    @dp.message(Command("train", "training", "тренировка"))
    async def cmd_train(message: Message, command: CommandObject) -> None:
        if not b._allowed(message):
            return
        arg = (command.args or "").strip().lower()
        if arg.startswith("reset") or arg.startswith("заново"):
            uid = message.from_user.id if message.from_user else None
            if not await owner_only(uid, message.chat.id):
                return
            try:
                usd = float(arg.split()[1]) if len(arg.split()) > 1 else 10.0
            except ValueError:
                usd = 10.0
            b.mentors.train.reset(max(1.0, min(usd, 1000.0)))
            b.save_copy()
        if b.mentors.seed():
            b.save_copy()
        await b._send(message.chat.id, b.mentors.train.text(), train_kb(b))

    @dp.callback_query(F.data.startswith("tn:"))
    async def cb_train(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        parts = cb.data.split(":")
        action = parts[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        await cb.answer()
        tr = b.mentors.train
        if action == "show":
            await b._send(chat_id, tr.text(), train_kb(b))
            return
        if action == "help":
            await b._send(chat_id, TRAIN_HOW, train_kb(b))
            return
        if action == "trades":
            await b._send(chat_id, train_trades_text(b), train_kb(b))
            return
        if not await owner_only(cb.from_user.id, chat_id):
            return
        if action in ("signals", "notify", "toggle"):
            key = {"signals": "signals", "notify": "notify", "toggle": "on"}[action]
            tr.st[key] = not tr.st[key]
        elif action == "reset" and len(parts) > 2:
            tr.reset(float(parts[2]))
            await b._send(chat_id, f"♻️ Тренировка заново: у каждой стратегии ${float(parts[2]):g}. Выученные правила "
                                   "(стоп, тейк, лучшая стратегия) остались.")
        b.mentors._changed()
        b.save_copy()
        await b._send(chat_id, tr.text(), train_kb(b))

    @dp.callback_query(F.data.startswith("mn:"))
    async def cb_mentors(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        parts = cb.data.split(":", 2)
        action = parts[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        await cb.answer()
        mt = b.mentors
        if action == "show":
            await show(chat_id)
        elif action == "help":
            await b._send(chat_id, HOW_TEXT, keyboard(b))
        elif action == "tips":
            await b._send(chat_id, tips_text(b), keyboard(b))
        elif action == "calls":
            await b._send(chat_id, calls_text(b), keyboard(b))
        elif action == "list":
            await b._send(chat_id, list_text(b), list_kb(b))
        elif action == "scout":
            await b._send(chat_id, mt.scout.text(), scout_kb(b))
        elif action == "srank":
            await b._send(chat_id, mt.scout.rank_text(), scout_kb(b))
        elif action == "gcheck":
            await b._send(chat_id, "⏳ Проверяю ключ Gemini…")
            await b._send(chat_id, await gemini_check_text(b), keyboard(b))
        elif not await owner_only(cb.from_user.id, chat_id):
            return
        elif action == "run":
            if mt.busy:
                await b._send(chat_id, "⏳ Уже проверяю, подожди минуту.")
                return
            await b._send(chat_id, "⏳ Проверяю каналы и прогнозы…")
            for s in mt.active("youtube"):
                s["checked_at"] = 0.0
            await mentors_tick(b)
            await show(chat_id)
        elif action == "sfind":
            await scout_find(b, chat_id)
        elif action == "shist":
            await scout_history(b, chat_id)
        elif action == "stoggle":
            mt.scout.st["on"] = not mt.scout.st["on"]
            mt._changed()
            b.save_copy()
            await b._send(chat_id, mt.scout.text(), scout_kb(b))
        elif action == "notify":
            mt.st["notify"] = not mt.st["notify"]
            b.save_copy()
            await show(chat_id)
        elif action == "toggle":
            mt.st["on"] = not mt.st["on"]
            b.save_copy()
            await show(chat_id)
        elif action == "del" and len(parts) > 2:
            src = mt.st["sources"].get(parts[2])
            if src:
                mt.remove(src)
                b.save_copy()
            await b._send(chat_id, list_text(b), list_kb(b))
        elif action in ("add", "video") and len(parts) > 2:
            await add(chat_id, parts[2])


def link_offer(b: ArbitrageBot, text: str) -> tuple[str, InlineKeyboardMarkup] | None:
    """Пользователь просто прислал ссылку на YouTube/Twitch — предложить разобрать или следить."""
    p = parse_link(text) if len(text) < 300 and ("youtu" in text or "twitch.tv" in text) else None
    if not p:
        return None
    kind, value = p
    short = {"yt_video": f"https://youtu.be/{value}", "yt_channel": value, "yt_handle": f"@{value}",
             "twitch": f"twitch:{value}"}[kind][:52]  # данные кнопки — не длиннее 64 байт
    if kind == "yt_video":
        return ("🎓 Это ролик YouTube. Разобрать его (суть, прогнозы, советы, реклама) и проверить прогнозы ценой?",
                InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎓 Разобрать ролик",
                                                                            callback_data=f"mn:video:{short}")]]))
    label = "канал Twitch" if kind == "twitch" else "канал YouTube"
    return (f"🎓 Это {label}. Следить за ним и проверять советы ценой?",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Следить", callback_data=f"mn:add:{short}")]]))


async def mentors_tick(b: ArbitrageBot) -> None:
    mt = b.mentors
    if b.owner_id is None:
        return
    if mt.seed():
        await send_events(b, [{"text": "🎓 <b>Начал учиться у трейдеров</b>: слежу за 4 каналами YouTube и 5 "
                                       "memecoin-стримерами Twitch. Каждый их прогноз проверю ценой — через несколько "
                                       "дней будет видно, кому можно верить. /mentors", "buttons": []}])
    before = mt.copy.changes
    try:
        events = await mt.tick()
    except Exception as e:
        log.exception("Учёба у трейдеров")
        mt.last_error = str(e)[:200]
        events = []
    await send_events(b, events)
    if mt.copy.changes != before:
        b.save_copy()
        if events:
            await b._backup_now()


async def mentors_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(40)
    twitch_task = asyncio.create_task(b.mentors.twitch.run())
    try:
        while True:
            try:
                await mentors_tick(b)
            except Exception:
                log.exception("Ошибка учёбы у трейдеров")
            await asyncio.sleep(TICK_SEC if not b.mentors.twitch.queue.qsize() else 60)
    finally:
        twitch_task.cancel()


__all__ = ["register", "mentors_loop", "mentors_tick", "link_offer", "VIEWS"]
