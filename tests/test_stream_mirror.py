"""🎥 Мелкие детали на экране стрима (инсайд-чат, трекер, цифры токена) и 🪞 повтор действий стримера на демо."""

import asyncio
import time

from bot.dex import Pair
from bot.livewatch import LiveGemini, LiveWatch, clean_obs, token_flags
from bot.mirror import BOOKS, stop_fraction, take_fraction
from tests.test_livewatch import FakeLiveAI
from tests.test_mentors import Prices, make
from bot.pulse import Pulse
from tests.test_pulse import FakeCopy, FakeGmgn

NOW = time.time()
MINT = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"

INSIDER = {"type": "insider_chat", "text": "в Telegram-чате «alpha» кинули POPCAT и адрес", "who": "alpha calls",
           "asset": "$popcat", "contract": MINT}


def test_clean_details_token_and_flags():
    o = clean_obs({"trading": True, "action": "enter_long", "asset": "POPCAT", "confidence": "high",
                   "details": [INSIDER, {"type": "magic", "text": "x"}, dict(INSIDER), {"type": "wallet_tracker",
                               "text": "Cielo: кит купил", "contract": "мусор"}, "строка"],
                   "token": {"mcap": "1.2M", "top10_pct": 62, "dev_pct": "20%", "bundles_pct": 5, "holders": 900,
                             "snipers_pct": 500}})
    assert [d["type"] for d in o["details"]] == ["insider_chat", "wallet_tracker"]  # мусор и повтор отброшены
    assert o["details"][0]["asset"] == "POPCAT" and o["details"][1]["contract"] == ""
    assert o["token"]["top10"] == 0.62 and o["token"]["dev"] == 0.2 and "snipers" not in o["token"]
    # адрес мелькнул в чате, а торгуется тот же тикер → бот знает контракт
    assert o["contract"] == MINT
    # бот сам видит по цифрам с экрана: вошёл, хотя топ-10 держат 62% и у разработчика 20%
    assert any("топ-10 держат 62%" in f for f in o["red_flags"]) and any("разработчика 20%" in f for f in o["red_flags"])
    assert o["mistakes"][-1]["tag"] == "ignored_red_flags"
    assert token_flags({"rug": 0.5, "liq": 3000, "honeypot": True}) == ["риск rug 0.50", "продать нельзя (honeypot)",
                                                                       "ликвидность всего $3 000"]
    assert clean_obs({})["details"] == [] and clean_obs({})["token"] == {}


def test_stop_and_take_fractions():
    assert stop_fraction(100, 95, 1) == 0.05 and stop_fraction(100, 105, -1) == 0.05
    assert stop_fraction(100, 300000, 1) is None  # перепутал цену с капитализацией — не верим
    assert stop_fraction(100, None, 1) is None
    assert take_fraction(100, 120, 1) == 0.2 and take_fraction(100, 50, 1) is None


def test_gemini_body_high_resolution_and_fallback():
    g = LiveGemini("AQ.key", None)
    body = g._body(b"QUJD", "video/mp4", "смотри")
    assert b"MEDIA_RESOLUTION_HIGH" in body and b'"fps":2' in body
    import json
    plain = g._strip(body)
    assert b"MEDIA_RESOLUTION" not in plain and b"fps" not in plain and json.loads(plain)
    assert g.res is None and b"videoMetadata" not in g._body(b"QUJD", "video/mp4", "x")


def _session(L, src, login="decu"):
    L._open(login, src, {"id": "s1", "title": "SOL trenches", "started": NOW - 600}, NOW)
    return L.st["sessions"][login]


def env(prices=None):
    mt, ct, yt, dex = make(NOW, prices=prices or Prices(SOL=100.0))
    src = mt._add("twitch", "decu", "decu")
    L = LiveWatch(mt, ai=FakeLiveAI([]))
    mt.live = L
    return mt, src, L


def obs(**kw):
    raw = {"trading": True, "asset": "SOL", "market": "perp", "action": "watching", "confidence": "high",
           "position": {"side": "none"}, "discipline": 6}
    raw.update(kw)
    return clean_obs(raw)


def test_mirror_exact_and_smart_copy_cex():
    prices = Prices(SOL=100.4)
    mt, src, L = env(prices)
    sess = _session(L, src)

    async def run():
        # вошёл в лонг по 100 без стопа с FOMO: точная копия повторяет, умная — пропускает
        ev = await L.apply(sess, src, obs(action="enter_long", position={"side": "long", "entry": 100},
                                          mistakes=[{"tag": "fomo_chase"}]), NOW)
        text = ev[0]["text"]
        assert "Повторил на демо (🪞 Точная копия)" in text and "опоздание стоило +0.40%" in text
        assert "Умная копия пропустила вход" in text
        books = L.mirror.st["books"]
        assert len(books["exact"]["positions"]) == 1 and not books["smart"]["positions"]
        pos = next(iter(books["exact"]["positions"].values()))
        assert pos["stop"] is None and pos["usd"] == 2.0 and pos["late"] == 0.4  # 20% от $10, без стопа — как стример
        # второй вход — чистый, со стопом 95: обе копии входят, стоп переводится в долю
        prices.p["ETH"] = 3000.0
        ev = await L.apply(sess, src, obs(asset="ETH", action="enter_long",
                                          position={"side": "long", "entry": 3000, "stop": 2850}), NOW + 60)
        assert "Точная копия, 🧠 Умная копия" in ev[0]["text"]
        eth = [p for p in books["smart"]["positions"].values() if p["asset"] == "ETH"][0]
        assert eth["stop"] == 0.05
        # стример закрыл SOL в плюс — точная копия закрывает вслед
        prices.p["SOL"] = 110.0
        ev = await L.apply(sess, src, obs(action="take_profit", position={"side": "none"}), NOW + 400)
        assert "закрыл SOL вслед за стримером" in ev[0]["text"]
        assert books["exact"]["n"] == 1 and books["exact"]["rets"][0] > 9  # +9,6% цены минус комиссии спота
        # ETH упал ниже стопа: обе копии закрываются сами
        prices.p["ETH"] = 2800.0
        ev = await L.mirror.update(NOW + 900)
        assert len(ev) == 2 and all("стоп −5.0%" in e["text"] for e in ev)
        d = L.mirror.status_dict()
        s = d["streamers"][0]
        assert s["name"] == "decu" and s["n"] == 2 and s["smart_n"] == 1 and s["verdict"] == "data"
        assert d["late_avg"] is not None and [b["id"] for b in d["books"]] == list(BOOKS)
        text = L.mirror.text()
        assert "Копировать ли стримера" in text and "decu" in text
        # уходит в Профит-центр как отдельный способ
        from bot.profit import collect
        engines = [e for e in collect(_bot_like(mt)) if e["id"] == "mirror"]
        assert engines and engines[0]["n"] == 1

    asyncio.run(run())


def _bot_like(mt):
    """Минимальный «бот» для Профит-центра: только то, что нужно для повтора стримеров."""
    class Nothing:
        def __getattr__(self, name):
            raise AttributeError(name)

    class B(Nothing):
        pass

    b = B()
    b.__dict__["mentors"] = mt
    return b


def test_insider_chat_detail_goes_to_pulse_and_meme_mirror():
    mt, src, L = env()
    c = FakeCopy()
    g = FakeGmgn()
    g.sec[MINT] = {"top10": 0.7, "dev": 0.02, "insider": 0.4, "rug": 0.1}
    L.pulse = Pulse(c, g)
    mt.copy._pairs = c._pairs  # цены мемкоинов для повтора берём из того же поддельного DexScreener
    c.dex.pairs[MINT] = Pair(chain="solana", dex="raydium", url="", pair_address="P1", symbol="POPCAT", name="p",
                             address=MINT, quote="SOL", price_usd=0.01, liquidity_usd=50_000)
    sess = _session(L, src)

    async def run():
        # в углу экрана мелькнул инсайд-чат с адресом — стример пока только смотрит
        ev = await L.apply(sess, src, obs(asset="POPCAT", market="meme", action="watching", details=[INSIDER]), NOW)
        assert ev and "🕵️ закрытый/инсайд-чат (alpha calls)" in ev[0]["text"] and MINT in ev[0]["text"]
        assert "🔬 Проверил по GMGN" in ev[0]["text"] and "топ-10 70%" in ev[0]["text"]
        assert any("gmgn.ai/sol/token" in u for _, u in ev[0]["buttons"])
        ext = L.pulse.st["external"]
        assert ext and ext[0]["src"] == "insider" and ext[0]["who"] == "decu"
        assert src["live"]["seen"]["insider_chat"] == 1
        # та же деталь через минуту — не новость
        assert await L.apply(sess, src, obs(asset="POPCAT", market="meme", action="watching", details=[INSIDER]),
                             NOW + 60) == []
        # стример купил этот токен: красные флаги из GMGN → ошибка «несмотря на флаги», точная копия покупает
        ev = await L.apply(sess, src, obs(asset="POPCAT", market="meme", action="enter_long", contract=MINT,
                                          position={"side": "long", "entry": 0.0099}), NOW + 120)
        text = ev[0]["text"]
        assert "🚩 Красные флаги" in text and "входит, несмотря на красные флаги" in text
        assert "Повторил на демо (🪞 Точная копия)" in text and "Умная копия пропустила вход" in text
        pos = next(iter(L.mirror.st["books"]["exact"]["positions"].values()))
        assert pos["kind"] == "sol" and pos["key"] == MINT and pos["stop"] is None
        assert src["live"]["miss"]["ignored_red_flags"] == 1
        # срок мемкоина — сутки: бот закрывает сам, даже если стример ушёл
        c.dex.pairs[MINT].price_usd = 0.005
        ev = await L.mirror.update(NOW + 25 * 3600)
        assert ev and "срок 24 ч" in ev[0]["text"] and L.mirror.st["books"]["exact"]["rets"][0] < -50
        d = L.status_dict()
        assert d["details"][0]["type"] == "insider_chat" and d["mirror"]["books"][0]["n"] == 1
        assert "Мелкие детали на экране" in L.text()
        ctx = mt.ai_context()["live_streams"]
        assert ctx["small_details_seen"] and ctx["mirror_demo"]

    asyncio.run(run())


def test_mirror_off_and_low_confidence():
    mt, src, L = env(Prices(SOL=100.0))
    sess = _session(L, src)

    async def run():
        await L.apply(sess, src, obs(action="enter_long", confidence="low", position={"side": "long"}), NOW)
        assert not L.mirror.st["books"]["exact"]["positions"]  # неуверенный взгляд — не повторяем
        L.mirror.st["on"] = False
        await L.apply(sess, src, obs(action="enter_long", position={"side": "long"}), NOW + 60)
        assert not L.mirror.st["books"]["exact"]["positions"]

    asyncio.run(run())
