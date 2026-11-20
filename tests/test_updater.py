"""Самообновление: бот видит новую версию на GitHub и обновляется через Render Deploy Hook."""

import asyncio

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from bot import updater
from bot.cloud import BOT_VERSION, STATUS, status_html
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import send

HOOK = "https://api.render.com/deploy/srv-abc123XYZ?key=Qw_e-rty9"


def fresh_state():
    updater.UPDATE.update(latest=None, checked_at=0.0, error=None, triggered={}, notified={})


def test_hook_validation_and_versions():
    assert updater.hook_url({"RENDER_DEPLOY_HOOK_URL": HOOK}) == HOOK
    assert updater.hook_url({"RENDER_DEPLOY_HOOK_URL": "https://evil.example/deploy"}) is None
    assert updater.hook_problem({"RENDER_DEPLOY_HOOK_URL": "srv-123"}) and updater.hook_problem({}) is None
    assert updater.parse_version('x = 1\nBOT_VERSION = "21"  # comment') == "21"
    assert updater.is_newer("17", "16") and not updater.is_newer("16", "16") and not updater.is_newer(None)


def test_fetch_latest_and_trigger_against_fake_servers():
    hits = []

    async def cloud_py(_):
        return web.Response(text=f'BOT_VERSION = "{int(BOT_VERSION) + 1}"\n')

    async def deploy(request):
        hits.append(request.method)
        return web.json_response({"deploy": {"id": "dep-1"}}, status=201)

    async def gone(_):
        return web.Response(status=404)

    async def main():
        app = web.Application()
        app.router.add_get("/bot/cloud.py", cloud_py)
        app.router.add_post("/deploy", deploy)
        app.router.add_post("/gone", gone)
        server = TestServer(app)
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as s:
                latest = await updater.check(s, str(server.make_url("/bot/cloud.py")))
                ok, _ = await updater.trigger(s, str(server.make_url("/deploy")))
                bad, msg = await updater.trigger(s, str(server.make_url("/gone")))
                missing = await updater.check(s, str(server.make_url("/nope")))
            return latest, ok, bad, msg, missing
        finally:
            await server.close()

    fresh_state()
    latest, ok, bad, msg, missing = asyncio.run(main())
    assert latest == str(int(BOT_VERSION) + 1) and ok and hits == ["POST"]
    assert not bad and "заново" in msg and missing is None and "404" in updater.UPDATE["error"]


def test_update_command_without_hook_explains_manual_deploy(bot_env, monkeypatch):
    b, session = bot_env
    fresh_state()
    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL", raising=False)

    async def latest(_s, _u=None):
        updater.UPDATE["latest"] = "99"
        return "99"

    monkeypatch.setattr(updater, "check", latest)
    asyncio.run(send(b, "/update"))
    text = session.sent[-1][1]
    assert "v99" in text and "Manual Deploy" in text and "Deploy latest commit" in text
    assert "RENDER_DEPLOY_HOOK_URL" in text and "секретная" in text
    assert "доступна новая версия v99" in status_html()  # страница /status тоже говорит
    STATUS.pop("update", None)


def test_update_button_triggers_hook(bot_env, monkeypatch):
    b, session = bot_env
    fresh_state()
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    calls = []

    async def latest(_s, _u=None):
        updater.UPDATE["latest"] = "99"
        return "99"

    async def trig(_s, url):
        calls.append(url)
        return True, "Render принял команду обновиться"

    monkeypatch.setattr(updater, "check", latest)
    monkeypatch.setattr(updater, "trigger", trig)

    async def flow():
        await send(b, "/update")
        assert "up:go" in buttons(session.markups[-1])
        assert HOOK not in session.sent[-1][1]  # секрет никогда не показываем
        await press(b, "up:go")

    asyncio.run(flow())
    assert calls == [HOOK] and "Render принял" in session.sent[-1][1]
    STATUS.pop("update", None)


def test_new_version_self_update_and_reminder_once(bot_env, monkeypatch):
    b, session = bot_env
    fresh_state()
    calls = []

    async def trig(_s, url):
        calls.append(url)
        return True, "ok"

    monkeypatch.setattr(updater, "trigger", trig)
    monkeypatch.setenv("RENDER_DEPLOY_HOOK_URL", HOOK)
    asyncio.run(b._on_new_version(None, "99"))
    asyncio.run(b._on_new_version(None, "99"))  # второй раз сразу — не дёргаем Render снова
    assert calls == [HOOK] and "Обновляюсь сам" in session.sent[-1][1]

    monkeypatch.delenv("RENDER_DEPLOY_HOOK_URL")
    n = len(session.sent)
    asyncio.run(b._on_new_version(None, "100"))
    asyncio.run(b._on_new_version(None, "100"))  # напоминание не чаще раза в сутки
    assert len(session.sent) == n + 1 and "Manual Deploy" in session.sent[-1][1]
