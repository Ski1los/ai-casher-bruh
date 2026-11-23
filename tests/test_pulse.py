"""⚡ Пульс рынка: кандидаты из GMGN/DexScreener/стримов, бумажные стратегии, стоп/тейк, обучение, сигналы,
сохранение состояния, Профит-центр, Telegram и Mini App."""

import asyncio
import time

from bot.dex import Pair
from bot.gmgn import GmgnError
from bot.pulse import (AVOID_MIN, BEST_MIN, HOLD_H, PRESETS, Pulse, bucket_ids, exit_point, is_safe, net_pct,
                       passes)

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

NOW = time.time()


def mint(i: int) -> str:
    return ("So1" + str(i).rjust(6, "0") + "x" * 40)[:44]


def pair(m: str, price: float = 1.0, liq: float = 50_000, **kw) -> Pair:
    p = Pair(chain="solana", dex="raydium", url="", pair_address="P" + m[:20], symbol=kw.pop("symbol", "T" + m[3:9]),
             name="t", address=m, quote="SOL", price_usd=price, liquidity_usd=liq, volume_h24=100_000,
             market_cap=300_000, created_at=NOW - 3 * 3600, buys_h1=kw.pop("buys", 120), sells_h1=kw.pop("sells", 80),
             change={"m5": kw.pop("m5", 1.0), "h1": 5.0})
    for k, v in kw.items():
        setattr(p, k, v)
    return p


class FakeDex:
    def __init__(self):
        self.pairs: dict[str, Pair] = {}
        self.trend: list[Pair] = []
        self.rugs: dict[str, dict] = {}

    async def trending(self, chain="solana", limit=8):
        return self.trend[:limit]

    async def rug_safe(self, m):
        return self.rugs.get(m)


class FakeCopy:
    def __init__(self):
        self.st: dict = {}
        self.changes = 0
        self.dex = FakeDex()

    def cost_side(self):
        return 0.01

    async def _pairs(self, mints):
        return {m: self.dex.pairs[m] for m in mints if m in self.dex.pairs}


class FakeGmgn:
    def __init__(self, configured=True):
        self.configured = configured
        self.rank: list[dict] = []
        self.trench: list[dict] = []
        self.smart: list[dict] = []
        self.calls: list[dict] = []
        self.sec: dict[str, dict] = {}
        self.fail = False

    async def trending(self, chain, interval, limit=50):
        if self.fail:
            raise GmgnError("GMGN: лимит запросов, жду")
        return self.rank

    async def trenches(self, chain, limit=30):
        return self.trench

    async def smartmoney(self, chain, limit=100):
        return self.smart

    async def kol(self, chain, limit=100):
        return []

    async def callouts(self, chain, limit=30):
        return self.calls

    async def security(self, m):
        if m not in self.sec:
            raise GmgnError("нет")
        return dict(self.sec[m])

    def status(self):
        return {"configured": self.configured, "test_key": False, "calls": 0, "wait": 0, "error": None,
                "key_bad": False}


def tok(m, **kw):
    base = {"mint": m, "symbol": "G" + m[3:8], "category": "trend_5m", "top10": 0.15, "dev": 0.0, "bundler": 0.05,
            "insider": 0.02, "rug": 0.05, "smart": 3, "kol": 0, "dev_sold": True, "honeypot": False, "wash": False}
    base.update(kw)
    return base


def env(configured=True):
    c = FakeCopy()
    g = FakeGmgn(configured)
    return c, g, Pulse(c, g)


def run(coro):
    return asyncio.run(coro)


# --- чистые функции -----------------------------------------------------------------------------

def test_filters_and_buckets():
    good = {"top10": 0.15, "bundler": 0.05, "insider": 0.02, "rug": 0.05, "smart": 3, "liq": 50_000, "dev": 0.0,
            "src": ["trend"], "chg5m": 3.0, "bs": 1.5}
    assert is_safe(good) and passes("strict", good) and passes("smart", good) and passes("momentum", good)
    assert not is_safe({**good, "rug": None})  # неизвестно — не «безопасно»
    assert not is_safe({**good, "wash": True})
    assert not passes("stretch", good) and passes("stretch", {**good, "src": ["stretch"]})
    assert passes("callout", {**good, "src": ["insider"]})  # мелькнул в инсайд-чате на стриме
    ids = bucket_ids(good)
    assert "top10:lo" in ids and "smart:3" in ids and "src:trend" in ids and "liq:mid" in ids
    assert not passes("learned", good, ["top10:lo"]) and passes("learned", good, ["top10:hi"])
    assert exit_point([0, -10, -40, 50], 0.35, None) == (-40, "stop")
    assert exit_point([0, 20, 60], 0.35, 0.5) == (60, "tp")
    assert exit_point([0, 5], 0.35, None) == (5, "time")
    assert round(net_pct(0, 2.0, 0.01), 2) < -2.9  # комиссии с двух сторон + сеть съедают ~3%


# --- поиск и бумажные сделки ------------------------------------------------------------------

def test_scan_collects_gmgn_dex_and_stream_candidates():
    c, g, p = env()
    m1, m2, m3, m4, m5 = (mint(i) for i in range(1, 6))
    g.rank = [tok(m1), tok(m4, honeypot=True)]
    g.trench = [tok(m2, category="stretch", rug=0.6, bundler=0.5)]
    g.smart = [{"mint": m1, "symbol": "A", "maker": "W1", "side": "buy", "usd": 500, "at": NOW - 60, "opened": True,
                "who": "ansem"},
               {"mint": m1, "symbol": "A", "maker": "W2", "side": "buy", "usd": 300, "at": NOW - 60, "opened": True,
                "who": ""}]
    c.dex.trend = [pair(m3, boosts=5)]
    p.add_external(m5, "insider", "Decu")
    for m in (m1, m2, m3, m4):
        c.dex.pairs[m] = pair(m)
    c.dex.pairs[m5] = pair(m5, liq=1000)  # ликвидности нет — даже на бумагу не берём
    ev = run(p.scan(NOW))
    st = p.st
    assert set(st["picks"]) == {m1, m2, m3}  # honeypot и пустой пул отброшены
    f1 = st["picks"][m1]["f"]
    assert f1["smart_buys"] == 2 and f1["smart"] == 3 and "smart" in f1["src"] and "trend" in f1["src"]
    assert st["picks"][m1]["who"] == "ansem"
    assert st["picks"][m3]["f"]["src"] == ["promo"] and st["source"] == "GMGN + DexScreener"
    assert st["external"] == [] and m5 in st["seen"]
    assert m1 in st["books"]["strict"]["positions"] and m1 in st["books"]["safe"]["positions"]
    assert m2 in st["books"]["stretch"]["positions"] and m2 not in st["books"]["safe"]["positions"]
    assert all(len(b["positions"]) <= 5 for b in st["books"].values())
    assert ev == []  # активной стратегии ещё нет — сигналов нет
    # тот же токен второй раз — не кандидат
    assert run(p.scan(NOW + 700)) == [] and len(st["picks"]) == 3


def test_without_gmgn_uses_dexscreener_and_rugcheck():
    c, g, p = env(configured=False)
    m1 = mint(7)
    c.dex.trend = [pair(m1)]
    c.dex.pairs[m1] = pair(m1)
    c.dex.rugs[m1] = {"score": 45, "risks": [{"name": "Mutable metadata", "level": "danger"}]}
    run(p.scan(NOW))
    f = p.st["picks"][m1]["f"]
    assert f["rug"] == 0.45 and p.st["source"] == "DexScreener"
    chk = run(p.token_check(m1, NOW))
    assert chk["src"] == "RugCheck" and chk["risks"] == ["Mutable metadata"]
    g.configured = True
    g.sec[m1] = {"top10": 0.7, "insider": 0.4}
    assert run(p.token_check(m1, NOW + 10))["src"] == "RugCheck"  # кэш 10 минут
    assert run(p.token_check(m1, NOW + 700))["src"] == "GMGN"


def test_gmgn_error_falls_back_and_is_shown():
    c, g, p = env()
    g.fail = True
    g.trench = []
    g.rank = []
    m1 = mint(8)
    c.dex.trend = [pair(m1)]
    c.dex.pairs[m1] = pair(m1)
    run(p.scan(NOW))
    assert m1 in p.st["picks"]
    assert "Пульс рынка" in p.text()


def test_update_stops_takes_rug_and_learning():
    c, g, p = env()
    st = p.st
    ms = [mint(100 + i) for i in range(30)]
    # половина токенов с большими бандлами падает, половина безопасных растёт
    for i, m in enumerate(ms):
        bad = i % 2 == 0
        g.rank.append(tok(m, bundler=0.5 if bad else 0.05, smart=0 if bad else 3))
        c.dex.pairs[m] = pair(m)
    t = NOW
    from bot.pulse import MAX_NEW
    for chunk in range(0, 30, MAX_NEW):
        g.rank_all = g.rank
        g.rank = [x for x in g.rank_all if x["mint"] in ms[chunk:chunk + MAX_NEW]]
        run(p.scan(t))
        g.rank = g.rank_all
        t += 600
    assert len(st["picks"]) == 30
    for i, m in enumerate(ms):
        c.dex.pairs[m].price_usd = 0.4 if i % 2 == 0 else 1.6
    del c.dex.pairs[ms[0]]  # пул слили — пары нет
    for k in range(3):
        run(p.update(t + k * 300))
    assert st["picks"][ms[0]]["path"][-1] == -95.0
    # плохие: −60% → стоп −35% сработал сразу; хорошие: +60%, тейка нет — держим
    assert all(m not in b["positions"] for b in st["books"].values() for m in ms[::2])
    assert any(m in st["books"]["all"]["positions"] for m in ms[1::2])
    for m in ms[1::2]:
        c.dex.pairs[m].price_usd = 1.05  # рост выдохся — без тейка прибыль почти вся ушла
    ev = run(p.update(t + HOLD_H * 3600 + 3600))
    assert not st["picks"] and len(st["done"]) == 30
    ins = {x["id"]: x for x in p.insights()}
    assert ins["bundler:hi"]["avg"] < -30 and ins["bundler:lo"]["avg"] > 30
    assert ins["bundler:hi"]["verdict"] == "losing"
    assert "bundler:hi" in st["avoid"] and "smart:0" in st["avoid"]  # выучил сам (≥15 токенов, доказанный минус)
    assert all(not x.startswith("src:") for x in st["avoid"])
    assert AVOID_MIN == 15 and st["rule"]["tp"] is not None  # подобрал тейк: на этих путях фиксировать выгоднее
    assert any("выучил, чего избегать" in e["text"] for e in ev)
    b = st["books"]["all"]
    assert b["n"] >= 5 and len(b["rets"]) == b["n"]
    # выученный фильтр больше не берёт токены с плохими признаками
    m_bad, m_ok = mint(900), mint(901)
    g.rank = [tok(m_bad, bundler=0.5, smart=0), tok(m_ok)]
    c.dex.pairs[m_bad], c.dex.pairs[m_ok] = pair(m_bad), pair(m_ok)
    run(p.scan(t + 30 * 3600))
    assert m_ok in st["books"]["learned"]["positions"] and m_bad not in st["books"]["learned"]["positions"]


def test_best_strategy_sends_signals_and_profit_route():
    c, g, p = env()
    st = p.st
    st["books"]["safe"]["rets"] = [12.0, 8.0, 15.0, 9.0, 11.0, 7.0, 14.0, 10.0, 13.0, 9.5, 12.5, 8.5]
    st["books"]["safe"]["n"] = 12
    st["books"]["all"]["rets"] = [-20.0, 5.0] * 6
    st["books"]["all"]["n"] = 12
    assert len(st["books"]["safe"]["rets"]) >= BEST_MIN
    ev = p.learn(NOW)
    assert st["active"] == "safe" and any("Безопасные" in e["text"] for e in ev)
    m1 = mint(50)
    g.rank = [tok(m1)]
    c.dex.pairs[m1] = pair(m1)
    ev = run(p.scan(NOW))
    sig = [e for e in ev if e.get("mint") == m1]
    assert sig and "сигнал стратегии «Безопасные»" in sig[0]["text"] and "не гарантия" in sig[0]["text"]
    assert any("gmgn.ai/sol/token/" in u for _, u in sig[0]["buttons"])
    name, rets, first = p.engine_rets()
    assert name == "Безопасные" and len(rets) == 12
    # стратегия испортилась — сигналы выключаются
    st["books"]["safe"]["rets"] = [-15.0, -12.0, -18.0] * 4
    ev = p.learn(NOW + 10)
    assert st["active"] is None and any("Сигналы выключил" in e["text"] for e in ev)
    d = p.status_dict()
    assert len(d["books"]) == len(PRESETS) and d["gmgn"]["configured"]
    assert p.ai_context()["strategies"]


def test_state_survives_copytrader_restore():
    from bot.copytrade import CopyTrader
    ct = CopyTrader(FakeDex())
    p = Pulse(ct, FakeGmgn())
    p.st["avoid"] = ["bundler:hi"]
    p.add_external(mint(3), "stream", "Decu", NOW)
    data = ct.export()
    ct2 = CopyTrader(FakeDex())
    ct2.restore(data)
    p2 = Pulse(ct2, FakeGmgn())
    assert p2.st["avoid"] == ["bundler:hi"] and p2.st["external"][0]["who"] == "Decu"


def test_step_respects_intervals_and_off():
    c, g, p = env()
    m1 = mint(60)
    g.rank = [tok(m1)]
    c.dex.pairs[m1] = pair(m1)
    run(p.step(NOW))
    assert m1 in p.st["picks"] and p.st["scans"] == 1
    run(p.step(NOW + 60))
    assert p.st["scans"] == 1  # раньше 10 минут не ищем
    p.add_external(mint(61), "tracker", "Decu", NOW + 70)
    c.dex.pairs[mint(61)] = pair(mint(61))
    run(p.step(NOW + 130))
    assert p.st["scans"] == 2 and mint(61) in p.st["picks"]  # токен со стрима — сразу
    p.st["on"] = False
    assert run(p.step(NOW + 5000)) == [] and p.st["scans"] == 2


# --- Telegram и Mini App --------------------------------------------------------------------------

def test_pulse_command_and_buttons(bot_env):
    from tests.test_ai_flow import buttons, press
    from tests.test_telegram_flow import send

    b, session = bot_env
    assert b.pulse is not None and b.mentors.live.pulse is b.pulse

    async def go():
        await send(b, "/pulse")
        text = session.sent[-1][1]
        assert "Пульс рынка" in text and "GMGN" in text and "Axiom" in text
        kb = buttons(session.markups[-1])
        assert "pu:learn" in kb and "pu:help" in kb and "pu:toggle" in kb
        await press(b, "pu:help")
        assert "Final Stretch" in session.sent[-1][1]
        await press(b, "pu:toggle")
        assert b.pulse.st["on"] is False
        await press(b, "pu:toggle")
        assert b.pulse.st["on"] is True
        await press(b, "pu:learn")
        assert "Чему Пульс научился" in session.sent[-1][1]
        await press(b, "pu:picks")
        assert "Кандидаты Пульса" in session.sent[-1][1]
        await press(b, "mn:mirror")
        assert "Повтор стримеров на демо" in session.sent[-1][1] and "Умная копия" in session.sent[-1][1]
        await press(b, "mn:mtoggle")
        assert b.mentors.live.mirror.st["on"] is False

    asyncio.run(go())
    titles = [e[0] for e in b.autonomy.engines_status()]
    assert "⚡ Пульс рынка" in titles and "🪞 Повтор стримеров на демо" in titles
    assert "⚡ пульс рынка" in b.autonomy.ensure() or b.pulse.st["on"]
    assert b.mentors.live.mirror.st["on"] is True  # автомат включил повтор обратно
    assert "market_pulse_gmgn_axiom" in b.ai_context()["extras"]


def test_webapp_pulse_and_mirror(app_env):
    from tests.test_webapp import call, get, post

    b, session = app_env

    async def go(client):
        status, d = await get(client, "/api/mentors")
        assert status == 200 and d["pulse"]["books"] and "mirror" in d["live"] and "details" in d["live"]
        status, d = await post(client, "/api/mentors", {"pulse": {"on": False}, "live": {"mirror": False}})
        assert status == 200
        assert b.pulse.st["on"] is False and b.mentors.live.mirror.st["on"] is False

    call(b, go)
