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

from .mentors import TICK_SEC, VIEWS, MentorError, parse_link

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
            [InlineKeyboardButton(text="🧠 Советы", callback_data="mn:tips"),
             InlineKeyboardButton(text="📈 Прогнозы", callback_data="mn:calls"),
             InlineKeyboardButton(text="👥 Трейдеры", callback_data="mn:list")],
            [InlineKeyboardButton(text="🔔 Сообщать о роликах" if not st["notify"] else "🔕 Не сообщать о роликах",
                                  callback_data="mn:notify"),
             InlineKeyboardButton(text="⏸ Выключить" if st["on"] else "🟢 Включить", callback_data="mn:toggle")],
            [InlineKeyboardButton(text="❔ Как это работает", callback_data="mn:help")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
        icon = {"star": "⭐", "watch": "👀", "muted": "🔇"}[s["status"]]
        where = "▶️ YouTube" if s["kind"] == "youtube" else "📺 Twitch" + (" · 🟢 чат" if s["live_chat"] else "")
        lines.append(f"{icon} <b>{html.escape(s['name'])}</b> ({where}) — {html.escape(s['note'] or '')}"
                     + (f"\n   ⚠️ {html.escape(s['error'])}" if s["error"] else ""))
    return "\n".join(lines)


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
    e = mt.video_event(rec, src)
    if rec["tips"][1:]:
        e["text"] += "\nЕщё советы: " + "; ".join(html.escape(t["text"]) for t in rec["tips"][1:4])
    if src:
        e["text"] += f"\n\n▶️ Канал «{html.escape(src['name'])}» добавил в список — новые ролики разберу сам."
    await send_events(b, [e])


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
        if cmd in ("add", "добавить", "video", "видео", "ролик") and rest:
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
