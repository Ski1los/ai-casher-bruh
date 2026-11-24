"""v42: 🔁 переводы между биржами — анализ перед выводом и перед покупкой, лимиты, слежение, автобаланс."""

from __future__ import annotations

import asyncio
import time

import pytest

from bot import transfers as tr
from bot import users_ui
from bot.trading import TradeError
from tests.fake_exchange import FakeTradingExchange, mk_market
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import send

FRIEND = 777
BYBIT_ADDR = "TBybitDepositAddr0000000001"
BINGX_ADDR = "TBingxDepositAddr0000000002"


def net(network, fee, withdraw=True, deposit=True, mn=1.0):
    return {"network": network, "fee": fee, "withdraw": withdraw, "deposit": deposit, "active": True,
            "limits": {"withdraw": {"min": mn}}, "info": {"big": "x" * 50}}


class Wallet(FakeTradingExchange):
    """Фейковая биржа с кошельком: статус монет, адреса пополнения, выводы, депозиты, внутренние переводы."""

    def __init__(self, name, nets_by_coin, address, balances, funding=None):
        markets = {"SOL/USDT": mk_market("SOL", "USDT"), "LUNA/USDT": mk_market("LUNA", "USDT")}
        tickers = {"SOL/USDT": {"bid": 150, "ask": 150.1, "last": 150, "quoteVolume": 1e9},
                   "LUNA/USDT": {"bid": 0.3, "ask": 0.301, "last": 0.3, "quoteVolume": 1e7}}
        super().__init__(name, markets, tickers, balances)
        self.nets = nets_by_coin
        self.address = address
        self.funding = dict(funding or {})
        self.withdrawn: list[tuple] = []
        self.withdrawals: list[dict] = []
        self.deposits: list[dict] = []
        self.moved: list[tuple] = []
        self.currency_calls = 0

    async def fetch_currencies(self, params=None):
        self.currency_calls += 1
        out = {}
        for coin, spec in self.nets.items():
            nets = spec.get("nets", {})
            out[coin] = {"id": coin, "code": coin, "active": spec.get("active", True),
                         "withdraw": spec.get("withdraw", True), "deposit": spec.get("deposit", True),
                         "networks": dict(nets), "info": {"raw": "y" * 50}}
        return out

    async def fetch_balance(self, params=None):
        if (params or {}).get("type") == "funding":
            return {"free": dict(self.funding)}
        return await super().fetch_balance(params)

    async def fetch_deposit_address(self, code, params=None):
        return {"address": self.address, "tag": "MEMO1" if code == "TON" else None, "network": (params or {}).get("network")}

    async def withdraw(self, code, amount, address, params=None):
        self.withdrawn.append((code, amount, address, dict(params or {})))
        self.balance[code] = self.balance.get(code, 0) - amount
        wid = f"w{len(self.withdrawn)}"
        self.withdrawals.append({"id": wid, "status": "pending", "txid": None, "amount": amount})
        return {"id": wid}

    async def fetch_withdrawals(self, code=None, since=None, limit=None, params=None):
        return list(self.withdrawals)

    async def fetch_deposits(self, code=None, since=None, limit=None, params=None):
        return list(self.deposits)

    async def transfer(self, code, amount, from_acc, to_acc, params=None):
        self.moved.append((code, amount, from_acc, to_acc))
        self.funding[code] = self.funding.get(code, 0) - amount
        return {"id": "t1"}


def usdt(**over):
    base = {"nets": {"TRX": net("TRX", 1.0), "ETH": net("ETH", 5.0), "SOL": net("SOL", 0.8, withdraw=False)}}
    base.update(over)
    return base


def setup(b, allowed=True, bybit_bal=None, bingx_bal=None, luna_withdraw=False):
    bybit = Wallet("Bybit", {"USDT": usdt(), "LUNA": {"withdraw": luna_withdraw, "nets": {"LUNA": net("LUNA", 0.1, withdraw=luna_withdraw)}},
                             "SOL": {"nets": {"SOL": net("SOL", 0.01, mn=0.02)}}},
                   BYBIT_ADDR, bybit_bal if bybit_bal is not None else {"USDT": 100.0, "SOL": 1.0, "LUNA": 50.0})
    bingx = Wallet("BingX", {"USDT": {"nets": {"TRC20": net("TRC20", 1.5), "ERC20": net("ERC20", 4.0),
                                               "SOL": net("SOL", 1.0)}},
                             "LUNA": {"nets": {"Terra": net("Terra", 0.1)}},
                             "SOL": {"nets": {"SOL": net("SOL", 0.01)}}},
                   BINGX_ADDR, bingx_bal if bingx_bal is not None else {"USDT": 3.0})
    b.scanner.exchanges = {"bybit": bybit, "bingx": bingx}
    b.trader.creds = {"bybit": {"apiKey": "k", "secret": "s"}, "bingx": {"apiKey": "k", "secret": "s"}}
    for n, c in b.trader.creds.items():
        b.trader._apply_creds(n, c)
    b.trader.key_status = {}
    b.trader.key_info = {"bybit": {"withdraw": True, "spot_trade": True, "ip_bound": True},
                         "bingx": {"withdraw": True, "spot_trade": True, "ip_bound": True}}
    b.transfers.st["allowed"] = allowed
    return bybit, bingx


def checks(plan, status):
    return [t for s, t in plan.checks if s == status]


def test_net_names_match_between_exchanges():
    assert tr.canon_net("TRX") == tr.canon_net("TRC20") == tr.canon_net("tron") == "TRC20"
    assert tr.canon_net("BSC") == tr.canon_net("BEP20(BSC)") == "BEP20"
    assert tr.canon_net("Terra") == tr.canon_net("LUNA")
    assert tr.mask(BYBIT_ADDR) == "TBybit…0001"


def test_analyze_picks_cheapest_open_common_network(bot_env):
    b, _ = bot_env
    bybit, _bingx = setup(b)
    plan = asyncio.run(b.transfers.analyze("usdt", 20, "bybit"))  # «куда» — вторая биржа сама
    assert plan.ok, plan.checks
    assert plan.dst == "bingx" and plan.network == "TRC20"  # SOL дешевле, но вывод там закрыт
    assert (plan.src_net, plan.dst_net, plan.fee) == ("TRX", "TRC20", 1.0)
    assert plan.address == BINGX_ADDR and plan.usd == pytest.approx(20)
    assert any("адресную книгу" in t or "адресной книги" in t or "адресов" in t for t in checks(plan, "warn"))
    # статус монеты обновлён и для остального бота — без сырого info
    assert "info" not in bybit.currencies["USDT"] and "info" not in bybit.currencies["USDT"]["networks"]["TRX"]
    # кэш: повторный анализ не дёргает биржу
    calls = bybit.currency_calls
    asyncio.run(b.transfers.analyze("USDT", 10, "bybit", "bingx"))
    assert bybit.currency_calls == calls


def test_terra_like_block_is_caught_before_withdraw(bot_env):
    b, _ = bot_env
    bybit, _ = setup(b)
    plan = asyncio.run(b.transfers.analyze("LUNA", 10, "bybit", "bingx"))
    assert not plan.ok
    bad = " ".join(checks(plan, "bad"))
    assert "закрыт самой биржей" in bad and "Terra" in bad
    assert bybit.withdrawn == []


def test_every_reason_to_refuse(bot_env):
    b, _ = bot_env
    setup(b)
    t = b.transfers

    def bad(*a, **kw):
        return " ".join(checks(asyncio.run(t.analyze(*a, **kw)), "bad"))

    assert "больше лимита" in bad("USDT", 50, "bybit", "bingx")  # $30 за раз
    assert "нужно" in bad("USDT", 99.5, "bybit", "bingx")  # 99.5 + комиссия 1 > 100
    assert "минимума" in bad("SOL", 0.015, "bybit", "bingx")
    assert "комиссия" in bad("USDT", 2, "bybit", "bingx")  # 1 USDT из 2 = 50%
    t.st["day_sent"] = 90
    assert "в сутки" in bad("USDT", 20, "bybit", "bingx")
    t.st["day_sent"] = 0
    b.trader.key_info["bybit"]["withdraw"] = False
    assert "нет права вывода" in bad("USDT", 20, "bybit", "bingx")
    b.trader.key_info["bybit"]["withdraw"] = True
    t.st["allowed"] = False
    assert "не разрешены" in bad("USDT", 20, "bybit", "bingx")
    t.st["allowed"] = True
    del b.trader.creds["bingx"]
    assert "нет API-ключа" in bad("USDT", 20, "bybit", "bingx")


def test_all_amount_leaves_room_for_fee(bot_env):
    b, _ = bot_env
    setup(b, bybit_bal={"USDT": 25.0})
    plan = asyncio.run(b.transfers.analyze("USDT", "all", "bybit", "bingx"))
    assert plan.ok and plan.amount == pytest.approx(24.0)


def test_telegram_flow_allow_check_send_track(bot_env):
    b, session = bot_env
    bybit, bingx = setup(b, allowed=False)
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def run():
        await send(b, "/transfer")
        assert "Переводы между биржами" in session.sent[-1][1]
        assert "xf:allow" in buttons(session.markups[-1])
        await press(b, "xf:allow", user=FRIEND)  # приглашённый не может разрешить вывод
        assert not b.transfers.st["allowed"]
        await press(b, "xf:allow")
        assert "Разрешить боту выводить" in session.sent[-1][1]
        await press(b, "xf:allow2")
        assert b.transfers.st["allowed"]

        await send(b, "/transfer 20 USDT с bybit на bingx")
        text = session.sent[-1][1]
        assert "Анализ перед выводом" in text and "TRC20" in text and "TBingx…0002" in text
        kb = buttons(session.markups[-1])
        go = next(x for x in kb if x.startswith("xf:go:"))
        await press(b, go)
        assert bybit.withdrawn == [("USDT", 20.0, BINGX_ADDR, {"network": "TRX", "accountType": "FUND,UTA"})]
        assert "Вывод отправлен" in session.sent[-1][1]
        assert b.transfers.st["day_sent"] == pytest.approx(20)
        await press(b, go)  # повторное нажатие не выводит второй раз
        assert len(bybit.withdrawn) == 1 and "устарела" in session.sent[-1][1]

        # слежение: вывод прошёл → депозит пришёл на Funding → бот переносит на торговый счёт
        bybit.withdrawals[0].update(status="ok", txid="0xabc")
        bingx.deposits.append({"status": "ok", "amount": 19.0})
        bingx.funding["USDT"] = 19.0
        msgs = await b.transfers.track()
        assert len(msgs) == 1 and "Перевод дошёл" in msgs[0] and "торговый счёт" in msgs[0]
        assert bingx.moved == [("USDT", 19.0, "funding", "spot")]
        assert b.transfers.st["items"][-1]["status"] == "done"
        assert await b.transfers.track() == []

        await send(b, "/transfer check LUNA")
        assert "⛔ вывод закрыт биржей" in session.sent[-1][1]

    asyncio.run(run())


def test_recheck_right_before_withdraw(bot_env):
    b, _ = bot_env
    bybit, _ = setup(b)
    t = b.transfers

    async def run():
        plan = await t.analyze("USDT", 20, "bybit", "bingx")
        assert plan.ok
        t.keep(plan)
        bybit.nets["USDT"]["nets"]["TRX"] = net("TRX", 1.0, withdraw=False)  # за минуту сеть закрыли
        bybit.nets["USDT"]["nets"]["ETH"] = net("ETH", 5.0, withdraw=False)
        with pytest.raises(TradeError, match="проверил ещё раз"):
            await t.execute(plan.id)
        assert bybit.withdrawn == []

    asyncio.run(run())


def test_failed_withdraw_is_reported(bot_env):
    b, _ = bot_env
    bybit, _ = setup(b)

    async def run():
        plan = await b.transfers.analyze("USDT", 20, "bybit", "bingx")
        b.transfers.keep(plan)
        await b.transfers.execute(plan.id)
        bybit.withdrawals[0]["status"] = "failed"
        msgs = await b.transfers.track()
        assert "не прошёл" in msgs[0] and b.transfers.st["items"][-1]["status"] == "failed"

    asyncio.run(run())


def test_withdraw_errors_explained():
    plan = tr.TransferPlan("x", "USDT", 20, "bybit", "bingx", address=BINGX_ADDR)
    assert "адресную книгу" in tr.withdraw_error(plan, Exception("bybit {retCode:131002, address not in whitelist}"))
    assert "права вывода" in tr.withdraw_error(plan, Exception("permission denied"))


def test_parse_args(bot_env):
    b, _ = bot_env
    setup(b)
    assert tr.parse_args(b, ["USDT", "20", "bybit", "bingx"]) == ("USDT", 20.0, "bybit", "bingx")
    assert tr.parse_args(b, ["5,5", "sol", "с", "байбит"]) == ("SOL", 5.5, "bybit", None)
    assert tr.parse_args(b, ["usdt", "всё", "bingx", "bybit"]) == ("USDT", "all", "bingx", "bybit")
    with pytest.raises(TradeError):
        tr.parse_args(b, ["USDT"])


def test_limit_command_main_owner_only(bot_env):
    b, session = bot_env
    setup(b)
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def run():
        await send(b, "/transfer limit 50 150", user=FRIEND)
        assert b.transfers.st["max_usd"] == 30
        await send(b, "/transfer limit 50 150")
        assert (b.transfers.st["max_usd"], b.transfers.st["day_usd"]) == (50, 150)
        await send(b, "/transfer limit 500 100")  # за раз больше, чем в сутки — нельзя
        assert b.transfers.st["max_usd"] == 50

    asyncio.run(run())


def test_auto_rebalance(bot_env):
    b, _ = bot_env
    bybit, _ = setup(b)  # Bybit 100 USDT, BingX 3 USDT
    bybit.nets["USDT"]["nets"]["TRX"] = net("TRX", 0.1)
    t = b.transfers
    b.trader.state.auto = True
    b.settings.max_trade_usd = 10

    async def run():
        assert await t.auto_step() is None  # автобаланс выключен
        t.st["auto"] = True
        plan = await t.auto_step()
        assert plan is not None and plan.ok and plan.src == "bybit" and plan.dst == "bingx"
        assert bybit.withdrawn[0][1] == pytest.approx(7.0)  # до размера сделки $10
        assert await t.auto_step() is None  # не чаще раза в 12 часов
        t.st["last_auto"] = time.time() - tr.AUTO_EVERY_SEC - 1
        assert await t.auto_step() is None  # прошлый перевод ещё в пути — второй не шлём
        assert len(bybit.withdrawn) == 1
        # дорогая комиссия — автобаланс не переводит
        t.st["items"][-1]["status"] = "done"
        bybit.nets["USDT"]["nets"]["TRX"] = net("TRX", 1.0)
        bybit.nets["USDT"]["nets"]["ETH"] = net("ETH", 5.0)
        t._cur.clear()
        t.st["last_auto"] = 0
        plan = await t.auto_step()
        assert plan is not None and not plan.ok and len(bybit.withdrawn) == 1

    asyncio.run(run())


def test_before_buy_coin_wall(bot_env):
    b, _ = bot_env
    bybit, _ = setup(b)
    t = b.trader
    asyncio.run(b.transfers.currency("bybit", "LUNA"))  # статус монеты в кэше ccxt
    assert "закрыла вывод" in t.coin_wall("bybit", "LUNA")
    assert t.coin_wall("bybit", "USDT") is None and t.coin_wall("bingx", "XYZ") is None

    async def run():
        plan = await t.plan_market("bybit", "buy", "LUNA", "5")
        assert any("закрыла вывод" in w for w in plan.warnings)  # вручную — предупреждение
        with pytest.raises(TradeError, match="анализ перед покупкой"):
            await t.plan_market("bybit", "buy", "LUNA", "5", source="ai")  # ИИ такое не предложит

    asyncio.run(run())
    # автопилот такую монету не берёт
    sc = b.scalper
    sc.ranking = {"bybit": [{"symbol": "LUNA/USDT", "tp": 1.0, "rsi_buy": 30, "strategy": "dip", "net_pct": 2.0,
                              "verdict": "ok", "trades": 10, "win": 0.7}]}
    picked, held = sc.candidates("bybit", time.time())
    assert not any(r["symbol"] == "LUNA/USDT" for r, _, _ in picked)
    assert any("LUNA" in h and "закрыла вывод" in h for h in held)


def test_key_warnings_follow_transfer_permission(bot_env):
    b, _ = bot_env
    setup(b, allowed=False)
    t = b.trader
    assert any("переводы выключены" in w for w in t.key_warnings())
    b.transfers.st["allowed"] = True
    assert t.key_warnings() == []
    t.key_info["bybit"]["ip_bound"] = False
    assert any("без привязки к IP" in w for w in t.key_warnings())
    # /stop снимает разрешение на переводы
    b.transfers.st["auto"] = True
    asyncio.run(t.panic())
    assert not b.transfers.st["allowed"] and not b.transfers.st["auto"]


def test_transfer_state_survives_restart(bot_env):
    b, _ = bot_env
    setup(b)
    b.transfers.st["max_usd"] = 42
    raw = b.trader.export()
    b.trader.restore(raw)
    assert b.trader.state.transfers["max_usd"] == 42 and b.transfers.st["allowed"]


app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env


def test_mini_app_transfer_api(app_env):
    from tests.test_webapp import FRIEND as VIEWER
    from tests.test_webapp import call, get, post

    b, _ = app_env
    bybit, _ = setup(b)

    async def fn(client):
        st, x = await get(client, "/api/transfers")
        assert st == 200 and x["allowed"] and x["perms"] == {"bybit": True, "bingx": True}
        st, r = await post(client, "/api/transfers/check", {"coin": "USDT", "amount": "20", "src": "bybit", "dst": "bingx"},
                           uid=VIEWER)
        assert st == 403  # смотреть можно всем, проверять и выводить — только владельцу
        st, r = await post(client, "/api/transfers/check", {"coin": "usdt; drop", "amount": "1", "src": "bybit"})
        assert st == 400
        st, r = await post(client, "/api/transfers/check", {"coin": "LUNA", "amount": "5", "src": "bybit"})
        assert st == 200 and not r["plan"]["ok"]
        st, r = await post(client, "/api/transfers/check", {"coin": "USDT", "amount": "20", "src": "bybit", "dst": "bingx"})
        p = r["plan"]
        assert st == 200 and p["ok"] and p["network"] == "TRC20" and p["address"] == "TBingx…0002"
        assert any(BINGX_ADDR in c["text"] for c in p["checks"])  # полный адрес — для адресной книги Bybit
        st, r = await post(client, "/api/transfers/go", {"id": p["id"]})
        assert st == 200 and r["item"]["status"] == "sent" and r["transfers"]["items"][0]["coin"] == "USDT"
        st, r = await post(client, "/api/transfers/go", {"id": p["id"]})
        assert st == 400 and "устарела" in r["error"]
        return True

    assert call(b, fn)
    assert len(bybit.withdrawn) == 1


def test_ip_and_help_commands(bot_env, monkeypatch):
    b, session = bot_env

    async def fake_ip():
        return "203.0.113.7"

    monkeypatch.setattr(tr, "outbound_ip", fake_ip)

    async def run():
        await send(b, "/transfer ip")
        assert "203.0.113.7" in session.sent[-1][1] and "Outbound" in session.sent[-1][1]
        await send(b, "/transfer help")
        assert "адресная книга" in session.sent[-1][1].lower() and "Withdraw" in session.sent[-1][1]

    asyncio.run(run())


def test_deposit_address_fallback_by_network_name(bot_env):
    b, _ = bot_env
    _, bingx = setup(b)

    async def none_addr(code, params=None):
        return None  # как BingX, когда имя сети не совпало точно

    async def by_network(code, params=None):
        return {"TRX": {"address": "TFallback000000000000000009", "network": "TRX", "tag": None}}

    bingx.fetch_deposit_address = none_addr
    bingx.fetch_deposit_addresses_by_network = by_network
    plan = asyncio.run(b.transfers.analyze("USDT", 20, "bybit", "bingx"))
    assert plan.ok and plan.address == "TFallback000000000000000009"


# --- отдельный ключ вывода и IP сервера -------------------------------------------------------------------


def wd_setup(b, monkeypatch, ip_ok=True, perms=(2, 5)):
    bybit, bingx = setup(b)
    b.trader.key_info["bingx"] = {"spot_trade": True, "ip_bound": False}  # торговый ключ — без вывода и без IP
    b.transfers.env = {"BINGX_WITHDRAW_API_KEY": "wk", "BINGX_WITHDRAW_API_SECRET": "ws"}
    made = {}

    def make_ex(name):
        ex = Wallet("BingX", bingx.nets, BINGX_ADDR, {})
        ex.balance = bingx.balance  # тот же счёт, другой ключ

        async def perms_api(params=None):
            if not ip_ok:
                raise Exception('bingx {"code":100419,"msg":"your current request IP is not in the whitelist"}')
            return {"code": 0, "data": {"permissions": list(perms), "ipAddresses": ["74.220.51.10"]}}

        ex.accountV1PrivateGetAccountApiPermissions = perms_api
        made[name] = ex
        return ex

    b.transfers.make_ex = make_ex

    async def fake_ip():
        return "74.220.51.10"

    monkeypatch.setattr(tr, "outbound_ip", fake_ip)
    bingx.balance["USDT"] = 50.0
    return bybit, bingx, made


def test_separate_withdraw_key_is_used_only_for_sending(bot_env, monkeypatch):
    b, _ = bot_env
    bybit, bingx, made = wd_setup(b, monkeypatch)

    async def run():
        plan = await b.transfers.analyze("USDT", 20, "bingx", "bybit")
        assert plan.ok, plan.checks
        assert any("отдельный ключ вывода" in t for t in checks(plan, "ok"))
        b.transfers.keep(plan)
        await b.transfers.execute(plan.id)
        assert bingx.withdrawn == []  # торговый ключ не выводит
        assert made["bingx"].withdrawn[0][:3] == ("USDT", 20.0, BYBIT_ADDR)
        assert made["bingx"].apiKey == "wk"
        assert made["bingx"].markets is not None  # рынки взяты у основного экземпляра

    asyncio.run(run())
    assert b.transfers.can_withdraw_key("bingx") is True


def test_withdraw_key_ip_not_whitelisted_is_explained(bot_env, monkeypatch):
    b, session = bot_env
    _, _, made = wd_setup(b, monkeypatch, ip_ok=False)

    async def run():
        await b.transfers.check_ip()
        plan = await b.transfers.analyze("USDT", 20, "bingx", "bybit")
        bad = " ".join(checks(plan, "bad"))
        assert not plan.ok and "74.220.51.10" in bad and "белом списке" in bad
        from bot.doctor import _withdraw_key_checks
        rows = await _withdraw_key_checks(b, "bingx")
        assert rows[0].status == "bad" and "74.220.51.10" in rows[0].text and "Торговля" in rows[0].text

    asyncio.run(run())
    assert tr.is_ip_error("retCode 10010 Unmatched IP") and not tr.is_ip_error("insufficient balance")


def test_withdraw_key_without_withdraw_permission(bot_env, monkeypatch):
    b, _ = bot_env
    wd_setup(b, monkeypatch, perms=(2,))
    plan = asyncio.run(b.transfers.analyze("USDT", 20, "bingx", "bybit"))
    assert any("нет права Withdraw" in t for t in checks(plan, "bad"))


def test_server_ips_are_remembered_for_whitelist(bot_env, monkeypatch):
    b, session = bot_env
    setup(b)
    seq = iter(["74.220.51.10", "74.220.51.10", "74.220.59.3"])

    async def fake_ip():
        return next(seq)

    monkeypatch.setattr(tr, "outbound_ip", fake_ip)

    async def run():
        assert await b.transfers.check_ip() == ("74.220.51.10", True)
        assert await b.transfers.check_ip() == ("74.220.51.10", False)
        await send(b, "/transfer ip")
        text = session.sent[-1][1]
        assert "74.220.51.10,74.220.59.3" in text and "отдельному ключу" in text

    asyncio.run(run())
    # другой экземпляр не делит список IP через DEFAULTS
    assert tr.DEFAULTS["ips"] == {} and tr.DEFAULTS["items"] == []
