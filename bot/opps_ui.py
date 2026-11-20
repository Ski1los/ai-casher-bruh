"""Telegram: /earn — «💡 Как заработать сейчас» (акции Bybit, проценты Earn, новости, план на $5)."""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import TYPE_CHECKING

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .opportunities import ALERT_KINDS, KINDS, PLAN_QUESTION, Promo, risk_coins

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

OPPS_EVERY_SEC = 15 * 60
HONEST = ("<i>Честно: с $5 самые большие проценты дают бесплатные акции (Launchpool, аирдропы, задания), "
          "а не трейдинг — но в долларах это центы и единицы долларов, не сотни. Гарантий нет.</i>")


def held_coins(b: ArbitrageBot) -> set[str]:
    """Монеты, которые есть на биржах (по последнему чтению балансов)."""
    out = set()
    for _, rows in b.trader.balance_cache.values():
        out |= {c for c, _, total, _ in rows if total > 0}
    out |= {p["symbol"].split("/")[0] for p in b.scalper.positions.values() if p}
    return out


def known_coins(b: ArbitrageBot) -> set[str]:
    out = set()
    for ex in b.scanner.exchanges.values():
        for m in (getattr(ex, "markets", None) or {}).values():
            if m.get("base"):
                out.add(m["base"])
    return out


async def snapshot(b: ArbitrageBot, force: bool = False) -> dict:
    snap = await b.opps.snapshot(held_coins(b), known_coins(b), force=force)
    b.opps_last = snap
    return snap


def _date(ts: float | None) -> str:
    return time.strftime("%d.%m", time.gmtime(ts + 3 * 3600)) if ts else ""


def promo_line(p: dict, full: bool = False) -> str:
    end = f" · до {_date(p['end'])}" if p.get("end") else ""
    line = (f"{p['icon']} <b>{html.escape(p['kind_name'])}</b> · {p['fit_text']}{end}\n"
            f"<a href=\"{html.escape(p['url'], quote=True)}\">{html.escape(p['title'][:140])}</a>")
    if full:
        line += f"\n<i>Что делать:</i> {html.escape(p['todo'])}"
    return line


def earn_lines(rows: list[dict], usd: float = 5.0) -> list[str]:
    out = []
    for r in rows[:8]:
        month = usd * r["apr"] / 100 / 12
        kind = "гибкий, забрать в любой момент" if r["category"] == "FlexibleSaving" else "on-chain стейкинг"
        out.append(f"• <b>{html.escape(r['coin'])}</b>: {r['apr']:.2f}% годовых ({kind}) — с ${usd:g} ≈ ${month:.3f} в месяц")
    return out


def main_text(b: ArbitrageBot, snap: dict) -> str:
    lines = ["💡 <b>Как заработать сейчас</b>", ""]
    held = [p for p in snap["delistings"] if p.get("held")]
    for p in held:
        lines.append(f"🚨 <b>Bybit уберёт монету, которая у тебя есть:</b> "
                     f"<a href=\"{html.escape(p['url'], quote=True)}\">{html.escape(p['title'][:120])}</a>. Продай заранее.")
    if held:
        lines.append("")
    good = [p for p in snap["promos"] if p["fit"] in ("yes", "maybe")]
    lines.append(f"🎁 <b>Акции Bybit для маленькой суммы:</b> {len(good)}")
    for p in good[:3]:
        lines.append(promo_line(p))
    if not good:
        lines.append("сейчас нет — бот напишет, как только появятся")
    stable = [r for r in snap["earn"] if r["coin"] in ("USDT", "USDC")]
    if stable:
        best = max(stable, key=lambda r: r["apr"])
        lines += ["", f"💰 <b>Проценты:</b> {best['coin']} — до {best['apr']:.2f}% годовых в Bybit Earn"]
    if snap["listings"]:
        lines += ["", f"🆕 <b>Новые листинги:</b> {len(snap['listings'])} (подробно — кнопка)"]
    if snap["news"]:
        lines += ["", "📰 <b>Главное в новостях:</b>"]
        lines += [f"• <a href=\"{html.escape(n['url'], quote=True)}\">{html.escape(n['title'][:110])}</a>"
                  for n in snap["news"][:3]]
    if snap["errors"]:
        lines += ["", "⚠️ Не загрузилось: " + html.escape(", ".join(snap["errors"])) + " — попробую позже"]
    lines += ["", "🎯 Плюс режим $5 на биржах: /scalp", "", HONEST]
    return "\n".join(lines)


def keyboard(b: ArbitrageBot, uid: int | None) -> InlineKeyboardMarkup:
    on = b.opps_notify
    rows = [[InlineKeyboardButton(text="🎁 Акции", callback_data="op:promos"),
             InlineKeyboardButton(text="💰 Проценты", callback_data="op:earn")],
            [InlineKeyboardButton(text="🆕 Листинги", callback_data="op:list"),
             InlineKeyboardButton(text="📰 Новости", callback_data="op:news")],
            [InlineKeyboardButton(text="🤖 План для моих $5", callback_data="op:plan")],
            [InlineKeyboardButton(text=("🔔 Уведомления об акциях: вкл" if on else "🔕 Уведомления об акциях: выкл"),
                                  callback_data="op:notify"),
             InlineKeyboardButton(text="🔄", callback_data="op:show")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def section_text(snap: dict, section: str) -> str:
    if section == "promos":
        items = snap["promos"]
        head = "🎁 <b>Акции Bybit</b> (сначала подходящие для $5)"
        body = [promo_line(p, full=True) for p in items[:8]] or ["Сейчас активных акций нет."]
        tail = ("\n\nКак участвовать: открой ссылку → «Участвовать/Join» в приложении Bybit. "
                "Бот сам в акции не вкладывает: у его ключа есть права только на спот-торговлю, так безопаснее.")
        return head + "\n\n" + "\n\n".join(body) + tail
    if section == "earn":
        body = earn_lines(snap["earn"]) or ["Проценты не загрузились, попробуй позже."]
        return ("💰 <b>Bybit Earn — проценты за хранение</b>\n\n" + "\n".join(body)
                + "\n\nКуда нажать: приложение Bybit → Финансы (Earn) → Easy Earn → монета → Подписаться.\n"
                "⚠️ Деньги в Earn не торгуются в режиме $5. Это для денег, которые просто лежат. "
                "Проценты меняются каждый день; «on-chain» может требовать ожидания при выводе.")
    if section == "list":
        items = snap["listings"] + snap["delistings"]
        body = [promo_line(p, full=True) for p in items[:8]] or ["Новых листингов и делистингов нет."]
        return "🆕 <b>Листинги и делистинги Bybit</b>\n\n" + "\n\n".join(body)
    if section == "news":
        body = []
        for n in snap["news"][:10]:
            coins = f" · {', '.join(n['coins'])}" if n["coins"] else ""
            body.append(f"• <a href=\"{html.escape(n['url'], quote=True)}\">{html.escape(n['title'][:140])}</a> "
                        f"<i>({html.escape(n['source'])}{coins})</i>")
        return ("📰 <b>Новости крипты</b>\n\n" + ("\n".join(body) or "Новости не загрузились, попробуй позже.")
                + "\n\n<i>Новость — не сигнал покупать: к моменту публикации цена обычно уже сдвинулась. "
                "Спроси ИИ, что из этого важно для твоих $5: кнопка «🤖 План».</i>")
    return ""


def alert_text(items: list[Promo]) -> str:
    lines = ["🔔 <b>Новые акции Bybit</b>"]
    for p in items[:5]:
        lines.append("")
        lines.append(promo_line(p.as_dict(), full=True))
    lines.append("\nВсе возможности: /earn · выключить уведомления: там же кнопкой")
    return "\n".join(lines)


async def opps_once(b: ArbitrageBot) -> None:
    """Новые объявления Bybit: акции (если уведомления включены) и делистинг монеты, которая есть у тебя (всегда)."""
    items = await b.opps.announcements(force=True)
    try:  # щит автопилота: делистинги и плохие новости — эти монеты бот не покупает
        news = await b.opps.news(known_coins(b))
    except Exception as e:
        log.info("Новости для щита: %s", e)
        news = []
    b.scalper.set_risk(risk_coins(items, news))
    new = b.opps.fresh(items)
    if not new or b.owner_id is None:
        return
    held = held_coins(b)
    danger = [p for p in new if p.kind == "delisting" and any(c in held for c in p.coins)]
    for p in danger:
        await b._send(b.owner_id, f"🚨 <b>Bybit уберёт монету, которая у тебя есть</b>\n{promo_line(p.as_dict(), full=True)}")
    promos = [p for p in new if p.kind in ALERT_KINDS]
    if promos and b.opps_notify:
        await b._send(b.owner_id, alert_text(promos))


async def opps_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await opps_once(b)
        except Exception:
            log.exception("Ошибка проверки акций")
        await asyncio.sleep(OPPS_EVERY_SEC)


def register(b: ArbitrageBot) -> None:
    dp = b.dp

    async def show(chat_id: int, uid: int | None, force: bool = False) -> None:
        snap = await snapshot(b, force=force)
        await b._send(chat_id, main_text(b, snap), keyboard(b, uid))

    @dp.message(Command("earn", "opps", "bonus", "zarabotok"))
    async def cmd_earn(message: Message) -> None:
        if not b._allowed(message):
            return
        await message.answer("💡 Собираю акции Bybit, проценты и новости…")
        await show(message.chat.id, message.from_user.id if message.from_user else None)

    @dp.message(Command("news"))
    async def cmd_news(message: Message) -> None:
        if not b._allowed(message):
            return
        snap = await snapshot(b)
        await b._send(message.chat.id, section_text(snap, "news"), keyboard(b, None))

    @dp.callback_query(F.data.startswith("op:"))
    async def cb_opps(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        if action == "notify":
            if b.owner_id is not None and cb.from_user.id != b.owner_id:
                await cb.answer("Только владелец", show_alert=True)
                return
            b.opps_notify = not b.opps_notify
            await b.save_extras()
            await cb.answer("Уведомления " + ("включены 🔔" if b.opps_notify else "выключены 🔕"))
            try:
                await cb.message.edit_reply_markup(reply_markup=keyboard(b, cb.from_user.id))
            except Exception:
                pass
            return
        await cb.answer()
        if action == "show":
            await show(chat_id, cb.from_user.id, force=True)
        elif action == "plan":
            if not b.ai.configured:
                await b._send(chat_id, "🤖 ИИ не подключён: добавь AI_API_KEY в Render (бесплатный ключ Groq). Подробно: /ai")
                return
            await snapshot(b)
            await b._refresh_balances()
            await b._ai_respond(chat_id, lambda: b.ai.ask(chat_id, PLAN_QUESTION, b.ai_context()))
        else:
            snap = b.opps_last or await snapshot(b)
            text = section_text(snap, action)
            if text:
                await b._send(chat_id, text, keyboard(b, cb.from_user.id))


__all__ = ["register", "opps_loop", "opps_once", "snapshot", "KINDS"]
