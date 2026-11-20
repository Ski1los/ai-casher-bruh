"""Только Bybit и BingX, перенос старых данных с убранных бирж и уборка памяти."""

import asyncio
import json
import time

from bot import memory
from bot.config import SUPPORTED_EXCHANGES, exchanges_from_env, load_settings
from bot.demo import DemoTrade
from bot.demo_manager import DemoManager
from tests.test_telegram_flow import send


def test_only_bybit_and_bingx(monkeypatch):
    assert SUPPORTED_EXCHANGES == ("bybit", "bingx")
    monkeypatch.delenv("EXCHANGES", raising=False)
    assert exchanges_from_env() == ["bybit", "bingx"]
    assert load_settings().exchanges == ["bybit", "bingx"]
    # старое значение из Render: лишние биржи молча отбрасываются
    monkeypatch.setenv("EXCHANGES", "bybit,bingx,okx,gate,mexc,kucoin,htx,bitget")
    assert exchanges_from_env() == ["bybit", "bingx"]
    monkeypatch.setenv("EXCHANGES", " BingX , bingx ")
    assert exchanges_from_env() == ["bingx"]
    monkeypatch.setenv("EXCHANGES", "okx,gate")  # ничего подходящего: работаем с обеими
    assert exchanges_from_env() == ["bybit", "bingx"]


def test_saved_fees_and_alerts_from_removed_exchanges(bot_env):
    b, _ = bot_env  # в фикстуре биржи «a» и «b»
    b._apply_saved_settings({
        "fees": {"a": 0.0005, "okx": 0.0008},
        "alerts": [{"id": "x1", "coin": "TON", "op": "<", "price": 5.0, "exchange": "okx", "chat_id": 1, "created_at": 1.0},
                   {"id": "x2", "coin": "SOL", "op": ">", "price": 300.0, "exchange": "b", "chat_id": 1, "created_at": 1.0}],
    })
    assert b.settings.fee_overrides == {"a": 0.0005}
    assert "okx" not in b._settings_state()["fees"]
    assert [(a.id, a.exchange) for a in b.alerts.items] == [("x1", "a"), ("x2", "b")]


def test_demo_accounts_leave_removed_exchanges(tmp_path, bot_env):
    b, _ = bot_env
    now = time.time()
    trade = DemoTrade(symbol="PEPE/USDT", buy_exchange="a", sell_exchange="okx", opened_at=now, cost_usd=5.0,
                      buy_price=1, coins_bought=5, withdraw_fee_coins=0, withdraw_fee_known=True,
                      coins_arrived=5, expected_usd=5.1, signal_percent=1)
    raw = {"version": 2, "chat_id": 7, "created_at": now, "accounts": {
        "cross-5": {"start_balance": 5.0, "balance": 0.0, "strategy": "cross", "location": "a",
                    "open_trade": trade.__dict__, "trades": [trade.__dict__] * 400},
        "tri-5": {"start_balance": 5.0, "balance": 5.2, "strategy": "tri", "location": "gate"},
        "tri-50": {"start_balance": 50.0, "balance": 51.0, "strategy": "tri", "location": "b"},
    }}
    path = tmp_path / "old_demo.json"
    path.write_text(json.dumps(raw))
    m = DemoManager(b.scanner, b.settings, path)
    cross = m.accounts["cross-5"]
    assert cross.open_trade is None and cross.balance == 5.0 and cross.location is None  # деньги вернулись
    assert len(cross.trades) <= 300  # старая история подрезана
    assert m.accounts["tri-5"].location is None  # счёт снова может торговать
    assert m.accounts["tri-50"].location == "b"  # живая биржа: ничего не трогаем


def test_memory_helpers():
    now, peak = memory.rss_mb(), memory.peak_mb()
    assert 5 < now <= peak + 1
    before, after = memory.free_memory()
    assert before > 0 and after > 0
    assert "МБ" in memory.memory_line()
    memory.tune_malloc()  # не падает ни на какой системе


def test_cleanup_and_clean_command(bot_env):
    b, session = bot_env
    old = time.time() - 10 * 86400
    b.sent_alerts[("PEPE/USDT", "a", "b")] = (old, 1.0)
    b.sent_alerts[("BTC/USDT", "a", "b")] = (time.time(), 1.0)
    b._key_warned["старое"] = old
    b.webapp._market_cache[("a", "BTC/USDT", "15m")] = (old, {})
    b.webapp._tickers_cache["a"] = (time.time(), [])
    b.dex._cache["u"] = (time.time(), {})

    r = b.cleanup()  # обычная уборка: только устаревшее
    assert r["removed"] == 3 and r["after"] > 0
    assert list(b.sent_alerts) == [("BTC/USDT", "a", "b")]
    assert b.webapp._tickers_cache and b.dex._cache  # свежие кэши остались

    asyncio.run(send(b, "/clean"))  # /clean: все кэши
    text = session.sent[-1][1]
    assert "Память почищена" in text and "МБ" in text and "не тронуты" in text
    assert not b.webapp._tickers_cache and not b.dex._cache
    assert b.sent_alerts  # антиповтор сигналов не сбрасывается
