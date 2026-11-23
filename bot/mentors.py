"""🎓 Учёба у трейдеров: YouTube и Twitch.

Что делает:
  * YouTube — новые видео выбранных каналов (RSS, без ключа). Видео «смотрит» Gemini по ссылке (Google сам
    открывает ролик, блокировки облачных серверов не мешают; бесплатно до 8 ч видео в день). Без ключа Gemini —
    разбор по названию и описанию через обычный ИИ бота. Из ролика достаём: суть, прогнозы (монета, куда, срок),
    практические советы, признаки рекламы и хайпа.
  * Twitch — анонимно читаем чат memecoin-стримеров (без ключей). Когда сам стример, модератор или бот канала
    пишет адрес токена Solana — это «колл»: стример его торгует или показывает.
  * Каждый прогноз и колл бот ПРОВЕРЯЕТ ценой: запоминает цену в момент, когда мог бы войти, и смотрит, что
    было через срок прогноза (мемкоины — как копию: стоп/тейк, комиссии). Так у каждого трейдера появляется
    честный счёт: ⭐ — советы окупаются, 🔇 — теряют (или реклама).
  * Советы с цифрами (стоп −20%, ликвидность от $50K, …) бот проверяет на своей истории копий и принимает,
    только если они дали бы больше. Остальные советы и выводы видит ИИ — с пометкой, насколько автор надёжен.

Бот не покупает «потому что сказал блогер»: ⭐-трейдеры дают сигнал (и сделку на 🧪 демо-счёте), а настоящие
деньги — только кнопкой.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import statistics
import time
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable

import aiohttp

from .ai import GEMINI_ENV_ALIASES, clean_key, is_gemini_key
from .copytrade import (EXIT_GAIN, EXIT_MIN, EXIT_STOPS, EXIT_TPS, FILTER_GRID, LEARN_GAIN, LEARN_SUBSET, _passes,
                        add_path, exit_net, simulate_exit, tp_text)
from .dex import fmt_usd
from .solana import valid_address
from .scout import Scout, history_text
from .training import Training
from .livewatch import LiveWatch

log = logging.getLogger(__name__)

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
TWITCH_WS = "wss://irc-ws.chat.twitch.tv:443"

VIDEO_MAX_SEC = 20 * 60        # смотрим первые 20 минут: главное обычно в начале, и экономим лимит
VIDEOS_PER_DAY = 15            # 15 × 20 мин = 5 ч из бесплатных 8 ч YouTube-видео в день
VIDEOS_PER_TICK = 2
VIDEO_MAX_AGE = 48 * 3600      # старые ролики не разбираем: прогнозы в них уже не проверить честно
TICK_SEC = 15 * 60
RSS_EVERY = 45 * 60
CEX_COST = 0.002               # спот: две комиссии ~0,1%
SOL_HORIZON_H = 24
CALL_DEDUP_SEC = 6 * 3600
MIN_CHECKED = 8                # судим о трейдере не раньше чем по 8 проверенным прогнозам
STAR_AVG = 2.0
MUTE_AVG = -3.0
TWITCH_BOTS = {"nightbot", "streamelements", "fossabot", "moobot", "streamlabs", "wizebot", "botrixoficial"}
ADDR_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
TICKER_RE = re.compile(r"^[A-Z0-9]{2,10}$")
STABLE = {"USDT", "USDC", "DAI", "FDUSD", "USD", "BUSD", "TUSD"}

# Проверенные каналы для старта (их можно удалить и добавить свои). id каналов проверены.
DEFAULT_SOURCES = [
    ("youtube", "UCRvqjQPSeaWn-uEx-w0XOIg", "Benjamin Cowen", "данные и циклы BTC/альтов, без рефералок"),
    ("youtube", "UCqK_GSMbpiV8spgD3ZGloSw", "Coin Bureau", "обзоры и фундаментал"),
    ("youtube", "UCN9Nj4tjXbVTLYWN0EKly_Q", "Crypto Banter", "эфиры, много хайпа — проверим"),
    ("youtube", "UCbLhGKVY-bJPcawebgtNfbw", "Altcoin Daily", "альткоины, много прогнозов — проверим"),
    ("twitch", "decu", "decu", "мемкоины Solana вживую"),
    ("twitch", "fashr", "fashr", "мемкоины Solana, Axiom"),
    ("twitch", "dvces", "dvces", "мемкоины и перпы"),
    ("twitch", "heyitsyolo", "heyitsyolo", "мемкоины Solana"),
    ("twitch", "kryptokinglive", "kryptokinglive", "мемкоины Solana, долгие эфиры"),
]

PROMPT = """\
Ты аналитик, который смотрит ролик крипто-трейдера для бота-новичка. Верни СТРОГО JSON без пояснений:
{"summary": "2–4 предложения по-русски: главная мысль ролика",
 "market_view": "bullish|bearish|neutral|mixed",
 "calls": [{"asset": "тикер без $, например SOL", "contract": "адрес токена Solana, если назван или показан на экране, иначе пусто",
            "direction": "long|short|avoid", "horizon_days": 1, "confidence": "low|medium|high",
            "quote": "что именно сказал, до 120 символов, по-русски"}],
 "tips": [{"text": "практический совет по риску, входу или выходу, до 160 символов, по-русски",
           "rule": {"param": "stop_pct|tp_pct|min_liq_usd|min_age_hours|max_chase_pct|none", "value": null}}],
 "sponsored": false, "sponsor_note": "что рекламирует (биржа, пресейл, курс), если есть",
 "hype": 0}
Правила: в calls только ЯВНЫЕ прогнозы самого автора («жду рост SOL», «шорчу», «не покупайте X»), не гипотезы \
и не чужие мнения; не больше 6. horizon_days — срок, о котором говорит автор (1–30; не сказал — 3). \
В tips — не больше 5 конкретных советов; rule заполняй, только если в совете есть число \
(стоп −20% → {"param":"stop_pct","value":20}; тейк +50% → tp_pct 50; ликвидность от $50K → min_liq_usd 50000; \
токену не меньше часа → min_age_hours 1; не входить, если цена уже выросла на 30% → max_chase_pct 30). \
hype — 0…10: насколько ролик — кликбейт и эмоции без аргументов. sponsored = true, если есть реклама, \
реферальные ссылки или промокоды."""


class MentorError(Exception):
    pass


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _hm(ts: float) -> str:
    return time.strftime("%d.%m %H:%M", time.gmtime(ts + 3 * 3600))


def shrunk_avg(rets: list[float], prior: int = 5) -> float:
    """Средний результат с поправкой на везение: как будто к N прогнозам добавили 5 нулевых."""
    return sum(rets) / (len(rets) + prior) if rets else 0.0


def parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise MentorError("ИИ вернул не JSON")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as e:
        raise MentorError(f"ИИ вернул битый JSON: {e}") from None


def clean_analysis(raw: dict) -> dict:
    """Привести ответ ИИ к ожидаемому виду: обрезать, проверить типы, выкинуть мусор."""
    def s(v, n):
        return str(v or "").strip()[:n]

    calls = []
    for c in (raw.get("calls") or [])[:6]:
        if not isinstance(c, dict):
            continue
        asset = s(c.get("asset"), 16).upper().lstrip("$")
        contract = s(c.get("contract"), 50)
        contract = contract if valid_address(contract) else ""
        direction = s(c.get("direction"), 8).lower()
        if direction not in ("long", "short", "avoid") or not (asset or contract):
            continue
        try:
            days = int(float(c.get("horizon_days") or 3))
        except (TypeError, ValueError):
            days = 3
        calls.append({"asset": asset, "contract": contract, "direction": direction,
                      "horizon_days": max(1, min(30, days)),
                      "confidence": s(c.get("confidence"), 8).lower() or "medium", "quote": s(c.get("quote"), 140)})
    tips = []
    for t in (raw.get("tips") or [])[:5]:
        if not isinstance(t, dict) or not s(t.get("text"), 200):
            continue
        rule = t.get("rule") if isinstance(t.get("rule"), dict) else {}
        param = s(rule.get("param"), 20)
        try:
            value = float(rule.get("value")) if rule.get("value") is not None else None
        except (TypeError, ValueError):
            value = None
        tips.append({"text": s(t.get("text"), 200),
                     "rule": {"param": param, "value": value} if param in TIP_PARAMS and value is not None else None})
    view = s(raw.get("market_view"), 10).lower()
    try:
        hype = max(0, min(10, int(float(raw.get("hype") or 0))))
    except (TypeError, ValueError):
        hype = 0
    return {"summary": s(raw.get("summary"), 600), "market_view": view if view in VIEWS else "neutral",
            "calls": calls, "tips": tips, "sponsored": bool(raw.get("sponsored")),
            "sponsor_note": s(raw.get("sponsor_note"), 120), "hype": hype}


VIEWS = {"bullish": "🟢 рост", "bearish": "🔴 падение", "neutral": "⚪ нейтрально", "mixed": "🟡 по-разному"}
TIP_PARAMS = {"stop_pct", "tp_pct", "min_liq_usd", "min_age_hours", "max_chase_pct"}


# ---------------------------------------------------------------------------
# Ссылки и YouTube
# ---------------------------------------------------------------------------

def parse_link(text: str) -> tuple[str, str] | None:
    """Ссылка → (вид, значение): ("yt_channel", UC…), ("yt_handle", name), ("yt_video", id), ("twitch", login)."""
    t = (text or "").strip()
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", t)
    if m:
        return "yt_channel", m.group(1)
    if re.fullmatch(r"UC[\w-]{22}", t):
        return "yt_channel", t
    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/))([\w-]{11})", t)
    if m:
        return "yt_video", m.group(1)
    m = re.search(r"youtube\.com/@([\w.\-]{2,40})", t) or re.fullmatch(r"@([\w.\-]{2,40})", t)
    if m:
        return "yt_handle", m.group(1)
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]{3,25})", t) or re.fullmatch(r"(?:twitch:)([A-Za-z0-9_]{3,25})", t)
    if m:
        return "twitch", m.group(1).lower()
    return None


def parse_feed(xml_text: str) -> tuple[str, list[dict]]:
    """RSS канала → (название канала, ролики: id, title, published, description, url)."""
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
          "media": "http://search.yahoo.com/mrss/"}
    root = ET.fromstring(xml_text)
    name = (root.findtext("a:title", "", ns) or "").strip()
    out = []
    for e in root.findall("a:entry", ns):
        vid = e.findtext("yt:videoId", "", ns)
        link = e.find("a:link", ns)
        url = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}"
        published = e.findtext("a:published", "", ns)
        try:
            ts = time.mktime(time.strptime(published[:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except ValueError:
            ts = 0.0
        stat = e.find("media:group/media:community/media:statistics", ns)
        try:
            views = int(stat.get("views")) if stat is not None and stat.get("views") is not None else None
        except ValueError:
            views = None
        out.append({"id": vid, "title": (e.findtext("a:title", "", ns) or "").strip(), "published": ts,
                    "url": url, "short": "/shorts/" in (url or ""), "views": views,
                    "description": (e.findtext("media:group/media:description", "", ns) or "")[:1500]})
    return name, out


class YouTube:
    def __init__(self, session_factory: Callable[[], Awaitable[aiohttp.ClientSession]]):
        self._session = session_factory

    async def _get(self, url: str) -> str:
        http = await self._session()
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ai-casher-bot)", "Accept-Language": "en",
                   "Cookie": "CONSENT=YES+1; SOCS=CAI"}
        async with http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise MentorError(f"YouTube ответил {r.status}")
            return await r.text()

    async def feed(self, channel_id: str) -> tuple[str, list[dict]]:
        return parse_feed(await self._get(RSS_URL.format(channel_id)))

    async def resolve(self, kind: str, value: str) -> tuple[str, str]:
        """@имя или ролик → (id канала, название)."""
        url = f"https://www.youtube.com/@{value}" if kind == "yt_handle" else f"https://www.youtube.com/watch?v={value}"
        page = await self._get(url)
        m = (re.search(r'"(?:channelId|externalId|browseId)":"(UC[\w-]{22})"', page)
             or re.search(r"youtube\.com/channel/(UC[\w-]{22})", page))
        if not m:
            raise MentorError("не нашёл канал на странице (YouTube мог не пустить сервер) — пришли ссылку вида "
                              "youtube.com/channel/UC…")
        name = re.search(r'<meta (?:property|name)="og:title" content="([^"]+)"', page)
        if kind == "yt_video":
            author = re.search(r'"author":"([^"]+)"', page)
            return m.group(1), html.unescape(author.group(1)) if author else value
        return m.group(1), html.unescape(name.group(1)) if name else value


# ---------------------------------------------------------------------------
# Разбор ролика: Gemini по ссылке, иначе — название и описание через ИИ бота
# ---------------------------------------------------------------------------

def gemini_key_info(env: dict | None = None) -> tuple[str, str | None, str | None]:
    """(ключ, имя переменной, подсказка). Ключи Gemini: новый формат AQ. (с 2026 г.) и старый AIza."""
    env = os.environ if env is None else env
    hint = None
    for name in (*GEMINI_ENV_ALIASES, "AI_API_KEY"):
        key = clean_key(env.get(name))
        if not key:
            continue
        if is_gemini_key(key):
            return key, name, None
        if name != "AI_API_KEY":
            hint = (f"в {name} лежит не ключ Gemini (начало «{key[:4]}…»). Ключ Gemini начинается с AQ. или AIza — "
                    "скопируй его в AI Studio кнопкой «Copy key»")
    return "", None, hint


def gemini_key(env: dict | None = None) -> str:
    return gemini_key_info(env)[0]


class GeminiVideo:
    """Gemini «смотрит» публичный ролик YouTube по ссылке (звук + кадр в секунду, первые 20 минут)."""

    PREFER = ("flash",)

    def __init__(self, key: str, session_factory, model: str | None = None):
        self.key = key
        self._session = session_factory
        self.model = model or (os.environ.get("GEMINI_VIDEO_MODEL") or "").strip() or None
        self.dead: set[str] = set()
        self.disabled_until = 0.0
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def _pick_model(self) -> str:
        if self.model and self.model not in self.dead:
            return self.model
        http = await self._session()
        names: list[str] = []
        try:
            async with http.get(f"{GEMINI_BASE}/models?pageSize=200", headers={"x-goog-api-key": self.key}) as r:
                if r.status in (400, 401, 403):
                    body = (await r.text())[:160]
                    raise MentorError(f"ключ Gemini не подходит (ошибка {r.status}): {body}")
                if r.status == 200:
                    data = await r.json(content_type=None)
                    names = [m["name"].removeprefix("models/") for m in data.get("models", [])
                             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        except MentorError:
            raise
        except Exception as e:
            log.info("Gemini: список моделей: %s", e)
        model = pick_video_model(names, self.dead) or next(
            (m for m in ("gemini-flash-latest", "gemini-2.5-flash") if m not in self.dead), None)
        if not model:
            raise MentorError("Gemini: нет подходящей модели. Задай GEMINI_VIDEO_MODEL")
        self.model = model
        return model

    async def check(self) -> str:
        """Проверка ключа: короткий запрос к Gemini. Возвращает понятный итог (для кнопки «🔌 Проверить Gemini»)."""
        if not self.key:
            raise MentorError("ключа Gemini нет")
        self.dead.discard(self.model or "")
        model = await self._pick_model()
        http = await self._session()
        body = {"contents": [{"parts": [{"text": "Ответь одним словом: ок"}]}],
                "generationConfig": {"maxOutputTokens": 200}}
        async with http.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                             headers={"x-goog-api-key": self.key}, timeout=aiohttp.ClientTimeout(total=40)) as r:
            text = await r.text()
            if r.status == 200:
                self.last_error = None
                return model
            if r.status == 429:
                raise MentorError("ключ рабочий, но сейчас исчерпан бесплатный лимит — подожди минуту")
            if r.status in (400, 401, 403):
                raise MentorError(f"Google отклонил ключ (ошибка {r.status}): {text[:180]}")
            raise MentorError(f"Gemini: ошибка {r.status}: {text[:180]}")

    async def analyze(self, url: str, title: str = "") -> dict:
        if time.time() < self.disabled_until:
            raise MentorError("Gemini: лимит, жду")
        http = await self._session()
        for _ in range(3):
            model = await self._pick_model()
            body = {"contents": [{"parts": [
                {"file_data": {"file_uri": url}, "video_metadata": {"start_offset": "0s",
                                                                    "end_offset": f"{VIDEO_MAX_SEC}s"}},
                {"text": PROMPT + (f"\nНазвание ролика: {title}" if title else "")}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                     "maxOutputTokens": 3000, "mediaResolution": "MEDIA_RESOLUTION_LOW"}}
            async with http.post(f"{GEMINI_BASE}/models/{model}:generateContent", json=body,
                                 headers={"x-goog-api-key": self.key},
                                 timeout=aiohttp.ClientTimeout(total=240)) as r:
                text = await r.text()
                if r.status == 200:
                    data = json.loads(text)
                    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                    out = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                    if not out:
                        raise MentorError("Gemini вернул пустой ответ (ролик недоступен или ещё идёт эфир)")
                    self.last_error = None
                    return clean_analysis(parse_json(out))
                low = text.lower()
                if r.status == 429:
                    self.disabled_until = time.time() + 15 * 60
                    raise MentorError("Gemini: лимит бесплатного тарифа, продолжу через 15 минут")
                if r.status in (401, 403):
                    raise MentorError(f"Gemini: ключ не подходит ({r.status})")
                if r.status in (400, 404) and "model" in low and ("not found" in low or "not supported" in low):
                    self.dead.add(model)
                    self.model = None
                    continue
                if r.status == 400 and ("mediaresolution" in low or "media_resolution" in low or "video_metadata" in low):
                    body["generationConfig"].pop("mediaResolution", None)  # старые модели не знают параметр
                    continue
                raise MentorError(f"Gemini: ошибка {r.status}: {text[:160]}")
        raise MentorError("Gemini: не удалось разобрать ролик")


def pick_video_model(names: list[str], dead: set) -> str | None:
    ok = [n for n in names if "flash" in n and n not in dead
          and not any(x in n for x in ("image", "tts", "live", "audio", "embed", "thinking-exp", "8b"))]
    if not ok:
        return None

    def key(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        ver = float(m.group(1)) if m else 0.0
        return ("preview" in n or "exp" in n, "lite" in n, -ver, len(n))

    return sorted(ok, key=key)[0]


async def analyze_by_text(ai_client, title: str, description: str) -> dict:
    """Запасной путь: обычный ИИ бота читает название и описание (сам ролик не видит)."""
    messages = [{"role": "system", "content": PROMPT + "\nВидео недоступно: суди по названию и описанию, "
                                                       "в summary напиши, что это по описанию."},
                {"role": "user", "content": f"Название: {title}\nОписание:\n{description[:1500]}"}]
    return clean_analysis(parse_json(await ai_client.chat(messages, max_tokens=900)))


# ---------------------------------------------------------------------------
# Twitch: анонимное чтение чата
# ---------------------------------------------------------------------------

def parse_irc(line: str) -> dict | None:
    """Строка IRC Twitch → {channel, user, text, badges, display} для PRIVMSG, иначе None."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    m = re.match(r":(\w+)!\S+ PRIVMSG #(\w+) :(.*)", line)
    if not m:
        return None
    badges = {b.split("/")[0] for b in tags.get("badges", "").split(",") if b}
    return {"user": m.group(1).lower(), "channel": m.group(2).lower(), "text": m.group(3).strip(),
            "badges": badges, "display": tags.get("display-name") or m.group(1)}


def trusted_sender(msg: dict) -> bool:
    return msg["user"] == msg["channel"] or bool(msg["badges"] & {"broadcaster", "moderator", "vip"}) \
        or msg["user"] in TWITCH_BOTS


def addresses_in(text: str) -> list[str]:
    return [a for a in dict.fromkeys(ADDR_RE.findall(text or "")) if valid_address(a)]


class TwitchChat:
    """Слушает чаты каналов без логина (justinfan). Адреса токенов от стримера/модераторов кладёт в очередь."""

    def __init__(self, session_factory):
        self._session = session_factory
        self.channels: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.connected = False
        self.last_error: str | None = None
        self.messages = 0
        self.last_call: dict[str, float] = {}  # канал → когда стример/модератор кинул адрес токена (🎥 смотреть сразу)
        self._ws = None
        self._want: set[str] = set()

    async def set_channels(self, channels: set[str]) -> None:
        self._want = {c.lower() for c in channels}
        ws = self._ws
        if ws is not None and not ws.closed:
            add, drop = self._want - self.channels, self.channels - self._want
            if add:
                await ws.send_str("JOIN " + ",".join("#" + c for c in sorted(add)))
            if drop:
                await ws.send_str("PART " + ",".join("#" + c for c in sorted(drop)))
            self.channels = set(self._want)

    def handle(self, line: str) -> None:
        msg = parse_irc(line)
        if not msg:
            return
        self.messages += 1
        if not trusted_sender(msg):
            return
        found = addresses_in(msg["text"])
        if found:
            self.last_call[msg["channel"]] = time.time()
        for addr in found:
            try:
                self.queue.put_nowait({"channel": msg["channel"], "mint": addr, "by": msg["display"],
                                       "text": msg["text"][:200], "at": time.time()})
            except asyncio.QueueFull:
                pass

    async def run(self) -> None:
        delay = 5
        while True:
            if not self._want:
                await asyncio.sleep(30)
                continue
            try:
                http = await self._session()
                async with http.ws_connect(TWITCH_WS, heartbeat=60, timeout=20) as ws:
                    self._ws = ws
                    await ws.send_str("CAP REQ :twitch.tv/tags twitch.tv/commands")
                    await ws.send_str("PASS SCHMOOPIIE")
                    await ws.send_str(f"NICK justinfan{random.randint(10000, 99999)}")
                    self.channels = set(self._want)
                    await ws.send_str("JOIN " + ",".join("#" + c for c in sorted(self.channels)))
                    self.connected, self.last_error, delay = True, None, 5
                    async for m in ws:
                        if m.type != aiohttp.WSMsgType.TEXT:
                            break
                        for line in m.data.split("\r\n"):
                            if line.startswith("PING"):
                                await ws.send_str("PONG" + line[4:])
                            elif line:
                                self.handle(line)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"чат Twitch: {e}"[:160]
                log.info("%s", self.last_error)
            self.connected, self._ws = False, None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


# ---------------------------------------------------------------------------
# Наставники
# ---------------------------------------------------------------------------

def new_state() -> dict:
    return {"on": True, "notify": True, "sources": {}, "videos": [], "calls": [], "tips": [], "log": [],
            "budget": {"day": "", "videos": 0}, "seeded": False, "seq": 0}


class Mentors:
    def __init__(self, copy, meme=None, ai_client=None, price_of=None, env: dict | None = None,
                 youtube: YouTube | None = None, gemini: GeminiVideo | None = None,
                 twitch: TwitchChat | None = None, candles_of=None) -> None:
        self.copy = copy
        self.meme = meme
        self.ai_client = ai_client
        self.price_of = price_of  # async (coins) -> {COIN: цена в $} с бирж Bybit/BingX
        self.candles_of = candles_of  # async (coin, since_ms) -> дневные свечи (📜 история прогнозов в разведчике)
        self._http: aiohttp.ClientSession | None = None
        self.youtube = youtube or YouTube(self._session)
        key, self.gemini_env, self.gemini_hint = gemini_key_info(env)
        self.gemini = gemini if gemini is not None else GeminiVideo(key, self._session)
        self.twitch = twitch or TwitchChat(self._session)
        self.last_error: str | None = None
        self.busy = False
        self.train = Training(self)  # 🏋️ сам торгует выученным на демо (bot/training.py)
        self.scout = Scout(self)  # 🔭 сам ищет трейдеров и отсеивает фейки (bot/scout.py)
        self.pending: list[dict] = []  # сигналы тренировки, появившиеся при разборе ролика
        self.live = LiveWatch(self)  # 🎥 смотрит эфиры Twitch: действия стримера, ошибки и плюсы (bot/livewatch.py)

    def take_pending(self) -> list[dict]:
        out, self.pending = self.pending, []
        return out

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()

    @property
    def st(self) -> dict:
        st = self.copy.st.get("mentors")
        if not isinstance(st, dict):
            st = {}
            self.copy.st["mentors"] = st
        for k, v in new_state().items():
            st.setdefault(k, v)
        return st

    def _changed(self) -> None:
        self.copy.changes += 1

    def _log(self, text: str, now: float | None = None) -> str:
        self.st["log"].append({"at": time.time() if now is None else now, "text": text})
        del self.st["log"][:-30]
        self._changed()
        return text

    # --- источники ------------------------------------------------------------

    def seed(self) -> bool:
        st = self.st
        if st["seeded"]:
            return False
        st["seeded"] = True
        for kind, ident, name, note in DEFAULT_SOURCES:
            self._add(kind, ident, name, note=note, user=False)
        self._changed()
        return True

    def _add(self, kind: str, ident: str, name: str, note: str = "", user: bool = True) -> dict:
        sid = f"{'yt' if kind == 'youtube' else 'tw'}:{ident}"
        src = self.st["sources"].get(sid)
        if src:
            src["removed"] = False
            return src
        src = {"id": sid, "kind": kind, "ident": ident, "name": name[:40], "note": note, "user": user,
               "status": "watch", "added": time.time(), "seen": [], "videos": 0, "calls": 0, "rets": [],
               "sponsored": 0, "hype": [], "checked_at": 0.0, "error": None, "removed": False}
        self.st["sources"][sid] = src
        self._changed()
        return src

    def active(self, kind: str | None = None) -> list[dict]:
        return [s for s in self.st["sources"].values() if not s.get("removed") and (kind is None or s["kind"] == kind)]

    def find(self, query: str) -> dict | None:
        q = (query or "").strip().lower().lstrip("@")
        for s in self.active():
            if q in (s["id"].lower(), s["ident"].lower(), s["name"].lower()):
                return s
        return None

    async def add_link(self, text: str) -> tuple[dict | None, str]:
        """Ссылка от пользователя → источник. Ролик → разобрать сразу (и подписаться на канал, если удастся)."""
        p = parse_link(text)
        if not p:
            return None, ("Не понял ссылку. Подходит: youtube.com/@канал, youtube.com/channel/UC…, ссылка на ролик "
                          "или twitch.tv/канал")
        kind, value = p
        if kind == "twitch":
            src = self._add("twitch", value, value)
            await self.twitch.set_channels({s["ident"] for s in self.active("twitch")})
            return src, f"📺 Слушаю чат Twitch <b>{html.escape(value)}</b>: адреса токенов от стримера и модераторов " \
                        "будут проверяться ценой."
        if kind == "yt_channel":
            name = value
            try:
                name, _ = await self.youtube.feed(value)
            except Exception as e:
                log.info("RSS %s: %s", value, e)
            src = self._add("youtube", value, name or value)
            return src, f"▶️ Слежу за каналом <b>{html.escape(src['name'])}</b>: новые ролики разберу и проверю прогнозы."
        if kind == "yt_handle":
            cid, name = await self.youtube.resolve(kind, value)
            src = self._add("youtube", cid, name)
            return src, f"▶️ Слежу за каналом <b>{html.escape(src['name'])}</b>: новые ролики разберу и проверю прогнозы."
        return None, "video:" + value

    def remove(self, src: dict) -> None:
        src["removed"] = True
        self._changed()

    # --- разбор роликов -------------------------------------------------------------

    def _budget_left(self, now: float) -> int:
        b = self.st["budget"]
        if b["day"] != _day(now):
            b["day"], b["videos"] = _day(now), 0
        return VIDEOS_PER_DAY - b["videos"]

    async def analyze_video(self, src: dict | None, video: dict, now: float | None = None,
                            wait_on_limit: bool = False) -> dict:
        """Разобрать ролик → запись с выводами + прогнозы на проверку.

        wait_on_limit — при лимите Gemini не разбирать по описанию, а бросить ошибку (ролик попробуем позже)."""
        now = time.time() if now is None else now
        method = "gemini"
        if self.gemini.configured and self._budget_left(now) > 0:
            try:
                res = await self.gemini.analyze(video["url"], video.get("title", ""))
                self.st["budget"]["videos"] += 1
            except MentorError as e:
                self.last_error = str(e)
                if "лимит" not in str(e):
                    self.gemini.last_error = str(e)
                text_ok = self.ai_client is not None and getattr(self.ai_client, "configured", True)
                if not text_ok or (wait_on_limit and "лимит" in str(e)) or not (video.get("title") or video.get("description")):
                    raise
                method = "text"
                res = await analyze_by_text(self.ai_client, video.get("title", ""), video.get("description", ""))
        elif self.ai_client is not None and getattr(self.ai_client, "configured", True):
            if not (video.get("title") or video.get("description")):
                raise MentorError("без ключа Gemini ролик по ссылке не посмотреть, а названия я не нашёл. "
                                  "Добавь GEMINI_API_KEY (бесплатно, /mentors → ❔)")
            method = "text"
            res = await analyze_by_text(self.ai_client, video.get("title", ""), video.get("description", ""))
        else:
            raise MentorError("нет ни Gemini, ни ИИ бота — разбирать ролики нечем (нужен GEMINI_API_KEY или AI_API_KEY)")
        st = self.st
        st["seq"] += 1
        rec = {"id": video["id"], "source": src["id"] if src else None, "author": src["name"] if src else
               video.get("author", ""), "title": video.get("title", "")[:150], "url": video["url"],
               "published": video.get("published") or now, "at": now, "method": method, **res, "call_ids": []}
        if src:
            src["videos"] += 1
            src["sponsored"] += res["sponsored"]
            src["hype"] = (src["hype"] + [res["hype"]])[-20:]
        for c in res["calls"]:
            call = await self._open_call(src, c, now, video=video["id"])
            if call:
                rec["call_ids"].append(call["id"])
                e = self.train.on_call(call, src, {"sponsored": res["sponsored"], "hype": res["hype"]}, now)
                if e:
                    self.pending.append(e)
        st["videos"].append(rec)
        del st["videos"][:-40]
        for t in res["tips"]:
            self._add_tip(src, t, now)
        self._changed()
        return rec

    async def check_youtube(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        events: list[dict] = []
        done = 0
        for src in sorted(self.active("youtube"), key=lambda s: s["checked_at"]):
            if done >= VIDEOS_PER_TICK:
                break
            if now - src["checked_at"] < RSS_EVERY or not self.scout.can_watch(src, now, self._budget_left(now)):
                continue
            src["checked_at"] = now
            try:
                name, videos = await self.youtube.feed(src["ident"])
                src["error"] = None
                if name and src["name"] == src["ident"]:
                    src["name"] = name[:40]
            except Exception as e:
                src["error"] = str(e)[:120]
                continue
            fresh = [v for v in videos if not v["short"] and now - v["published"] <= VIDEO_MAX_AGE]
            if not src["seen"]:  # первое знакомство: разбираем только самый свежий ролик, остальное — в «видел»
                newest = max(fresh, key=lambda v: v["published"])["id"] if fresh else None
                src["seen"] = [v["id"] for v in videos if v["id"] != newest][-40:] or ["-"]
            for v in sorted(fresh, key=lambda v: v["published"]):
                if v["id"] in src["seen"] or done >= VIDEOS_PER_TICK:
                    continue
                try:
                    rec = await self.analyze_video(src, v, now, wait_on_limit=True)
                except Exception as e:
                    self.last_error = f"{src['name']}: {e}"[:200]
                    if "лимит" in str(e):
                        break  # попробуем этот ролик позже
                    src["seen"] = (src["seen"] + [v["id"]])[-40:]
                    continue
                src["seen"] = (src["seen"] + [v["id"]])[-40:]
                done += 1
                if self.st["notify"] and src["status"] != "muted":
                    events.append(self.video_event(rec, src))
            self._changed()
        return events

    def video_event(self, rec: dict, src: dict | None) -> dict:
        icon = {"star": "⭐", "muted": "🔇"}.get(src["status"], "👀") if src else "📺"
        lines = [f"🎓 {icon} <b>{html.escape(rec['author'])}</b>: {html.escape(rec['title'])}",
                 f"{VIEWS.get(rec['market_view'], '')} · {html.escape(rec['summary'])}"]
        if rec["calls"]:
            lines.append("Прогнозы (бот проверит ценой): " + ", ".join(
                f"{'📈' if c['direction'] == 'long' else '📉' if c['direction'] == 'short' else '🚫'} "
                f"{html.escape(c['asset'] or c['contract'][:6])} на {c['horizon_days']} дн." for c in rec["calls"][:5]))
        if rec["tips"]:
            lines.append("Совет: " + html.escape(rec["tips"][0]["text"]))
        warn = []
        if rec["sponsored"]:
            warn.append("🎟 реклама" + (f": {html.escape(rec['sponsor_note'])}" if rec["sponsor_note"] else ""))
        if rec["hype"] >= 7:
            warn.append(f"🎪 хайп {rec['hype']}/10")
        if rec["method"] == "text":
            warn.append("разбор по описанию — сам ролик не смотрел")
        if src and src["rets"]:
            warn.append(f"счёт автора: {self.score_text(src)}")
        if warn:
            lines.append(" · ".join(warn))
        return {"text": "\n".join(lines), "buttons": [("▶️ Открыть ролик", rec["url"])]}

    # --- прогнозы и их проверка --------------------------------------------------------

    async def _open_call(self, src: dict | None, c: dict, now: float, video: str | None = None,
                         kind: str | None = None) -> dict | None:
        """Запомнить цену в момент, когда бот мог бы войти. Проверим её через срок прогноза."""
        st = self.st
        mint = c.get("contract") or ""
        sid = src["id"] if src else None
        for old in st["calls"][-200:]:
            if old["source"] == sid and old["key"] == (mint or c["asset"]) and now - old["at"] < CALL_DEDUP_SEC:
                return None
        call = {"id": f"c{st['seq']}_{len(st['calls'])}", "source": sid, "video": video, "asset": c.get("asset", ""),
                "mint": mint, "key": mint or c["asset"], "direction": c["direction"], "at": now,
                "horizon_h": SOL_HORIZON_H if mint else c.get("horizon_days", 3) * 24, "quote": c.get("quote", ""),
                "conf": c.get("confidence", "medium"), "path": [0.0], "done": False, "ret": None, "price0": None,
                "kind": kind or ("sol" if mint else "cex")}
        if call["asset"] in STABLE and not mint:
            return None
        if mint:
            pair = (await self.copy._pairs([mint])).get(mint)
            if not pair or not pair.price_usd:
                return None
            call["price0"], call["asset"] = pair.price_usd, (pair.symbol or call["asset"] or mint[:6])[:16]
            call["liq"] = pair.liquidity_usd
        else:
            if not TICKER_RE.match(call["asset"]) or self.price_of is None:
                return None
            prices = await self.price_of([call["asset"]])
            if not prices.get(call["asset"]):
                return None  # монеты нет на Bybit/BingX — честно проверить не можем
            call["price0"] = prices[call["asset"]]
        st["calls"].append(call)
        del st["calls"][:-200]
        if src:
            src["calls"] += 1
        self._changed()
        return call

    def _sign(self, call: dict) -> int:
        return -1 if call["direction"] in ("short", "avoid") else 1

    def _finish(self, call: dict) -> float:
        """Итог прогноза в %: мемкоин — как копия со стопом/тейком бота; монета с биржи — по цене через срок."""
        last = call["path"][-1]
        if call["kind"] == "sol" and call["direction"] == "long":
            stop = self.meme.st["stop_pct"] if self.meme else 0.4
            tp = self.meme.st["tp_pct"] if self.meme else None
            cost = self.copy.cost_side()
            return round(simulate_exit({"path": call["path"], "ret": exit_net(last, cost)}, stop, tp, cost), 2)
        if call["direction"] == "avoid":
            return round(-last, 2)  # «не покупай» прав, если цена упала (сделки нет — комиссий нет)
        return round(self._sign(call) * last - 2 * CEX_COST * 100, 2)

    async def update_calls(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        open_ = [c for c in st["calls"] if not c["done"]]
        if not open_:
            return []
        mints = [c["mint"] for c in open_ if c["mint"]]
        coins = sorted({c["asset"] for c in open_ if not c["mint"]})
        pairs = {}
        prices = {}
        try:
            pairs = await self.copy._pairs(mints) if mints else {}
        except Exception as e:
            log.info("Цены мемкоинов для прогнозов: %s", e)
        if coins and self.price_of:
            try:
                prices = await self.price_of(coins)
            except Exception as e:
                log.info("Цены монет для прогнозов: %s", e)
        events = []
        for c in open_:
            p = (pairs.get(c["mint"]).price_usd if pairs.get(c["mint"]) else None) if c["mint"] else prices.get(c["asset"])
            if p and c["price0"]:
                add_path(c["path"], (p / c["price0"] - 1) * 100)
            if now - c["at"] >= c["horizon_h"] * 3600:
                c["done"], c["ret"], c["closed"] = True, self._finish(c), now
                c["path"] = c["path"][-1:]  # итог посчитан — путь цены больше не нужен (бэкап легче)
                src = st["sources"].get(c["source"]) if c["source"] else None
                if src:
                    src["rets"] = (src["rets"] + [c["ret"]])[-40:]
                    e = self._rate(src, now)
                    if e:
                        events.append(e)
        self._changed()
        return events

    def score_text(self, src: dict) -> str:
        r = src["rets"]
        if not r:
            return "прогнозов ещё не проверено"
        wins = sum(x > 0 for x in r)
        return f"{len(r)} проверено, сбылось {wins}, в среднем {shrunk_avg(r):+.1f}% (с поправкой на везение)"

    def _rate(self, src: dict, now: float) -> dict | None:
        r = src["rets"]
        if len(r) < MIN_CHECKED:
            return None
        avg = shrunk_avg(r)
        ads = src["videos"] >= 4 and src["sponsored"] / src["videos"] >= 0.5
        new = "star" if avg >= STAR_AVG and not ads else "muted" if avg <= MUTE_AVG else "watch"
        if new == src["status"]:
            return None
        old, src["status"] = src["status"], new
        if new == "star":
            text = (f"⭐ <b>{html.escape(src['name'])}</b> заслужил доверие: {self.score_text(src)}. Его новые прогнозы "
                    "теперь приходят сигналом" + (", а адреса токенов — сделкой на 🧪 демо-счёте." if src["kind"] == "twitch"
                                                  else "."))
        elif new == "muted":
            text = (f"🔇 <b>{html.escape(src['name'])}</b>: советы не окупаются — {self.score_text(src)}. Сигналы от него "
                    "выключил, ИИ будет считать его мнение ненадёжным.")
        else:
            text = f"👀 <b>{html.escape(src['name'])}</b> снова под наблюдением ({'был ⭐' if old == 'star' else 'был 🔇'}): " \
                   f"{self.score_text(src)}."
        self._log(re.sub(r"<[^>]+>", "", text), now)
        return {"text": "🎓 " + text, "buttons": []}

    # --- Twitch ------------------------------------------------------------------

    async def drain_twitch(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        events = []
        while not self.twitch.queue.empty():
            item = self.twitch.queue.get_nowait()
            src = self.st["sources"].get(f"tw:{item['channel']}")
            if not src or src.get("removed"):
                continue
            try:
                call = await self._open_call(src, {"asset": "", "contract": item["mint"], "direction": "long",
                                                   "quote": f"{item['by']}: {item['text']}"}, now, kind="sol")
            except Exception as e:
                log.info("Колл из Twitch: %s", e)
                continue
            if not call:
                continue
            signal = self.train.on_call(call, src, {"by_streamer": item["by"].lower() == item["channel"]}, now)
            if src["status"] == "star" and self.st["notify"]:
                events.append(await self._star_call_event(src, call, now))
            elif signal:
                events.append(signal)
        return events

    async def _star_call_event(self, src: dict, call: dict, now: float) -> dict:
        text = (f"🎓 ⭐ <b>{html.escape(src['name'])}</b> (Twitch) показал токен <b>{html.escape(call['asset'])}</b> · "
                f"ликвидность ${fmt_usd(call.get('liq') or 0)}\n{html.escape(call['quote'][:160])}\n"
                f"Счёт стримера: {self.score_text(src)}")
        if self.meme and self.meme.demo_on and call["mint"] not in self.meme.demo["positions"]:
            try:
                e = await self.meme.demo_buy(call["mint"], self.meme.st["size_usd"], source="mentor",
                                             symbol=call["asset"], wallet_name=src["name"], now=now, category="mentor")
                text += "\n" + e["text"]
            except Exception as err:
                text += f"\n🧪 На демо не купил: {html.escape(str(err))[:120]}"
        return {"text": text, "buttons": [("📈 DexScreener", f"https://dexscreener.com/solana/{call['mint']}")],
                "mint": call["mint"]}

    # --- советы ---------------------------------------------------------------------

    def _add_tip(self, src: dict | None, tip: dict, now: float) -> dict:
        rec = {"text": tip["text"], "rule": tip.get("rule"), "source": src["id"] if src else None,
               "author": src["name"] if src else "", "at": now, "verdict": None}
        if rec["rule"]:
            rec["verdict"] = self.verify_tip(rec)
        self.st["tips"].append(rec)
        del self.st["tips"][:-50]
        return rec

    def verify_tip(self, tip: dict) -> dict:
        """Совет с числом проверяем на истории копий: принимаем, только если он дал бы заметно больше."""
        rule = tip["rule"]
        param, value = rule["param"], float(rule["value"])
        closed = self.copy.st["closed"]
        if param in ("stop_pct", "tp_pct"):
            trades = [t for t in closed if t.get("passed") and len(t.get("path") or []) >= 2]
            if len(trades) < EXIT_MIN:
                return {"state": "wait", "text": f"проверю, когда будет {EXIT_MIN} копий с путём цены (сейчас {len(trades)})"}
            ms = self.meme.st if self.meme else {"stop_pct": 0.4, "tp_pct": None}
            cost = self.copy.cost_side()
            stop, tp = ms["stop_pct"], ms["tp_pct"]
            if param == "stop_pct":
                new_stop, new_tp = min(EXIT_STOPS, key=lambda s: abs(s - value / 100)), tp
            else:
                new_stop, new_tp = stop, min([x for x in EXIT_TPS if x], key=lambda x: abs(x - value / 100))
            cur = sum(simulate_exit(t, stop, tp, cost) for t in trades)
            alt = sum(simulate_exit(t, new_stop, new_tp, cost) for t in trades)
            what = f"стоп −{new_stop:.0%}" if param == "stop_pct" else f"тейк-профит {tp_text(new_tp)}"
            if (new_stop, new_tp) == (stop, tp):
                return {"state": "same", "text": f"{what} — бот уже так и делает"}
            if alt - cur >= EXIT_GAIN and self.meme:
                ms["stop_pct"], ms["tp_pct"] = new_stop, new_tp
                self._log(f"✅ Принял совет {tip['author']}: {what}. На {len(trades)} копиях было бы {alt:+.1f}% вместо "
                          f"{cur:+.1f}%.")
                return {"state": "accepted", "text": f"принят: {what} дал бы {alt:+.1f}% вместо {cur:+.1f}% на {len(trades)} копиях"}
            return {"state": "rejected", "text": f"не подтвердился: {what} дал бы {alt:+.1f}% против нынешних {cur:+.1f}%"}
        name, val, fmt = {"min_liq_usd": ("min_liq", value, lambda v: f"${fmt_usd(v)}"),
                          "min_age_hours": ("min_age_h", value, lambda v: f"{v:g} ч"),
                          "max_chase_pct": ("max_chase", value / 100, lambda v: f"{v:.0%}")}[param]
        trades = [t for t in closed if t.get("feats") and not t.get("hard_fail")]
        if len(trades) < 20:
            return {"state": "wait", "text": f"проверю, когда будет 20 копий (сейчас {len(trades)})"}
        filt = self.copy.st["filter"]
        val = min(FILTER_GRID[name], key=lambda v: abs(v - val))
        if filt[name] == val:
            return {"state": "same", "text": f"фильтр уже {fmt(val)}"}

        def total(v):
            sub = [t["ret"] for t in trades if _passes(t["feats"], dict(filt, **{name: v}))]
            return sum(sub), len(sub)

        (cur, cur_n), (alt, n) = total(filt[name]), total(val)
        if alt - cur >= LEARN_GAIN and n >= LEARN_SUBSET:
            old = filt[name]
            filt[name] = val
            self._log(f"✅ Принял совет {tip['author']}: фильтр {fmt(old)} → {fmt(val)}. На истории копий было бы "
                      f"{alt:+.1f}% ({n} сделок) вместо {cur:+.1f}% ({cur_n}).")
            return {"state": "accepted", "text": f"принят: {fmt(val)} дал бы {alt:+.1f}% вместо {cur:+.1f}%"}
        return {"state": "rejected", "text": f"не подтвердился: {fmt(val)} дал бы {alt:+.1f}% ({n} сделок) против "
                                             f"{cur:+.1f}% ({cur_n})"}

    # --- шаг -----------------------------------------------------------------------

    async def tick(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        if not self.st["on"]:
            await self.twitch.set_channels(set())  # выключено — чаты не слушаем
            return []
        if self.busy:
            return []
        self.busy = True
        try:
            events = await self.drain_twitch(now)
            events += await self.check_youtube(now)
            events += self.take_pending()
            events += await self.update_calls(now)
            events += await self.train.update(now)
            events += await self.scout.step(now)
            await self.twitch.set_channels({s["ident"] for s in self.active("twitch")})
            return events
        finally:
            self.busy = False

    # --- показ -----------------------------------------------------------------------

    def consensus(self, days: int = 7, now: float | None = None) -> list[dict]:
        """Что трейдеры говорят о монетах за неделю: голоса с весом по их счёту (⭐ ×2, 🔇 не считаем)."""
        now = time.time() if now is None else now
        st = self.st
        out: dict[str, dict] = {}
        for c in st["calls"]:
            if now - c["at"] > days * 86400 or c["kind"] != "cex":
                continue
            src = st["sources"].get(c["source"]) if c["source"] else None
            w = 0 if src and src["status"] == "muted" else 2 if src and src["status"] == "star" else 1
            if not w:
                continue
            d = out.setdefault(c["asset"], {"asset": c["asset"], "long": 0, "short": 0, "authors": set()})
            d["long" if c["direction"] == "long" else "short"] += w
            if src:
                d["authors"].add(src["name"])
        rows = sorted(out.values(), key=lambda d: -(d["long"] + d["short"]))[:8]
        return [{**d, "authors": sorted(d["authors"])[:4]} for d in rows]

    def status_dict(self) -> dict:
        st = self.st
        srcs = []
        for s in sorted(self.active(), key=lambda s: ({"star": 0, "watch": 1, "muted": 2}[s["status"]], -len(s["rets"]))):
            srcs.append({"id": s["id"], "kind": s["kind"], "name": s["name"], "note": s.get("note", ""),
                         "status": s["status"], "videos": s["videos"], "calls": s["calls"], "checked": len(s["rets"]),
                         "wins": sum(x > 0 for x in s["rets"]), "avg": round(shrunk_avg(s["rets"]), 2),
                         "sponsored": s["sponsored"], "hype": round(statistics.mean(s["hype"]), 1) if s["hype"] else None,
                         "error": s.get("error"), "user": s["user"], "auto": bool(s.get("auto")),
                         "trial": bool(s.get("auto") and s.get("trial_until")),
                         "history": history_text(s.get("history")),
                         "live_chat": s["kind"] == "twitch" and s["ident"] in self.twitch.channels and self.twitch.connected})
        open_calls = [c for c in st["calls"] if not c["done"]]
        done = [c for c in st["calls"] if c["done"]]
        return {"on": st["on"], "notify": st["notify"], "gemini": self.gemini.configured,
                "gemini_model": self.gemini.model, "text_ai": bool(self.ai_client and getattr(self.ai_client, "configured", True)),
                "twitch_connected": self.twitch.connected, "twitch_error": self.twitch.last_error,
                "budget_left": self._budget_left(time.time()), "sources": srcs,
                "videos": [{k: v[k] for k in ("author", "title", "url", "summary", "market_view", "sponsored", "hype",
                                              "method", "at")} | {"calls": len(v["calls"])} for v in reversed(st["videos"][-10:])],
                "calls_open": len(open_calls), "calls_done": len(done),
                "recent_calls": [{k: c.get(k) for k in ("asset", "direction", "at", "horizon_h", "done", "ret", "kind")}
                                 | {"author": (st["sources"].get(c["source"]) or {}).get("name", ""),
                                    "now": c["path"][-1] if c["path"] else None}
                                 for c in reversed(st["calls"][-12:])],
                "tips": [{k: t[k] for k in ("text", "author", "at")} | {"verdict": (t["verdict"] or {}).get("text"),
                                                                       "state": (t["verdict"] or {}).get("state")}
                         for t in reversed(st["tips"][-10:])],
                "consensus": self.consensus(), "log": list(reversed(st["log"][-8:])), "last_error": self.last_error,
                "scout": self.scout.status_dict()}

    def ai_context(self) -> dict:
        """Коротко для ИИ: кому верить, что говорят, какие советы проверены."""
        d = self.status_dict()
        return {
            "how": "/mentors: бот смотрит ролики YouTube (Gemini) и чаты Twitch трейдеров и проверяет их прогнозы ценой",
            "traders": [{"name": s["name"], "where": s["kind"], "status": s["status"], "checked": s["checked"],
                         "avg_pct": s["avg"], "ads_videos": s["sponsored"]} for s in d["sources"][:10]],
            "latest_videos": [{"author": v["author"], "view": v["market_view"], "summary": v["summary"][:220],
                               "ad": v["sponsored"], "hype": v["hype"]} for v in d["videos"][:4]],
            "consensus_7d": [{"asset": c["asset"], "long": c["long"], "short": c["short"]} for c in d["consensus"][:5]],
            "tips_checked": [{"tip": t["text"][:120], "by": t["author"], "verdict": t["verdict"]}
                             for t in d["tips"][:4] if t["verdict"]],
            "training_demo": self.train.ai_context(),
            "live_streams": self.live.ai_context(),
            "scout": {"how": "🔭 бот сам ищет трейдеров (Gemini+Google, поиск YouTube, Twitch Crypto), фейк-фильтр, "
                             "испытательный срок, убирает убыточных и рекламщиков",
                      "on_trial": [t["name"] for t in d["scout"]["trial"]], "kept": d["scout"]["kept"],
                      "rejected": [f"{r['name']}: {r['why']}" for r in d["scout"]["rejected"][:4]],
                      "leaderboard_proven_profit": [
                          {"name": x["name"], "live_checked": x["n"], "live_avg_pct": x["avg"],
                           "past_calls": x["hist_n"], "past_avg_pct": x["hist_avg"]} for x in d["scout"]["leaderboard"][:6]]},
        }

    def text(self) -> str:
        d = self.status_dict()
        st = self.st
        if d["gemini"]:
            mode = f"✅ Gemini смотрит ролики (осталось сегодня {max(0, d['budget_left'])} из {VIDEOS_PER_DAY})"
            if self.gemini.last_error:
                mode += f"\n⚠️ {html.escape(self.gemini.last_error[:200])} — нажми «🔌 Проверить Gemini»"
        elif self.gemini_hint:
            mode = f"⚠️ Gemini не подключён: {html.escape(self.gemini_hint)}"
        elif d["text_ai"]:
            mode = "⚠️ Gemini не подключён — разбираю ролики только по названию и описанию (добавь GEMINI_API_KEY)"
        else:
            mode = "⚠️ Нет ключа ИИ — ролики разбирать нечем (GEMINI_API_KEY)"
        tw = sum(1 for s in d["sources"] if s["kind"] == "twitch")
        lines = ["🎓 <b>Учусь у трейдеров</b> — YouTube и Twitch, каждый совет проверяю ценой",
                 f"{'🟢 работает' if d['on'] else '⏸ выключено'} · {mode}",
                 f"Twitch: {'🟢 слушаю чаты' if d['twitch_connected'] else '⏳ подключаюсь' if tw else 'каналов нет'}"
                 + (f" ({tw})" if tw else "")]
        if d["sources"]:
            lines.append("\n<b>Трейдеры</b> (⭐ советы окупаются · 👀 проверяю · 🔭 нашёл сам, на испытании · 🔇 теряют):")
            for s in d["sources"][:12]:
                icon = "🔭" if s["trial"] and s["status"] == "watch" else {"star": "⭐", "watch": "👀", "muted": "🔇"}[s["status"]]
                where = "▶️" if s["kind"] == "youtube" else "📺"
                score = (f"{s['checked']} пров., сбылось {s['wins']}, {s['avg']:+.1f}%" if s["checked"]
                         else f"роликов {s['videos']}, прогнозов {s['calls']}" if s["kind"] == "youtube"
                         else f"токенов {s['calls']}")
                ads = f" · 🎟 реклама в {s['sponsored']}" if s["sponsored"] else ""
                lines.append(f"{icon}{where} <b>{html.escape(s['name'])}</b> — {score}{ads}")
        if d["videos"]:
            lines.append("\n<b>Последние ролики</b>:")
            for v in d["videos"][:3]:
                lines.append(f"• <b>{html.escape(v['author'])}</b>: {VIEWS.get(v['market_view'], '')} — "
                             f"{html.escape(v['summary'][:180])}")
        if d["consensus"]:
            lines.append("\n<b>О чём говорят за неделю</b>: " + ", ".join(
                f"{html.escape(c['asset'])} {'📈' * min(3, c['long'])}{'📉' * min(3, c['short'])}" for c in d["consensus"][:6]))
        lines.append(f"\nПрогнозов на проверке: {d['calls_open']}, проверено: {d['calls_done']}.")
        lv = self.live.st
        on_air = len(lv["sessions"])
        lines.append(f"🎥 Стримы вживую: {'смотрю действия стримеров' if lv['on'] else 'выключено'}"
                     + (f" · сейчас в эфире {on_air}" if on_air else "") + " — кнопка «🎥 Стримы вживую»")
        tl = self.train.short_line()
        lines.append(tl or "🏋️ Тренировка: идеи трейдеров бот сам торгует на демо — /train")
        sc = d["scout"]
        lines.append(f"🔭 Разведчик: {'ищет сам' if sc['on'] else 'выключен'} · на испытании {len(sc['trial'])}, "
                     f"оставил ⭐ {sc['stars']}, отсеял {sc['rejected_total']} — кнопка «🔭 Разведчик»")
        if st["log"]:
            lines.append("🧠 " + html.escape(st["log"][-1]["text"][:240]))
        if d["last_error"]:
            lines.append(f"⚠️ {html.escape(d['last_error'])[:160]}")
        lines.append("\nДобавить: <code>/mentors add ссылка</code> (YouTube-канал или twitch.tv/канал) · "
                     "разобрать ролик: <code>/mentors video ссылка</code>")
        return "\n".join(lines)

    def short_line(self) -> str:
        d = self.status_dict()
        if not d["on"] or not d["sources"]:
            return ""
        stars = [s["name"] for s in d["sources"] if s["status"] == "star"]
        return (f"🎓 <b>Трейдеры</b>: следим за {len(d['sources'])}, ⭐ {len(stars)}"
                + (f" ({', '.join(html.escape(x) for x in stars[:3])})" if stars else "")
                + f", прогнозов проверено {d['calls_done']} — /mentors")
