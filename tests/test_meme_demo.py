"""🧪 Демо-счёт мемкоинов и 🧠 самообучение: подбор стопа/тейка по пути цены копий, калибровка комиссий по
котировкам Jupiter, решение о 👀-кошельках; демо-сделки без кошелька, Telegram и приложение."""

import time

from bot.copytrade import EXIT_MIN, learn_exits, simulate_exit
from bot.memetrade import LAMPORTS, MemeTrader
from tests.test_copytrade import MINT, MINT2, W, buy_tx, run, sell_tx, setup
from tests.test_memetrade import Chain, FakeJupiter, star, trader


def demo_trader(ct, jup=None):
    """Кошелёк НЕ подключён: торгует только демо (котировки — поддельный Jupiter по ценам DexScreener)."""
    jup = jup or FakeJupiter(Chain(ct.dex))
    mt = MemeTrader(ct, env={}, jup=jup)
    mt.start_demo_if_new()
    return mt, jup


def trade(ret, path, **kw):
    return {"passed": True, "ret": ret, "path": path, "closed": time.time(), **kw}


# --- обучение выходов --------------------------------------------------------------------

def test_learn_exits_moves_one_step_only_with_enough_evidence():
    # типичный мемкоин: взлетел на +60%, потом слился на −35% к моменту продажи кошелька
    pumps = [trade(-38.0, [0.0, 20.0, 60.0, 10.0, -35.0]) for _ in range(EXIT_MIN)]
    assert learn_exits(pumps[:EXIT_MIN - 1], 0.4, None, 0.02)[2] is None  # мало данных — не трогаем
    stop, tp, lesson = learn_exits(pumps, 0.4, None, 0.02)
    assert (stop, tp) == (0.4, 0.3) and "тейк-профит нет → +30%" in lesson  # один шаг: нет → +30%
    assert learn_exits(pumps, stop, tp, 0.02)[2] is None  # +50% дал бы столько же — не дёргаемся
    assert 50 < simulate_exit(pumps[0], 0.4, 0.3, 0.02) < 60
    # стоп: падают до −33%, потом копия закрывается по своему стопу −40% уже на −45%
    dumps = [trade(-47.0, [0.0, -5.0, -33.0, -45.0]) for _ in range(EXIT_MIN)]
    stop, tp, lesson = learn_exits(dumps, 0.4, None, 0.02)
    assert stop == 0.3 and tp is None and "стоп −40% → −30%" in lesson
    # шум: разница меньше 5% — правило не меняем
    flat = [trade(1.0, [0.0, 2.0, -3.0, 1.0]) for _ in range(EXIT_MIN)]
    assert learn_exits(flat, 0.4, None, 0.02)[2] is None


def test_paper_copies_record_price_path_and_cost():
    now = time.time()
    ct, rpc, dex = setup(now)
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    pos = ct.st["open"][f"{W}|{MINT}"]
    assert set(pos["path"]) == {0.0} and pos["cost"] == ct.cost_side() and pos["wstatus"] == "watch"
    dex.set_price(MINT, 0.0151 * 1.4)
    run(ct.step(now + 200))
    dex.set_price(MINT, 0.0151 * 1.1)
    rpc.push(W, sell_tx("s1", now + 250))
    run(ct.step(now + 300))
    t = ct.st["closed"][-1]
    assert max(t["path"]) > 35 and len(t["path"]) >= 3 and t["wstatus"] == "watch"


def test_cost_calibration_from_jupiter_quotes():
    now = time.time()
    ct, _, _ = setup(now)
    before = ct.cost_side()
    lessons = [ct.add_cost_sample(0.012, now) for _ in range(5)]
    assert lessons[:4] == [None] * 4 and "Калибровка" in lessons[4]
    assert abs(ct.cost_side() - (0.006 + 0.003)) < 1e-9 and ct.cost_side() < before  # 0,6% в одну сторону + запас
    assert ct.add_cost_sample(0.013, now) is None  # мелкое изменение — без урока
    assert ct.st["calib"]["cost"] == 0.009


# --- демо-счёт -----------------------------------------------------------------------------

def test_demo_copies_star_wallet_without_wallet_and_learns_costs():
    now = time.time()
    ct, rpc, dex = setup(now)
    mt, jup = demo_trader(ct)
    assert not mt.configured and mt.demo_on and ct.on
    star(ct)
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    events = run(mt.after_copy_step(now + 100))
    assert len(events) == 1 and "🧪 Демо: купил" in events[0]["text"] and "⭐ Кит" in events[0]["text"]
    assert ("💸 Продать (демо)", f"cb:mt:ds:{MINT}") in events[0]["buttons"]
    d = mt.demo
    pos = d["positions"][MINT]
    start = int(10 / 150 * LAMPORTS)
    assert d["start_sol"] == start and d["cash"] == start - pos["sol_in"] - 100_000 and pos["src"] == "jupiter"
    assert all(o[3] is None for o in jup.orders)  # демо никогда не просит транзакцию — только котировки
    assert ct.st["calib"]["rts"]  # замер стоимости входа-выхода записан
    dex.set_price(MINT, 0.0151 * 1.3)
    rpc.push(W, sell_tx("s1", now + 150))
    run(ct.step(now + 200))
    events = run(mt.after_copy_step(now + 200))
    assert any("🧪 Демо: ✅ продал" in e["text"] and "кошелёк продал" in e["text"] for e in events)
    s = d["stats"]
    assert not d["positions"] and s["sells"] == 1 and s["wins"] == 1 and 0.4 < s["pnl_usd"] < 0.6
    dd = mt.demo_dict()
    assert dd["equity_usd"] > 10 and dd["total_pct"] > 0 and dd["ready"]["state"] == "wait"
    assert "Демо-счёт" in mt.text() and "SOLANA_TRADER_KEY" in mt.text()
    assert "🧪 <b>Демо мемкоинов</b>" in mt.short_line()


def test_demo_also_tries_good_watch_wallets_unless_they_proved_losing():
    now = time.time()
    ct, rpc, dex = setup(now)
    mt, _ = demo_trader(ct)
    ct.st["wallets"][W]["hist"] = {"grade": "good"}  # 👀 с хорошей историей, без ⭐
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    run(mt.after_copy_step(now + 100))
    assert MINT in mt.demo["positions"] and mt.demo["positions"][MINT]["category"] == "good"
    mt.st["learn"]["good_ok"] = False
    dex.set_price(MINT2, 0.0151)
    rpc.push(W, buy_tx("b2", now + 110, mint=MINT2))
    run(ct.step(now + 120))
    run(mt.after_copy_step(now + 120))
    assert MINT2 not in mt.demo["positions"]


def test_demo_stop_take_profit_and_dexscreener_fallback():
    now = time.time()
    ct, _, dex = setup(now)
    mt, jup = demo_trader(ct)
    dex.set_price(MINT, 0.015)
    run(mt.demo_buy(MINT, 2, symbol="MOODENG", now=now))
    assert run(mt.guard(now + 90)) == [] and mt.demo["positions"][MINT]["value"] > 0
    dex.set_price(MINT, 0.015 * 0.5)
    events = run(mt.guard(now + 180))
    assert "стоп −40%" in events[0]["text"] and not mt.demo["positions"] and mt.demo["stats"]["pnl_usd"] < -0.9
    # тейк-профит, и Jupiter недоступен — демо считает по цене DexScreener
    mt.st["tp_pct"] = 0.3

    async def down(*a, **k):
        raise ConnectionError("нет сети")

    jup.order = down
    dex.set_price(MINT2, 0.02)
    e = run(mt.demo_buy(MINT2, 2, symbol="X", now=now))
    assert "Демо: купил" in e["text"] and mt.demo["positions"][MINT2]["src"] == "dex"
    dex.set_price(MINT2, 0.02 * 1.4)
    events = run(mt.guard(now + 300))
    assert "тейк-профит +30%" in events[0]["text"] and mt.demo["stats"]["wins"] == 1


def test_demo_limits_pause_and_reset_keep_lessons():
    now = time.time()
    ct, _, dex = setup(now)
    mt, _ = demo_trader(ct)
    d = mt.demo
    d["paused_until"] = now + 60
    assert "пауза" in mt._why_not({"passed": True, "mint": MINT, "reasons": []}, now, d)
    d["paused_until"] = 0
    dex.set_price(MINT, 0.015)
    try:
        run(mt.demo_buy(MINT, 50, now=now))  # $50 при демо-счёте $10
        raise AssertionError("должен отказать")
    except Exception as e:
        assert "мало SOL" in str(e)
    mt.st["stop_pct"], mt.st["tp_pct"] = 0.3, 0.5
    run(mt.demo_buy(MINT, 2, now=now))
    mt.demo_reset(20)
    assert not d["positions"] and d["start_usd"] == 20 and d["stats"]["buys"] == 0 and d["resets"] == 1
    assert (mt.st["stop_pct"], mt.st["tp_pct"]) == (0.3, 0.5)  # то, чему научился, остаётся
    assert abs(mt.demo_dict()["equity_usd"] - 20) < 0.01


def test_demo_is_quiet_while_real_wallet_trades():
    now = time.time()
    ct, rpc, dex = setup(now)
    mt, chain, jup = trader(ct)
    mt.start_demo_if_new()
    mt.st["on"] = True
    star(ct)
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    events = run(mt.after_copy_step(now + 100))
    assert len(events) == 1 and "Купил MOODENG" in events[0]["text"]  # о демо не шумим
    assert MINT in mt.st["positions"] and MINT in mt.demo["positions"]  # но демо учится параллельно


def test_real_low_balance_falls_back_to_demo():
    now = time.time()
    ct, rpc, dex = setup(now)
    mt, chain, jup = trader(ct, sol=0.005)  # кошелёк подключён, но не пополнен
    mt.start_demo_if_new()
    mt.st["on"] = True
    star(ct)
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    events = run(mt.after_copy_step(now + 100))
    assert not mt.st["positions"] and MINT in mt.demo["positions"]
    assert any("Пока пополнишь — сделку повторит 🧪 демо-счёт" in e["text"] for e in events)
    assert mt.status_dict()["funded"] is False


def test_learn_applies_exit_rules_and_decides_on_watch_wallets():
    now = time.time()
    ct, _, _ = setup(now)
    mt, _ = demo_trader(ct)
    ct.st["closed"] = [trade(-38.0, [0.0, 60.0, -35.0], wstatus="star") for _ in range(EXIT_MIN)]
    events = mt.learn(now)
    assert mt.st["tp_pct"] == 0.3 and any("Урок по выходам" in e["text"] for e in events)
    assert ct.st["lessons"][-1]["text"].startswith("🧠 Урок по выходам")
    assert mt.learn(now) == []  # новых копий нет — не пересчитываем
    ct.st["closed"] += [trade(15.0, [0.0, 15.0], wstatus="watch", wgrade="good") for _ in range(12)]
    events = mt.learn(now)
    assert mt.st["learn"]["good_ok"] is True and any("окупаются" in e["text"] for e in events)
    ct.st["closed"] += [trade(-30.0, [0.0, -30.0], wstatus="watch", wgrade="good") for _ in range(12)]
    mt.st["scope"] = "good"
    events = mt.learn(now)
    assert mt.st["learn"]["good_ok"] is False and mt.st["scope"] == "star"
    assert any("теряют" in e["text"] for e in events)
    assert "стоп" in mt.rules_text() and "тейк-профит" in mt.rules_text()


def test_old_state_without_demo_is_upgraded():
    now = time.time()
    ct, _, _ = setup(now)
    ct.st["real"] = {"on": False, "size_usd": 3.0, "positions": {}, "stats": {"buys": 1}}
    mt = MemeTrader(ct, env={}, jup=FakeJupiter(Chain(ct.dex)))
    assert mt.st["size_usd"] == 3.0 and mt.st["stats"]["buys"] == 1 and mt.st["stats"]["sells"] == 0
    assert mt.demo["positions"] == {} and mt.st["tp_pct"] is None and not mt.demo["started"]
    assert mt.start_demo_if_new() and not mt.start_demo_if_new()


# --- Telegram и приложение -------------------------------------------------------------

def test_telegram_demo_screen_buttons_and_lessons(bot_env):
    from tests.test_ai_flow import buttons, press
    from tests.test_telegram_flow import send

    b, session = bot_env
    ct, _, _ = setup(time.time())
    b.copy = ct
    b.meme = MemeTrader(ct, env={}, jup=FakeJupiter(Chain(ct.dex)))
    b.meme.start_demo_if_new()
    run(send(b, "/meme"))
    text, kb = session.sent[-1][1], buttons(session.markups[-1])
    assert "демо-счёте" in text and "mt:doff" in kb and "mt:dr" in kb and "mt:learn" in kb and "mt:on" not in kb
    run(press(b, "mt:dr"))
    assert "mt:dr:5" in buttons(session.markups[-1])
    run(press(b, "mt:dr:5"))
    assert "начат заново: $5" in session.sent[-1][1] and b.meme.demo["start_usd"] == 5
    run(press(b, "mt:doff"))
    assert not b.meme.demo_on and "на паузе" in session.sent[-1][1]
    run(press(b, "mt:size:3"))
    assert b.meme.st["size_usd"] == 3.0  # правила общие — меняются и без кошелька
    run(press(b, "mt:learn"))
    assert "Чему бот научился" in session.sent[-1][1]
    run(press(b, "mt:on"))  # настоящий счёт без кошелька не включить
    assert not b.meme.st["on"]


def test_webapp_demo_api(app_env):
    from tests import test_webapp as tw

    b, session = app_env
    ct, _, _ = setup(time.time())
    b.copy = ct
    b.meme = MemeTrader(ct, env={}, jup=FakeJupiter(Chain(ct.dex)))
    b.meme.start_demo_if_new()

    async def fn(client):
        status, d = await tw.get(client, "/api/copy")
        assert status == 200 and d["real"]["demo"]["on"] and d["real"]["demo"]["equity_usd"] == 10
        assert d["real"]["learned"]["cost_pct"] > 0
        status, d = await tw.post(client, "/api/meme", {"demo_reset": 20}, uid=tw.FRIEND)
        assert status == 403
        status, d = await tw.post(client, "/api/meme", {"demo_reset": 20})
        assert status == 200 and d["real"]["demo"]["start_usd"] == 20
        status, d = await tw.post(client, "/api/meme", {"demo_on": False})
        assert status == 200 and not d["real"]["demo"]["on"]
        status, d = await tw.post(client, "/api/meme", {"on": True})
        assert status == 400 and "SOLANA_TRADER_KEY" in d["error"]
        status, d = await tw.get(client, "/api/overview")
        assert "demo" in d["copy"]

    tw.call(b, fn)


app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env
