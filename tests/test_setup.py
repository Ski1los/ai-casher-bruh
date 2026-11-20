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
        assert "Проверка на истории: в плюсе 1 монет" in text
        assert "Всё готово" in text
        assert "sc:on" in buttons(session.markups[-1])
        await press(b, "sc:on")
        assert b.scalper.on
        await press(b, "su:again")
        text = session.sent[-1][1]
        assert "работает в тесте" in text and "tr:live" in buttons(session.markups[-1])

    asyncio.run(flow())


def test_setup_points_to_funding(bot_env):
    b, session = bot_env
    attach(b, balance=0.0, funding=5.0)
    asyncio.run(send(b, "/setup"))
    text = session.sent[-1][1]
    assert "на <b>Funding $5.00</b>" in text and "Перевод" in text


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
