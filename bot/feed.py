"""📡 Лента событий: всё, что бот сделал сам, в одном месте (приложение и /feed).

Сюда попадает каждое уведомление бота (сделки, мемкоины, копитрейдинг, предупреждения, решения автомата) и каждая
починка сторожа — даже если уведомления выключены (/notify) или «тихие часы» отложили их до утра. Хранится последние
FEED_MAX записей вместе с состоянием автомата (copytrade.json → бэкап), поэтому лента переживает перезапуск.
"""

from __future__ import annotations

import html
import re
import time
from typing import TYPE_CHECKING

from aiogram.filters import Command
from aiogram.types import Message

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

FEED_MAX = 60
TEXT_MAX = 240
KINDS = ("trade", "meme", "auto", "warn", "info")
KIND_TITLE = {"trade": "сделки", "meme": "мемкоины", "auto": "автомат", "warn": "проблемы", "info": "прочее"}

_TAG = re.compile(r"<[^>]+>")
_WARN = ("⚠️", "⛔", "🚨", "❌", "🔑", "⏸", "📥")
_AUTO = ("♾", "🔁", "🧹", "🛡", "🧠", "⬆️", "🆕", "👋")
_MEME = ("🐋", "🪙", "💸", "🦎", "📡", "⚡", "🎓", "🎥", "🔭", "🏋️", "🪞")
_TRADE = ("⚙️", "🤖", "🔺", "🎯", "💰", "📈", "📉", "🔴", "🧪", "📱", "✅")


def plain(text: str) -> str:
    """HTML Telegram → одна строка простого текста."""
    t = html.unescape(_TAG.sub("", text or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= TEXT_MAX else t[:TEXT_MAX - 1].rstrip() + "…"


def kind_of(text: str) -> str:
    t = (text or "").lstrip()
    for k, marks in (("warn", _WARN), ("auto", _AUTO), ("meme", _MEME), ("trade", _TRADE)):
        if t.startswith(marks):
            return k
    if any(m in t[:80] for m in ("⚠️", "⛔", "🚨", "❌")):
        return "warn"
    low = t.lower()
    if "мемкоин" in low or "кошел" in low:
        return "meme"
    if "сделк" in low or "ордер" in low:
        return "trade"
    return "info"


def _store(b: ArbitrageBot) -> list | None:
    auto = getattr(b, "autonomy", None)
    if auto is None:
        return None
    try:
        st = auto.st
    except Exception:  # фейки в тестах без копитрейдинга
        return None
    feed = st.get("feed")
    if not isinstance(feed, list):
        feed = st["feed"] = []
    return feed


def add(b: ArbitrageBot, text: str, kind: str | None = None, at: float | None = None) -> dict | None:
    feed = _store(b)
    t = plain(text)
    if feed is None or not t:
        return None
    k = kind if kind in KINDS else kind_of(t)
    at = time.time() if at is None else at
    if feed and feed[-1].get("text") == t and at - feed[-1].get("at", 0) < 60:
        return feed[-1]  # одно и то же подряд — не дублируем
    item = {"at": round(at, 1), "kind": k, "text": t}
    feed.append(item)
    del feed[:-FEED_MAX]
    return item


def items(b: ArbitrageBot, n: int = FEED_MAX, kind: str | None = None) -> list[dict]:
    feed = _store(b) or []
    out = [x for x in reversed(feed) if isinstance(x, dict) and (kind is None or x.get("kind") == kind)]
    return out[:n]


def counts(b: ArbitrageBot, since: float) -> dict[str, int]:
    out = dict.fromkeys(KINDS, 0)
    for x in _store(b) or []:
        if isinstance(x, dict) and x.get("at", 0) >= since and x.get("kind") in out:
            out[x["kind"]] += 1
    return out


ICON = {"trade": "💹", "meme": "🐋", "auto": "♾", "warn": "⚠️", "info": "•"}


def text(b: ArbitrageBot, n: int = 15) -> str:
    rows = items(b, n)
    if not rows:
        return ("📡 <b>Лента событий</b>\nПока пусто: здесь появится всё, что бот делает сам — сделки, мемкоины, "
                "решения автомата и починки.")
    off = b.settings.report_utc_offset * 3600
    day = 24 * 3600
    c = counts(b, time.time() - day)
    lines = ["📡 <b>Лента событий</b> — что бот сделал сам",
             "За сутки: " + " · ".join(f"{KIND_TITLE[k]} {c[k]}" for k in KINDS if c[k]) if any(c.values()) else ""]
    for x in rows:
        when = time.strftime("%d.%m %H:%M", time.gmtime(x["at"] + off))
        lines.append(f"<code>{when}</code> {html.escape(x['text'][:160])}")
    lines.append("\nВсё и с фильтрами — в приложении (кнопка «Лента»).")
    return "\n".join(line for line in lines if line)


def register(b: ArbitrageBot) -> None:
    @b.dp.message(Command("feed", "лента", "events"))
    async def cmd_feed(message: Message) -> None:
        if not b._allowed(message):
            return
        await b._answer(message, text(b))
