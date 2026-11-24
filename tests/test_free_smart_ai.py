"""v39: самые умные бесплатные ИИ без телефона (Ollama Cloud, SambaNova, Z.ai, Cloudflare) и 🩺 проверка каждого ИИ."""

from __future__ import annotations

import asyncio
import time

from aiohttp import web

from bot import doctor
from bot.ai import PROVIDERS, AIAssistant, Endpoint, LLMClient, endpoints_from_env, pick_model
from tests.test_ai import FakeLLM, serve
from tests.test_ai_flow import buttons, press
from tests.test_telegram_flow import send

ACCOUNT = "0123456789abcdef0123456789abcdef"


def test_new_free_providers_from_env():
    env = {"OLLAMA_API_KEY": "o1", "SAMBANOVA_API_KEY": "s2", "ZHIPU_API_KEY": "z3", "CLOUDFLARE_API_TOKEN": "c4",
           "CLOUDFLARE_ACCOUNT_ID": ACCOUNT}
    eps = endpoints_from_env(env)
    assert [e.provider for e in eps] == ["ollama", "sambanova", "zai", "cloudflare"]
    assert eps[0].base == "https://ollama.com/v1" and eps[1].base == "https://api.sambanova.ai/v1"
    assert eps[2].base == "https://api.z.ai/api/paas/v4"
    assert eps[3].base == f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/v1"
    # Cloudflare без ID аккаунта (или с мусором) не включаем — иначе каждый вопрос падал бы с ошибкой
    assert endpoints_from_env({"CLOUDFLARE_API_TOKEN": "c4"}) == []
    assert endpoints_from_env({"CF_API_TOKEN": "c4", "CLOUDFLARE_ACCOUNT_ID": "my-account"}) == []
    for name in ("ollama", "sambanova", "zai", "cloudflare"):
        assert PROVIDERS[name]["access"] == "nophone" and PROVIDERS[name]["signup"].startswith("https://")


def test_smartest_free_models_are_picked():
    assert pick_model("ollama", ["gpt-oss:120b", "deepseek-v4-flash", "deepseek-v4-pro:0813", "qwen3.5:397b"]) \
        == "deepseek-v4-pro:0813"
    assert pick_model("sambanova", ["Meta-Llama-3.3-70B-Instruct", "DeepSeek-V3.1", "DeepSeek-V3.2"]) == "DeepSeek-V3.2"
    assert pick_model("llm7", ["gpt-oss:20b", "deepseek-v4-flash-0731"]) == "deepseek-v4-flash-0731"
    assert pick_model("openrouter", ["qwen/qwen3-coder:free", "deepseek/deepseek-v4-flash:free",
                                     "deepseek/deepseek-v4-pro"]) == "deepseek/deepseek-v4-flash:free"
    # Z.ai и Cloudflare: только точно бесплатные модели. Платную «flashx» и GLM-5 не берём никогда
    assert pick_model("zai", ["glm-5.3", "glm-4.7-flashx", "glm-4.7-flash"]) == "glm-4.7-flash"
    assert pick_model("zai", ["glm-5.3", "glm-4.7-flashx"]) is None
    assert pick_model("cloudflare", ["@cf/moonshotai/kimi-k2.6", "@cf/openai/gpt-oss-120b"]) == "@cf/openai/gpt-oss-120b"


class PaidOnly(FakeLLM):
    """Модель только для подписчиков: 403 «requires a subscription» → бот берёт бесплатную модель."""

    async def chat(self, request):
        body = await request.json()
        if body["model"] == "deepseek-v4-pro":
            self.requests.append(body)
            return web.json_response({"error": "this model requires a subscription, upgrade for access"}, status=403)
        return await self._answer(body)

    async def _answer(self, body):
        self.requests.append(body)
        return web.json_response({"choices": [{"message": {"content": f"ok {body['model']}"}}], "usage": {}})


def test_paid_only_model_switches_to_free_one():
    async def run():
        fake = PaidOnly(models=["deepseek-v4-pro", "gpt-oss:120b"], key="o1")
        runner, base = await serve(fake)
        client = LLMClient([Endpoint("ollama", base, "o1")])
        try:
            answer = await client.chat([{"role": "user", "content": "привет"}])
        finally:
            await client.close()
            await runner.cleanup()
        assert answer == "ok gpt-oss:120b" and "deepseek-v4-pro" in client.endpoints[0].dead_models

    asyncio.run(run())


def test_ping_all_checks_every_ai_without_burning_paused_ones():
    async def run():
        good = FakeLLM(models=["openai/gpt-oss-120b"], reply="работает")
        bad = FakeLLM(models=["x"], key="другой")
        r1, b1 = await serve(good)
        r2, b2 = await serve(bad)
        paused = Endpoint("sambanova", "http://127.0.0.1:9/v1", "s")
        paused.disabled_until = time.time() + 120
        client = LLMClient([Endpoint("groq", b1, "gsk_ok"), Endpoint("cerebras", b2, "csk-bad"), paused])
        try:
            rows = await client.ping_all()
        finally:
            await client.close()
            await r1.cleanup()
            await r2.cleanup()
        ok, wrong, rest = rows
        assert ok["ok"] and ok["text"] == "работает" and ok["model"] == "openai/gpt-oss-120b" and ok["sec"] < 10
        assert not wrong["ok"] and "ключ не подходит" in wrong["error"]
        assert rest["paused"] > 100 and not rest["ok"] and not rest["error"]
        assert client.requests_today == 2  # на паузе — не спрашивали

    asyncio.run(run())


def _fake_ai(b, eps):
    b.ai = AIAssistant(LLMClient(eps))


def test_doctor_ai_button_checks_each_ai(bot_env):
    b, session = bot_env

    async def run():
        good = FakeLLM(models=["openai/gpt-oss-120b"], reply="работает")
        bad = FakeLLM(models=["x"], key="другой")
        r1, b1 = await serve(good)
        r2, b2 = await serve(bad)
        _fake_ai(b, [Endpoint("groq", b1, "gsk_ok"), Endpoint("ollama", b2, "o-bad")])
        try:
            await send(b, "/doctor ai")
            text = session.sent[-1][1]
            assert "Проверка ИИ" in text and "Groq: ответил за" in text and "openai/gpt-oss-120b" in text
            assert "Ollama Cloud: ключ не подходит" in text and "OLLAMA_API_KEY" in text
            assert "отвечают 1 из 2" in text and "минимум 2" in text
            assert "Можно добавить бесплатно" in text and "SambaNova" in text and "YandexGPT" not in text
            assert buttons(session.markups[-1]) == ["dr:all", "dr:ai", "cn:keys"]
            n = len(session.sent)
            await press(b, "dr:ai")
            assert len(session.sent) > n and "Проверка ИИ" in session.sent[-1][1]
            await press(b, "cn:keys")
            assert "Ollama Cloud" in session.sent[-1][1] and "CLOUDFLARE_API_TOKEN" in session.sent[-1][1]
        finally:
            await b.ai.client.close()
            await r1.cleanup()
            await r2.cleanup()

    asyncio.run(run())


def test_full_check_has_ai_and_automat_and_start_button(bot_env):
    b, session = bot_env

    async def run():
        good = FakeLLM(models=["openai/gpt-oss-120b"], reply="работает")
        r1, b1 = await serve(good)
        good2 = FakeLLM(models=["DeepSeek-V3.2"], reply="работает", key="s")
        r2, b2 = await serve(good2)
        _fake_ai(b, [Endpoint("groq", b1, "gsk_ok"), Endpoint("sambanova", b2, "s")])
        try:
            checks = await doctor.run_doctor(b)
            groups = {c.group for c in checks}
            assert {"Биржи", "ИИ", "Автомат", "Сервис"} <= groups
            total = next(c for c in checks if c.title == "Итог по ИИ")
            assert total.status == "ok" and "консилиум готов" in total.text
            assert any(c.title == "♾ Полный автомат" for c in checks)
            assert any(c.title == "GMGN" for c in checks) and any(c.title == "Кошелёк мемкоинов" for c in checks)
            await send(b, "/start")
            assert "dr:all" in buttons(session.markups[-1])
            await press(b, "dr:all")
            text = session.sent[-1][1]
            assert "Проверка всего" in text and "SambaNova: ответил за" in text and "Итог:" in text
        finally:
            await b.ai.client.close()
            await r1.cleanup()
            await r2.cleanup()

    asyncio.run(run())


def test_doctor_without_any_ai_explains_how_to_add(bot_env):
    b, _ = bot_env
    checks = asyncio.run(doctor.check_ai(b))
    first = checks[0]
    assert first.status == "warn" and "/council keys" in first.text
    assert any("Ollama Cloud" in c.text for c in checks)
