"""v38: ИИ без телефона — без ключа (OVHcloud, LLM7), по номеру РФ (GigaChat, YandexGPT)."""

from __future__ import annotations

import asyncio
import base64
import ssl
import time
from types import SimpleNamespace

from aiohttp import web

from bot import council
from bot.ai import (ACCESS, NO_AI_TEXT, PROVIDERS, RU_CA_FILE, AIAssistant, Endpoint, LLMClient, endpoints_from_env,
                    keyless_endpoints, ru_ssl)
from tests.test_ai import FakeLLM, serve
from tests.test_telegram_flow import send

AUTH_KEY = base64.b64encode(b"client-id:secret").decode()


def test_every_provider_says_what_signup_needs():
    for name, p in PROVIDERS.items():
        assert p.get("access") in ACCESS, name
    assert PROVIDERS["mistral"]["access"] == PROVIDERS["nvidia"]["access"] == "phone"
    assert PROVIDERS["gigachat"]["access"] == "ru" and PROVIDERS["groq"]["access"] == "nophone"
    assert {n for n, p in PROVIDERS.items() if p["access"] == "keyless"} == {"ovh", "llm7"}
    assert "телефон" in ACCESS["phone"] and "РФ" in ACCESS["ru"]
    assert "GIGACHAT_AUTH_KEY" in NO_AI_TEXT and "AI_KEYLESS=0" in NO_AI_TEXT


def test_keyless_ais_on_by_default_and_can_be_turned_off(monkeypatch):
    eps = keyless_endpoints({})
    assert [e.provider for e in eps] == ["ovh", "llm7"] and all(e.keyless and not e.key for e in eps)
    assert eps[0].base == "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
    for off in ("0", "no", "off", "нет"):
        assert keyless_endpoints({"AI_KEYLESS": off}) == []
    # свой бесплатный токен LLM7 (по почте) — тогда это уже обычный ключ, без дубля без ключа
    have = endpoints_from_env({"LLM7_API_KEY": "tok"})
    assert [e.provider for e in have] == ["llm7"] and not have[0].keyless
    assert [e.provider for e in keyless_endpoints({}, have)] == ["ovh"]
    # в боте: сначала ИИ с ключами, без ключа — в конце (запасные и голоса консилиума)
    monkeypatch.setenv("AI_KEYLESS", "1")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_1")
    ai = AIAssistant.from_env()
    assert [e.provider for e in ai.client.endpoints] == ["groq", "ovh", "llm7"] and ai.configured
    monkeypatch.setenv("AI_KEYLESS", "0")
    monkeypatch.delenv("GROQ_API_KEY")
    assert not AIAssistant.from_env().configured


class Keyless(FakeLLM):
    """Сервер без ключа: заголовок Authorization не нужен (и не должен приходить)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.auth_headers: list = []

    def _auth_ok(self, request):
        self.auth_headers.append(request.headers.get("Authorization"))
        return True


def test_keyless_call_sends_no_key_and_429_moves_on_without_waiting():
    async def run():
        free = Keyless(models=["Meta-Llama-3_3-70B-Instruct", "gpt-oss-120b"], reply="Ответ OVH")
        busy = Keyless(models=["gpt-oss:20b"], status=429)
        r1, base1 = await serve(free)
        r2, base2 = await serve(busy)
        client = LLMClient([Endpoint("llm7", base2, ""), Endpoint("ovh", base1, "")])
        try:
            t0 = time.monotonic()
            answer = await client.chat([{"role": "user", "content": "привет"}])
            spent = time.monotonic() - t0
        finally:
            await client.close()
            await r1.cleanup()
            await r2.cleanup()
        assert answer == "Ответ OVH" and client.last_provider == "OVHcloud"
        assert free.requests[0]["model"] == "gpt-oss-120b"
        assert set(free.auth_headers) == {None} and set(busy.auth_headers) == {None}
        assert spent < 5  # лимит без ключа общий на IP сервера: не ждём, а отдаём вопрос другому ИИ
        assert client.endpoints[0].disabled_until > time.time() + 30

    asyncio.run(run())


class FakeSber:
    """OAuth Сбера (ключ авторизации → токен на 30 минут) + GigaChat chat/completions."""

    def __init__(self):
        self.tokens = 0
        self.oauth: list[dict] = []
        self.chats = 0
        self.expire_next = False

    def app(self):
        app = web.Application()
        app.router.add_post("/api/v2/oauth", self.token)
        app.router.add_get("/v1/models", self.models)
        app.router.add_post("/v1/chat/completions", self.chat)
        return app

    async def token(self, request):
        form = await request.post()
        self.oauth.append({"auth": request.headers.get("Authorization"), "rq": request.headers.get("RqUID"),
                           "scope": form.get("scope"), "ctype": request.headers.get("Content-Type", "")})
        if request.headers.get("Authorization") != f"Basic {AUTH_KEY}":
            return web.json_response({"code": 6, "message": "Credentials doesn't match"}, status=401)
        self.tokens += 1
        return web.json_response({"access_token": f"tok{self.tokens}",
                                  "expires_at": int((time.time() + 1800) * 1000)})  # миллисекунды

    def _ok(self, request):
        return request.headers.get("Authorization") == f"Bearer tok{self.tokens}" and self.tokens

    async def models(self, request):
        if not self._ok(request):
            return web.json_response({"message": "Unauthorized"}, status=401)
        return web.json_response({"data": [{"id": "GigaChat"}, {"id": "GigaChat-2-Pro"}, {"id": "GigaChat-2-Max"}]})

    async def chat(self, request):
        if self.expire_next:
            self.expire_next = False
            self.tokens += 1  # старый токен «протух» на стороне Сбера
        if not self._ok(request):
            return web.json_response({"message": "Token has expired"}, status=401)
        self.chats += 1
        body = await request.json()
        return web.json_response({"choices": [{"message": {"content": f"Привет от {body['model']}"}}],
                                  "usage": {"total_tokens": 10}})


def test_gigachat_swaps_auth_key_for_token_and_refreshes_it():
    async def run():
        sber = FakeSber()
        runner, base = await serve(sber)  # base = http://127.0.0.1:port/v1
        root = base.removesuffix("/v1")
        ep = endpoints_from_env({"GIGACHAT_AUTH_KEY": AUTH_KEY, "GIGACHAT_BASE_URL": base,
                                 "GIGACHAT_AUTH_URL": f"{root}/api/v2/oauth"})[0]
        assert ep.provider == "gigachat" and ep.scope == "GIGACHAT_API_PERS" and not ep.keyless
        client = LLMClient([ep])
        try:
            a1 = await client.chat([{"role": "user", "content": "привет"}])
            a2 = await client.chat([{"role": "user", "content": "ещё"}])
            assert sber.tokens == 1  # токен живёт 30 минут: второй вопрос — без нового входа
            sber.expire_next = True
            a3 = await client.chat([{"role": "user", "content": "снова"}])
        finally:
            await client.close()
            await runner.cleanup()
        assert a1 == a2 == a3 == "Привет от GigaChat-2-Max" and client.last_provider == "GigaChat (Сбер)"
        o = sber.oauth[0]
        assert o["auth"] == f"Basic {AUTH_KEY}" and o["scope"] == "GIGACHAT_API_PERS" and len(o["rq"]) == 36
        assert o["ctype"].startswith("application/x-www-form-urlencoded")
        assert 1700 < ep.token_exp - time.time() <= 1800  # миллисекунды Сбера поняты верно
        assert len(sber.oauth) == 2 and sber.chats == 3  # после 401 взял новый токен сам

    asyncio.run(run())


def test_gigachat_wrong_key_explains_where_to_copy():
    async def run():
        sber = FakeSber()
        runner, base = await serve(sber)
        ep = endpoints_from_env({"GIGACHAT_CREDENTIALS": "bad", "GIGACHAT_BASE_URL": base,
                                 "GIGACHAT_AUTH_URL": base.removesuffix("/v1") + "/api/v2/oauth"})[0]
        client = LLMClient([ep])
        try:
            try:
                await client.chat([{"role": "user", "content": "привет"}])
                raise AssertionError("ждали ошибку")
            except Exception as e:
                text = str(e)
        finally:
            await client.close()
            await runner.cleanup()
        assert "Authorization Key" in text and "developers.sber.ru" in text

    asyncio.run(run())


def test_russian_root_ca_is_bundled_for_gigachat():
    assert RU_CA_FILE.exists() and "BEGIN CERTIFICATE" in RU_CA_FILE.read_text()
    ctx = ru_ssl()
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED
    names = [dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()]
    assert "Russian Trusted Root CA" in names
    assert LLMClient._tls(Endpoint("gigachat", "https://api.giga.chat/v1", "k")) == {"ssl": ctx}
    assert LLMClient._tls(Endpoint("groq", "https://api.groq.com/openai/v1", "gsk_1")) == {}


def test_yandex_needs_folder_and_sends_it():
    assert endpoints_from_env({"YANDEX_API_KEY": "AQVN1"}) == []  # без каталога не включаем
    ep = endpoints_from_env({"YANDEX_API_KEY": "AQVN1", "YANDEX_FOLDER_ID": "b1gfolder"})[0]
    assert ep.model == "gpt://b1gfolder/yandexgpt/latest" and ep.fallback == ["gpt://b1gfolder/yandexgpt-lite/latest"]
    h = LLMClient([ep])._headers(ep)
    assert h["Authorization"] == "Bearer AQVN1" and h["OpenAI-Project"] == "b1gfolder"
    ep2 = endpoints_from_env({"AI_API_KEY": "AQVN1", "AI_PROVIDER": "yandex", "YANDEX_FOLDER_ID": "f",
                              "YANDEX_MODEL": "yandexgpt-5.1"})[0]
    assert ep2.provider == "yandex" and ep2.model == "gpt://f/yandexgpt-5.1"


def test_keys_text_groups_by_what_signup_needs():
    ai = SimpleNamespace(client=SimpleNamespace(endpoints=[Endpoint("ovh", "x", ""), Endpoint("groq", "y", "gsk_1")]))
    text = council.keys_text(ai)
    order = [text.index(ACCESS[g]) for g in council.KEYS_ORDER]
    assert order == sorted(order)
    assert text.index("Mistral") > text.index(ACCESS["phone"]) > text.index("GigaChat")
    assert "✅ <b>OVHcloud</b>" in text and "⏸ <b>LLM7</b>" in text and "✅ <b>Groq</b>" in text
    assert "GIGACHAT_AUTH_KEY" in text and "developers.sber.ru/studio" in text and len(text) < 4096


def test_council_keys_command_works_even_without_ai(bot_env):
    b, session = bot_env

    async def run():
        assert not b.ai.configured
        await send(b, "/council keys")
        assert "GigaChat за 5 минут" in session.sent[-1][1]
        await send(b, "/council")
        assert "пока не подключён" in session.sent[-1][1]

    asyncio.run(run())


class NoList(Keyless):
    async def list_models(self, request):
        return web.json_response({"error": "auth required"}, status=401)


def test_keyless_without_model_list_uses_default_model():
    async def run():
        fake = NoList(reply="ok")
        runner, base = await serve(fake)
        client = LLMClient([Endpoint("ovh", base, "")])
        try:
            assert await client.chat([{"role": "user", "content": "привет"}]) == "ok"
        finally:
            await client.close()
            await runner.cleanup()
        assert fake.requests[0]["model"] == "gpt-oss-120b"

    asyncio.run(run())
