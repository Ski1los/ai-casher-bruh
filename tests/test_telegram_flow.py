"""Сквозной тест: команды идут через настоящий aiogram Dispatcher, а Telegram API подменён."""

import asyncio
import copy
import datetime as dt

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import Chat, Message, Update, User

OWNER = 6069246522
STRANGER = 111


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[tuple[int, str]] = []
        self.markups: list = []  # клавиатура каждого сообщения из sent (или None)
        self.calls: list = []  # все остальные методы API

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, (SendMessage, EditMessageText)):
            text = method.text
            assert len(text) <= 4096, "Telegram не примет сообщение длиннее 4096 символов"
            self.sent.append((method.chat_id, text))
            self.markups.append(method.reply_markup)
        else:
            self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=len(self.sent),
                date=dt.datetime.now(),
                chat=Chat(id=method.chat_id or OWNER, type="private"),
                text=text,
            ).as_(bot)
        return True

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def close(self):
        pass


class FakeExchange:
    has = {"fetchTickers": True}

    def __init__(self, name, tickers, markets):
        self.name = name
        self._tickers = tickers
        self.markets = markets
        self.currencies = {}

    async def load_markets(self, reload=False):
        return self.markets

    async def fetch_tickers(self):
        return self._tickers

    async def fetch_order_book(self, symbol, limit=None):
        t = self._tickers[symbol]
        return {"asks": [[t["ask"], 1e12]], "bids": [[t["bid"], 1e12]]}

    async def close(self):
        pass


def mk(base, quote, min_cost=1.0):
    # минимальный ордер в валюте котировки: 1 USDT или 0.00002 BTC (≈ $1)
    return {"spot": True, "active": True, "base": base, "quote": quote, "taker": 0.001,
            "limits": {"cost": {"min": min_cost}, "amount": {"min": 0}}}


def exchanges():
    markets = {"BTC/USDT": mk("BTC", "USDT"), "PEPE/USDT": mk("PEPE", "USDT"), "PEPE/BTC": mk("PEPE", "BTC", 0.00002)}
    a = FakeExchange("ExA", {
        "BTC/USDT": {"bid": 50_000, "ask": 50_001, "quoteVolume": 1e7},
        "PEPE/USDT": {"bid": 0.0000099, "ask": 0.00001, "quoteVolume": 1e7},
        "PEPE/BTC": {"bid": 2.04e-10, "ask": 2.05e-10, "quoteVolume": 200},
    }, markets)
    b = FakeExchange("ExB", {
        "BTC/USDT": {"bid": 50_000, "ask": 50_001, "quoteVolume": 1e7},
        "PEPE/USDT": {"bid": 0.0000103, "ask": 0.0000104, "quoteVolume": 1e7},
        "PEPE/BTC": {"bid": 2.0e-10, "ask": 2.01e-10, "quoteVolume": 200},
    }, copy.deepcopy(markets))  # у каждой биржи свои рынки, как в ccxt
    return {"a": a, "b": b}


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    import bot.demo as demo_mod
    import bot.telegram_bot as tb
    from bot.config import Settings
    from bot.demo_manager import DemoManager

    monkeypatch.setattr(demo_mod, "DEMO_FILE", tmp_path / "demo.json")
    monkeypatch.setattr(tb, "DEMO_FILE", tmp_path / "demo.json")
    monkeypatch.setattr(tb, "SUBSCRIBERS_FILE", tmp_path / "subs.json")
    monkeypatch.setattr(tb, "SETTINGS_FILE", tmp_path / "settings.json")
    for var in ("AI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    settings = Settings(exchanges=[], min_profit_percent=0.5, tri_min_profit_percent=0.1, demo_transfer_min=0)
    b = tb.ArbitrageBot("123456:" + "A" * 35, settings, [OWNER])
    session = RecordingSession()
    b.bot.session = session
    b.scanner.exchanges = exchanges()
    b.subscribers = tb.SubscriberStore(tmp_path / "subs.json")
    b.demo = DemoManager(b.scanner, settings, tmp_path / "demo.json")
    b.backup = None
    return b, session


async def send(b, text, user=OWNER):
    msg = Message(
        message_id=1,
        date=dt.datetime.now(),
        chat=Chat(id=user, type="private"),
        from_user=User(id=user, is_bot=False, first_name="Тест"),
        text=text,
    )
    await b.dp.feed_update(b.bot, Update(update_id=1, message=msg))


def test_full_user_flow(bot_env):
    b, session = bot_env

    async def run():
        await send(b, "/start")
        assert "Арбитражный сканер" in session.sent[-1][1]

        await send(b, "/scan")
        scan_text = "\n".join(t for _, t in session.sent[-3:])
        assert "PEPE/USDT" in scan_text  # межбиржевая связка ExA → ExB
        assert "USDT → PEPE → BTC → USDT" in scan_text  # треугольник на ExA

        await send(b, "/demo_start 5 50")
        assert "Демо запущено" in session.sent[-1][1]
        assert len(b.demo.accounts) == 4

        # Два прохода автоцикла: открытие межбиржевой сделки и треугольник, затем закрытие
        for _ in range(2):
            result = await b._scan()
            await b._run_demo(result)
        demo_events = [t for _, t in session.sent if "Демо:" in t]
        assert any("Демо: треугольник" in t for t in demo_events)
        assert len(b.demo.accounts["tri-50"].trades) == 1
        assert len(b.demo.accounts["tri-5"].trades) == 1  # $5 > мин. ордера ≈ $1
        assert any("сделка открыта" in t for t in demo_events)
        assert any("сделка закрыта" in t for t in demo_events)

        await send(b, "/demo")
        assert "Межбиржевой" in session.sent[-1][1] and "Треугольный" in session.sent[-1][1]
        await send(b, "/demo 50")
        assert "Сейчас:" in session.sent[-1][1]
        await send(b, "/demo_history")
        assert "Последние демо-сделки" in session.sent[-1][1]
        await send(b, "/report")
        assert "Отчёт" in session.sent[-1][1] and "сканов" in session.sent[-1][1]
        await send(b, "/set tri_min 0.2")
        assert b.settings.tri_min_profit_percent == 0.2
        await send(b, "/settings")
        assert "tri_min" in session.sent[-1][1]
        await send(b, "/status")
        assert "треугольников" in session.sent[-1][1]
        await send(b, "/auto_on")
        assert OWNER in b.subscribers.chats
        await send(b, "/demo_stop")
        assert not b.demo.active

    asyncio.run(run())


def test_stranger_is_blocked(bot_env):
    b, session = bot_env

    async def run():
        await send(b, "/start", user=STRANGER)
        assert "Доступ закрыт" in session.sent[-1][1]
        before = len(session.sent)
        await send(b, "/demo_start", user=STRANGER)
        await send(b, "/scan", user=STRANGER)
        assert len(session.sent) == before and not b.demo.accounts

    asyncio.run(run())


def test_bad_arguments_are_handled(bot_env):
    b, session = bot_env

    async def run():
        await send(b, "/demo_start abc")
        assert "Формат" in session.sent[-1][1]
        await send(b, "/demo_start 0.5")
        assert "от 1" in session.sent[-1][1]
        await send(b, "/demo xyz")
        assert "Формат" in session.sent[-1][1]
        await send(b, "/set interval 1")
        assert "допустимо от 10" in session.sent[-1][1]
        await send(b, "/set tri_min -1")
        assert "допустимо" in session.sent[-1][1]
        await send(b, "/set nope 1")
        assert "Формат" in session.sent[-1][1]

    asyncio.run(run())
