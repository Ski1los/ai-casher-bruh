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
import json
import logging
import os

import aiohttp
from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaDocument
from aiohttp import web

log = logging.getLogger(__name__)

BACKUP_FILENAME = "bot_state.json"
BACKUP_CAPTION = "💾 Резервная копия бота (демо-счёт и настройки). Не открепляй это сообщение."
SELF_PING_SEC = 10 * 60


# ---------------------------------------------------------------------------
# HTTP-сервер и самопинг
# ---------------------------------------------------------------------------


async def start_health_server(port: int) -> web.AppRunner:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
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
            data = json.dumps(state, ensure_ascii=False).encode("utf-8")
            file = BufferedInputFile(data, filename=BACKUP_FILENAME)
            if self.message_id:
                try:
                    await self.bot.edit_message_media(
                        media=InputMediaDocument(media=file, caption=BACKUP_CAPTION),
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                    )
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
                await self.bot.pin_chat_message(self.chat_id, msg.message_id, disable_notification=True)
            except Exception as e:
                log.warning("Бэкап: не удалось сохранить: %s", e)
