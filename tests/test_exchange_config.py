"""Проверяем, КУДА ccxt отправляет запросы (сеть подменена, биржи не нужны)."""

import asyncio

import ccxt.async_support as ccxt
import pytest

from bot.scanner import make_exchange

EXCHANGES = ["bybit", "bingx", "okx", "gate", "mexc", "kucoin", "htx", "bitget"]
FUTURES_MARKERS = ["linear", "inverse", "option", "swap", "futures", "contract", "delivery", "FUTURES", "SWAP", "OPTION"]


async def captured_urls(ex, action):
    urls = []

    async def fake_fetch(url, method="GET", headers=None, body=None):
        urls.append(url)
        raise ccxt.NetworkError("offline")

    async def no_currencies(params={}):
        return {}

    ex.fetch = fake_fetch
    ex.fetch_currencies = no_currencies
    try:
        await action(ex)
    except Exception:
        pass
    await asyncio.sleep(0.05)
    return urls


def spot_market():
    return {"id": "BTCUSDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "baseId": "BTC",
            "quoteId": "USDT", "type": "spot", "spot": True, "swap": False, "future": False,
            "option": False, "contract": False, "active": True, "precision": {}, "limits": {}, "info": {}}


@pytest.mark.parametrize("name", EXCHANGES)
def test_loads_only_spot_markets(name):
    async def run():
        ex = make_exchange(name)
        try:
            return await captured_urls(ex, lambda e: e.load_markets())
        finally:
            await ex.close()

    urls = asyncio.run(run())
    assert urls, "должен быть хотя бы один запрос"
    for url in urls:
        path = url.split("://", 1)[1]
        assert not any(m in path for m in FUTURES_MARKERS), f"{name} грузит не только спот: {url}"


@pytest.mark.parametrize("name", EXCHANGES)
def test_tickers_are_spot(name):
    async def run():
        ex = make_exchange(name)
        ex.set_markets([spot_market()])
        try:
            return await captured_urls(ex, lambda e: e.fetch_tickers())
        finally:
            await ex.close()

    urls = asyncio.run(run())
    assert urls
    for url in urls:
        assert not any(m in url for m in FUTURES_MARKERS), f"{name} берёт тикеры не спота: {url}"
    if name == "bybit":
        assert "category=spot" in urls[0]
