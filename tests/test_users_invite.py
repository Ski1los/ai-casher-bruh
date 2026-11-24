"""👥 Пользователи бота (v37): приглашение по одноразовой ссылке, полный доступ, удаление, переживает перезапуск."""

from __future__ import annotations

import asyncio
import json
import re
import time

from bot import users_ui
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import OWNER, STRANGER, send

FRIEND = 777


def _link(text: str) -> str:
    m = re.search(r"start=inv_([A-Za-z0-9_-]+)", text)
    assert m, text
    return m.group(1)


def test_invite_link_gives_full_access_and_owner_can_remove(bot_env):
    b, session = bot_env
    b._username = "casher_bot"

    async def run():
        # чужой до приглашения — не пускают
        await send(b, "/scan", user=FRIEND)
        await send(b, "/start", user=FRIEND)
        assert "Доступ закрыт" in session.sent[-1][1]
        assert not b.is_owner(FRIEND)

        await send(b, "/users")
        assert "Пользователи бота" in session.sent[-1][1] and "полный доступ" in session.sent[-1][1]
        assert buttons(session.markups[-1])[0] == "us:inv"
        await press(b, "us:inv")
        text = session.sent[-1][1]
        assert "https://t.me/casher_bot?start=inv_" in text and "24 часа" in text
        code = _link(text)
        assert len(b.invites) == 1 and code in b.invites

        # друг открыл ссылку → полный доступ, владелец получил уведомление
        await send(b, f"/start inv_{code}", user=FRIEND)
        sent_to = {chat: t for chat, t in session.sent[-4:]}
        assert "Добро пожаловать" in sent_to[FRIEND] or any("Добро пожаловать" in t for _, t in session.sent[-4:])
        assert any(chat == OWNER and "присоединился" in t for chat, t in session.sent[-4:])
        assert FRIEND in b.allowed_ids and b.is_owner(FRIEND) and str(FRIEND) in b.users
        assert not b.invites  # ссылка одноразовая

        # права как у владельца: например, лимиты риска (раньше — только владелец)
        await send(b, "/set max_trade 7", user=FRIEND)
        assert "только владелец" not in session.sent[-1][1].lower()
        assert b.settings.max_trade_usd == 7

        # повторно ссылку использовать нельзя
        await send(b, f"/start inv_{code}", user=STRANGER)
        assert "недействительна" in session.sent[-1][1] and STRANGER not in b.allowed_ids

        # но управлять людьми может только главный владелец
        await send(b, "/users", user=FRIEND)
        assert "только главный владелец" in session.sent[-1][1]
        await press(b, "us:inv", user=FRIEND)
        assert not b.invites

        # сохранено в настройках (и в бэкапе) — переживёт перезапуск Render
        saved = json.loads(b.settings_file.read_text())
        assert str(FRIEND) in saved["people"]["users"]
        assert saved["people"]["users"][str(FRIEND)]["how"] == "ссылка"

        # друг подписался на сигналы и радар — после удаления рассылки ему не идут
        b.subscribers.add(FRIEND)
        b.radar_chats.add(FRIEND)
        # владелец убирает друга: подтверждение → доступ закрыт
        await send(b, "/users")
        assert f"us:ask:{FRIEND}" in buttons(session.markups[-1])
        await press(b, f"us:ask:{FRIEND}")
        assert "Убрать" in session.sent[-1][1] and f"us:del:{FRIEND}" in buttons(session.markups[-1])
        await press(b, f"us:del:{FRIEND}")
        assert any(chat == FRIEND and "закрыл тебе доступ" in t for chat, t in session.sent[-3:])
        assert FRIEND not in b.allowed_ids and not b.is_owner(FRIEND)
        assert FRIEND not in b.subscribers.chats and FRIEND not in b.radar_chats
        await send(b, "/start", user=FRIEND)
        assert "Доступ закрыт" in session.sent[-1][1]

    asyncio.run(run())


def test_restore_after_restart_and_expired_links(bot_env):
    b, session = bot_env
    now = time.time()
    users_ui.restore(b, {"users": {str(FRIEND): {"name": "Друг", "added": now, "how": "ссылка"}, "bad": {}},
                         "invites": {"old": now - 5, "fresh": now + 600, "junk": "x"}})
    assert b.is_owner(FRIEND) and FRIEND in b.allowed_ids
    assert set(b.invites) == {"fresh"}  # просроченные и битые выброшены
    assert "bad" not in b.users

    # просроченная ссылка не пускает
    b.invites["late"] = now - 1
    assert users_ui.accept_invite(b, "late", STRANGER, "Чужой") == "bad"
    # владелец открыл свою же ссылку — ничего не тратится
    assert users_ui.accept_invite(b, "fresh", OWNER, "Я") == "owner" and "fresh" in b.invites
    # у кого уже есть доступ — ссылку не тратим
    assert users_ui.accept_invite(b, "fresh", FRIEND, "Друг") == "already" and "fresh" in b.invites

    # одновременно не больше MAX_INVITES ссылок: старые гасятся
    for _ in range(users_ui.MAX_INVITES + 3):
        users_ui.new_invite(b)
    assert len(b.invites) == users_ui.MAX_INVITES

    # ID из ALLOWED_USER_IDS убрать из бота нельзя (только в Render)
    b.env_ids.add(4242)
    b.allowed_ids.add(4242)
    users_ui.add_user(b, 4242, "env", "ссылка")
    assert users_ui.remove_user(b, 4242) and 4242 in b.allowed_ids


def test_open_bot_ignores_invites(bot_env):
    """ALLOWED_USER_IDS пустой (бот открыт): приглашение не должно «закрыть» бот одним пользователем."""
    b, _ = bot_env
    b.owner_id = None
    b.allowed_ids.clear()
    b.invites["x"] = time.time() + 600
    assert users_ui.accept_invite(b, "x", FRIEND, "Друг") == "bad"
    assert not b.allowed_ids and not b.is_owner(FRIEND)
    users_ui.restore(b, {"users": {str(FRIEND): {"name": "Друг"}}})
    assert not b.allowed_ids


def test_invite_clear_and_webapp_owner_flag(bot_env):
    b, session = bot_env
    b._username = ""  # имя бота неизвестно — даём команду вместо ссылки

    async def run():
        await press(b, "us:inv")
        assert "/start inv_" in session.sent[-1][1]
        await send(b, "/users")
        assert "us:clear" in buttons(session.markups[-1])
        await press(b, "us:clear")
        assert not b.invites

    asyncio.run(run())
    # Mini App: приглашённый получает права владельца
    users_ui.add_user(b, FRIEND, "Друг", "ссылка")
    assert b.is_owner(FRIEND) and b.is_owner(OWNER) and not b.is_owner(STRANGER) and not b.is_owner(None)
