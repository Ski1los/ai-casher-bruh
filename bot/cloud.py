"""Всё, что нужно для бесплатного облака (Render):

* HTTP-сервер с /health: Render запускает только веб-сервисы, которые слушают порт;
* самопинг: бесплатный сервис засыпает через 15 минут без входящих запросов,
  поэтому бот сам стучится на свой публичный адрес каждые 10 минут;
* резервная копия в Telegram: на бесплатном тарифе файлы стираются при каждом
  перезапуске, поэтому состояние (демо-счёт, подписки) хранится файлом
  в закреплённом сообщении в твоём чате с ботом.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import time

import aiohttp
from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaDocument
from aiohttp import web

log = logging.getLogger(__name__)

BACKUP_FILENAME = "bot_state.json"
BACKUP_CAPTION = "💾 Резервная копия бота (демо-счёт и настройки). Не открепляй это сообщение."
SELF_PING_SEC = 10 * 60
BOT_VERSION = "46"  # растёт с каждым обновлением: по нему видно, что Render поставил новую версию


def version_text() -> str:
    """«v9 (adc7bd5)»: номер версии и коммит, который Render развернул (RENDER_GIT_COMMIT)."""
    commit = os.getenv("RENDER_GIT_COMMIT", "").strip()[:7]
    return f"v{BOT_VERSION}" + (f" ({commit})" if commit else "")


def instance_text() -> str:
    """Где работает эта копия бота: имя сервиса Render и адрес (чтобы найти лишнюю копию)."""
    name = os.getenv("RENDER_SERVICE_NAME", "").strip()
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if name or host:
        return "Render: " + " · ".join(x for x in (name, host) if x)
    return "не Render (другой хостинг или компьютер)"


# ---------------------------------------------------------------------------
# Страница состояния: открывается в браузере телефона по адресу сервиса (…onrender.com/status)
# ---------------------------------------------------------------------------

# Переменные, про которые страница говорит «задана / нет». Значения НИКОГДА не показываются.
ENV_SHOWN = (
    ("TELEGRAM_BOT_TOKEN", "токен бота от @BotFather", True),
    ("ALLOWED_USER_IDS", "твой Telegram ID (владелец)", True),
    ("BYBIT_API_KEY", "ключ Bybit", False),
    ("BYBIT_API_SECRET", "секрет Bybit", False),
    ("BINGX_API_KEY", "ключ BingX", False),
    ("BINGX_API_SECRET", "секрет BingX", False),
    ("AI_API_KEY", "ключ ИИ (Groq, или Gemini в GEMINI_API_KEY)", False),
    ("PHANTOM_ADDRESS", "адрес Phantom", False),
    ("RENDER_DEPLOY_HOOK_URL", "ссылка Deploy Hook — бот обновляется сам", False),
)

# Что сейчас с ботом: starting | running | problem. Обновляют main.py и ArbitrageBot.run().
STATUS: dict = {"state": "starting", "bot": None, "title": "", "steps": [], "since": time.time()}


def set_status(state: str, title: str = "", steps: list[str] | None = None, bot: str | None = None) -> None:
    STATUS.update(state=state, title=title, steps=list(steps or []), since=time.time())
    if bot is not None:
        STATUS["bot"] = bot


def _env_rows(env=None) -> list[tuple[str, str, bool, bool]]:
    env = os.environ if env is None else env
    rows = []
    for key, what, required in ENV_SHOWN:
        value = (env.get(key) or "").strip()
        if key == "AI_API_KEY" and not value:
            from .ai import ai_keys_present  # любой ключ ИИ (Groq, Gemini, Cerebras, Mistral, …)
            value = "1" if ai_keys_present(env) else ""
        rows.append((key, what, bool(value), required))
    return rows


def status_html(env=None) -> str:
    """Простая страница на русском: работает ли бот, что не так и что нажать. Без секретов."""
    state = STATUS["state"]
    head = {"running": ("✅", "Бот работает"), "starting": ("⏳", "Бот запускается…"),
            "problem": ("❌", "Бот не запустился")}[state if state in ("running", "starting", "problem") else "problem"]
    lines = [f"<h1>{head[0]} {html.escape(STATUS.get('title') or head[1])}</h1>"]
    if STATUS.get("bot"):
        lines.append(f"<p>Бот в Telegram: <b>@{html.escape(STATUS['bot'])}</b>. Напиши ему /start.</p>")
    if STATUS.get("steps"):
        lines.append("<h2>Что сделать</h2><ol>" + "".join(f"<li>{s}</li>" for s in STATUS["steps"]) + "</ol>")
    lines.append("<h2>Переменные в Render → Environment</h2><table>")
    for key, what, ok, required in _env_rows(env):
        icon = "✅" if ok else ("❌" if required else "➖")
        note = "задана" if ok else ("<b>НЕ задана — обязательно</b>" if required else "не задана (по желанию)")
        lines.append(f"<tr><td>{icon}</td><td><code>{key}</code><br><small>{html.escape(what)}</small></td><td>{note}</td></tr>")
    lines.append("</table>")
    if STATUS.get("update"):
        lines.append(f"<p>⬆️ {html.escape(STATUS['update'])}. Подробно: команда /update в боте.</p>")
    lines.append(f"<p><small>Версия бота: {html.escape(version_text())}. Значения ключей здесь не показываются никогда. "
                 "Страница обновляется сама раз в 20 секунд.</small></p>")
    return ("<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><meta http-equiv=refresh content=20>"
            "<title>Состояние бота</title><style>body{font-family:system-ui,sans-serif;max-width:640px;margin:20px auto;"
            "padding:0 14px;line-height:1.45;background:#0f1115;color:#e8e8e8}td{padding:6px;vertical-align:top;"
            "border-bottom:1px solid #2a2d35}table{border-collapse:collapse;width:100%}code{color:#8fd3ff}"
            "li{margin:6px 0}h1{font-size:22px}h2{font-size:17px;margin-top:22px}</style></head><body>"
            + "".join(lines) + "</body></html>")


# ---------------------------------------------------------------------------
# HTTP-сервер и самопинг
# ---------------------------------------------------------------------------


async def start_health_server(port: int, setup=None) -> web.AppRunner:
    """HTTP-сервер: /health для Render, /status (страница состояния) и (если передан setup) Mini App с API."""

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def root(_: web.Request) -> web.Response:
        if setup and STATUS["state"] == "running":
            raise web.HTTPFound("/app")
        return await status(_)

    async def status(_: web.Request) -> web.Response:
        return web.Response(text=status_html(), content_type="text/html")

    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/status", status)
    if setup:
        setup(app)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP-сервер для облака слушает порт %s", port)
    return runner


async def self_ping_loop(url: str) -> None:
    """Не даёт бесплатному Render усыпить сервис."""
    target = url.rstrip("/") + "/health"
    log.info("Самопинг включён: %s каждые %d мин", target, SELF_PING_SEC // 60)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while True:
            await asyncio.sleep(SELF_PING_SEC)
            try:
                async with session.get(target) as resp:
                    log.debug("Самопинг: %s", resp.status)
            except Exception as e:
                log.warning("Самопинг не удался: %s", e)


async def serve_problem_forever(port: int) -> None:
    """Режим спасения: бот не запустился (нет токена, токен отклонён). Не падаем, а держим страницу
    состояния, чтобы с телефона было видно, что не так. Render после правки Environment перезапустит сервис сам."""
    runner = await start_health_server(port)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def cloud_port() -> int | None:
    value = os.getenv("PORT", "").strip()
    return int(value) if value.isdigit() else None


def public_url() -> str | None:
    # Render сам задаёт RENDER_EXTERNAL_URL; для других хостингов можно указать PUBLIC_URL
    return os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PUBLIC_URL") or None


# ---------------------------------------------------------------------------
# Резервная копия в Telegram
# ---------------------------------------------------------------------------


class TelegramBackup:
    """Хранит JSON-файл состояния в закреплённом сообщении чата владельца."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id: int | None = None
        self._lock = asyncio.Lock()
        self._last_digest: bytes | None = None  # не загружаем тот же файл повторно

    async def restore(self) -> dict | None:
        try:
            chat = await self.bot.get_chat(self.chat_id)
        except Exception as e:
            log.warning("Бэкап: не могу открыть чат %s (%s). Напиши боту /start", self.chat_id, e)
            return None
        pinned = chat.pinned_message
        if not pinned or not pinned.document or pinned.document.file_name != BACKUP_FILENAME:
            return None
        self.message_id = pinned.message_id
        try:
            buf = await self.bot.download(pinned.document)
            return json.loads(buf.read().decode("utf-8"))
        except Exception as e:
            log.warning("Бэкап: не удалось прочитать файл: %s", e)
            return None

    async def save(self, state: dict) -> None:
        async with self._lock:
            data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            digest = hashlib.blake2b(data, digest_size=16).digest()
            if digest == self._last_digest and self.message_id:
                return  # ничего не изменилось — не тратим запрос к Telegram
            file = BufferedInputFile(data, filename=BACKUP_FILENAME)
            if self.message_id:
                try:
                    await self.bot.edit_message_media(
                        media=InputMediaDocument(media=file, caption=BACKUP_CAPTION),
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                    )
                    self._last_digest = digest
                    return
                except Exception as e:
                    log.info("Бэкап: не удалось обновить старое сообщение (%s), создаю новое", e)
            try:
                msg = await self.bot.send_document(
                    self.chat_id,
                    BufferedInputFile(data, filename=BACKUP_FILENAME),
                    caption=BACKUP_CAPTION,
                    disable_notification=True,
                )
                self.message_id = msg.message_id
                self._last_digest = digest
                await self.bot.pin_chat_message(self.chat_id, msg.message_id, disable_notification=True)
            except Exception as e:
                log.warning("Бэкап: не удалось сохранить: %s", e)
