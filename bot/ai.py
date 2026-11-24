"""ИИ-помощник: анализирует сканы, демо-счета, комиссии и настройки, отвечает на вопросы.

Работает через любой OpenAI-совместимый API. Бесплатные варианты без банковской карты:
  * Groq (рекомендуется): ключ начинается с gsk_, https://console.groq.com/keys
  * OpenRouter (модели :free): ключ начинается с sk-or-, https://openrouter.ai/keys
  * Google AI Studio (Gemini): ключ начинается с AQ. (новый формат с 2026 г.) или AIza (старый),
    https://aistudio.google.com/apikey. Gemini вызываем через родной адрес Google (заголовок x-goog-api-key):
    новые ключи AQ. на OpenAI-совместимом адресе Google часто отклоняет.

  * Cerebras (очень быстрый): ключ csk-, https://cloud.cerebras.ai
  * Mistral (бесплатный тариф Experiment): ключ без приставки → только MISTRAL_API_KEY, https://console.mistral.ai
  * NVIDIA NIM (DeepSeek, Kimi, Qwen бесплатно для проб): ключ nvapi-, https://build.nvidia.com
  * GitHub Models (GPT-4.1 бесплатно, ~50 запросов в день): токен github_pat_ с правом models:read
  * DeepSeek (платный, но очень дешёвый): DEEPSEEK_API_KEY, https://platform.deepseek.com

Достаточно одной переменной AI_API_KEY: провайдер определяется по началу ключа.
Можно задать несколько ключей (GROQ_API_KEY, GEMINI_API_KEY, CEREBRAS_API_KEY, MISTRAL_API_KEY, …):
если один упрётся в лимит или сломается, бот переключится на следующий. А 🧠 консилиум (/council)
спрашивает несколько ИИ сразу и показывает, в чём они сходятся.

Модель выбирается автоматически из списка, который отдаёт провайдер (/models), поэтому,
если провайдер уберёт модель, бот возьмёт другую. Свою модель можно задать в AI_MODEL.

ИИ НЕ торгует и не меняет настройки сам: он может только предложить изменение,
а применить его можно кнопкой в Telegram.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import ssl
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from .config import BOUNDS, EDITABLE, RISK_KEYS

log = logging.getLogger(__name__)

PROVIDERS: dict[str, dict] = {
    "groq": {
        "title": "Groq",
        "base": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY",
        "prefix": "gsk_",
        # по порядку предпочтения: подстроки id модели
        "prefer": ["gpt-oss-120b", "gpt-oss-20b", "qwen", "kimi", "llama-3.3-70b", "llama"],
        "fallback": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    },
    "openrouter": {
        "title": "OpenRouter",
        "base": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "prefix": "sk-or-",
        "prefer": ["openrouter/free", "gpt-oss-120b:free", "deepseek", "qwen", "gemma", "llama"],
        "only_free": True,
        "fallback": ["openrouter/free"],
    },
    "gemini": {
        "title": "Google Gemini",
        "base": "https://generativelanguage.googleapis.com/v1beta",  # родной API (не /openai): так работают ключи AQ.
        "env": "GEMINI_API_KEY",
        "prefix": ("AQ.", "AIza"),
        "prefer": ["flash"],
        "fallback": ["gemini-2.5-flash"],
    },
    # --- v37: ещё бесплатные «мозги». Все OpenAI-совместимые: тот же код, другой адрес. -----------------
    "cerebras": {
        "title": "Cerebras",
        "base": "https://api.cerebras.ai/v1",
        "env": "CEREBRAS_API_KEY",
        "prefix": "csk-",
        "prefer": ["gpt-oss-120b", "qwen-3-235b", "zai-glm", "llama-3.3-70b", "llama"],
        "fallback": ["gpt-oss-120b"],
        "signup": "https://cloud.cerebras.ai",
        "free": "самый быстрый; новым аккаунтам могут попросить карту (пробные $5)",
        "access": "nophone",
    },
    "mistral": {
        "title": "Mistral",
        "base": "https://api.mistral.ai/v1",
        "env": "MISTRAL_API_KEY",
        "prefix": None,  # ключ без приставки: узнаём только по имени переменной
        "prefer": ["mistral-medium-latest", "mistral-large-latest", "magistral-medium-latest", "mistral-small-latest"],
        "fallback": ["mistral-medium-latest", "mistral-small-latest"],
        "signup": "https://console.mistral.ai/api-keys",
        "free": "бесплатный тариф Experiment, без карты",
        "access": "phone",
    },
    "nvidia": {
        "title": "NVIDIA NIM",
        "base": "https://integrate.api.nvidia.com/v1",
        "env": "NVIDIA_API_KEY",
        "prefix": "nvapi-",
        "prefer": ["deepseek-v", "kimi-k2", "qwen3-235b", "llama-3.3-70b", "nemotron"],
        "fallback": ["deepseek-ai/deepseek-v3.1", "meta/llama-3.3-70b-instruct"],
        "signup": "https://build.nvidia.com",
        "free": "бесплатно для проб: DeepSeek, Kimi, Qwen, ~40 запросов в минуту",
        "access": "phone",
    },
    "github": {
        "title": "GitHub Models",
        "base": "https://models.github.ai/inference",
        "env": "GITHUB_MODELS_TOKEN",
        "prefix": ("github_pat_", "ghp_"),
        "prefer": ["gpt-4.1", "gpt-4o", "deepseek-v3"],
        # список моделей тут по другому адресу (/catalog/models), поэтому сразу берём известные
        "fallback": ["openai/gpt-4.1", "openai/gpt-4o", "openai/gpt-4.1-mini"],
        "signup": "https://github.com/settings/personal-access-tokens/new",
        "free": "GPT-4.1 бесплатно, ~50 запросов в день",
        "access": "nophone",
    },
    "deepseek": {
        "title": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
        "prefix": None,  # sk-… как у многих: только по имени переменной
        "prefer": ["deepseek-chat"],
        "fallback": ["deepseek-chat"],
        "signup": "https://platform.deepseek.com/api_keys",
        "free": "платный, но копейки: ~$0.3 за 1000 вопросов (оплата зарубежной картой)",
        "access": "nophone",
    },
    # --- v38: российские ИИ (вход по номеру РФ) ------------------------------------------------------------
    "gigachat": {
        "title": "GigaChat (Сбер)",
        "base": "https://api.giga.chat/v1",
        "env": "GIGACHAT_AUTH_KEY",
        "prefix": None,  # «ключ авторизации» — base64, без приставки
        "prefer": ["gigachat-2-max", "gigachat-2-pro", "gigachat-2", "gigachat-max", "gigachat-pro", "gigachat"],
        "fallback": ["GigaChat-2-Max", "GigaChat-2-Pro", "GigaChat-2"],
        "signup": "https://developers.sber.ru/studio",
        "free": "бесплатно для физлиц (Freemium), вход по Сбер ID / номеру РФ",
        "access": "ru",
    },
    "yandex": {
        "title": "YandexGPT",
        "base": "https://llm.api.cloud.yandex.net/v1",
        "env": "YANDEX_API_KEY",
        "prefix": None,
        "prefer": [],
        "fallback": [],  # модель зависит от каталога (YANDEX_FOLDER_ID) — задаётся при запуске
        "signup": "https://aistudio.yandex.ru",
        "free": "платный (карта РФ, рубли), нужен ещё YANDEX_FOLDER_ID",
        "access": "ru",
    },
    # --- v38: работают вообще без ключа и без регистрации (включены сами; выключить: AI_KEYLESS=0) ----------
    "ovh": {
        "title": "OVHcloud",
        "base": "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1",
        "env": "OVH_AI_ENDPOINTS_ACCESS_TOKEN",
        "prefix": None,
        "prefer": ["gpt-oss-120b", "qwen3.5-397b", "llama-3_3-70b", "qwen"],
        "fallback": ["gpt-oss-120b", "Meta-Llama-3_3-70B-Instruct"],
        "signup": "https://endpoints.ai.cloud.ovh.net",
        "free": "без ключа: GPT-OSS 120B, Qwen, Llama; ~2 запроса в минуту",
        "access": "keyless",
    },
    "llm7": {
        "title": "LLM7",
        "base": "https://api.llm7.io/v1",
        "env": "LLM7_API_KEY",
        "prefix": None,
        "prefer": ["gpt-oss", "deepseek", "qwen", "mistral", "llama"],
        "fallback": ["gpt-oss:20b"],
        "signup": "https://token.llm7.io",
        "free": "без ключа; бесплатный токен по почте — больше лимит",
        "access": "keyless",
    },
}
ACCESS = {  # как получить: показываем в /council keys
    "keyless": "🟢 без ключа и регистрации — уже работает",
    "nophone": "✉️ без телефона (почта, Google или GitHub)",
    "ru": "🇷🇺 по номеру РФ",
    "google": "🌐 аккаунт Google; из РФ сайт открывается только через VPN",
    "phone": "📵 нужен зарубежный номер телефона (РФ-номер обычно не принимают)",
}
KEYLESS_OFF = ("0", "false", "no", "off", "нет")
for _name, _url, _free, _access in (
        ("groq", "https://console.groq.com/keys", "бесплатно, без карты", "nophone"),
        ("openrouter", "https://openrouter.ai/keys", "бесплатные модели :free", "nophone"),
        ("gemini", "https://aistudio.google.com/apikey", "бесплатно, ещё смотрит видео", "google")):
    PROVIDERS[_name].setdefault("signup", _url)
    PROVIDERS[_name].setdefault("free", _free)
    PROVIDERS[_name].setdefault("access", _access)
EXTRA_ENV = {  # синонимы имён переменных
    "github": ("GITHUB_MODELS_TOKEN", "GITHUB_MODELS_KEY"),
    "gigachat": ("GIGACHAT_AUTH_KEY", "GIGACHAT_CREDENTIALS", "GIGACHAT_API_KEY"),
}
GIGACHAT_AUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
RU_CA_FILE = Path(__file__).parent / "certs" / "russian_trusted_root_ca.pem"  # корневой сертификат Минцифры (Сбер)
_RU_SSL: ssl.SSLContext | None = None


def ru_ssl() -> ssl.SSLContext:
    """Обычные корневые сертификаты + «Russian Trusted Root CA»: им подписаны адреса GigaChat."""
    global _RU_SSL
    if _RU_SSL is None:
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cafile=str(RU_CA_FILE))
        _RU_SSL = ctx
    return _RU_SSL
# Модели, которые не умеют в обычный чат
SKIP_MODELS = ("whisper", "tts", "guard", "embed", "image", "vision", "audio", "live", "orpheus",
               "transcribe", "compound", "safeguard", "lite", "nano", "8b", "1b", "3b")

MAX_CONTEXT_CHARS = 9_000  # ≈3 тыс. токенов: вместе с ответом влезает в бесплатный лимит Groq 8K токенов/мин
HISTORY_TURNS = 3  # сколько последних вопросов-ответов помнить в чате
REQUEST_TIMEOUT = 90
COUNCIL_MAX = 5  # сколько ИИ максимум спрашивает консилиум
COUNCIL_TIMEOUT = 60
# Запас выходных токенов на «размышления» Gemini 2.5+/3: они считаются в maxOutputTokens. Без запаса модель
# обрывает мысль → пустой или обрезанный ответ → повторный запрос (вдвое дольше и минус из дневного лимита).
# Лишние токены не тратятся: платим (и ждём) только за то, что модель реально сгенерировала.
THINK_ROOM = 6000

# --- Перегрузка серверов (503 «high demand», 500/502/504) ------------------------------------------
# Это временно и не наша ошибка: повторяем через несколько секунд, потом берём другую модель Gemini,
# а занятую не трогаем 10 минут. В чат — одна понятная строка, а не сырой JSON сервера.
BUSY_STATUSES = (500, 502, 503, 504)
BUSY_WORDS = ("overloaded", "high demand", "unavailable", "try again later", "resource has been exhausted")
BUSY_RETRY_SEC = 3.0
BUSY_MODEL_SEC = 10 * 60
BUSY_TEXT = "серверы Google сейчас перегружены (ошибка {status}, это временно, ключ в порядке)"
BUSY_ASK = " — спроси ещё раз через минуту"


def is_busy(status: int, text: str = "") -> bool:
    if status in BUSY_STATUSES:
        return True
    return status == 429 and any(w in (text or "").lower() for w in ("overloaded", "high demand"))


def short_api_error(text: str, limit: int = 140) -> str:
    """Из ответа сервера — короткая строка: message из JSON {"error": {...}}, без скобок и переносов."""
    msg = ""
    try:
        data = json.loads(text)
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            err = data[0].get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("status") or "")
        elif isinstance(err, str):
            msg = err
        elif isinstance(data, dict):
            msg = str(data.get("message") or "")
    except (ValueError, TypeError, AttributeError):
        pass
    msg = " ".join((msg or text or "").split())
    return msg[:limit].rstrip() + ("…" if len(msg) > limit else "")


def tidy_error(text: str | None) -> str | None:
    """Для показа: старые ошибки с сырым JSON сервера → одна строка («кто: Gemini: ошибка 503: перегружен…»)."""
    if not text or "{" not in text:
        return text
    head = " ".join(text.split("{", 1)[0].split()).rstrip(" :")
    m = re.search(r'"message"\s*:\s*"([^"]{3,})', text)
    code = re.search(r"\b(5\d\d|4\d\d)\b", text)
    if code and is_busy(int(code.group(1)), text):
        return f"{head.split(': ошибка')[0]}: " + BUSY_TEXT.format(status=code.group(1))
    return f"{head}: {' '.join(m.group(1).split())[:140]}" if m else head


class BusyModels:
    """Какие модели сейчас перегружены: {модель: до какого времени не трогать}."""

    def __init__(self) -> None:
        self.until: dict[str, float] = {}

    def mark(self, model: str, now: float | None = None) -> None:
        self.until[model] = (time.time() if now is None else now) + BUSY_MODEL_SEC

    def now(self, now: float | None = None) -> set[str]:
        t = time.time() if now is None else now
        self.until = {m: u for m, u in self.until.items() if u > t}
        return set(self.until)


class AIError(Exception):
    """Понятная пользователю ошибка ИИ."""


# ---------------------------------------------------------------------------
# Настройки и выбор провайдера
# ---------------------------------------------------------------------------


GEMINI_ENV_ALIASES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY", "GOOGLE_AI_KEY")


def clean_key(raw: str | None) -> str:
    """Ключ из переменной окружения без случайного мусора: пробелов, кавычек, «Bearer », «ИМЯ=» и переносов строк
    (так часто бывает, когда копируешь с телефона)."""
    key = (raw or "").strip().strip("\"'«»“”` ").strip()
    if "=" in key and re.match(r"^[A-Z_]{3,40}=", key):  # вставили «GEMINI_API_KEY=AQ...» целиком
        key = key.split("=", 1)[1].strip().strip("\"'`")
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return re.sub(r"\s+", "", key)


def is_gemini_key(key: str) -> bool:
    return key.startswith(PROVIDERS["gemini"]["prefix"])


def detect_provider(key: str) -> str | None:
    for name, p in PROVIDERS.items():
        if p.get("prefix") and key.startswith(p["prefix"]):
            return name
    return None


def provider_env_names(name: str) -> tuple[str, ...]:
    if name == "gemini":
        return GEMINI_ENV_ALIASES
    return EXTRA_ENV.get(name) or (PROVIDERS[name]["env"],)


def ai_keys_present(env=None) -> bool:
    env = os.environ if env is None else env
    return bool((env.get("AI_API_KEY") or "").strip()) or any(
        (env.get(n) or "").strip() for name in PROVIDERS for n in provider_env_names(name))


@dataclass
class Endpoint:
    provider: str  # groq / openrouter / gemini / custom
    base: str
    key: str
    model: str | None = None  # None = выбрать автоматически
    disabled_until: float = 0.0  # после 429/ошибки не дёргаем какое-то время
    dead_models: set = field(default_factory=set)
    busy: BusyModels = field(default_factory=BusyModels)  # перегруженные модели (503) — временно обходим
    stopgap: bool = False  # модель выбрана на время перегрузки: потом вернёмся к лучшей
    pinned: str | None = None  # модель из AI_MODEL: после перегрузки возвращаемся к ней
    headers: dict = field(default_factory=dict)  # доп. заголовки (Яндекс: каталог)
    fallback: list = field(default_factory=list)  # свои запасные модели (Яндекс: с каталогом в имени)
    auth_url: str = ""  # GigaChat: ключ меняется на токен на 30 минут
    scope: str = ""
    token: str = ""
    token_exp: float = 0.0

    def __post_init__(self) -> None:
        self.pinned = self.pinned or self.model

    @property
    def title(self) -> str:
        return PROVIDERS.get(self.provider, {}).get("title", self.provider)

    @property
    def keyless(self) -> bool:
        return not self.key and self.provider != "gigachat"


def endpoints_from_env(env: dict | None = None) -> list[Endpoint]:
    env = os.environ if env is None else env
    model = (env.get("AI_MODEL") or "").strip() or None
    out: list[Endpoint] = []
    seen: set[str] = set()

    main = clean_key(env.get("AI_API_KEY"))
    if main:
        base = (env.get("AI_BASE_URL") or "").strip().rstrip("/")
        provider = (env.get("AI_PROVIDER") or "").strip().lower() or detect_provider(main)
        if base:
            out.append(Endpoint(provider or "custom", base, main, model))
        elif provider in ("yandex", "gigachat"):  # особая настройка — как у отдельной переменной
            ep = (_yandex_endpoint(env, main) if provider == "yandex" else
                  Endpoint("gigachat", PROVIDERS["gigachat"]["base"], main, model))
            if ep:
                out.append(ep)
        elif provider in PROVIDERS:
            out.append(Endpoint(provider, PROVIDERS[provider]["base"], main, model))
        else:
            log.warning("AI_API_KEY задан, но провайдер не распознан. Укажи AI_PROVIDER или AI_BASE_URL")
        seen.add(main)

    for name, p in PROVIDERS.items():
        names = provider_env_names(name)
        key = next((k for k in (clean_key(env.get(n)) for n in names) if k), "")
        if key and key not in seen:
            seen.add(key)
            if name == "yandex":
                ep = _yandex_endpoint(env, key)
                if ep:
                    out.append(ep)
                continue
            # своя модель применяется только к основному провайдеру
            ep = Endpoint(name, p["base"], key, model if not out else None)
            if name == "gigachat":
                ep.base = (env.get("GIGACHAT_BASE_URL") or p["base"]).strip().rstrip("/")
                ep.auth_url = (env.get("GIGACHAT_AUTH_URL") or GIGACHAT_AUTH_URL).strip()
                ep.scope = (env.get("GIGACHAT_SCOPE") or "GIGACHAT_API_PERS").strip()
            out.append(ep)
    return out


def _yandex_endpoint(env, key: str) -> Endpoint | None:
    folder = (env.get("YANDEX_FOLDER_ID") or "").strip()
    if not folder:
        log.warning("YANDEX_API_KEY задан, но нет YANDEX_FOLDER_ID (ID каталога в Yandex Cloud) — YandexGPT выключен")
        return None
    names = [(env.get("YANDEX_MODEL") or "").strip() or "yandexgpt/latest", "yandexgpt/latest", "yandexgpt-lite/latest"]
    models = list(dict.fromkeys(f"gpt://{folder}/{m}" for m in names))
    return Endpoint("yandex", PROVIDERS["yandex"]["base"], key, models[0], fallback=models[1:],
                    headers={"OpenAI-Project": folder, "x-folder-id": folder})


def keyless_endpoints(env=None, have: list[Endpoint] | None = None) -> list[Endpoint]:
    """ИИ, которым не нужен ни ключ, ни регистрация. В конце списка: запасные и голоса в консилиуме."""
    env = os.environ if env is None else env
    if str(env.get("AI_KEYLESS", "1")).strip().lower() in KEYLESS_OFF:
        return []
    taken = {e.provider for e in (have or [])}
    return [Endpoint(name, p["base"], "") for name, p in PROVIDERS.items()
            if p.get("access") == "keyless" and name not in taken]


def pick_model(provider: str, ids: list[str], dead: set | None = None) -> str | None:
    """Выбирает лучшую доступную модель по списку предпочтений провайдера."""
    dead = dead or set()
    p = PROVIDERS.get(provider, {})
    clean = [i for i in ids if i not in dead and not any(s in i.lower() for s in SKIP_MODELS)]
    if p.get("only_free"):
        clean = [i for i in clean if i.endswith(":free") or i == "openrouter/free"]
    for pattern in p.get("prefer", []):
        matches = [i for i in clean if pattern in i.lower()]
        if matches:
            # у Gemini номер версии в названии: берём самую новую стабильную
            stable = [m for m in matches if "preview" not in m and "exp" not in m] or matches
            return sorted(stable, reverse=True)[0]
    return clean[0] if clean else None


# ---------------------------------------------------------------------------
# HTTP-клиент
# ---------------------------------------------------------------------------


class LLMClient:
    """Минимальный клиент OpenAI-совместимого chat/completions с переключением провайдеров."""

    def __init__(self, endpoints: list[Endpoint], daily_limit: int = 300):
        self.endpoints = endpoints
        self.daily_limit = daily_limit
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()  # по одному запросу за раз: бережём лимит токенов в минуту
        self._bg_lock = asyncio.Lock()  # фоновые разборы — своя очередь: вопрос владельца не ждёт их
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.requests_today = 0
        self.tokens_today = 0
        self.last_model: str | None = None
        self.last_provider: str | None = None
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.endpoints)

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _count_day(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self.day:
            self.day, self.requests_today, self.tokens_today = today, 0, 0
        if self.requests_today >= self.daily_limit:
            raise AIError(
                f"Дневной лимит запросов к ИИ исчерпан ({self.daily_limit}). "
                "Он сбросится в 03:00 по Москве. Можно поднять AI_DAILY_LIMIT."
            )

    def _headers(self, ep: Endpoint) -> dict:
        if ep.provider == "gemini" and "/openai" not in ep.base:
            return {"x-goog-api-key": ep.key, "Content-Type": "application/json"}
        h = {"Content-Type": "application/json", **ep.headers}
        secret = ep.token if ep.provider == "gigachat" else ep.key
        if secret:  # ИИ без ключа (OVHcloud, LLM7): заголовок не шлём совсем
            h["Authorization"] = f"Bearer {secret}"
        if ep.provider == "openrouter":
            h["HTTP-Referer"] = "https://github.com/Ski1los/ai-casher-bruh"
            h["X-Title"] = "ai-casher-bot"
        return h

    @staticmethod
    def _tls(ep: Endpoint) -> dict:
        return {"ssl": ru_ssl()} if ep.provider == "gigachat" else {}

    async def _auth(self, ep: Endpoint, force: bool = False) -> None:
        """GigaChat: «ключ авторизации» → токен доступа на 30 минут (обновляем заранее, за минуту до конца)."""
        if ep.provider != "gigachat" or (ep.token and not force and ep.token_exp - time.time() > 60):
            return
        http = await self._http()
        headers = {"Authorization": f"Basic {ep.key}", "RqUID": str(uuid.uuid4()), "Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded"}
        async with http.post(ep.auth_url or GIGACHAT_AUTH_URL, data={"scope": ep.scope or "GIGACHAT_API_PERS"},
                             headers=headers, **self._tls(ep)) as r:
            text = await r.text()
            if r.status in (400, 401, 403):
                raise AIError(f"{ep.title}: ключ авторизации не подходит (ошибка {r.status}). Скопируй заново "
                              "«Authorization Key» в developers.sber.ru → GigaChat API → Настройки API.")
            if r.status != 200:
                raise AIError(f"{ep.title}: не выдал токен (ошибка {r.status}): {short_api_error(text)}")
            data = json.loads(text)
        ep.token = str(data.get("access_token") or "")
        exp = float(data.get("expires_at") or 0)
        ep.token_exp = exp / 1000 if exp > 1e11 else (exp or time.time() + 1800)  # Сбер присылает миллисекунды
        if not ep.token:
            raise AIError(f"{ep.title}: пустой токен")

    async def _model_for(self, ep: Endpoint) -> str:
        busy = ep.busy.now()
        skip = ep.dead_models | busy  # убранные и временно перегруженные (503) модели
        if ep.pinned and ep.pinned not in skip:
            ep.model, ep.stopgap = ep.pinned, False
            return ep.model
        if ep.model and ep.model not in skip and (busy or not ep.stopgap):
            return ep.model
        ids: list[str] = []
        try:
            http = await self._http()
            url = f"{ep.base}/models" + ("?pageSize=200" if self._native(ep) else "")
            await self._auth(ep)
            async with http.get(url, headers=self._headers(ep), **self._tls(ep)) as r:
                if r.status in (401, 403) and not ep.keyless:  # без ключа список моделей могут не отдать — возьмём запасную
                    raise AIError(f"{ep.title}: ключ не подходит (ошибка {r.status}). Проверь AI_API_KEY.")
                if r.status == 200:
                    data = await r.json(content_type=None)
                    if "models" in data:  # родной API Gemini: {"models": [{"name": "models/…", ...}]}
                        ids = [str(m.get("name", "")).removeprefix("models/") for m in data["models"]
                               if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"])]
                    else:
                        ids = [str(m.get("id", "")).removeprefix("models/") for m in data.get("data", [])]
        except AIError:
            raise
        except Exception as e:  # сеть: попробуем модель по умолчанию
            log.warning("ИИ: не смог получить список моделей %s: %s", ep.title, e)
        model = pick_model(ep.provider, ids, skip) if ids else None
        if model is None:
            fallback = [m for m in (ep.fallback or PROVIDERS.get(ep.provider, {}).get("fallback", [])) if m not in skip]
            if not fallback and ep.busy.now():
                ep.disabled_until = time.time() + 60  # все модели заняты: пусть ответит другой ИИ, если он есть
                raise AIError(f"{ep.title}: " + BUSY_TEXT.format(status=503) + BUSY_ASK)
            if not fallback:
                raise AIError(f"{ep.title}: не нашёл подходящей модели. Задай AI_MODEL вручную.")
            model = fallback[0]
        ep.model = model
        ep.stopgap = bool(busy)
        log.info("ИИ: %s, модель %s", ep.title, model)
        return model

    def _payload(self, ep: Endpoint, model: str, messages: list[dict], max_tokens: int, extras: bool) -> dict:
        body = {"model": model, "messages": messages, "temperature": 0.3, "max_tokens": max_tokens}
        if extras and ep.provider in ("groq", "cerebras") and "gpt-oss" in model:
            body["reasoning_effort"] = "low"  # меньше «размышлений» = меньше токенов из лимита
            if ep.provider == "groq":
                body["include_reasoning"] = False
        return body

    @staticmethod
    def _native(ep: Endpoint) -> bool:
        return ep.provider == "gemini" and "/openai" not in ep.base

    @staticmethod
    def _gemini_body(messages: list[dict], max_tokens: int) -> dict:
        """Сообщения в стиле OpenAI → запрос родного API Gemini (system → systemInstruction, assistant → model)."""
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = []
        for m in messages:
            if m["role"] == "system":
                continue
            role = "model" if m["role"] == "assistant" else "user"
            if contents and contents[-1]["role"] == role:  # Gemini не любит два сообщения подряд от одного
                contents[-1]["parts"][0]["text"] += "\n\n" + m["content"]
            else:
                contents.append({"role": role, "parts": [{"text": m["content"]}]})
        body = {"contents": contents or [{"role": "user", "parts": [{"text": "…"}]}],
                # «размышления» новых моделей тоже тратят выходные токены — даём запас, чтобы мысль не обрывалась
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens + THINK_ROOM}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    async def _busy(self, ep: Endpoint, model: str, status: int, tries: int) -> None:
        """Сервер перегружен: первый раз — ждём пару секунд и повторяем, дальше — другая модель на 10 минут."""
        log.info("ИИ: %s, модель %s перегружена (%s)", ep.title, model, status)
        if tries == 1:
            await asyncio.sleep(BUSY_RETRY_SEC)
            return
        ep.busy.mark(model)
        ep.model = None

    async def _call_gemini(self, ep: Endpoint, messages: list[dict], max_tokens: int) -> str:
        http = await self._http()
        busy = 0
        for attempt in range(6):
            model = await self._model_for(ep)
            body = self._gemini_body(messages, max_tokens)
            async with http.post(f"{ep.base}/models/{model}:generateContent", json=body, headers=self._headers(ep)) as r:
                text = await r.text()
                if r.status == 200:
                    data = json.loads(text)
                    self.tokens_today += int((data.get("usageMetadata") or {}).get("totalTokenCount") or 0)
                    cand = (data.get("candidates") or [{}])[0]
                    parts = (cand.get("content") or {}).get("parts") or []
                    content = clean_answer("".join(p.get("text", "") for p in parts if not p.get("thought")))
                    if not content:
                        if cand.get("finishReason") == "MAX_TOKENS" and max_tokens < 8000:
                            max_tokens *= 2
                            continue
                        block = (data.get("promptFeedback") or {}).get("blockReason") or cand.get("finishReason")
                        raise AIError(f"Gemini вернул пустой ответ{f' ({block})' if block else ''}")
                    self.last_model, self.last_provider = model, ep.title
                    return content
                low = text.lower()
                if r.status in (401, 403) or (r.status == 400 and ("api key" in low or "api_key" in low)):
                    raise AIError(f"{ep.title}: ключ не подходит (ошибка {r.status}). Проверь ключ в Render: "
                                  "скопируй его заново в AI Studio кнопкой «Copy key».")
                if r.status == 429:
                    wait = _retry_after(r.headers, text)
                    if wait <= 20 and attempt == 0:
                        await asyncio.sleep(wait + 0.5)
                        continue
                    ep.disabled_until = time.time() + max(wait, 30)
                    raise AIError(f"{ep.title}: лимит бесплатного тарифа, подожди ~{max(wait, 30):.0f} с")
                if is_busy(r.status, text):
                    busy += 1
                    await self._busy(ep, model, r.status, busy)
                    continue
                if r.status in (404, 400) and "model" in low:
                    ep.dead_models.add(model)
                    ep.model = None
                    continue
                raise AIError(f"{ep.title}: ошибка {r.status}: {short_api_error(text)}")
        if busy:
            ep.disabled_until = time.time() + 60
            raise AIError(f"{ep.title}: " + BUSY_TEXT.format(status=503) + BUSY_ASK)
        raise AIError(f"{ep.title}: не удалось получить ответ")

    async def _call(self, ep: Endpoint, messages: list[dict], max_tokens: int) -> str:
        if self._native(ep):
            return await self._call_gemini(ep, messages, max_tokens)
        http = await self._http()
        extras = True
        busy = 0
        refreshed = False
        for attempt in range(6):
            model = await self._model_for(ep)
            body = self._payload(ep, model, messages, max_tokens, extras)
            await self._auth(ep)
            async with http.post(f"{ep.base}/chat/completions", json=body, headers=self._headers(ep),
                                 **self._tls(ep)) as r:
                text = await r.text()
                if r.status == 200:
                    data = json.loads(text)
                    usage = data.get("usage") or {}
                    self.tokens_today += int(usage.get("total_tokens") or 0)
                    choice = (data.get("choices") or [{}])[0]
                    content = clean_answer((choice.get("message") or {}).get("content") or "")
                    if not content:
                        if choice.get("finish_reason") == "length" and max_tokens < 4000:
                            max_tokens *= 2  # всё ушло на «размышления», даём больше места
                            continue
                        raise AIError("модель вернула пустой ответ")
                    self.last_model, self.last_provider = model, ep.title
                    return content
                low = text.lower()
                if r.status == 401 and ep.provider == "gigachat" and not refreshed:
                    refreshed = True  # токен истёк раньше срока — берём новый
                    await self._auth(ep, force=True)
                    continue
                if r.status in (401, 403):
                    if ep.keyless:
                        ep.disabled_until = time.time() + 3600
                        raise AIError(f"{ep.title}: бесплатный доступ без ключа сейчас закрыт (ошибка {r.status})")
                    raise AIError(f"{ep.title}: ключ не подходит (ошибка {r.status}). Проверь ключ.")
                if r.status == 402:  # на эту модель нет оплаченных/бесплатных токенов — пробуем попроще
                    ep.dead_models.add(model)
                    ep.model = None
                    if ep.pinned == model:
                        ep.pinned = None
                    if len(ep.dead_models) >= 4:
                        raise AIError(f"{ep.title}: закончились бесплатные токены (ошибка 402)")
                    continue
                if is_busy(r.status, text):
                    busy += 1
                    await self._busy(ep, model, r.status, busy)
                    continue
                if r.status == 429:
                    wait = _retry_after(r.headers, text)
                    if wait <= 20 and attempt == 0 and not ep.keyless:
                        await asyncio.sleep(wait + 0.5)
                        continue
                    # без ключа лимит общий на IP сервера — не ждём, сразу отдаём вопрос другому ИИ
                    ep.disabled_until = time.time() + max(wait, 60 if ep.keyless else 30)
                    raise AIError(f"{ep.title}: лимит бесплатного тарифа, подожди ~{max(wait, 30):.0f} с")
                if r.status == 400 and extras and ("reasoning" in low or "unsupported" in low or "property" in low):
                    extras = False  # провайдер не знает доп. параметры, пробуем без них
                    continue
                if r.status in (404, 400) and "model" in low:
                    ep.dead_models.add(model)  # модель убрали или она недоступна: выбираем другую
                    ep.model = None
                    continue
                if r.status == 413 or ("context" in low and "length" in low):
                    raise AIError("слишком длинный запрос для модели")
                raise AIError(f"{ep.title}: ошибка {r.status}: {short_api_error(text)}")
        if busy:
            ep.disabled_until = time.time() + 60
            raise AIError(f"{ep.title}: " + BUSY_TEXT.format(status=503) + BUSY_ASK)
        raise AIError(f"{ep.title}: не удалось получить ответ")

    async def chat(self, messages: list[dict], max_tokens: int = 1200, background: bool = False) -> str:
        """background=True — фоновая задача (разбор роликов): идёт своей очередью, чтобы вопрос в чате
        не ждал за ней до полутора минут. Одновременно максимум 2 запроса: лимиты бесплатных тарифов выдерживают."""
        if not self.endpoints:
            raise AIError("ИИ не подключён: в Render нет ключа (GEMINI_API_KEY или AI_API_KEY)")
        async with self._bg_lock if background else self._lock:
            self._count_day()
            self.requests_today += 1
            errors: list[str] = []
            now = time.time()
            candidates = [e for e in self.endpoints if e.disabled_until <= now] or self.endpoints
            for ep in candidates:
                try:
                    answer = await self._call(ep, messages, max_tokens)
                    self.last_error = None
                    return answer
                except AIError as e:
                    errors.append(str(e))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                    errors.append(f"{ep.title}: нет связи ({type(e).__name__})")
                    ep.disabled_until = time.time() + 60
                log.warning("ИИ: %s", errors[-1])
            self.last_error = "; ".join(errors)
            raise AIError(self.last_error)


    async def chat_each(self, messages: list[dict], max_tokens: int = 700,
                        limit: int = COUNCIL_MAX) -> list[tuple[str, str | None, str | None, str | None]]:
        """🧠 Консилиум: один и тот же вопрос — каждому ИИ отдельно и одновременно.
        Возвращает [(провайдер, модель, ответ, ошибка)]. Каждый ответ считается в дневной лимит."""
        if not self.endpoints:
            raise AIError("ИИ не подключён: в Render нет ключа (GEMINI_API_KEY или AI_API_KEY)")
        now = time.time()
        ready = [e for e in self.endpoints if e.disabled_until <= now][:limit]
        if not ready:
            raise AIError("все ИИ на паузе из-за лимитов, попробуй через минуту")
        self._count_day()
        ready = ready[:max(1, self.daily_limit - self.requests_today)]
        self.requests_today += len(ready)

        async def one(ep: Endpoint):
            try:
                text = await asyncio.wait_for(self._call(ep, messages, max_tokens), COUNCIL_TIMEOUT)
                return ep.title, ep.model, text, None
            except AIError as e:
                return ep.title, ep.model, None, str(e)
            except asyncio.TimeoutError:
                return ep.title, ep.model, None, f"{ep.title}: не успел за {COUNCIL_TIMEOUT} с"
            except (aiohttp.ClientError, ValueError) as e:
                ep.disabled_until = time.time() + 60
                return ep.title, ep.model, None, f"{ep.title}: нет связи ({type(e).__name__})"

        return list(await asyncio.gather(*(one(ep) for ep in ready)))


def _retry_after(headers, body: str) -> float:
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = headers.get(name)
        if raw:
            m = re.match(r"^\s*(?:(\d+)m)?\s*([\d.]+)?s?\s*$", raw)
            if m and (m.group(1) or m.group(2)):
                return int(m.group(1) or 0) * 60 + float(m.group(2) or 0)
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", body)
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    return 60.0


def clean_answer(text: str) -> str:
    """Убирает «размышления» моделей вида <think>…</think>."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    return text.strip()


# ---------------------------------------------------------------------------
# Форматирование ответа для Telegram
# ---------------------------------------------------------------------------


def to_telegram_html(text: str) -> str:
    """Простой Markdown модели → безопасный HTML для Telegram."""
    text = html.escape(text, quote=False)
    text = re.sub(r"```(?:\w+)?\n?(.*?)```", lambda m: f"<pre>{m.group(1).strip()}</pre>", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])__(.+?)__(?![\w*])", r"<b>\1</b>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.M)
    text = re.sub(r"^\s*\|?\s*:?-{3,}.*$\n?", "", text, flags=re.M)  # разделители таблиц
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Предложения настроек
# ---------------------------------------------------------------------------


@dataclass
class Proposal:
    key: str
    value: float
    reason: str = ""


SET_LINE = re.compile(
    r"^[^\w\n]*SET[^\w\n]+([a-z_]+)[*_`\s]*[=:][*_`\s]*(-?\d[\d.,]*)[*_`\s]*(?:[-—–:|#][*_`\s]*(.*))?$", re.M | re.I
)


def extract_proposals(text: str) -> tuple[str, list[Proposal]]:
    """Достаёт из ответа строки «SET ключ=значение — причина» и проверяет их."""
    proposals: list[Proposal] = []
    for m in SET_LINE.finditer(text):
        key = m.group(1).lower()
        try:
            value = float(m.group(2).replace(",", ".").rstrip("."))
        except ValueError:
            continue
        if key in RISK_KEYS:  # лимиты риска меняет только человек
            continue
        if validate_setting(key, value) is None and all(p.key != key for p in proposals):
            proposals.append(Proposal(key, value, (m.group(3) or "").strip(" *_`")))
    cleaned = SET_LINE.sub("", text).strip()
    return cleaned, proposals[:4]


@dataclass
class OrderIntent:
    """Ордер, который ИИ предлагает по просьбе пользователя. Исполняется только после кнопки."""

    side: str  # buy / sell
    exchange: str
    coin: str
    amount: str  # для buy — USDT; для sell — монеты, all или 50%
    price: float | None = None  # для лимитного ордера
    reason: str = ""


ORDER_LINE = re.compile(
    r"^[^\w\n]*ORDER[^\w\n]+(buy|sell)\s+([a-z0-9._-]+)\s+([a-z0-9]{1,15})\s+(\d[\d.,]*%?|all)"
    r"(?:\s*@\s*(\d[\d.,]*))?[*_`\s]*(?:[-—–:|#][*_`\s]*(.*))?$",
    re.M | re.I,
)


def extract_orders(text: str) -> tuple[str, list[OrderIntent]]:
    orders: list[OrderIntent] = []
    for m in ORDER_LINE.finditer(text):
        price = None
        if m.group(5):
            try:
                price = float(m.group(5).replace(",", ".").rstrip("."))
            except ValueError:
                continue
        orders.append(OrderIntent(
            m.group(1).lower(), m.group(2).lower(), m.group(3).upper(), m.group(4).lower().rstrip("."),
            price, (m.group(6) or "").strip(" *_`"),
        ))
    return ORDER_LINE.sub("", text).strip(), orders[:3]


_ENABLE = r"(включ|разреш|активир|постав\w* галоч|отмет)"
_WITHDRAW = r"(вывод|withdraw)"
_BAD_ADVICE = re.compile(rf"{_ENABLE}\w*[^.!?\n]{{0,40}}{_WITHDRAW}|{_WITHDRAW}[^.!?\n]{{0,30}}{_ENABLE}", re.I)
_NEGATION = re.compile(r"\bне\s+(нужн|надо|включ|разреш|требу|добавл|став|отмеч)|без\s+(права\s+)?вывод|никогда", re.I)
WITHDRAW_NOTE = "🔒 Право «Вывод» у ключа боту не нужно — ни для теста, ни для реальной торговли: хватает «Чтение + Спот торговля»."


def guard_withdraw(text: str) -> str:
    """Страховка от ошибки модели: выкидывает строки с советом включить вывод у API-ключа.

    Модели иногда понимают «вывод выключен» как проблему и советуют его включить. Это опасно
    (украденный ключ с выводом = украденные деньги), поэтому такие строки не доходят до пользователя.
    """
    out, dropped = [], False
    for line in text.split("\n"):
        if _BAD_ADVICE.search(line) and not _NEGATION.search(line):
            dropped = True
            continue
        out.append(line)
    if dropped:
        out += ["", WITHDRAW_NOTE]
    return "\n".join(out)


def validate_setting(key: str, value: float) -> str | None:
    """None, если значение допустимо, иначе текст ошибки."""
    if key not in EDITABLE:
        return "неизвестная настройка"
    lo, hi = BOUNDS.get(key, (0, float("inf")))
    if not lo <= value <= hi:
        return f"допустимо от {lo:g} до {hi:g}"
    return None


# ---------------------------------------------------------------------------
# Сам помощник
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
Ты ИИ-помощник внутри Telegram-бота, который ищет арбитраж на двух криптобиржах, Bybit и BingX \
(других бирж в боте нет), и ведёт демо-счета (виртуальные деньги, реальные стаканы и комиссии). Пользователь новичок, пишет по-русски с телефона.

Как отвечать:
- По-русски, коротко и по делу (обычно до 1500 знаков), простыми словами, без воды.
- Опирайся ТОЛЬКО на цифры из блока ДАННЫЕ. Не выдумывай цены, комиссии и результаты. \
Если данных не хватает, так и скажи и подскажи, какую команду запустить (/scan, /demo_start, /report).
- Честно: арбитраж не даёт гарантированной прибыли, бесплатных денег нет. Не уговаривай вкладывать деньги. \
Прибыль на демо ещё не значит прибыль в реальности (задержки, ручные действия, проскальзывание).
- Комиссии считай явно. Межбиржевой: 2 торговые комиссии + плата за вывод монеты (на маленькой сумме \
она съедает всё) + цена может уйти, пока монета едет. Треугольный: 3 торговые комиссии подряд, \
порог безубыточности ≈ 3 × taker. Объясняй, откуда берётся убыток: комиссия, стакан, мин. ордер, задержка.
- Комиссии в данных: типичные taker из ccxt. Реальные для аккаунта могут быть ниже (VIP-уровень, \
скидка за оплату токеном биржи, maker-ордера). Не называй точные размеры скидок, если не уверен: \
советуй проверить страницу комиссий биржи и, если ставка другая, задать её командой /fee биржа процент.
- Биржи для сканирования меняются только в настройках Render (EXCHANGES).
- API-ключи: боту нужны ТОЛЬКО права «Чтение» и «Спот-торговля». НИКОГДА не советуй включать «Вывод» (Withdraw), \
«Universal Transfer», субаккаунты, P2P или фьючерсы: бот ими не пользуется, а при утечке ключа с выводом деньги украдут. \
Если пользователь думает, что для торговли нужен вывод, — твёрдо скажи, что не нужен. Привязка к IP не нужна \
(у Render адрес меняется). Секрет ключа биржа показывает один раз: потерял — удали ключ и создай новый.

Торговля. В боте есть реальная торговля через API-ключи бирж (блок trading в ДАННЫХ: режим test/live, \
биржи с ключами, последние балансы, лимиты). Если пользователь ЯВНО просит купить или продать \
(например «купи эфира на 5 долларов на байбите»), добавь в конце ответа строку строго в формате:
ORDER buy биржа МОНЕТА сумма_в_USDT — короткая причина
ORDER sell биржа МОНЕТА количество|all|50% — короткая причина
Для лимитного ордера добавь @цена: ORDER buy bybit BTC 5 @60000 — причина
Биржа — id из trading.exchanges_with_keys, монета торгуется против USDT, сумма покупки не больше \
trading.max_trade_usd. Бот сам покажет предпросмотр с реальной ценой и комиссией и кнопку «Подтвердить»: \
без кнопки ничего не исполнится. Если просьба неоднозначна (не ясна биржа или сумма), переспроси, а не угадывай.
Не предлагай ORDER по своей инициативе ради спекуляции и не предсказывай цены: куда пойдёт курс, не знает никто, \
говори это честно. Можно предложить вернуть в USDT монеты, застрявшие после сбоя цикла. \
Если ключей нет, объясни, что нужно подключить их: /keys. Реальный режим (/live) и автоторговлю \
(/autotrade) включает только сам пользователь, ты их не включаешь.
- Оформление: обычный текст для Telegram, можно **жирный** и списки через «- ». Без таблиц и заголовков #.

Кошелёк Phantom и прочее (блок extras). Phantom — личный некастодиальный кошелёк Solana: бот видит его \
баланс ТОЛЬКО по публичному адресу и НЕ торгует им (ORDER для Phantom не бывает). Никогда не проси и не \
принимай сид-фразу или приватный ключ; если пользователь их прислал, скажи срочно создать новый кошелёк и \
перевести туда средства. Чтобы торговать монетами из Phantom, их нужно перевести на биржу (сеть Solana). \
Ценовые алерты: /alert МОНЕТА < цена или /alert МОНЕТА +5%. Полная проверка бирж и ключей: /doctor. \
Если в extras.api_keys ключ скоро истекает или у него ВКЛЮЧЁН вывод (это опасно), скажи об этом. \
Выключенный вывод — правильное состояние, его не упоминай как проблему.

Автоторговля (trading.autotrade_status). Если спрашивают «почему автоторговля не работает / нет сделок», \
отвечай по этим данным: reason и tried (почему пропущены циклы), blocked_now (ключ «только чтение», нет права \
Spot Trade), free и low_money (мало денег на торговом счёте: Bybit через API принимает ордер от $5 на КАЖДЫЙ \
из трёх шагов цикла, need_usd — сколько нужно минимум), funding (деньги лежат на счёте Funding, их надо \
перевести на торговый счёт: Активы → Перевод), \
best (лучший цикл сейчас против порога need). Отсутствие сделок при спокойном рынке — это нормально и \
безопасно, а не поломка. Подробный экран: /why.

Режим $5 = автопилот (trading.scalp_mode_5usd, команды /autopilot и /scalp): для маленьких сумм бот торгует сам. \
Два ордера вместо трёх: покупка монеты по сигналу одной из 3 стратегий — «Откат» (dip: RSI низкий, цена ниже \
средней), «Отскок» (bounce: разворот от нижней полосы Боллинджера), «Тренд» (trend: откат к средней в растущем рынке) — \
на все свободные USDT и сразу лимитная продажа на +0,6…1,3% после комиссий. Для каждой монеты стратегия выбирается \
на первых 2/3 истории (свечи 5 мин за ~3,5 дня) и проверяется на последней трети (поле top: strategy, net_pct, \
test_pct — результат на новых данных, ok — прошла ли). Тормоз рынка: если BTC упал > 2% за час, новых покупок нет. \
use_coins = «Монеты в дело»: лежащие монеты (например, SOL) бот сам продаёт в USDT, чтобы торговать ими; если \
пользователь жалуется, что бот не торгует, а деньги в монете — советуй /autopilot. Через сутки без \
цели продажа переставляется в безубыток. Стоп-лосс -6% работает, только если позиция больше минимального ордера биржи \
($5), поэтому с ровно $5 при падении деньги «ждут» в монете. reason — что режим делает сейчас. Если пользователь \
с $5 спрашивает, как торговать, советуй /autopilot, но честно: прибыль не гарантирована. \
Если trading.second_bot_copy_running = true, работают две копии бота (два сервиса в Render): сделки на паузе, \
скажи удалить лишний сервис.

Профит-центр (блок profit_center, команда /profit) — ГЛАВНЫЙ источник правды о том, что приносит деньги: результаты \
всех способов (автопилот реальный/тест, лаборатория, копитрейдинг, мемкоины демо/реальные, тренировка, арбитраж) \
в одной шкале. mean — средняя сделка в % (для демо уже минус поправка на реальность), lcb — средняя при невезении, \
verdict: proven (доказан плюс), promising (плюс, но может быть везение), flat, losing (доказанный минус), data \
(мало сделок). plan — куда разложить plan_for_usd долларов (четверть Келли, остаток — Earn). Когда спрашивают, \
как заработать больше, отвечай по этому блоку: деньги — только туда, где verdict proven (лучше на настоящих \
сделках), promising — маленькой суммой, losing — не включать; не путай демо и реальные деньги. Страж прибыли \
(guard_on, paused) ставит на паузу то, что доказанно теряет; снять паузу — кнопкой в /profit.

Мемкоины и брокер. /dex (DexScreener: цена, ликвидность, возраст, ссылки на GMGN и Axiom), /rug (RugCheck \
для токенов Solana), /hot (тренды), /radar on (новые токены после фильтров). Покупать мемкоины бот может \
только с отдельного кошелька «Бот» через Jupiter (/meme, ключ лежит в Render), пока его нет — торгует на \
🧪 демо-счёте. С GMGN, Axiom и FOMO бот не торгует: у них нет официального API торговли. \
Предупреждай, что большинство мемкоинов падает почти до нуля.

Учёба у трейдеров (extras.traders_youtube_twitch, /mentors). Бот смотрит ролики YouTube (через Gemini) и читает \
чаты memecoin-стримеров Twitch, а каждый прогноз проверяет ценой. traders — счёт каждого: status star = прогнозы \
окупаются, watch = ещё мало проверок, muted = теряют деньги; avg_pct — средний результат с поправкой на везение; \
ads_videos — сколько роликов с рекламой. Опирайся на мнение трейдера, только если status star, и всегда называй \
его счёт; мнение watch подавай как непроверенное, muted — как ненадёжное. Реклама бирж, пресейлов и «сигнальных \
групп» означает, что автору платят: это не совет. consensus_7d — сколько голосов за рост (long) и падение (short) \
за неделю; это настроение блогеров, а не прогноз. tips_checked — советы, проверенные на истории копий бота: \
verdict «принят» — бот уже так делает, «не подтвердился» — объясни, что на данных бота это было бы хуже. \
Никогда не советуй покупать «потому что сказал блогер». training_demo (/train) — бот сам торгует идеями трейдеров \
на виртуальных деньгах: 5 стратегий параллельно, active — лучшая по демо-сделкам (только её идеи приходят \
сигналом), rules — стоп и тейк, которые бот подобрал сам. Это демо: реальный результат обычно хуже на 1–3%. \
extras.ai_council_last — итог последнего консилиума нескольких ИИ (/council): verdict trade/careful/wait, agree из answered.

Изменение настроек бота. Если по данным видно, что настройку стоит поменять, в самом конце ответа \
добавь отдельные строки строго в формате:
SET ключ=число — короткая причина
Пользователь подтвердит их кнопкой. Ключи и допустимые значения:
{keys}
Предлагай только то, что обосновано данными, не больше 3 строк. Если менять нечего, строк SET не пиши.\
"""


def _keys_help() -> str:
    lines = []
    for key, (_, desc) in EDITABLE.items():
        if key in RISK_KEYS:
            continue
        lo, hi = BOUNDS.get(key, (0, float("inf")))
        lines.append(f"{key} ({desc}, от {lo:g} до {hi:g})")
    return "\n".join(lines)


def compact_json(data: dict, limit: int = MAX_CONTEXT_CHARS) -> str:
    """JSON без пробелов. Если не влезает, срезаем самые длинные списки."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) <= limit:
        return text
    data = json.loads(text)

    def lists(node, path=()):
        if isinstance(node, dict):
            for k, v in node.items():
                yield from lists(v, path + (k,))
        elif isinstance(node, list) and len(node) > 1:
            yield path, node

    for _ in range(50):
        found = sorted(lists(data), key=lambda x: -len(json.dumps(x[1], ensure_ascii=False)))
        if not found:
            break
        _, biggest = found[0]
        del biggest[len(biggest) // 2:]
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(text) <= limit:
            return text
    return text[:limit]


@dataclass
class Answer:
    html: str
    proposals: list[Proposal]
    model: str | None = None
    orders: list[OrderIntent] = field(default_factory=list)


class AIAssistant:
    def __init__(self, client: LLMClient):
        self.client = client
        self.history: dict[int, deque] = {}
        self._review_cache: tuple[float, Answer] | None = None

    @classmethod
    def from_env(cls) -> AIAssistant:
        limit = int(float(os.getenv("AI_DAILY_LIMIT", "300") or 300))
        eps = endpoints_from_env()
        return cls(LLMClient(eps + keyless_endpoints(have=eps), daily_limit=limit))

    @property
    def configured(self) -> bool:
        return self.client.configured

    def reset(self, chat_id: int) -> None:
        self.history.pop(chat_id, None)

    def _system(self, context: dict) -> dict:
        return {
            "role": "system",
            "content": SYSTEM_PROMPT.format(keys=_keys_help()) + "\n\nДАННЫЕ (JSON):\n" + compact_json(context),
        }

    async def _run(self, messages: list[dict], max_tokens: int = 1200, allow_orders: bool = False) -> Answer:
        raw = guard_withdraw(await self.client.chat(messages, max_tokens=max_tokens))
        text, proposals = extract_proposals(raw)
        text, orders = extract_orders(text)  # из разборов ордера выкидываются: только по прямой просьбе
        return Answer(to_telegram_html(text) or "🤷 Пустой ответ", proposals, self.client.last_model,
                      orders if allow_orders else [])

    async def ask(self, chat_id: int, question: str, context: dict) -> Answer:
        question = question.strip()[:2000]
        hist = self.history.setdefault(chat_id, deque(maxlen=HISTORY_TURNS * 2))
        messages = [self._system(context), *hist, {"role": "user", "content": question}]
        answer = await self._run(messages, allow_orders=True)
        hist.append({"role": "user", "content": question})
        hist.append({"role": "assistant", "content": re.sub(r"<[^>]+>", "", answer.html)[:1500]})
        return answer

    async def review(self, context: dict, cache_sec: float = 300) -> Answer:
        """Полный разбор: что происходит, что съедают комиссии, что поменять."""
        if self._review_cache and time.time() - self._review_cache[0] < cache_sec:
            return self._review_cache[1]
        task = (
            "Сделай разбор работы бота по данным. Структура:\n"
            "1) Итог одной фразой: есть ли сейчас реальный шанс заработать и на какой стратегии/сумме "
            "(смотри profit_center: что доказало плюс, что минус).\n"
            "2) Комиссии: на какой бирже дешевле всего, сколько % съедают в каждой стратегии, "
            "какие суммы из-за платы за вывод не имеют смысла.\n"
            "3) Демо-счета: что работает, что нет и почему (причины отказов, ожидание vs реальность).\n"
            "4) Конкретные действия (2–4 пункта). Если стоит поменять настройки, добавь строки SET."
        )
        answer = await self._run([self._system(context), {"role": "user", "content": task}], max_tokens=1600)
        self._review_cache = (time.time(), answer)
        return answer

    async def explain(self, items: dict, context: dict) -> Answer:
        """Разбор конкретных сигналов: стоит ли вообще пытаться и где подвох."""
        task = (
            "Разбери эти сигналы для новичка. Для каждого (максимум 3): реальна ли прибыль после всех "
            "комиссий и платы за вывод, что может пойти не так (стакан, задержка, вывод закрыт, разные "
            "монеты с одним тикером), и вердикт: пробовать на демо / пропустить. Коротко.\n\nСИГНАЛЫ:\n"
            + compact_json(items, 4000)
        )
        return await self._run([self._system(context), {"role": "user", "content": task}], max_tokens=1200)

    def status_text(self) -> str:
        c = self.client
        if not c.configured:
            return "🤖 ИИ не подключён."
        lines = ["🤖 <b>ИИ-помощник</b>"]
        for ep in c.endpoints:
            state = "⏳ пауза из-за лимита" if ep.disabled_until > time.time() else "✅"
            free = " (без ключа)" if ep.keyless else ""
            lines.append(f"  {html.escape(ep.title)}{free}: "
                         f"{html.escape(ep.model or 'модель выберется при первом запросе')} {state}")
        if all(ep.keyless for ep in c.endpoints):
            lines.append("Сейчас только бесплатные ИИ без ключа — медленно и с маленьким лимитом. "
                         "Свой ключ без телефона (Groq) или по номеру РФ (GigaChat): /council keys")
        lines.append(f"Запросов сегодня: {c.requests_today} из {c.daily_limit}, токенов: {c.tokens_today}")
        if c.last_error:
            lines.append(f"Последняя ошибка: {html.escape(c.last_error[:300])}")
        return "\n".join(lines)


NO_AI_TEXT = """\
🤖 <b>ИИ-помощник пока не подключён</b>

<b>Без телефона и без карты</b> (3 минуты с телефона):
1. Открой <a href="https://console.groq.com/keys">console.groq.com/keys</a>, войди через Google или почту.
2. <b>Create API Key</b> → скопируй ключ (начинается с <code>gsk_</code>).
3. Render → сервис <b>ai-casher-bot</b> → <b>Environment</b> → <b>Add Environment Variable</b>:
   Key: <code>AI_API_KEY</code>, Value: ключ → <b>Save Changes</b>. Бот перезапустится сам.

🇷🇺 <b>По номеру РФ:</b> GigaChat от Сбера — бесплатно для физлиц, ключ в <code>GIGACHAT_AUTH_KEY</code>. \
Как получить: /council keys

🟢 <b>Совсем без ключа</b> бот сам берёт бесплатные OVHcloud и LLM7 (если не выключены через \
<code>AI_KEYLESS=0</code>) — но они медленные, с маленьким лимитом.

Ключ никому не показывай и не присылай в чат.

А пока работает разбор без ИИ: /analysis и /fees"""
