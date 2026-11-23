"""Gemini перегружен (503 «high demand»): повтор, другая модель, возврат к лучшей и понятный текст вместо JSON."""

import asyncio
import json
import time

import pytest

from bot import ai, livewatch
from bot import mentors as mn
from bot.ai import AIError, BusyModels, Endpoint, LLMClient, is_busy, short_api_error, tidy_error
from bot.livewatch import LiveError, LiveGemini
from bot.mentors import GeminiVideo, MentorError, temporary_error

# ровно то, что прислал Google на скриншоте
BUSY_JSON = {"error": {"code": 503, "message": "This model is currently experiencing high demand. Spikes in demand "
                                               "are usually temporary. Please try again later.",
                       "status": "UNAVAILABLE"}}
MODELS = ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash"]
LIVE_OK = {"action": "none", "summary": "смотрит график"}
VIDEO_OK = {"summary": "ok", "market_view": "bullish", "calls": [], "tips": []}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    for mod in (ai, livewatch, mn):
        monkeypatch.setattr(mod, "BUSY_RETRY_SEC", 0, raising=False)


class Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def text(self):
        return self.body if isinstance(self.body, str) else json.dumps(self.body)

    async def json(self, content_type=None):
        return self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeGemini:
    """Поддельный Gemini: модели из busy отвечают 503, остальные — answer."""

    def __init__(self, answer, busy=()):
        self.answer = answer
        self.busy = set(busy)
        self.calls: list[str] = []
        self.bodies: list = []

    def get(self, url, **kw):
        return Resp(200, {"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
                                     for m in MODELS]})

    def post(self, url, json=None, data=None, **kw):
        model = url.rsplit("/models/", 1)[1].split(":")[0]
        self.calls.append(model)
        self.bodies.append(json if json is not None else data)
        if model in self.busy:
            return Resp(503, BUSY_JSON)
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": self.answer}]}, "finishReason": "STOP"}]})


def session_of(http):
    async def session():
        return http
    return session


# --- чистые функции -------------------------------------------------------------------------------

def test_helpers_short_text_and_busy_detection():
    assert is_busy(503) and is_busy(500) and is_busy(502) and is_busy(504)
    assert not is_busy(400) and not is_busy(429, "quota") and is_busy(429, "model is overloaded")
    assert short_api_error(json.dumps(BUSY_JSON)).startswith("This model is currently experiencing high demand")
    assert "{" not in short_api_error(json.dumps(BUSY_JSON))
    raw = ('kryptokinglive: Gemini: ошибка 503: {\n  "error": {\n    "code": 503,\n    "message": "This model is '
           'currently experiencing high demand. Spikes in demand are usually temporary. Please try again later')
    nice = tidy_error(raw)
    assert nice.startswith("kryptokinglive: Gemini: серверы Google сейчас перегружены (ошибка 503")
    assert "{" not in nice and "\n" not in nice
    assert tidy_error("обычная ошибка") == "обычная ошибка" and tidy_error(None) is None
    b = BusyModels()
    b.mark("m", now=1000)
    assert b.now(1000 + 60) == {"m"} and b.now(1000 + 11 * 60) == set()
    assert temporary_error(MentorError("Gemini: серверы Google сейчас перегружены (ошибка 503)"))
    assert temporary_error(MentorError("лимит")) and not temporary_error(MentorError("ключ не подходит"))


# --- 🎥 эфиры (то, что было на скриншоте) --------------------------------------------------------

def test_live_retries_then_switches_model_and_returns_later():
    http = FakeGemini(json.dumps(LIVE_OK), busy={"gemini-3.5-flash-lite"})
    g = LiveGemini("AQ.key", session_of(http))
    out = run(g.analyze(b"video", "video/mp4", "смотри"))
    assert out["summary"] == "смотрит график"
    # повтор той же модели, потом другая Flash-Lite (не Flash: у Flash всего ~20 запросов в день)
    assert http.calls == ["gemini-3.5-flash-lite", "gemini-3.5-flash-lite", "gemini-2.5-flash-lite"]
    assert g.model == "gemini-2.5-flash-lite" and "gemini-3.5-flash-lite" in g.busy.now()
    # пока перегрузка не прошла — работаем на замене
    http.calls.clear()
    run(g.analyze(b"video", "video/mp4", "смотри"))
    assert http.calls == ["gemini-2.5-flash-lite"]
    # 10 минут прошли, Google отпустило — возвращаемся к лучшей модели
    http.busy.clear()
    g.busy.until = {m: time.time() - 1 for m in g.busy.until}
    http.calls.clear()
    run(g.analyze(b"video", "video/mp4", "смотри"))
    assert http.calls == ["gemini-3.5-flash-lite"] and g.model == "gemini-3.5-flash-lite"


def test_live_uses_flash_only_when_all_lite_busy_and_clear_error_when_all_busy():
    http = FakeGemini(json.dumps(LIVE_OK), busy={"gemini-3.5-flash-lite", "gemini-2.5-flash-lite",
                                                  "gemini-flash-lite-latest"})
    g = LiveGemini("AQ.key", session_of(http))
    run(g.analyze(b"video", "video/mp4", "смотри"))
    assert http.calls[-1] in ("gemini-3.5-flash", "gemini-2.5-flash")
    http.busy |= set(MODELS) | {"gemini-flash-latest"}
    g2 = LiveGemini("AQ.key", session_of(http))
    with pytest.raises(LiveError) as e:
        run(g2.analyze(b"video", "video/mp4", "смотри"))
    msg = str(e.value)
    assert "перегружены" in msg and "503" in msg and "{" not in msg and "повторю" in msg
    assert len(http.calls) < 20  # не долбит сервер бесконечно


# --- 🤖 ответы ИИ в чате ------------------------------------------------------------------------

def test_chat_switches_model_and_explains_when_all_busy():
    http = FakeGemini("Ответ", busy={"gemini-3.5-flash"})
    client = LLMClient([Endpoint("gemini", "https://g/v1beta", "AQ.key")])

    async def fake_http():
        return http
    client._http = fake_http
    answer = run(client.chat([{"role": "user", "content": "привет"}]))
    assert answer == "Ответ" and client.last_model == "gemini-2.5-flash"
    assert http.calls == ["gemini-3.5-flash", "gemini-3.5-flash", "gemini-2.5-flash"]
    http.busy |= set(MODELS) | {"gemini-flash-latest"}
    client2 = LLMClient([Endpoint("gemini", "https://g/v1beta", "AQ.key")])
    client2._http = fake_http
    with pytest.raises(AIError) as e:
        run(client2.chat([{"role": "user", "content": "привет"}]))
    msg = str(e.value)
    assert "перегружены" in msg and "спроси ещё раз" in msg and "{" not in msg
    assert client2.endpoints[0].disabled_until > time.time()  # если есть второй ИИ (Groq) — ответит он


def test_chat_returns_to_pinned_model_after_overload():
    http = FakeGemini("Ответ", busy={"gemini-2.5-flash"})
    ep = Endpoint("gemini", "https://g/v1beta", "AQ.key", model="gemini-2.5-flash")
    client = LLMClient([ep])

    async def fake_http():
        return http
    client._http = fake_http
    run(client.chat([{"role": "user", "content": "x"}]))
    assert client.last_model == "gemini-3.5-flash"
    http.busy.clear()
    ep.busy.until = {}
    run(client.chat([{"role": "user", "content": "x"}]))
    assert client.last_model == "gemini-2.5-flash"  # модель из AI_MODEL снова главная


# --- 🎓 ролики YouTube ----------------------------------------------------------------------------

def test_video_busy_switches_and_old_media_resolution_fallback_works():
    http = FakeGemini(json.dumps(VIDEO_OK), busy={"gemini-3.5-flash"})
    g = GeminiVideo("AIzaTEST", session_of(http))
    res = run(g.analyze("https://www.youtube.com/watch?v=abcdefghijk"))
    assert res["market_view"] == "bullish" and http.calls[-1] == "gemini-2.5-flash"
    http.busy |= set(MODELS) | {"gemini-flash-latest"}
    g2 = GeminiVideo("AIzaTEST", session_of(http))
    with pytest.raises(MentorError) as e:
        run(g2.analyze("https://www.youtube.com/watch?v=abcdefghijk"))
    assert "перегружены" in str(e.value) and temporary_error(e.value) and "{" not in str(e.value)

    class OldModel(FakeGemini):
        def post(self, url, json=None, data=None, **kw):
            if "mediaResolution" in (json or {}).get("generationConfig", {}):
                self.calls.append("400")
                return Resp(400, {"error": {"code": 400, "message": 'Unknown name "mediaResolution"'}})
            return super().post(url, json=json, data=data, **kw)

    old = OldModel(json.dumps(VIDEO_OK))
    g3 = GeminiVideo("AIzaTEST", session_of(old))
    assert run(g3.analyze("https://www.youtube.com/watch?v=abcdefghijk"))["summary"] == "ok"
    assert old.calls.count("400") == 1  # раньше параметр возвращался в запрос и все попытки падали


def test_busy_video_is_postponed_not_lost():
    """Ролик при перегрузке Google не помечается «просмотренным» — его разберут позже, а не выбросят."""
    import inspect
    src = inspect.getsource(mn.Mentors)
    assert "if temporary_error(e):\n                        break" in src
    assert "wait_on_limit and temporary_error(e)" in src


def test_gemini_check_button_says_key_ok_when_google_busy():
    http = FakeGemini("ок", busy=set(MODELS) | {"gemini-flash-latest"})
    g = GeminiVideo("AIzaTEST", session_of(http))
    with pytest.raises(MentorError) as e:
        run(g.check())
    assert "ключ рабочий" in str(e.value) and "перегружены" in str(e.value)
