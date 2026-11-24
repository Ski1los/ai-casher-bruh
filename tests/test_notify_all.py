"""v40: 🔔 уведомления бота приходят всем пользователям (владелец + приглашённые), а не только владельцу."""

from __future__ import annotations

import asyncio

from bot import copy_ui, notify, users_ui
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import OWNER, send
from tests.test_webapp import call, post

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

FRIEND = 777


def _to(session, text_part: str) -> dict[int, str]:
    """Кому ушло сообщение с этим текстом: {чат: текст}."""
    return {chat: t for chat, t in session.sent if text_part in t}


def test_bot_events_reach_owner_and_invited_users(bot_env):
    b, session = bot_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def run():
        # события автомата/копитрейдинга/мемкоинов/эфиров/пульса идут через send_events
        await copy_ui.send_events(b, [{"text": "🐋 Кит купил BONK", "buttons": []}])
        got = _to(session, "Кит купил BONK")
        assert set(got) == {OWNER, FRIEND}
        # предупреждения бота (ключи, автоторговля, обновления) — тоже всем
        assert await b.notify("⚠️ Ключ Bybit истекает через 3 дня") == 2
        assert set(_to(session, "Ключ Bybit истекает")) == {OWNER, FRIEND}
        # ежедневный отчёт
        assert {OWNER, FRIEND} <= b._report_recipients()

    asyncio.run(run())


def test_everyone_can_mute_himself_and_owner_can_mute_anyone(bot_env):
    b, session = bot_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def run():
        await send(b, "/notify", user=FRIEND)
        assert "Уведомления</b>: включены" in session.sent[-1][1] and "Вася" in session.sent[-1][1]
        assert buttons(session.markups[-1]) == ["nt:off"]
        await press(b, "nt:off", user=FRIEND)
        assert FRIEND in b.muted
        await b.notify("📈 Сделка автомата")
        assert set(_to(session, "Сделка автомата")) == {OWNER}  # Вася выключил — ему не пришло
        await send(b, "/notify on", user=FRIEND)
        assert FRIEND not in b.muted

        # владелец в /users видит колокольчики и выключает любому
        await send(b, "/users")
        assert "🔔 <b>Вася</b>" in session.sent[-1][1] and "/notify" in session.sent[-1][1]
        assert f"us:nt:{FRIEND}" in buttons(session.markups[-1])
        await press(b, f"us:nt:{FRIEND}")
        assert FRIEND in b.muted
        # чужой не может трогать чужие уведомления
        await press(b, f"us:nt:{OWNER}", user=FRIEND)
        assert OWNER not in b.muted

        # главный владелец тоже может выключить себе — с предупреждением
        await send(b, "/notify off")
        assert OWNER in b.muted and "не узнаешь" in session.sent[-1][1]

    asyncio.run(run())


def test_muted_list_survives_restart(bot_env):
    b, _ = bot_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")
    notify.set_muted(b, FRIEND, True)
    state = b._settings_state()
    assert state["people"]["muted"] == [FRIEND]
    b.muted = set()
    users_ui.restore(b, state["people"])
    assert b.muted == {FRIEND}
    # убрали человека — и из списка «без уведомлений» тоже
    users_ui.remove_user(b, FRIEND)
    assert FRIEND not in b.muted and notify.recipients(b) == [OWNER]
    # без приглашённых и выключенных — в настройки ничего лишнего не пишем
    assert "people" not in b._settings_state()


def test_others_see_who_did_it(bot_env):
    b, session = bot_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def run():
        token = notify.ACTOR.set(FRIEND)
        try:
            await b.notify("🔴 Реальный режим включён")
        finally:
            notify.ACTOR.reset(token)
        got = _to(session, "Реальный режим включён")
        assert got[OWNER].startswith("👤 <b>Вася</b>: ") and not got[FRIEND].startswith("👤")

    asyncio.run(run())


def test_action_in_app_by_friend_is_shown_to_owner_with_name(app_env):
    b, session = app_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def flow(c):
        status, _ = await post(c, "/api/mode", {"live": False}, uid=FRIEND)
        assert status == 200
        for _ in range(20):  # уведомление уходит в фоне, после ответа приложению
            await asyncio.sleep(0.02)
            if len(_to(session, "Тестовый режим включён из приложения")) == 2:
                break
        got = _to(session, "Тестовый режим включён из приложения")
        assert got[OWNER].startswith("👤 <b>Вася</b>: ") and FRIEND in got

    call(b, flow)


def test_real_money_action_in_chat_is_mirrored_to_others(bot_env):
    b, session = bot_env
    users_ui.add_user(b, FRIEND, "Вася", "ссылка")

    async def run():
        await send(b, "/stop", user=FRIEND)
        got = _to(session, "Остановил всё")
        assert list(got) == [OWNER] and got[OWNER].startswith("👤 <b>Вася</b>")

    asyncio.run(run())
