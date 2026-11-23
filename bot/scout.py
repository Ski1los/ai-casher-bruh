"""🔭 Разведчик: бот сам ищет трейдеров на YouTube и Twitch, отсеивает фейки и оставляет только тех, чьи прогнозы
окупаются на проверке ценой.

Как это работает:
  1. ПОИСК (раз в сутки или по кнопке). Источники, по очереди и без платных ключей:
       * Gemini с поиском Google — просим найти активных трейдеров, которые дают конкретные проверяемые прогнозы
         (монета, направление, срок) и не продают «VIP-сигналы», казино и пресейлы;
       * поиск YouTube по каналам (страница поиска, без ключа API);
       * категория Crypto на Twitch — кто сейчас стримит трейдинг мемкоинов.
  2. ФЕЙК-ФИЛЬТР — до того как тратить на канал лимит Gemini:
       * накрученные подписчики: подписчиков много, а просмотров у роликов почти нет;
       * кликбейт в названиях: «100x», «гарантированно», «миллионер», «срочно», 🚨🚨;
       * в описаниях — казино (Stake, Rollbit, BC.Game), платные сигнальные группы, пресейлы, промокоды бирж;
       * канал заброшен (нет роликов 3 недели) или почти не про крипту;
       * на Twitch — гемблинг и «раздачи» в названии эфира.
  3. ИСПЫТАТЕЛЬНЫЙ СРОК. Прошедшие фильтр попадают в «🔭 на испытании» (не больше 5 каналов YouTube и 5 Twitch
     сразу, и ролики испытуемых разбираются только из остатка дневного лимита). Каждый их прогноз бот проверяет
     ценой, как у остальных трейдеров.
  4. ИТОГ:
       ⭐ средний результат прогнозов от +2% с поправкой на везение (от 8 проверок) — остаётся, сигналы приходят тебе;
       🔇 прогнозы теряют деньги — убираю и больше не беру;
       🎟 реклама в половине роликов, 🎪 сплошной хайп, нет проверяемых прогнозов за срок — убираю.
     Отсеянных бот запоминает и повторно не берёт.

Честно: заранее прибыльность трейдера не знает никто, а «топ трейдеров» в интернете часто реклама. Поэтому бот
не верит ни подписчикам, ни отзывам — только проверке ценой. Хороших трейдеров мало: нормально, если из 10
найденных останется 1–2.
"""

from __future__ import annotations

import html
import json
import logging
import re
import statistics
import time
import urllib.parse

import aiohttp

log = logging.getLogger(__name__)

DISCOVER_EVERY = 24 * 3600
MAX_TRIAL = {"youtube": 5, "twitch": 5}
ADD_PER_RUN = {"youtube": 3, "twitch": 3}
TRIAL_DAYS = {"youtube": 21, "twitch": 10}
NO_CALLS_AFTER_VIDEOS = 4       # разобрал 4 ролика, а проверяемых прогнозов нет — канал бесполезен для проверки
TWITCH_NO_CALLS_DAYS = 6        # Twitch: за 6 дней в чате ни одного адреса токена — проверять нечего
FAKE_REJECT = 6                 # фейк-риск от 6 из 10 — не берём
HYPE_REJECT = 7.0               # средний хайп роликов от 7 из 10 — убираем
ADS_REJECT = 0.5                # реклама в половине роликов — убираем
MIN_SUBS = 5_000
MIN_TWITCH_VIEWERS = 15
DEAD_DAYS = 21
TRIAL_RSS_EVERY = 3 * 3600      # испытуемых проверяем реже, чтобы не тратить лимит
TRIAL_BUDGET_RESERVE = 5        # из дневного лимита роликов 5 всегда остаются проверенным трейдерам
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # публичный id веб-сайта Twitch (без логина)

YT_QUERIES = (
    "crypto trading live", "memecoin trading solana", "bitcoin technical analysis today",
    "altcoin trade setup", "крипто трейдинг", "мемкоины solana трейдинг", "crypto day trading strategy",
    "solana memecoins live trading",
)
CRYPTO_WORDS = re.compile(
    r"(bitcoin|\bbtc\b|crypto|\beth\b|ethereum|solana|\bsol\b|altcoin|memecoin|meme coin|\bxrp\b|doge|pump|"
    r"trading|trade|chart|bull|bear|крипт|биткоин|биток|альт|мемкоин|трейд|эфир|солан|токен|token|defi|futures)", re.I)
CLICKBAIT = re.compile(
    r"(\b\d{3,5}x\b|\bx\d{3,5}\b|guarantee|гарантир|millionaire|миллионер|get rich|разбогате|last chance|последний шанс|"
    r"urgent|срочно|don'?t miss|не пропусти|secret|секрет|insane|бесплатн\w* деньги|free money|"
    r"🚨\s*🚨|💰\s*💰|🔥\s*🔥\s*🔥)", re.I)
SCAM_DESC = re.compile(
    r"(stake\.(?:com|us)|rollbit|bc\.game|roobet|shuffle\.com|duelbits|casino|казино|слоты|\bslots?\b|"
    r"vip (?:group|signals|channel)|vip[- ]?сигнал|платн\w* (?:сигнал|канал|групп)|paid (?:group|signals)|"
    r"presale|пресейл|giveaway|розыгрыш|раздача|airdrop now|send \d+ (?:eth|sol|btc))", re.I)
REFERRAL = re.compile(r"(\bref(?:erral)?\b|invite|promo ?code|промокод|bonus|бонус|affiliate|\?code=|/register\?)", re.I)
TWITCH_BAD = re.compile(r"(casino|slots?|stake|rollbit|bc\.game|roobet|gambl|giveaway|раздача|free sol|airdrop)", re.I)
TWITCH_GOOD = re.compile(r"(meme|solana|\bsol\b|pump|axiom|gmgn|photon|bullx|trench|calls?|trading|degen|"
                         r"мем|трейд|charts?)", re.I)

GEMINI_PROMPT = """\
Найди через поиск Google {n} активных крипто-трейдеров, у которых можно проверить прогнозы ценой: на YouTube \
(каналы с разборами рынка и конкретными сетапами) и на Twitch (стримеры, которые вживую торгуют мемкоины Solana и \
показывают адреса токенов в чате). Нужны те, кто:
- дают КОНКРЕТНЫЕ прогнозы: монета, направление, срок (или адрес токена);
- активны в последний месяц;
- НЕ продают платные VIP-сигналы, курсы «как разбогатеть», не рекламируют казино, пресейлы и не делают раздачи.
Возьми и англоязычных, и русскоязычных. Не повторяй: {known}.
Ответ СТРОГО JSON-массивом без пояснений:
[{{"platform": "youtube|twitch", "handle": "для YouTube — @имя канала, для Twitch — логин", "name": "как называется",
   "why": "кратко по-русски, чем полезен"}}]"""


# ---------------------------------------------------------------------------
# Разбор страниц и ответов
# ---------------------------------------------------------------------------

def parse_count(text: str) -> int | None:
    """«1.2M subscribers», «523K», «12,3 тыс. подписчиков», «1,5 млн» → число."""
    t = (text or "").lower().replace("\xa0", " ")
    m = re.search(r"([\d][\d.,\s]*)\s*(k|m|b|тыс|млн|млрд)?", t)
    if not m:
        return None
    num = m.group(1).strip().replace(" ", "")
    mult = {"k": 1e3, "тыс": 1e3, "m": 1e6, "млн": 1e6, "b": 1e9, "млрд": 1e9}.get(m.group(2) or "", 1)
    if mult > 1:
        num = num.replace(",", ".")
    else:
        num = num.replace(",", "").replace(".", "")
    try:
        return int(float(num) * mult)
    except ValueError:
        return None


def parse_yt_search(page: str) -> list[dict]:
    """Страница поиска YouTube (фильтр «каналы») → [{channel_id, name, subs}]."""
    out, seen = [], set()
    for m in re.finditer(r'"channelRenderer":\{"channelId":"(UC[\w-]{22})"', page):
        cid = m.group(1)
        if cid in seen:
            continue
        seen.add(cid)
        chunk = page[m.end(): m.end() + 4000]
        name = re.search(r'"title":\{"simpleText":"((?:[^"\\]|\\.)*)"', chunk)
        subs = None
        for sm in re.finditer(r'"simpleText":"([^"]*(?:subscriber|подписчик)[^"]*)"', chunk):
            subs = parse_count(sm.group(1))
            break
        try:
            title = json.loads(f'"{name.group(1)}"') if name else cid
        except ValueError:
            title = name.group(1) if name else cid
        out.append({"channel_id": cid, "name": title[:40], "subs": subs})
    return out


def parse_twitch_streams(data) -> list[dict]:
    """Ответ Twitch GQL (категория Crypto) → [{login, name, viewers, title, followers}]."""
    try:
        edges = data["data"]["game"]["streams"]["edges"]
    except (KeyError, TypeError):
        return []
    out = []
    for e in edges or []:
        n = (e or {}).get("node") or {}
        b = n.get("broadcaster") or {}
        if not b.get("login"):
            continue
        out.append({"login": b["login"].lower(), "name": b.get("displayName") or b["login"],
                    "viewers": int(n.get("viewersCount") or 0), "title": n.get("title") or "",
                    "followers": ((b.get("followers") or {}).get("totalCount"))})
    return out


def parse_gemini_candidates(text: str) -> list[dict]:
    """Ответ Gemini → [{platform, handle, name, why}] (лишнее и мусор отбрасываем)."""
    t = (text or "").strip()
    m = re.search(r"\[.*\]", t, re.S)
    if not m:
        return []
    try:
        raw = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        platform = str(r.get("platform") or "").lower().strip()
        handle = str(r.get("handle") or "").strip()
        if platform == "youtube":
            m2 = re.search(r"@([\w.\-]{2,40})", handle) or re.fullmatch(r"([\w.\-]{2,40})", handle)
            if not m2:
                continue
            handle = m2.group(1)
        elif platform == "twitch":
            m2 = re.search(r"(?:twitch\.tv/)?([A-Za-z0-9_]{3,25})$", handle)
            if not m2:
                continue
            handle = m2.group(1).lower()
        else:
            continue
        out.append({"platform": platform, "handle": handle, "name": str(r.get("name") or handle)[:40],
                    "why": str(r.get("why") or "")[:140]})
    return out[:20]


# ---------------------------------------------------------------------------
# Фейк-фильтр
# ---------------------------------------------------------------------------

def vet_youtube(videos: list[dict], subs: int | None, now: float) -> dict:
    """Проверка канала до испытания. → {risk 0..10, reasons, ok, relevant, avg_views}."""
    reasons, risk = [], 0
    long_videos = [v for v in videos if not v.get("short")]
    if not videos:
        return {"risk": 10, "reasons": ["нет роликов"], "ok": False, "relevant": 0.0, "avg_views": None}
    newest = max(v.get("published") or 0 for v in videos)
    if now - newest > DEAD_DAYS * 86400:
        reasons.append(f"новых роликов нет {int((now - newest) / 86400)} дн.")
        risk += 10
    if not long_videos:
        reasons.append("только короткие ролики — прогнозов в них нет")
        risk += 6
    titles = [v.get("title") or "" for v in videos]
    relevant = sum(bool(CRYPTO_WORDS.search(t)) for t in titles) / len(titles)
    if relevant < 0.4:
        reasons.append("почти не про крипту")
        risk += 6
    bait = sum(bool(CLICKBAIT.search(t)) for t in titles) / len(titles)
    if bait >= 0.5:
        reasons.append(f"кликбейт в {bait:.0%} названий")
        risk += 4
    elif bait >= 0.25:
        risk += 2
    descs = [v.get("description") or "" for v in videos]
    scam = sum(bool(SCAM_DESC.search(d)) for d in descs) / len(descs)
    if scam >= 0.3:  # в половине роликов казино/VIP-сигналы — это бизнес на зрителях, а не на трейдинге
        reasons.append("в описаниях казино, платные сигналы, пресейлы или раздачи")
        risk += FAKE_REJECT if scam >= 0.5 else 4
    refs = sum(bool(REFERRAL.search(d)) for d in descs) / len(descs)
    if refs >= 0.8:
        reasons.append("рефералки бирж в каждом ролике")
        risk += 2
    views = [v["views"] for v in videos if v.get("views") is not None]
    avg_views = statistics.median(views) if views else None
    if subs and subs < MIN_SUBS:
        reasons.append(f"мало подписчиков ({subs})")
        risk += 3
    if subs and avg_views is not None and subs >= 50_000 and avg_views < subs * 0.002:
        reasons.append(f"похоже на накрутку: {subs} подписчиков, а у ролика в среднем {int(avg_views)} просмотров")
        risk += 6
    risk = min(10, risk)
    return {"risk": risk, "reasons": reasons, "ok": risk < FAKE_REJECT, "relevant": round(relevant, 2),
            "avg_views": int(avg_views) if avg_views is not None else None}


def vet_twitch(stream: dict) -> dict:
    reasons, risk = [], 0
    title = stream.get("title") or ""
    if TWITCH_BAD.search(title):
        reasons.append("гемблинг или раздачи в названии эфира")
        risk += 8
    if stream.get("viewers") is not None and stream["viewers"] < MIN_TWITCH_VIEWERS:
        reasons.append(f"почти нет зрителей ({stream['viewers']})")
        risk += 4
    if title and not TWITCH_GOOD.search(title):
        reasons.append("эфир не про трейдинг мемкоинов")
        risk += 3
    risk = min(10, risk)
    return {"risk": risk, "reasons": reasons, "ok": risk < FAKE_REJECT}


# ---------------------------------------------------------------------------
# Разведчик
# ---------------------------------------------------------------------------

def new_state() -> dict:
    return {"on": True, "last": 0.0, "q": 0, "rejected": {}, "log": [], "found": 0, "kept": 0, "dropped": 0,
            "last_error": None}


class Scout:
    def __init__(self, mentors) -> None:
        self.m = mentors
        self.busy = False

    @property
    def st(self) -> dict:
        ms = self.m.st
        st = ms.get("scout")
        if not isinstance(st, dict):
            st = ms["scout"] = {}
        for k, v in new_state().items():
            st.setdefault(k, v)
        return st

    def _log(self, text: str, now: float) -> None:
        self.st["log"].append({"at": now, "text": text[:300]})
        del self.st["log"][:-20]
        self.m._changed()

    def trial(self, kind: str | None = None) -> list[dict]:
        return [s for s in self.m.active(kind) if s.get("auto") and s.get("trial_until")]

    def known_ids(self) -> set[str]:
        return set(self.m.st["sources"]) | set(self.st["rejected"])

    def _reject(self, sid: str, name: str, why: str, now: float) -> None:
        self.st["rejected"][sid] = {"name": name[:40], "why": why[:160], "at": now}
        if len(self.st["rejected"]) > 300:  # храним последние 300 отказов
            for k in sorted(self.st["rejected"], key=lambda k: self.st["rejected"][k]["at"])[:50]:
                del self.st["rejected"][k]

    # --- сеть -------------------------------------------------------------------------

    async def _gemini_candidates(self, n: int) -> list[dict]:
        g = self.m.gemini
        if not g.configured or time.time() < getattr(g, "disabled_until", 0):
            return []
        known = sorted({s["name"] for s in self.m.active()} | {r["name"] for r in self.st["rejected"].values()})[:40]
        prompt = GEMINI_PROMPT.format(n=n, known=", ".join(known) or "—")
        http = await self.m._session()
        model = await g._pick_model()
        for tools in ([{"google_search": {}}], None):
            body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.4,
                                                                                    "maxOutputTokens": 4000}}
            if tools:
                body["tools"] = tools
            async with http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                 json=body, headers={"x-goog-api-key": g.key},
                                 timeout=aiohttp.ClientTimeout(total=90)) as r:
                text = await r.text()
                if r.status == 200:
                    data = json.loads(text)
                    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                    return parse_gemini_candidates("".join(p.get("text", "") for p in parts if not p.get("thought")))
                if r.status == 400 and tools:  # модель не умеет поиск Google — спросим без него
                    continue
                raise RuntimeError(f"Gemini {r.status}: {text[:120]}")
        return []

    async def _yt_search(self, query: str) -> list[dict]:
        url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query) + "&sp=EgIQAg%253D%253D")
        return parse_yt_search(await self.m.youtube._get(url))

    async def _twitch_streams(self) -> list[dict]:
        http = await self.m._session()
        q = ('query{game(name:"Crypto"){streams(first:40){edges{node{title viewersCount '
             'broadcaster{login displayName followers{totalCount}}}}}}}')
        async with http.post(TWITCH_GQL, json={"query": q}, headers={"Client-Id": TWITCH_CLIENT_ID},
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise RuntimeError(f"Twitch {r.status}")
            return parse_twitch_streams(await r.json(content_type=None))

    # --- поиск ------------------------------------------------------------------------

    async def discover(self, now: float | None = None, force: bool = False) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        if self.busy or (not force and (not st["on"] or now - st["last"] < DISCOVER_EVERY)):
            return []
        free = {k: MAX_TRIAL[k] - len(self.trial(k)) for k in MAX_TRIAL}
        if not force and free["youtube"] <= 0 and free["twitch"] <= 0:
            st["last"] = now
            return []
        self.busy = True
        st["last"] = now
        added: list[dict] = []
        notes: list[str] = []
        try:
            want = {k: min(ADD_PER_RUN[k], max(0, free[k])) for k in free}
            # 1) Gemini + поиск Google
            yt_cands: list[dict] = []
            tw_cands: list[dict] = []
            try:
                for c in await self._gemini_candidates(12):
                    (yt_cands if c["platform"] == "youtube" else tw_cands).append({**c, "via": "Gemini + Google"})
            except Exception as e:
                notes.append(f"Gemini: {str(e)[:100]}")
            # 2) поиск YouTube (без ключа)
            if want["youtube"] > 0:
                q = YT_QUERIES[st["q"] % len(YT_QUERIES)]
                st["q"] += 1
                try:
                    for c in (await self._yt_search(q))[:12]:
                        yt_cands.append({"platform": "youtube", "channel_id": c["channel_id"], "name": c["name"],
                                         "subs": c["subs"], "why": f"нашёл по запросу «{q}»", "via": "поиск YouTube"})
                except Exception as e:
                    notes.append(f"поиск YouTube: {str(e)[:100]}")
            # 3) Twitch, категория Crypto
            streams: dict[str, dict] = {}
            if want["twitch"] > 0:
                try:
                    for s in await self._twitch_streams():
                        streams[s["login"]] = s
                        tw_cands.append({"platform": "twitch", "handle": s["login"], "name": s["name"],
                                         "why": f"стримит: {s['title'][:80]}", "via": "Twitch Crypto", "stream": s})
                except Exception as e:
                    notes.append(f"Twitch: {str(e)[:100]}")
            added += await self._take_youtube(yt_cands, want["youtube"], now)
            added += self._take_twitch(tw_cands, want["twitch"], now)
        finally:
            self.busy = False
        st["found"] += len(added)
        st["last_error"] = "; ".join(notes) if notes and not added else None
        if not added:
            return []
        await self.m.twitch.set_channels({s["ident"] for s in self.m.active("twitch")})
        names = ", ".join(f"{'▶️' if s['kind'] == 'youtube' else '📺'} {html.escape(s['name'])}" for s in added)
        text = (f"🔭 <b>Разведчик нашёл трейдеров</b>: {names}.\nОни прошли фейк-фильтр и теперь на испытании: бот "
                "проверит их прогнозы ценой. Окупаются — станут ⭐ и начнут присылать сигналы, теряют или "
                "рекламируют — уберу сам.")
        self._log(re.sub(r"<[^>]+>", "", text), now)
        return [{"text": text, "buttons": [("🔭 Разведчик", "mn:scout")]}]

    async def _take_youtube(self, cands: list[dict], want: int, now: float) -> list[dict]:
        added = []
        known = self.known_ids()
        for c in cands:
            if len(added) >= want:
                break
            try:
                cid = c.get("channel_id")
                name = c["name"]
                if not cid:
                    cid, name = await self.m.youtube.resolve("yt_handle", c["handle"])
                sid = f"yt:{cid}"
                if sid in known:
                    continue
                known.add(sid)
                feed_name, videos = await self.m.youtube.feed(cid)
                name = feed_name or name
                v = vet_youtube(videos, c.get("subs"), now)
            except Exception as e:
                log.info("Разведчик, YouTube %s: %s", c.get("name"), e)
                continue
            if not v["ok"]:
                self._reject(sid, name, "фейк-фильтр: " + "; ".join(v["reasons"]), now)
                continue
            src = self.m._add("youtube", cid, name, note=f"🔭 {c.get('why') or c['via']}"[:120], user=False)
            self._mark(src, c["via"], v["risk"], now, "youtube")
            src["seen"] = [x["id"] for x in videos][-40:] or ["-"]  # прошлые ролики не разбираем: судим по новым
            added.append(src)
        return added

    def _take_twitch(self, cands: list[dict], want: int, now: float) -> list[dict]:
        added = []
        known = self.known_ids()
        for c in sorted(cands, key=lambda c: -int(bool(TWITCH_GOOD.search((c.get("stream") or {}).get("title", "")))
                                                    ) * 1000 - (c.get("stream") or {}).get("viewers", 0)):
            if len(added) >= want:
                break
            sid = f"tw:{c['handle']}"
            if sid in known:
                continue
            known.add(sid)
            v = vet_twitch(c["stream"]) if c.get("stream") else {"risk": 0, "reasons": [], "ok": True}
            if not v["ok"]:
                self._reject(sid, c["name"], "фейк-фильтр: " + "; ".join(v["reasons"]), now)
                continue
            src = self.m._add("twitch", c["handle"], c["name"], note=f"🔭 {c.get('why') or c['via']}"[:120], user=False)
            self._mark(src, c["via"], v["risk"], now, "twitch")
            added.append(src)
        return added

    def _mark(self, src: dict, via: str, risk: int, now: float, kind: str) -> None:
        src.update(auto=True, via=via, fake_risk=risk, trial_until=now + TRIAL_DAYS[kind] * 86400, found_at=now)

    # --- итоги испытания ---------------------------------------------------------------

    def review(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        events = []
        for src in [s for s in self.m.active() if s.get("auto")]:
            why = self._verdict(src, now)
            if why == "star":
                if src.get("trial_until"):
                    src["trial_until"] = None
                    self.st["kept"] += 1
                    text = (f"🔭⭐ <b>{html.escape(src['name'])}</b> прошёл испытание: {self.m.score_text(src)}. "
                            "Оставляю — его прогнозы теперь приходят сигналом.")
                    self._log(re.sub(r"<[^>]+>", "", text), now)
                    events.append({"text": text, "buttons": [("🎓 Трейдеры", "mn:list")]})
                continue
            if not why:
                continue
            src["removed"] = True
            self._reject(src["id"], src["name"], why, now)
            self.st["dropped"] += 1
            self.m._changed()
            text = f"🔭 Убрал <b>{html.escape(src['name'])}</b>: {html.escape(why)}."
            self._log(re.sub(r"<[^>]+>", "", text), now)
            if self.m.st["notify"]:
                events.append({"text": text, "buttons": []})
        return events

    def _verdict(self, src: dict, now: float) -> str | None:
        """'star' — оставить; строка — причина убрать; None — ещё рано судить."""
        if src["status"] == "star":
            return "star"
        if src["status"] == "muted":
            return f"прогнозы теряют деньги ({self.m.score_text(src)})"
        if src["videos"] >= 4 and src["sponsored"] / src["videos"] >= ADS_REJECT:
            return f"реклама в {src['sponsored']} из {src['videos']} роликов"
        if len(src["hype"]) >= 4 and statistics.mean(src["hype"]) >= HYPE_REJECT:
            return f"сплошной хайп (в среднем {statistics.mean(src['hype']):.0f}/10) без аргументов"
        if src["kind"] == "youtube" and src["videos"] >= NO_CALLS_AFTER_VIDEOS and not src["calls"]:
            return f"в {src['videos']} роликах ни одного проверяемого прогноза"
        found = src.get("found_at") or src.get("added") or now
        if src["kind"] == "twitch" and not src["calls"] and now - found >= TWITCH_NO_CALLS_DAYS * 86400:
            return f"за {TWITCH_NO_CALLS_DAYS} дней в чате ни одного адреса токена — проверять нечего"
        until = src.get("trial_until")
        if until and now >= until:
            if len(src["rets"]) >= 3 and sum(src["rets"]) > 0:
                src["trial_until"] = now + 7 * 86400  # в плюсе, но проверок мало — даём ещё неделю
                return None
            return f"испытание закончилось без доказанного плюса ({self.m.score_text(src)})"
        return None

    # --- для разбора роликов ------------------------------------------------------------

    def can_watch(self, src: dict, now: float, budget_left: int) -> bool:
        """Испытуемых смотрим реже и только из остатка дневного лимита."""
        if not (src.get("auto") and src.get("trial_until")):
            return True
        return budget_left > TRIAL_BUDGET_RESERVE and now - src["checked_at"] >= TRIAL_RSS_EVERY

    async def step(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        events = self.review(now)
        try:
            events += await self.discover(now)
        except Exception as e:
            log.info("Разведчик: %s", e)
            self.st["last_error"] = str(e)[:160]
        return events

    # --- показ ----------------------------------------------------------------------------

    def status_dict(self) -> dict:
        st = self.st
        trial = [{"id": s["id"], "name": s["name"], "kind": s["kind"], "via": s.get("via"), "checked": len(s["rets"]),
                  "avg": round(sum(s["rets"]) / (len(s["rets"]) + 5), 2) if s["rets"] else 0.0, "calls": s["calls"],
                  "videos": s["videos"], "until": s.get("trial_until")} for s in self.trial()]
        kept = [s["name"] for s in self.m.active() if s.get("auto") and not s.get("trial_until") and s["status"] == "star"]
        rejected = sorted(st["rejected"].values(), key=lambda r: -r["at"])[:8]
        return {"on": st["on"], "last": st["last"], "next": st["last"] + DISCOVER_EVERY if st["last"] else None,
                "trial": trial, "kept": kept, "rejected": rejected, "rejected_total": len(st["rejected"]),
                "found": st["found"], "dropped": st["dropped"],
                "stars": st["kept"], "log": list(reversed(st["log"][-6:])), "error": st["last_error"],
                "max_trial": dict(MAX_TRIAL), "gemini": self.m.gemini.configured}

    def text(self) -> str:
        d = self.status_dict()
        lines = ["🔭 <b>Разведчик</b> — сам ищет трейдеров на YouTube и Twitch, отсеивает фейки и проверяет ценой",
                 f"{'🟢 ищу раз в сутки' if d['on'] else '⏸ выключен'} · взял на испытание {d['found']}, прошли "
                 f"{d['stars']}, отсеяно {d['rejected_total']}"]
        if not d["gemini"]:
            lines.append("ℹ️ Без ключа Gemini ищу только поиском YouTube и в категории Crypto на Twitch.")
        if d["trial"]:
            lines.append(f"\n<b>На испытании</b> (до {d['max_trial']['youtube']} YouTube и {d['max_trial']['twitch']} Twitch):")
            for t in d["trial"]:
                where = "▶️" if t["kind"] == "youtube" else "📺"
                score = (f"{t['checked']} пров., {t['avg']:+.1f}%" if t["checked"] else
                         f"роликов {t['videos']}, прогнозов {t['calls']}" if t["kind"] == "youtube" else f"токенов {t['calls']}")
                left = max(0, int(((t["until"] or 0) - time.time()) / 86400))
                lines.append(f"🔭{where} <b>{html.escape(t['name'])}</b> — {score} · ещё {left} дн. · {html.escape(t['via'] or '')}")
        else:
            lines.append("\nНа испытании пока никого." + (" Поиск — по кнопке или сам через сутки." if d["on"] else ""))
        if d["kept"]:
            lines.append("⭐ Нашёл и оставил: " + ", ".join(html.escape(x) for x in d["kept"]))
        if d["rejected"]:
            lines.append("\n<b>Отсеял</b>:")
            for r in d["rejected"][:6]:
                lines.append(f"✖️ {html.escape(r['name'])} — {html.escape(r['why'])}")
        if d["error"]:
            lines.append(f"\n⚠️ {html.escape(d['error'])}")
        lines.append("\nКак отсеиваю: накрутка подписчиков (много подписчиков — мало просмотров), кликбейт «100x», казино, "
                     "платные VIP-сигналы, пресейлы, заброшенные каналы. Остальное решает проверка ценой: ⭐ — от 8 "
                     "проверенных прогнозов и в среднем от +2% с поправкой на везение. Хороших мало — это нормально.")
        return "\n".join(lines)
