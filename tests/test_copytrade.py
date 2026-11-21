"""🐋 Копитрейдинг: разбор транзакций Solana, анализ кошелька, риск-фильтр, бумажные копии, эволюция, обучение."""

import asyncio
import time

from bot.copytrade import (FILTER_DEFAULT, STALE_SEC, USDC_MINT, WSOL, CopyTrader, analyze_history, buy_links,
                           diagnose, learn_filter, parse_swap)
from bot.dex import Pair
from tests import fake_market_api as fm

W = "Wha1e1111111111111111111111111111111111111"  # кошелёк, за которым следим
POOL = "Poo1111111111111111111111111111111111111111"
MINT = fm.NEW  # «MOODENG»: ликвидность $85k, RugCheck 12, 10 часов
MINT2 = fm.WIF
L = 1_000_000_000


def run(coro):
    return asyncio.run(coro)


def tb(idx, mint, owner, amount):
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"uiAmountString": str(amount), "decimals": 6}}


def tx(sig, ts, keys, pre, post, pre_t, post_t, fee=5000, err=None):
    return {"blockTime": ts, "slot": 100, "meta": {"err": err, "fee": fee, "preBalances": pre, "postBalances": post,
                                                   "preTokenBalances": pre_t, "postTokenBalances": post_t},
            "transaction": {"signatures": [sig], "message": {"accountKeys": [
                {"pubkey": k, "signer": i == 0, "writable": True} for i, k in enumerate(keys)]}}}


def buy_tx(sig, ts, wallet=W, mint=MINT, sol=0.1, amount=1000.0, new_account=True):
    """Покупка за SOL: кошелёк платит SOL + комиссию + аренду за новый счёт токена."""
    rent = 2_039_280 if new_account else 0
    return tx(sig, ts, [wallet, "Ata" + sig[:40].ljust(40, "1"), POOL],
              [5 * L, 0 if new_account else 2_039_280, 100 * L],
              [5 * L - int(sol * L) - 5000 - rent, 2_039_280, 100 * L + int(sol * L)],
              [] if new_account else [tb(1, mint, wallet, 0)], [tb(1, mint, wallet, amount)])


def sell_tx(sig, ts, wallet=W, mint=MINT, sol=0.13, amount=1000.0, close_account=True):
    rent = 2_039_280 if close_account else 0
    return tx(sig, ts, [wallet, "Ata" + sig[:40].ljust(40, "1"), POOL],
              [5 * L, 2_039_280, 100 * L],
              [5 * L + int(sol * L) - 5000 + rent, 0 if close_account else 2_039_280, 100 * L - int(sol * L)],
              [tb(1, mint, wallet, amount)], [tb(1, mint, wallet, 0)] if not close_account else [])


# --- разбор транзакций ---------------------------------------------------------

def test_parse_buy_with_sol_ignores_fee_and_account_rent():
    sw = parse_swap(buy_tx("s1", 1000), W)
    assert sw["side"] == "buy" and sw["mint"] == MINT and sw["amount"] == 1000
    assert abs(sw["sol"] - 0.1) < 1e-9  # без комиссии сети и без 0,002 SOL аренды
    sw = parse_swap(sell_tx("s2", 1100), W)
    assert sw["side"] == "sell" and abs(sw["sol"] - 0.13) < 1e-9  # возврат аренды — не доход


def test_parse_sell_for_usdc_and_buy_through_wsol():
    usdc = tx("u1", 1, [W, "AtaA", "AtaB", POOL], [L, 2_039_280, 2_039_280, L], [L - 5000, 2_039_280, 2_039_280, L],
              [tb(1, MINT, W, 500), tb(2, USDC_MINT, W, 0)], [tb(1, MINT, W, 0), tb(2, USDC_MINT, W, 25)])
    sw = parse_swap(usdc, W)
    assert sw["side"] == "sell" and sw["usd"] == 25 and sw["amount"] == 500
    wsol = tx("w1", 1, [W, "AtaA", "AtaW", POOL], [L, 2_039_280, 2_039_280, L], [L - 5000, 2_039_280, 2_039_280, L],
              [tb(1, MINT, W, 0), tb(2, WSOL, W, 0.5)], [tb(1, MINT, W, 700), tb(2, WSOL, W, 0.4)])
    sw = parse_swap(wsol, W)
    assert sw["side"] == "buy" and abs(sw["sol"] - 0.1) < 1e-9


def test_parse_ignores_transfers_failed_and_foreign_transactions():
    transfer = tx("t1", 1, [W, "AtaA", "Friend"], [L, 2_039_280, L], [L - 5000, 2_039_280, L],
                  [tb(1, MINT, W, 100)], [tb(1, MINT, W, 40)])
    assert parse_swap(transfer, W) is None
    failed = buy_tx("f1", 1)
    failed["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert parse_swap(failed, W) is None
    assert parse_swap(buy_tx("x1", 1, wallet="Other111111111111111111111111111111111111"), W) is None
    assert parse_swap(None, W) is None


# --- анализ кошелька ------------------------------------------------------------

def test_analyze_history_grades():
    now = 1_000_000
    good = []
    for k, (b, s) in enumerate([(1.0, 1.6), (1.0, 0.8), (0.5, 1.2), (1.0, 1.3)]):
        m = f"M{k}"
        good += [{"ts": now - 86400 + k * 3600, "side": "buy", "mint": m, "sol": b, "amount": 100, "usd": 0},
                 {"ts": now - 86400 + k * 3600 + 1800, "side": "sell", "mint": m, "sol": s, "amount": 100, "usd": 0}]
    h = analyze_history(good, now)
    assert h["grade"] == "good" and h["closed"] == 4 and h["wins"] == 3 and abs(h["pnl_sol"] - 1.4) < 1e-9
    assert h["hold_min"] == 30
    bot = [{"ts": now - 600 + k * 5, "side": "buy" if k % 2 == 0 else "sell", "mint": f"B{k // 2}", "sol": 0.1,
            "amount": 10, "usd": 0} for k in range(40)]
    assert analyze_history(bot, now)["grade"] == "bot"
    fast = []
    for k in range(4):
        fast += [{"ts": now - 86400 + k * 7200, "side": "buy", "mint": f"F{k}", "sol": 1, "amount": 1, "usd": 0},
                 {"ts": now - 86400 + k * 7200 + 40, "side": "sell", "mint": f"F{k}", "sol": 1.1, "amount": 1, "usd": 0}]
    assert analyze_history(fast, now)["grade"] == "fast"
    assert analyze_history([], now)["grade"] == "unknown"


# --- обучение фильтра и разбор ошибок -------------------------------------------

def feats(liq=60_000, rug=10, age=5, chase=0.05, h1=10):
    return {"liq": liq, "rug": rug, "age_h": age, "chase": chase, "h1": h1, "fdv_liq": 10}


def test_filter_learns_from_losses_one_step_at_a_time():
    closed = ([{"ret": -30.0, "feats": feats(liq=25_000)} for _ in range(10)]
              + [{"ret": 10.0, "feats": feats(liq=60_000)} for _ in range(15)])
    new, lesson = learn_filter(closed, dict(FILTER_DEFAULT))
    assert new["min_liq"] == 30_000  # один шаг: 20k → 30k, а не сразу в лучшее значение
    assert "ликвидность не меньше" in lesson and "+150.0%" in lesson and "-150.0%" in lesson
    again, lesson2 = learn_filter(closed, new)
    assert again == new and lesson2 is None  # дальше двигать незачем
    assert learn_filter(closed[:10], dict(FILTER_DEFAULT)) == (FILTER_DEFAULT, None)  # мало данных — не учится


def test_diagnose_names_the_mistake():
    pos = {"opened": 0, "closed_at": 120, "feats": feats(liq=8_000, chase=0.3, age=0.4)}
    assert set(diagnose(pos, -20, "кошелёк продал")) == {"late", "fast", "liq", "young"}
    assert diagnose({"opened": 0, "closed_at": 9999, "feats": feats()}, -41, "стоп-лосс") == ["dump"]
    assert diagnose(pos, 5, "кошелёк продал") == []


def test_buy_links_open_jupiter_inside_phantom():
    links = buy_links(MINT)
    assert links["jupiter"] == f"https://jup.ag/swap/SOL-{MINT}"
    assert links["phantom"].startswith("https://phantom.app/ul/browse/https%3A%2F%2Fjup.ag%2Fswap%2FSOL-")


# --- копитрейдер целиком на поддельных Solana и DexScreener ------------------------

class FakeRpc:
    def __init__(self):
        self.sigs: dict[str, list[dict]] = {}  # адрес -> подписи, новые первыми
        self.txs: dict[str, dict] = {}
        self.calls = []

    def push(self, address, t, slot=100):
        sig = t["transaction"]["signatures"][0]
        self.txs[sig] = t
        self.sigs.setdefault(address, []).insert(0, {"signature": sig, "err": None, "slot": slot,
                                                      "blockTime": t["blockTime"]})

    async def call(self, method, params):
        self.calls.append(method)
        if method == "getSignaturesForAddress":
            addr, opts = params
            out = list(self.sigs.get(addr, []))
            if opts.get("before"):
                idx = [s["signature"] for s in out].index(opts["before"])
                out = out[idx + 1:]
            if opts.get("until"):
                names = [s["signature"] for s in out]
                out = out[:names.index(opts["until"])] if opts["until"] in names else out
            return out[:opts["limit"]]
        if method == "getTransaction":
            return self.txs.get(params[0])
        raise AssertionError(method)


class FakeDex:
    def __init__(self):
        self.pairs = {m: Pair.from_json(ps[0]) for m, ps in fm.PAIRS.items()}
        self.trend = []

    def set_price(self, mint, price, liq=None):
        p = self.pairs[mint]
        p.price_usd = price
        if liq is not None:
            p.liquidity_usd = liq

    async def token_prices(self, chain, mints):
        return {WSOL: ("SOL", 150.0)}

    async def tokens(self, chain, mints):
        return [self.pairs[m] for m in mints if m in self.pairs]

    async def rug_safe(self, mint):
        from bot.dex import parse_rug
        return parse_rug(fm.RUGS[mint]) if mint in fm.RUGS else None

    async def trending(self, chain="solana", limit=8):
        return self.trend


def setup(now):
    rpc, dex = FakeRpc(), FakeDex()
    ct = CopyTrader(dex, rpc)
    ct.st["on"] = True
    run(ct.add_wallet(W, "Кит", now=now))
    rpc.push(W, buy_tx("old1", now - 86400, mint=MINT2))  # старая сделка — копировать не надо
    assert run(ct.step(now)) == [] and not ct.st["open"]
    return ct, rpc, dex


def test_copy_buy_then_wallet_sell_closes_paper_copy_with_costs():
    now = time.time()
    ct, rpc, dex = setup(now)
    # кошелёк купил 1000 MOODENG за 0,1 SOL (= $15 при SOL $150 → $0.015 за токен); мы видим цену на 5% выше
    dex.set_price(MINT, 0.01575)
    rpc.push(W, buy_tx("b1", now - 30))
    assert run(ct.step(now + 100)) == []  # кошелёк пока без ⭐ — сигналов нет, но копия открыта
    pos = ct.st["open"][f"{W}|{MINT}"]
    assert pos["passed"] and abs(pos["feats"]["chase"] - 0.05) < 1e-6 and pos["w_usd"] == 15.0
    dex.set_price(MINT, 0.01575 * 1.3)
    rpc.push(W, sell_tx("s1", now + 150))
    run(ct.step(now + 200))
    assert not ct.st["open"]
    t = ct.st["closed"][-1]
    assert t["why"] == "кошелёк продал" and 24 < t["ret"] < 26  # +30% минус комиссии и проскальзывание
    w = ct.st["wallets"][W]
    assert w["rets"] == [t["ret"]] and w["status"] == "watch"
    assert ct.summary()["taken"] == 1


def test_star_wallet_sends_signal_with_phantom_button_and_sell_alert():
    now = time.time()
    ct, rpc, dex = setup(now)
    ct.st["wallets"][W]["status"] = "star"
    ct.st["wallets"][W]["rets"] = [12.0] * 6
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    events = run(ct.step(now + 100))
    assert len(events) == 1 and "Сигнал копирования: MOODENG" in events[0]["text"]
    assert events[0]["buttons"][0][0] == "👻 Купить в Phantom" and "phantom.app" in events[0]["buttons"][0][1]
    rpc.push(W, sell_tx("s1", now + 150))
    events = run(ct.step(now + 200))
    assert any("продал <b>MOODENG</b>" in e["text"] for e in events)
    # повторная покупка того же токена в течение 6 ч — без повторного сигнала
    rpc.push(W, buy_tx("b2", now + 250, new_account=False))
    assert run(ct.step(now + 300)) == []


def test_stop_loss_stale_buys_and_risky_tokens():
    now = time.time()
    ct, rpc, dex = setup(now)
    rpc.push(W, buy_tx("stale", now - STALE_SEC - 60))
    run(ct.step(now))
    assert not ct.st["open"] and ct.st["stats"]["skipped_stale"] == 1
    dex.set_price(MINT, 0.015)
    rpc.push(W, buy_tx("b1", now))
    run(ct.step(now + 10))
    dex.set_price(MINT, 0.015 * 0.55)
    run(ct.step(now + 20))
    t = ct.st["closed"][-1]
    assert t["why"] == "стоп-лосс" and t["ret"] < -40 and t["mistakes"] == ["dump"]
    # скам-токен: фильтр не пропускает, но копия ведётся на бумаге — чтобы учиться
    rpc.push(W, buy_tx("scam", now + 30, mint=fm.SCAM, amount=1e7, sol=0.05))
    run(ct.step(now + 40))
    pos = ct.st["open"][f"{W}|{fm.SCAM}"]
    assert not pos["passed"] and pos["hard_fail"] and any("RugCheck" in r for r in pos["reasons"])


def test_wallet_evolution_star_then_dropped():
    now = time.time()
    ct, rpc, dex = setup(now)
    w = ct.st["wallets"][W]
    events = []
    for _ in range(5):
        ct._evolve(W, w, 8.0, now, events)
    assert w["status"] == "star" and "повышен" in events[-1]["text"]
    for _ in range(4):
        ct._evolve(W, w, -20.0, now, events)
    assert w["status"] == "dropped" and "4 убытка подряд" in w["reason"]
    assert "Он твой" in events[-1]["text"] and W in ct.active()  # свой кошелёк не удаляем, но сигналов нет
    assert len(ct.st["lessons"]) >= 2


def test_discover_finds_early_buyers_and_rejects_bots():
    now = time.time()
    rpc, dex = FakeRpc(), FakeDex()
    ct = CopyTrader(dex, rpc)
    p = dex.pairs[MINT]
    p.pair_address, p.change["h24"] = POOL, 140.0
    dex.trend = [p, dex.pairs[fm.BONK]]  # BONK старый и не вырос — не подходит
    sniper = "Snipe11111111111111111111111111111111111111"
    good = "Smart11111111111111111111111111111111111111"
    bot = "Robot11111111111111111111111111111111111111"
    rpc.push(POOL, buy_tx("p0", now - 36000, wallet=sniper), slot=10)  # первый блок — снайпер, пропускаем
    rpc.push(POOL, buy_tx("p1", now - 35000, wallet=good), slot=20)
    rpc.push(POOL, buy_tx("p2", now - 34000, wallet=bot), slot=21)
    for k in range(30):  # бот: сделки каждые 5 секунд
        rpc.push(bot, buy_tx(f"r{k}", now - 1000 + k * 5, wallet=bot, mint=f"M{k}"))
    report = run(ct.discover(now, force=True))
    assert good in ct.st["wallets"] and ct.st["wallets"][good]["source"] == "auto"
    assert bot not in ct.st["wallets"] and bot in ct.st["graveyard"]
    assert sniper not in ct.st["wallets"]
    assert any("MOODENG" in r for r in report) and any("бот-снайпер" in r for r in report)
    assert run(ct.discover(now + 60)) == []  # не чаще раза в 2 часа


def test_export_restore_and_texts():
    now = time.time()
    ct, rpc, dex = setup(now)
    raw = ct.export()
    ct2 = CopyTrader(FakeDex(), FakeRpc())
    ct2.restore(raw)
    assert W in ct2.st["wallets"] and ct2.st["filter"] == FILTER_DEFAULT
    assert "Копитрейдинг" in ct2.text() and "Кит" in ct2.text()
    d = ct2.status_dict()
    assert d["wallets"][0]["name"] == "Кит" and d["on"]
    assert "Кит" in ct2.wallet_text(W)


# --- Telegram ------------------------------------------------------------------

def test_copy_commands_buttons_signals_report_and_backup(bot_env):
    from bot import copy_ui
    from tests.test_ai_flow import buttons, press
    from tests.test_telegram_flow import OWNER, send
    FRIEND = 222
    b, session = bot_env
    b.allowed_ids = {OWNER, FRIEND}
    rpc, dex = FakeRpc(), FakeDex()
    b.copy = CopyTrader(dex, rpc)
    run(send(b, "/copy"))
    assert "Копитрейдинг" in session.sent[-1][1] and "cp:on" in buttons(session.markups[-1])
    run(send(b, f"/copy add {W} Кит"))
    assert W in b.copy.st["wallets"] and "Кит" in session.sent[-1][1]
    run(send(b, "/copy add abc"))
    assert "не адрес Solana" in session.sent[-1][1]
    run(send(b, f"/copy add {W}", user=FRIEND))
    assert "только владелец" in session.sent[-1][1]
    run(press(b, "cp:on"))
    assert b.copy.on and "включён" in session.sent[-1][1]
    run(press(b, "cp:sig"))
    assert not b.copy.st["signals"]
    run(press(b, "cp:sig"))
    run(press(b, "cp:lessons"))
    assert "Чему бот научился" in session.sent[-1][1]
    # сигнал ⭐-кошелька приходит с кнопкой-ссылкой на Phantom
    now = time.time()
    b.copy.st["discovered_at"] = now  # поиск кошельков не нужен
    run(copy_ui.copy_tick(b))  # первый шаг запоминает, откуда начинать
    w = b.copy.st["wallets"][W]
    w["status"], w["rets"] = "star", [10.0] * 6
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 20))
    run(copy_ui.copy_tick(b))
    assert "Сигнал копирования" in session.sent[-1][1]
    btn = session.markups[-1].inline_keyboard[0][0]
    assert btn.url.startswith("https://phantom.app/ul/browse/")
    assert "Копитрейдинг" in b.report_text()
    assert b._state()["copy"]["wallets"][W]["name"] == "Кит"
    assert b.ai_context()["extras"]["copytrading_solana"]["wallets"][0]["name"] == "Кит"
    assert b.copy_file.exists()  # сохранено на диск


def test_first_trade_of_empty_wallet_is_copied():
    now = time.time()
    rpc, dex = FakeRpc(), FakeDex()
    ct = CopyTrader(dex, rpc)
    ct.st["on"] = True
    run(ct.add_wallet(W, now=now))
    run(ct.step(now))  # истории нет совсем
    rpc.push(W, buy_tx("b1", now))
    run(ct.step(now + 10))
    assert f"{W}|{MINT}" in ct.st["open"]


# --- приложение ------------------------------------------------------------------

def test_webapp_copy_api(app_env):
    from tests import test_webapp as tw
    b, session = app_env
    rpc, dex = FakeRpc(), FakeDex()
    b.copy = CopyTrader(dex, rpc)

    async def fn(client):
        r = await client.get("/api/copy", headers=tw.auth(tw.OWNER))
        d = await r.json()
        assert r.status == 200 and d["wallets"] == [] and not d["on"]
        status, d = await tw.post(client, "/api/copy", {"add": W, "name": "Кит"})
        assert status == 200 and d["wallets"][0]["name"] == "Кит"
        status, d = await tw.post(client, "/api/copy", {"add": "nope"})
        assert status == 400 and "не адрес Solana" in d["error"]
        status, d = await tw.post(client, "/api/copy", {"on": True, "signals": False})
        assert d["on"] and not d["signals"] and not d["searching"]  # кошелёк уже есть — искать не надо
        status, d = await tw.post(client, "/api/copy", {"on": False}, uid=tw.FRIEND)
        assert status == 403
        r = await client.get("/api/overview", headers=tw.auth(tw.FRIEND))
        ov = await r.json()
        assert ov["copy"]["on"] and ov["copy"]["wallets"] == 1  # друг видит, но управлять не может
        status, d = await tw.post(client, "/api/copy", {"remove": W})
        assert d["wallets"] == []

    tw.call(b, fn)


app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env
