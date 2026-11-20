"""«💡 Как заработать сейчас»: акции Bybit, проценты Earn, новости и уведомления."""

import asyncio
import time

from bot import opportunities as op
from bot.opportunities import OpportunityHub, classify, coins_in, for_ai, parse_announcements, parse_earn, parse_rss
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import send
from tests import test_webapp

app_env = test_webapp.app_env  # фикстура Mini App

NOW = time.time()
MS = lambda t: int(t * 1000)  # noqa: E731


def ann(title, key="latest_activities", tags=(), start=NOW - 3600, end=NOW + 5 * 86400, url=None):
    return {"title": title, "description": "", "type": {"key": key, "title": key}, "tags": list(tags),
            "url": url or "https://announcements.bybit.com/a/" + str(abs(hash(title))),
            "dateTimestamp": MS(start), "publishTime": MS(start),
            "startDateTimestamp": MS(start), "endDateTimestamp": MS(end)}


ANNS = {"retCode": 0, "result": {"total": 6, "list": [
    ann("Bybit Launchpool: Stake USDT and MNT to Earn XYZ (XYZ)!"),
    ann("Token Splash: Trade ABC to Share 2,000,000 ABC (ABC)"),
    ann("WSOT 2026: Trading Competition with 10,000,000 USDT Prize Pool"),
    ann("New Listing: Fresh Coin (FRSH) — Spot Trading", key="new_crypto", tags=["Spot", "Spot Listings"],
        end=NOW - 1800),  # «конец» листинга в прошлом — его всё равно показываем
    ann("Delisting of OLD (OLD) Spot Trading Pairs", key="delistings", tags=["Spot"]),
    ann("Old finished Launchpool (ZZZ)", end=NOW - 60),
]}}
EARN = {"retCode": 0, "result": {"list": [
    {"category": "FlexibleSaving", "estimateApr": "4.5%", "coin": "USDT", "minStakeAmount": "1", "maxStakeAmount": "1000000",
     "productId": "1", "status": "Available"},
    {"category": "FlexibleSaving", "estimateApr": "8%", "coin": "USDT", "minStakeAmount": "1", "maxStakeAmount": "500",
     "productId": "2", "status": "Available"},
    {"category": "FlexibleSaving", "estimateApr": "0.5%", "coin": "SHIB", "status": "Available"},
    {"category": "FlexibleSaving", "estimateApr": "30%", "coin": "NEWT", "status": "Available"},
    {"category": "FlexibleSaving", "estimateApr": "9%", "coin": "BTC", "status": "NotAvailable"},
]}}
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Биткоин обновил максимум, Solana растёт</title><link>https://forklog.com/news/1</link>
<pubDate>Thu, 19 Nov 2026 10:00:00 +0000</pubDate></item>
<item><title>SEC approves new PEPE ETF?</title><link>https://forklog.com/news/2</link>
<pubDate>Thu, 19 Nov 2026 09:00:00 +0000</pubDate></item>
</channel></rss>"""


def test_classify_and_fit():
    kinds = [p.kind for p in parse_announcements(ANNS, NOW)]
    assert kinds == ["launchpool", "splash", "competition", "listing", "delisting"]  # закончившаяся акция скрыта
    assert classify("Earn up to 555% APR on USDT Flexible Savings") == "earn"
    assert classify("New perpetual contract: XYZUSDT", "new_crypto", ["Derivatives"]) == "futures"
    assert op.KINDS["launchpool"][3] == "yes" and op.KINDS["competition"][3] == "no"
    assert parse_announcements(ANNS, NOW)[0].coins == ["XYZ"]


def test_earn_keeps_best_product_and_majors():
    rows = parse_earn(EARN)
    usdt = [r for r in rows if r["coin"] == "USDT"]
    assert len(usdt) == 1 and usdt[0]["apr"] == 8.0
    coins = {r["coin"] for r in rows}
    assert "NEWT" in coins and "SHIB" not in coins and "BTC" not in coins  # недоступное не показываем


def test_news_rss_and_coins():
    items = parse_rss(RSS, "ForkLog", known={"PEPE", "SOL", "BTC"})
    assert items[0]["coins"] == ["BTC", "SOL"] and items[1]["coins"] == ["PEPE"]
    assert parse_rss("<broken", "x") == []
    assert coins_in("AI CEO talks about ETF") == []


def test_fresh_alerts_do_not_spam_on_start():
    hub = OpportunityHub()
    items = parse_announcements(ANNS, NOW)
    assert hub.fresh(items) == []  # первый раз только запоминаем
    extra = parse_announcements({"result": {"list": [ann("Bybit Launchpool: new (NEW)")]}}, NOW)
    assert [p.kind for p in hub.fresh(items + extra)] == ["launchpool"]
    assert hub.fresh(items + extra) == []


def fake_hub(monkeypatch, hub):
    async def get_json(url, params):
        if "announcements" in url:
            return ANNS
        if params.get("category") == "OnChain":
            return {"result": {"list": [{"category": "OnChain", "estimateApr": "3%", "coin": "USDT", "status": "Available"},
                                        {"category": "OnChain", "estimateApr": "6%", "coin": "SOL", "status": "Available"}]}}
        return EARN
    monkeypatch.setattr(hub, "_get_json", get_json)

    async def news(known=None, force=False):
        return parse_rss(RSS, "ForkLog", known)
    monkeypatch.setattr(hub, "news", news)


def test_earn_command_shows_everything(bot_env, monkeypatch):
    b, session = bot_env
    fake_hub(monkeypatch, b.opps)
    b.trader.balance_cache["bybit"] = (time.time(), [("OLD", 3.0, 3.0, 1.2), ("USDT", 5.0, 5.0, 5.0)])

    async def flow():
        await send(b, "/earn")
        text = session.sent[-1][1]
        assert "Как заработать сейчас" in text and "уберёт монету, которая у тебя есть" in text
        assert "Launchpool" in text and "до 8.00% годовых" in text and "Биткоин" in text
        assert {"op:promos", "op:earn", "op:news", "op:plan", "op:notify"} <= set(buttons(session.markups[-1]))
        await press(b, "op:promos")
        t = session.sent[-1][1]
        assert t.index("Launchpool") < t.index("Соревнование") and "Что делать" in t
        await press(b, "op:earn")
        assert "≈ $0.033 в месяц" in session.sent[-1][1]  # 5 * 8% / 12
        await press(b, "op:notify")
        assert b.opps_notify is False

    asyncio.run(flow())
    ctx = for_ai(b.opps_last)
    assert ctx["delistings_user_holds"] and ctx["bybit_promos_now"][0]["fits_5usd"] == "yes"
    assert "earning_opportunities_now" in b.ai_context()


def test_loop_alerts_new_promo_and_held_delisting(bot_env, monkeypatch):
    b, session = bot_env
    fake_hub(monkeypatch, b.opps)
    asyncio.run(op_once(b))  # первый проход: только запоминает
    n = len(session.sent)
    ANNS["result"]["list"].append(ann("Bybit Launchpool: Stake USDT to Earn NEW (NEW)"))
    ANNS["result"]["list"].append(ann("Delisting of MINE (MINE)", key="delistings"))
    b.trader.balance_cache["bybit"] = (time.time(), [("MINE", 1.0, 1.0, 2.0)])
    b.opps_notify = False
    try:
        asyncio.run(op_once(b))
        assert len(session.sent) == n + 1 and "MINE" in session.sent[-1][1]  # делистинг — всегда, акции — выключены
    finally:
        del ANNS["result"]["list"][-2:]


def op_once(b):
    from bot.opps_ui import opps_once
    return opps_once(b)



def test_webapp_opps_api(app_env, monkeypatch):
    from tests.test_webapp import FRIEND, call, get, post

    b, _ = app_env
    fake_hub(monkeypatch, b.opps)

    async def flow(client):
        st, r = await get(client, "/api/opps")
        assert st == 200 and r["promos"][0]["kind"] == "launchpool" and r["earn"][0]["coin"] == "USDT"
        assert [e["coin"] for e in r["earn"]].count("USDT") == 1 and r["earn"][0]["apr"] == 8.0  # лучший из двух
        assert any(e["coin"] == "SOL" and e["category"] == "OnChain" for e in r["earn"])
        assert "Launchpool" in r["plan_q"] and r["notify"] is True
        st, _ = await post(client, "/api/opps/notify", {"on": False}, uid=FRIEND)
        assert st == 403  # переключать может только владелец
        st, r = await post(client, "/api/opps/notify", {"on": False})
        assert st == 200 and r["notify"] is False

    call(b, flow)
    assert b.opps_notify is False
