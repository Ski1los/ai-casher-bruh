import asyncio
import io
import socket
from types import SimpleNamespace

import aiohttp

from bot.cloud import BACKUP_FILENAME, TelegramBackup, start_health_server


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_health_server_responds():
    async def run():
        port = free_port()
        runner = await start_health_server(port)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as resp:
                    return resp.status, await resp.text()
        finally:
            await runner.cleanup()

    assert asyncio.run(run()) == (200, "ok")


class FakeTelegram:
    """Минимальная имитация Bot API: один чат, закреплённое сообщение с файлом."""

    def __init__(self):
        self.files: dict[int, bytes] = {}
        self.pinned: int | None = None
        self.next_id = 1
        self.edits = 0

    def _doc(self, mid):
        return SimpleNamespace(file_name=BACKUP_FILENAME, file_id=str(mid))

    async def get_chat(self, chat_id):
        pm = None
        if self.pinned:
            pm = SimpleNamespace(message_id=self.pinned, document=self._doc(self.pinned))
        return SimpleNamespace(pinned_message=pm)

    async def download(self, document):
        return io.BytesIO(self.files[int(document.file_id)])

    async def send_document(self, chat_id, file, caption=None, disable_notification=False):
        mid = self.next_id
        self.next_id += 1
        self.files[mid] = file.data
        return SimpleNamespace(message_id=mid)

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        self.pinned = message_id

    async def edit_message_media(self, media, chat_id, message_id):
        self.files[message_id] = media.media.data
        self.edits += 1


def test_backup_roundtrip_survives_restart():
    tg = FakeTelegram()

    async def run():
        # Первый запуск: бэкапа нет, сохраняем дважды (второй раз через редактирование)
        b1 = TelegramBackup(tg, chat_id=42)
        assert await b1.restore() is None
        await b1.save({"demo": {"balance": 5}})
        await b1.save({"demo": {"balance": 5.2}})
        assert tg.edits == 1 and len(tg.files) == 1

        # «Перезапуск сервера»: новый объект, данные берутся из закреплённого сообщения
        b2 = TelegramBackup(tg, chat_id=42)
        return await b2.restore(), b2.message_id

    state, mid = asyncio.run(run())
    assert state == {"demo": {"balance": 5.2}}
    assert mid == tg.pinned


def _make_bot(tb, demo_mod, tmp_path, subs_name):
    from bot.config import Settings
    from bot.demo_manager import DemoManager

    b = tb.ArbitrageBot("123456:" + "A" * 35, Settings(exchanges=[]), [42])
    b.subscribers = tb.SubscriberStore(tmp_path / subs_name)
    b.demo = DemoManager(b.scanner, b.settings, tmp_path / "demo.json")
    return b


def _patch_paths(monkeypatch, tmp_path):
    import bot.demo as demo_mod
    import bot.telegram_bot as tb

    monkeypatch.setattr(demo_mod, "DEMO_FILE", tmp_path / "demo.json")
    monkeypatch.setattr(tb, "DEMO_FILE", tmp_path / "demo.json")
    monkeypatch.setattr(tb, "SUBSCRIBERS_FILE", tmp_path / "subs.json")
    monkeypatch.setattr(tb, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(tb, "TRADING_FILE", tmp_path / "trading.json")
    monkeypatch.setattr(tb, "COPY_FILE", tmp_path / "copytrade.json")
    return tb, demo_mod


def test_bot_restores_demo_from_backup(tmp_path, monkeypatch):
    tb, demo_mod = _patch_paths(monkeypatch, tmp_path)

    async def run():
        tg = FakeTelegram()
        b = _make_bot(tb, demo_mod, tmp_path, "subs.json")
        b.backup = TelegramBackup(tg, 42)
        b.demo.start([5, 50], 42)
        b.subscribers.add(42)
        await b._backup_now()

        # Перезапуск: локальные файлы стёрлись
        (tmp_path / "demo.json").unlink()
        b2 = _make_bot(tb, demo_mod, tmp_path, "subs2.json")
        b2.backup = TelegramBackup(tg, 42)
        await b2._restore_backup()
        result = (sorted(b2.demo.accounts), b2.demo.active, b2.subscribers.chats)
        for x in (b, b2):
            await x.bot.session.close()
        return result

    accounts, active, subs = asyncio.run(run())
    assert accounts == ["cross-5", "cross-50", "tri-5", "tri-50"]
    assert active and subs == {42}


def test_restore_old_v1_backup_format(tmp_path, monkeypatch):
    """Бэкап, сделанный первой версией бота (один межбиржевой счёт), должен подняться."""
    tb, demo_mod = _patch_paths(monkeypatch, tmp_path)
    old_state = {
        "version": 1,
        "subscribers": [42],
        "demo": {
            "active": True, "chat_id": 42, "start_balance": 5.0, "balance": 5.0, "location": None,
            "created_at": 1.0, "open_trade": None, "trades": [], "rejects": {"fees": 3}, "scans": 10,
            "best_rejected": None, "best_rejected_percent": 0.0,
        },
    }

    async def run():
        tg = FakeTelegram()
        await TelegramBackup(tg, 42).save(old_state)
        b = _make_bot(tb, demo_mod, tmp_path, "subs.json")
        b.backup = TelegramBackup(tg, 42)
        await b._restore_backup()
        acc = b.demo.accounts.get("cross-5")
        await b.bot.session.close()
        return acc

    acc = asyncio.run(run())
    assert acc is not None and acc.rejects == {"fees": 3} and acc.scans == 10


def test_status_page_shows_problem_and_env_without_secrets():
    from bot import cloud

    cloud.set_status("problem", "Не задан TELEGRAM_BOT_TOKEN", ["Открой @BotFather"])
    try:
        env = {"BYBIT_API_KEY": "SUPERSECRETKEY", "ALLOWED_USER_IDS": "6069246522"}
        page = cloud.status_html(env)
        assert "❌ Не задан TELEGRAM_BOT_TOKEN" in page and "Открой @BotFather" in page
        assert "SUPERSECRETKEY" not in page and "6069246522" not in page  # значения не показываются
        assert "<b>НЕ задана — обязательно</b>" in page  # токена нет
        cloud.set_status("running", bot="my_bot")
        assert "✅ Бот работает" in cloud.status_html(env) and "@my_bot" in cloud.status_html(env)
    finally:
        cloud.set_status("starting")
        cloud.STATUS["bot"] = None
