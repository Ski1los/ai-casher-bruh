"""🔭 Разведчик: поиск трейдеров, фейк-фильтр, испытательный срок, Telegram и Mini App."""

import asyncio
import time

from bot import scout as sc
from bot.mentors import parse_feed
from tests.test_ai_flow import press
from tests.test_mentors import make
from tests.test_webapp import call, post

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

NOW = time.time()
DAY = 86400


def uc(n: int) -> str:
    return "UC" + str(n).rjust(22, "x")


def videos(n=6, title="Bitcoin and SOL trade setup", desc="My analysis", views=20_000, age_days=1, short=False):
    return [{"id": f"v{i}{title[:3]}", "title": title, "description": desc, "views": views, "short": short,
             "published": NOW - (age_days + i) * DAY, "url": f"https://www.youtube.com/watch?v=v{i}"} for i in range(n)]


# ---------------------------------------------------------------------------
# Разбор
# ---------------------------------------------------------------------------

def test_parse_counts_and_search_page():
    assert sc.parse_count("1.2M subscribers") == 1_200_000
    assert sc.parse_count("523K subscribers") == 523_000
    assert sc.parse_count("12,3 тыс. подписчиков") == 12_300
    assert sc.parse_count("1 234 subscribers") == 1234
    assert sc.parse_count("нет") is None
    page = ('var ytInitialData = {"contents":[{"channelRenderer":{"channelId":"' + uc(1) + '","title":{"simpleText":'
            '"Crypto \\"Pro\\" Trader"},"subscriberCountText":{"simpleText":"@cryptopro"},"videoCountText":'
            '{"simpleText":"245K subscribers"}}},{"channelRenderer":{"channelId":"' + uc(2) + '","title":{"simpleText":'
            '"Мемкоины"},"videoCountText":{"simpleText":"8,1 тыс. подписчиков"}}},{"channelRenderer":{"channelId":"'
            + uc(1) + '"}}]}')
    rows = sc.parse_yt_search(page)
    assert [r["channel_id"] for r in rows] == [uc(1), uc(2)]
    assert rows[0]["name"] == 'Crypto "Pro" Trader' and rows[0]["subs"] == 245_000 and rows[1]["subs"] == 8100


def test_parse_twitch_and_gemini():
    data = {"data": {"game": {"streams": {"edges": [
        {"node": {"title": "SOL memecoin trenches live", "viewersCount": 340,
                  "broadcaster": {"login": "DegenKing", "displayName": "DegenKing", "followers": {"totalCount": 9000}}}},
        {"node": {"title": "x", "broadcaster": {}}}]}}}}
    rows = sc.parse_twitch_streams(data)
    assert rows == [{"login": "degenking", "name": "DegenKing", "viewers": 340, "title": "SOL memecoin trenches live",
                     "followers": 9000}]
    assert sc.parse_twitch_streams({"errors": [1]}) == []
    text = """```json
[{"platform": "youtube", "handle": "https://youtube.com/@DataDash", "name": "DataDash", "why": "TA"},
 {"platform": "twitch", "handle": "twitch.tv/Trencher_1", "name": "Trencher"},
 {"platform": "tiktok", "handle": "x"}, {"platform": "youtube", "handle": "!!!"}, "мусор"]
```"""
    c = sc.parse_gemini_candidates(text)
    assert [(x["platform"], x["handle"]) for x in c] == [("youtube", "DataDash"), ("twitch", "trencher_1")]
    assert sc.parse_gemini_candidates("не JSON") == []


def test_feed_has_views():
    xml = """<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom"><title>Канал</title>
      <entry><yt:videoId>abc</yt:videoId><title>BTC</title><link href="https://www.youtube.com/watch?v=abc"/>
      <published>2026-11-18T10:00:00+00:00</published><media:group><media:description>d</media:description>
      <media:community><media:statistics views="4321"/></media:community></media:group></entry></feed>"""
    _, vids = parse_feed(xml)
    assert vids[0]["views"] == 4321


# ---------------------------------------------------------------------------
# Фейк-фильтр
# ---------------------------------------------------------------------------

def test_vet_youtube_catches_fakes():
    good = sc.vet_youtube(videos(), 120_000, NOW)
    assert good["ok"] and good["risk"] < 3, good
    bought = sc.vet_youtube(videos(views=150), 900_000, NOW)  # 900K подписчиков и 150 просмотров
    assert not bought["ok"] and "накрутку" in " ".join(bought["reasons"])
    casino = sc.vet_youtube(videos(desc="Play at stake.com, join my VIP signals group"), 50_000, NOW)
    assert not casino["ok"] and "казино" in " ".join(casino["reasons"])
    dead = sc.vet_youtube(videos(age_days=40), 80_000, NOW)
    assert not dead["ok"] and "новых роликов нет" in dead["reasons"][0]
    bait = sc.vet_youtube(videos(title="🚨🚨 THIS COIN WILL 1000x GUARANTEED"), 80_000, NOW)
    assert bait["risk"] >= 4
    offtopic = sc.vet_youtube(videos(title="My cooking vlog"), 80_000, NOW)
    assert not offtopic["ok"]
    shorts = sc.vet_youtube(videos(short=True), 80_000, NOW)
    assert not shorts["ok"]
    assert not sc.vet_youtube([], 10, NOW)["ok"]


def test_vet_twitch():
    assert sc.vet_twitch({"title": "Solana memecoins live, axiom trenches", "viewers": 200})["ok"]
    assert not sc.vet_twitch({"title": "STAKE casino slots giveaway", "viewers": 900})["ok"]
    lonely = sc.vet_twitch({"title": "just chatting", "viewers": 3})
    assert not lonely["ok"] and len(lonely["reasons"]) == 2


# ---------------------------------------------------------------------------
# Поиск и испытание
# ---------------------------------------------------------------------------

def setup_scout(gemini_c=None, yt_rows=None, streams=None):
    mt, ct, yt, dex = make(NOW)
    mt.scout.st["on"] = True
    s = mt.scout

    async def gem(n):
        if isinstance(gemini_c, Exception):
            raise gemini_c
        return list(gemini_c or [])

    async def search(q):
        return list(yt_rows or [])

    async def tw():
        return list(streams or [])

    s._gemini_candidates, s._yt_search, s._twitch_streams = gem, search, tw

    async def resolve(kind, value):
        return {"DataDash": uc(9), "Fake": uc(8)}[value], value

    yt.resolve = resolve
    return mt, yt, s


def add_feed(yt, cid, vids):
    yt.videos[cid] = vids


def test_discover_vets_and_puts_on_trial():
    rows = [{"channel_id": uc(1), "name": "Good TA", "subs": 150_000},
            {"channel_id": uc(2), "name": "Bot Subs", "subs": 2_000_000},
            {"channel_id": uc(3), "name": "Casino Guy", "subs": 70_000}]
    streams = [{"login": "trencher", "name": "Trencher", "viewers": 300, "title": "SOL memes, axiom calls", "followers": 1},
               {"login": "slotsman", "name": "SlotsMan", "viewers": 5000, "title": "Stake slots", "followers": 1}]
    gem = [{"platform": "youtube", "handle": "DataDash", "name": "DataDash", "why": "сетапы"},
           {"platform": "twitch", "handle": "gemtrader", "name": "GemTrader", "why": "мемы"}]
    mt, yt, s = setup_scout(gem, rows, streams)
    add_feed(yt, uc(1), videos())
    add_feed(yt, uc(2), videos(views=100))
    add_feed(yt, uc(3), videos(desc="rollbit casino bonus"))
    add_feed(yt, uc(9), videos(title="Altcoin setups: ETH, SOL"))
    events = asyncio.run(s.discover(NOW))
    srcs = {x["id"]: x for x in mt.active()}
    assert f"yt:{uc(9)}" in srcs and f"yt:{uc(1)}" in srcs  # DataDash (Gemini) и Good TA прошли
    assert f"yt:{uc(2)}" not in srcs and f"yt:{uc(3)}" not in srcs
    assert "tw:trencher" in srcs and "tw:gemtrader" in srcs and "tw:slotsman" not in srcs
    t = srcs[f"yt:{uc(1)}"]
    assert t["auto"] and t["trial_until"] > NOW + 20 * DAY and t["via"] == "поиск YouTube" and not t["user"]
    assert len(t["seen"]) == 6  # старые ролики не разбираем
    rej = s.st["rejected"]
    assert "накрутку" in rej[f"yt:{uc(2)}"]["why"] and "казино" in rej[f"yt:{uc(3)}"]["why"]
    assert "гемблинг" in rej["tw:slotsman"]["why"]
    assert events and "Разведчик нашёл" in events[0]["text"]
    # повторный поиск раньше суток — ничего не делает; отсеянных и знакомых не берёт снова
    assert asyncio.run(s.discover(NOW + 3600)) == []
    before = len(mt.active())
    asyncio.run(s.discover(NOW + 2 * DAY))
    assert len(mt.active()) == before


def test_trial_limits_respected():
    rows = [{"channel_id": uc(i), "name": f"TA {i}", "subs": 100_000} for i in range(10, 20)]
    mt, yt, s = setup_scout(None, rows, [])
    for i in range(10, 20):
        add_feed(yt, uc(i), videos())
    asyncio.run(s.discover(NOW))
    assert len(s.trial("youtube")) == sc.ADD_PER_RUN["youtube"]
    asyncio.run(s.discover(NOW + 1.1 * DAY))
    assert len(s.trial("youtube")) == sc.MAX_TRIAL["youtube"]  # не больше 5 испытуемых сразу
    asyncio.run(s.discover(NOW + 2.2 * DAY))
    assert len(s.trial("youtube")) == sc.MAX_TRIAL["youtube"]


def test_discover_survives_broken_sources():
    mt, yt, s = setup_scout(RuntimeError("Gemini 429"), [], [])

    async def broken(q):
        raise RuntimeError("YouTube ответил 429")

    s._yt_search = broken
    assert asyncio.run(s.discover(NOW)) == []
    assert "Gemini" in s.st["last_error"] and "YouTube" in s.st["last_error"]


def trial_src(mt, s, kind="youtube", ident=None, **kw):
    src = mt._add(kind, ident or uc(50), "Испытуемый", user=False)
    s._mark(src, "тест", 0, NOW, kind)
    src.update(kw)
    return src


def test_review_outcomes():
    mt, yt, s = setup_scout()
    star = trial_src(mt, s, ident=uc(51), status="star", rets=[5.0] * 9)
    loser = trial_src(mt, s, ident=uc(52), status="muted", rets=[-6.0] * 9)
    ads = trial_src(mt, s, ident=uc(53), videos=4, sponsored=3)
    hype = trial_src(mt, s, ident=uc(54), hype=[8, 9, 8, 9])
    silent = trial_src(mt, s, ident=uc(55), videos=4, calls=0)
    quiet_tw = trial_src(mt, s, kind="twitch", ident="quietguy", found_at=NOW - 7 * DAY)
    waiting = trial_src(mt, s, ident=uc(56), videos=2, calls=3)
    events = s.review(NOW)
    assert not star["removed"] and star["trial_until"] is None and s.st["kept"] == 1
    assert any("прошёл испытание" in e["text"] for e in events)
    for src, word in ((loser, "теряют"), (ads, "реклама"), (hype, "хайп"), (silent, "ни одного проверяемого"),
                      (quiet_tw, "адреса токена")):
        assert src["removed"], src["name"]
        assert word in s.st["rejected"][src["id"]]["why"]
    assert not waiting["removed"]
    # срок вышел: в плюсе, но проверок мало — ещё неделя; без плюса — убираем
    waiting["rets"] = [1.0, 2.0, 0.5]
    s.review(waiting["trial_until"] + 1)
    assert not waiting["removed"] and waiting["trial_until"] > NOW + 21 * DAY
    waiting["rets"] = [-1.0, 0.5, -2.0]
    s.review(waiting["trial_until"] + 1)
    assert waiting["removed"] and "без доказанного плюса" in s.st["rejected"][waiting["id"]]["why"]
    # ⭐, который потом стал терять, тоже убирается (он найден ботом, а не тобой)
    star["status"] = "muted"
    s.review(NOW + DAY)
    assert star["removed"]
    # свои и стартовые каналы разведчик не трогает
    own = mt._add("youtube", uc(57), "Мой канал", user=True)
    own["status"] = "muted"
    s.review(NOW)
    assert not own["removed"]


def test_trial_videos_use_only_spare_budget():
    mt, yt, s = setup_scout()
    src = trial_src(mt, s, ident=uc(60))
    src["checked_at"] = 0
    assert s.can_watch(src, NOW, 10)
    assert not s.can_watch(src, NOW, sc.TRIAL_BUDGET_RESERVE)  # 5 последних роликов дня — проверенным трейдерам
    src["checked_at"] = NOW - 3600
    assert not s.can_watch(src, NOW, 10)  # испытуемых — раз в 3 часа
    normal = mt._add("youtube", uc(61), "Обычный")
    assert s.can_watch(normal, NOW, 0)


def test_status_and_text():
    mt, yt, s = setup_scout()
    trial_src(mt, s, ident=uc(70))
    s._reject("yt:x", "Фейк", "фейк-фильтр: похоже на накрутку", NOW)
    d = mt.status_dict()
    assert d["scout"]["trial"][0]["name"] == "Испытуемый" and d["scout"]["rejected_total"] == 1
    assert any(x["trial"] for x in d["sources"])
    text = s.text()
    assert "Разведчик" in text and "На испытании" in text and "накрутку" in text
    assert "🔭" in mt.text()
    assert mt.ai_context()["scout"]["on_trial"] == ["Испытуемый"]


# ---------------------------------------------------------------------------
# Telegram и Mini App
# ---------------------------------------------------------------------------

def test_telegram_scout(bot_env):
    b, session = bot_env
    found = []

    async def fake_discover(now=None, force=False):
        found.append(force)
        return [{"text": "🔭 <b>Разведчик нашёл трейдеров</b>: ▶️ Тест", "buttons": []}]

    b.mentors.scout.discover = fake_discover

    async def run():
        await press(b, "mn:scout")
        assert "Разведчик" in session.sent[-1][1]
        await press(b, "mn:sfind")
        assert found == [True]
        assert any("нашёл трейдеров" in t for _, t in session.sent[-3:])
        on = b.mentors.scout.st["on"]
        await press(b, "mn:stoggle")
        assert b.mentors.scout.st["on"] is (not on)
        from tests.test_telegram_flow import send
        await send(b, "/scout")
        assert "Разведчик" in session.sent[-1][1]

    asyncio.run(run())


def test_webapp_scout_toggle(app_env):
    b, _ = app_env

    async def fn(client):
        st, d = await post(client, "/api/mentors", {"scout": {"on": True}})
        assert st == 200 and d["scout"]["on"] is True
        st, d = await post(client, "/api/mentors", {"scout": {"on": False}})
        assert d["scout"]["on"] is False

    call(b, fn)
