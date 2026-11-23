"""🎓 Учёба у трейдеров: разбор ссылок, RSS, Twitch-чат, проверка прогнозов ценой, рейтинг, проверка советов,
сохранение состояния, Gemini (поддельный HTTP) и команды Telegram."""

import asyncio
import json
import time

from bot import mentors as mn
from bot.copytrade import EXIT_MIN, FILTER_DEFAULT, exit_net
from bot.mentors import (MIN_CHECKED, GeminiVideo, Mentors, TwitchChat, addresses_in, clean_analysis, parse_feed,
                         parse_irc, parse_link, pick_video_model, shrunk_avg, trusted_sender)
from tests.test_copytrade import MINT, run, setup
from tests.test_meme_demo import demo_trader

UC = "UCRvqjQPSeaWn-uEx-w0XOIg"
HOUR = 3600


# --- поддельные источники ---------------------------------------------------------------------

class FakeYouTube:
    def __init__(self):
        self.videos: dict[str, list[dict]] = {}

    def add(self, cid, vid, age_h=1.0, title="Bitcoin next move", short=False, now=None):
        now = time.time() if now is None else now
        self.videos.setdefault(cid, []).append({
            "id": vid, "title": title, "published": now - age_h * HOUR, "description": "desc",
            "url": f"https://www.youtube.com/{'shorts/' if short else 'watch?v='}{vid}", "short": short})

    async def feed(self, cid):
        return "Канал " + cid[:4], list(self.videos.get(cid, []))

    async def resolve(self, kind, value):
        return UC, "Benjamin Cowen"


class FakeGemini:
    configured = True
    model = "gemini-test-flash"
    last_error = None

    def __init__(self, result=None):
        self.result = result
        self.seen: list[str] = []
        self.fail: str | None = None

    async def analyze(self, url, title=""):
        if self.fail:
            raise mn.MentorError(self.fail)
        self.seen.append(url)
        return clean_analysis(self.result or {
            "summary": "Ждёт рост SOL", "market_view": "bullish",
            "calls": [{"asset": "SOL", "direction": "long", "horizon_days": 2, "quote": "SOL вырастет"},
                      {"asset": "USDT", "direction": "long"}, {"asset": "NOPECOIN", "direction": "long"}],
            "tips": [{"text": "Ставь стоп −20%", "rule": {"param": "stop_pct", "value": 20}}],
            "sponsored": True, "sponsor_note": "биржа X", "hype": 8})


class Prices:
    def __init__(self, **prices):
        self.p = dict(prices)

    async def __call__(self, coins):
        return {c: self.p[c] for c in coins if c in self.p}


def make(now=None, gemini=None, prices=None, meme=None):
    now = time.time() if now is None else now
    ct, rpc, dex = setup(now)
    yt = FakeYouTube()
    mt = Mentors(ct, meme, None, price_of=prices or Prices(SOL=100.0), env={}, youtube=yt,
                 gemini=gemini or FakeGemini(), twitch=TwitchChat(None))
    mt.scout.st["on"] = False  # разведчик ходит в сеть — в этих тестах он не нужен (свои тесты — test_scout.py)
    return mt, ct, yt, dex


# --- разбор текста -------------------------------------------------------------------------------

def test_parse_links():
    assert parse_link("https://www.youtube.com/channel/" + UC) == ("yt_channel", UC)
    assert parse_link(UC) == ("yt_channel", UC)
    assert parse_link("https://youtu.be/dQw4w9WgXcQ?si=1") == ("yt_video", "dQw4w9WgXcQ")
    assert parse_link("https://www.youtube.com/watch?app=desktop&v=dQw4w9WgXcQ") == ("yt_video", "dQw4w9WgXcQ")
    assert parse_link("https://youtube.com/shorts/dQw4w9WgXcQ") == ("yt_video", "dQw4w9WgXcQ")
    assert parse_link("https://www.youtube.com/@CoinBureau/videos") == ("yt_handle", "CoinBureau")
    assert parse_link("https://www.twitch.tv/Decu") == ("twitch", "decu")
    assert parse_link("twitch:fashr") == ("twitch", "fashr")
    assert parse_link("просто текст") is None


def test_parse_feed():
    xml = """<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
      <title>Benjamin Cowen</title>
      <entry><yt:videoId>abcdefghijk</yt:videoId><title>BTC: the cycle</title>
        <link rel="alternate" href="https://www.youtube.com/watch?v=abcdefghijk"/>
        <published>2026-11-18T10:00:00+00:00</published>
        <media:group><media:description>Sponsored by X</media:description></media:group></entry>
      <entry><yt:videoId>shortshort1</yt:videoId><title>#shorts</title>
        <link rel="alternate" href="https://www.youtube.com/shorts/shortshort1"/>
        <published>2026-11-18T11:00:00+00:00</published></entry>
    </feed>"""
    name, videos = parse_feed(xml)
    assert name == "Benjamin Cowen" and [v["id"] for v in videos] == ["abcdefghijk", "shortshort1"]
    assert videos[0]["description"] == "Sponsored by X" and not videos[0]["short"] and videos[1]["short"]
    assert abs(videos[0]["published"] - 1794996000) < 1  # 2026-11-18 10:00 UTC



def test_irc_and_trusted_senders():
    line = ("@badge-info=;badges=moderator/1;display-name=ModGuy;mod=1 :modguy!modguy@modguy.tmi.twitch.tv "
            f"PRIVMSG #decu :CA {MINT} lfg")
    msg = parse_irc(line)
    assert msg["channel"] == "decu" and msg["user"] == "modguy" and "moderator" in msg["badges"]
    assert trusted_sender(msg) and addresses_in(msg["text"]) == [MINT]
    viewer = parse_irc(f"@badges=;display-name=Rnd :rnd!rnd@rnd.tmi.twitch.tv PRIVMSG #decu :{MINT}")
    assert not trusted_sender(viewer)
    assert trusted_sender(parse_irc(f":decu!decu@decu.tmi.twitch.tv PRIVMSG #decu :{MINT}"))  # сам стример
    assert parse_irc("PING :tmi.twitch.tv") is None
    assert addresses_in("0x123 и просто слова") == []


def test_clean_analysis_drops_garbage():
    raw = {"summary": "x" * 900, "market_view": "moon", "hype": "15",
           "calls": [{"asset": "$eth", "direction": "LONG", "horizon_days": 90}, {"asset": "BTC", "direction": "up"},
                     "мусор", {"asset": "", "contract": "not-an-address", "direction": "long"}],
           "tips": [{"text": "Стоп 20%", "rule": {"param": "stop_pct", "value": "20"}},
                    {"text": "Не жадничай", "rule": {"param": "none", "value": None}}, {"text": ""}]}
    a = clean_analysis(raw)
    assert len(a["summary"]) == 600 and a["market_view"] == "neutral" and a["hype"] == 10
    assert a["calls"] == [{"asset": "ETH", "contract": "", "direction": "long", "horizon_days": 30,
                           "confidence": "medium", "quote": ""}]
    assert a["tips"][0]["rule"] == {"param": "stop_pct", "value": 20.0} and a["tips"][1]["rule"] is None
    assert len(a["tips"]) == 2 and a["sponsored"] is False


def test_pick_video_model_prefers_stable_newest_flash():
    names = ["gemini-2.5-flash", "gemini-3-flash-preview", "gemini-2.5-flash-lite", "gemini-2.5-pro",
             "gemini-2.5-flash-image", "gemini-3.5-flash", "gemini-2.0-flash-live"]
    assert pick_video_model(names, set()) == "gemini-3.5-flash"
    assert pick_video_model(names, {"gemini-3.5-flash"}) == "gemini-2.5-flash"
    assert pick_video_model(["gemini-2.5-pro"], set()) is None
    assert shrunk_avg([10.0] * 5) == 5.0 and shrunk_avg([]) == 0.0


# --- YouTube: новые ролики, прогнозы, советы ---------------------------------------------------------

def test_first_sight_analyzes_only_newest_then_new_videos():
    now = time.time()
    mt, ct, yt, _ = make(now)
    assert mt.seed() and not mt.seed()
    assert len(mt.active("youtube")) == 4 and len(mt.active("twitch")) == 5
    yt.add(UC, "oldvideo001", age_h=100, now=now)      # старый — не разбираем
    yt.add(UC, "newvideo001", age_h=3, now=now)
    yt.add(UC, "newvideo002", age_h=1, now=now)
    yt.add(UC, "shortvid001", age_h=0.5, short=True, now=now)
    events = run(mt.check_youtube(now))
    assert mt.gemini.seen == ["https://www.youtube.com/watch?v=newvideo002"]  # только самый свежий
    src = mt.st["sources"]["yt:" + UC]
    assert src["videos"] == 1 and src["sponsored"] == 1 and len(events) == 1
    assert "реклама: биржа X" in events[0]["text"] and "хайп 8/10" in events[0]["text"]
    # прогноз SOL записан с ценой; USDT (стейбл) и монета, которой нет на биржах, — пропущены
    calls = mt.st["calls"]
    assert [(c["asset"], c["price0"], c["horizon_h"]) for c in calls] == [("SOL", 100.0, 48)]
    # совет «стоп −20%» записан, но копий с путём цены мало — ждём
    assert mt.st["tips"][-1]["verdict"]["state"] == "wait"
    # через 45 минут: новый ролик разбираем, уже виденные — нет
    yt.add(UC, "newvideo003", age_h=0, now=now + 3000)
    run(mt.check_youtube(now + 3000))
    assert mt.gemini.seen[-1].endswith("newvideo003") and len(mt.gemini.seen) == 2
    assert len(mt.st["calls"]) == 1  # тот же прогноз SOL от того же автора за 6 ч не дублируем
    ctx = mt.ai_context()
    assert ctx["latest_videos"][0]["ad"] is True and ctx["traders"]
    assert ct.changes > 0


def test_gemini_limit_retries_video_later_without_marking_seen():
    now = time.time()
    mt, _, yt, _ = make(now)
    mt.seed()
    yt.add(UC, "newvideo001", age_h=1, now=now)
    mt.gemini.fail = "Gemini: лимит бесплатного тарифа, продолжу через 15 минут"
    assert run(mt.check_youtube(now)) == []
    src = mt.st["sources"]["yt:" + UC]
    assert "newvideo001" not in src["seen"]
    mt.gemini.fail = None
    run(mt.check_youtube(now + 3000))
    assert mt.gemini.seen == ["https://www.youtube.com/watch?v=newvideo001"]


def test_calls_are_scored_and_trader_rated():
    now = time.time()
    prices = Prices(SOL=100.0)
    mt, _, _, _ = make(now, prices=prices)
    src = mt._add("youtube", UC, "Cowen")
    for i in range(MIN_CHECKED + 2):  # прогнозы «SOL вырастет за сутки» — каждый сбывается на +10%
        t = now + i * 7 * HOUR
        prices.p["SOL"] = 100.0
        call = run(mt._open_call(src, {"asset": "SOL", "direction": "long", "horizon_days": 1}, t))
        assert call is not None
        prices.p["SOL"] = 110.0
        run(mt.update_calls(t + 25 * HOUR))
    assert len(src["rets"]) == MIN_CHECKED + 2
    assert abs(src["rets"][0] - (10.0 - 0.4)) < 1e-9  # минус две комиссии по 0,2%
    assert src["status"] == "star"
    assert any("заслужил доверие" in x["text"] for x in mt.st["log"])
    # «шорт» и «не покупай» правы, когда цена падает
    prices.p["SOL"] = 100.0
    short = run(mt._open_call(src, {"asset": "SOL", "direction": "short", "horizon_days": 1}, now + 1000 * HOUR))
    prices.p["SOL"] = 90.0
    run(mt.update_calls(now + 1030 * HOUR))
    assert short["done"] and abs(short["ret"] - 9.6) < 1e-9
    assert "Cowen" in mt.text() and "⭐" in mt.short_line()


def test_bad_or_sponsored_trader_is_muted_or_not_starred():
    now = time.time()
    prices = Prices(SOL=100.0)
    mt, _, _, _ = make(now, prices=prices)
    src = mt._add("youtube", UC, "Shill")
    for i in range(MIN_CHECKED + 4):
        t = now + i * 7 * HOUR
        prices.p["SOL"] = 100.0
        run(mt._open_call(src, {"asset": "SOL", "direction": "long", "horizon_days": 1}, t))
        prices.p["SOL"] = 80.0
        run(mt.update_calls(t + 25 * HOUR))
    assert src["status"] == "muted"
    # хорошие прогнозы, но половина роликов — реклама: ⭐ не даём
    src2 = mt._add("youtube", "UCqK_GSMbpiV8spgD3ZGloSw", "Ads")
    src2.update(videos=6, sponsored=4, rets=[10.0] * (MIN_CHECKED + 4))
    assert mt._rate(src2, now) is None and src2["status"] == "watch"


# --- Twitch -----------------------------------------------------------------------------------------

def test_twitch_call_from_streamer_and_demo_buy_for_star():
    now = time.time()
    ct, rpc, dex = setup(now)
    meme, _ = demo_trader(ct)
    mt = Mentors(ct, meme, None, price_of=Prices(), env={}, youtube=FakeYouTube(), gemini=FakeGemini(),
                 twitch=TwitchChat(None))
    mt.seed()
    src = mt.st["sources"]["tw:decu"]
    dex.set_price(MINT, 0.02)
    mt.twitch.handle(f"@badges=;display-name=Viewer :viewer!v@v.tmi.twitch.tv PRIVMSG #decu :{MINT}")
    mt.twitch.handle(f"@badges=broadcaster/1;display-name=Decu :decu!d@d.tmi.twitch.tv PRIVMSG #decu :ape {MINT}")
    assert mt.twitch.queue.qsize() == 1  # зритель не считается
    assert run(mt.drain_twitch(now)) == []  # 👀 — молча записываем
    call = mt.st["calls"][-1]
    assert call["kind"] == "sol" and call["price0"] == 0.02 and call["horizon_h"] == 24 and src["calls"] == 1
    # стример стал ⭐: следующий его токен — сигнал + сделка на демо
    src["status"] = "star"
    mint2 = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    mt.twitch.handle(f":decu!d@d.tmi.twitch.tv PRIVMSG #decu :{mint2}")
    events = run(mt.drain_twitch(now + 60))
    assert len(events) == 1 and "показал токен" in events[0]["text"]
    assert mint2 in meme.demo["positions"] and meme.demo["positions"][mint2]["category"] == "mentor"
    assert "🎓 decu" in events[0]["text"]
    # итог мемкоин-колла — как копия: цена +35%, потом +60%; при тейке +30% продали бы на +35% (минус комиссии)
    meme.st["tp_pct"] = 0.3
    dex.set_price(MINT, 0.027)
    run(mt.update_calls(now + 2 * HOUR))
    assert not call["done"]
    dex.set_price(MINT, 0.032)
    run(mt.update_calls(now + 25 * HOUR))
    assert call["done"] and call["path"] == [60.0]  # итог посчитан по пути 0 → +35% → +60%, путь сжат
    assert abs(call["ret"] - round(exit_net(35.0, ct.cost_side()), 2)) < 1e-9 and call["ret"] < 35


def test_twitch_set_channels_without_connection():
    tw = TwitchChat(None)
    run(tw.set_channels({"Decu", "fashr"}))
    assert tw._want == {"decu", "fashr"} and tw.channels == set()


# --- советы ------------------------------------------------------------------------------------

def test_stop_tip_accepted_only_if_history_confirms():
    now = time.time()
    ct, _, _ = setup(now)
    meme, _ = demo_trader(ct)
    mt = Mentors(ct, meme, None, env={}, youtube=FakeYouTube(), gemini=FakeGemini(), twitch=TwitchChat(None))
    # копии падали до −25%, потом до −45%: стоп −20% сэкономил бы деньги
    ct.st["closed"] = [{"passed": True, "ret": -47.0, "path": [0.0, -25.0, -45.0], "closed": now}
                       for _ in range(EXIT_MIN)]
    tip = mt._add_tip(None, {"text": "стоп 20%", "rule": {"param": "stop_pct", "value": 20}}, now)
    assert tip["verdict"]["state"] == "accepted" and meme.st["stop_pct"] == 0.2
    # копии отскакивали после −25% и закрывались в плюсе: стоп −20% был бы хуже
    ct.st["closed"] = [{"passed": True, "ret": 15.0, "path": [0.0, -25.0, 20.0], "closed": now}
                       for _ in range(EXIT_MIN)]
    meme.st["stop_pct"] = 0.4
    tip = mt._add_tip(None, {"text": "стоп 30%", "rule": {"param": "stop_pct", "value": 30}}, now)
    assert tip["verdict"]["state"] == "rejected" and meme.st["stop_pct"] == 0.4
    tip = mt._add_tip(None, {"text": "стоп 40%", "rule": {"param": "stop_pct", "value": 40}}, now)
    assert tip["verdict"]["state"] == "same"


def test_filter_tip_checked_on_copy_history():
    now = time.time()
    ct, _, _ = setup(now)
    mt = Mentors(ct, None, None, env={}, youtube=FakeYouTube(), gemini=FakeGemini(), twitch=TwitchChat(None))

    def t(liq, ret):
        return {"feats": {"liq": liq, "rug": 10, "age_h": 5, "chase": 0.0, "h1": 10}, "ret": ret, "closed": now}

    ct.st["closed"] = [t(25_000, -30.0) for _ in range(12)] + [t(80_000, 8.0) for _ in range(12)]
    tip = mt._add_tip(None, {"text": "ликвидность от $50K", "rule": {"param": "min_liq_usd", "value": 50000}}, now)
    assert tip["verdict"]["state"] == "accepted" and ct.st["filter"]["min_liq"] == 50_000
    ct.st["closed"] = ct.st["closed"][:5]
    tip = mt._add_tip(None, {"text": "возраст от часа", "rule": {"param": "min_age_hours", "value": 1}}, now)
    assert tip["verdict"]["state"] == "wait"
    assert FILTER_DEFAULT["min_liq"] == 20_000


# --- состояние -------------------------------------------------------------------------------------

def test_state_survives_restore():
    now = time.time()
    mt, ct, _, _ = make(now)
    mt.seed()
    mt._add("twitch", "megga", "megga")
    saved = json.loads(json.dumps(ct.export()))
    ct.restore(saved)
    assert ct.st["mentors"]["seeded"] and "tw:megga" in mt.st["sources"]
    ct.restore({})
    assert mt.st["sources"] == {} and mt.seed()


# --- Gemini: запрос и ошибки (поддельный HTTP) ---------------------------------------------------------

class FakeResp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def text(self):
        return self.body if isinstance(self.body, str) else json.dumps(self.body)

    async def json(self, content_type=None):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeHttp:
    def __init__(self, replies):
        self.replies = list(replies)
        self.posts: list[tuple[str, dict]] = []

    def get(self, url, **kw):
        return FakeResp(200, {"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]})

    def post(self, url, json=None, **kw):
        self.posts.append((url, json))
        return FakeResp(*self.replies.pop(0))


def test_gemini_request_shape_and_fallbacks():
    answer = {"summary": "ok", "market_view": "bearish", "calls": [], "tips": []}
    ok = {"candidates": [{"content": {"parts": [{"text": "думаю…", "thought": True},
                                                  {"text": "```json\n" + json.dumps(answer) + "\n```"}]}}]}
    http = FakeHttp([(404, "models/gemini-3.5-flash is not found for API version v1beta"), (200, ok)])

    async def session():
        return http

    g = GeminiVideo("AIzaTEST", session)
    res = run(g.analyze("https://www.youtube.com/watch?v=abcdefghijk", "BTC"))
    assert res["market_view"] == "bearish" and res["summary"] == "ok"
    assert g.dead == {"gemini-3.5-flash"} and g.model == "gemini-2.5-flash"
    url, body = http.posts[-1]
    assert url.endswith("/models/gemini-2.5-flash:generateContent")
    part = body["contents"][0]["parts"][0]
    assert part["file_data"]["file_uri"].endswith("abcdefghijk") and part["video_metadata"]["end_offset"] == "1200s"
    assert body["contents"][0]["parts"][1]["text"].endswith("Название ролика: BTC")
    # 429 → пауза 15 минут, без лишних запросов
    http.replies = [(429, "quota")]
    try:
        run(g.analyze("https://www.youtube.com/watch?v=abcdefghijk"))
        raise AssertionError("ждали ошибку")
    except mn.MentorError as e:
        assert "лимит" in str(e)
    n = len(http.posts)
    try:
        run(g.analyze("https://www.youtube.com/watch?v=abcdefghijk"))
    except mn.MentorError:
        pass
    assert len(http.posts) == n and g.disabled_until > time.time()


def test_gemini_key_from_env():
    assert mn.gemini_key({"GEMINI_API_KEY": "AIzaX"}) == "AIzaX"
    assert mn.gemini_key({"AI_API_KEY": "AIzaY"}) == "AIzaY"
    assert mn.gemini_key({"AI_API_KEY": "gsk_groq"}) == ""


def test_text_fallback_when_no_gemini():
    class FakeAI:
        configured = True

        async def chat(self, messages, max_tokens=0, **kw):
            assert "Название: BTC breakout" in messages[1]["content"]
            return json.dumps({"summary": "по описанию", "market_view": "bullish",
                               "calls": [{"asset": "BTC", "direction": "long", "horizon_days": 7}]})

    now = time.time()
    ct, _, _ = setup(now)
    gem = GeminiVideo("", None)
    mt = Mentors(ct, None, FakeAI(), price_of=Prices(BTC=90000.0), env={}, youtube=FakeYouTube(), gemini=gem,
                 twitch=TwitchChat(None))
    rec = run(mt.analyze_video(None, {"id": "x", "url": "u", "title": "BTC breakout", "description": "d"}, now))
    assert rec["method"] == "text" and mt.st["calls"][0]["horizon_h"] == 168
    assert "по описанию" in mt.video_event(rec, None)["text"]
    try:
        run(mt.analyze_video(None, {"id": "y", "url": "u", "title": "", "description": ""}, now))
        raise AssertionError("ждали ошибку")
    except mn.MentorError as e:
        assert "GEMINI_API_KEY" in str(e)


# --- Telegram ------------------------------------------------------------------------------------------

def test_telegram_mentors_flow(bot_env):
    from tests.test_ai_flow import buttons, press
    from tests.test_telegram_flow import send

    b, session = bot_env
    b.mentors.youtube = FakeYouTube()
    b.mentors.gemini = FakeGemini()

    async def go():
        await send(b, "/mentors")
        text = session.sent[-1][1]
        assert "Учусь у трейдеров" in text and "Coin Bureau" in text and "Gemini" in text
        assert "mn:help" in buttons(session.markups[-1])
        await press(b, "mn:help")
        assert "aistudio.google.com/apikey" in session.sent[-1][1]
        # просто прислали ссылку на Twitch — бот предлагает следить
        await send(b, "https://www.twitch.tv/Megga")
        assert "Следить" in str(session.markups[-1]) and "mn:add:twitch:megga" in buttons(session.markups[-1])
        await press(b, "mn:add:twitch:megga")
        assert "tw:megga" in b.mentors.st["sources"] and "Слушаю чат Twitch" in session.sent[-1][1]
        # ссылка на ролик — разбор сразу (регистр id сохраняется)
        await send(b, "/mentors https://youtu.be/AbCdEfGhIjK")
        assert b.mentors.gemini.seen[-1].endswith("AbCdEfGhIjK")
        assert "Ждёт рост SOL" in session.sent[-1][1]
        await send(b, "/mentors del megga")
        assert b.mentors.st["sources"]["tw:megga"]["removed"]
        await press(b, "mn:toggle")
        assert not b.mentors.st["on"]
        assert b.mentors.ai_context()["traders"]
        assert "traders_youtube_twitch" in b._extras_context()

    asyncio.run(go())


def test_webapp_mentors_api(app_env):
    from tests import test_webapp as tw

    b, session = app_env
    b.mentors.youtube = FakeYouTube()
    b.mentors.gemini = FakeGemini()

    async def fn(client):
        status, d = await tw.get(client, "/api/mentors")
        assert status == 200 and len(d["sources"]) == 9 and d["gemini"] and d["owner"]
        status, d = await tw.get(client, "/api/mentors", uid=tw.FRIEND)
        assert status == 200 and not d["owner"]
        status, d = await tw.post(client, "/api/mentors", {"on": False}, uid=tw.FRIEND)
        assert status == 403
        status, d = await tw.post(client, "/api/mentors", {"add": "https://twitch.tv/megga"})
        assert status == 200 and "Слушаю чат Twitch" in d["done"][0] and any(s["id"] == "tw:megga" for s in d["sources"])
        status, d = await tw.post(client, "/api/mentors", {"add": "непонятно что"})
        assert status == 400 and "Не понял ссылку" in d["error"]
        status, d = await tw.post(client, "/api/mentors", {"remove": "tw:megga", "notify": False})
        assert status == 200 and not d["notify"] and not any(s["id"] == "tw:megga" for s in d["sources"])
        status, d = await tw.get(client, "/api/overview")
        assert d["mentors"]["sources"] == 9 and d["mentors"]["gemini"]

    tw.call(b, fn)


app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env


def test_mentors_tick_sends_start_note_and_video_digest(bot_env):
    from bot import mentors_ui

    b, session = bot_env
    yt = FakeYouTube()
    b.mentors.youtube = yt
    b.mentors.gemini = FakeGemini()
    b.mentors.price_of = Prices(SOL=100.0)
    yt.add(UC, "newvideo001", age_h=1)

    async def go():
        await mentors_ui.mentors_tick(b)
        texts = [t for _, t in session.sent]
        assert any("Начал учиться у трейдеров" in t for t in texts)
        assert any("Ждёт рост SOL" in t and "реклама" in t for t in texts)
        n = len(session.sent)
        await mentors_ui.mentors_tick(b)  # повторно: RSS ещё рано, ничего нового
        assert len(session.sent) == n
        assert b.mentors.st["seeded"] and b.copy.st["mentors"]["calls"]

    asyncio.run(go())
