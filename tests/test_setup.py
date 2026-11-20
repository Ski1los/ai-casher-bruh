"""/setup: чек-лист готовности к торговле и приветствие после новой установки."""

import asyncio

from bot.scalper import Scalper
from bot.trading import Trader
from tests.fake_exchange import FakeTradingExchange, mk_market
from tests.test_ai_flow import buttons, press
from tests.test_scalper import dip_candles
from tests.test_trading import KEYS
from tests.test_telegram_flow import send


def attach(b, balance=5.1, keys=KEYS, funding=None):
    ex = FakeTradingExchange("ExA", {"SOL/USDT": mk_market("SOL", "USDT", min_cost=5.0)},
                             {"SOL/USDT": {"bid": 98.40, "ask": 98.42, "quoteVolume": 1e8}}, {"USDT": balance})
    ex.ohlcv = {"SOL/USDT": dip_candles(end_in_dip=False)}
    if funding is not None:
        ex.options = {"accountsByType": {"funding": "FUND", "spot": "UNIFIED"}}
        ex.funding = {"USDT": funding}
    b.scanner.exchanges = {"a": ex}
    b.trader = Trader(b.scanner, b.settings, env=keys)
    b.scalper = Scalper(b.trader, b.duplicate_running)
    return ex


def test_setup_without_keys(bot_env):
    b, session = bot_env
    attach(b, keys={})
    asyncio.run(send(b, "/setup"))
    text = session.sent[-1][1]
    assert "Ключи бирж не подключены" in text and "Осталось сделать" in text
    assert "su:keys" in buttons(session.markups[-1])


def test_setup_ready_then_enable_scalp(bot_env):
    b, session = bot_env
    attach(b)

    async def flow():
        await send(b, "/setup")
        text = session.sent[-1][1]
        assert "✅ ExA: ключ работает" in text
        assert "5.10 USDT" in text and "режиму $5 хватает" in text
        assert "прошли 1 монет" in text and "проверкой на новых данных" in text
        assert "Всё готово" in text
        assert "ap:on" in buttons(session.markups[-1])
        await press(b, "ap:on")  # одна кнопка: режим $5 + «Монеты в дело», реальные деньги — отдельно
        assert b.scalper.on and b.scalper.st["use_coins"] and not b.trader.state.live
        assert "Включить реальную торговлю" in session.sent[-1][1] and "live:yes" in buttons(session.markups[-1])
        await press(b, "su:again")
        text = session.sent[-1][1]
        assert "работает в тесте" in text and "tr:live" in buttons(session.markups[-1])

    asyncio.run(flow())


def test_setup_points_to_funding(bot_env):
    b, session = bot_env
    attach(b, balance=0.0, funding=5.0)
    asyncio.run(send(b, "/setup"))
    text = session.sent[-1][1]
    assert "«Funding» $5.00" in text and "Перевести" in text


def test_hello_after_fresh_install(bot_env):
    b, session = bot_env
    asyncio.run(b._hello("fresh"))
    assert "Бот установлен и работает" in session.sent[-1][1]
    assert "su:again" in buttons(session.markups[-1])
    asyncio.run(b._hello(None))
    assert len(session.sent) == 1


def test_withdraw_question_answered_without_ai(bot_env):
    b, session = bot_env
    asyncio.run(send(b, "для реальной торговли просит включить вывод в апи ключе"))
    text = session.sent[-1][1]
    assert "«Вывод» боту НЕ нужно" in text and "Спот торговля" in text
    asyncio.run(send(b, "сделай вывод по рынку"))  # «вывод» = итог, не про ключ: обычный ответ
    assert "«Вывод» боту НЕ нужно" not in session.sent[-1][1]


def test_perms_line_says_withdraw_off_is_right():
    from bot.trading import parse_bingx_key
    from bot.trading_ui import perms_line

    info = parse_bingx_key({"permissions": [1, 2], "ipAddresses": []})
    line = perms_line(info)
    assert "спот ✅" in line and "вывод ⬜️ выключен, так и надо" in line
    assert "включён" in perms_line(parse_bingx_key({"permissions": [1, 2, 5]}))
    assert perms_line(None) == ""


def test_keys_help_never_asks_for_withdraw(bot_env):
    b, session = bot_env
    b.trader.creds = {}
    asyncio.run(send(b, "/keys"))
    text = session.sent[-1][1]
    assert "Вывод (Withdraw) НЕ включай" in text and "⬜️ Вывод" in text


def test_setup_finds_money_stuck_in_a_coin(bot_env):
    b, session = bot_env
    ex = attach(b, balance=0.01)
    ex.balance["SOL"] = 0.059  # ≈ $5.8, куплено вручную
    asyncio.run(send(b, "/setup"))
    text = session.sent[-1][1]
    assert "лежит в SOL" in text and "/sell a SOL all" in text and "Включить автопилот" in text


def test_autopilot_sells_idle_sol_and_starts_trading(bot_env):
    """Как у пользователя на Bybit: USDT почти нет, ≈$5.8 лежит в SOL. Автопилот сам пускает их в дело."""
    b, session = bot_env
    ex = attach(b, balance=0.01)
    ex.ohlcv = {"SOL/USDT": dip_candles()}  # в конце свежий откат — есть сигнал
    ex.balance["SOL"] = 0.059
    b.trader.state.live = True

    async def flow():
        await send(b, "/autopilot")
        assert b.scalper.on and b.scalper.st["use_coins"]
        assert any("продаст SOL" in t for _, t in session.sent[-3:])
        events = await b.scalper.step()  # шаг 1: SOL → USDT
        assert events and "SOL → 5.80 USDT" in events[0].replace("5.79", "5.80")
        assert ex.created[0][:2] == ("sell", "SOL/USDT") and ex.balance["SOL"] < 1e-9
        events = await b.scalper.step()  # шаг 2: покупка по сигналу на эти деньги
        assert events and "купил SOL" in events[0] and "Сигнал «" in events[0]
        assert b.trader.state.log[0]["note"].startswith("автопилот: монета без дела")
        await send(b, "/scalp")
        assert "Монеты в дело: <b>вкл</b>" in session.sent[-1][1]
        await press(b, "sc:coins")
        assert not b.scalper.st["use_coins"]

    asyncio.run(flow())


def test_idle_coins_untouched_without_permission_or_in_test(bot_env):
    b, session = bot_env
    ex = attach(b, balance=0.01)
    ex.balance["SOL"] = 0.059
    b.trader.state.live = True
    b.scalper.turn_on()
    assert asyncio.run(b.scalper.step()) == []
    assert not ex.created and "Монеты в дело" in b.scalper.last_reason  # без разрешения не продаёт, но подсказывает
    b.scalper.st["use_coins"] = True
    b.trader.state.live = False
    asyncio.run(b.scalper.step())
    assert not ex.created  # в тесте реальные монеты не продаются


class BingXAccounts(FakeTradingExchange):
    """Как настоящий BingX с 2025 года: спот-API видит только счёт «Спот», остальное — в обзоре счетов."""

    def __init__(self, *a, overview=None, **kw):
        super().__init__(*a, **kw)
        self.overview = overview or []

    async def accountV1PrivateGetAllAccountBalance(self, params=None):
        return {"code": 0, "data": self.overview}


def attach_bingx(b, spot_usdt, overview, fund=None):
    ex = BingXAccounts("BingX", {"SOL/USDT": mk_market("SOL", "USDT", min_cost=5.0)},
                       {"SOL/USDT": {"bid": 98.40, "ask": 98.42, "quoteVolume": 1e8}}, {"USDT": spot_usdt},
                       overview=overview)
    ex.ohlcv = {"SOL/USDT": dip_candles(end_in_dip=False)}
    if fund is not None:
        ex.options = {"accountsByType": {"funding": "fund", "spot": "spot"}}
        ex.funding = {"USDT": fund}
    b.scanner.exchanges = {"bingx": ex}
    b.trader = Trader(b.scanner, b.settings, env={"BINGX_API_KEY": "k", "BINGX_API_SECRET": "s"})
    b.scalper = Scalper(b.trader, b.duplicate_running)
    return ex


def test_bingx_money_on_fund_account_is_found(bot_env):
    b, session = bot_env
    attach_bingx(b, 0.0, [{"accountType": "sopt", "usdtBalance": "5.00"}], fund=5.0)
    asyncio.run(send(b, "/setup"))
    text = session.sent[-1][1]
    assert "«Фонд (Fund)» $5.00" in text and "с «Фонд (Fund)» на «Спот»" in text
    asyncio.run(send(b, "/balance"))
    assert "Фонд (Fund)" in session.sent[-1][1] and "свободно для сделок: 0.00 USDT" in session.sent[-1][1]


def test_bingx_money_on_futures_is_found(bot_env):
    b, session = bot_env
    attach_bingx(b, 0.0, [{"accountType": "USDTMPerp", "usdtBalance": "5.20"}])
    asyncio.run(send(b, "/setup"))
    text = session.sent[-1][1]
    assert "Бессрочные фьючерсы USDT-M" in text and "на «Спот»" in text


def test_bingx_spot_money_is_shown(bot_env):
    b, session = bot_env
    attach_bingx(b, 5.0, [{"accountType": "sopt", "usdtBalance": "5.00"}])
    asyncio.run(send(b, "/setup"))
    text = session.sent[-1][1]
    assert "На торговом счёте BingX: <b>5.00 USDT</b>" in text and "другом счёте" not in text
    asyncio.run(send(b, "/scalp"))
    assert "на бирже: свободно 5.00 USDT" in session.sent[-1][1]
