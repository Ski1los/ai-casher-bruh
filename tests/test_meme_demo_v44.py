"""v44: 🧪 демо мемкоинов действительно торгует.

Раньше демо копировало только ⭐ (их нужно заслужить 5+ копиями) и 👀 с историей «в плюсе» — на практике сделок
не было неделями. Теперь демо учится и на 🔍 новых кошельках (пока копии не докажут убыток), кошельки ищутся ещё
и в «умных деньгах»/KOL GMGN, сбой котировки Jupiter не считается ловушкой, а экран объясняет, почему сделок нет.
"""

import time

from bot import solana
from bot.copytrade import CopyTrader
from bot.memetrade import NEW_MIN, TOKEN_PROGRAMS, SwapError
from tests.test_copytrade import MINT, MINT2, W, FakeDex, FakeRpc, buy_tx, run, setup
from tests.test_meme_demo import demo_trader
from tests.test_memetrade import trader

SMART = "Smart22222222222222222222222222222222222222"
KOL = "Ko1111111111111111111111111111111111111111"


def test_demo_copies_new_wallet_but_real_account_does_not():
    now = time.time()
    ct, rpc, dex = setup(now)
    assert (ct.st["wallets"][W].get("hist") or {}).get("grade", "unknown") == "unknown"  # 🔍 история неизвестна
    mt, chain, jup = trader(ct, sol=1.0)  # настоящий кошелёк подключён и пополнен
    mt.st["on"] = True
    mt.start_demo_if_new()
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    run(mt.after_copy_step(now + 100))
    assert MINT in mt.demo["positions"] and mt.demo["positions"][MINT]["category"] == "new"
    assert MINT not in mt.st["positions"]  # настоящие деньги — только за ⭐ (или 👀 по кнопке)
    assert not any(o[3] for o in jup.orders)  # ни одной настоящей транзакции
    assert "🔍 новые кошельки" in mt.demo_text()
    assert mt.demo_dict()["positions"][0]["category"] == "new"


def test_demo_stops_copying_new_wallets_when_they_prove_losing():
    now = time.time()
    ct, rpc, dex = setup(now)
    mt, _ = demo_trader(ct)
    ct.st["closed"] += [{"wallet": W, "mint": MINT, "symbol": "X", "ret": -12.0, "why": "стоп-лосс", "opened": now,
                         "closed": now, "feats": {}, "passed": True, "wstatus": "watch", "wgrade": None}
                        for _ in range(NEW_MIN)]
    events = mt.learn(now)
    lr = mt.st["learn"]
    assert lr["new_ok"] is False and lr["new_n"] == NEW_MIN and lr["new_avg"] < -1
    assert any("новых кошельков теряют" in e["text"] for e in events)
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    run(mt.after_copy_step(now + 100))
    assert MINT not in mt.demo["positions"]
    assert "без 🔍 новых" in mt.demo_scope_text()


class FlakyJupiter:
    """Покупка котируется, а обратная котировка падает из-за лимита — это не ловушка."""

    def __init__(self, inner, fail_back=1, no_route=False):
        self.inner, self.fail_back, self.no_route = inner, fail_back, no_route

    async def close(self):
        pass

    async def order(self, input_mint, output_mint, amount, taker=None):
        if input_mint != "So11111111111111111111111111111111111111112":
            if self.no_route:
                raise SwapError("Jupiter: нет маршрута")
            if self.fail_back:
                self.fail_back -= 1
                raise SwapError("Jupiter просит подождать (лимит запросов)")
        return await self.inner.order(input_mint, output_mint, amount, taker)


def test_jupiter_rate_limit_is_not_a_honeypot():
    now = time.time()
    ct, _, dex = setup(now)
    mt, jup = demo_trader(ct)
    dex.set_price(MINT, 0.0151)
    mt.jup = FlakyJupiter(jup, fail_back=1)  # одна неудача — повтор проходит
    e = run(mt.demo_buy(MINT, 2, now=now))
    assert "купил" in e["text"] and mt.demo["positions"][MINT]["src"] == "jupiter"
    mt.jup = FlakyJupiter(jup, fail_back=5)  # Jupiter совсем не отвечает — считаем по DexScreener
    dex.set_price(MINT2, 2.0)
    e = run(mt.demo_buy(MINT2, 2, now=now))
    assert mt.demo["positions"][MINT2]["src"] == "dex"
    mt.jup = FlakyJupiter(jup, no_route=True)  # обратного маршрута действительно нет — это ловушка
    mt.demo["positions"].clear()
    try:
        run(mt.demo_buy(MINT, 2, now=now))
        raise AssertionError("должен отказаться")
    except SwapError as err:
        assert "honeypot" in str(err)


class FakeGmgn:
    configured = True

    def __init__(self, now):
        self.now = now

    async def smartmoney(self, chain="sol", limit=100):
        return [{"mint": MINT, "symbol": "MOODENG", "maker": SMART, "side": "buy", "usd": 300.0, "at": self.now - 60,
                 "who": ""},
                {"mint": MINT, "symbol": "MOODENG", "maker": "bad", "side": "buy", "usd": 1.0, "at": self.now, "who": ""},
                {"mint": MINT2, "symbol": "WIF", "maker": SMART, "side": "sell", "usd": 50.0, "at": self.now, "who": ""}]

    async def kol(self, chain="sol", limit=100):
        return [{"mint": MINT2, "symbol": "WIF", "maker": KOL, "side": "buy", "usd": 90.0, "at": self.now - 120,
                 "who": "ansem"},
                {"mint": MINT2, "symbol": "WIF", "maker": W, "side": "buy", "usd": 9.0, "at": self.now - 7200,
                 "who": "old"}]


def test_discover_takes_gmgn_smart_money_even_when_solana_node_fails():
    now = time.time()
    rpc, dex = FakeRpc(), FakeDex()

    async def broken(method, params):
        raise solana.WalletError("узел Solana просит подождать")
    rpc.call = broken
    ct = CopyTrader(dex, rpc)
    ct.gmgn = FakeGmgn(now)
    report = run(ct.discover(now, force=True))
    assert SMART in ct.st["wallets"] and KOL in ct.st["wallets"] and W not in ct.st["wallets"]
    assert "GMGN" in ct.st["wallets"][SMART]["name"] and "ansem" in ct.st["wallets"][KOL]["name"]
    assert ct.st["wallets"][SMART]["hist"] is None  # судить будем по копиям без денег
    assert any(r.startswith("➕") for r in report)
    # кошельков мало — следующий поиск через 20 минут, а не через 2 часа
    assert run(ct.discover(now + 60)) == []
    ct.gmgn = None
    assert run(ct.discover(now + 21 * 60)) is not None and ct.st["discovered_at"] == now + 21 * 60


def test_demo_explains_why_there_are_no_trades():
    now = time.time()
    rpc, dex = FakeRpc(), FakeDex()
    ct = CopyTrader(dex, rpc)
    mt, _ = demo_trader(ct)
    assert "ещё нет кошельков" in mt.demo_idle(now)
    assert "GMGN_API_KEY" in mt.demo_idle(now)
    ct.gmgn = FakeGmgn(now)
    assert "умные деньги GMGN" in mt.demo_idle(now)
    ct, rpc, dex = setup(now)
    mt, _ = demo_trader(ct)
    assert "ничего не купили" in mt.demo_idle(now)
    ct.st["closed"].append({"wallet": W, "mint": MINT, "symbol": "X", "ret": -5.0, "opened": now - 60,
                            "closed": now, "passed": False, "hard_fail": False,
                            "feats": {"liq": 3000.0, "rug": 10, "age_h": 5, "chase": 0.0, "h1": 5}})
    assert "мало ликвидности ×1" in mt.demo_idle(now)
    assert "Почему пока нет сделок" in mt.demo_text() and mt.demo_dict()["idle"]
    ct.st["on"] = False
    assert "копитрейдинг выключен" in mt.demo_idle(now)
    mt.demo["on"] = False
    assert mt.demo_idle(now) is None


def test_token_2022_program_id_is_correct():
    assert TOKEN_PROGRAMS == solana.TOKEN_PROGRAMS
