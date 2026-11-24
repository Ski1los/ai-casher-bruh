"""v41: 📡 лента событий, 🌙 тихие часы, 🩺 здоровье, 🧠 штурман-ИИ, ⚡ ETag, больше монет у автопилота."""

from __future__ import annotations

import asyncio
import time

from bot import brief, feed, notify, users_ui
from bot.scanner import Quote, liquid_symbols
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import OWNER, send
from tests.test_webapp import auth, call, get, post

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

FRIEND = 777


def _night(b) -> float:
    """Момент «02:00 по времени отчётов» (сегодня)."""
    off = b.settings.report_utc_offset * 3600
    day = (time.time() + off) // 86400 * 86400
    return day + 2 * 3600 - off


def test_feed_records_every_notification_and_kinds(bot_env):
    b, _session = bot_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def run():
        await b.notify("⚙️ <b>Треугольник</b> USDT → ETH → BTC: +0.03 USDT")
        await b.notify("🐋 Кит купил BONK")
        await b.notify("⚠️ Ключ Bybit истекает")
        notify.set_muted(b, OWNER, True)
        notify.set_muted(b, FRIEND, True)
        await b.notify("🧪 Демо: продал PNUT +18%")  # все выключили уведомления — в ленту всё равно пишется

    asyncio.run(run())
    rows = feed.items(b)
    assert [r["kind"] for r in rows] == ["trade", "warn", "meme", "trade"]  # новые сверху
    assert rows[-1]["text"] == "⚙️ Треугольник USDT → ETH → BTC: +0.03 USDT"  # без HTML
    assert feed.counts(b, 0)["trade"] == 2
    # повтор того же текста подряд не дублируется
    feed.add(b, "🐋 Кит купил BONK")
    feed.add(b, "🐋 Кит купил BONK")
    assert len(feed.items(b)) == 5
    # лента сохраняется вместе с автоматом (copytrade.json)
    assert b.copy.export()["autonomy"]["feed"][-1]["text"] == "🐋 Кит купил BONK"
    # починки сторожа — тоже в ленте
    b.autonomy._fix("🔁 перезапустил «радар»", time.time())
    assert feed.items(b, 1)[0]["kind"] == "auto"


def test_feed_command(bot_env):
    b, session = bot_env

    async def run():
        await send(b, "/feed")
        assert "пусто" in session.sent[-1][1].lower()
        await b.notify("🤖 Автопилот купил SOL")
        await send(b, "/feed")
        assert "Лента событий" in session.sent[-1][1] and "Автопилот купил SOL" in session.sent[-1][1]

    asyncio.run(run())


def test_quiet_hours_hold_until_morning_but_money_comes_at_once(bot_env, monkeypatch):
    b, session = bot_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")
    night = _night(b)
    monkeypatch.setattr(notify.time, "time", lambda: night)

    async def run():
        await send(b, "/notify", user=FRIEND)
        assert "Тихие часы" in session.sent[-1][1] and "nt:q1" in buttons(session.markups[-1])
        await press(b, "nt:q1", user=FRIEND)
        assert FRIEND in b.quiet
        n0 = len(session.sent)
        await b.notify("🐋 Кит купил BONK")                       # обычное — Васе утром
        await b.notify("🔴 Реальный режим: купил SOL на $5")       # деньги — сразу всем
        await b.notify("⚠️ Ключ BingX не работает")                # проблема — сразу всем
        got = session.sent[n0:]
        assert [c for c, t in got if "BONK" in t] == [OWNER]
        assert {c for c, t in got if "SOL" in t} == {OWNER, FRIEND}
        assert {c for c, t in got if "BingX" in t} == {OWNER, FRIEND}
        assert len(b._held[FRIEND]) == 1
        # ночью не отправляем, утром — одним сообщением
        assert await notify.flush_quiet(b, night) == 0
        assert await notify.flush_quiet(b, night + 7 * 3600) == 1
        assert session.sent[-1][0] == FRIEND and "Пока ты спал" in session.sent[-1][1] \
            and "BONK" in session.sent[-1][1]
        assert not b._held

    asyncio.run(run())
    assert b._settings_state()["people"]["quiet"] == [FRIEND]


def test_health_score_and_issues(bot_env):
    b, _ = bot_env
    h = b.autonomy.health()
    assert 0 <= h["score"] <= 100 and h["level"] in ("ok", "warn", "bad")
    keys = {i["key"] for i in h["items"]}
    assert {"auto", "loops", "keys", "ai", "dup", "mem", "update"} <= keys
    # без ключей бирж и без ИИ — подсказки, как починить
    hints = " ".join(i["hint"] for i in h["issues"])
    assert "/setup" in hints and "/keys" in hints
    b.autonomy.set_on(False)
    assert b.autonomy.health()["score"] < h["score"]
    assert "Здоровье" in b.autonomy.text()


class FakeChat:
    def __init__(self, answer="Сейчас: учусь на демо.\nРиск: нет ключа.\nШаг: /setup"):
        self.endpoints = [object()]
        self.answer = answer
        self.calls = 0
        self.last_model = "fake-model"
        self.configured = True

    async def chat(self, messages, max_tokens=1200, background=False):
        self.calls += 1
        assert "штурман" in messages[0]["content"] and background
        return "**" + self.answer + "**"


def test_brief_is_cached_and_used_in_digest(bot_env):
    b, _ = bot_env
    fake = FakeChat()
    b.ai.client = fake

    async def run():
        r = await brief.make(b)
        assert r["text"].startswith("Сейчас:") and "**" not in r["text"] and r["model"] == "fake-model"
        await brief.make(b)
        assert fake.calls == 1  # кэш 20 минут — бесплатные лимиты целы
        await brief.make(b, force=True)
        assert fake.calls == 2

    asyncio.run(run())
    ctx = brief.context(b)
    assert "здоровье" in ctx and "лента_за_сутки" in ctx


def test_brief_without_ai(bot_env):
    b, _ = bot_env
    r = asyncio.run(brief.make(b))
    assert not r["text"] and "/keys" in r["error"]


def test_webapp_overview_feed_notify_and_etag(app_env):
    b, _ = app_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")
    feed.add(b, "🐋 Кит купил BONK")

    async def flow(c):
        status, ov = await get(c, "/api/overview")
        assert status == 200 and ov["auto"]["health"]["score"] >= 0 and ov["auto"]["engines"]
        assert ov["feed"][0]["text"] == "🐋 Кит купил BONK" and ov["me"]["member"] and not ov["me"]["quiet"]
        # ⚡ ничего не изменилось → 304 без тела
        r = await c.get("/api/overview", headers=auth(OWNER))
        tag = r.headers["ETag"]
        r2 = await c.get("/api/overview", headers={**auth(OWNER), "If-None-Match": tag})
        assert r2.status == 304 and not await r2.read()
        feed.add(b, "🤖 Автопилот купил SOL")
        r3 = await c.get("/api/overview", headers={**auth(OWNER), "If-None-Match": tag})
        assert r3.status == 200
        # лента с фильтром
        status, f = await get(c, "/api/feed?kind=meme")
        assert status == 200 and [x["text"] for x in f["items"]] == ["🐋 Кит купил BONK"] and f["counts"]["trade"] == 1
        status, _ = await get(c, "/api/feed?kind=xxx")
        assert status == 400
        # свои уведомления — каждый себе
        status, d = await post(c, "/api/notify", {"quiet": True, "muted": True}, uid=FRIEND)
        assert status == 200 and d["me"]["quiet"] and d["me"]["muted"] and FRIEND in b.quiet and FRIEND in b.muted
        status, _ = await post(c, "/api/notify", {"muted": True}, uid=222)  # 222 — не пользователь бота
        assert status == 403 or 222 in notify.everyone(b)
        status, d = await get(c, "/api/brief")
        assert status == 200 and "error" in d
        status, d = await post(c, "/api/clientlog", {"msg": "TypeError: x is undefined", "src": "app.js", "line": 1})
        assert status == 200 and d["ok"]

    call(b, flow)


def _q(sym, vol, spread=0.0005):
    return Quote("a", sym, 100.0, 100.0 * (1 + spread), vol, 0.001)


def test_liquid_symbols_filter():
    qs = {s: _q(s, v) for s, v in [("BTC/USDT", 9e9), ("PEPE/USDT", 4e8), ("USDC/USDT", 9e9), ("BTC3L/USDT", 5e8),
                                   ("ETHUP/USDT", 5e8), ("TINY/USDT", 1e6)]}
    qs["WIDE/USDT"] = _q("WIDE/USDT", 5e8, spread=0.01)
    assert liquid_symbols(qs) == ["BTC/USDT", "PEPE/USDT"]


def test_autopilot_checks_liquid_coins_too(bot_env):
    b, _ = bot_env
    name = next(iter(b.scanner.exchanges))
    base = b.scalper.symbols(name)
    extra = [s for s in b.scanner.exchanges[name].markets if s.endswith("/USDT") and s not in base][:2]
    b.scanner.liquid[name] = extra + base[:1]
    out = b.scalper.symbols(name)
    assert out[:len(base)] == base and all(s in out for s in extra) and len(out) == len(set(out)) <= 20
