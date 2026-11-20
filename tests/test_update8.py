"""Обновление 8: автоторговля (диагностика, минимальный ордер, ключ «только чтение», сводки),
мемкоины (DexScreener + RugCheck + радар), Т-Инвестиции (только просмотр) и их API в Mini App."""

import asyncio
import copy
import hashlib
import json
import ssl
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot import market_ui
from bot.config import Settings
from bot.dex import (DexClient, Pair, best_pairs, fmt_token_price, is_address, links, pair_text, parse_rug, risk_flags,
                     verdict)
from bot.doctor import run_doctor
from bot.tinvest import CA_FILE, TInvestClient, money, parse_portfolio, ssl_context, token_from_env
from bot.trading import Trader, key_trade_problem
from bot.triangular import path_min_amount, path_min_order_ok
from bot.webapp import WebApp
from tests import fake_market_api as fm
from tests.fake_exchange import tri_exchange
from tests.test_telegram_flow import OWNER, STRANGER, send
from tests.test_trading import FakeScanner, make_trader, run, triangle_opp
from tests.test_webapp import TOKEN, auth

BYBIT_KEYS = {"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}
READ_ONLY = {"read_only": True, "spot_trade": False, "withdraw": False}


class R:
    """Результат скана для auto_step."""

    def __init__(self, triangles, near=None):
        self.triangles = triangles
        self.near_tri = near


def bybit_trader(balances=None, **kw):
    ex = tri_exchange(balances)
    ex.markets = copy.deepcopy(ex.markets)
    settings = Settings(exchanges=[], tri_min_profit_percent=0.1, **kw)
    return Trader(FakeScanner({"bybit": ex}), settings, env=BYBIT_KEYS), ex


def tri_for(trader, name):
    from bot.triangular import find_triangles

    ex = trader.scanner.exchanges[name]
    found = find_triangles(name, ex.markets, ex.tickers, min_profit_percent=0, min_volume_usd=0)
    return next(o for o in found if o.path_text == "USDT → PEPE → BTC → USDT")


# ---------------------------------------------------------------------------
# Автоторговля
# ---------------------------------------------------------------------------


def test_path_min_amount_matches_min_order_check():
    trader, ex = make_trader()
    opp = triangle_opp(trader)
    ex.markets["PEPE/BTC"]["limits"]["cost"]["min"] = 0.0001  # ≈ $5 в BTC
    markets = {leg.symbol: ex.markets[leg.symbol] for leg in opp.legs}
    need = path_min_amount(opp.legs, markets)
    assert 4.5 < need < 5.5
    assert not path_min_order_ok(opp.legs, need * 0.98, markets)
    assert path_min_order_ok(opp.legs, need * 1.02, markets)


def test_key_trade_problem():
    assert "только чтение" in key_trade_problem("bybit", READ_ONLY)
    assert "Spot Trade" in key_trade_problem("bybit", {"read_only": False, "spot_trade": False})
    assert key_trade_problem("bybit", {"read_only": False, "spot_trade": True}) is None
    assert "Спотовая" in key_trade_problem("bingx", {"read": True})
    assert key_trade_problem("bingx", {"read": True, "spot_trade": True}) is None
    assert key_trade_problem("bybit", {"error": "нет связи"}) is None  # не знаем — не блокируем
    assert key_trade_problem("okx", {}) is None


def test_auto_status_explains_no_candidates_and_records_stats():
    trader, _ = make_trader(auto_min_profit_percent=5)
    trader.state.auto = True
    opp = triangle_opp(trader)
    assert run(trader.auto_step(R([opp]))) == []
    st = trader.auto_status
    assert st["code"] == "no_candidates" and st["candidates"] == 0 and st["triangles"] == 1
    assert st["best"]["path"] == opp.path_text and "≥5%" in st["reason"]
    assert trader.auto_stats["checks"] == 1
    text = trader.auto_status_text()
    assert "Почему нет сделки" in text and "Лучший сейчас" in text and "auto_min" in text
    assert trader.auto_problem() is None  # спокойный рынок — не проблема


def test_auto_uses_trade_amount_not_100_usd_depth():
    """Сканер проверил стакан на $100 и увидел мало прибыли, но торгуем мы $10: цикл должен проверяться."""
    trader, ex = make_trader(auto_min_profit_percent=0.3, max_trade_usd=10)
    trader.state.auto = True
    opp = triangle_opp(trader)
    from bot.sim import DepthResult

    opp.depth = DepthResult(100, 99.9, True)  # на $100 −0.1%
    [res] = run(trader.auto_step(R([opp])))
    assert res.executed and not res.live  # тест: ордера не отправлялись
    assert trader.auto_status["executed"] and trader.auto_stats["executed"] == 1


def test_live_low_balance_is_explained_and_skips_exchange():
    trader, ex = bybit_trader({"USDT": 1.14}, auto_min_profit_percent=0.3)
    ex.markets["PEPE/BTC"]["limits"]["cost"]["min"] = 0.0001
    trader.state.auto = trader.state.live = True
    opp = tri_for(trader, "bybit")
    other = copy.copy(opp)
    other.legs = list(opp.legs)
    other.net_percent = opp.net_percent - 0.01
    other.exchange = "bybit"
    assert run(trader.auto_step(R([opp]))) == []
    st = trader.auto_status
    assert st["tried"][0]["code"] == "min_order" and "нужно от $5" in st["reason"]
    assert st["low_money"] == {"bybit": 1.14}
    assert ex.created == []
    code, text = trader.auto_problem()
    assert code == "money"
    assert "Пополни" in trader._auto_hint()
    assert "мало: для цикла нужно от" in trader.auto_status_text()


def test_live_read_only_key_blocks_and_test_mode_does_not():
    trader, ex = bybit_trader({"USDT": 100})
    trader.key_info = {"bybit": READ_ONLY}
    trader.state.auto = True
    opp = tri_for(trader, "bybit")
    # в тесте права ключа не важны: цикл «исполняется» на бумаге
    [res] = run(trader.auto_step(R([opp])))
    assert res.executed and not res.live
    trader.recent_tri.clear()
    trader.state.live = True
    assert run(trader.auto_step(R([opp]))) == []
    st = trader.auto_status
    assert st["code"] == "blocked" and "только чтение" in st["blocked"]["bybit"]
    assert ex.created == []  # на бирже ничего не отправлено
    code, text = trader.auto_problem()
    assert code == "blocked" and "Read-Write" in text
    assert "⛔" in trader.auto_status_text() and "⛔" in trader.panel_text()


def test_auto_skip_cooldown_is_short_and_limits_stop_early():
    trader, ex = make_trader(auto_min_profit_percent=0.3, daily_trades=1)
    trader.state.auto = True
    opp = triangle_opp(trader)
    run(trader.auto_step(R([opp])))  # исполнено в тесте (лимиты в тесте тоже считаются, но day_trades только в live)
    key = "|".join(opp.key)
    assert trader.recent_tri[key] - time.time() > 500  # исполненный цикл: 10 минут
    trader.recent_tri.clear()
    trader.state.day_trades = 1
    assert run(trader.auto_step(R([opp]))) == []
    assert trader.auto_status["code"] == "limits"


def test_digest_and_problem_notifications(bot_env):
    b, session = bot_env
    ex = tri_exchange({"USDT": 1.14})
    ex.markets = copy.deepcopy(ex.markets)
    ex.markets["PEPE/BTC"]["limits"]["cost"]["min"] = 0.0001
    b.scanner.exchanges = {"bybit": ex}
    b.trader = Trader(b.scanner, b.settings, env=BYBIT_KEYS)

    async def flow():
        await send(b, "/autotrade on")
        assert "Автоторговля включена" in session.sent[-1][1] and "/why" in session.sent[-1][1]
        await send(b, "/live")
        from tests.test_ai_flow import press
        await press(b, "live:yes")
        assert "Реальный режим включён" in session.sent[-1][1] and "1.14" in session.sent[-1][1]
        n = len(session.sent)
        result = await b._scan()
        await b._run_autotrade(result)
        warn = [t for _, t in session.sent[n:] if "торговать не может" in t]
        assert len(warn) == 1 and ("мало денег" in warn[0] or "минимального ордера" in warn[0])
        await b._run_autotrade(result)  # повтор не спамит
        assert len([t for _, t in session.sent[n:] if "торговать не может" in t]) == 1
        # сводка через час после включения
        b.trader.auto_stats["since"] -= 3601
        await b._auto_notify()
        assert "сводка" in session.sent[-1][1] and "Сканов" in session.sent[-1][1]
        assert b.trader.auto_stats["checks"] == 0  # счётчики начались заново
        await send(b, "/why")
        assert "Автоторговля" in session.sent[-1][1] and "Почему нет сделки" in session.sent[-1][1]
        await send(b, "/set auto_report 0")
        b.trader.auto_stats["since"] -= 10 ** 6
        n = len(session.sent)
        await b._auto_notify()
        assert not [t for _, t in session.sent[n:] if "сводка" in t]

    asyncio.run(flow())


def test_why_button_in_trade_panel(bot_env):
    b, session = bot_env
    b.scanner.exchanges["a"] = tri_exchange()
    b.trader = Trader(b.scanner, b.settings, env={"A_API_KEY": "k", "A_API_SECRET": "s"})

    async def flow():
        await send(b, "/trade")
        from tests.test_ai_flow import buttons, press
        assert "tr:why" in buttons(session.markups[-1])
        await press(b, "tr:why")
        assert "Автоторговля" in session.sent[-1][1]

    asyncio.run(flow())


def test_doctor_shows_autotrade_and_read_only_is_bad_in_live(bot_env):
    b, _ = bot_env
    ex = tri_exchange({"USDT": 1.14})
    ex.markets = copy.deepcopy(ex.markets)

    async def ro(params=None):
        return {"result": {"readOnly": 1, "ips": [], "permissions": {"Spot": []}, "deadlineDay": 90}}

    ex.privateGetV5UserQueryApi = ro
    b.scanner.exchanges = {"bybit": ex}
    b.trader = Trader(b.scanner, b.settings, env=BYBIT_KEYS)
    b.trader.state.live = b.trader.state.auto = True
    checks = asyncio.run(run_doctor(b))
    by_title = {c.title: c for c in checks}
    assert by_title["ExA: права"].status == "bad" and "Read-Write" in by_title["ExA: права"].text
    assert by_title["ExA: деньги"].status == "warn"
    assert "Автоторговля" in by_title and "Т-Инвестиции" in by_title and "Радар мемкоинов" in by_title


# ---------------------------------------------------------------------------
# DexScreener / RugCheck
# ---------------------------------------------------------------------------


def test_pair_parsing_flags_and_links():
    p = Pair.from_json(fm.PAIRS[fm.SCAM][0])
    assert p.symbol == "BONK" and p.liquidity_usd == 4100 and p.sells_h24 == 0
    rug = parse_rug(fm.RUGS[fm.SCAM])
    flags = risk_flags(p, rug)
    assert any("honeypot" in f for f in flags) and any("ликвидности" in f for f in flags)
    assert verdict(p, rug)[0] == "red"
    good = Pair.from_json(fm.PAIRS[fm.BONK][0])
    assert verdict(good, parse_rug(fm.RUGS[fm.BONK]))[0] == "green"
    lk = links(good)
    assert lk["gmgn"] == f"https://gmgn.ai/sol/token/{fm.BONK}"
    assert lk["axiom"].startswith("https://axiom.trade/meme/") and lk["rugcheck"].endswith(fm.BONK)
    text = pair_text(p, rug)
    assert "🔴" in text and "GMGN" in text and "Axiom" in text and "<code>" in text
    assert best_pairs(Pair.from_json(x) for x in fm.PAIRS[fm.BONK])[0].liquidity_usd == 9_800_000
    assert is_address(fm.BONK) and is_address("0x" + "a" * 40) and not is_address("BONK")
    assert fmt_token_price(0.00002612) == "$0.00002612"
    assert parse_rug({"score": 5000, "risks": []})["score"] == 50  # старый формат


def test_dex_client_search_trending_rug_radar():
    async def main():
        server = TestServer(fm.make_app())
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        c = DexClient(base, base + "/rug/v1")
        try:
            pairs = await c.search("bonk")
            assert {p.address for p in pairs} == {fm.BONK, fm.SCAM}  # скам с тем же тикером тоже находится
            hot = await c.trending("solana")
            assert {p.symbol for p in hot} >= {"MOODENG", "WIF"} and all(p.chain == "solana" for p in hot)
            rug = await c.rugcheck(fm.NEW)
            assert rug["score"] == 12 and rug["label"] == "низкий"
            assert await c.rugcheck("0x" + "1" * 40) is None
            radar = await c.radar()
            assert [p.symbol for p, _ in radar] == ["MOODENG"]  # BONK/WIF старые, скам отсеян
            prices = await c.token_prices("solana", [fm.BONK, fm.NEW])
            assert prices[fm.BONK][0] == "BONK" and prices[fm.NEW][1] == pytest.approx(0.004271)
        finally:
            await c.close()
            await server.close()

    asyncio.run(main())


@pytest.fixture
def market_env(bot_env):
    b, session = bot_env
    return b, session


def test_dex_commands_and_radar(market_env):
    b, session = market_env

    async def flow():
        server = TestServer(fm.make_app())
        await server.start_server()
        fm.wire(b, str(server.make_url("")).rstrip("/"))
        try:
            await send(b, "/dex")
            assert "DexScreener" in session.sent[-1][1]
            await send(b, "/dex bonk")
            text = session.sent[-1][1]
            assert "BONK" in text and "RugCheck: риск 3/100" in text and "проверь адрес" in text
            await send(b, f"/dex {fm.SCAM}")
            assert "🔴" in session.sent[-1][1] and "honeypot" in session.sent[-1][1]
            await send(b, "/rug nope")
            assert "адрес токена" in session.sent[-1][1]
            await send(b, f"/rug {fm.SCAM}")
            assert "Mint Authority" in session.sent[-1][1] or "RugCheck" in session.sent[-1][1]
            await send(b, "/hot")
            assert "MOODENG" in session.sent[-1][1] and "В тренде" in session.sent[-1][1]

            await send(b, "/radar on")
            assert OWNER in b.radar_chats
            assert json.loads(b.settings_file.read_text())["radar"] == [OWNER]
            assert await market_ui.radar_once(b) == 1
            assert "Радар мемкоинов" in session.sent[-1][1] and "MOODENG" in session.sent[-1][1]
            assert await market_ui.radar_once(b) == 0  # повторно о том же токене не пишет
            await send(b, "/radar off")
            assert OWNER not in b.radar_chats
        finally:
            await b.dex.close()
            await server.close()

    asyncio.run(flow())


def test_radar_chats_survive_restart(market_env):
    b, _ = market_env
    b.radar_chats.add(OWNER)
    b._save_settings()
    import bot.telegram_bot as tb
    b2 = tb.ArbitrageBot(TOKEN, b.settings, [OWNER])
    assert b2.radar_chats == {OWNER}


# ---------------------------------------------------------------------------
# Т-Инвестиции
# ---------------------------------------------------------------------------


def test_tinvest_parsing_and_ca_bundle():
    assert money({"units": "12", "nano": 500000000}) == 12.5
    assert money({"units": "-3", "nano": -250000000}) == -3.25
    assert money(None) == 0
    p = parse_portfolio(fm.TI_PORTFOLIOS["2000111"])
    assert p["total"] == pytest.approx(65395.12) and p["yield_pct"] == pytest.approx(6.84)
    sber = next(x for x in p["positions"] if x["ticker"] == "SBER")
    assert sber["value"] == pytest.approx(60 * 301.25) and sber["yield_pct"] == pytest.approx(1971 / (60 * 268.4) * 100)
    assert p["by_type"]["share"] == pytest.approx(48210.5) and "futures" not in p["by_type"]
    assert token_from_env({"TINKOFF_TOKEN": " t "}) == "t" and token_from_env({}) == ""
    # корневой сертификат Минцифры: проверяем, что в репозитории именно он (отпечаток SHA-256)
    der = ssl.PEM_cert_to_DER_cert(CA_FILE.read_text())
    assert hashlib.sha256(der).hexdigest().upper() == "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"
    ctx = ssl_context()
    assert any("Russian Trusted Root CA" in str(c.get("subject")) for c in ctx.get_ca_certs())


def test_tinvest_client_and_command(market_env, monkeypatch):
    b, session = market_env

    async def flow():
        server = TestServer(fm.make_app())
        await server.start_server()
        base = str(server.make_url("")).rstrip("/")
        try:
            b.tinvest = TInvestClient("")
            await send(b, "/tinvest")
            assert "TINVEST_TOKEN" in session.sent[-1][1] and "Только чтение" in session.sent[-1][1]

            fm.wire(b, base)
            b.allowed_ids.add(STRANGER)
            await send(b, "/tinvest", user=STRANGER)
            assert "только владелец" in session.sent[-1][1]
            await send(b, "/tinvest")
            text = session.sent[-1][1]
            assert "Брокерский счёт" in text and "ИИС" in text and "SBER" in text and "Закрытый" not in text
            assert "65 395.12 ₽" in text and "Всего" in text and "−" not in text
            assert b.tinvest.snapshot["read_only"]

            fm.wire(b, base, token="bad")
            b.tinvest.token = "wrong"
            snap = await b.tinvest.get(force=True)
            assert "токен не подходит" in snap["error"]
        finally:
            await b.tinvest.close()
            await server.close()

    asyncio.run(flow())


# ---------------------------------------------------------------------------
# Mini App
# ---------------------------------------------------------------------------


def test_webapp_new_endpoints(market_env, monkeypatch):
    b, _ = market_env
    monkeypatch.delenv("WEBAPP_DEV_USER", raising=False)
    friend = 222
    b.allowed_ids.add(friend)
    b.scanner.exchanges["a"] = tri_exchange()
    b.trader = Trader(b.scanner, b.settings, env={"A_API_KEY": "k", "A_API_SECRET": "s"})
    b.webapp = WebApp(b, TOKEN)

    async def main():
        fake = TestServer(fm.make_app())
        await fake.start_server()
        fm.wire(b, str(fake.make_url("")).rstrip("/"))
        app = web.Application()
        b.webapp.setup(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            async def get(path, uid=OWNER):
                r = await client.get(path, headers=auth(uid))
                return r.status, await r.json()

            async def post(path, body, uid=OWNER):
                r = await client.post(path, json=body, headers=auth(uid))
                return r.status, await r.json()

            st, r = await post("/api/auto", {"on": True})
            assert st == 200 and r["auto"]
            await b._scan()
            await b._run_autotrade(b.scanner.last_result)
            st, r = await get("/api/overview")
            assert st == 200 and r["trading"]["auto_status"]["at"] and "stats" in r["trading"]["auto_status"]
            st, r = await get("/api/autostatus")
            assert st == 200 and "Автоторговля" in r["text"] and "<b>" not in r["text"]
            st, _ = await get("/api/autostatus", uid=friend)
            assert st == 403

            st, r = await get("/api/dex/search?q=bonk", uid=friend)
            assert st == 200 and r["items"][0]["symbol"] == "BONK" and r["items"][0]["rug"]["score"] == 3
            assert r["items"][0]["links"]["gmgn"].startswith("https://gmgn.ai/sol/token/")
            st, r = await get("/api/dex/search?q=")
            assert st == 400
            st, r = await get("/api/dex/hot")
            assert st == 200 and r["items"] and r["radar"] is False
            st, r = await get(f"/api/dex/rug?mint={fm.SCAM}")
            assert st == 200 and r["rug"]["score"] == 88 and r["item"]["verdict"] == "red"
            st, r = await post("/api/radar", {"on": True}, uid=friend)
            assert r["radar"] and friend in b.radar_chats

            st, r = await get("/api/tinvest")
            assert st == 200 and r["configured"] and len(r["accounts"]) == 2
            st, _ = await get("/api/tinvest", uid=friend)
            assert st == 403
            b.tinvest = TInvestClient("")
            st, r = await get("/api/tinvest")
            assert r == {"configured": False}
        finally:
            await client.close()
            await b.dex.close()
            await fake.close()

    asyncio.run(main())


def test_ai_context_has_autotrade_and_extras(market_env):
    b, _ = market_env
    b.trader.state.auto = True
    run(b.trader.auto_step(R([])))
    ctx = b._trading_context()
    assert "autotrade_status" in ctx and ctx["autotrade_status"]["stats"]["checks"] == 1
    extras = b._extras_context()
    assert extras["tinvest"]["connected"] is False and "/dex" in extras["useful_commands"]
    json.dumps(ctx, ensure_ascii=False)
    json.dumps(extras, ensure_ascii=False)


# ---------------------------------------------------------------------------
# $5 на Bybit: минимум API и деньги на счёте Funding
# ---------------------------------------------------------------------------


def bybit_with_api_minimum(balances):
    """Как настоящий Bybit с 2025 года: ордер через API от 5 USDT на каждой паре."""
    trader, ex = bybit_trader(balances, auto_min_profit_percent=0.3)
    for sym, m in ex.markets.items():
        m["limits"]["cost"]["min"] = 5.0 if sym.endswith("/USDT") else 0.0001
    return trader, ex


def test_five_dollars_is_not_enough_for_a_bybit_cycle():
    trader, ex = bybit_with_api_minimum({"USDT": 5.0})
    need = trader.min_start_usd("bybit")
    assert 5.0 < need < 5.3  # три шага по ≥ $5 после комиссии и движения цены
    trader.state.auto = True  # тестовый режим: баланс всё равно виден
    run(trader.auto_step(R([])))
    st = trader.auto_status
    assert st["free"] == {"bybit": 5.0} and st["low_money"] == {"bybit": 5.0}
    text = trader.auto_status_text()
    assert "на торговом счёте 5.00 USDT" in text and f"нужно от ${need:.2f}" in text
    trader.recent_tri.clear()
    trader.free_cache.clear()
    ex.balance["USDT"] = 20.0
    run(trader.auto_step(R([])))
    assert trader.auto_status["low_money"] == {}


def test_money_on_funding_account_is_found_and_explained():
    trader, ex = bybit_with_api_minimum({"USDT": 0.0})
    ex.options = {"accountsByType": {"funding": "FUND", "spot": "UNIFIED"}}
    ex.funding = {"USDT": 5.0}
    trader.state.auto = trader.state.live = True
    run(trader.auto_step(R([])))
    st = trader.auto_status
    assert st["funding"] == {"bybit": 5.0}
    code, text = trader.auto_problem()
    assert code == "funding" and "Funding" in text and "Unified Trading" in text
    hint = trader._auto_hint()
    assert hint.startswith("💡 Деньги не на том счёте")
    assert "на счёте Funding $5.00 (ими бот не торгует)" in trader.auto_status_text()
    # биржа без отдельного Funding: ничего не выдумываем
    ex.options = {}
    trader.funding_cache.clear()
    assert run(trader.funding_usd("bybit")) is None


def test_period_best_says_it_is_the_market(monkeypatch):
    trader, ex = bybit_trader({"USDT": 50})
    trader.state.auto = True
    trader.auto_stats["checks"] = 120
    trader.auto_stats["best"] = {"net": 0.12, "path": "USDT → PEPE → BTC → USDT", "exchange": "bybit"}
    run(trader.auto_step(R([])))
    trader.auto_stats["checks"] = 120
    trader.auto_stats["best"] = {"net": 0.12, "path": "USDT → PEPE → BTC → USDT", "exchange": "bybit"}
    text = trader.auto_status_text()
    assert "Лучший цикл за это время: +0.120%" in text and "дело в рынке" in text


def test_version_text(monkeypatch):
    from bot.cloud import BOT_VERSION, version_text

    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    assert version_text() == f"v{BOT_VERSION}"
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567")
    assert version_text() == f"v{BOT_VERSION} (abcdef1)"


def test_ai_sees_plain_words_about_keys(market_env):
    """ИИ путал withdraw=false с «надо включить» и писал «у BingX срок не указан»."""
    b, _ = market_env
    b.trader.key_info = {"bingx": {"spot_trade": True, "withdraw": False},
                         "bybit": {"spot_trade": True, "withdraw": True}}
    keys = b._extras_context()["api_keys"]
    assert keys["bingx"]["expiry"].startswith("не истекает")
    assert keys["bingx"]["withdraw_permission"].startswith("выключен — ПРАВИЛЬНО")
    assert keys["bybit"]["withdraw_permission"].startswith("ВКЛЮЧЁН")
