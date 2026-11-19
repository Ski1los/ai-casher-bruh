"""ИИ-помощник: выбор провайдера и модели, разбор ответа, сетевые ошибки и переключение.

Вместо настоящего Groq/OpenRouter поднимается локальный HTTP-сервер с тем же API.
"""

import asyncio
import json

from aiohttp import web

from bot.ai import (
    AIAssistant,
    AIError,
    Endpoint,
    LLMClient,
    _retry_after,
    clean_answer,
    compact_json,
    detect_provider,
    endpoints_from_env,
    extract_proposals,
    pick_model,
    to_telegram_html,
    validate_setting,
)

# ---------------------------------------------------------------------------
# Чистые функции
# ---------------------------------------------------------------------------


def test_detect_provider_and_env():
    assert detect_provider("gsk_abc") == "groq"
    assert detect_provider("sk-or-v1-abc") == "openrouter"
    assert detect_provider("AIzaXYZ") == "gemini"
    assert detect_provider("xyz") is None

    eps = endpoints_from_env({"AI_API_KEY": "gsk_1", "OPENROUTER_API_KEY": "sk-or-2", "GEMINI_API_KEY": "AIza3"})
    assert [e.provider for e in eps] == ["groq", "openrouter", "gemini"]  # основной первым, затем запасные
    assert endpoints_from_env({}) == []
    # тот же ключ дважды не добавляется
    assert len(endpoints_from_env({"AI_API_KEY": "gsk_1", "GROQ_API_KEY": "gsk_1"})) == 1
    # свой адрес и модель
    custom = endpoints_from_env({"AI_API_KEY": "k", "AI_BASE_URL": "http://x/v1/", "AI_MODEL": "m"})
    assert custom[0].base == "http://x/v1" and custom[0].model == "m"
    # нераспознанный ключ без адреса пропускается, а не роняет бота
    assert endpoints_from_env({"AI_API_KEY": "weird"}) == []


def test_pick_model():
    groq = ["whisper-large-v3", "llama-3.1-8b-instant", "openai/gpt-oss-20b", "openai/gpt-oss-120b",
            "meta-llama/llama-guard-4-12b", "qwen/qwen3-32b"]
    assert pick_model("groq", groq) == "openai/gpt-oss-120b"
    assert pick_model("groq", groq, dead={"openai/gpt-oss-120b"}) == "openai/gpt-oss-20b"
    assert pick_model("groq", ["whisper-large-v3"]) is None

    gemini = ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-4-pro-preview",
              "gemini-3.8-flash-image", "text-embedding-004"]
    assert pick_model("gemini", gemini) == "gemini-3.8-flash"

    orouter = ["openai/gpt-5", "deepseek/deepseek-v4:free", "anthropic/claude", "openrouter/free"]
    assert pick_model("openrouter", orouter) == "openrouter/free"
    # только бесплатные модели: платные OpenRouter не выбирает никогда
    assert pick_model("openrouter", ["openai/gpt-5", "x/y"]) is None


def test_extract_proposals_validates():
    text = (
        "Комиссии съедают всё.\n\n"
        "SET tri_min=0.25 — треугольники теряют 0.1% на исполнении\n"
        "SET interval=1 — слишком часто\n"  # ниже минимума 10 с: отбрасывается
        "SET hack=5\n"  # неизвестный ключ
        "**SET min_profit = 1,5** | вывод дорогой\n"
        "SET tri_min=0.3\n"  # дубль ключа
    )
    cleaned, props = extract_proposals(text)
    assert [(p.key, p.value) for p in props] == [("tri_min", 0.25), ("min_profit", 1.5)]
    assert props[0].reason.startswith("треугольники")
    assert "SET" not in cleaned and cleaned.startswith("Комиссии")
    assert validate_setting("tri_min", 11) and validate_setting("nope", 1)
    assert validate_setting("amount", 100) is None


def test_to_telegram_html_is_safe():
    out = to_telegram_html("## Итог\n**Важно**: 1 < 2 & `tri_min`\n- пункт <script>\n|---|---|")
    assert "<b>Итог</b>" in out and "<b>Важно</b>" in out
    assert "&lt;script&gt;" in out and "1 &lt; 2 &amp;" in out
    assert "<code>tri_min</code>" in out and "• пункт" in out and "---" not in out
    assert clean_answer("<think>долго думаю</think>\nОтвет") == "Ответ"


def test_compact_json_trims_long_lists():
    data = {"a": list(range(5000)), "b": {"c": ["x" * 50] * 300}, "keep": 1}
    text = compact_json(data, limit=2000)
    assert len(text) <= 2000
    assert json.loads(text)["keep"] == 1


def test_retry_after_parsing():
    assert _retry_after({"retry-after": "7"}, "") == 7
    assert _retry_after({"x-ratelimit-reset-tokens": "1m2.5s"}, "") == 62.5
    assert _retry_after({}, "Please try again in 12.3s.") == 12.3
    assert _retry_after({}, "") == 60


# ---------------------------------------------------------------------------
# Фейковый сервер провайдера
# ---------------------------------------------------------------------------


class FakeLLM:
    """OpenAI-совместимый сервер: /models и /chat/completions с настраиваемыми ошибками."""

    def __init__(self, models=("openai/gpt-oss-120b",), reply="Ответ ИИ", status=200, dead_models=(), key="gsk_ok"):
        self.models = list(models)
        self.reply = reply
        self.status = status
        self.dead_models = set(dead_models)
        self.key = key
        self.requests: list[dict] = []

    def app(self):
        app = web.Application()
        app.router.add_get("/v1/models", self.list_models)
        app.router.add_post("/v1/chat/completions", self.chat)
        return app

    def _auth_ok(self, request):
        return request.headers.get("Authorization") == f"Bearer {self.key}"

    async def list_models(self, request):
        if not self._auth_ok(request):
            return web.json_response({"error": "bad key"}, status=401)
        return web.json_response({"data": [{"id": m} for m in self.models]})

    async def chat(self, request):
        body = await request.json()
        self.requests.append(body)
        if not self._auth_ok(request):
            return web.json_response({"error": {"message": "Invalid API Key"}}, status=401)
        if self.status == 429:
            return web.json_response(
                {"error": {"message": "Rate limit reached. Please try again in 45s."}},
                status=429, headers={"retry-after": "45"},
            )
        if body["model"] in self.dead_models:
            return web.json_response({"error": {"message": f"The model `{body['model']}` does not exist"}}, status=404)
        reply = self.reply(body) if callable(self.reply) else self.reply
        return web.json_response({
            "choices": [{"message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 321},
        })


async def serve(fake: FakeLLM):
    runner = web.AppRunner(fake.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


def test_client_happy_path_and_payload():
    async def run():
        fake = FakeLLM(models=["whisper-large-v3", "openai/gpt-oss-20b", "openai/gpt-oss-120b"])
        runner, base = await serve(fake)
        client = LLMClient([Endpoint("groq", base, "gsk_ok")])
        try:
            answer = await client.chat([{"role": "user", "content": "привет"}])
        finally:
            await client.close()
            await runner.cleanup()
        assert answer == "Ответ ИИ"
        body = fake.requests[0]
        assert body["model"] == "openai/gpt-oss-120b"  # выбрана лучшая модель из /models
        assert body["reasoning_effort"] == "low"  # экономим лимит токенов Groq
        assert client.requests_today == 1 and client.tokens_today == 321
        assert client.last_provider == "Groq"

    asyncio.run(run())


def test_client_skips_removed_model():
    async def run():
        fake = FakeLLM(models=["openai/gpt-oss-120b", "openai/gpt-oss-20b"], dead_models={"openai/gpt-oss-120b"})
        runner, base = await serve(fake)
        client = LLMClient([Endpoint("groq", base, "gsk_ok")])
        try:
            assert await client.chat([{"role": "user", "content": "x"}]) == "Ответ ИИ"
        finally:
            await client.close()
            await runner.cleanup()
        assert [r["model"] for r in fake.requests] == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]

    asyncio.run(run())


def test_client_falls_back_to_second_provider_on_rate_limit():
    async def run():
        limited = FakeLLM(status=429)
        backup = FakeLLM(models=["openrouter/free"], reply="ответ запасного", key="sk-or-ok")
        r1, base1 = await serve(limited)
        r2, base2 = await serve(backup)
        client = LLMClient([Endpoint("groq", base1, "gsk_ok"), Endpoint("openrouter", base2, "sk-or-ok")])
        try:
            assert await client.chat([{"role": "user", "content": "x"}]) == "ответ запасного"
            assert client.endpoints[0].disabled_until > 0  # Groq на паузе
            # следующий запрос сразу идёт к запасному, Groq не дёргаем
            await client.chat([{"role": "user", "content": "y"}])
            assert len(limited.requests) == 1
        finally:
            await client.close()
            await r1.cleanup()
            await r2.cleanup()

    asyncio.run(run())


def test_client_bad_key_and_daily_limit():
    async def run():
        fake = FakeLLM(key="gsk_right")
        runner, base = await serve(fake)
        client = LLMClient([Endpoint("groq", base, "gsk_wrong")], daily_limit=1)
        try:
            try:
                await client.chat([{"role": "user", "content": "x"}])
                raise AssertionError("должна быть ошибка")
            except AIError as e:
                assert "ключ не подходит" in str(e)
            try:
                await client.chat([{"role": "user", "content": "x"}])
                raise AssertionError("должна быть ошибка лимита")
            except AIError as e:
                assert "лимит" in str(e)
        finally:
            await client.close()
            await runner.cleanup()

    asyncio.run(run())


def test_client_no_network():
    async def run():
        client = LLMClient([Endpoint("groq", "http://127.0.0.1:9/v1", "gsk_x", model="m")])
        try:
            await client.chat([{"role": "user", "content": "x"}])
            raise AssertionError("должна быть ошибка")
        except AIError as e:
            assert "нет связи" in str(e)
        finally:
            await client.close()

    asyncio.run(run())


def test_assistant_ask_keeps_history_and_extracts_proposals():
    async def run():
        def reply(body):
            return f"Сообщений: {len(body['messages'])}\nSET tri_min=0.3 — запас на проскальзывание"

        fake = FakeLLM(reply=reply)
        runner, base = await serve(fake)
        ai = AIAssistant(LLMClient([Endpoint("groq", base, "gsk_ok")]))
        ctx = {"settings": {"tri_min": 0.15}, "analysis": {"fees_taker_percent": {"bybit": 0.1}}}
        try:
            a1 = await ai.ask(1, "почему нет сделок?", ctx)
            a2 = await ai.ask(1, "а на $50?", ctx)
            a3 = await ai.ask(2, "другой чат", ctx)
        finally:
            await ai.client.close()
            await runner.cleanup()
        assert "Сообщений: 2" in a1.html  # system + вопрос
        assert "Сообщений: 4" in a2.html  # + прошлый вопрос и ответ
        assert "Сообщений: 2" in a3.html  # у другого чата своя история
        assert [(p.key, p.value) for p in a1.proposals] == [("tri_min", 0.3)]
        system = fake.requests[0]["messages"][0]["content"]
        assert "ДАННЫЕ" in system and '"bybit":0.1' in system and "tri_min (" in system

    asyncio.run(run())
