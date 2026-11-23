"""💰 Профит-центр: честная статистика, план денег, Страж прибыли, Telegram и Mini App."""

import asyncio
import time

from bot import profit as pf
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import send
from tests.test_webapp import FRIEND, call, get, post

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

NOW = time.time()


def scalp_trade(b, pct, live=True, at=None, cost=5.0):
    pnl = cost * pct / 100
    b.trader.state.log.append({"at": at or NOW - 3600, "exchange": "a", "kind": "sell", "what": "BTC/USDT", "live": live,
                               "spent": 0.0001, "spent_ccy": "BTC", "got": cost + pnl, "got_ccy": "USDT",
                               "usd_value": cost + pnl, "pnl_usd": pnl, "source": "scalp", "ok": True, "note": ""})


# ---------------------------------------------------------------------------
# Статистика и план
# ---------------------------------------------------------------------------

def test_edge_verdicts_are_honest():
    assert pf.edge_stats([])["verdict"] == "none"
    assert pf.edge_stats([5, 5, 5])["verdict"] == "data"  # 3 удачные сделки — ещё ничего не доказано
    proven = pf.edge_stats([1.0, 0.6, -0.4, 0.9, 0.8, 1.1, -0.2, 0.7] * 2)
    assert proven["verdict"] == "proven" and proven["lcb"] > 0 and proven["n"] == 16
    assert pf.edge_stats([-1.0, -0.5, 0.2, -0.8, -1.2, -0.3, 0.1, -0.9, -0.6])["verdict"] == "losing"
    lucky = pf.edge_stats([30, -10, -12, 25, -9, -11, -8, 2])  # один-два больших выигрыша — может быть везение
    assert lucky["verdict"] in ("promising", "flat")
    # поправка на реальность: демо-мемкоины с +1,5% за сделку после −2% — уже минус
    assert pf.edge_stats([1.5] * 10 + [1.4] * 6, haircut=2.0)["verdict"] == "losing"
    # судим по последним 40 сделкам
    assert pf.edge_stats([-5] * 50 + [1] * 40)["verdict"] == "proven"


def test_kelly_and_growth():
    assert pf.kelly_fraction(-1, 2) == 0 and pf.kelly_fraction(1, 0) == 0
    assert 0 < pf.kelly_fraction(0.5, 2) <= 10
    assert round(pf.grow(5, 1, 30), 2) == round(5 * 1.01 ** 30, 2)


def _eng(eid, route, rets, kind="demo", market="cex", per_day_cap=4):
    return pf._engine(eid, "•", eid, kind, market, "/x", rets, NOW - 5 * 86400, NOW, route, 1.0, per_day_cap, True)


def test_plan_puts_money_only_where_plus_is_proven():
    nothing = pf.plan([_eng("lab", "pilot", [0.1, -0.2, 0.05])], 5, apr=6.0)
    assert [r["route"] for r in nothing["rows"]] == ["earn"] and not nothing["proven"]
    assert "6.0% годовых" in nothing["rows"][0]["why"]

    good = [0.9, 0.7, -0.3, 0.8, 1.0, 0.6, -0.2, 0.9] * 2
    p5 = pf.plan([_eng("lab", "pilot", good)], 5)
    assert p5["rows"][0]["route"] == "pilot" and p5["rows"][0]["usd"] == 5 and p5["proven"]
    assert p5["d30"] > 5 and p5["low30"] <= p5["d30"]

    p100 = pf.plan([_eng("lab", "pilot", good), _eng("meme_demo", "meme", [-5, -3, -8, -4, -2, -6, -1, -7, -3],
                                                       market="sol")], 100, apr=5)
    routes = {r["route"]: r["usd"] for r in p100["rows"]}
    assert "meme" not in routes  # доказанный минус денег не получает
    assert routes["pilot"] == 5 and routes["earn"] == 95  # плюс только на демо → сначала минимальный ордер
    assert "начни с минимума" in p100["rows"][0]["why"]
    real_good = pf.plan([_eng("pilot_real", "pilot", good, kind="real")], 100, apr=5)
    routes = {r["route"]: r["usd"] for r in real_good["rows"]}
    assert 5 < routes["pilot"] <= 60 and routes["earn"] >= 40  # подтвердили настоящие сделки → по Келли, ≤ 60%
    assert abs(sum(routes.values()) - 100) < 0.05

    # настоящие сделки (от 8) важнее демо, даже если демо выглядит лучше
    real_bad = _eng("pilot_real", "pilot", [-0.5, -0.4, 0.1, -0.6, -0.3, -0.2, -0.7, -0.4], kind="real")
    ev = pf.route_evidence([_eng("lab", "pilot", good), real_bad])
    assert ev["pilot"]["id"] == "pilot_real"
    assert pf.plan([_eng("lab", "pilot", good), real_bad], 50)["rows"][0]["route"] == "earn"


# ---------------------------------------------------------------------------
# Сбор из бота и Страж
# ---------------------------------------------------------------------------

def test_collect_from_all_parts(bot_env):
    b, _ = bot_env
    assert b.profit.engines() == []
    assert "Сделок пока нет" in b.profit.text()
    scalp_trade(b, 0.8, live=False)
    b.copy.st["closed"].append({"symbol": "X", "ret": 12.0, "why": "tp", "passed": True, "closed": NOW - 7200,
                                "mistakes": []})
    b.meme.demo["closed"].append({"symbol": "Y", "pct": -4.0, "pnl_usd": -0.2, "closed": NOW - 100})
    b.scalper.lab["stats"]["a|BTC/USDT|dip"] = {"rets": [0.5, -0.3], "sum": 0.2, "n": 2, "wins": 1, "since": NOW - 86400}
    ids = {e["id"] for e in b.profit.engines()}
    assert ids == {"pilot_test", "copy", "meme_demo", "lab"}
    text = b.profit.text(20)
    assert "Профит-центр" in text and "План для $20" in text and "Страж прибыли" in text
    assert "/profit" in b.profit.short_line()
    ctx = b.ai_context()["profit_center"]
    assert ctx["plan_for_usd"] == 5.0 and len(ctx["engines"]) == 4


def test_guard_pauses_losing_real_autopilot_and_resumes(bot_env):
    b, _ = bot_env
    for _ in range(7):
        scalp_trade(b, -1.5)
    assert b.profit.check(NOW) == []  # 7 сделок — ещё рано судить
    scalp_trade(b, 0.3)
    scalp_trade(b, -1.2)
    events = b.profit.check(NOW)
    assert any("Страж прибыли" in e["text"] for e in events)
    assert ("▶️ Снять паузу", "pf:resume:pilot") in events[0]["buttons"]
    sc = b.scalper.st
    assert sc["pause"] and all(until > NOW + 2 * 86400 for until in sc["pause"].values())
    assert any("Страж" in r["reason"] for r in b.scalper.shield_rows(NOW))
    assert not [e for e in b.profit.check(NOW) if "Страж" in e["text"]]  # не повторяет без новых сделок
    assert "Пауза снята" in b.profit.resume("pilot", NOW)
    assert all(v == 0 for v in sc["pause"].values()) and not sc["pause_why"]
    for _ in range(4):
        scalp_trade(b, -1.0)
    assert not [e for e in b.profit.check(NOW) if "Страж" in e["text"]]
    scalp_trade(b, -1.0)  # 5-я новая сделка — снова доказанный минус
    assert [e for e in b.profit.check(NOW) if "Страж" in e["text"]]


def test_guard_off_does_nothing(bot_env):
    b, _ = bot_env
    b.profit.st["guard"] = False
    for _ in range(10):
        scalp_trade(b, -2)
    b.profit.check(NOW)
    assert not any(v > NOW for v in b.scalper.st["pause"].values())


def test_guard_pauses_real_memecoins(bot_env):
    b, _ = bot_env
    for i in range(9):
        b.meme.st["closed"].append({"symbol": f"T{i}", "pct": -12.0 if i % 3 else 4.0, "pnl_usd": -0.3,
                                    "closed": NOW - 3600})
    events = b.profit.check(NOW)
    assert any("Мемкоины" in e["text"] for e in events)
    assert b.meme.st["paused_until"] > NOW + 2 * 86400
    b.profit.resume("meme", NOW)
    assert b.meme.st["paused_until"] == 0


def test_guard_mutes_losing_copy_signals_and_milestones(bot_env):
    b, _ = bot_env
    for i in range(10):
        b.copy.st["closed"].append({"symbol": f"T{i}", "ret": -6.0 - i % 3, "why": "stop", "passed": True,
                                    "closed": NOW - 3600, "mistakes": []})
    assert b.copy.st["signals"]
    events = b.profit.check(NOW)
    assert not b.copy.st["signals"] and b.profit.st["muted"]["copy"]
    assert any("pf:unmute:copy" in [u for _, u in e["buttons"]] for e in events)
    assert any("демо доказало минус" in e["text"] for e in events)  # первое знакомство с минусом — сообщаем
    b.profit.unmute("copy")
    assert b.copy.st["signals"]
    # демо мемкоинов доказало плюс → совет попробовать настоящие деньги
    for i in range(16):
        b.meme.demo["closed"].append({"symbol": f"M{i}", "pct": 9.0 if i % 4 else -3.0, "pnl_usd": 0.4,
                                      "closed": NOW - 86400})
    events = b.profit.check(NOW)
    proven = [e for e in events if "доказал плюс" in e["text"]]
    assert proven and "/meme" in proven[0]["text"]
    assert not [e for e in b.profit.check(NOW) if "доказал плюс" in e["text"]]  # один раз


def test_scale_hint_after_20_real_proven_trades(bot_env):
    b, _ = bot_env
    for i in range(22):
        scalp_trade(b, 0.9 if i % 5 else -0.4)
    d = b.profit.status_dict(50)
    assert d["scale"] and "в 2 раза" in d["scale"]
    assert d["plan"]["rows"][0]["route"] == "pilot"


def test_state_survives_restore(bot_env):
    b, _ = bot_env
    b.profit.st["guard"] = False
    b.profit.st["amount"] = 50.0
    raw = b.copy.export()
    b.copy.restore(raw)
    assert b.profit.st["guard"] is False and b.profit.st["amount"] == 50.0


# ---------------------------------------------------------------------------
# Telegram и Mini App
# ---------------------------------------------------------------------------

def test_telegram_profit_flow(bot_env):
    b, session = bot_env

    async def run():
        await send(b, "/profit")
        assert "Профит-центр" in session.sent[-1][1] and "План для $5" in session.sent[-1][1]
        await send(b, "/profit 50")
        assert "План для $50" in session.sent[-1][1]
        await press(b, "pf:plan:20")
        assert "План для $20" in session.sent[-1][1] and b.profit.st["amount"] == 20
        await press(b, "pf:guard")
        assert b.profit.st["guard"] is False
        await press(b, "pf:help")
        assert "Келли" in session.sent[-1][1]
        await press(b, "pf:guard", user=FRIEND)  # не владелец не может выключить Стража
        assert b.profit.st["guard"] is False and "только владелец" in session.sent[-1][1]
        await press(b, "pf:ai")  # без ключа ИИ — понятная инструкция
        assert "console.groq.com" in session.sent[-1][1]
        kb = __import__("bot.profit_ui", fromlist=["keyboard"]).keyboard(b)
        assert "pf:plan:100" in buttons(kb) and "pf:ai" in buttons(kb)

    b.allowed_ids.add(FRIEND)
    asyncio.run(run())


def test_webapp_profit_api(app_env):
    b, _ = app_env

    async def fn(client):
        st, ov = await get(client, "/api/overview")
        assert st == 200 and ov["profit"]["count"] == 0 and ov["profit"]["guard"] is True
        st, d = await get(client, "/api/profit?amount=20")
        assert st == 200 and d["plan"]["amount"] == 20 and d["owner"] is True
        st, d = await post(client, "/api/profit", {"guard": False, "amount": 100})
        assert st == 200 and d["guard"] is False and d["plan"]["amount"] == 100
        st, _ = await post(client, "/api/profit", {"guard": True}, uid=FRIEND)
        assert st == 403
        st, d = await get(client, "/api/profit", uid=FRIEND)
        assert st == 200 and d["owner"] is False

    call(b, fn)
