"""💸 Реальная торговля мемкоинами: ключ, подпись, Jupiter, автокопирование ⭐, стопы, лимиты, Telegram, приложение.

Сеть Solana и Jupiter подменены, но подпись настоящая: поддельный Jupiter проверяет её открытым ключом,
как это делает сеть."""

import base64
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from bot.copytrade import WSOL
from bot.memetrade import LAMPORTS, MemeTrader, MemeWallet, SwapError
from bot.solkey import (BadKey, Keypair, b58decode, b58encode, close_accounts_tx, looks_like_secret, message_keys,
                        shortvec_decode, shortvec_encode, sign_transaction)
from tests.test_copytrade import MINT, W, buy_tx, run, sell_tx, setup

SEED = bytes(range(32))
KP = Keypair(SEED)
SECRET = b58encode(SEED + KP.public)  # как «Показать приватный ключ» в Phantom
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
T22 = "TokenzQdBNbLqP5VEhdkAS6EPFNoz2z5Z7m8wVPVQEz"
RENT = 2_039_280


# --- ключ и подпись -----------------------------------------------------------------

def test_base58_and_shortvec_roundtrip():
    for data in (b"", b"\0", b"\0\0\x01", bytes(range(64)), KP.public):
        assert b58decode(b58encode(data)) == data
    assert b58encode(b58decode(WSOL)) == WSOL
    for n in (0, 1, 127, 128, 300, 16383, 16384):
        enc = shortvec_encode(n)
        assert shortvec_decode(enc, 0) == (n, len(enc))


def test_keypair_from_phantom_and_cli_formats_never_leaks_secret():
    assert Keypair.from_secret(SECRET).address == KP.address
    assert Keypair.from_secret(f'  "{SECRET}"\n').address == KP.address
    assert Keypair.from_secret(str(list(SEED + KP.public))).address == KP.address
    assert SECRET not in repr(Keypair.from_secret(SECRET)) and KP.address in repr(KP)
    broken = b58encode(SEED + bytes(32))
    for bad, words in ((broken, "повреждён"), (KP.address, "32 или 64"), ("0OIl", "лишние символы"),
                       ("", "пустой"), ("[1,2]", "64")):
        if bad == KP.address:
            Keypair.from_secret(bad)  # 32 байта = это seed, допустимо; адрес тоже 32 байта — это не опасно
            continue
        with pytest.raises(BadKey) as e:
            Keypair.from_secret(bad)
        assert words in str(e.value) and (not bad or bad not in str(e.value))


def unsigned_tx(signers: list[bytes], extra_keys: int = 1) -> bytes:
    keys = signers + [bytes([7] * 32)] * extra_keys
    msg = bytes([0x80, len(signers), 0, 0]) + shortvec_encode(len(keys)) + b"".join(keys) + bytes(32) + shortvec_encode(0) \
        + shortvec_encode(0)
    return shortvec_encode(len(signers)) + bytes(64 * len(signers)) + msg


def test_sign_puts_signature_in_own_slot_only():
    mm = bytes([9] * 32)
    for signers, idx in (([KP.public], 0), ([mm, KP.public], 1)):  # второй случай — как маркет-мейкер JupiterZ
        tx = unsigned_tx(signers)
        signed = sign_transaction(tx, KP)
        n, off = shortvec_decode(signed, 0)
        msg = signed[off + 64 * n:]
        Ed25519PublicKey.from_public_bytes(KP.public).verify(signed[off + 64 * idx: off + 64 * idx + 64], msg)
        others = [i for i in range(n) if i != idx]
        assert all(signed[off + 64 * i: off + 64 * i + 64] == bytes(64) for i in others)
    with pytest.raises(ValueError):
        sign_transaction(unsigned_tx([mm]), KP)  # нашего адреса нет — не подписываем чужое


def test_close_accounts_transaction_is_valid():
    accs = [(b58encode(bytes([1] * 32)), TOKEN), (b58encode(bytes([2] * 32)), T22)]
    raw = close_accounts_tx(KP, accs, b58encode(bytes([5] * 32)))
    n, off = shortvec_decode(raw, 0)
    msg = raw[off + 64:]
    Ed25519PublicKey.from_public_bytes(KP.public).verify(raw[off:off + 64], msg)
    required, keys = message_keys(msg)
    assert n == 1 and required == 1 and keys[0] == KP.public and len(keys) == 5
    solders = pytest.importorskip("solders.transaction")  # эталон, если установлен
    t = solders.Transaction.from_bytes(raw)
    assert t.verify() is None
    m = t.message
    for ix, (acc, prog) in zip(m.instructions, accs):
        assert str(m.account_keys[ix.program_id_index]) == prog and bytes(ix.data) == b"\x09"
        assert [str(m.account_keys[i]) for i in ix.accounts] == [acc, KP.address, KP.address]


def test_secret_detector():
    assert looks_like_secret(SECRET) and looks_like_secret("вот ключ: " + SECRET)
    assert looks_like_secret(str(list(SEED + KP.public)))
    assert looks_like_secret("abandon ability able about above absent absorb abstract absurd abuse access accident")
    assert not looks_like_secret(KP.address) and not looks_like_secret("купи BONK на 2 доллара")
    assert not looks_like_secret("hello how are you")


# --- поддельные Solana и Jupiter ----------------------------------------------------------

class Chain:
    """Кошелёк бота в «сети»: SOL, счета токенов. Цены — как у DexScreener в тестах копитрейдинга."""

    def __init__(self, dex, sol=0.1):
        self.dex = dex
        self.lamports = int(sol * LAMPORTS)
        self.accounts: dict[str, dict] = {}  # mint -> {raw, lamports, pubkey, program}
        self.sent = []

    def price(self, mint):
        return self.dex.pairs[mint].price_usd


class WalletRpc:
    def __init__(self, chain):
        self.chain = chain
        self.calls = []

    async def call(self, method, params):
        self.calls.append(method)
        c = self.chain
        if method == "getBalance":
            return {"value": c.lamports}
        if method == "getTokenAccountsByOwner":
            flt = params[1]
            accs = [(m, a) for m, a in c.accounts.items()
                    if flt.get("mint") in (None, m) and flt.get("programId") in (None, a["program"])]
            return {"value": [{"pubkey": a["pubkey"], "account": {"owner": a["program"], "lamports": a["lamports"], "data": {
                "parsed": {"info": {"mint": m, "tokenAmount": {"amount": str(a["raw"]), "decimals": 6}}}}}}
                for m, a in accs]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": b58encode(bytes([3] * 32))}}
        if method == "sendTransaction":
            raw = base64.b64decode(params[0])
            n, off = shortvec_decode(raw, 0)
            msg = raw[off + 64 * n:]
            Ed25519PublicKey.from_public_bytes(KP.public).verify(raw[off:off + 64], msg)  # подпись настоящая
            _, keys = message_keys(msg)
            for m, a in list(c.accounts.items()):
                if b58decode(a["pubkey"]) in keys and a["raw"] == 0:
                    c.lamports += a["lamports"]
                    del c.accounts[m]
            c.sent.append(raw)
            return "closeSig"
        raise AssertionError(method)


class FakeJupiter:
    def __init__(self, chain, fee=0.01):
        self.chain = chain
        self.fee = fee
        self.honeypot: set[str] = set()
        self.fail_sells = 0
        self.orders = []
        self.pending = {}

    async def close(self):
        pass

    def quote(self, input_mint, output_mint, amount):
        c = self.chain
        if input_mint == WSOL:
            usd = amount / LAMPORTS * 150
            return int(usd / c.price(output_mint) * 1e6 * (1 - self.fee))
        if input_mint in self.honeypot:
            raise SwapError("Jupiter: нет маршрута")
        usd = amount / 1e6 * c.price(input_mint)
        return int(usd / 150 * LAMPORTS * (1 - self.fee))

    async def order(self, input_mint, output_mint, amount, taker=None):
        out = self.quote(input_mint, output_mint, amount)
        self.orders.append((input_mint, output_mint, amount, taker))
        res = {"outAmount": str(out), "requestId": f"r{len(self.orders)}", "transaction": None}
        if taker:
            assert taker == KP.address
            res["transaction"] = base64.b64encode(unsigned_tx([KP.public])).decode()
            self.pending[res["requestId"]] = (input_mint, output_mint, amount, out)
        return res

    async def execute(self, signed_b64, request_id):
        raw = base64.b64decode(signed_b64)
        n, off = shortvec_decode(raw, 0)
        Ed25519PublicKey.from_public_bytes(KP.public).verify(raw[off:off + 64], raw[off + 64 * n:])
        input_mint, output_mint, amount, out = self.pending.pop(request_id)
        c = self.chain
        if input_mint != WSOL and self.fail_sells:
            self.fail_sells -= 1
            return {"status": "Failed", "error": "slippage exceeded", "code": -1, "signature": "failSig"}
        if input_mint == WSOL:
            c.lamports -= amount
            acc = c.accounts.setdefault(output_mint, {"raw": 0, "lamports": RENT, "pubkey": b58encode(bytes([len(c.accounts) + 1] * 32)),
                                                      "program": TOKEN})
            if acc["raw"] == 0 and acc["lamports"] == RENT:
                c.lamports -= 0  # аренду в тесте не моделируем при покупке
            acc["raw"] += out
        else:
            c.accounts[input_mint]["raw"] -= amount
            c.lamports += out
        return {"status": "Success", "signature": f"sig{request_id}", "inputAmountResult": str(amount),
                "outputAmountResult": str(out)}


def trader(ct, sol=0.1):
    chain = Chain(ct.dex, sol)
    rpc, jup = WalletRpc(chain), FakeJupiter(chain)
    mt = MemeTrader(ct, env={}, jup=jup, wallet=MemeWallet(KP, rpc, jup))
    return mt, chain, jup


def star(ct):
    w = ct.st["wallets"][W]
    w["status"], w["rets"] = "star", [12.0] * 6
    return w


# --- сделки ---------------------------------------------------------------------------

def test_env_key_configures_wallet_and_bad_key_explains():
    now = time.time()
    ct, _, _ = setup(now)
    assert MemeTrader(ct, env={"SOLANA_TRADER_KEY": SECRET}).address == KP.address
    bad = MemeTrader(ct, env={"SOLANA_TRADER_KEY": KP.address + "xx"})
    assert not bad.configured and "SOLANA_TRADER_KEY" in bad.key_error
    none = MemeTrader(ct, env={})
    assert not none.configured and none.key_error is None and "Как подключить" in none.text()
    with pytest.raises(SwapError):
        run(none.buy(MINT, 2))


def test_manual_buy_sell_with_pnl_and_rent_back():
    now = time.time()
    ct, _, dex = setup(now)
    mt, chain, jup = trader(ct)
    dex.set_price(MINT, 0.015)
    e = run(mt.buy(MINT, 3, symbol="MOODENG", now=now))
    pos = mt.st["positions"][MINT]
    assert "Купил MOODENG" in e["text"] and pos["sol_in"] == int(3 / 150 * LAMPORTS) and pos["raw"] > 0
    assert ("💸 Продать сейчас", f"cb:mt:s:{MINT}") in e["buttons"]
    assert chain.lamports == int(0.1 * LAMPORTS) - pos["sol_in"]
    assert [o[3] for o in jup.orders] == [None, None, KP.address]  # проверка туда-обратно, потом сделка
    with pytest.raises(SwapError, match="уже куплен"):
        run(mt.buy(MINT, 3))
    dex.set_price(MINT, 0.015 * 1.5)
    before = chain.lamports
    e = run(mt.sell(MINT, "вручную", now=now + 600))
    assert "Продал MOODENG" in e["text"] and "Вернул залог" in e["text"]
    assert not mt.st["positions"] and not chain.accounts and chain.lamports > before + RENT
    s = mt.st["stats"]
    assert s["sells"] == 1 and s["wins"] == 1 and 1.3 < s["pnl_usd"] < 1.5 and abs(s["rent_back_sol"] - RENT / LAMPORTS) < 1e-6
    assert mt.st["closed"][-1]["pct"] > 40


def test_buy_refuses_honeypot_thin_market_and_low_balance():
    now = time.time()
    ct, _, dex = setup(now)
    mt, chain, jup = trader(ct)
    jup.honeypot.add(MINT)
    with pytest.raises(SwapError, match="honeypot"):
        run(mt.buy(MINT, 2))
    jup.honeypot.clear()
    jup.fee = 0.04  # вход+выход ≈ 8%
    with pytest.raises(SwapError, match="тонкий рынок"):
        run(mt.buy(MINT, 2))
    jup.fee = 0.01
    with pytest.raises(SwapError, match="мало SOL"):
        run(mt.buy(MINT, 14))  # 0,1 SOL = $15, минус запас 0,01 SOL
    assert not mt.st["positions"] and chain.lamports == int(0.1 * LAMPORTS)
    assert all(o[3] is None for o in jup.orders)  # до настоящей сделки не дошло


def test_auto_copy_star_wallet_buys_and_sells_after_wallet():
    now = time.time()
    ct, rpc, dex = setup(now)
    mt, chain, jup = trader(ct)
    mt.st["on"] = True
    star(ct)
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    events = run(mt.after_copy_step(now + 100))
    assert len(events) == 1 and "копия ⭐ Кит" in events[0]["text"] and MINT in mt.st["positions"]
    assert mt.st["decisions"][-1]["text"] == "купил за ⭐ Кит"
    assert run(mt.after_copy_step(now + 101)) == []  # одно событие — одна покупка
    dex.set_price(MINT, 0.0151 * 1.2)
    rpc.push(W, sell_tx("s1", now + 150))
    run(ct.step(now + 200))
    events = run(mt.after_copy_step(now + 200))
    assert any("Продал MOODENG" in e["text"] and "кошелёк продал" in e["text"] for e in events)
    assert not mt.st["positions"] and mt.st["stats"]["wins"] == 1


def test_auto_copy_scope_filter_and_limits():
    now = time.time()
    ct, rpc, dex = setup(now)
    mt, chain, jup = trader(ct)
    mt.st["on"] = True
    dex.set_price(MINT, 0.0151)
    rpc.push(W, buy_tx("b1", now - 30))
    run(ct.step(now + 100))
    assert run(mt.after_copy_step(now + 100)) == [] and not mt.st["positions"]  # 👀 без ⭐ — не покупаем
    assert not mt.st["decisions"]
    # расширили до «хороших» — но лимит позиций 0 мест
    mt.st["scope"] = "good"
    ct.st["wallets"][W]["hist"] = {"grade": "good"}
    mt.st["max_open"] = 0
    from tests.test_copytrade import MINT2
    dex.set_price(MINT2, 0.0151)
    rpc.push(W, buy_tx("b2", now + 110, mint=MINT2))
    run(ct.step(now + 120))
    run(mt.after_copy_step(now + 120))
    assert "из 0 позиций" in mt.st["decisions"][-1]["text"] and not mt.st["positions"]
    # пауза после убытков и дневной лимит
    mt.st["max_open"] = 2
    pos = {"passed": True, "mint": "X", "reasons": []}
    mt.st["paused_until"] = now + 100
    assert "пауза" in mt._why_not(pos, now)
    mt.st["paused_until"] = 0
    mt._roll_day(now)
    mt.st["day_pnl_usd"] = -2.0
    assert "дневной лимит" in mt._why_not(pos, now)
    assert "риск-фильтр" in mt._why_not({"passed": False, "mint": "X", "reasons": ["ликвидность $9K < $20K"]}, now)


def test_guard_stop_loss_retry_failed_sell_and_pause_after_losses():
    now = time.time()
    ct, _, dex = setup(now)
    mt, chain, jup = trader(ct, sol=0.5)
    dex.set_price(MINT, 0.015)
    run(mt.buy(MINT, 2, symbol="MOODENG", now=now))
    assert run(mt.guard(now + 90)) == [] and mt.st["positions"][MINT]["value"] > 0
    dex.set_price(MINT, 0.015 * 0.5)  # −50% → стоп −40%
    jup.fail_sells = 1
    events = run(mt.guard(now + 180))
    assert "Не смог продать MOODENG" in events[0]["text"] and "Phantom" in events[0]["buttons"][0][0]
    assert mt.st["positions"][MINT]["sell_why"] == "стоп −40%"
    events = run(mt.guard(now + 270))  # повтор без новой котировки
    assert "Продал MOODENG" in events[0]["text"] and "стоп −40%" in events[0]["text"]
    assert mt.st["streak"] == 1
    for k in range(2):  # ещё два убытка → пауза
        dex.set_price(MINT, 0.015)
        run(mt.buy(MINT, 1, symbol="MOODENG", now=now + 300 + k))
        dex.set_price(MINT, 0.012)
        e = run(mt.sell(MINT, "вручную", now=now + 400 + k))
    assert "на паузе 12 часов" in e["text"] and mt.st["paused_until"] > now


def test_copy_position_sold_after_48h_or_when_paper_copy_disappears_and_manual_sale_detected():
    now = time.time()
    ct, _, dex = setup(now)
    mt, chain, jup = trader(ct, sol=0.5)
    dex.set_price(MINT, 0.015)
    run(mt.buy(MINT, 2, source="copy", key=f"{W}|{MINT}", wallet_name="Кит", now=now))
    events = run(mt.guard(now + 60))  # бумажной копии нет (кошелёк убрали) — выходим
    assert "копия уже закрыта" in events[0]["text"]
    run(mt.buy(MINT, 2, symbol="M", now=now))
    chain.accounts[MINT]["raw"] = 0  # продал сам в Phantom
    e = run(mt.sell(MINT, "вручную"))
    assert "продал вручную" in e["text"] and not mt.st["positions"]


def test_lost_execute_answer_still_tracks_tokens():
    now = time.time()
    ct, _, dex = setup(now)
    mt, chain, jup = trader(ct, sol=0.5)
    mt.CHECK_DELAY = 0
    dex.set_price(MINT, 0.015)
    real_execute = jup.execute

    async def lost(signed, rid):
        await real_execute(signed, rid)  # сделка прошла в сети…
        raise SwapError("Jupiter не отвечает (TimeoutError)")  # …а ответ потерялся

    jup.execute = lost
    e = run(mt.buy(MINT, 2, symbol="MOODENG", now=now))
    assert "Купил MOODENG" in e["text"] and mt.st["positions"][MINT]["raw"] == chain.accounts[MINT]["raw"]

    async def failed(signed, rid):
        return {"status": "Failed", "error": "slippage", "code": -1}

    jup.execute = failed
    with pytest.raises(SwapError, match="не прошла"):
        run(mt.buy(fm_wif(), 2))


def fm_wif():
    from tests.test_copytrade import MINT2
    return MINT2


def test_state_lives_inside_copytrade_backup():
    now = time.time()
    ct, _, dex = setup(now)
    mt, chain, jup = trader(ct)
    mt.st["size_usd"] = 3.0
    dex.set_price(MINT, 0.015)
    run(mt.buy(MINT, 2, symbol="MOODENG", now=now))
    saved = __import__("json").loads(__import__("json").dumps(ct.export()))
    ct2, _, _ = setup(now)
    ct2.restore(saved)
    mt2 = MemeTrader(ct2, env={}, jup=jup, wallet=mt.wallet)
    assert mt2.st["size_usd"] == 3.0 and MINT in mt2.st["positions"]
    ct2.restore({"on": True})  # старый бэкап без «real» — значения по умолчанию
    assert mt2.st["size_usd"] == 2.0 and mt2.st["positions"] == {}
    d = mt.status_dict()
    assert d["configured"] and d["positions"][0]["symbol"] == "MOODENG" and d["address"] == KP.address


# --- Telegram ---------------------------------------------------------------------------

def texts(session, n=3):
    return [t for _, t in session.sent[-n:]]


def test_telegram_meme_flow_and_secret_guard(bot_env):
    from aiogram.methods import DeleteMessage

    from bot import copy_ui
    from tests.test_ai_flow import buttons, press
    from tests.test_telegram_flow import send
    b, session = bot_env
    now = time.time()
    ct, rpc, dex = setup(now)
    b.copy = ct
    b.meme = MemeTrader(ct, env={})
    run(send(b, "/meme"))
    assert "SOLANA_TRADER_KEY" in session.sent[-1][1]
    # ключ в чате: удаляем и предупреждаем, ИИ его не видит
    run(send(b, SECRET))
    assert any(isinstance(m, DeleteMessage) for m in session.calls) and "засвеченным" in session.sent[-1][1]

    mt, chain, jup = trader(ct, sol=0.5)
    b.meme = mt
    dex.set_price(MINT, 0.015)
    run(send(b, f"/meme buy https://dexscreener.com/solana/{MINT} 2"))
    assert any("Купил MOODENG" in t for t in texts(session)) and MINT in mt.st["positions"]
    assert f"mt:s:{MINT}" in buttons(session.markups[-1])
    run(send(b, "/meme"))
    kb = session.markups[-1]
    assert "Кошелёк бота" in session.sent[-1][1] and f"mt:s:{MINT}" in buttons(kb) and "mt:on" in buttons(kb)
    run(press(b, "mt:on"))
    assert "Включить реальное автокопирование" in session.sent[-1][1] and not mt.st["on"]
    run(press(b, "mt:on2"))
    assert mt.st["on"] and ct.on and "включено" in session.sent[-1][1]
    run(press(b, "mt:size:5"))
    assert mt.st["size_usd"] == 5.0
    run(press(b, f"mt:s:{MINT}"))
    assert any("Продал MOODENG" in t for t in texts(session)) and not mt.st["positions"]
    run(send(b, "/meme sell all"))
    assert "Позиций нет" in session.sent[-1][1]
    # сигнал ⭐ получает кнопку «Купить ботом»
    star(ct)
    mt.st["on"] = False
    ct.st["discovered_at"] = time.time()
    rpc.push(W, buy_tx("b9", time.time() - 20))
    run(copy_ui.copy_tick(b))
    assert any("mt:b:" + MINT in buttons(m) for m in session.markups[-3:])
    assert "Мемкоины (реально)" in copy_ui.short_line(b)
    assert b.ai_context()["extras"]["copytrading_solana"]["real_trading"]["connected"]


def test_stranger_cannot_trade(bot_env):
    from tests.test_ai_flow import press
    from tests.test_telegram_flow import OWNER, send
    b, session = bot_env
    FRIEND = 222
    b.allowed_ids = {OWNER, FRIEND}
    ct, _, dex = setup(time.time())
    b.copy = ct
    mt, chain, jup = trader(ct)
    b.meme = mt
    run(send(b, f"/meme buy {MINT}", user=FRIEND))
    assert "только владелец" in session.sent[-1][1] and not jup.orders
    run(press(b, "mt:on2", FRIEND))
    assert not mt.st["on"]


# --- приложение ---------------------------------------------------------------------------

def test_webapp_meme_api(app_env):
    from tests import test_webapp as tw
    b, session = app_env
    ct, _, dex = setup(time.time())
    b.copy = ct
    b.meme = MemeTrader(ct, env={})

    async def not_configured(client):
        status, d = await tw.get(client, "/api/copy")
        assert status == 200 and not d["real"]["configured"]
        status, d = await tw.post(client, "/api/meme", {"on": True})
        assert status == 400 and "SOLANA_TRADER_KEY" in d["error"]

    tw.call(b, not_configured)
    mt, chain, jup = trader(ct, sol=0.5)
    b.meme = mt
    dex.set_price(MINT, 0.015)

    async def fn(client):
        status, d = await tw.get(client, "/api/copy")
        assert d["real"]["configured"] and d["real"]["balance_sol"] == 0.5 and d["real"]["address"] == KP.address
        status, d = await tw.get(client, "/api/copy", uid=tw.FRIEND)
        assert "address" not in d["real"] and "balance_sol" not in d["real"]
        status, d = await tw.post(client, "/api/meme", {"on": True}, uid=tw.FRIEND)
        assert status == 403 and not mt.st["on"]
        status, d = await tw.post(client, "/api/meme", {"on": True, "size": 3, "scope": "good"})
        assert status == 200 and d["real"]["on"] and d["real"]["size_usd"] == 3 and d["real"]["scope"] == "good"
        assert ct.on  # копитрейдинг включился вместе с реальной торговлей
        status, d = await tw.post(client, "/api/meme", {"buy": "nope"})
        assert status == 400 and "адрес токена" in d["error"]
        status, d = await tw.post(client, "/api/meme", {"buy": MINT})
        assert status == 200 and d["real"]["positions"][0]["symbol"] == "MOODENG" and "Купил MOODENG" in d["done"][0]
        r = await client.get("/api/overview", headers=tw.auth(tw.OWNER))
        assert (await r.json())["copy"]["real"]["positions"] == 1
        status, d = await tw.post(client, "/api/meme", {"buy": MINT})
        assert status == 400 and "уже куплен" in d["error"]
        status, d = await tw.post(client, "/api/meme", {"sell": MINT})
        assert status == 200 and not d["real"]["positions"] and "Продал MOODENG" in d["done"][0]
        assert d["real"]["closed"][0]["symbol"] == "MOODENG"

    tw.call(b, fn)


app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env
