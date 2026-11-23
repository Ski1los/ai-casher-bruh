"""🎥 Смотрю стримы вживую: что делает стример на экране — входы, выходы, стопы, ошибки и сильные стороны.

Как это устроено:
  * Кто в эфире — один запрос к Twitch GQL раз в 2 минуты на всех стримеров из 🎓 (без логина и ключей).
  * Видео — так же, как его получает плеер Twitch: токен PlaybackAccessToken → плейлист HLS (usher) → последние
    16 секунд эфира в 720p/480p (кусочки по 2 с, рекламные вставки пропускаем). ffmpeg перекладывает их в MP4
    без перекодирования (доли секунды даже на слабом сервере). Нет ffmpeg — берём кадр-превью эфира.
  * Ролик со звуком смотрит Gemini Flash-Lite (бесплатно ~500 запросов в день, отдельно от 20 запросов Flash,
    которые уходят на YouTube). Он видит график, терминал, позицию и слышит, что говорит стример. Ответ — строгий
    JSON: действие (вход/выход/стоп…), монета, вход/стоп/тейк/плечо, сказанное, плюсы и ошибки по фиксированному
    списку, оценка дисциплины.
  * Бот помнит предыдущий взгляд на эфир, поэтому видит ДЕЙСТВИЯ во времени: передвинул стоп, усреднил убыток,
    закрыл в минус.
  * Каждый вход стримера бот проверяет ценой (как прогнозы в 🎓) и запоминает, с какими ошибками он был сделан.
    Через неделю-другую видно на цифрах, сколько стоит каждая ошибка: «входы без стопа — в среднем −3%».
  * Когда эфир кончился — итог: сделки, сильные стороны, ошибки, средняя дисциплина, главный урок.

Ограничения (честно): это не непрерывный просмотр — бот смотрит 16 секунд раз в несколько минут (лимит Gemini),
плюс сразу, когда стример кидает адрес токена в чат. Задержка от действия до сообщения ~30–60 с. Цифры с экрана
ИИ может прочитать неточно, поэтому всё, что идёт в счёт трейдера, проверяется ценой с бирж/DexScreener.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
import random
import re
import shutil
import statistics
import time
from datetime import datetime
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_USHER = "https://usher.ttvnw.net"
TWITCH_PREVIEW = "https://static-cdn.jtvnw.net/previews-ttv/live_user_{}-1280x720.jpg"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # публичный id веб-плеера Twitch (без логина)
TOKEN_HASH = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"  # PlaybackAccessToken (как streamlink)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
LOGIN_RE = re.compile(r"^[a-z0-9_]{3,25}$")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


CLIP_SEC = _env_int("LIVE_CLIP_SEC", 16, 6, 40)          # сколько секунд эфира смотрит ИИ за раз
LIVE_PER_DAY = _env_int("LIVE_PER_DAY", 300, 0, 2000)   # просмотров в день (из ~500 бесплатных Flash-Lite)
MAX_CLIP_BYTES = 10 * 1024 * 1024                        # Gemini принимает до 20 МБ в запросе (base64 +33%)
MAX_BW = 3_600_000                                       # 720p30 (~3 Мбит/с) влезает, 720p60/1080p — нет
INTERVALS = (3, 5, 10, 20)                               # минут между взглядами на один эфир
MAX_STREAMS = 2                                          # одновременно смотрим не больше двух эфиров
ONLINE_EVERY = 120                                       # проверять, кто в эфире
TICK_SEC = 30
SAME_ACTION_SEC = 30 * 60                                # повтор того же действия с той же монетой — не новость
NOTIFY_MODES = ("actions", "all", "summary", "off")
NOTIFY_TEXT = {"actions": "о действиях и ошибках", "all": "о каждом взгляде", "summary": "только итог эфира",
               "off": "не писать"}

ACTIONS = {
    "enter_long": "🟢 Вошёл в LONG", "enter_short": "🔴 Вошёл в SHORT", "add": "➕ Добавил к позиции",
    "take_profit": "💰 Зафиксировал прибыль", "exit_loss": "✂️ Закрыл в убыток", "exit": "🚪 Закрыл позицию",
    "move_stop": "↕️ Передвинул стоп", "set_stop": "🛡 Поставил стоп", "watching": "👀 Смотрит график",
    "talking": "💬 Рассуждает", "none": "—",
}
ENTRY = {"enter_long", "enter_short"}
EXITS = {"take_profit", "exit_loss", "exit"}
SIGNIFICANT = ENTRY | EXITS | {"add", "move_stop", "set_stop"}
PLUS_TAGS = {
    "stop_set": "ставит стоп", "took_profit": "фиксирует прибыль", "cut_loss": "быстро режет убыток",
    "small_size": "небольшой размер позиции", "waited_setup": "ждёт сетап, не спешит",
    "followed_plan": "действует по плану", "explained": "объясняет, почему входит",
}
MISTAKE_TAGS = {
    "no_stop": "нет стопа", "averaging_down": "усредняет убыток", "overleverage": "большое плечо",
    "fomo_chase": "догоняет рост (FOMO)", "revenge": "отыгрывается после убытка", "moved_stop": "отодвигает стоп",
    "oversize": "слишком большая позиция", "no_plan": "вход без плана", "held_loser": "пересиживает убыток",
    "early_exit": "выходит слишком рано", "overtrading": "слишком много сделок",
}
MARKETS = {"spot": "спот", "perp": "фьючерсы", "meme": "мемкоин", "unknown": ""}
LESSON_MIN = 3   # показывать «цену ошибки» от 3 проверенных входов
LESSON_SURE = 8  # от 8 — уверенно

PROMPT = """\
Ты наставник-трейдер. Смотришь {what} прямой трансляции крипто-трейдера «{name}» для бота-новичка: картинку \
(график, терминал, открытая позиция, ордера) и звук (что стример говорит). Название эфира: «{title}».
{before}
Опиши ТОЛЬКО то, что видно и слышно в этом фрагменте; чего не видно — null или пусто, ничего не выдумывай. \
Верни СТРОГО JSON:
{{"trading": true/false (на экране торговля или стример говорит о конкретной сделке),
 "summary": "1–2 предложения по-русски: что сейчас делает стример",
 "platform": "где торгует: Axiom, Photon, GMGN, Bybit, BingX, Binance, TradingView… или пусто",
 "asset": "тикер без $ или пусто", "contract": "адрес токена Solana, если виден целиком, иначе пусто",
 "market": "spot|perp|meme|unknown",
 "action": "enter_long|enter_short|add|take_profit|exit_loss|exit|move_stop|set_stop|watching|talking|none",
 "position": {{"side": "long|short|none", "entry": null, "size_usd": null, "leverage": null, "stop": null, \
"take": null, "pnl_pct": null}},
 "said": "главное, что сказал о сделке, по-русски, до 160 символов",
 "pluses": [{{"tag": "{plus}", "text": "до 120 символов: что именно сделал хорошо"}}],
 "mistakes": [{{"tag": "{miss}", "text": "до 120 символов: что именно сделал не так"}}],
 "discipline": 0-10 или null (насколько действия дисциплинированы; null — если торговли не видно),
 "lesson": "один практический вывод для новичка из этого фрагмента, до 160 символов",
 "confidence": "low|medium|high"}}
Правила: action — действие именно в этом фрагменте (позиция уже была открыта и он просто смотрит — watching). \
Сравни с прошлым взглядом: стоп дальше от цены, чем был, — move_stop и ошибка moved_stop; добавил к убыточной \
позиции — add и ошибка averaging_down. Плюсы и ошибки — только с тегами из списка, не больше 3 каждого, \
только если это действительно видно или сказано. Числа — как на экране (цена, $, %, плечо x)."""


class LiveError(Exception):
    pass


def _s(v: Any, n: int) -> str:
    return str(v or "").strip()[:n]


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = re.sub(r"[\s$%xX×+]", "", v)
        if "," in v and "." in v:
            v = v.replace(",", "")  # 1,234.5
        elif re.fullmatch(r"-?\d{1,3}(,\d{3})+", v):
            v = v.replace(",", "")  # 1,200
        else:
            v = v.replace(",", ".")  # 0,25
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e12 else None


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts + 3 * 3600))


def _dur(sec: float) -> str:
    m = int(sec // 60)
    return f"{m // 60} ч {m % 60} мин" if m >= 60 else f"{m} мин"


def _price(v: float | None) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v:,.0f}".replace(",", " ")
    if abs(v) >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.6g}"


# ---------------------------------------------------------------------------
# Разбор плейлистов Twitch и ответов
# ---------------------------------------------------------------------------

def _attrs(line: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1] if ":" in line else ""):
        out[m.group(1)] = m.group(2).strip('"')
    return out


def parse_master(text: str) -> list[dict]:
    """Мастер-плейлист → варианты качества [{url, bw, h, fps, name}]."""
    out, pending = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            a = _attrs(line)
            res = re.match(r"(\d+)x(\d+)", a.get("RESOLUTION", ""))
            pending = {"bw": int(_num(a.get("BANDWIDTH")) or 0), "h": int(res.group(2)) if res else 0,
                       "fps": _num(a.get("FRAME-RATE")) or 30.0, "name": a.get("VIDEO", "")}
        elif line and not line.startswith("#") and pending is not None:
            out.append({**pending, "url": line})
            pending = None
    return out


def pick_variant(variants: list[dict], max_bw: int = MAX_BW, max_h: int = 720) -> dict | None:
    """Лучшее качество, которое влезает в запрос к Gemini: до 720p и ~3,6 Мбит/с (16 с ≈ 7 МБ)."""
    video = [v for v in variants if v["h"] > 0]
    ok = [v for v in video if v["h"] <= max_h and (not v["bw"] or v["bw"] <= max_bw)]
    if ok:
        return max(ok, key=lambda v: (v["h"], -v["fps"], -v["bw"]))
    return min(video, key=lambda v: v["bw"] or 1e12) if video else (variants[0] if variants else None)


def _iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def parse_media(text: str) -> list[dict]:
    """Плейлист эфира → кусочки [{seq, dur, url, ad}]. Реклама Twitch: заголовок не «live» / Amazon,
    либо кусок попадает в интервал EXT-X-DATERANGE класса twitch-stitched-ad."""
    seq = 0
    ads: list[tuple[float, float]] = []
    out = []
    dur, title, when = 2.0, "live", None
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(_num(line.split(":", 1)[1]) or 0)
        elif line.startswith("#EXT-X-DATERANGE"):
            a = _attrs(line)
            if a.get("CLASS") == "twitch-stitched-ad" or a.get("ID", "").startswith("stitched-ad-"):
                start = _iso(a.get("START-DATE", ""))
                if start is not None:
                    ads.append((start, start + (_num(a.get("DURATION")) or 30.0)))
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            when = _iso(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            d, _, t = line[8:].partition(",")
            dur, title = _num(d) or 2.0, t.strip()
        elif line and not line.startswith("#"):
            ad = (title != "live" and title != "") or "Amazon" in title or (
                when is not None and any(a <= when < b for a, b in ads))
            out.append({"seq": seq, "dur": dur, "url": line, "ad": ad})
            seq += 1
            dur, title, when = 2.0, "live", None
    return out


def tail_segments(segs: dict[int, dict], seconds: float) -> list[dict]:
    """Самые свежие кусочки без рекламы подряд (без дыр) общей длиной ~seconds."""
    out: list[dict] = []
    total = 0.0
    for seq in sorted(segs, reverse=True):
        if out and out[-1]["seq"] != seq + 1:
            break
        out.append(segs[seq])
        total += segs[seq]["dur"]
        if total >= seconds:
            break
    return list(reversed(out))


def live_query(logins: list[str]) -> str:
    parts = [f'u{i}: user(login: "{lg}") {{ login displayName stream {{ id title createdAt viewersCount '
             f'game {{ name }} }} }}' for i, lg in enumerate(x for x in logins if LOGIN_RE.match(x))]
    return "{ " + " ".join(parts) + " }"


def parse_live(data: dict, logins: list[str]) -> dict[str, dict | None]:
    """Ответ GQL → {login: {id, title, started, viewers, game, name} | None (не в эфире)}."""
    d = (data or {}).get("data") or {}
    out: dict[str, dict | None] = {}
    for i, lg in enumerate(x for x in logins if LOGIN_RE.match(x)):
        u = d.get(f"u{i}") or {}
        st = u.get("stream") if isinstance(u, dict) else None
        if not st:
            out[lg] = None
            continue
        out[lg] = {"id": str(st.get("id") or ""), "title": _s(st.get("title"), 140),
                   "started": _iso(st.get("createdAt") or "") or time.time(),
                   "viewers": int(st.get("viewersCount") or 0), "game": _s((st.get("game") or {}).get("name"), 40),
                   "name": _s(u.get("displayName"), 40) or lg}
    return out


def clean_obs(raw: dict) -> dict:
    """Ответ ИИ → проверенный вид: действие и теги только из списков, числа — числа."""
    raw = raw if isinstance(raw, dict) else {}
    action = _s(raw.get("action"), 16).lower()
    pos = raw.get("position") if isinstance(raw.get("position"), dict) else {}
    side = _s(pos.get("side"), 8).lower()
    position = {"side": side if side in ("long", "short") else "none"}
    for k in ("entry", "size_usd", "leverage", "stop", "take", "pnl_pct"):
        position[k] = _num(pos.get(k))
    if position["leverage"] is not None and not 1 <= position["leverage"] <= 500:
        position["leverage"] = None

    def tags(items, vocab):
        out, seen = [], set()
        for it in (items or [])[:6]:
            if not isinstance(it, dict):
                continue
            tag = _s(it.get("tag"), 20).lower()
            if tag in vocab and tag not in seen:
                seen.add(tag)
                out.append({"tag": tag, "text": _s(it.get("text"), 140) or vocab[tag]})
        return out[:3]

    disc = _num(raw.get("discipline"))
    market = _s(raw.get("market"), 8).lower()
    conf = _s(raw.get("confidence"), 8).lower()
    asset = _s(raw.get("asset"), 16).upper().lstrip("$")
    contract = _s(raw.get("contract"), 50)
    from .solana import valid_address
    obs = {"trading": bool(raw.get("trading")), "summary": _s(raw.get("summary"), 300),
           "platform": _s(raw.get("platform"), 30), "asset": asset if re.fullmatch(r"[A-Z0-9]{1,15}", asset) else "",
           "contract": contract if valid_address(contract) else "",
           "market": market if market in MARKETS else "unknown", "action": action if action in ACTIONS else "none",
           "position": position, "said": _s(raw.get("said"), 180), "pluses": tags(raw.get("pluses"), PLUS_TAGS),
           "mistakes": tags(raw.get("mistakes"), MISTAKE_TAGS),
           "discipline": None if disc is None else max(0.0, min(10.0, disc)),
           "lesson": _s(raw.get("lesson"), 180), "confidence": conf if conf in ("low", "medium", "high") else "medium"}
    # то, что бот видит сам по цифрам, не полагаясь на ИИ: добавил к убыточной позиции = усреднение убытка
    pnl = position["pnl_pct"]
    if obs["action"] == "add" and pnl is not None and pnl < 0 and all(m["tag"] != "averaging_down" for m in obs["mistakes"]):
        obs["mistakes"] = (obs["mistakes"] + [{"tag": "averaging_down",
                                               "text": f"добавил к позиции в минусе ({pnl:+.1f}%)"}])[:3]
    if not obs["trading"] and obs["action"] in SIGNIFICANT:
        obs["trading"] = True
    return obs


def pick_live_model(names: list[str], dead: set) -> str | None:
    """Flash-Lite: бесплатно ~500 запросов в день (у Flash — 20, их тратят ролики YouTube)."""
    ok = [n for n in names if "flash-lite" in n and n not in dead
          and not any(x in n for x in ("image", "tts", "audio", "embed", "native"))]
    if not ok:
        return None

    def key(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return ("preview" in n or "exp" in n, -(float(m.group(1)) if m else 0.0), len(n))

    return sorted(ok, key=key)[0]


# ---------------------------------------------------------------------------
# Сеть: Twitch (видео) и Gemini (разбор)
# ---------------------------------------------------------------------------

class TwitchVideo:
    """Кусочек живого эфира Twitch: анонимно, как веб-плеер."""

    POLL_SEC = 3.0
    WAIT_SEC = 45.0  # сколько ждать, если идёт рекламная вставка

    def __init__(self, session_factory):
        self._session = session_factory
        self._media: dict[str, tuple[str, float]] = {}  # login → (плейлист выбранного качества, когда получили)
        self._ffmpeg: str | None | bool = False
        self.gql_url, self.usher_url, self.preview_url = TWITCH_GQL, TWITCH_USHER, TWITCH_PREVIEW

    def ffmpeg(self) -> str | None:
        if self._ffmpeg is False:
            path = os.environ.get("LIVE_FFMPEG", "").strip()
            if path == "0":
                self._ffmpeg = None
            elif path:
                self._ffmpeg = path
            else:
                self._ffmpeg = shutil.which("ffmpeg")
                if not self._ffmpeg:
                    try:
                        import imageio_ffmpeg  # ставится из requirements.txt: готовый ffmpeg внутри пакета
                        self._ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                    except Exception as e:
                        log.info("ffmpeg не найден (%s) — буду смотреть превью-кадры", e)
                        self._ffmpeg = None
        return self._ffmpeg or None

    async def _gql(self, payload) -> Any:
        http = await self._session()
        async with http.post(self.gql_url, json=payload, headers={"Client-ID": TWITCH_CLIENT_ID, "User-Agent": UA},
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise LiveError(f"Twitch GQL: ошибка {r.status}")
            return await r.json(content_type=None)

    async def online(self, logins: list[str]) -> dict[str, dict | None]:
        logins = [x for x in logins if LOGIN_RE.match(x)][:35]
        if not logins:
            return {}
        return parse_live(await self._gql({"query": live_query(logins)}), logins)

    async def _token(self, login: str) -> tuple[str, str]:
        data = await self._gql({"operationName": "PlaybackAccessToken",
                                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": TOKEN_HASH}},
                                "variables": {"isLive": True, "login": login, "isVod": False, "vodID": "",
                                              "playerType": "embed", "platform": "site"}})
        tok = ((data or {}).get("data") or {}).get("streamPlaybackAccessToken") if isinstance(data, dict) else None
        if not tok:  # сохранённый запрос поменяли — обычный запрос с тем же смыслом
            q = ('query($login: String!) { streamPlaybackAccessToken(channelName: $login, params: {platform: "web", '
                 'playerBackend: "mediaplayer", playerType: "embed"}) { value signature } }')
            data = await self._gql({"query": q, "variables": {"login": login}})
            tok = ((data or {}).get("data") or {}).get("streamPlaybackAccessToken") if isinstance(data, dict) else None
        if not tok or not tok.get("value"):
            raise LiveError("Twitch не дал доступ к эфиру (канал не в эфире или только для подписчиков)")
        return tok["signature"], tok["value"]

    async def _media_url(self, login: str, fresh: bool = False) -> str:
        cached = self._media.get(login)
        if cached and not fresh and time.time() - cached[1] < 15 * 60:
            return cached[0]
        sig, token = await self._token(login)
        params = {"sig": sig, "token": token, "platform": "web", "p": str(random.randint(0, 999999)),
                  "allow_source": "true", "allow_audio_only": "true", "playlist_include_framerate": "true",
                  "multigroup_video": "true", "supported_codecs": "h264", "fast_bread": "true"}
        http = await self._session()
        async with http.get(f"{self.usher_url}/api/v2/channel/hls/{login}.m3u8", params=params,
                            headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 404:
                raise LiveError("не в эфире")
            if r.status != 200:
                raise LiveError(f"Twitch: плейлист эфира — ошибка {r.status}")
            variant = pick_variant(parse_master(await r.text()))
        if not variant:
            raise LiveError("Twitch: в плейлисте нет видео")
        self._media[login] = (variant["url"], time.time())
        return variant["url"]

    async def clip(self, login: str, seconds: float = CLIP_SEC) -> tuple[bytes, str, dict]:
        """Последние ~seconds эфира → (MP4, mime, {sec, mode}). Без ffmpeg или если видео не отдали (реклама,
        Twitch поменял протокол) — превью-кадр эфира (JPEG): хуже, но взгляд не пропадает."""
        ff = self.ffmpeg()
        if not ff:
            return await self.snapshot(login), "image/jpeg", {"sec": 0, "mode": "image", "why": "нет ffmpeg"}
        try:
            return await self._video(login, ff, seconds)
        except (LiveError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            if "не в эфире" in str(e):
                raise
            log.info("Эфир %s: видео не получил (%s) — беру кадр", login, e)
            why = str(e) or type(e).__name__
        return await self.snapshot(login), "image/jpeg", {"sec": 0, "mode": "image", "why": why[:120]}

    async def _video(self, login: str, ff: str, seconds: float) -> tuple[bytes, str, dict]:
        http = await self._session()
        url = await self._media_url(login)
        got: dict[int, dict] = {}
        deadline = time.monotonic() + self.WAIT_SEC
        retried = False
        while True:
            async with http.get(url, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status in (403, 404, 410) and not retried:  # токен истёк — новый плейлист
                    retried = True
                    url = await self._media_url(login, fresh=True)
                    continue
                if r.status != 200:
                    raise LiveError(f"Twitch: эфир — ошибка {r.status}")
                text = await r.text()
            for s in parse_media(text):
                if not s["ad"]:
                    got.setdefault(s["seq"], s)
            if sum(s["dur"] for s in tail_segments(got, seconds)) >= seconds - 0.5 or time.monotonic() > deadline:
                break
            await asyncio.sleep(self.POLL_SEC)
        segs = tail_segments(got, seconds)
        if not segs:
            raise LiveError("идёт рекламная вставка Twitch — посмотрю позже")
        chunks, size = [], 0
        for s in reversed(segs):  # от свежих к старым, пока влезает в лимит
            async with http.get(s["url"], headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    break
                data = await r.read()
            if size + len(data) > MAX_CLIP_BYTES:
                break
            chunks.append(data)
            size += len(data)
        if not chunks:
            raise LiveError("не скачал видео эфира")
        n = len(chunks)
        ts = b"".join(reversed(chunks))
        del chunks
        mp4 = await remux(ff, ts)
        return mp4, "video/mp4", {"sec": round(sum(s["dur"] for s in segs[-n:])), "mode": "video"}

    async def snapshot(self, login: str) -> bytes:
        http = await self._session()
        url = self.preview_url.format(login) + f"?t={int(time.time() // 60)}"
        async with http.get(url, headers={"User-Agent": UA}, timeout=aiohttp.ClientTimeout(total=15),
                            allow_redirects=False) as r:
            if r.status != 200:  # у оффлайн-канала Twitch отдаёт редирект на заглушку
                raise LiveError("нет кадра эфира (канал не в эфире?)")
            return await r.read()


async def remux(ffmpeg: str, ts: bytes) -> bytes:
    """MPEG-TS (как у Twitch) → MP4 без перекодирования: Gemini понимает MP4, а не TS."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(ts), 90)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise LiveError("ffmpeg не успел") from None
    if proc.returncode != 0 or not out:
        raise LiveError(f"ffmpeg: {err.decode(errors='replace').strip()[:120] or 'пустой результат'}")
    return out


class LiveGemini:
    """Gemini Flash-Lite смотрит кусочек эфира (видео со звуком или кадр)."""

    def __init__(self, key: str, session_factory, model: str | None = None):
        self.key = key
        self._session = session_factory
        self.model = model or (os.environ.get("GEMINI_LIVE_MODEL") or "").strip() or None
        self.dead: set[str] = set()
        self.disabled_until = 0.0
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def _pick_model(self) -> str:
        if self.model and self.model not in self.dead:
            return self.model
        from . import mentors
        names: list[str] = []
        try:
            http = await self._session()
            async with http.get(f"{mentors.GEMINI_BASE}/models?pageSize=200", headers={"x-goog-api-key": self.key},
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status == 200:
                    data = await r.json(content_type=None)
                    names = [m["name"].removeprefix("models/") for m in data.get("models", [])
                             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        except Exception as e:
            log.info("Gemini (эфиры): список моделей: %s", e)
        model = pick_live_model(names, self.dead) or next(
            (m for m in ("gemini-flash-lite-latest", "gemini-2.5-flash-lite") if m not in self.dead), None) \
            or mentors.pick_video_model(names, self.dead)
        if not model:
            raise LiveError("Gemini: нет подходящей модели. Задай GEMINI_LIVE_MODEL")
        self.model = model
        return model

    async def analyze(self, data: bytes, mime: str, prompt: str) -> dict:
        if time.time() < self.disabled_until:
            raise LiveError("Gemini: лимит, жду")
        from . import mentors
        b64 = base64.b64encode(data)
        cfg = {"responseMimeType": "application/json", "temperature": 0.1, "maxOutputTokens": 1500}
        # тело собираем из байтов: так огромная строка base64 не копируется лишний раз через json.dumps
        body = b"".join([b'{"contents":[{"parts":[{"inline_data":{"mime_type":', json.dumps(mime).encode(),
                         b',"data":"', b64, b'"}},{"text":', json.dumps(prompt, ensure_ascii=False).encode(),
                         b'}]}],"generationConfig":', json.dumps(cfg).encode(), b"}"])
        del b64
        http = await self._session()
        for _ in range(3):
            model = await self._pick_model()
            async with http.post(f"{mentors.GEMINI_BASE}/models/{model}:generateContent", data=body,
                                 headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
                                 timeout=aiohttp.ClientTimeout(total=120)) as r:
                text = await r.text()
            if r.status == 200:
                out = json.loads(text)
                parts = ((out.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                answer = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if not answer:
                    raise LiveError("Gemini вернул пустой ответ")
                self.last_error = None
                try:
                    return mentors.parse_json(answer)
                except mentors.MentorError as e:
                    raise LiveError(str(e)) from None
            low = text.lower()
            if r.status == 429:
                self.disabled_until = time.time() + 10 * 60
                raise LiveError("Gemini: лимит бесплатного тарифа, продолжу через 10 минут")
            if r.status in (401, 403):
                raise LiveError(f"Gemini: ключ не подходит ({r.status})")
            if r.status in (400, 404) and "model" in low and ("not found" in low or "not supported" in low):
                self.dead.add(model)
                self.model = None
                continue
            raise LiveError(f"Gemini: ошибка {r.status}: {text[:160]}")
        raise LiveError("Gemini: не удалось разобрать эфир")


# ---------------------------------------------------------------------------
# Логика: сессии эфиров, действия, профиль стримера, цена ошибок
# ---------------------------------------------------------------------------

def new_state() -> dict:
    return {"on": True, "every_min": 5, "notify": "actions", "budget": {"day": "", "n": 0}, "sessions": {},
            "history": [], "focus": "", "total": 0, "last_error": None}


def new_profile() -> dict:
    return {"sessions": 0, "obs": 0, "minutes": 0, "entries": 0, "plus": {}, "miss": {}, "disc": []}


class LiveWatch:
    def __init__(self, mentors, tv: TwitchVideo | None = None, ai: LiveGemini | None = None) -> None:
        self.m = mentors
        self.tv = tv or TwitchVideo(mentors._session)
        self._ai = ai
        self.online: dict[str, dict | None] = {}
        self.online_at = 0.0
        self.lock = asyncio.Lock()
        self.events: list[dict] = []

    @property
    def ai(self) -> LiveGemini:
        key = getattr(self.m.gemini, "key", "") or ""
        if self._ai is None or (not self._ai.key and key):
            self._ai = LiveGemini(key, self.m._session)
        return self._ai

    @property
    def st(self) -> dict:
        st = self.m.st.get("live")
        if not isinstance(st, dict):
            st = {}
            self.m.st["live"] = st
        for k, v in new_state().items():
            st.setdefault(k, v)
        return st

    def profile(self, src: dict) -> dict:
        p = src.get("live")
        if not isinstance(p, dict):
            p = src["live"] = new_profile()
        for k, v in new_profile().items():
            p.setdefault(k, v)
        return p

    def budget_left(self, now: float) -> int:
        b = self.st["budget"]
        if b["day"] != _day(now):
            b["day"], b["n"] = _day(now), 0
        return LIVE_PER_DAY - b["n"]

    def sources(self) -> dict[str, dict]:
        return {s["ident"].lower(): s for s in self.m.active("twitch") if LOGIN_RE.match(s["ident"].lower())}

    # --- кто в эфире -------------------------------------------------------------

    async def refresh_online(self, now: float) -> list[dict]:
        srcs = self.sources()
        events: list[dict] = []
        try:
            self.online = await self.tv.online(sorted(srcs))
            self.online_at = now
        except Exception as e:
            self.st["last_error"] = f"кто в эфире: {e}"[:160]
            return events
        sessions = self.st["sessions"]
        for login, sess in list(sessions.items()):
            info = self.online.get(login)
            if login not in srcs or info is None or (info["id"] and sess.get("stream_id") and info["id"] != sess["stream_id"]):
                events += self.finish(login, now)
        for login, info in self.online.items():
            if info and login not in sessions:
                self._open(login, srcs[login], info, now)
                if self.st["notify"] == "all":
                    events.append({"text": f"🎥 🔴 <b>{html.escape(srcs[login]['name'])}</b> в эфире: "
                                           f"{html.escape(info['title'][:120])}. Смотрю и разбираю действия.",
                                   "buttons": [("📺 Открыть стрим", f"https://twitch.tv/{login}")]})
        return events

    def targets(self) -> list[str]:
        st = self.st
        srcs = self.sources()

        def rank(login):
            s = srcs.get(login) or {}
            return (login != st["focus"], {"star": 0, "watch": 1, "muted": 2}.get(s.get("status"), 1),
                    not s.get("trial_until"), login)

        return sorted((x for x in st["sessions"] if x in srcs), key=rank)[:MAX_STREAMS]

    # --- шаг ---------------------------------------------------------------------

    async def step(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        if not st["on"] or not self.m.st["on"] or not self.m.gemini.configured:
            return []
        events: list[dict] = []
        if now - self.online_at >= ONLINE_EVERY:
            events += await self.refresh_online(now)
        if self.budget_left(now) <= 0 or time.time() < self.ai.disabled_until:
            return events
        chat = getattr(self.m.twitch, "last_call", {}) or {}
        for login in self.targets():
            sess = st["sessions"][login]
            due = now - sess["last_at"] >= st["every_min"] * 60
            by_chat = chat.get(login, 0) > sess["last_at"] and now - sess["last_at"] >= 60
            if due or by_chat:
                try:
                    events += await self.observe(login, now, why="chat" if by_chat and not due else "")
                except (LiveError, aiohttp.ClientError, asyncio.TimeoutError) as e:  # сеть/Twitch/лимит — тихо
                    st["last_error"] = f"{sess['name']}: {e or type(e).__name__}"[:200]
                    sess["last_at"] = now - st["every_min"] * 60 + 90  # не долбить: повтор через ~1,5 мин
                except Exception as e:
                    log.exception("Эфир %s", login)
                    st["last_error"] = f"{sess['name']}: {e}"[:200]
                    sess["last_at"] = now
                break  # за шаг — один эфир: меньше памяти и лимита
        return events

    async def observe(self, login: str, now: float | None = None, why: str = "", force_event: bool = False) -> list[dict]:
        """Посмотреть эфир сейчас: кусочек видео → Gemini → действия, плюсы, ошибки."""
        now = time.time() if now is None else now
        st = self.st
        srcs = self.sources()
        src = srcs.get(login)
        sess = st["sessions"].get(login)
        if not src:
            raise LiveError("такого стримера нет в 🎓")
        if not self.ai.configured:
            raise LiveError("нужен ключ Gemini (GEMINI_API_KEY) — без него смотреть эфир нечем")
        if self.budget_left(now) <= 0:
            raise LiveError(f"на сегодня лимит просмотров исчерпан ({LIVE_PER_DAY}), продолжу завтра")
        async with self.lock:
            if sess is None:  # кнопка «посмотреть» раньше, чем бот узнал об эфире
                info = (await self.tv.online([login])).get(login)
                if not info:
                    raise LiveError("сейчас не в эфире")
                self.online[login] = info
                self._open(login, src, info, now)
                sess = st["sessions"][login]
            sess["last_at"] = now
            data, mime, meta = await self.tv.clip(login)
            prompt = self.prompt(sess, src, meta)
            try:
                raw = await self.ai.analyze(data, mime, prompt)
            finally:
                del data
            st["budget"]["n"] += 1
            st["total"] += 1
            st["last_error"] = None
            sess["mode"] = meta.get("mode", "video")
            sess["why"] = meta.get("why", "")
        obs = clean_obs(raw)
        return await self.apply(sess, src, obs, now, why=why, force_event=force_event)

    def _open(self, login: str, src: dict, info: dict, now: float) -> None:
        self.st["sessions"][login] = {"login": login, "name": src["name"], "stream_id": info["id"],
                                      "title": info["title"], "started": info["started"], "opened": now,
                                      "last_at": 0.0, "n": 0, "obs": [], "actions": [], "plus": {}, "miss": {},
                                      "disc": [], "lessons": [], "acted": {}, "counted": {}, "mode": ""}
        self.m._changed()

    def prompt(self, sess: dict, src: dict, meta: dict) -> str:
        what = f"последние {meta.get('sec') or CLIP_SEC} секунд" if meta.get("mode") == "video" else \
            "кадр (без звука)"
        before = ""
        if sess["obs"]:
            last = sess["obs"][-1]
            p = last.get("position") or {}
            pos = "нет" if p.get("side", "none") == "none" else (
                f"{p['side']} {last.get('asset') or ''} вход {_price(p.get('entry'))}, стоп {_price(p.get('stop'))}, "
                f"тейк {_price(p.get('take'))}, плечо {_price(p.get('leverage'))}, P&L {_price(p.get('pnl_pct'))}%")
            before = (f"Прошлый взгляд ({_hm(last['at'])} МСК): {last.get('summary', '')} Позиция тогда: {pos}. "
                      f"Последние действия: " + (", ".join(f"{a['action']} {a.get('asset', '')}" for a in
                                                            sess["actions"][-3:]) or "нет") + ".")
        return PROMPT.format(what=what, name=src["name"], title=sess.get("title", ""), before=before,
                             plus="|".join(PLUS_TAGS), miss="|".join(MISTAKE_TAGS))

    async def apply(self, sess: dict, src: dict, obs: dict, now: float, why: str = "",
                    force_event: bool = False) -> list[dict]:
        st = self.st
        prof = self.profile(src)
        sess["n"] += 1
        prof["obs"] += 1
        asset = obs["asset"] or (obs["contract"][:6] if obs["contract"] else "")
        new_tags = []
        for kind, items in (("plus", obs["pluses"]), ("miss", obs["mistakes"])):
            if obs["confidence"] == "low":
                continue
            for it in items:
                key = f"{kind}:{it['tag']}:{asset}"
                if now - sess["counted"].get(key, 0) < SAME_ACTION_SEC:
                    continue  # та же ошибка с той же монетой подряд — не считаем дважды
                sess["counted"][key] = now
                sess[kind][it["tag"]] = sess[kind].get(it["tag"], 0) + 1
                prof[kind][it["tag"]] = prof[kind].get(it["tag"], 0) + 1
                new_tags.append((kind, it))
        if obs["trading"] and obs["discipline"] is not None and obs["confidence"] != "low":
            sess["disc"] = (sess["disc"] + [obs["discipline"]])[-60:]
            prof["disc"] = (prof["disc"] + [obs["discipline"]])[-60:]
        if obs["lesson"] and obs["trading"] and obs["lesson"] not in sess["lessons"]:
            sess["lessons"] = (sess["lessons"] + [obs["lesson"]])[-4:]
        new_action = False
        if obs["action"] in SIGNIFICANT and obs["confidence"] != "low":
            group = "entry" if obs["action"] in ENTRY or obs["action"] == "add" else obs["action"]
            key = f"{group}:{obs['action'] if group == 'entry' else ''}:{asset}"
            if now - sess["acted"].get(key, 0) >= SAME_ACTION_SEC:
                sess["acted"][key] = now
                new_action = True
                sess["actions"] = (sess["actions"] + [{"at": now, "action": obs["action"], "asset": asset,
                                                       "said": obs["said"][:120]}])[-30:]
        call = None
        if new_action and obs["action"] in ENTRY:
            prof["entries"] += 1
            call = await self._check_entry(src, obs, now)
        for k in list(sess["acted"]):
            if now - sess["acted"][k] > 6 * 3600:
                del sess["acted"][k]
        for k in list(sess["counted"]):
            if now - sess["counted"][k] > 6 * 3600:
                del sess["counted"][k]
        sess["obs"] = (sess["obs"] + [{"at": now, "summary": obs["summary"][:200], "asset": asset,
                                       "action": obs["action"], "position": obs["position"]}])[-4:]
        self.m._changed()
        mode = st["notify"]
        fresh_miss = [it for k, it in new_tags if k == "miss"]
        show = force_event or (mode == "all" and bool(obs["trading"] or obs["summary"])) or (
            mode == "actions" and bool(new_action or fresh_miss))
        return [self.obs_event(sess, src, obs, call, why)] if show else []

    async def _check_entry(self, src: dict, obs: dict, now: float) -> dict | None:
        """Вход стримера → прогноз на проверку ценой (как в 🎓) + идея для 🏋️ тренировки на демо."""
        direction = "long" if obs["action"] == "enter_long" else "short"
        c = {"asset": obs["asset"], "contract": obs["contract"], "direction": direction, "horizon_days": 1,
             "quote": f"вживую: {obs['said'] or ACTIONS[obs['action']]}"[:140], "confidence": obs["confidence"]}
        if not (c["asset"] or c["contract"]) or (c["contract"] and direction == "short"):
            return None  # мемкоин в шорт не проверить как копию
        try:
            call = await self.m._open_call(src, c, now, video="live", kind="sol" if c["contract"] else None)
        except Exception as e:
            log.info("Вход со стрима: %s", e)
            return None
        if not call:
            return None
        call["live"] = True
        call["tags"] = [m["tag"] for m in obs["mistakes"]] + [p["tag"] for p in obs["pluses"]]
        signal = self.m.train.on_call(call, src, {"by_streamer": True}, now)
        if signal:
            self.m.pending.append(signal)
        return call

    def obs_event(self, sess: dict, src: dict, obs: dict, call: dict | None, why: str = "") -> dict:
        login = sess["login"]
        icon = {"star": "⭐", "muted": "🔇"}.get(src.get("status"), "👀")
        head = f"🎥 {icon} <b>{html.escape(src['name'])}</b> · вживую {_hm(time.time())}"
        if why == "chat":
            head += " · кинул токен в чат"
        lines = [head]
        p = obs["position"]
        what = ACTIONS.get(obs["action"], "")
        asset = obs["asset"] or (obs["contract"][:6] + "…" if obs["contract"] else "")
        where = ", ".join(x for x in (obs["platform"], MARKETS.get(obs["market"], "")) if x)
        if obs["action"] in SIGNIFICANT or p["side"] != "none":
            line = f"{what}" + (f" <b>{html.escape(asset)}</b>" if asset else "") + (
                f" ({html.escape(where)})" if where else "")
            nums = [f"{k} {_price(p[f])}" for k, f in (("вход", "entry"), ("стоп", "stop"), ("тейк", "take"))
                    if p[f] is not None]
            if p["leverage"]:
                nums.append(f"плечо {p['leverage']:g}x")
            if p["size_usd"]:
                nums.append(f"${_price(p['size_usd'])}")
            if p["pnl_pct"] is not None:
                nums.append(f"сейчас {p['pnl_pct']:+.1f}%")
            lines.append(line + (" · " + " · ".join(nums) if nums else ""))
        if obs["summary"]:
            lines.append(html.escape(obs["summary"]))
        if obs["said"]:
            lines.append(f"💬 «{html.escape(obs['said'])}»")
        if obs["pluses"]:
            lines.append("✅ " + "; ".join(f"<b>{PLUS_TAGS[x['tag']]}</b> — {html.escape(x['text'])}" for x in obs["pluses"]))
        if obs["mistakes"]:
            lines.append("❌ " + "; ".join(f"<b>{MISTAKE_TAGS[x['tag']]}</b> — {html.escape(x['text'])}"
                                          for x in obs["mistakes"]))
        tail = []
        if obs["discipline"] is not None and obs["trading"]:
            tail.append(f"🧭 дисциплина {obs['discipline']:.0f}/10")
        if obs["lesson"]:
            tail.append(f"урок: {html.escape(obs['lesson'])}")
        if tail:
            lines.append(" · ".join(tail))
        if call:
            lines.append(f"📏 Вход проверю ценой через {call['horizon_h']} ч" +
                         (" (мемкоин — как копия со стопом)" if call["kind"] == "sol" else "") + ".")
        elif obs["action"] in ENTRY:
            lines.append("📏 Проверить ценой не смогу: монеты нет на Bybit/BingX/DexScreener или не видно, какая.")
        if obs["confidence"] == "low" or sess.get("mode") == "image":
            why = f": {sess['why']}" if sess.get("mode") == "image" and sess.get("why") else ""
            lines.append("<i>Видно плохо" + (f" (только кадр без звука{html.escape(why)})" if sess.get("mode") == "image"
                                             else "") + " — выводы осторожные.</i>")
        return {"text": "\n".join(lines), "buttons": [("📺 Открыть стрим", f"https://twitch.tv/{login}")]}

    def finish(self, login: str, now: float) -> list[dict]:
        """Эфир кончился: итог в чат, профиль стримера, архив."""
        st = self.st
        sess = st["sessions"].pop(login, None)
        self.m._changed()
        if not sess:
            return []
        src = self.m.st["sources"].get(f"tw:{login}")
        minutes = max(0, int((now - (sess.get("started") or sess["opened"])) / 60))
        if src:
            prof = self.profile(src)
            prof["sessions"] += 1
            prof["minutes"] += minutes
        if not sess["n"]:
            return []
        entries = [a for a in sess["actions"] if a["action"] in ENTRY]
        exits = [a for a in sess["actions"] if a["action"] in EXITS]
        disc = round(statistics.mean(sess["disc"]), 1) if sess["disc"] else None
        rec = {"login": login, "name": sess["name"], "title": sess["title"], "at": now, "minutes": minutes,
               "n": sess["n"], "entries": len(entries), "exits": len(exits),
               "wins": sum(a["action"] == "take_profit" for a in exits),
               "losses": sum(a["action"] == "exit_loss" for a in exits),
               "assets": list(dict.fromkeys(a["asset"] for a in entries if a["asset"]))[:5],
               "plus": dict(sess["plus"]), "miss": dict(sess["miss"]), "disc": disc, "lessons": sess["lessons"][-2:]}
        st["history"] = (st["history"] + [rec])[-20:]
        if st["notify"] == "off":
            return []
        return [{"text": self.summary_text(rec), "buttons": []}]

    @staticmethod
    def summary_text(rec: dict) -> str:
        def top(d, vocab):
            return ", ".join(f"{vocab[k]} ×{v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:4] if k in vocab)

        lines = [f"🎥 <b>Итог эфира {html.escape(rec['name'])}</b> · {_dur(rec['minutes'] * 60)} · "
                 f"смотрел {rec['n']} раз"]
        if rec["entries"] or rec["exits"]:
            lines.append(f"Сделки: входов {rec['entries']}" + (f" ({', '.join(html.escape(a) for a in rec['assets'])})"
                                                                if rec["assets"] else "")
                         + f", выходов {rec['exits']}" + (f" (💰 {rec['wins']}, ✂️ {rec['losses']})"
                                                          if rec["wins"] or rec["losses"] else ""))
        else:
            lines.append("Сделок на экране не видел.")
        if rec["plus"]:
            lines.append("✅ Сильные стороны: " + top(rec["plus"], PLUS_TAGS))
        if rec["miss"]:
            lines.append("❌ Ошибки: " + top(rec["miss"], MISTAKE_TAGS))
        if rec["disc"] is not None:
            lines.append(f"🧭 Дисциплина в среднем {rec['disc']:.1f}/10")
        if rec["lessons"]:
            lines.append("🎓 Уроки: " + " · ".join(html.escape(x) for x in rec["lessons"]))
        lines.append("Его входы бот проверяет ценой — итог будет в 📈 Прогнозах и в счёте стримера.")
        return "\n".join(lines)

    # --- выводы ------------------------------------------------------------------

    def lessons(self) -> list[dict]:
        """Цена каждой ошибки и плюса: средний итог входов стримеров (проверенных ценой) с этим тегом."""
        done = [c for c in self.m.st["calls"] if c.get("live") and c.get("done") and c.get("ret") is not None]
        out = []
        for vocab, kind in ((MISTAKE_TAGS, "miss"), (PLUS_TAGS, "plus")):
            for tag, label in vocab.items():
                rets = [c["ret"] for c in done if tag in (c.get("tags") or [])]
                if len(rets) >= LESSON_MIN:
                    out.append({"tag": tag, "kind": kind, "label": label, "n": len(rets),
                                "avg": round(statistics.mean(rets), 2), "sure": len(rets) >= LESSON_SURE})
        clean = [c["ret"] for c in done if not any(t in MISTAKE_TAGS for t in (c.get("tags") or []))]
        if len(clean) >= LESSON_MIN:
            out.append({"tag": "clean", "kind": "plus", "label": "входы без ошибок", "n": len(clean),
                        "avg": round(statistics.mean(clean), 2), "sure": len(clean) >= LESSON_SURE})
        return sorted(out, key=lambda x: x["avg"])

    def card(self, src: dict) -> dict:
        p = self.profile(src) if isinstance(src.get("live"), dict) else new_profile()
        return {"name": src["name"], "login": src["ident"], "status": src.get("status", "watch"),
                "sessions": p["sessions"], "obs": p["obs"], "minutes": p["minutes"], "entries": p["entries"],
                "disc": round(statistics.mean(p["disc"]), 1) if p["disc"] else None,
                "miss": sorted(p["miss"].items(), key=lambda kv: -kv[1])[:3],
                "plus": sorted(p["plus"].items(), key=lambda kv: -kv[1])[:3]}

    def status_dict(self) -> dict:
        st = self.st
        now = time.time()
        srcs = self.sources()
        live = []
        for login, sess in st["sessions"].items():
            last = sess["obs"][-1] if sess["obs"] else None
            live.append({"login": login, "name": sess["name"], "title": sess["title"], "n": sess["n"],
                         "since": sess.get("started") or sess["opened"], "watching": login in self.targets(),
                         "last": last["summary"] if last else "", "last_at": last["at"] if last else None,
                         "actions": [{"at": a["at"], "text": f"{ACTIONS.get(a['action'], '')} {a['asset']}".strip()}
                                     for a in sess["actions"][-4:]],
                         "plus": [PLUS_TAGS[k] for k in sess["plus"]][:3], "miss": [MISTAKE_TAGS[k] for k in sess["miss"]][:3],
                         "disc": round(statistics.mean(sess["disc"]), 1) if sess["disc"] else None})
        cards = [self.card(s) for s in srcs.values() if isinstance(s.get("live"), dict) and s["live"].get("obs")]
        cards.sort(key=lambda c: -c["obs"])
        ffmpeg = self.tv.ffmpeg() if hasattr(self.tv, "ffmpeg") else None
        return {"on": st["on"], "every_min": st["every_min"], "notify": st["notify"],
                "notify_text": NOTIFY_TEXT[st["notify"]], "gemini": bool(self.m.gemini.configured),
                "model": self.ai.model if self.m.gemini.configured else None,
                "budget_left": max(0, self.budget_left(now)), "per_day": LIVE_PER_DAY, "total": st["total"],
                "video": bool(ffmpeg), "channels": len(srcs), "live": live,
                "cards": [{**c, "miss": [[MISTAKE_TAGS[k], v] for k, v in c["miss"]],
                           "plus": [[PLUS_TAGS[k], v] for k, v in c["plus"]]} for c in cards[:8]],
                "history": list(reversed(st["history"][-5:])), "lessons": self.lessons(),
                "error": st["last_error"], "focus": st["focus"]}

    def ai_context(self) -> dict:
        d = self.status_dict()
        return {"how": "🎥 бот смотрит эфиры Twitch-трейдеров (видео+звук через Gemini) и разбирает действия: входы, "
                       "выходы, стопы, ошибки и плюсы; входы проверяет ценой",
                "live_now": [{"name": x["name"], "last": x["last"][:160], "mistakes": x["miss"], "pluses": x["plus"]}
                             for x in d["live"]],
                "streamer_habits": [{"name": c["name"], "discipline": c["disc"], "top_mistakes": c["miss"],
                                     "top_pluses": c["plus"], "entries_seen": c["entries"]} for c in d["cards"][:6]],
                "cost_of_mistakes_checked_by_price": [{"what": x["label"], "entries": x["n"], "avg_pct": x["avg"]}
                                                      for x in d["lessons"]]}

    def text(self) -> str:
        d = self.status_dict()
        st = self.st
        if not d["gemini"]:
            mode = "⚠️ Нужен ключ Gemini (GEMINI_API_KEY) — без него смотреть эфиры нечем"
        else:
            mode = (f"{'🟢 смотрю' if d['on'] and self.m.st['on'] else '⏸ выключено'} · раз в {d['every_min']} мин "
                    f"по {CLIP_SEC} с {'видео со звуком' if d['video'] else 'кадр (нет ffmpeg)'} · осталось сегодня "
                    f"{d['budget_left']} из {d['per_day']}")
        lines = ["🎥 <b>Стримы вживую</b> — бот смотрит, что стример делает на экране, и разбирает его действия",
                 mode, f"Сообщать: {d['notify_text']} · стримеров Twitch в 🎓: {d['channels']}"]
        if d["live"]:
            lines.append("\n<b>Сейчас в эфире</b>:")
            for x in d["live"]:
                eye = "👁" if x["watching"] else "⏳"
                row = f"{eye} <b>{html.escape(x['name'])}</b> — {_dur(time.time() - x['since'])}, смотрел {x['n']} раз"
                if x["disc"] is not None:
                    row += f", дисциплина {x['disc']:.1f}/10"
                if x["last"]:
                    row += f"\n   {_hm(x['last_at'])}: {html.escape(x['last'][:160])}"
                if x["actions"]:
                    row += "\n   " + " · ".join(f"{_hm(a['at'])} {html.escape(a['text'])}" for a in x["actions"][-3:])
                lines.append(row)
            if len(d["live"]) > MAX_STREAMS:
                lines.append(f"<i>Одновременно смотрю {MAX_STREAMS} эфира (⭐ и выбранный — первыми).</i>")
        else:
            lines.append("\nСейчас никто из стримеров не в эфире — проверяю каждые 2 минуты.")
        if d["cards"]:
            lines.append("\n<b>Привычки стримеров</b> (по увиденному):")
            for c in d["cards"][:6]:
                row = f"• <b>{html.escape(c['name'])}</b>: эфиров {c['sessions']}, взглядов {c['obs']}, входов {c['entries']}"
                if c["disc"] is not None:
                    row += f", дисциплина {c['disc']:.1f}/10"
                if c["plus"]:
                    row += "\n   ✅ " + ", ".join(f"{k} ×{v}" for k, v in c["plus"])
                if c["miss"]:
                    row += "\n   ❌ " + ", ".join(f"{k} ×{v}" for k, v in c["miss"])
                lines.append(row)
        if d["lessons"]:
            lines.append("\n<b>Сколько стоит ошибка</b> (входы стримеров, проверенные ценой):")
            for x in d["lessons"][:6]:
                mark = "❌" if x["kind"] == "miss" else "✅"
                lines.append(f"{mark} {x['label']}: {x['n']} вх., в среднем <b>{x['avg']:+.1f}%</b>"
                             + ("" if x["sure"] else " <i>(мало данных)</i>"))
        else:
            lines.append(f"\n📏 Каждый увиденный вход бот проверяет ценой. От {LESSON_MIN} проверенных входов здесь "
                         "появится «цена» каждой ошибки на цифрах.")
        if d["history"]:
            h = d["history"][0]
            lines.append(f"\nПоследний итог: <b>{html.escape(h['name'])}</b>, {_dur(h['minutes'] * 60)}, входов "
                         f"{h['entries']}" + (f", дисциплина {h['disc']:.1f}/10" if h["disc"] is not None else ""))
        if st["last_error"]:
            lines.append(f"⚠️ {html.escape(st['last_error'])}")
        lines.append("\nДобавить стримера: <code>/mentors add twitch.tv/ник</code> · смотреть сразу: "
                     "<code>/stream ник</code>")
        return "\n".join(lines)


__all__ = ["LiveWatch", "LiveGemini", "TwitchVideo", "LiveError", "clean_obs", "parse_master", "parse_media",
           "pick_variant", "parse_live", "live_query", "pick_live_model", "tail_segments", "remux", "ACTIONS",
           "PLUS_TAGS", "MISTAKE_TAGS", "NOTIFY_MODES", "INTERVALS", "TICK_SEC", "LIVE_PER_DAY", "CLIP_SEC"]
