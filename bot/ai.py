"""ИИ-помощник: анализирует сканы, демо-счета, комиссии и настройки, отвечает на вопросы.

Работает через любой OpenAI-совместимый API. Бесплатные варианты без банковской карты:
  * Groq (рекомендуется): ключ начинается с gsk_, https://console.groq.com/keys
  * OpenRouter (модели :free): ключ начинается с sk-or-, https://openrouter.ai/keys
  * Google AI Studio (Gemini): ключ начинается с AIza, https://aistudio.google.com/apikey

Достаточно одной переменной AI_API_KEY: провайдер определяется по началу ключа.
Можно задать несколько ключей (GROQ_API_KEY, OPENROUTER_API_KEY, GEMINI_API_KEY):
если один упрётся в лимит или сломается, бот переключится на следующий.

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
import time
from collections import deque
from dataclasses import dataclass, field

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
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env": "GEMINI_API_KEY",
        "prefix": "AIza",
        "prefer": ["flash"],
        "fallback": ["gemini-2.5-flash"],
    },
}
# Модели, которые не умеют в обычный чат
SKIP_MODELS = ("whisper", "tts", "guard", "embed", "image", "vision", "audio", "live", "orpheus",
               "transcribe", "compound", "safeguard", "lite", "nano", "8b", "1b", "3b")

MAX_CONTEXT_CHARS = 9_000  # ≈3 тыс. токенов: вместе с ответом влезает в бесплатный лимит Groq 8K токенов/мин
HISTORY_TURNS = 3  # сколько последних вопросов-ответов помнить в чате
REQUEST_TIMEOUT = 90


class AIError(Exception):
    """Понятная пользователю ошибка ИИ."""


# ---------------------------------------------------------------------------
# Настройки и выбор провайдера
# ---------------------------------------------------------------------------


def detect_provider(key: str) -> str | None:
    for name, p in PROVIDERS.items():
        if key.startswith(p["prefix"]):
            return name
    return None


@dataclass
class Endpoint:
    provider: str  # groq / openrouter / gemini / custom
    base: str
    key: str
    model: str | None = None  # None = выбрать автоматически
    disabled_until: float = 0.0  # после 429/ошибки не дёргаем какое-то время
    dead_models: set = field(default_factory=set)

    @property
    def title(self) -> str:
        return PROVIDERS.get(self.provider, {}).get("title", self.provider)


def endpoints_from_env(env: dict | None = None) -> list[Endpoint]:
    env = os.environ if env is None else env
    model = (env.get("AI_MODEL") or "").strip() or None
    out: list[Endpoint] = []
    seen: set[str] = set()

    main = (env.get("AI_API_KEY") or "").strip()
    if main:
        base = (env.get("AI_BASE_URL") or "").strip().rstrip("/")
        provider = (env.get("AI_PROVIDER") or "").strip().lower() or detect_provider(main)
        if base:
            out.append(Endpoint(provider or "custom", base, main, model))
        elif provider in PROVIDERS:
            out.append(Endpoint(provider, PROVIDERS[provider]["base"], main, model))
        else:
            log.warning("AI_API_KEY задан, но провайдер не распознан. Укажи AI_PROVIDER или AI_BASE_URL")
        seen.add(main)

    for name, p in PROVIDERS.items():
        key = (env.get(p["env"]) or "").strip()
        if key and key not in seen:
            # своя модель применяется только к основному провайдеру
            out.append(Endpoint(name, p["base"], key, model if not out else None))
            seen.add(key)
    return out


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
        h = {"Authorization": f"Bearer {ep.key}", "Content-Type": "application/json"}
        if ep.provider == "openrouter":
            h["HTTP-Referer"] = "https://github.com/Ski1los/ai-casher-bruh"
            h["X-Title"] = "ai-casher-bot"
        return h

    async def _model_for(self, ep: Endpoint) -> str:
        if ep.model and ep.model not in ep.dead_models:
            return ep.model
        ids: list[str] = []
        try:
            http = await self._http()
            async with http.get(f"{ep.base}/models", headers=self._headers(ep)) as r:
                if r.status in (401, 403):
                    raise AIError(f"{ep.title}: ключ не подходит (ошибка {r.status}). Проверь AI_API_KEY.")
                if r.status == 200:
                    data = await r.json(content_type=None)
                    ids = [str(m.get("id", "")).removeprefix("models/") for m in data.get("data", [])]
        except AIError:
            raise
        except Exception as e:  # сеть: попробуем модель по умолчанию
            log.warning("ИИ: не смог получить список моделей %s: %s", ep.title, e)
        model = pick_model(ep.provider, ids, ep.dead_models) if ids else None
        if model is None:
            fallback = [m for m in PROVIDERS.get(ep.provider, {}).get("fallback", []) if m not in ep.dead_models]
            if not fallback:
                raise AIError(f"{ep.title}: не нашёл подходящей модели. Задай AI_MODEL вручную.")
            model = fallback[0]
        ep.model = model
        log.info("ИИ: %s, модель %s", ep.title, model)
        return model

    def _payload(self, ep: Endpoint, model: str, messages: list[dict], max_tokens: int, extras: bool) -> dict:
        body = {"model": model, "messages": messages, "temperature": 0.3, "max_tokens": max_tokens}
        if extras and ep.provider == "groq" and "gpt-oss" in model:
            body["reasoning_effort"] = "low"  # меньше «размышлений» = меньше токенов из лимита
            body["include_reasoning"] = False
        return body

    async def _call(self, ep: Endpoint, messages: list[dict], max_tokens: int) -> str:
        http = await self._http()
        extras = True
        for attempt in range(4):
            model = await self._model_for(ep)
            body = self._payload(ep, model, messages, max_tokens, extras)
            async with http.post(f"{ep.base}/chat/completions", json=body, headers=self._headers(ep)) as r:
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
                if r.status in (401, 403):
                    raise AIError(f"{ep.title}: ключ не подходит (ошибка {r.status}). Проверь ключ.")
                if r.status == 429:
                    wait = _retry_after(r.headers, text)
                    if wait <= 20 and attempt == 0:
                        await asyncio.sleep(wait + 0.5)
                        continue
                    ep.disabled_until = time.time() + max(wait, 30)
                    raise AIError(f"{ep.title}: лимит бесплатного тарифа, подожди ~{max(wait, 30):.0f} с")
                if r.status == 400 and extras and ("reasoning" in low or "unsupported" in low or "property" in low):
                    extras = False  # провайдер не знает доп. параметры, пробуем без них
                    continue
                if r.status in (404, 400) and "model" in low:
                    ep.dead_models.add(model)  # модель убрали или она недоступна: выбираем другую
                    ep.model = None
                    continue
                if r.status == 413 or "context" in low and "length" in low:
                    raise AIError("слишком длинный запрос для модели")
                raise AIError(f"{ep.title}: ошибка {r.status}: {text[:160]}")
        raise AIError(f"{ep.title}: не удалось получить ответ")

    async def chat(self, messages: list[dict], max_tokens: int = 1200) -> str:
        if not self.endpoints:
            raise AIError("ИИ не подключён: нет ключа AI_API_KEY")
        async with self._lock:
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
Ты ИИ-помощник внутри Telegram-бота, который ищет арбитраж на криптобиржах и ведёт демо-счета \
(виртуальные деньги, реальные стаканы и комиссии). Пользователь новичок, пишет по-русски с телефона.

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
Если в extras.api_keys ключ скоро истекает или у него включён вывод, обязательно скажи об этом.

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
        return cls(LLMClient(endpoints_from_env(), daily_limit=limit))

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
        raw = await self.client.chat(messages, max_tokens=max_tokens)
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
            "1) Итог одной фразой: есть ли сейчас реальный шанс заработать и на какой стратегии/сумме.\n"
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
            lines.append(f"  {html.escape(ep.title)}: {html.escape(ep.model or 'модель выберется при первом запросе')} {state}")
        lines.append(f"Запросов сегодня: {c.requests_today} из {c.daily_limit}, токенов: {c.tokens_today}")
        if c.last_error:
            lines.append(f"Последняя ошибка: {html.escape(c.last_error[:300])}")
        return "\n".join(lines)


NO_AI_TEXT = """\
🤖 <b>ИИ-помощник пока не подключён</b>

Это бесплатно и без банковской карты, займёт 3 минуты с телефона:
1. Открой <a href="https://console.groq.com/keys">console.groq.com/keys</a> и войди через Google.
2. Нажми <b>Create API Key</b>, назови как угодно и скопируй ключ (начинается с <code>gsk_</code>).
3. В Render открой сервис <b>ai-casher-bot</b> → <b>Environment</b> → <b>Add Environment Variable</b>:
   Key: <code>AI_API_KEY</code>, Value: твой ключ → <b>Save Changes</b>.
4. Бот перезапустится сам (около минуты), и можно писать вопросы прямо сюда обычным текстом.

Ключ никому не показывай и не присылай в чат.

А пока работает разбор без ИИ: /analysis и /fees"""
