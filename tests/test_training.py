"""🏋️ Тренировка на демо: идеи трейдеров → сделки 5 стратегий, выходы по стопу/тейку/сроку с комиссиями,
выбор лучшей стратегии, подбор стопа/тейка по полному пути цены, сброс, Telegram и приложение."""

import asyncio
import time

from bot import training as tr
from bot.training import BEST_MIN, EXIT_MIN_PATHS, VARIANTS, exit_point, net_pct, passes
from tests.test_copytrade import MINT, run
from tests.test_mentors import UC, FakeGemini, FakeYouTube, Prices, make

HOUR = 3600


def call(mt, src, asset="SOL", now=None, horizon_days=1, conf="medium"):
    now = time.time() if now is None else now
    return run(mt._open_call(src, {"asset": asset, "direction": "long", "horizon_days": horizon_days,
                                   "confidence": conf}, now))


def env(now=None, **prices):
    now = time.time() if now is None else now
    p = Prices(SOL=100.0, ETH=3000.0, **prices)
    mt, ct, yt, dex = make(now, prices=p)
    return mt, p


# --- чистые функции --------------------------------------------------------------------------

def test_exit_point_and_costs():
    assert exit_point([0.0, 4.0, -9.0, 20.0], 0.08, None) == (-9.0, "stop")
    assert exit_point([0.0, 4.0, 12.0, -20.0], 0.08, 0.1) == (12.0, "tp")
    assert exit_point([0.0, 3.0, 5.0], 0.08, 0.1) == (5.0, "time")
    assert abs(net_pct(0.0, "cex", 2.5, 0.0015) - (-0.2996)) < 1e-3  # две комиссии ≈ −0,3%
    assert net_pct(0.0, "sol", 2.5, 0.01) < net_pct(0.0, "cex", 2.5, 0.01)  # мемкоин: ещё и сеть Solana
    f = {"status": "watch", "checked": 0, "avg": 0.0, "authors": 1, "sponsored": True, "hype": 8, "conf": "medium"}
    assert [n for n in VARIANTS if passes(n, f)] == ["all"]
    f.update(status="star", authors=2, sponsored=False, hype=2, conf="high")
    assert all(passes(n, f) for n in VARIANTS)


# --- идеи → сделки ---------------------------------------------------------------------------

def test_idea_opens_positions_only_in_strategies_that_trust_it():
    now = time.time()
    mt, _ = env(now)
    a = mt._add("youtube", UC, "Cowen")
    b = mt._add("youtube", "UCqK_GSMbpiV8spgD3ZGloSw", "Bureau")
    c1 = call(mt, a, now=now)
    assert mt.train.on_call(c1, a, {"sponsored": True, "hype": 8}, now) is None  # лучшей стратегии ещё нет
    vs = mt.train.st["variants"]
    assert list(vs["all"]["positions"]) == [c1["id"]] and vs["all"]["cash"] == 7.5  # 25% от $10
    assert not vs["clean"]["positions"] and not vs["trusted"]["positions"] and not vs["consensus"]["positions"]
    # второй автор хвалит ту же монету → «согласие 2+»; без рекламы → «без рекламы и хайпа»; уверен → «уверенные»
    c2 = call(mt, b, now=now + 60, conf="high")
    mt.train.on_call(c2, b, {"sponsored": False, "hype": 3}, now + 60)
    assert c2["id"] in vs["consensus"]["positions"] and c2["id"] in vs["clean"]["positions"]
    assert c2["id"] in vs["confident"]["positions"]
    assert c2["id"] not in vs["all"]["positions"] and vs["all"]["skipped"] == 1  # SOL уже есть у «всех»
    # шорты и «не покупай» на споте не торгуем
    short = run(mt._open_call(a, {"asset": "ETH", "direction": "short", "horizon_days": 1}, now))
    assert mt.train.on_call(short, a, {}, now) is None and len(mt.train.st["track"]) == 2


def test_stop_tp_time_exits_with_costs_and_full_path_tracking():
    now = time.time()
    mt, p = env(now)
    a = mt._add("youtube", UC, "Cowen")
    c = call(mt, a, now=now, horizon_days=2)
    mt.train.on_call(c, a, {}, now)
    v = mt.train.st["variants"]["all"]
    p.p["SOL"] = 103.0
    run(mt.train.update(now + HOUR))
    assert v["positions"][c["id"]]["now"] == 3.0 and 7.5 < mt.train.equity(v) < 10.3
    p.p["SOL"] = 90.0  # −10% < стопа −8%
    run(mt.train.update(now + 2 * HOUR))
    assert not v["positions"] and v["n"] == 1 and v["closed"][-1]["why"] == "stop"
    assert abs(v["rets"][-1] - round(net_pct(-10.0, "cex", 2.5, tr.CEX_SIDE_COST), 2)) < 1e-9
    assert abs(v["cash"] - (7.5 + 2.5 * (1 + v["rets"][-1] / 100))) < 1e-3
    # после выхода цену продолжаем записывать до конца срока — чтобы понять, был ли стоп правильным
    p.p["SOL"] = 115.0
    run(mt.train.update(now + 10 * HOUR))
    assert mt.train.st["track"][c["id"]]["path"][-1] == 15.0
    run(mt.train.update(now + 49 * HOUR))
    assert not mt.train.st["track"] and mt.train.st["paths"][-1]["path"] == [0.0, 3.0, -10.0, 15.0, 15.0]
    # выход по сроку
    c2 = call(mt, a, asset="ETH", now=now + 50 * HOUR)
    mt.train.on_call(c2, a, {}, now + 50 * HOUR)
    p.p["ETH"] = 3060.0
    run(mt.train.update(now + 75 * HOUR))
    assert v["closed"][-1]["why"] == "time" and 1.5 < v["closed"][-1]["pct"] < 2.0


def test_memecoin_idea_from_twitch_uses_dex_price():
    now = time.time()
    mt, ct, yt, dex = make(now)
    mt.seed()
    dex.set_price(MINT, 0.02)
    mt.twitch.handle(f":decu!d@d.tmi.twitch.tv PRIVMSG #decu :{MINT}")
    run(mt.drain_twitch(now))
    v = mt.train.st["variants"]
    assert len(v["all"]["positions"]) == 1 and len(v["confident"]["positions"]) == 1  # стример сам написал адрес
    dex.set_price(MINT, 0.032)  # +60% — выше тейка? тейка нет (по умолчанию), стоп −40% не задет
    run(mt.train.update(now + HOUR))
    pos = next(iter(v["all"]["positions"].values()))
    assert pos["kind"] == "sol" and pos["now"] == 60.0
    dex.set_price(MINT, 0.011)  # −45% → стоп −40%
    run(mt.train.update(now + 2 * HOUR))
    assert v["all"]["closed"][-1]["why"] == "stop" and v["all"]["closed"][-1]["pct"] < -45


# --- обучение ------------------------------------------------------------------------------

def _trade_round(mt, p, src, now, k, end_price, conf="medium", meta=None):
    t = now + k * 30 * HOUR
    p.p["SOL"] = 100.0
    c = call(mt, src, now=t, conf=conf)
    e = mt.train.on_call(c, src, meta or {}, t)
    p.p["SOL"] = end_price
    events = run(mt.train.update(t + 25 * HOUR))
    return e, events


def test_best_strategy_is_chosen_and_only_it_gives_signals():
    now = time.time()
    mt, p = env(now)
    src = mt._add("youtube", UC, "Cowen")
    # уверенные прогнозы сбываются (+6%), обычные — нет (−7%)
    lessons = []
    for k in range(2 * BEST_MIN):
        conf = "high" if k % 2 else "medium"
        _, ev = _trade_round(mt, p, src, now, k, 106.0 if conf == "high" else 93.0, conf=conf)
        lessons += [e["text"] for e in ev]
    st = mt.train.st
    assert st["active"] == "confident"
    assert any("Лучшая стратегия на демо — «Уверенные прогнозы»" in x for x in lessons)
    sc = mt.train.scores()
    assert sc["confident"] > 0 > sc["all"]
    # теперь сигнал приходит только по идеям, которые проходят лучшую стратегию
    t = now + 100 * 30 * HOUR
    p.p["SOL"] = 100.0
    weak = call(mt, src, now=t)
    assert mt.train.on_call(weak, src, {}, t) is None
    strong = call(mt, src, asset="ETH", now=t + 1, conf="high")
    e = mt.train.on_call(strong, src, {}, t + 1)
    assert e and "Сигнал проверенной стратегии" in e["text"] and "Уверенные прогнозы" in e["text"]
    assert "bybit.com/trade/spot/ETH/USDT" in e["buttons"][0][1]
    assert "🏆" in mt.train.text() and "Тренировка" in mt.train.short_line()


def test_no_signals_when_every_strategy_loses():
    now = time.time()
    mt, p = env(now)
    src = mt._add("youtube", UC, "Cowen")
    mt.train.st["active"] = "all"
    for k in range(BEST_MIN):
        _, ev = _trade_round(mt, p, src, now, k, 95.0)
    assert mt.train.st["active"] is None
    assert any("Ни одна стратегия сейчас не в плюсе" in x["text"] for x in mt.train.st["lessons"])


def test_exit_rules_learned_from_full_paths_one_step_at_a_time():
    now = time.time()
    mt, _ = env(now)
    st = mt.train.st
    # монеты: сначала +7%, потом откат к −6% к концу срока → тейк +5% выгоднее, чем «нет тейка»
    st["paths"] = [{"kind": "cex", "path": [0.0, 7.0, 2.0, -6.0], "asset": "SOL", "at": now}
                   for _ in range(EXIT_MIN_PATHS - 1)]
    assert mt.train._learn_exits("cex", now) is None  # мало данных
    st["paths"].append(dict(st["paths"][0]))
    text = mt.train._learn_exits("cex", now)
    assert text and "Урок по выходам" in text and st["rules"]["cex"] == {"stop": 0.08, "tp": 0.05}
    assert mt.train._learn_exits("cex", now) is None  # +10% уже не достаётся — не дёргаемся
    # стоп: цена проваливается до −10%, потом отскакивает в +4% → стоп −8% вредит, свободнее (−12%) лучше
    st["paths"] = [{"kind": "cex", "path": [0.0, -10.0, 4.0], "asset": "ETH", "at": now}
                   for _ in range(EXIT_MIN_PATHS)]
    st["rules"]["cex"] = {"stop": 0.08, "tp": None}
    mt.train._learn_exits("cex", now)
    assert st["rules"]["cex"]["stop"] == 0.12


def test_reset_keeps_learned_rules_and_verdict():
    now = time.time()
    mt, p = env(now)
    src = mt._add("youtube", UC, "Cowen")
    for k in range(tr.VERDICT_MIN + 2):
        _trade_round(mt, p, src, now, k, 106.0)
    st = mt.train.st
    assert st["active"] and st["verdict_sent"]
    assert any("Итог тренировки" in x["text"] for x in st["lessons"])
    st["rules"]["cex"]["tp"] = 0.1
    active = st["active"]
    mt.train.reset(20)
    assert st["variants"]["all"]["cash"] == 20 and st["variants"]["all"]["n"] == 0
    assert st["rules"]["cex"]["tp"] == 0.1 and st["active"] == active and not st["verdict_sent"]


def test_training_state_survives_restore():
    import json
    now = time.time()
    mt, p = env(now)
    src = mt._add("youtube", UC, "Cowen")
    c = call(mt, src, now=now)
    mt.train.on_call(c, src, {}, now)
    saved = json.loads(json.dumps(mt.copy.export()))
    mt.copy.restore(saved)
    assert c["id"] in mt.train.st["variants"]["all"]["positions"] and mt.train.st["ideas"] == 1


def test_video_ideas_flow_into_training_and_signal_via_tick():
    now = time.time()
    mt, p = env(now)
    mt.seed()
    mt.train.st["active"] = "all"
    mt.youtube.add(UC, "newvideo001", age_h=1, now=now)
    events = run(mt.tick(now))
    texts = [e["text"] for e in events]
    assert any("Ждёт рост SOL" in t for t in texts) and any("Сигнал проверенной стратегии" in t for t in texts)
    assert mt.train.st["ideas"] == 1 and mt.ai_context()["training_demo"]["active"] == "Все прогнозы"


# --- Telegram и приложение ------------------------------------------------------------------

def test_telegram_train_commands(bot_env):
    from tests.test_ai_flow import buttons, press
    from tests.test_telegram_flow import send

    b, session = bot_env
    b.mentors.youtube = FakeYouTube()
    b.mentors.gemini = FakeGemini()

    async def go():
        await send(b, "/train")
        assert "Тренировка на демо" in session.sent[-1][1] and "Все прогнозы" in session.sent[-1][1]
        assert "tn:help" in buttons(session.markups[-1])
        await press(b, "tn:help")
        assert "5 стратегий" in session.sent[-1][1]
        await press(b, "tn:notify")
        assert b.mentors.train.st["notify"]
        await press(b, "tn:reset:5")
        assert b.mentors.train.st["variants"]["all"]["cash"] == 5
        await send(b, "/train reset 20")
        assert b.mentors.train.st["start_usd"] == 20
        await press(b, "tn:trades")
        assert "Закрытых демо-сделок пока нет" in session.sent[-1][1]
        await send(b, "/mentors")
        assert "tn:show" in buttons(session.markups[-1])
        assert "Тренировка" in session.sent[-1][1]

    asyncio.run(go())


def test_webapp_train_api(app_env):
    from tests import test_webapp as tw

    b, session = app_env

    async def fn(client):
        status, d = await tw.get(client, "/api/mentors")
        assert status == 200 and len(d["train"]["variants"]) == 5 and d["train"]["start_usd"] == 10
        status, d = await tw.post(client, "/api/mentors", {"train": {"reset": 5}}, uid=tw.FRIEND)
        assert status == 403
        status, d = await tw.post(client, "/api/mentors", {"train": {"reset": 5, "signals": False}})
        assert status == 200 and d["train"]["start_usd"] == 5 and not d["train"]["signals"]
        assert "Тренировка заново" in d["done"][0]
        status, d = await tw.get(client, "/api/overview")
        assert "train" in d["mentors"]

    tw.call(b, fn)


app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env
