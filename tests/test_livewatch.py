"""🎥 Стримы вживую: плейлисты Twitch, реклама, ffmpeg, разбор действий, итог эфира, цена ошибок, Telegram и Mini App."""

import asyncio
import base64
import json
import subprocess
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import livewatch as lw
from bot import mentors as mn
from bot.livewatch import (LiveError, LiveGemini, LiveWatch, TwitchVideo, clean_obs, live_query, parse_live,
                           parse_master, parse_media, pick_live_model, pick_variant, tail_segments)
from tests.test_ai_flow import press
from tests.test_mentors import Prices, make
from tests.test_webapp import call, post

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

NOW = time.time()

MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:NODE="video-edge",MANIFEST-NODE="x"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60 (source)",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=6500000,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2",VIDEO="chunked",FRAME-RATE=60.000
{base}/v/1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3400000,RESOLUTION=1280x720,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="720p30",FRAME-RATE=30.000
{base}/v/720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=852x480,CODECS="avc1.4D401F,mp4a.40.2",VIDEO="480p30",FRAME-RATE=30.000
{base}/v/480.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=160000,CODECS="mp4a.40.2",VIDEO="audio_only"
{base}/v/audio.m3u8
"""


def media(base, first=100, n=8, ads=0, daterange=False):
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
    if daterange:
        lines.append('#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-11-23T10:00:00.000Z",'
                     'DURATION=4.000')
    for i in range(n):
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:2026-11-23T10:00:{i * 2:02d}.000Z")
        ad = i < ads
        lines.append("#EXTINF:2.000," + ("Amazon|123" if ad and not daterange else "live"))
        lines.append(f"{base}/seg/{i % 8}.ts")
    lines.append(f"#EXT-X-TWITCH-PREFETCH:{base}/seg/0.ts")
    return "\n".join(lines) + "\n"


# --- разбор ------------------------------------------------------------------------------------

def test_master_and_variant_pick():
    v = parse_master(MASTER.format(base="https://x"))
    assert [x["h"] for x in v] == [1080, 720, 480, 0]
    best = pick_variant(v)
    assert best["url"] == "https://x/v/720.m3u8" and best["name"] == "720p30"
    # 720p30 нет — 480p, а не 1080p60 (не влезет в запрос к Gemini)
    assert pick_variant([x for x in v if x["h"] != 720])["h"] == 480
    assert pick_variant([v[0]])["h"] == 1080  # другого нет — берём что есть
    assert pick_variant([]) is None


def test_media_ads_and_tail():
    segs = parse_media(media("https://x", ads=3))
    assert len(segs) == 8 and [s["ad"] for s in segs[:4]] == [True, True, True, False]
    assert segs[0]["seq"] == 100 and segs[-1]["seq"] == 107  # PREFETCH не кусок
    dr = parse_media(media("https://x", daterange=True))
    assert [s["ad"] for s in dr[:3]] == [True, True, False]  # реклама по EXT-X-DATERANGE (4 с = 2 куска)
    got = {s["seq"]: s for s in segs if not s["ad"]}
    tail = tail_segments(got, 6)
    assert [s["seq"] for s in tail] == [105, 106, 107]
    got.pop(105)  # дыра: берём только свежие куски подряд
    assert [s["seq"] for s in tail_segments(got, 20)] == [106, 107]


def test_live_query_and_parse():
    q = live_query(["decu", "bad login\"}", "fashr"])
    assert 'user(login: "decu")' in q and "bad" not in q and "u1: user(login: \"fashr\")" in q
    data = {"data": {"u0": {"login": "decu", "displayName": "Decu", "stream": {
        "id": "42", "title": "SOL trenches", "createdAt": "2026-11-23T10:00:00Z", "viewersCount": 300,
        "game": {"name": "Crypto"}}}, "u1": {"login": "fashr", "stream": None}}}
    out = parse_live(data, ["decu", "bad login\"}", "fashr"])
    assert out["fashr"] is None and out["decu"]["id"] == "42" and out["decu"]["name"] == "Decu"
    assert out["decu"]["viewers"] == 300 and out["decu"]["started"] > 1.7e9


def test_clean_obs_validates_everything():
    raw = {"trading": "yes", "summary": "Вошёл в WIF", "platform": "Axiom", "asset": "$wif", "contract": "мусор",
           "market": "meme", "action": "ENTER_LONG",
           "position": {"side": "long", "entry": "$1,200", "leverage": "50x", "stop": "0,25", "pnl_pct": "-3.5%",
                        "size_usd": "1,234.5"},
           "pluses": [{"tag": "stop_set", "text": "стоп под лоу"}, {"tag": "magic"}, {"tag": "stop_set"}, "x"],
           "mistakes": [{"tag": "overleverage"}], "discipline": 14, "confidence": "HIGH"}
    o = clean_obs(raw)
    assert o["asset"] == "WIF" and o["contract"] == "" and o["action"] == "enter_long" and o["trading"] is True
    assert o["position"]["entry"] == 1200 and o["position"]["leverage"] == 50 and o["position"]["stop"] == 0.25
    assert o["position"]["size_usd"] == 1234.5 and o["position"]["pnl_pct"] == -3.5
    assert [p["tag"] for p in o["pluses"]] == ["stop_set"] and o["mistakes"][0]["text"] == "большое плечо"
    assert o["discipline"] == 10 and o["confidence"] == "high"
    # бот сам видит усреднение убытка: добавил к позиции в минусе
    add = clean_obs({"action": "add", "position": {"side": "long", "pnl_pct": -8}, "trading": True})
    assert add["mistakes"][0]["tag"] == "averaging_down" and "-8.0%" in add["mistakes"][0]["text"]
    junk = clean_obs({"action": "fly", "position": "x", "discipline": "abc", "market": "?"})
    assert junk["action"] == "none" and junk["discipline"] is None and junk["market"] == "unknown"
    assert clean_obs(None)["position"]["side"] == "none"


def test_pick_live_model():
    names = ["gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash-lite-preview",
             "gemini-3.5-flash-lite-native-audio", "gemini-2.5-flash-lite"]
    assert pick_live_model(names, set()) == "gemini-3.5-flash-lite"
    assert pick_live_model(names, {"gemini-3.5-flash-lite"}) == "gemini-3.1-flash-lite"
    assert pick_live_model(["gemini-2.5-flash"], set()) is None


# --- настоящее видео: ffmpeg -------------------------------------------------------------------

@pytest.fixture(scope="module")
def segments(tmp_path_factory):
    tv = TwitchVideo(None)
    ff = tv.ffmpeg()
    if not ff:
        pytest.skip("нет ffmpeg")
    d = tmp_path_factory.mktemp("seg")
    subprocess.run([ff, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15", "-f", "lavfi",
                    "-i", "sine=frequency=440:sample_rate=22050", "-t", "16", "-c:v", "libx264", "-preset",
                    "ultrafast", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-c:a", "aac", "-f", "hls",
                    "-hls_time", "2", "-hls_list_size", "0", "-hls_segment_filename", str(d / "%d.ts"),
                    str(d / "p.m3u8")], check=True, timeout=60)
    return [(d / f"{i}.ts").read_bytes() for i in range(8)]


def test_remux_ts_to_mp4(segments):
    ff = TwitchVideo(None).ffmpeg()
    mp4 = asyncio.run(lw.remux(ff, b"".join(segments[:3])))
    assert b"ftyp" in mp4[:32] and len(mp4) > 1000
    with pytest.raises(LiveError):
        asyncio.run(lw.remux(ff, b"not a video"))


# --- поддельный Twitch -------------------------------------------------------------------------

class FakeTwitch:
    def __init__(self, segments):
        self.segments = segments
        self.live = True
        self.ads = 0
        self.gql: list[dict] = []
        self.seg_hits = 0
        self.base = ""

    def app(self):
        app = web.Application()
        app.router.add_post("/gql", self.gql_h)
        app.router.add_get("/api/v2/channel/hls/{login}.m3u8", self.usher)
        app.router.add_get("/v/{q}.m3u8", self.media)
        app.router.add_get("/seg/{i}.ts", self.seg)
        app.router.add_get("/preview/{login}.jpg", self.preview)
        return app

    async def gql_h(self, request):
        body = await request.json()
        assert request.headers.get("Client-ID") == lw.TWITCH_CLIENT_ID
        self.gql.append(body)
        if body.get("operationName") == "PlaybackAccessToken":
            assert body["variables"]["login"] == "decu" and body["variables"]["playerType"] == "embed"
            return web.json_response({"data": {"streamPlaybackAccessToken": {"value": '{"channel":"decu"}',
                                                                            "signature": "abc"}}})
        q = body["query"]
        out = {}
        for i, login in enumerate(["decu", "fashr", "dvces", "heyitsyolo", "kryptokinglive"]):
            if f'"{login}"' in q:
                out[f"u{i}"] = {"login": login, "displayName": login.title(), "stream": {
                    "id": "s1", "title": "SOL memecoins live", "createdAt": "2026-11-23T10:00:00Z",
                    "viewersCount": 120, "game": {"name": "Crypto"}} if self.live and login == "decu" else None}
        # alias-номера идут по порядку переданных логинов
        logins = [x.split('"')[1] for x in q.split("user(login: ")[1:]]
        return web.json_response({"data": {f"u{i}": next((v for v in out.values() if v["login"] == lg), None)
                                           for i, lg in enumerate(logins)}})

    async def usher(self, request):
        assert request.query["sig"] == "abc" and request.query["supported_codecs"] == "h264"
        if not self.live:
            return web.Response(status=404)
        return web.Response(text=MASTER.format(base=self.base))

    async def media(self, request):
        assert request.match_info["q"] == "720"
        return web.Response(text=media(self.base, ads=self.ads))

    async def seg(self, request):
        self.seg_hits += 1
        return web.Response(body=self.segments[int(request.match_info["i"])])

    async def preview(self, request):
        return web.Response(body=b"\xff\xd8\xff\xe0JPEGDATA", content_type="image/jpeg")


class FakeLiveAI:
    key = "AQ.test"
    model = "gemini-test-flash-lite"
    disabled_until = 0.0
    last_error = None
    configured = True

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, int, str]] = []

    async def analyze(self, data, mime, prompt):
        self.calls.append((mime, len(data), prompt))
        if data[:4] != b"\xff\xd8\xff\xe0":
            assert b"ftyp" in data[:32]  # настоящий MP4 после ffmpeg
        return self.answers.pop(0) if self.answers else {"trading": False, "summary": "болтает с чатом",
                                                         "action": "talking"}


ENTRY = {"trading": True, "summary": "Открыл лонг SOL на Bybit с плечом", "platform": "Bybit", "asset": "SOL",
         "market": "perp", "action": "enter_long", "said": "беру лонг, стоп пока не ставлю",
         "position": {"side": "long", "entry": 100, "leverage": 50, "stop": None, "pnl_pct": 0},
         "pluses": [{"tag": "explained", "text": "объяснил уровень"}],
         "mistakes": [{"tag": "no_stop", "text": "открыл позицию без стопа"},
                      {"tag": "overleverage", "text": "плечо 50x"}],
         "discipline": 3, "lesson": "Стоп ставится сразу при входе", "confidence": "high"}
ADD = {"trading": True, "summary": "Докупил SOL в минусе", "asset": "SOL", "market": "perp", "action": "add",
       "position": {"side": "long", "entry": 98, "pnl_pct": -4}, "mistakes": [{"tag": "no_stop"}],
       "discipline": 2, "lesson": "Не усредняй убыток", "confidence": "high"}
EXIT = {"trading": True, "summary": "Закрыл SOL в минус", "asset": "SOL", "action": "exit_loss",
        "position": {"side": "none"}, "pluses": [{"tag": "cut_loss", "text": "закрыл, не стал пересиживать"}],
        "discipline": 6, "confidence": "medium"}


def live_env(segments, answers, monkeypatch=None):
    mt, ct, yt, dex = make(NOW, prices=Prices(SOL=100.0))
    mt._add("twitch", "decu", "decu")
    tw = FakeTwitch(segments)
    ai = FakeLiveAI(answers)
    return mt, tw, ai


async def start(mt, tw, ai):
    server = TestServer(tw.app())
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    tw.base = base
    tv = TwitchVideo(mt._session)
    tv.gql_url, tv.usher_url, tv.preview_url = base + "/gql", base, base + "/preview/{}.jpg"
    tv.POLL_SEC, tv.WAIT_SEC = 0.0, 0.0
    mt.live = LiveWatch(mt, tv=tv, ai=ai)
    return server


def test_full_stream_flow(segments):
    mt, tw, ai = live_env(segments, [ENTRY, ADD, EXIT])

    async def run():
        server = await start(mt, tw, ai)
        try:
            L = mt.live
            ev = await L.step(NOW)
            # эфир найден, первый взгляд: вход в лонг без стопа
            assert "decu" in L.st["sessions"] and len(ai.calls) == 1
            mime, size, prompt = ai.calls[0]
            assert mime == "video/mp4" and size > 1000 and "Прошлый взгляд" not in prompt and "decu" in prompt
            assert tw.seg_hits == 8  # 16 с = 8 кусков по 2 с
            assert len(ev) == 1 and "Вошёл в LONG" in ev[0]["text"] and "<b>SOL</b>" in ev[0]["text"]
            assert "нет стопа" in ev[0]["text"] and "плечо 50x" in ev[0]["text"] and "проверю ценой" in ev[0]["text"]
            calls = [c for c in mt.st["calls"] if c.get("live")]
            assert len(calls) == 1 and calls[0]["price0"] == 100.0 and set(calls[0]["tags"]) >= {"no_stop", "explained"}
            src = mt.st["sources"]["tw:decu"]
            assert src["live"]["entries"] == 1 and src["live"]["miss"] == {"no_stop": 1, "overleverage": 1}

            # через минуту — рано (смотрим раз в 5 минут)
            assert await L.step(NOW + 60) == [] and len(ai.calls) == 1
            # стример кинул токен в чат → смотрим сразу
            mt.twitch.last_call["decu"] = NOW + 100
            ev = await L.step(NOW + 130)
            assert len(ai.calls) == 2 and "Прошлый взгляд" in ai.calls[1][2] and "enter_long SOL" in ai.calls[1][2]
            assert "кинул токен в чат" in ev[0]["text"] and "Добавил к позиции" in ev[0]["text"]
            assert "усредняет убыток" in ev[0]["text"]
            # «нет стопа» с той же монетой подряд не считаем дважды; усреднение — новая ошибка
            assert src["live"]["miss"] == {"no_stop": 1, "overleverage": 1, "averaging_down": 1}

            ev = await L.step(NOW + 130 + 300)
            assert "Закрыл в убыток" in ev[0]["text"] and "быстро режет убыток" in ev[0]["text"]

            # эфир кончился → итог
            tw.live = False
            ev = await L.step(NOW + 130 + 300 + 130)
            assert "decu" not in L.st["sessions"]
            text = ev[-1]["text"]
            assert "Итог эфира decu" in text and "смотрел 3 раз" in text and "входов 1 (SOL)" in text
            assert "✂️ 1" in text and "нет стопа ×1" in text and "быстро режет убыток ×1" in text
            assert "Дисциплина в среднем 3.7/10" in text
            assert src["live"]["sessions"] == 1 and L.st["history"][-1]["entries"] == 1
            d = L.status_dict()
            assert d["cards"][0]["name"] == "decu" and d["cards"][0]["entries"] == 1 and d["video"] is True
            assert "Привычки стримеров" in L.text()
            assert mt.ai_context()["live_streams"]["streamer_habits"][0]["top_mistakes"]
        finally:
            await server.close()
            await mt.close()

    asyncio.run(run())


def test_ads_budget_and_limits(segments):
    mt, tw, ai = live_env(segments, [dict(ENTRY, summary="кадр: график SOL", action="watching")])

    async def run():
        server = await start(mt, tw, ai)
        try:
            L = mt.live
            tw.ads = 8  # всё — рекламная вставка: видео нет, смотрим превью-кадр эфира
            ev = await L.step(NOW)
            assert ai.calls[0][0] == "image/jpeg" and "рекламная вставка" in ev[0]["text"]
            sess = L.st["sessions"]["decu"]
            assert sess["mode"] == "image"
            tw.ads = 3
            L.st["budget"] = {"day": lw._day(NOW), "n": lw.LIVE_PER_DAY}
            assert await L.step(NOW + 400) == [] and len(ai.calls) == 1  # лимит на день
            L.st["budget"]["n"] = 0
            L.st["notify"] = "summary"  # только итог — о действиях не пишем
            ai.answers.append(dict(ENTRY, asset="BTC"))
            mt.price_of = Prices(SOL=100.0, BTC=90000.0)
            assert await L.step(NOW + 800) == [] and len(ai.calls) == 2 and ai.calls[1][0] == "video/mp4"
            # Twitch лёг: ошибка, повтор через ~1,5 мин, а не через 5
            await server.close()
            mt.live.tv._media.clear()
            assert await L.step(NOW + 1200) == [] and len(ai.calls) == 2 and L.st["last_error"]
            assert 0 < NOW + 1200 - sess["last_at"] + 90 <= 5 * 60 + 90
            assert [c for c in mt.st["calls"] if c.get("live")]  # но вход всё равно проверяется ценой
            L.st["on"] = False
            assert await L.step(NOW + 2000) == []
        finally:
            await server.close()
            await mt.close()

    asyncio.run(run())


def test_snapshot_when_no_ffmpeg(segments):
    mt, tw, ai = live_env(segments, [{"trading": True, "summary": "график SOL", "action": "watching"}])

    async def run():
        server = await start(mt, tw, ai)
        try:
            mt.live.tv._ffmpeg = None
            ev = await mt.live.observe("decu", NOW, force_event=True)
            mime, size, prompt = ai.calls[0]
            assert mime == "image/jpeg" and "кадр (без звука)" in prompt
            assert "только кадр без звука" in ev[0]["text"]
            with pytest.raises(LiveError):
                await mt.live.observe("nobody", NOW)
        finally:
            await server.close()
            await mt.close()

    asyncio.run(run())


def test_lessons_cost_of_mistakes():
    mt, *_ = make(NOW)
    src = mt._add("twitch", "decu", "decu")
    for i, (tags, ret) in enumerate([(["no_stop"], -3), (["no_stop", "fomo_chase"], -5), (["no_stop"], -4),
                                     (["stop_set"], 2), (["stop_set"], 1), (["stop_set", "explained"], 3)]):
        mt.st["calls"].append({"id": f"l{i}", "source": src["id"], "live": True, "done": True, "ret": ret,
                               "tags": tags, "key": "SOL", "at": NOW, "kind": "cex", "direction": "long"})
    rows = {x["tag"]: x for x in mt.live.lessons()}
    assert rows["no_stop"]["n"] == 3 and rows["no_stop"]["avg"] == -4.0 and rows["no_stop"]["kind"] == "miss"
    assert rows["stop_set"]["avg"] == 2.0 and rows["clean"]["n"] == 3 and "fomo_chase" not in rows
    assert mt.live.lessons()[0]["tag"] == "no_stop"  # дорогие ошибки — первыми
    assert "Сколько стоит ошибка" in mt.live.text()


# --- Gemini: настоящий формат запроса ---------------------------------------------------------

def test_live_gemini_request(monkeypatch):
    seen = {}
    state = {"status": 200}

    async def models(request):
        return web.json_response({"models": [
            {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]}]})

    async def gen(request):
        seen["model"] = request.match_info["model"]
        seen["key"] = request.headers.get("x-goog-api-key")
        seen["body"] = await request.json()
        if state["status"] == 429:
            return web.json_response({"error": {"message": "quota"}}, status=429)
        return web.json_response({"candidates": [{"content": {"parts": [{"text": json.dumps(ENTRY)}]}}]})

    async def run():
        app = web.Application()
        app.router.add_get("/v1beta/models", models)
        app.router.add_post("/v1beta/models/{model}:generateContent", gen)
        server = TestServer(app)
        await server.start_server()
        monkeypatch.setattr(mn, "GEMINI_BASE", str(server.make_url("/v1beta")))
        import aiohttp
        http = aiohttp.ClientSession()

        async def session():
            return http
        try:
            g = LiveGemini("AQ.key", session)
            out = await g.analyze(b"\x00\x01video", "video/mp4", "Смотри «эфир»")
            assert out["action"] == "enter_long" and seen["model"] == "gemini-3.5-flash-lite"
            part = seen["body"]["contents"][0]["parts"]
            assert part[0]["inline_data"]["mime_type"] == "video/mp4"
            assert base64.b64decode(part[0]["inline_data"]["data"]) == b"\x00\x01video"
            assert part[1]["text"] == "Смотри «эфир»" and seen["key"] == "AQ.key"
            assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
            state["status"] = 429
            with pytest.raises(LiveError, match="лимит"):
                await g.analyze(b"x", "video/mp4", "p")
            assert g.disabled_until > time.time()
            with pytest.raises(LiveError, match="жду"):
                await g.analyze(b"x", "video/mp4", "p")
        finally:
            await http.close()
            await server.close()

    asyncio.run(run())


# --- Telegram и Mini App ----------------------------------------------------------------------

def test_telegram_live(bot_env, segments):
    b, session = bot_env
    mt = b.mentors

    async def run():
        tw = FakeTwitch(segments)
        ai = FakeLiveAI([ENTRY])
        mt.price_of = Prices(SOL=100.0)
        server = await start(mt, tw, ai)
        try:
            await press(b, "mn:show")
            await press(b, "mn:live")
            assert "Стримы вживую" in session.sent[-1][1]
            await press(b, "mn:lint:10")
            assert mt.live.st["every_min"] == 10
            await press(b, "mn:lnotify")
            assert mt.live.st["notify"] == "all"
            await press(b, "mn:lhelp")
            assert "Как бот смотрит стримы" in session.sent[-1][1]
            from tests.test_telegram_flow import send
            await send(b, "/stream decu")  # decu — из стартового списка 🎓: смотрит сразу
            texts = [t for _, t in session.sent[-4:]]
            assert not any("Добавил стримера" in t for t in texts)
            assert any("Вошёл в LONG" in t for t in texts), texts
            assert mt.live.st["focus"] == "decu" and len(ai.calls) == 1
            await send(b, "/stream off")
            assert mt.live.st["on"] is False
            await send(b, "/stream")
            assert "Сейчас в эфире" in session.sent[-1][1] and "decu" in session.sent[-1][1]
            mt.live.st["on"] = True
            ai.answers.append({"trading": True, "summary": "график", "action": "watching"})
            await send(b, "/stream twitch.tv/NewTrader_1")  # нового — добавит в 🎓
            assert any("Добавил стримера <b>newtrader_1</b>" in t for _, t in session.sent[-4:])
            assert mt.st["sources"]["tw:newtrader_1"]["kind"] == "twitch"
            await send(b, "/stream ???")
            assert "Не понял ник" in session.sent[-1][1]
        finally:
            await server.close()
            await mt.close()

    asyncio.run(run())


def test_webapp_live(app_env):
    b, _ = app_env

    async def fn(client):
        st, d = await post(client, "/api/mentors", {"live": {"every_min": 10, "notify": "summary", "on": False}})
        assert st == 200 and d["live"]["every_min"] == 10 and d["live"]["notify"] == "summary"
        assert d["live"]["on"] is False and "lessons" in d["live"] and "cards" in d["live"]
        st, d = await post(client, "/api/mentors", {"live": {"every_min": 7}})
        assert st == 400
        st, d = await post(client, "/api/mentors", {"live": {"notify": "spam"}})
        assert st == 400

    call(b, fn)
