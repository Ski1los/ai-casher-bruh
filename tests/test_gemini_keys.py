"""Ключи Gemini нового формата «AQ.» (Google AI Studio выдаёт их с 2026 г.) и старые «AIza»: распознавание, очистка
от мусора при копировании, родной API Gemini (заголовок x-goog-api-key, а не Bearer), проверка ключа в боте.

Ключи в тестах ненастоящие."""

import asyncio

from aiohttp import web

from bot import mentors as mn
from bot.ai import Endpoint, LLMClient, clean_key, detect_provider, endpoints_from_env
from tests.test_mentors import FakeGemini

NEW = "AQ.AbTESTtestTESTtestTESTtestTESTtestTESTtestTESTte"
OLD = "AIzaSyTESTtestTESTtestTESTtestTESTtest"


def test_new_key_format_recognized_everywhere():
    assert detect_provider(NEW) == "gemini" and detect_provider(OLD) == "gemini"
    eps = endpoints_from_env({"AI_API_KEY": NEW})
    assert [e.provider for e in eps] == ["gemini"] and "/openai" not in eps[0].base
    assert mn.gemini_key({"GEMINI_API_KEY": NEW}) == NEW
    assert mn.gemini_key({"AI_API_KEY": NEW}) == NEW
    assert mn.gemini_key({"GOOGLE_API_KEY": NEW}) == NEW  # другое привычное имя переменной
    assert endpoints_from_env({"GOOGLE_API_KEY": NEW})[0].provider == "gemini"


def test_key_cleanup_from_phone_copy_paste():
    for raw in (f"  {NEW}\n", f'"{NEW}"', f"«{NEW}»", f"GEMINI_API_KEY={NEW}", f"Bearer {NEW}",
                NEW[:20] + "\n" + NEW[20:]):
        assert clean_key(raw) == NEW, raw
    assert mn.gemini_key({"GEMINI_API_KEY": f'"{NEW} "'}) == NEW


def test_wrong_value_gives_clear_hint():
    key, env, hint = mn.gemini_key_info({"GEMINI_API_KEY": "gsk_groqkey"})
    assert key == "" and "GEMINI_API_KEY" in hint and "AQ." in hint
    assert mn.gemini_key_info({}) == ("", None, None)


class FakeGeminiNative:
    """Родной API Gemini: принимает ключ только в x-goog-api-key (как Google для ключей AQ.)."""

    def __init__(self, key=NEW):
        self.key = key
        self.bodies: list[dict] = []

    def app(self):
        app = web.Application()
        app.router.add_get("/v1beta/models", self.models)
        app.router.add_post("/v1beta/models/{model}:generateContent", self.generate)
        return app

    def _ok(self, request):
        return request.headers.get("x-goog-api-key") == self.key and "Authorization" not in request.headers

    async def models(self, request):
        if not self._ok(request):
            return web.json_response({"error": {"message": "API key not valid"}}, status=400)
        return web.json_response({"models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-tts", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]}]})

    async def generate(self, request):
        if not self._ok(request):
            return web.json_response({"error": {"message": "API key not valid. Please pass a valid API key."}},
                                     status=400)
        body = await request.json()
        self.bodies.append({"model": request.match_info["model"], **body})
        return web.json_response({"candidates": [{"content": {"parts": [
            {"text": "думаю…", "thought": True}, {"text": "**Ответ** Gemini"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"totalTokenCount": 77}})


async def serve(fake):
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1beta"


def test_ai_helper_uses_native_gemini_api_with_new_key():
    async def run():
        fake = FakeGeminiNative()
        runner, base = await serve(fake)
        client = LLMClient([Endpoint("gemini", base, NEW)])
        try:
            answer = await client.chat([{"role": "system", "content": "Ты помощник"},
                                        {"role": "user", "content": "привет"},
                                        {"role": "assistant", "content": "здравствуй"},
                                        {"role": "user", "content": "как дела?"}])
            assert answer == "**Ответ** Gemini" and client.last_model == "gemini-3.5-flash"
            body = fake.bodies[0]
            assert body["systemInstruction"]["parts"][0]["text"] == "Ты помощник"
            assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
            assert client.tokens_today == 77
            # неверный ключ → понятная ошибка, а не «не подключён»
            bad = LLMClient([Endpoint("gemini", base, "AQ.Abwrong")])
            try:
                await bad.chat([{"role": "user", "content": "x"}])
                raise AssertionError("ждали ошибку")
            except Exception as e:
                assert "ключ не подходит" in str(e) and "Copy key" in str(e)
            finally:
                await bad.close()
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(run())


def test_gemini_check_button(bot_env):
    from bot import mentors_ui
    from tests.test_ai_flow import press

    b, session = bot_env

    async def go():
        # ключа нет — подробная инструкция
        await press(b, "mn:gcheck")
        assert "Gemini не подключён" in session.sent[-1][1] and "AQ." in session.sent[-1][1]
        # ключ есть и рабочий
        g = FakeGemini()
        g.key = NEW

        async def check():
            return "gemini-3.5-flash"

        g.check = check
        b.mentors.gemini, b.mentors.gemini_env = g, "GEMINI_API_KEY"
        text = await mentors_ui.gemini_check_text(b)
        assert "Gemini подключён ✅" in text and "AQ.Ab…" in text and NEW not in text  # ключ целиком не показываем

        async def bad():
            raise mn.MentorError("Google отклонил ключ (ошибка 400): API key not valid")

        g.check = bad
        text = await mentors_ui.gemini_check_text(b)
        assert "не работает" in text and "Copy key" in text

    asyncio.run(go())
