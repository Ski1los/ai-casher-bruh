"""♾ Полный автомат: держит обучение включённым, сторож циклов, «Результат», деньги — только с разрешения."""

import asyncio
import time

from bot import autonomy as au
from tests.test_ai_flow import buttons, press
from tests.test_profit import scalp_trade
from tests.test_telegram_flow import send
from tests.test_webapp import FRIEND, call, get, post

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

NOW = time.time()
GOOD = [0.9, 0.7, -0.3, 0.8, 1.0, 0.6, -0.2, 0.9] * 2  # 16 сделок, доказанный плюс


def lab_proven(b):
    b.scalper.lab["stats"]["a|BTC/USDT|dip"] = {"rets": list(GOOD), "sum": sum(GOOD), "n": len(GOOD), "wins": 12,
                                                "since": NOW - 5 * 86400}


def with_keys(b):
    b.trader.creds = {"a": {"apiKey": "k", "secret": "s"}}
    b.trader.key_status = {}


def test_state_defaults_and_survives_restore(bot_env):
    b, _ = bot_env
    a = b.autonomy
    assert a.on and a.st["real"] is False and a.st["every_h"] == 6
    a.st["every_h"] = 12
    a.st["real"] = True
    b.copy.restore(b.copy.export())
    assert b.autonomy.st["every_h"] == 12 and b.autonomy.st["real"] is True
    # заодно: состояние эфиров (v32) тоже переживает перезапуск
    b.mentors.live.st["every_min"] = 9
    b.copy.restore(b.copy.export())
    assert b.mentors.live.st["every_min"] == 9


def test_ensure_turns_on_all_learning_without_money(bot_env):
    b, _ = bot_env
    b.copy.st["on"] = False
    b.mentors.train.st["on"] = False
    done = b.autonomy.ensure(NOW)
    assert b.demo.active and b.demo.chat_id == b.owner_id  # демо-арбитраж на $5
    assert b.meme.demo_on and b.copy.on and b.mentors.train.st["on"] and b.mentors.scout.st["on"]
    assert any("демо-арбитраж" in x for x in done) and any("копитрейдинг" in x for x in done)
    # v45: тест автопилота работает и без ключа — по публичным ценам на виртуальные деньги
    assert b.scalper.on and not b.trader.state.live
    assert b.autonomy.ensure(NOW) == []  # второй раз — ничего не надо
    # остановленное руками демо не перезапускаем (оно обнулило бы результаты)
    b.demo.stop()
    assert not any("арбитраж" in x for x in b.autonomy.ensure(NOW))


def test_ensure_starts_autopilot_only_in_test_mode(bot_env):
    b, _ = bot_env
    with_keys(b)
    b.trader.state.live = True  # реальный режим включён руками — автопилот сам не включаем
    b.autonomy.ensure(NOW)
    assert not b.scalper.on
    b.trader.state.live = False
    assert any("автопилот в тесте" in x for x in b.autonomy.ensure(NOW))
    assert b.scalper.on and not b.trader.state.live


def test_stop_button_is_respected(bot_env):
    b, _ = bot_env
    with_keys(b)
    b.autonomy.st["real"] = True
    b.autonomy.ensure(NOW)
    assert b.scalper.on
    asyncio.run(b.trader.panic())
    a = b.autonomy
    assert a.st["real"] is False and a.st["pilot_hold"] and not b.scalper.on
    a.ensure(NOW)
    assert not b.scalper.on  # после /stop автопилот сам не включается
    assert "остановлен тобой" in a.text()
    a.set_on(True)  # включил автомат заново — снова можно
    a.ensure(NOW)
    assert b.scalper.on


def test_watchdog_restarts_dead_and_stuck_loops(bot_env):
    b, _ = bot_env

    async def run():
        runs = []

        async def dies():
            runs.append("dies")
            raise RuntimeError("boom")

        async def hangs():
            runs.append("hangs")
            await asyncio.sleep(3600)

        b._spawn("copy", dies)
        b._spawn("scalp", hangs)
        await asyncio.sleep(0)
        b._beats["scalp"] = NOW - 5000  # не отмечался больше 20 минут — завис
        stuck = b._loops["scalp"][1]
        fixes = b.autonomy.watchdog(NOW)
        await asyncio.sleep(0)
        assert any("копитрейдинг" in f and "упал" in f for f in fixes)
        assert any("автопилот" in f and "завис" in f for f in fixes)
        assert stuck.cancelled() and runs.count("dies") == 2 and runs.count("hangs") == 2
        assert b.autonomy.st["restarts"] == 2 and len(b.autonomy.st["fixes"]) == 2
        b._beats["scalp"] = NOW
        b._loops["copy"] = (dies, b._loops["scalp"][1])  # живой цикл не трогаем
        assert b.autonomy.watchdog(NOW) == []
        for _, task in b._loops.values():
            task.cancel()

    asyncio.run(run())


def test_scan_loop_revives_autonomy_loop(bot_env):
    b, _ = bot_env

    async def run():
        n = []

        async def loop():
            n.append(1)

        b._spawn("autonomy", loop)
        await asyncio.sleep(0)
        b._guard_autonomy()
        await asyncio.sleep(0)
        assert len(n) == 2

    asyncio.run(run())


def test_no_real_money_without_permission(bot_env):
    b, _ = bot_env
    with_keys(b)
    lab_proven(b)
    assert b.autonomy.money_step(NOW) == []
    assert not b.trader.state.live
    assert "Разрешить деньги" in b.autonomy.next_step()


def test_promotes_proven_autopilot_and_reverts_on_losses(bot_env):
    b, _ = bot_env
    a = b.autonomy
    lab_proven(b)
    a.allow_real(True, NOW)
    assert a.money_step(NOW) == []  # нет ключа биржи
    assert "нет ключа" in a.status_dict(NOW)["money"]["pilot"]
    with_keys(b)
    b.settings.max_trade_usd = 50  # лимиты выше безопасных — сам не включаю
    assert a.money_step(NOW) == [] and "лимиты выше" in a.status_dict(NOW)["money"]["pilot"]
    b.settings.max_trade_usd = 10
    events = a.money_step(NOW)
    assert events and "включил настоящие деньги" in events[0]["text"]
    assert b.trader.state.live and b.scalper.on and "pilot" in a.st["promoted"]
    assert a.money_step(NOW) == []  # второй раз не дублирует
    assert "работает на настоящих деньгах" in a.next_step()
    # настоящие сделки пошли в минус → Страж ставит паузу → автомат возвращает в тест
    for _ in range(9):
        scalp_trade(b, -1.5)
    b.profit.check(NOW)
    events = a.money_step(NOW)
    assert events and "вернул" in events[0]["text"]
    assert not b.trader.state.live and b.scalper.on  # учится дальше в тесте
    assert a.st["blocked"]["pilot"] > NOW + 2 * 86400 and not a.st["promoted"]
    assert a.money_step(NOW) == []  # 3 дня не включает снова


def test_promotes_memecoins_only_with_wallet(bot_env):
    b, _ = bot_env
    a = b.autonomy
    for i in range(16):
        b.meme.demo["closed"].append({"symbol": f"M{i}", "pct": 9.0 if i % 4 else -3.0, "pnl_usd": 0.4,
                                      "closed": NOW - 86400})
    a.allow_real(True, NOW)
    a.money_step(NOW)
    assert not b.meme.st["on"]
    assert "Phantom" in a.status_dict(NOW)["money"]["meme"]


def test_manual_disable_and_revoke(bot_env):
    b, _ = bot_env
    a = b.autonomy
    with_keys(b)
    lab_proven(b)
    a.allow_real(True, NOW)
    a.money_step(NOW)
    assert b.trader.state.live
    text = a.allow_real(False, NOW)
    assert "Запретил" in text and "Автопилот" in text and not b.trader.state.live and not a.st["promoted"]
    # руками выключили автопилот — автомат не спорит и просто забывает
    a.allow_real(True, NOW)
    a.money_step(NOW)
    b.scalper.turn_off()
    assert a.money_step(NOW) == [] and not b.scalper.on and not a.st["promoted"]
    assert "выключил его руками" in a.status_dict(NOW)["money"]["pilot"]
    assert a.money_step(NOW) == []
    a.allow_real(True, NOW)  # заново разрешил — снова можно
    assert a.money_step(NOW) and b.scalper.on


def test_digest_reports_results_and_stays_quiet(bot_env):
    b, _ = bot_env
    a = b.autonomy
    a.st["last_digest"] = NOW - 6 * 3600
    a.st["last_sent"] = NOW - 6 * 3600
    assert a.digest(NOW) is None  # ничего не произошло — не спамим
    scalp_trade(b, 0.8, live=False, at=NOW - 600)
    a._fix("🔁 перезапустил «сканер» (завис)", NOW + 0.5)
    text = a.digest(NOW + 1)
    assert "Результат за" in text and "Учёба без денег" in text and "Автопилот — тест" in text
    assert "перезапустил" in text and "👉" in text and len(text) < 4096
    assert a.digest(NOW + 2) is None  # те же сделки второй раз не считаем
    # настоящие деньги
    scalp_trade(b, 2.0, live=True, at=NOW + 3)
    text = a.digest(NOW + 4)
    assert "Настоящие деньги" in text and "+$0.10" in text
    # раз в сутки «всё тихо» — чтобы было видно, что бот жив
    text = a.digest(NOW + 5 + 25 * 3600)
    assert text and "Новых сделок не было" in text


def test_digest_due_respects_night_and_off(bot_env):
    b, _ = bot_env
    a = b.autonomy
    b.settings.report_utc_offset = 0
    day = (int(NOW) // 86400) * 86400
    a.st["last_digest"] = day - 7 * 3600
    assert a.digest_due(day + 12 * 3600)
    assert not a.digest_due(day + 3 * 3600)  # ночь
    a.st["every_h"] = 0
    assert not a.digest_due(day + 12 * 3600)
    assert a.cycle_every() == 3 and a.cycle_every() == 6


def test_tick_welcomes_once_and_reports_reenables(bot_env):
    b, session = bot_env

    async def run():
        await au.tick(b, NOW)
        assert "Включил полный автомат" in session.sent[-1][1]
        assert b.autonomy.st["welcomed"] and b.demo.active
        n = len(session.sent)
        await au.tick(b, NOW + 300)
        assert len(session.sent) == n  # всё в порядке — молчим
        b.copy.st["on"] = False
        await au.tick(b, NOW + 600)
        assert "включил обратно" in session.sent[-1][1] and b.copy.on
        b.autonomy.set_on(False)
        b.copy.st["on"] = False
        await au.tick(b, NOW + 900)
        assert not b.copy.on  # выключен — ничего не трогает

    asyncio.run(run())


def test_telegram_flow(bot_env):
    b, session = bot_env

    async def run():
        await send(b, "/autonomy")
        assert "Полный автомат" in session.sent[-1][1] and "Учится без денег" in session.sent[-1][1]
        btns = buttons(session.markups[-1])
        assert "au:real" in btns and "au:now" in btns and "au:hook" in btns
        await press(b, "au:real")
        assert "Разрешить автомату настоящие деньги" in session.sent[-1][1]
        assert "au:real2" in buttons(session.markups[-1])
        assert b.autonomy.st["real"] is False  # одно нажатие ничего не включает
        await press(b, "au:real2")
        assert b.autonomy.st["real"] is True and "Разрешил" in session.sent[-1][1]
        await press(b, "au:realoff")
        assert b.autonomy.st["real"] is False
        await press(b, "au:every")
        assert b.autonomy.st["every_h"] == 12
        await press(b, "au:now")
        assert "Результат" in session.sent[-1][1]
        await press(b, "au:hook")
        assert "RENDER_DEPLOY_HOOK_URL" in session.sent[-1][1]
        await press(b, "au:off")
        assert not b.autonomy.on
        await press(b, "au:real2", user=777)  # чужой не может
        assert b.autonomy.st["real"] is False
        await send(b, "/stop")
        assert b.autonomy.st["pilot_hold"]

    asyncio.run(run())


def test_lab_learns_on_public_candles_without_keys(bot_env):
    b, _ = bot_env
    seen = []

    async def fake_rank(name, force=False):
        seen.append(("rank", name))
        return []

    async def fake_lab(name):
        seen.append(("lab", name))

    b.scalper.rank = fake_rank
    b.scalper.lab_step = fake_lab
    asyncio.run(b._scalp_tick())
    assert seen and all(n in b.scanner.exchanges for _, n in seen)
    seen.clear()
    b.autonomy.set_on(False)
    asyncio.run(b._scalp_tick())
    assert seen == []  # автомат выключен и ключа нет — как раньше, не учимся


def test_report_and_profit_api(app_env):
    b, _ = app_env

    async def run(client):
        assert "♾" in b.report_text()
        _, d = await get(client, "/api/profit")
        a = d["autonomy"]
        assert a["on"] and a["every_h"] == 6 and a["engines"] and a["next"]
        _, d = await post(client, "/api/profit", {"auto_every": 24})
        assert d["autonomy"]["every_h"] == 24 and "раз в 24 ч" in d["done"][0]
        st, _ = await post(client, "/api/profit", {"auto_every": 5})
        assert st == 400
        _, d = await post(client, "/api/profit", {"auto": False})
        assert d["autonomy"]["on"] is False
        b.autonomy.st["real"] = True
        _, d = await post(client, "/api/profit", {"auto_real": False})
        assert d["autonomy"]["real"] is False
        _, d = await post(client, "/api/profit", {"auto_real": True})  # включить деньги из приложения нельзя
        assert d["autonomy"]["real"] is False
        st, _ = await post(client, "/api/profit", {"auto": True}, uid=FRIEND)  # не владелец
        assert st == 403
        _, ov = await get(client, "/api/overview")
        assert ov["profit"]["auto"] is False

    call(b, run)
