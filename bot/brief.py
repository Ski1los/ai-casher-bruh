"""🧠 Штурман-ИИ: короткий брифинг «что происходит → главный риск → одно действие».

Берёт то, что бот уже знает о себе (здоровье, лента событий, Профит-центр, сделки за сегодня), и просит подключённый
ИИ объяснить это новичку простыми словами. Ответ кэшируется на 20 минут, чтобы не тратить бесплатные лимиты:
приложение показывает его на главной, а «Результат» автомата в Telegram дополняется комментарием штурмана.

ИИ здесь ничего не меняет и не торгует — только объясняет и советует (кнопки и команды бота).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from . import feed

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

CACHE_SEC = 20 * 60
FAIL_RETRY_SEC = 5 * 60
WAIT_SEC = 45

PROMPT = (
    "Ты — штурман торгового бота AI Casher (крипта, биржи Bybit/BingX, мемкоины Solana). Пользователь — новичок. "
    "По данным ниже дай брифинг строго в 3 строки:\n"
    "1) «Сейчас:» что происходит (одно предложение, с цифрами из данных);\n"
    "2) «Риск:» главный риск или проблема (если есть — как исправить);\n"
    "3) «Шаг:» одно конкретное действие — команда бота (/autonomy, /scalp, /keys, /doctor, /profit, /copy, /meme, "
    "/setup) или «ничего делать не нужно».\n"
    "Не обещай прибыль, не выдумывай цифры, без воды, до 60 слов, простой текст без markdown."
)


def context(b: ArbitrageBot) -> dict:
    """Короткая сводка о боте для ИИ (без ключей и личных данных)."""
    now = time.time()
    t = b.trader
    out: dict = {"режим": "реальные деньги" if t.state.live else "тест (ордера не уходят)",
                 "биржи_с_ключом": list(t.working),
                 "сегодня": {"сделок": t.state.day_trades, "итог_usd": round(t.state.day_pnl_usd, 4)}}
    try:
        h = b.autonomy.health(now)
        out["здоровье"] = h["score"]
        out["проблемы"] = [f"{i['title']}: {i['hint']}" for i in h["issues"]][:4]
        out["автомат"] = "включён" if b.autonomy.on else "выключен"
        out["движки"] = [f"{a}{'' if o else ' (выкл)'}" for a, o, _n in b.autonomy.engines_status()]
    except Exception as e:
        log.debug("brief health: %s", e)
    try:
        d = b.profit.status_dict()
        out["профит_центр"] = [{"способ": e["title"], "сделок": e["n"], "в_среднем_%": round(e["mean"], 3),
                                "вердикт": e["verdict"]} for e in d["engines"][:5]]
    except Exception as e:
        log.debug("brief profit: %s", e)
    out["лента_за_сутки"] = feed.counts(b, now - 86400)
    out["последние_события"] = [x["text"][:140] for x in feed.items(b, 8)]
    return out


async def make(b: ArbitrageBot, force: bool = False) -> dict:
    """{"text", "at", "model", "error"} — из кэша, если свежий."""
    cur = getattr(b, "_brief", None) or {}
    now = time.time()
    fresh = CACHE_SEC if cur.get("text") else FAIL_RETRY_SEC
    if cur and not force and now - cur.get("at", 0) < fresh:
        return cur
    if not b.ai.configured:
        return {"text": "", "at": now, "model": None,
                "error": "ИИ не подключён: добавь бесплатный ключ (Groq, OpenRouter, Gemini…) — /keys"}
    lock = getattr(b, "_brief_lock", None)
    if lock is None:
        lock = b._brief_lock = asyncio.Lock()
    if lock.locked():  # уже думает — отдаём старое, а не ставим второй запрос
        return cur or {"text": "", "at": now, "model": None, "error": "думаю…"}
    async with lock:
        messages = [{"role": "system", "content": PROMPT},
                    {"role": "user", "content": json.dumps(context(b), ensure_ascii=False)}]
        try:
            text = await asyncio.wait_for(b.ai.client.chat(messages, max_tokens=300, background=True), WAIT_SEC)
            res = {"text": clean(text), "at": time.time(), "model": getattr(b.ai.client, "last_model", None),
                   "error": None}
        except Exception as e:
            log.info("Штурман-ИИ: %s", e)
            res = {"text": cur.get("text", ""), "at": time.time(), "model": cur.get("model"),
                   "error": (str(e) or type(e).__name__)[:200]}
        b._brief = res
        return res


def clean(text: str) -> str:
    t = (text or "").replace("**", "").replace("__", "").replace("#", "").strip()
    lines = [ln.strip(" -•*") for ln in t.splitlines() if ln.strip()]
    return "\n".join(lines[:5])[:600]
