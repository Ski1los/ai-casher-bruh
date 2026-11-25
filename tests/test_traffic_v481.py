"""v48.1: 📶 учёт трафика и экономия — чтобы бот укладывался в бесплатные 5 ГБ Render в месяц."""

from __future__ import annotations

import asyncio
import calendar
import gzip
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import traffic as tm
from bot.cloud import BACKUP_FILENAME, TelegramBackup
from tests.test_cloud import FakeTelegram
from tests.test_telegram_flow import send

bot_env = __import__("tests.test_telegram_flow", fromlist=["bot_env"]).bot_env

GB = tm.GB
MID_NOV = calendar.timegm((2026, 11, 16, 0, 0, 0))  # ровно половина месяца прошла


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _restore_meter(monkeypatch):
    monkeypatch.setattr(tm, "_meter", tm._meter)  # общий счётчик процесса не утекает в другие тесты


def netdev(path, rx, tx, lo=10**9):
    path.write_text("Inter-|   Receive |  Transmit\n face |bytes packets|bytes packets\n"
                    f"    lo: {lo} 1 0 0 0 0 0 0 {lo} 1 0 0 0 0 0 0\n"
                    f"  eth0: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n")


def test_netdev_counts_both_directions_without_loopback(tmp_path):
    p = tmp_path / "dev"
    netdev(p, 1000, 500)
    assert tm.read_netdev(str(p)) == 1500
    assert tm.read_netdev(str(tmp_path / "нет")) is None


def test_month_accounting_rollover_and_restart(tmp_path):
    p = tmp_path / "dev"
    netdev(p, 1000, 0)
    m = tm.Traffic(netdev=str(p))
    netdev(p, 1000 + 3 * tm.MB, tm.MB)
    assert m.update(MID_NOV) == 4 * tm.MB and m.st["used"] == 4 * tm.MB and m.st["month"] == "2026-11"
    netdev(p, 500, 0)  # новый контейнер: счётчик с нуля
    m.update(MID_NOV + 60)
    assert m.st["used"] == 4 * tm.MB + 500
    m.update(calendar.timegm((2026, 12, 1, 0, 5, 0)))  # новый месяц
    assert m.st["used"] == 0 and m.st["prev"] == {"month": "2026-11", "used": 4 * tm.MB + 500}


def test_levels_by_pace_and_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAFFIC_BUDGET_GB", "4")
    m = tm.Traffic(netdev=str(tmp_path / "нет"))
    m.st["month"] = "2026-11"
    assert abs(tm.month_fraction(MID_NOV) - 0.5) < 0.01
    m.st["used"] = int(1.5 * GB)  # прогноз 3 ГБ из 4 — норма
    assert m.level(MID_NOV) == 0 and m.factor(MID_NOV) == 1 and m.allow("live_video", MID_NOV)
    m.st["used"] = int(2.1 * GB)  # прогноз 4.2 — эконом
    assert m.level(MID_NOV) == 1 and m.factor(MID_NOV) == 2
    assert not m.allow("live_video", MID_NOV) and m.allow("scout", MID_NOV)
    m.st["used"] = int(3.0 * GB)  # прогноз 6 — сильная экономия
    assert m.level(MID_NOV) == 2 and not m.allow("scout", MID_NOV) and not m.allow("news_low", MID_NOV)
    m.st["used"] = int(3.8 * GB)  # ≥ 92% бюджета
    assert m.level(MID_NOV) == 3 and not m.allow("mentors", MID_NOV)
    m.st["used"] = 4 * GB
    assert m.level(MID_NOV) == 4 and m.factor(MID_NOV) == 16
    m.st["budget_gb"] = 20  # бюджет поменяли командой
    assert m.level(MID_NOV) == 0
    # первый день месяца: доля не меньше суток — 100 МБ не пугают
    m.st["used"], m.st["budget_gb"] = 100 * tm.MB, None
    assert m.level(calendar.timegm((2026, 11, 1, 0, 10, 0))) == 0


def test_level_change_message(tmp_path):
    m = tm.Traffic(netdev=str(tmp_path / "нет"))
    m.st["month"] = "2026-11"
    assert m.level_change_text(MID_NOV) is None  # первый раз — только запомнить
    m.st["used"] = int(3.0 * GB)
    text = m.level_change_text(MID_NOV)
    assert "Экономлю трафик" in text and "открытые сделки веду как обычно" in text
    m.st["used"] = int(0.5 * GB)
    assert "в норме" in m.level_change_text(MID_NOV)


def test_trace_counts_bytes_per_host(tmp_path):
    m = tm.Traffic(netdev=str(tmp_path / "нет"))
    tm.install(m)

    async def flow():
        app = web.Application()
        async def big(request):
            return web.Response(body=b"x" * 50_000)

        app.router.add_get("/big", big)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(server.make_url("/big")) as r:
                    await r.read()
        finally:
            await server.close()

    run(flow())
    assert m._session_bytes >= 50_000
    m.update(MID_NOV)
    host = max(m.st["hosts"], key=m.st["hosts"].get)
    assert host in ("127.0.0.1", "localhost") and m.st["used"] >= 50_000  # без /proc — по трассировке
    assert m.status_dict(MID_NOV)["hosts"][0]["share"] > 0.9
    tm.note_ws("irc-ws.chat.twitch.tv", 1234)
    assert m._session_hosts["irc-ws.chat.twitch.tv"] == 1234


def test_backup_is_gzipped_and_old_json_still_restores():
    tg = FakeTelegram()

    async def flow():
        b1 = TelegramBackup(tg, chat_id=42)
        state = {"demo": {"balance": 5}, "pad": "x" * 20_000}
        await b1.save(state)
        raw = tg.files[tg.pinned]
        assert raw[:2] == b"\x1f\x8b" and len(raw) < 2_000  # сжато в десятки раз
        assert await TelegramBackup(tg, chat_id=42).restore() == state
        tg.files[tg.pinned] = json.dumps({"old": True}).encode()  # бэкап до v48.1 — без сжатия
        tg._doc = lambda mid: type("D", (), {"file_name": "bot_state.json", "file_id": str(mid)})()
        assert await TelegramBackup(tg, chat_id=42).restore() == {"old": True}

    run(flow())
    assert BACKUP_FILENAME.endswith(".gz")
    assert gzip.decompress(gzip.compress(b"{}")) == b"{}"


def test_background_backups_are_throttled_and_flushed(bot_env):
    b, _ = bot_env
    saved = []

    class Store:
        async def save(self, state):
            saved.append(1)

    b.backup = Store()

    async def flow():
        await b._backup_now()
        await b._backup_now(soon=True)  # сразу после — откладываем
        assert len(saved) == 1 and b._backup_pending
        await b._backup_now()  # по команде человека — сразу
        assert len(saved) == 2 and not b._backup_pending

    run(flow())


def test_traffic_command_and_doctor(bot_env, tmp_path):
    b, session = bot_env
    b.traffic.netdev = str(tmp_path / "нет")
    b.traffic._last_raw = None

    async def flow():
        await send(b, "/traffic")
        assert "Трафик за месяц" in session.sent[-1][1] and "обычный" in session.sent[-1][1]
        await send(b, "/traffic used 4.5")
        assert abs(b.traffic.st["used"] / GB - 4.5) < 0.01 and "стоп" in session.sent[-1][1]
        await send(b, "/traffic budget 50")
        assert b.traffic.st["budget_gb"] == 50 and "из бюджета 50.00 ГБ" in session.sent[-1][1]
        await send(b, "/traffic used абв")
        assert "Напиши число" in session.sent[-1][1]
        await send(b, "/traffic budget 5", user=222)  # не владелец
        assert b.traffic.st["budget_gb"] == 50

    run(flow())
    from bot.doctor import _automat_checks
    checks = [c for c in _automat_checks(b, time.time()) if "Трафик" in c.title]
    assert checks and "из бюджета" in checks[0].text


def test_economy_stretches_loops_and_stale_limits(bot_env, tmp_path):
    b, _ = bot_env
    from bot.autonomy import stale_limit
    b.traffic.netdev = str(tmp_path / "нет")
    normal = stale_limit(b, "news")
    b.traffic.st.update(month=time.strftime("%Y-%m", time.gmtime()), used=10 * GB)
    assert b.traffic.factor() == 16 and stale_limit(b, "news") == normal * 16
    assert stale_limit(b, "copy") is None
    assert not tm.allow("live_video") and tm.factor(cap=2) == 2


def test_live_watch_uses_snapshot_when_saving(bot_env, tmp_path):
    b, _ = bot_env
    calls = []

    class TV:
        async def clip(self, login, video=True):
            calls.append(video)
            return b"jpg", "image/jpeg", {"mode": "image"}

    b.traffic.netdev = str(tmp_path / "нет")
    b.traffic.st.update(month=time.strftime("%Y-%m", time.gmtime()), used=int(3.9 * GB))
    assert not tm.allow("live_video")
    from bot import livewatch
    assert livewatch.MAX_BW <= 1_000_000 and livewatch.CLIP_SEC <= 10
    L = b.mentors.live
    L.st["budget"] = {"day": livewatch._day(time.time()), "n": 0, "bytes": livewatch.LIVE_MB_PER_DAY * 1024 * 1024}
    assert L.bytes_left(time.time()) <= 0  # дневной лимит трафика эфиров исчерпан


def test_news_not_modified_costs_nothing(bot_env):
    b, _ = bot_env
    nw = b.news
    hits = []

    async def feed(request):
        hits.append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304)
        return web.Response(text="<rss><channel><item><title>Hello</title><link>https://a/1</link></item>"
                                 "</channel></rss>", headers={"ETag": '"v1"'})

    async def flow():
        app = web.Application()
        app.router.add_get("/rss", feed)
        server = TestServer(app)
        await server.start_server()
        try:
            url = str(server.make_url("/rss"))
            text = await nw._get(url)
            assert "Hello" in text
            from bot.news import NotModified
            with pytest.raises(NotModified):
                await nw._get(url)
        finally:
            await server.close()
            await nw.close()

    run(flow())
    assert hits == [None, '"v1"']
