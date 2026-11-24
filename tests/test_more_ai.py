"""v37: больше ИИ (Cerebras, Mistral, NVIDIA, GitHub Models, DeepSeek) и 🧠 консилиум ИИ для анализа и торговли."""

from __future__ import annotations

import asyncio
import time

from aiohttp import web

from bot import council
from bot.ai import (PROVIDERS, AIAssistant, Endpoint, LLMClient, ai_keys_present, detect_provider, endpoints_from_env,
                    pick_model)
from tests.test_ai import FakeLLM, serve
from tests.test_ai_flow import buttons, press
from tests.test_autonomy import NOW, lab_proven, with_keys
from tests.test_telegram_flow import send
from tests.test_webapp import call, get

app_env = __import__("tests.test_webapp", fromlist=["app_env"]).app_env

TRADE_REPLY = ("ВЕРДИКТ: ТОРГОВАТЬ\nУВЕРЕННОСТЬ: 80\nГЛАВНОЕ: автопилот доказал +0.5% за сделку после комиссий\n"
               "ИДЕЯ: BTC на Bybit $5, стоп -1%\nРИСК: рынок развернётся")
WAIT_REPLY = ("**ВЕРДИКТ:** ЖДАТЬ\n**УВЕРЕННОСТЬ:** 75%\nГЛАВНОЕ: данных мало, комиссии съедают плюс\n"
              "ИДЕЯ: нет\nРИСК: потерять на комиссиях")
CAREFUL_REPLY = "ВЕРДИКТ: ОСТОРОЖНО\nУВЕРЕННОСТЬ: 60\nГЛАВНОЕ: только малой суммой\nИДЕЯ: нет\nРИСК: проскальзывание"


# ---------------------------------------------------------------------------
# Провайдеры
# ---------------------------------------------------------------------------


def test_new_providers_detected_and_loaded_from_env():
    assert detect_provider("csk-abc") == "cerebras"
    assert detect_provider("nvapi-xyz") == "nvidia"
    assert detect_provider("github_pat_11AAA") == "github" and detect_provider("ghp_x") == "github"
    assert detect_provider("gsk_1") == "groq" and detect_provider("AQ.Ab1") == "gemini"
    # ключи без приставки (Mistral, DeepSeek) по началу не угадываем — только по имени переменной
    assert detect_provider("abcdef123456") is None and detect_provider("sk-123") is None
    assert detect_provider("sk-or-v1-1") == "openrouter"

    env = {"GROQ_API_KEY": "gsk_1", "CEREBRAS_API_KEY": "csk-2", "MISTRAL_API_KEY": "m3", "NVIDIA_API_KEY": "nvapi-4",
           "GITHUB_MODELS_KEY": "github_pat_5", "DEEPSEEK_API_KEY": "sk-6"}
    eps = endpoints_from_env(env)
    assert [e.provider for e in eps] == ["groq", "cerebras", "mistral", "nvidia", "github", "deepseek"]
    assert {e.base for e in eps} >= {"https://api.cerebras.ai/v1", "https://api.mistral.ai/v1",
                                     "https://integrate.api.nvidia.com/v1", "https://models.github.ai/inference",
                                     "https://api.deepseek.com/v1"}
    # основной AI_API_KEY с новым ключом — провайдер определяется сам
    eps = endpoints_from_env({"AI_API_KEY": "nvapi-9", "AI_MODEL": "meta/llama-3.3-70b-instruct"})
    assert eps[0].provider == "nvidia" and eps[0].model == "meta/llama-3.3-70b-instruct"
    assert ai_keys_present({"MISTRAL_API_KEY": "x"}) and not ai_keys_present({})
    for name, p in PROVIDERS.items():  # у каждого есть, где взять ключ и что бесплатно
        assert p["signup"].startswith("https://") and p["free"], name


def test_pick_model_prefers_strong_models():
    assert pick_model("nvidia", ["meta/llama-3.1-8b-instruct", "nvidia/nv-embed-v1", "deepseek-ai/deepseek-v3.1",
                                 "deepseek-ai/deepseek-v3.2", "meta/llama-3.3-70b-instruct"]) == "deepseek-ai/deepseek-v3.2"
    assert pick_model("cerebras", ["llama3.1-8b", "gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507"]) == "gpt-oss-120b"
    assert pick_model("mistral", ["mistral-embed", "mistral-small-latest", "mistral-medium-latest",
                                  "ministral-8b-latest"]) == "mistral-medium-latest"


class NoListLLM(FakeLLM):
    """Как GitHub Models: списка моделей по адресу /models нет (404) — берём известные."""

    def app(self):
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self.chat)
        return app


def test_github_without_model_list_uses_fallback_and_cerebras_payload():
    async def run():
        gh = NoListLLM(key="github_pat_ok", dead_models={"openai/gpt-4.1"})
        cb = FakeLLM(key="csk-ok", models=["llama3.1-8b", "gpt-oss-120b"])
        r1, base1 = await serve(gh)
        r2, base2 = await serve(cb)
        client = LLMClient([Endpoint("github", base1, "github_pat_ok"), Endpoint("cerebras", base2, "csk-ok")])
        try:
            assert await client._call(client.endpoints[0], [{"role": "user", "content": "привет"}], 100) == "Ответ ИИ"
            assert [r["model"] for r in gh.requests] == ["openai/gpt-4.1", "openai/gpt-4o"]  # 4.1 «нет» → 4o
            assert await client._call(client.endpoints[1], [{"role": "user", "content": "привет"}], 100) == "Ответ ИИ"
            body = cb.requests[-1]
            assert body["model"] == "gpt-oss-120b" and body["reasoning_effort"] == "low"
            assert "include_reasoning" not in body  # этот параметр понимает только Groq
        finally:
            await client.close()
            await r1.cleanup()
            await r2.cleanup()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Консилиум: разбор ответов и подсчёт голосов
# ---------------------------------------------------------------------------


def test_parse_vote_formats():
    v = council.parse_vote("Groq", "gpt-oss", TRADE_REPLY)
    assert v.verdict == council.TRADE and v.confidence == 80 and "+0.5%" in v.main and "Bybit" in v.idea
    w = council.parse_vote("Gemini", None, WAIT_REPLY)
    assert w.verdict == council.WAIT and w.confidence == 75 and w.idea == "нет"
    # «не торговать» — это «ждать», а не «торговать»
    assert council.parse_vote("X", None, "ВЕРДИКТ: не торговать\nУВЕРЕННОСТЬ: 90").verdict == council.WAIT
    # формат не соблюдён: вердикт ищем в начале, уверенность по умолчанию 50
    f = council.parse_vote("Y", None, "Думаю, лучше осторожно: малой суммой.")
    assert f.verdict == council.CAREFUL and f.confidence == 50 and f.main
    assert council.parse_vote("Z", None, "Привет!").verdict is None


def V(verdict, conf=70):
    return council.Vote("p", None, verdict=verdict, confidence=conf)


def test_tally_is_cautious():
    T, C, W = council.TRADE, council.CAREFUL, council.WAIT
    assert council.tally([V(T), V(T), V(T)])[:3] == (T, 3, 3)
    # один «торговать» — не консилиум
    verdict, _, _, note = council.tally([V(T, 90)])
    assert verdict == C and "один" in note
    # 2 «торговать» против уверенного «ждать» → понижаем
    verdict, _, _, note = council.tally([V(T, 90), V(T, 90), V(W, 80)])
    assert verdict == C and "уверенно против" in note
    # меньше 2/3 за «торговать» → понижаем
    assert council.tally([V(T, 95), V(C, 30), V(W, 30)])[0] == C
    # ничья — в пользу осторожности
    assert council.tally([V(T, 60), V(W, 60)])[0] == W
    assert council.tally([V(C, 50), V(W, 50)])[0] == W
    assert council.tally([council.Vote("p", None, error="лимит")])[0] is None


def test_council_asks_all_ais_in_parallel_and_survives_failures():
    async def run():
        fakes = [FakeLLM(key="k1", reply=TRADE_REPLY), FakeLLM(key="k2", reply=WAIT_REPLY),
                 FakeLLM(key="k3", reply=WAIT_REPLY.replace("75", "65")), FakeLLM(key="k4", status=429)]
        servers = [await serve(f) for f in fakes]
        eps = [Endpoint(p, base, f.key) for p, f, (_, base) in zip(("groq", "cerebras", "mistral", "nvidia"),
                                                                  fakes, servers)]
        ai = AIAssistant(LLMClient(eps))
        try:
            t0 = time.monotonic()
            res = await council.run(ai, {"profit": "демо +1%"}, "Входить в BTC?")
            elapsed = time.monotonic() - t0
        finally:
            await ai.client.close()
            for r, _ in servers:
                await r.cleanup()
        assert elapsed < 5  # одновременно, а не по очереди
        assert res.verdict == council.WAIT and res.agree == 2 and res.answered == 3 and len(res.votes) == 4
        assert ai.client.requests_today == 4  # каждый ответ — в дневной лимит
        # каждый ИИ получил одни и те же данные и вопрос в строгом формате
        for f in fakes[:3]:
            last = f.requests[-1]["messages"]
            assert "демо +1%" in last[0]["content"] and "Входить в BTC?" in last[-1]["content"]
            assert "ВЕРДИКТ" in last[-1]["content"]
        text = council.render(res)
        assert "Консилиум ИИ" in text and "ответили 3 из 4" in text and "🔴 ЖДАТЬ" in text
        assert "не ответил" in text and "лимит" in text and "не гарантия" in text
        brief = res.brief()
        assert brief["verdict"] == "wait" and brief["why"] and brief["asked"] == 4

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Консилиум в Telegram
# ---------------------------------------------------------------------------


def test_council_command_keys_and_second_opinion_on_orders(bot_env):
    b, session = bot_env

    def reply(body):
        last = body["messages"][-1]["content"]
        if "ВЕРДИКТ" in last:
            return CAREFUL_REPLY
        return "Купим эфир.\nORDER buy bybit ETH 5 — по твоей просьбе"

    async def run():
        await send(b, "/council")
        assert "console.groq.com" in session.sent[-1][1]  # ИИ нет — объясняем, как подключить

        f1, f2 = FakeLLM(key="k1", reply=reply), FakeLLM(key="k2", reply=reply)
        (r1, base1), (r2, base2) = await serve(f1), await serve(f2)
        b.ai = AIAssistant(LLMClient([Endpoint("groq", base1, "k1"), Endpoint("mistral", base2, "k2")]))
        try:
            await send(b, "/council keys")
            text = session.sent[-1][1]
            assert "Подключено: 2" in text and "✅ <b>Groq</b>" in text and "➕ <b>Cerebras</b>" in text
            assert "GITHUB_MODELS_TOKEN" in text and "NVIDIA_API_KEY" in text

            await send(b, "/council")
            text = session.sent[-1][1]
            assert "Консилиум ИИ" in text and "ответили 2 из 2" in text and "🟡 ОСТОРОЖНО" in text
            assert b.council_last["verdict"] == "careful"
            assert b.ai_context()["extras"]["ai_council_last"]["answered"] == 2

            # ИИ предложил сделку → кнопка «второе мнение» → консилиум про эту сделку
            await send(b, "купи эфира на 5$ на байбите")
            assert "cn:order" in buttons(session.markups[-1])
            await press(b, "cn:order")
            q = f1.requests[-1]["messages"][-1]["content"]
            assert "buy 5 ETH на bybit" in q and "ВЕРДИКТ" in q
            assert "Консилиум ИИ" in session.sent[-1][1]
        finally:
            await b.ai.client.close()
            await r1.cleanup()
            await r2.cleanup()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Консилиум и ♾ Автомат: перед настоящими деньгами
# ---------------------------------------------------------------------------


def _two_ais(b, reply):
    async def chat_each(messages, max_tokens=700, limit=5):
        return [("Groq", "m1", reply, None), ("Mistral", "m2", reply, None)]

    b.ai = AIAssistant(LLMClient([Endpoint("groq", "http://x", "k1"), Endpoint("mistral", "http://y", "k2")]))
    b.ai.client.chat_each = chat_each


def test_autonomy_asks_council_and_waits_when_majority_says_wait(bot_env):
    b, session = bot_env
    a = b.autonomy
    lab_proven(b)
    with_keys(b)
    a.allow_real(True, NOW)
    _two_ais(b, WAIT_REPLY)
    assert a.council_due(NOW) == ["pilot"]

    asyncio.run(__import__("bot.autonomy", fromlist=["ask_council"]).ask_council(b, "pilot", NOW))
    assert a.st["council"]["pilot"]["verdict"] == "wait"
    assert any("Консилиум ИИ против включения денег" in t for _, t in session.sent)
    assert a.money_step(NOW) == [] and not b.trader.state.live  # не включил
    assert "консилиум ИИ против" in a.status_dict(NOW)["money"]["pilot"]
    assert a.council_due(NOW) == []  # 12 часов не переспрашивает
    # через 12 часов мнение устарело — спросит снова
    assert a.council_due(NOW + council.FRESH_SEC + 60) == ["pilot"]


def test_autonomy_promotes_when_council_agrees_or_ai_unavailable(bot_env):
    b, _ = bot_env
    a = b.autonomy
    lab_proven(b)
    with_keys(b)
    a.allow_real(True, NOW)
    _two_ais(b, CAREFUL_REPLY)
    asyncio.run(__import__("bot.autonomy", fromlist=["ask_council"]).ask_council(b, "pilot", NOW))
    events = a.money_step(NOW)
    assert events and "включил настоящие деньги" in events[0]["text"] and b.trader.state.live

    # ИИ сломались — обычные правила работают без консилиума (он может только запретить)
    b2_state = a.st["council"]
    b2_state.clear()

    async def broken(messages, max_tokens=700, limit=5):
        raise RuntimeError("нет связи")

    b.ai.client.chat_each = broken
    asyncio.run(__import__("bot.autonomy", fromlist=["ask_council"]).ask_council(b, "meme", NOW))
    assert a.st["council"]["meme"]["verdict"] is None and a.council_block("meme", NOW) is None


def test_autonomy_skips_council_with_one_ai(bot_env):
    b, _ = bot_env
    a = b.autonomy
    lab_proven(b)
    with_keys(b)
    a.allow_real(True, NOW)
    b.ai = AIAssistant(LLMClient([Endpoint("groq", "http://x", "k1")]))
    assert a.council_due(NOW) == []  # с одним ИИ консилиума нет — работает как раньше
    assert a.money_step(NOW)


# ---------------------------------------------------------------------------
# Приглашённый пользователь в Mini App — права владельца
# ---------------------------------------------------------------------------


def test_invited_user_is_owner_in_mini_app(app_env):
    from bot import users_ui
    b, _ = app_env
    guest = 5150

    async def fn(client):
        st_before, _ = await get(client, "/api/autostatus", uid=guest)
        users_ui.add_user(b, guest, "Гость", "ссылка")
        st_after, data = await get(client, "/api/autostatus", uid=guest)
        return st_before, st_after, data

    before, after, data = call(b, fn)
    assert before == 403 and after == 200 and "text" in data


def test_autonomy_tick_runs_council_before_money(bot_env):
    import inspect

    from bot import autonomy as au
    b, session = bot_env
    a = b.autonomy
    assert inspect.iscoroutinefunction(au.autonomy_loop) and inspect.iscoroutinefunction(au.ask_council)
    lab_proven(b)
    with_keys(b)
    a.allow_real(True, NOW)
    a.st["welcomed"] = True
    calls = []

    async def chat_each(messages, max_tokens=700, limit=5):
        calls.append(messages[-1]["content"])
        return [("Groq", "m1", WAIT_REPLY, None), ("Mistral", "m2", WAIT_REPLY, None)]

    b.ai = AIAssistant(LLMClient([Endpoint("groq", "http://x", "k1"), Endpoint("mistral", "http://y", "k2")]))
    b.ai.client.chat_each = chat_each
    asyncio.run(au.tick(b, NOW))
    assert len(calls) == 1 and "НАСТОЯЩИЕ деньги" in calls[0]
    assert not b.trader.state.live and "pilot" not in a.st["promoted"]
    asyncio.run(au.tick(b, NOW + 300))
    assert len(calls) == 1  # мнение свежее — не переспрашивает каждые 5 минут
