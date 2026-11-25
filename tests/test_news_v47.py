"""v47: 📰 новости → влияние на рынок → обучение: источники, разбор, замер цены, щит, пауза, бумажные сделки, ИИ."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from bot import news as nm
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import send

bot_env = __import__("tests.test_telegram_flow", fromlist=["bot_env"]).bot_env
app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

SRC = {s.key: s for s in nm.SOURCES}


def prepare(b, prices=None):
    """Без сети: источники отдают то, что положил тест; цены — из словаря."""
    nw = b.news
    feed: dict[str, list[dict]] = {}
    px = {"BTC": 50_000.0, "XRP": 1.0, "PEPE": 0.00001, "SOL": 100.0}
    if prices:
        px.update(prices)

    async def fetch(src):
        items = feed.pop(src.key, [])
        if isinstance(items, Exception):
            raise items
        return items

    async def prices_fn(coins, now):
        return {c: px[c] for c in coins if c in px}

    async def side(now):
        return None

    nw.fetch = fetch
    nw._prices = prices_fn
    nw.refresh_side = side
    nw.known = lambda: {"BTC", "ETH", "XRP", "PEPE", "SOL", "SUI", "TON"}
    return nw, feed, px


def item(title, ago=60, **kw):
    return {"title": title, "url": "https://example.com/" + str(abs(hash(title)))[:8],
            "published": time.time() - ago, **kw}


def run(coro):
    return asyncio.run(coro)


# --- разбор источников -----------------------------------------------------------------------------------

def test_parsers_rss_atom_and_exchange_json():
    rss = """<rss><channel><item><title>SEC sues Ripple over XRP</title><link>https://a/1</link>
             <pubDate>Wed, 18 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Bitcoin ETF inflows hit record</title>
              <link href="https://r/2"/><updated>2026-11-18T10:00:00+00:00</updated></entry></feed>"""
    a, b = nm.parse_feed(rss), nm.parse_feed(atom)
    assert a[0]["title"] == "SEC sues Ripple over XRP" and a[0]["url"] == "https://a/1" and a[0]["published"]
    assert b[0]["url"] == "https://r/2" and b[0]["published"]
    assert nm.parse_feed("<<не xml") == []
    assert nm.clean_title("Fed cuts rates - Reuters") == "Fed cuts rates"
    bn = nm.parse_binance({"data": {"catalogs": [{"articles": [{"title": "Binance Will List Foo (FOO)", "code": "abc",
                                                                 "releaseDate": 1763460000000}]}]}})
    assert bn[0]["url"].endswith("/abc") and bn[0]["published"] == 1763460000.0
    okx = nm.parse_okx({"data": [{"details": [{"title": "OKX to list BAR", "url": "https://okx/1",
                                               "pTime": "1763460000000"}]}]})
    assert okx[0]["title"] == "OKX to list BAR"
    up = nm.parse_upbit({"data": {"notices": [{"id": 5, "title": "[거래] 신규 거래 지원 안내 (SUI)",
                                               "first_listed_at": "2026-11-18T10:00:00+09:00"}]}})
    assert up[0]["url"].endswith("id=5") and up[0]["published"]
    cp = nm.parse_cryptopanic({"results": [{"title": "X", "published_at": "2026-11-18T10:00:00Z",
                                            "currencies": [{"code": "sol"}]}]})
    assert cp[0]["coins"] == ["SOL"]
    now = 1763460000.0
    mac = nm.parse_macro([{"title": "CPI m/m", "country": "USD", "impact": "High", "date": "2026-11-18T08:30:00-05:00"},
                          {"title": "German ZEW", "country": "EUR", "impact": "High", "date": "2026-11-18T05:00:00-05:00"},
                          {"title": "Crude Oil", "country": "USD", "impact": "Medium", "date": "2026-11-18T10:30:00-05:00"}],
                         now)
    assert len(mac) == 1 and mac[0]["ru"] == "инфляция в США (CPI)"


def test_classify_picks_the_strongest_meaning():
    assert nm.classify("Binance will delist FOO and BAR")[0] == "delist"
    assert nm.classify("Binance Will List Foo (FOO)")[0] == "listing_major"
    assert nm.classify("Some exchange lists FOO")[0] == "listing"
    assert nm.classify("Hackers drain $40M from bridge")[0] == "hack"
    assert nm.classify("Bitcoin jumps after Fed rate cut")[0] == "dovish"
    assert nm.classify("SEC sues Ripple")[0] == "lawsuit"
    assert nm.classify("Взлом биржи: украдено $20 млн")[0] == "hack"
    assert nm.classify("Weather is nice today") == ("other", 0.0)
    cat, prior = nm.classify("Exchange denies hack rumors")
    assert cat == "hack" and -2 < prior < 0  # опровержение — слабее
    # «sui» не находится в «lawsuit», «tron» — в «electronic»
    assert "SUI" not in nm.find_coins("Big lawsuit against electronic company", {"SUI", "TRX"})
    assert nm.find_coins("Solana and $PEPE rally", {"SOL", "PEPE"}) == ["SOL", "PEPE"]


# --- сбор, склейка, память ---------------------------------------------------------------------------------

def test_first_collect_only_remembers_then_new_news_are_events_and_duplicates_merge(bot_env):
    b, _ = bot_env
    nw, feed, _ = prepare(b)
    feed["ctg"] = [item("Old hack drains XRP bridge")]
    assert run(nw.tick()) == []
    assert all(e["old"] for e in nw.st["events"]) and not nw.st["first"]
    feed["ctg"] = [item("Hackers drain $40M from XRP bridge exploit")]
    feed["cdk"] = [item("XRP bridge exploit: hackers drain $40M")]
    feed["dcr"] = [item("Weather report for crypto traders")]
    run(nw.tick(now=time.time() + 700))
    fresh = [e for e in nw.st["events"] if not e["old"]]
    assert len(fresh) == 1 and set(fresh[0]["srcs"]) == {"ctg", "cdk"} and fresh[0]["coins"] == ["XRP"]
    assert fresh[0]["p0"]["XRP"] == 1.0 and fresh[0]["p0"]["BTC"] == 50_000.0
    assert any("Weather" in h["title"] for h in nw.headlines)  # без монеты и типа — только для контекста
    assert len(nw.st["seen"]) == 4


def test_broken_source_backs_off_and_does_not_stop_others(bot_env):
    b, _ = bot_env
    nw, feed, _ = prepare(b)
    now = time.time()
    feed["ctg"] = RuntimeError("ответ 503")
    feed["cdk"] = [item("SEC sues XRP issuer")]
    run(nw.collect(now))
    h = nw.health["ctg"]
    assert h["fail"] == 1 and "503" in h["err"] and h["next"] > now + 600
    assert nw.health["cdk"]["fail"] == 0
    assert "❌ Cointelegraph" in nw.src_text() and "CRYPTOPANIC_TOKEN" in nw.src_text()
    ok, total = nw.working()
    assert ok >= 1 and total >= len(nm.SOURCES)


def test_memory_stays_bounded(bot_env):
    b, _ = bot_env
    nw, feed, _ = prepare(b)
    nw.st["first"] = False
    now = time.time()
    items = [(SRC["ctg"], item(f"SEC sues company number {i} {'x' * (i % 7)} over XRP token {i * 7919}"))
             for i in range(nm.EVENTS_KEEP + 60)]
    for i, it in enumerate(items):
        it[1]["title"] = f"Lawsuit {i}: regulator charged firm {i} with XRP fraud case {i * 31}"
    nw.ingest(items, now)
    assert len(nw.st["events"]) <= nm.EVENTS_KEEP
    nw.st["seen"] = [str(i) for i in range(nm.SEEN_KEEP + 300)]
    nw.ingest([(SRC["ctg"], item("Another unique headline about SOL upgrade mainnet"))], now)
    assert len(nw.st["seen"]) <= nm.SEEN_KEEP


# --- замер и обучение ----------------------------------------------------------------------------------------

def test_measures_move_relative_to_btc_and_learns(bot_env):
    b, _ = bot_env
    nw, feed, px = prepare(b)
    nw.st["first"] = False
    t0 = time.time()
    feed["ctg"] = [item("SEC sues XRP issuer for fraud")]
    run(nw.tick(now=t0))
    ev = nw.st["events"][-1]
    assert ev["cat"] == "lawsuit" and ev["scope"] == "coin"
    px["XRP"], px["BTC"] = 0.95, 51_000.0  # XRP −5%, рынок +2% → относительно рынка −7%
    run(nw.measure([], t0 + 4 * 3600 + 5))
    assert set(ev["res"]) == {"15m", "1h", "4h"}
    rel, raw = ev["res"]["4h"]["XRP"]
    assert abs(raw + 5) < 1e-6 and abs(rel + 7) < 1e-6
    val, n, up = nw.est("lawsuit", "4h")
    assert n == 1 and up == 0 and val < nm.CATS["lawsuit"][2]  # факт хуже стартового ожидания
    assert nw.st["shield"] == {"n": 1, "right": 1}
    assert nw.st["measured"] == 3
    assert "иск / обвинения" in nw.learn_text()


def test_learning_can_turn_the_shield_off(bot_env):
    b, _ = bot_env
    nw, _, _ = prepare(b)
    now = time.time()
    ev = {"id": "x", "t": now - 60, "seen": now - 60, "title": "SEC sues PEPE team", "url": "", "src": "ctg",
          "srcs": ["ctg"], "tier": 1, "cat": "lawsuit", "prior": -2.0, "coins": ["PEPE"], "scope": "coin", "old": False,
          "p0": {}, "res": {}, "imp": 2}
    nw.st["events"].append(ev)
    assert "PEPE" in nw.risk(now)
    for _ in range(20):  # по фактам иски монету не роняют
        nw._learn(ev, "4h", 0.5)
    assert nw.est("lawsuit")[0] > nm.SHIELD_PCT and "PEPE" not in nw.risk(now)


# --- решения: щит, пауза, шторм -------------------------------------------------------------------------

def test_shield_reaches_autopilot_spread_and_transfers(bot_env):
    b, _ = bot_env
    nw, feed, _ = prepare(b)
    nw.st["first"] = False
    feed["ctg"] = [item("Hackers drain $40M in XRP exploit")]
    feed["cdk"] = [item("Bitcoin hack fears: BTC exchange breach")]
    run(nw.tick())
    risk = nw.risk()
    assert "XRP" in risk and "BTC" not in risk  # BTC/ETH — через настроение рынка, не через щит монеты
    assert "взлом" in b.scalper.shield_reason("XRP")
    assert any(r["kind"] == "news" and r["coin"] == "XRP" for r in b.scalper.shield_rows())
    from tests.test_spread_v46 import opp, result
    from tests.test_spread_v43 import setup
    setup(b)
    cands = b.spread.candidates(result(opp("XRP"), opp("SOL")))
    assert [o.symbol for o in cands] == ["SOL/USDT"] and b.spread.skipped["news"] == 1
    assert "тревожные новости" in b.spread.panel_text()
    nw.st["on"] = False  # выключенные новости снимают щит
    nw.apply()
    assert b.scalper.shield_reason("XRP") is None


def test_macro_window_pauses_new_buys_and_spread(bot_env):
    b, _ = bot_env
    nw, _, _ = prepare(b)
    now = time.time()
    nw.macro = [{"id": "m1", "at": now + 600, "title": "Federal Funds Rate", "ru": "решение ФРС по ставке",
                 "forecast": "4.25%", "previous": "4.50%"}]
    p = nw.pause(now)
    assert p and p[0] == now + 600 + nm.MACRO_AFTER and "ФРС" in p[1]
    nw.apply(now)
    assert b.scalper.news_pause(now) and any(r["kind"] == "news" for r in b.scalper.shield_rows(now))
    assert b.spread.news_pause(now)
    msgs = nw._alerts([], None, now)
    assert any("Через 10 мин" in m and "ФРС" in m for m in msgs)
    assert nw._alerts([], None, now) == []  # про одно событие — один раз
    assert nw.pause(now + 3 * 3600) is None
    from tests.test_spread_v46 import opp, result
    from tests.test_spread_v43 import setup
    setup(b)
    b.spread.st["on"] = True

    async def boom(*a, **k):
        raise AssertionError("в макро-окно спред не должен искать сделки")

    b.spread.evaluate = boom
    run(b.spread.process(result(opp("SOL")), now))
    assert "на паузе" in b.spread.panel_text()


def test_storm_pauses_autopilot_once_with_alert(bot_env):
    b, _ = bot_env
    nw, feed, _ = prepare(b)
    nw.st["first"] = False
    feed["ctg"] = [item("Crypto ban: China bans all crypto trading again")]
    feed["cdk"] = [item("Fed signals rate hike, hawkish stance shocks markets")]
    feed["dcr"] = [item("Bitcoin ETF outflows hit record as funds reject crypto")]
    feed["tbk"] = [item("Hotter-than-expected inflation rises again in US")]
    msgs = run(nw.tick())
    score, label, _ = nw.mood()
    assert score <= nm.STORM and "шторм" in label
    assert nw.pause() and "шторм" in nw.pause()[1]
    assert any("шторм" in m for m in msgs)
    assert b.scalper.news_pause()
    feed["bst"] = [item("Another US crackdown: regulators ban crypto ads")]
    msgs2 = run(nw.tick(now=time.time() + 700))
    assert not any("шторм в новостях" in m and "Автопилот 2 ч" in m for m in msgs2)  # шторм продлён, без повтора


# --- бумажные сделки и сигналы ----------------------------------------------------------------------------

def test_paper_trade_on_good_news_and_demo_row(bot_env):
    b, _ = bot_env
    nw, feed, px = prepare(b)
    nw.st["first"] = False
    t0 = time.time()
    feed["bnc"] = [item("Binance Will List Sui (SUI)")]
    px["SUI"] = 2.0
    run(nw.tick(now=t0))
    pp = nw.st["paper"]
    assert pp["open"] and pp["open"][0]["coin"] == "SUI"
    px["SUI"] = 2.1  # +5%
    run(nw.measure([], t0 + 4 * 3600 + 1))
    assert not pp["open"] and pp["n"] == 1 and 4.6 < pp["closed"][0]["pct"] < 4.9  # минус комиссии
    from bot import demo_report
    row = next(r for r in demo_report.rows(b) if r["id"] == "news")
    assert row["n"] == 1 and "в плюс 1 из 1" in row["result"]


def test_proven_signal_is_announced(bot_env):
    b, _ = bot_env
    nw, feed, px = prepare(b)
    nw.st["first"] = False
    ev = {"cat": "listing_major", "src": "bnc"}
    for _ in range(15):
        nw._learn(ev, "4h", 4.0)
    assert nw.proven("listing_major")
    feed["bnc"] = [item("Binance Will List Toncoin (TON)")]
    px["TON"] = 5.0
    msgs = run(nw.tick())
    assert any("Доказанный сигнал" in m and "TON" in m for m in msgs)


def test_alerts_respect_daily_limit_and_switch(bot_env):
    b, _ = bot_env
    nw, _, _ = prepare(b)
    now = time.time()
    evs = [{"id": str(i), "t": now, "seen": now, "title": f"Market crash {i}", "src": "ctg", "srcs": ["ctg"],
            "cat": "crash", "coins": [], "scope": "market", "imp": 3, "res": {}, "p0": {}} for i in range(10)]
    assert len(nw._alerts(evs, None, now)) == nm.ALERTS_PER_DAY
    assert nw._alerts(evs, None, now) == []
    nw.st["alerts"] = False
    nw.st["sent_day"] = ["", 0]
    assert nw._alerts(evs, None, now) == []


# --- ИИ ---------------------------------------------------------------------------------------------------

def test_ai_review_fixes_category_translates_and_digests(bot_env):
    b, _ = bot_env
    nw, feed, _ = prepare(b)
    nw.st["first"] = False
    asked = []

    class Client:
        async def chat(self, messages, max_tokens=900, background=False):
            asked.append(messages[-1]["content"])
            return json.dumps({"items": [{"i": 0, "cat": "hack", "coins": ["SOL"], "ru": "Взлом протокола на Solana",
                                          "why": "биржи могут закрыть вывод, цена падает"}],
                               "digest": "Главное сегодня — взлом на Solana."})

    b.ai = SimpleNamespace(configured=True, client=Client())
    feed["ctg"] = [item("Solana protocol incident: funds frozen")]  # правила не поняли — пусть ИИ разберёт
    run(nw.tick())
    ev = nw.st["events"][-1]
    assert asked and ev["ai"] and ev["ru"] == "Взлом протокола на Solana" and ev["cat"] == "hack"
    assert nw.st["digest"]["text"].startswith("Главное") and "Сводка ИИ" in nw.panel_text()
    feed["sec"] = [item("SEC sues big exchange for fraud over SOL")]
    run(nw.tick(now=time.time() + 60))
    assert len(asked) == 1  # не чаще раза в 30 минут


# --- Telegram и приложение ---------------------------------------------------------------------------------

def test_news_commands_and_buttons(bot_env):
    b, session = bot_env
    nw, feed, _ = prepare(b)

    async def flow():
        await send(b, "/news")
        text = session.sent[-1][1]
        assert "Новости → рынок" in text and "Настроение" in text
        assert "nw:refresh" in buttons(session.markups[-1]) and "nw:learn" in buttons(session.markups[-1])
        await send(b, "/news learn")
        assert "Чему бот научился" in session.sent[-1][1]
        await send(b, "/news src")
        assert "Источники новостей" in session.sent[-1][1]
        await send(b, "/news alerts off")
        assert not nw.st["alerts"]
        await press(b, "nw:alerts")
        assert nw.st["alerts"]
        feed["ctg"] = [item("SEC sues XRP issuer")]
        await press(b, "nw:refresh")
        assert "Новости → рынок" in session.sent[-1][1]
        await send(b, "/news off")
        assert not nw.st["on"]
        await send(b, "/news on")
        assert nw.st["on"]
        await send(b, "/news что-то")
        assert "Команды новостей" in session.sent[-1][1]

    run(flow())
    assert "news" in b.ai_context()["extras"]


def test_mini_app_news(app_env):
    from tests.test_webapp import FRIEND, call, get, post
    b, _ = app_env
    nw, feed, _ = prepare(b)

    async def flow(client):
        _, d = await get(client, "/api/news")
        assert "mood_label" in d and d["alerts"] is True and d["sources"]["total"] >= len(nm.SOURCES)
        st, _ = await post(client, "/api/news", {"alerts": False}, uid=FRIEND)
        assert st == 403
        _, d = await post(client, "/api/news", {"alerts": False})
        assert d["alerts"] is False
        feed["ctg"] = [item("Old SEC lawsuit")]
        st, d = await post(client, "/api/news", {"refresh": True}, uid=FRIEND)
        assert st == 200 and d["total"] >= 1

    call(b, flow)


def test_doctor_reports_news(bot_env):
    b, _ = bot_env
    nw, feed, _ = prepare(b)
    from bot.doctor import _automat_checks
    run(nw.tick())
    checks = [c for c in _automat_checks(b, time.time()) if "Новости" in c.title]
    assert checks and "источников работает" in checks[0].text
