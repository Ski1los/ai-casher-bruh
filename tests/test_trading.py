"""Торговля: ядро Trader на фейковой бирже и сквозные сценарии через Telegram-интерфейс."""

import asyncio
import copy
import time

import ccxt.async_support as ccxt
import pytest
from aiogram.methods import AnswerCallbackQuery

from bot.ai import AIAssistant, Endpoint, LLMClient, extract_orders, extract_proposals
from bot.config import Settings
from bot.trading import (
    TradeError,
    Trader,
    TradeRecord,
    credentials_from_env,
    human_error,
    parse_amount,
)
from tests.fake_exchange import tri_exchange
from tests.test_ai import FakeLLM, serve
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import STRANGER, send

KEYS = {"A_API_KEY": "key", "A_API_SECRET": "secret"}


class FakeScanner:
    """Минимум от Scanner, который нужен трейдеру."""

    def __init__(self, exchanges):
        self.exchanges = exchanges

    def display_name(self, n):
        return getattr(self.exchanges.get(n), "name", n)

    async def _ensure_markets(self, name, ex):
        await ex.load_markets()

    def market(self, n, symbol):
        return (self.exchanges[n].markets or {}).get(symbol, {})

    async def order_book(self, n, symbol):
        return await self.exchanges[n].fetch_order_book(symbol)

    async def order_books(self, n, symbols):
        return [await self.order_book(n, s) for s in symbols]


def make_trader(balances=None, env=KEYS, **settings_kw):
    ex = tri_exchange(balances)
    settings = Settings(exchanges=[], tri_min_profit_percent=0.1, **settings_kw)
    return Trader(FakeScanner({"a": ex}), settings, env=env), ex


def triangle_opp(trader):
    """Треугольник USDT → PEPE → BTC → USDT, как его находит сканер."""
    from bot.triangular import find_triangles

    ex = trader.scanner.exchanges["a"]
    found = find_triangles("a", ex.markets, ex.tickers, min_profit_percent=0, min_volume_usd=0)
    opp = next(o for o in found if o.path_text == "USDT → PEPE → BTC → USDT")
    from bot.triangular import simulate_path

    books = [{"asks": [[ex.tickers[l.symbol]["ask"], 1e12]], "bids": [[ex.tickers[l.symbol]["bid"], 1e12]]} for l in opp.legs]
    opp.depth = simulate_path(books, opp.legs, 10)
    return opp


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Мелкие функции
# ---------------------------------------------------------------------------


def test_parse_amount_and_credentials():
    assert parse_amount("5", None) == 5
    assert parse_amount("0,5", None) == 0.5
    assert parse_amount("$7", None) == 7
    assert parse_amount("all", 3.3) == 3.3
    assert parse_amount("всё", 2) == 2
    assert parse_amount("50%", 10) == 5
    for bad in ("abc", "-1", "0", "150%"):
        with pytest.raises(TradeError):
            parse_amount(bad, 10)
    with pytest.raises(TradeError):
        parse_amount("all", None)

    env = {"BYBIT_API_KEY": " k ", "BYBIT_API_SECRET": "s", "BINGX_API_KEY": "only-key"}
    creds = credentials_from_env(["bybit", "bingx"], env)
    assert creds == {"bybit": {"apiKey": "k", "secret": "s"}}  # без секрета ключ не считается


def test_human_errors_are_specific():
    # подклассы должны распознаваться раньше родителей
    assert "не найден" in human_error(ccxt.OrderNotFound("x"))
    assert "отклонила" in human_error(ccxt.InvalidOrder("x"))
    assert "ограничен" in human_error(ccxt.AccountSuspended("x"))
    assert "прав" in human_error(ccxt.PermissionDenied("x"))
    assert "ключ не подходит" in human_error(ccxt.AuthenticationError("x"))
    assert "недостаточно" in human_error(ccxt.InsufficientFunds("x"))
    assert "подождать" in human_error(ccxt.RateLimitExceeded("x"))


def test_ai_order_lines_and_risk_keys_are_filtered():
    text = (
        "Готово.\n"
        "ORDER buy bybit ETH 5 — ты просил\n"
        "**ORDER sell okx PEPE all** — вернуть в USDT\n"
        "ORDER buy bybit BTC 5 @60000 — лимит\n"
        "SET max_trade=1000 — больше денег\n"
        "SET tri_min=0.3 — запас\n"
    )
    cleaned, proposals = extract_proposals(text)
    assert [p.key for p in proposals] == ["tri_min"]  # лимиты риска ИИ не меняет
    cleaned, orders = extract_orders(cleaned)
    assert cleaned == "Готово."
    assert [(o.side, o.exchange, o.coin, o.amount, o.price) for o in orders] == [
        ("buy", "bybit", "ETH", "5", None), ("sell", "okx", "PEPE", "all", None), ("buy", "bybit", "BTC", "5", 60000.0)
    ]
    assert orders[0].reason == "ты просил"


# ---------------------------------------------------------------------------
# Ядро: ключи, ордера, лимиты
# ---------------------------------------------------------------------------


def test_keys_check_and_bad_key():
    trader, ex = make_trader()
    assert ex.apiKey == "key" and ex.secret == "secret"  # ключ передан в ccxt
    assert run(trader.check_keys()) == {"a": "ok"}
    ex.auth_error = True
    status = run(trader.check_keys())
    assert "ключ не подходит" in status["a"]
    assert trader.working == []
    with pytest.raises(TradeError, match="не работает"):
        run(trader.plan_market("a", "buy", "BTC", "5"))

    no_keys, _ = make_trader(env={})
    with pytest.raises(TradeError, match="/keys"):
        run(no_keys.plan_market("a", "buy", "BTC", "5"))


def test_test_mode_sends_nothing():
    trader, ex = make_trader()
    plan = run(trader.plan_market("a", "buy", "BTC", "5"))
    assert plan.spent == 5 and plan.got_ccy == "BTC"
    assert plan.est_got == pytest.approx(5 / 50_001 * 0.999)
    assert "ТЕСТ" in trader.plan_text(plan)
    rec = run(trader.execute_plan(plan.id))
    assert not rec.live and ex.created == []
    assert ex.balance == {"USDT": 100.0}
    assert trader.state.day_trades == 0  # тестовые сделки не тратят дневной лимит
    with pytest.raises(TradeError, match="использован"):
        run(trader.execute_plan(plan.id))  # одна кнопка = один ордер


def test_live_market_buy_and_sell_with_terse_exchange():
    trader, ex = make_trader()
    ex.terse_orders = True  # как Bybit: create_order отдаёт только id, статус нужно запросить
    trader.state.live = True
    plan = run(trader.plan_market("a", "buy", "BTC", "5"))
    rec = run(trader.execute_plan(plan.id))
    assert rec.live and rec.spent == pytest.approx(5)
    assert rec.got == pytest.approx(5 / 50_001 * 0.999)
    assert ex.balance["USDT"] == pytest.approx(95)
    assert trader.state.day_trades == 1

    plan = run(trader.plan_market("a", "sell", "BTC", "all"))
    assert plan.spent == pytest.approx(ex.balance["BTC"])
    rec = run(trader.execute_plan(plan.id))
    # BTC округляется до 0.000001 вниз (как на биржах), поэтому крошечный остаток монеты остаётся
    assert rec.got_ccy == "USDT" and rec.got == pytest.approx(0.000099 * 50_000 * 0.999)
    assert ex.balance["BTC"] < 1e-6
    assert "Ордер исполнен" in trader.record_text(rec)


def test_limits_and_funds():
    trader, ex = make_trader(max_trade_usd=10, daily_trades=1)
    with pytest.raises(TradeError, match="лимита"):
        run(trader.plan_market("a", "buy", "BTC", "11"))
    plan = run(trader.plan_market("a", "buy", "BTC", "all"))
    assert plan.spent == 10  # «всё» = не больше лимита на сделку

    # тест без денег на бирже можно (с предупреждением), реально — нет
    poor, _ = make_trader(balances={"USDT": 1})
    plan = run(poor.plan_market("a", "buy", "BTC", "5"))
    assert any("свободно 1 USDT" in w for w in plan.warnings)
    poor.state.live = True
    with pytest.raises(TradeError, match="свободно"):
        run(poor.plan_market("a", "buy", "BTC", "5"))
    with pytest.raises(TradeError, match="минимального"):
        run(trader.plan_market("a", "buy", "BTC", "0.5"))

    trader.state.live = True
    run(trader.execute_plan(run(trader.plan_market("a", "buy", "BTC", "5")).id))
    with pytest.raises(TradeError, match="лимит 1 сделок"):
        run(trader.plan_market("a", "buy", "BTC", "5"))


def test_slippage_guard_and_expired_preview():
    trader, ex = make_trader()
    trader.state.live = True
    plan = run(trader.plan_market("a", "buy", "BTC", "5"))
    ex.tickers["BTC/USDT"]["ask"] = 52_000  # цена убежала на 4%
    with pytest.raises(TradeError, match="цена выросла"):
        run(trader.execute_plan(plan.id))
    assert ex.created == []

    plan = run(trader.plan_market("a", "buy", "BTC", "5"))
    plan.created_at = time.time() - 600
    with pytest.raises(TradeError, match="устарел"):
        run(trader.execute_plan(plan.id))


def test_limit_order_orders_cancel_and_panic():
    trader, ex = make_trader()
    trader.state.live = True
    trader.state.auto = True
    plan = run(trader.plan_limit("a", "buy", "BTC", "5", 40_000))
    rec = run(trader.execute_plan(plan.id))
    assert rec.kind == "limit" and ex.created[-1] == ("limit", "BTC/USDT", "buy", 0.000125, 40_000.0)
    assert ex.balance["USDT"] == pytest.approx(95)
    orders = run(trader.open_orders("a"))
    assert len(orders) == 1

    text = run(trader.panic())
    assert "отменено ордеров 1" in text
    assert not trader.state.live and not trader.state.auto
    assert ex.balance["USDT"] == pytest.approx(100)  # деньги из ордера вернулись
    assert run(trader.open_orders("a")) == []


# ---------------------------------------------------------------------------
# Треугольник и автоторговля
# ---------------------------------------------------------------------------


def test_triangle_test_and_live():
    trader, ex = make_trader()
    opp = triangle_opp(trader)
    res = run(trader.execute_triangle(opp))
    assert res.executed and not res.live and res.pnl > 0 and ex.created == []

    trader.state.live = True
    res = run(trader.execute_triangle(opp))
    assert res.executed and res.live and not res.reason and res.stuck is None
    assert [c[1] for c in ex.created] == ["PEPE/USDT", "PEPE/BTC", "BTC/USDT"]
    assert res.amount == 10
    assert res.pnl > 0.1  # ≈ +1.6% на $10
    assert ex.balance["USDT"] == pytest.approx(90 + res.final)
    assert trader.state.day_pnl_usd == pytest.approx(res.pnl)
    assert "Треугольник исполнен" in res.text(trader.scanner.display_name)


def test_triangle_gone_on_fresh_books():
    trader, ex = make_trader()
    trader.state.live = True
    opp = triangle_opp(trader)
    ex.tickers["PEPE/BTC"]["bid"] = 1.9e-10  # пока думали, перекос исчез
    res = run(trader.execute_triangle(opp))
    assert not res.executed and "возможность ушла" in res.reason
    assert ex.created == []


def test_triangle_failure_unwinds_back_to_usdt():
    trader, ex = make_trader()
    trader.state.live = True
    opp = triangle_opp(trader)
    ex.fail_symbols = {"PEPE/BTC"}
    res = run(trader.execute_triangle(opp))
    assert "шаг 2" in res.reason and res.stuck is None
    assert any("возврат" in s for s in res.steps)
    assert ex.balance.get("PEPE", 0) < 1  # PEPE продан обратно
    assert res.final == pytest.approx(10 * 0.999 * 0.99 * 0.999, rel=1e-3)
    assert res.pnl < 0


def test_triangle_stuck_coin_is_valued_not_zeroed():
    trader, ex = make_trader()
    trader.state.live = True
    opp = triangle_opp(trader)
    ex.fail_symbols = {"PEPE/BTC", "PEPE/USDT"}
    original = ex.create_market_buy_order_with_cost

    async def buy_ok(symbol, cost, params=None):  # покупка проходит, а продажи отклоняются
        ex.fail_symbols.discard(symbol)
        try:
            return await original(symbol, cost, params)
        finally:
            ex.fail_symbols.add(symbol)

    ex.create_market_buy_order_with_cost = buy_ok
    res = run(trader.execute_triangle(opp))
    assert res.stuck and "PEPE" in res.stuck
    assert res.final == pytest.approx(999_000 * 0.0000099, rel=1e-3)  # оценка по цене, а не ноль
    assert "/sell" in res.text(trader.scanner.display_name)


def test_auto_step_respects_threshold_repeat_and_daily_loss():
    trader, ex = make_trader(auto_min_profit_percent=0.3, daily_loss_usd=2)

    class R:
        triangles = [triangle_opp(trader)]

    assert run(trader.auto_step(R)) == []  # выключена
    trader.state.auto = True
    trader.state.live = True
    [res] = run(trader.auto_step(R))
    assert res.executed and res.live
    assert run(trader.auto_step(R)) == []  # тот же цикл не повторяем 10 минут

    trader.recent_tri.clear()
    R.triangles[0].depth.received_usd = 10.01  # +0.1% < auto_min
    assert run(trader.auto_step(R)) == []

    # дневной убыток выключает автоторговлю
    trader._record(TradeRecord(time.time(), "a", "triangle", "x", True, 10, "USDT", 7, "USDT", 10, pnl_usd=-3, source="auto"))
    assert not trader.state.auto and "дневной убыток" in trader.state.paused_reason
    with pytest.raises(TradeError, match="пауз"):
        trader._check_limits(5, True, "auto")


def test_state_export_restore_has_no_secrets():
    trader, _ = make_trader()
    trader.state.live = True
    trader._record(TradeRecord(time.time(), "a", "buy", "BTC/USDT", True, 5, "USDT", 0.0001, "BTC", 5))
    raw = copy.deepcopy(trader.export())
    assert "secret" not in str(raw) and "key" not in str(raw).replace("kind", "")
    other, _ = make_trader()
    other.restore({**raw, "junk": 1})
    assert other.state.live and len(other.state.log) == 1


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


@pytest.fixture
def trade_env(bot_env):
    b, session = bot_env
    b.scanner.exchanges["a"] = tri_exchange()
    b.scanner.exchanges["a"].markets = copy.deepcopy(b.scanner.exchanges["a"].markets)
    b.trader = Trader(b.scanner, b.settings, env=KEYS)
    return b, session


def last_buttons(session):
    return buttons(session.markups[-1])


def test_telegram_manual_trading_flow(trade_env):
    b, session = trade_env
    ex = b.scanner.exchanges["a"]

    async def flow():
        await send(b, "/buy a PEPE 5", user=STRANGER)  # не из списка разрешённых: бот молчит
        assert session.sent == [] and ex.created == []
        b.allowed_ids.add(STRANGER)  # даже разрешённый пользователь, но не владелец, торговать не может
        await send(b, "/buy a PEPE 5", user=STRANGER)
        assert "только владелец" in session.sent[-1][1] and ex.created == []

        await send(b, "/trade")
        assert "Торговля" in session.sent[-1][1] and "tr:stop" in last_buttons(session)

        await send(b, "/buy")
        assert "Формат" in session.sent[-1][1]

        await send(b, "/buy a PEPE 5")
        assert "Покупка PEPE/USDT" in session.sent[-1][1] and "ТЕСТ" in session.sent[-1][1]
        ok = last_buttons(session)[0]
        assert ok.startswith("ordok:")
        await press(b, ok, user=STRANGER)
        assert ex.created == [] and b.trader.pending  # чужой не может подтвердить
        await press(b, ok)
        assert "Тест выполнен" in session.sent[-1][1] and ex.created == []

        # реальный режим: двойное подтверждение
        await send(b, "/live")
        assert "реальную торговлю" in session.sent[-1][1]
        assert last_buttons(session) == ["live:yes", "dismiss"]
        await press(b, "live:yes")
        assert b.trader.state.live
        assert b.trading_file.exists()  # режим переживёт перезапуск

        await send(b, "/buy bybit BTC 5")
        assert "не знаю биржу" in session.sent[-1][1]
        await send(b, "/buy a PEPE 5")
        assert "РЕАЛЬНЫЙ" in session.sent[-1][1]
        await press(b, last_buttons(session)[0])
        assert "Ордер исполнен" in session.sent[-1][1]
        assert ex.created == [("buy_cost", "PEPE/USDT", 5.0)]

        await send(b, "/sell a PEPE 50%")
        await press(b, last_buttons(session)[0])
        assert ex.created[-1][0] == "sell"

        await send(b, "/limit a buy BTC 5 40000")
        await press(b, last_buttons(session)[0])
        assert "Лимитный ордер выставлен" in session.sent[-1][1]
        await send(b, "/orders")
        cancel = [d for d in last_buttons(session) if d.startswith("cx:")]
        assert len(cancel) == 1
        await press(b, cancel[0])
        assert "отменён" in session.sent[-1][1]

        await send(b, "/balance")
        assert "USDT" in session.sent[-1][1] and "Всего" in session.sent[-1][1]
        await send(b, "/trades")
        assert "Сделки через бота" in session.sent[-1][1]

        await send(b, "/stop")
        assert "Стоп" in session.sent[-1][1] and not b.trader.state.live

    run(flow())


def test_telegram_triangle_button_and_autotrade(trade_env):
    b, session = trade_env
    ex = b.scanner.exchanges["a"]

    async def flow():
        await send(b, "/scan")
        tx = [d for m in session.markups[-3:] for d in buttons(m) if d.startswith("tx:")]
        assert tx, "под треугольником на бирже с ключом должна быть кнопка «Исполнить»"
        await press(b, tx[0])
        assert "Треугольник исполнен" in session.sent[-1][1] and "ТЕСТ" in session.sent[-1][1]
        await press(b, tx[0])
        answers = [c for c in session.calls if isinstance(c, AnswerCallbackQuery)]
        assert "устарел" in answers[-1].text  # одна кнопка = одно исполнение

        # автоторговля в реальном режиме
        await send(b, "/autotrade on")
        assert "Автоторговля включена" in session.sent[-1][1] and b.trader.state.auto
        b.trader.state.live = True
        result = await b._scan()
        await b._run_autotrade(result)
        assert "⚙️" in session.sent[-1][1] and "Треугольник исполнен" in session.sent[-1][1]
        assert len(ex.created) == 3
        await send(b, "/autotrade off")
        assert not b.trader.state.auto

    run(flow())


def test_scan_has_no_trade_buttons_for_others_or_without_keys(bot_env):
    b, session = bot_env

    async def flow():
        await send(b, "/scan")
        assert not [d for m in session.markups[-3:] for d in buttons(m) if d.startswith("tx:")]
        await send(b, "/keys")
        assert "API" in session.sent[-1][1] and "Withdraw" in session.sent[-1][1]

    run(flow())


def test_ai_trader_proposes_order_with_confirm_button(trade_env):
    b, session = trade_env
    ex = b.scanner.exchanges["a"]

    def reply(body):
        return (
            "Покупаю PEPE на 5 USDT, как ты просил. Цену никто не знает, это риск.\n"
            "ORDER buy a PEPE 5 — по твоей просьбе\n"
            "SET max_trade=1000 — хочу больше\n"
        )

    async def flow():
        fake = FakeLLM(reply=reply)
        runner, base = await serve(fake)
        b.ai = AIAssistant(LLMClient([Endpoint("groq", base, "gsk_ok")]))
        try:
            await send(b, "купи пепе на 5 долларов")
            text = session.sent[-1][1]
            assert "Покупка PEPE/USDT" in text and "предложил ИИ" in text and "ORDER" not in text
            kb = last_buttons(session)
            assert kb[0].startswith("ordok:") and not any(d.startswith("set:max_trade") for d in kb)
            context = fake.requests[-1]["messages"][0]["content"]
            assert '"trading"' in context and '"exchanges_with_keys":["a"]' in context
            assert "\nmax_trade (" not in context and "\ntri_min (" in context  # ИИ не предлагает лимиты риска
            assert ex.created == []  # без кнопки ничего не исполняется
            await press(b, kb[0])
            assert "Тест выполнен" in session.sent[-1][1]
            assert b.trader.state.log[-1]["source"] == "ai"

            # чужой (если вдруг в списке разрешённых) не получает кнопок ордера
            b.allowed_ids.add(STRANGER)
            await send(b, "купи пепе", user=STRANGER)
            assert "только владелец" in session.sent[-1][1]
        finally:
            await b.ai.client.close()
            await runner.cleanup()

    run(flow())


def test_risk_settings_only_by_human(bot_env):
    b, session = bot_env

    async def flow():
        await send(b, "/set max_trade 25")
        assert b.settings.max_trade_usd == 25
        await press(b, "set:max_trade:500")  # кнопка с лимитом риска (поддельная) не применяется
        assert b.settings.max_trade_usd == 25

    run(flow())
