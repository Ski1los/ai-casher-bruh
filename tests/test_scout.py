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


# ---------------------------------------------------------------------------
# v30: 📜 история прогнозов, снежный ком, самообучение, рейтинг
# ---------------------------------------------------------------------------

def test_title_calls():
    assert sc.title_calls("Bitcoin to $150K soon!") == [{"asset": "BTC", "direction": "long"}]
    assert sc.title_calls("SOL and ETH will CRASH this week") == [{"asset": "SOL", "direction": "short"},
                                                                   {"asset": "ETH", "direction": "short"}]
    assert sc.title_calls("Биткоин: обвал неизбежен?") == [{"asset": "BTC", "direction": "short"}]
    assert sc.title_calls("Don't buy $PEPE yet") == [{"asset": "PEPE", "direction": "short"}]
    assert sc.title_calls("XRP pump or dump?") == []      # обе стороны — не прогноз
    assert sc.title_calls("Bitcoin news today") == []      # нет направления
    assert sc.title_calls("This link will pump") == []     # «link» — слово, а не монета
    assert sc.parse_title_calls('[{"i": 2, "asset": "btc", "direction": "long"}, {"i": 9, "asset": "X", '
                                '"direction": "up"}, {"i": 1, "asset": "$SOL", "direction": "short"}]', 3) == [
        {"i": 2, "asset": "BTC", "direction": "long"}, {"i": 1, "asset": "SOL", "direction": "short"}]


def day_candles(start: float, closes: list[float]) -> list[list[float]]:
    """Дневные свечи с полуночи после start; open = close прошлой."""
    t0 = (int(start // DAY) + 1) * DAY
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append([(t0 + i * DAY) * 1000, prev, max(prev, c), min(prev, c), c, 1.0])
        prev = c
    return out


def test_backtest_math():
    pub = NOW - 20 * DAY
    candles = {"BTC": day_candles(pub, [100, 102, 104, 110, 120, 90] + [90] * 14)}
    items = [{"asset": "BTC", "direction": "long", "published": pub, "title": "BTC up"},
             {"asset": "BTC", "direction": "long", "published": pub + 3600, "title": "повтор"},
             {"asset": "BTC", "direction": "short", "published": pub + 3 * DAY, "title": "BTC down"},
             {"asset": "ETH", "direction": "long", "published": pub, "title": "нет свечей"},
             {"asset": "BTC", "direction": "long", "published": NOW - DAY, "title": "срок не прошёл"}]
    h = sc.backtest(items, candles, NOW)
    assert h["n"] == 2  # повтор, монета без свечей и свежий прогноз не считаются
    rows = sorted(h["rows"], key=lambda r: r["at"])
    # вход — открытие первой свечи после ролика (100), выход — закрытие 3-й (104), минус 0.2% комиссий
    assert rows[0]["ret"] == round((104 / 100 - 1) * 100 - 0.2, 2)
    # шорт: вход 104 (открытие первой свечи после pub+3д = закрытие прошлой), выход — закрытие через 3 дня (90)
    assert rows[1]["ret"] == round(-(90 / 104 - 1) * 100 - 0.2, 2)
    assert h["wins"] == 2 and h["bull_share"] == 0.5 and h["shrunk"] == round(sum(r["ret"] for r in rows) / 7, 2)
    bad = {"n": 6, "wins": 1, "avg": -5.0, "shrunk": -2.7, "bull_share": 1.0}
    assert sc.history_bad(bad) and not sc.history_bad({**bad, "n": 3}) and not sc.history_bad(None)
    assert "почти все «вверх»" in sc.history_text(bad)
    assert "нет проверяемых" in sc.history_text({**bad, "n": 0})


def test_extract_refs_and_queries():
    text = ("Мои друзья: https://www.youtube.com/@DataDash и youtube.com/channel/" + uc(5) +
            " стримы: https://twitch.tv/Trencher_1 , https://www.twitch.tv/videos/123")
    refs = sc.extract_refs(text)
    assert {"platform": "youtube", "channel_id": uc(5), "name": uc(5)} in refs
    assert {"platform": "youtube", "handle": "DataDash", "name": "DataDash"} in refs
    assert {"platform": "twitch", "handle": "trencher_1", "name": "Trencher_1"} in refs
    assert not any(r.get("handle") == "videos" for r in refs)
    assert sc.pick_queries({}, 2, 0) == list(sc.YT_QUERIES[:2])
    assert sc.pick_queries({}, 2, 3) == list(sc.YT_QUERIES[3:5])  # без опыта — по кругу
    good, bad = sc.YT_QUERIES[5], sc.YT_QUERIES[0]
    q = sc.pick_queries({good: {"taken": 3, "kept": 2}, bad: {"taken": 6, "kept": 0}}, 2, 0)
    assert q[0] == good and bad not in q


def with_candles(mt, series: dict[str, list[float]], start: float):
    async def candles_of(coin, since_ms):
        return day_candles(start, series[coin]) if coin in series else []

    mt.candles_of = candles_of


def titled(titles: list[str], first_age_days=4, step_days=4):
    return [{"id": f"t{i}", "title": t, "description": "my analysis", "views": 30_000, "short": False,
             "published": NOW - (first_age_days + i * step_days) * DAY, "url": ""} for i, t in enumerate(titles)]


def test_discover_uses_history_and_ranks():
    start = NOW - 40 * DAY
    up = [100 + i * 3 for i in range(45)]  # BTC растёт весь месяц
    rows = [{"channel_id": uc(i), "name": n, "subs": 100_000} for i, n in ((31, "Wrong Bear"), (32, "Meh"), (33, "Right Bull"))]
    mt, yt, s = setup_scout(None, rows, [])
    s._gemini_ready = lambda: False  # прогнозы из названий — по словам
    with_candles(mt, {"BTC": up}, start)
    add_feed(yt, uc(31), titled(["Bitcoin crash incoming"] * 6))           # всё время шортил растущий BTC
    add_feed(yt, uc(32), titled(["Crypto market update"] * 6))             # без прогнозов
    add_feed(yt, uc(33), titled(["Bitcoin breakout, going up"] * 6))       # угадывал
    for cid in (uc(31), uc(32), uc(33)):
        yt.videos[cid][0]["published"] = NOW - DAY                          # канал живой
    mt.scout.st["q_stats"] = {}
    sc_add = sc.ADD_PER_RUN["youtube"]
    sc.ADD_PER_RUN["youtube"] = 1
    try:
        asyncio.run(s.discover(NOW))
    finally:
        sc.ADD_PER_RUN["youtube"] = sc_add
    ids = {x["id"] for x in mt.active("youtube") if x.get("auto")}
    assert ids == {f"yt:{uc(33)}"}  # из двух прошедших взял лучшего по истории
    assert "в минусе" in s.st["rejected"][f"yt:{uc(31)}"]["why"]
    assert f"yt:{uc(32)}" not in s.st["rejected"]  # не взят, но и не отвергнут — найдётся позже
    src = mt.st["sources"][f"yt:{uc(33)}"]
    assert src["history"]["n"] >= 4 and src["history"]["avg"] > 0 and src["history"]["method"] == "по словам в названиях"
    assert "📜 история" in s.text()
    assert s.st["via_stats"]["поиск YouTube"]["taken"] == 1 and src["query"] in sc.YT_QUERIES


def test_gemini_title_extraction_and_fallback():
    mt, yt, s = setup_scout()
    vids = titled(["Watch this", "Big week ahead"])
    asked = []

    async def gj(prompt, search=False, max_tokens=4000):
        asked.append(prompt)
        return '[{"i": 2, "asset": "SOL", "direction": "long"}]'

    s._gemini_json = gj
    items, method = asyncio.run(s._title_calls(vids))
    assert method == "Gemini" and items[0]["asset"] == "SOL" and items[0]["published"] == vids[1]["published"]
    assert "2. Big week ahead" in asked[0]

    async def broken(prompt, search=False, max_tokens=4000):
        raise RuntimeError("Gemini 429")

    s._gemini_json, s._gemini_left = broken, 4
    items, method = asyncio.run(s._title_calls(titled(["ETH to the moon"])))
    assert method == "по словам в названиях" and items[0]["asset"] == "ETH"
    s._gemini_json, s._gemini_left = gj, 0  # лимит прохода исчерпан — тоже по словам
    assert asyncio.run(s._title_calls(vids))[1] == "по словам в названиях"


def test_snowball_from_star_traders():
    mt, yt, s = setup_scout()
    star = mt._add("youtube", uc(40), "Звезда")
    star["status"] = "star"
    yt.videos[uc(40)] = [{**videos(1)[0], "description": "коллега: https://youtube.com/channel/" + uc(41) +
                          " и стрим twitch.tv/solsniper"}]
    add_feed(yt, uc(41), videos())
    asyncio.run(s.discover(NOW))
    a, b = mt.st["sources"].get(f"yt:{uc(41)}"), mt.st["sources"].get("tw:solsniper")
    assert a and b and a["via"] == sc.VIA_SNOWBALL and "Звезда" in a["note"]


def test_soft_rejects_expire_hard_stay():
    mt, yt, s = setup_scout(None, [{"channel_id": uc(42), "name": "Уснул", "subs": 90_000},
                                   {"channel_id": uc(43), "name": "Накрутка", "subs": 3_000_000}])
    add_feed(yt, uc(42), videos(age_days=40))
    add_feed(yt, uc(43), videos(views=50))
    asyncio.run(s.discover(NOW))
    rej = s.st["rejected"]
    assert rej[f"yt:{uc(42)}"]["until"] and rej[f"yt:{uc(43)}"]["until"] is None
    later = NOW + (sc.SOFT_REJECT_DAYS + 1) * DAY
    known = s.known_ids(later)
    assert f"yt:{uc(42)}" not in known and f"yt:{uc(43)}" in known


def test_early_drop_and_learning():
    mt, yt, s = setup_scout()
    src = trial_src(mt, s, ident=uc(44), rets=[-9.0, -7.0, -8.0, -6.5, -10.0])
    s.review(NOW)
    assert src["removed"] and "уже на 5 проверках" in s.st["rejected"][src["id"]]["why"]
    assert s.st["rejected"][src["id"]]["until"] is None
    assert s.st["via_stats"]["тест"] == {"taken": 1, "kept": 0, "dropped": 1}
    good = trial_src(mt, s, ident=uc(45), status="star", rets=[4.0] * 8)
    s.review(NOW)
    assert not good["removed"] and s.st["via_stats"]["тест"]["kept"] == 1
    assert s.via_rate("тест") == round((1 + 1) / (2 + 3), 10)
    assert "Звезда" not in s._learned_hint() and "Испытуемый" in s._learned_hint()


def test_leaderboard_tiers_and_refresh_history():
    mt, yt, s = setup_scout()
    lucky = mt._add("youtube", uc(46), "Везунчик")
    lucky["rets"] = [30.0, 25.0, 20.0]
    steady = mt._add("youtube", uc(47), "Стабильный")
    steady["rets"] = [3.0] * 10
    hist_only = mt._add("youtube", uc(48), "Только история")
    hist_only["history"] = {"n": 6, "wins": 4, "avg": 3.0, "shrunk": 1.6, "bull_share": 0.5, "rows": [], "at": NOW}
    loser = mt._add("youtube", uc(49), "Доказанно теряет")
    loser["rets"] = [-4.0] * 9
    board = [x["name"] for x in s.leaderboard()]
    assert board.index("Стабильный") < board.index("Везунчик") < board.index("Только история") < board.index("Доказанно теряет")
    assert "Стабильный" in s.rank_text() and "по $10 на идею" in s.rank_text()
    start = NOW - 40 * DAY
    with_candles(mt, {"ETH": [100 - i for i in range(45)]}, start)
    s._gemini_ready = lambda: False
    add_feed(yt, uc(47), titled(["Ethereum dump coming"] * 5))
    for x in mt.active("youtube"):
        x["history"] = x.get("history") or {"at": NOW}
    steady["history"] = None
    assert asyncio.run(s.refresh_history(NOW)) == 1  # по одному за тик и только устаревшие
    assert steady["history"]["n"] >= 4 and steady["history"]["avg"] > 0
    assert asyncio.run(s.refresh_history(NOW + 3600)) == 0


def test_telegram_rank_and_history(bot_env):
    b, session = bot_env
    calls = []

    async def fake_refresh(now=None, force=False, limit=1):
        calls.append((force, limit))
        return 2

    b.mentors.scout.refresh_history = fake_refresh
    b.mentors.candles_of = lambda coin, since: asyncio.sleep(0, [])

    async def run():
        await press(b, "mn:srank")
        assert "Рейтинг трейдеров" in session.sent[-1][1]
        await press(b, "mn:shist")
        assert calls == [(True, 20)] and "Посчитал историю 2" in session.sent[-1][1]
        assert not b.mentors.scout.busy

    asyncio.run(run())


def test_public_candles_picks_first_exchange_with_coin():
    from types import SimpleNamespace
    from bot import extras_ui

    class Ex:
        def __init__(self, markets, rows=None, fail=False):
            self.markets, self.rows, self.fail, self.asked = markets, rows or [], fail, []

        async def fetch_ohlcv(self, symbol, tf, since=None, limit=None):
            self.asked.append((symbol, tf, since))
            if self.fail:
                raise RuntimeError("403")
            return self.rows

    async def ensure(name, ex):
        return None

    bybit = Ex({"ETH/USDT": {}}, fail=True)
    bingx = Ex({"ETH/USDT": {}}, [[1000, 1, 2, 0.5, 1.5, 9], [2000, 0, 0, 0, 0, 0]])
    b = SimpleNamespace(settings=SimpleNamespace(quote="USDT"),
                        scanner=SimpleNamespace(exchanges={"bybit": bybit, "bingx": bingx}, _ensure_markets=ensure))
    rows = asyncio.run(extras_ui.public_candles(b, "ETH", 123))
    assert rows == [[1000.0, 1.0, 2.0, 0.5, 1.5, 9.0]]  # пустая свеча отброшена, Bybit с ошибкой пропущен
    assert bingx.asked == [("ETH/USDT", "1d", 123)]
    assert asyncio.run(extras_ui.public_candles(b, "NOPE", 0)) == []


def test_fit_keeps_whole_lines():
    lines = ["<b>заголовок</b>"] + [f"<b>строка {i}</b> " + "x" * 90 for i in range(100)]
    out = sc.fit(lines)
    assert len(out) <= 4000 and out.count("<b>") == out.count("</b>")
