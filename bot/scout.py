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
  2½. 📜 ИСТОРИЯ ПРОГНОЗОВ — до испытания. Из названий последних роликов (Gemini, а без него по словам: «Bitcoin to
     150K», «SOL crash») берём прошлые прогнозы и проверяем на настоящих дневных свечах Bybit/BingX: вход — открытие
     следующего дня после ролика, выход — через 3 дня, минус комиссии. История в минусе — не берём; из прошедших
     берём лучших. Это подсказка, а не доказательство: названия неточны, а в растущий рынок «быки» выглядят умнее.
     Снежный ком: кого упоминают в описаниях ⭐ трейдеры — тоже кандидаты. Самообучение: бот считает, какие источники
     и запросы дали ⭐, и ищет там чаще.
  3. ИСПЫТАТЕЛЬНЫЙ СРОК. Прошедшие фильтр попадают в «🔭 на испытании» (не больше 5 каналов YouTube и 5 Twitch
     сразу, и ролики испытуемых разбираются только из остатка дневного лимита). Каждый их прогноз бот проверяет
     ценой, как у остальных трейдеров.
  4. ИТОГ:
       ⭐ средний результат прогнозов от +2% с поправкой на везение (от 8 проверок) — остаётся, сигналы приходят тебе;
       🔇 прогнозы теряют деньги — убираю и больше не беру (сильно теряющих — уже после 5 проверок);
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

from .ai import THINK_ROOM

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
HISTORY_HORIZON_DAYS = 3       # прошлый прогноз проверяем через 3 дня (как живые прогнозы с бирж)
HISTORY_MIN = 4                 # от 4 проверяемых прошлых прогнозов история что-то значит
HISTORY_REJECT = -2.0           # история в минусе (с поправкой на везение) — на испытание не берём
HISTORY_EVERY = 7 * 86400       # историю каналов из списка пересчитываю раз в неделю
HISTORY_COST_PCT = 0.2          # две комиссии спота
BULL_BIAS = 0.9                 # 90% прогнозов «вверх» — в растущий рынок такие выглядят умнее, чем есть
SOFT_REJECT_DAYS = 60           # «мягкий» отказ (заброшен, не про трейдинг, нет прогнозов) — через 60 дней можно снова
PROVEN_CHECKS = 8               # столько живых проверок нужно для ⭐ (как MIN_CHECKED в mentors.py)
EARLY_DROP_CHECKS = 5           # уже на 5 проверках сильно теряют — не ждём конца испытания
EARLY_DROP_AVG = -3.0
SNOWBALL_SOURCES = 3            # у скольких ⭐ трейдеров смотрим, кого они упоминают
QUERIES_PER_RUN = 2
VET_PER_RUN = 10                # сколько каналов YouTube проверяем за один поиск
GEMINI_TITLES_PER_RUN = 4       # разборов названий через Gemini за проход (бесплатный лимит нужен и роликам)
TWITCH_GQL = "https://gql.twitch.tv/gql"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # публичный id веб-сайта Twitch (без логина)

YT_QUERIES = (
    "crypto trading live", "memecoin trading solana", "bitcoin technical analysis today",
    "altcoin trade setup", "крипто трейдинг", "мемкоины solana трейдинг", "crypto day trading strategy",
    "solana memecoins live trading", "биткоин теханализ сегодня", "разбор крипторынка", "альткоины прогноз",
    "crypto swing trading setups",
)
VIA_SNOWBALL = "🔗 от ⭐ трейдеров"
COIN_NAMES = {
    "bitcoin": "BTC", "биткоин": "BTC", "биткойн": "BTC", "биток": "BTC", "btc": "BTC", "ethereum": "ETH", "ether": "ETH",
    "эфир": "ETH", "эфириум": "ETH", "eth": "ETH", "solana": "SOL", "солана": "SOL", "sol": "SOL", "xrp": "XRP",
    "ripple": "XRP", "рипл": "XRP", "cardano": "ADA", "кардано": "ADA", "dogecoin": "DOGE", "doge": "DOGE",
    "доги": "DOGE", "bnb": "BNB", "avalanche": "AVAX", "avax": "AVAX", "chainlink": "LINK", "polkadot": "DOT",
    "pepe": "PEPE", "shiba": "SHIB", "toncoin": "TON", "litecoin": "LTC", "tron": "TRX", "aptos": "APT",
    "arbitrum": "ARB", "injective": "INJ", "kaspa": "KAS", "ondo": "ONDO", "bonk": "BONK", "dogwifhat": "WIF",
    "hedera": "HBAR", "stellar": "XLM", "bittensor": "TAO", "sui": "SUI",
}
UPPER_TICKERS = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "AVAX", "LINK", "DOT", "SUI", "PEPE", "SHIB", "TON",
                 "LTC", "NEAR", "TRX", "APT", "ARB", "INJ", "KAS", "ONDO", "JUP", "WIF", "BONK", "HBAR", "XLM", "SEI",
                 "TIA", "OP", "FET", "TAO", "ENA", "POL", "RNDR", "ATOM", "FIL", "UNI", "AAVE"}
NEGATED_BUY = re.compile(r"(don'?t buy|do not buy|не покупа|не входи|avoid|избега)", re.I)
BULL_WORDS = re.compile(
    r"(pump|moon|rally|breakout|break ?out|bull|\bbuy|\blong\b|all[- ]time high|\bath\b|soar|explode|skyrocket|surge|"
    r"rocket|going up|to \$\d|\b\d{1,3}x\b|рост|раст[её]т|вырастет|памп|покупа|лонг|быч|взл[её]т|ракета|пробой|"
    r"на луну|иксы)", re.I)
BEAR_WORDS = re.compile(
    r"(crash|dump|\bsell|bear|\bshort\b|\bdrop|collapse|plunge|going down|top is in|bubble|обвал|падени|упад[её]т|"
    r"дамп|продава|шорт|медвеж|крах|пузыр)", re.I)
REF_YT_HANDLE = re.compile(r"youtube\.com/@([\w.\-]{2,40})", re.I)
REF_YT_CHANNEL = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
REF_TWITCH = re.compile(r"twitch\.tv/([A-Za-z0-9_]{3,25})\b", re.I)
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
{learned}Ответ СТРОГО JSON-массивом без пояснений:
[{{"platform": "youtube|twitch", "handle": "для YouTube — @имя канала, для Twitch — логин", "name": "как называется",
   "why": "кратко по-русски, чем полезен"}}]"""

TITLES_PROMPT = """\
Ниже названия роликов крипто-канала с номерами. Найди в них ЯВНЫЕ прогнозы цены: монета (тикер с биржи, например \
BTC, ETH, SOL) и направление — "long" (вырастет/покупать) или "short" (упадёт/продавать). Новости, обучение, \
вопросы без ответа и «может быть» — пропускай. В одном ролике не больше двух прогнозов.
Ответ СТРОГО JSON-массивом без пояснений: [{{"i": номер, "asset": "BTC", "direction": "long|short"}}]
{titles}"""


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


def fit(lines: list[str], limit: int = 4000) -> str:
    """Склеить строки, не превышая лимит Telegram и не разрезая HTML-теги внутри строки."""
    out, size = [], 0
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def title_calls(title: str) -> list[dict]:
    """Прогноз из названия ролика без ИИ: «Bitcoin to $150K!» → [{asset: BTC, direction: long}]. Неясно — пусто."""
    t = title or ""
    assets: list[str] = []
    for m in re.finditer(r"\$([A-Za-z]{2,10})\b|\b([A-Z]{2,6})\b|([A-Za-zА-Яа-яЁё]{3,12})", t):
        cash, upper, word = m.groups()
        a = None
        if cash and cash.upper() in UPPER_TICKERS | set(COIN_NAMES.values()):
            a = cash.upper()
        elif upper and upper in UPPER_TICKERS:
            a = upper
        elif word:
            a = COIN_NAMES.get(word.lower())
        if a and a not in assets:
            assets.append(a)
    if not assets:
        return []
    if NEGATED_BUY.search(t):
        direction = "short"
    else:
        bull, bear = bool(BULL_WORDS.search(t)), bool(BEAR_WORDS.search(t))
        if bull == bear:
            return []  # и «вверх», и «вниз» (или ни того ни другого) — это не прогноз
        direction = "long" if bull else "short"
    return [{"asset": a, "direction": direction} for a in assets[:2]]


def parse_title_calls(text: str, n_videos: int) -> list[dict]:
    """Ответ Gemini по названиям → [{i, asset, direction}]."""
    m = re.search(r"\[.*\]", text or "", re.S)
    try:
        raw = json.loads(m.group(0)) if m else []
    except ValueError:
        return []
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        try:
            i = int(r.get("i"))
        except (TypeError, ValueError):
            continue
        asset = re.sub(r"[^A-Z0-9]", "", str(r.get("asset") or "").upper())
        direction = str(r.get("direction") or "").lower()
        if 1 <= i <= n_videos and 2 <= len(asset) <= 10 and direction in ("long", "short"):
            out.append({"i": i, "asset": asset, "direction": direction})
    return out


def backtest(items: list[dict], candles: dict[str, list[list[float]]], now: float,
             horizon_days: int = HISTORY_HORIZON_DAYS, cost_pct: float = HISTORY_COST_PCT) -> dict:
    """Проверка прошлых прогнозов на дневных свечах.

    Вход — открытие первой дневной свечи ПОСЛЕ выхода ролика (раньше бот войти не мог), выход — закрытие через
    horizon_days дней, минус две комиссии. Повтор той же монеты в ту же сторону в течение срока не считаем дважды.
    """
    rows, last_at = [], {}
    for it in sorted(items, key=lambda x: x["published"]):
        key = (it["asset"], it["direction"])
        if it["published"] - last_at.get(key, -1e18) < horizon_days * 86400:
            continue
        cs = candles.get(it["asset"]) or []
        t0 = it["published"] * 1000
        i0 = next((i for i, c in enumerate(cs) if c[0] >= t0), None)
        if i0 is None or cs[i0][0] - t0 > 2 * 86400_000:
            continue
        i1 = i0 + horizon_days - 1
        if i1 >= len(cs) or cs[i1][0] + 86400_000 > now * 1000:  # свеча выхода ещё не закрылась
            continue
        entry, exit_ = cs[i0][1], cs[i1][4]
        if not entry or not exit_:
            continue
        sign = -1 if it["direction"] == "short" else 1
        ret = round(sign * (exit_ / entry - 1) * 100 - cost_pct, 2)
        last_at[key] = it["published"]
        rows.append({"asset": it["asset"], "direction": it["direction"], "ret": ret, "at": it["published"],
                     "title": (it.get("title") or "")[:80]})
    rets = [r["ret"] for r in rows]
    n = len(rets)
    return {"n": n, "wins": sum(r > 0 for r in rets), "avg": round(sum(rets) / n, 2) if n else 0.0,
            "shrunk": round(sum(rets) / (n + 5), 2) if n else 0.0,
            "bull_share": round(sum(r["direction"] == "long" for r in rows) / n, 2) if n else 0.0,
            "rows": list(reversed(rows))[:8]}


def history_bad(h: dict | None) -> bool:
    return bool(h) and h["n"] >= HISTORY_MIN and h["shrunk"] <= HISTORY_REJECT


def history_text(h: dict | None) -> str:
    if not h:
        return ""
    if not h["n"]:
        return "📜 в названиях прошлых роликов нет проверяемых прогнозов"
    bias = " · почти все «вверх» — на падении ещё не проверен" if h["n"] >= 3 and h["bull_share"] >= BULL_BIAS else ""
    return (f"📜 история: {h['n']} прошлых прогнозов, сбылось {h['wins']}, в среднем {h['avg']:+.1f}% за "
            f"{HISTORY_HORIZON_DAYS} дн. ({h['shrunk']:+.1f}% с поправкой на везение){bias}")


def extract_refs(text: str) -> list[dict]:
    """Ссылки на другие каналы в описании ролика → кандидаты (снежный ком от ⭐ трейдеров)."""
    out, seen = [], set()
    for m in REF_YT_CHANNEL.finditer(text or ""):
        if ("c", m.group(1)) not in seen:
            seen.add(("c", m.group(1)))
            out.append({"platform": "youtube", "channel_id": m.group(1), "name": m.group(1)})
    for m in REF_YT_HANDLE.finditer(text or ""):
        h = m.group(1).rstrip(".")
        if ("h", h.lower()) not in seen:
            seen.add(("h", h.lower()))
            out.append({"platform": "youtube", "handle": h, "name": h})
    for m in REF_TWITCH.finditer(text or ""):
        login = m.group(1).lower()
        if login not in ("videos", "directory", "settings", "subs", "p", "downloads") and ("t", login) not in seen:
            seen.add(("t", login))
            out.append({"platform": "twitch", "handle": login, "name": m.group(1)})
    return out


def pick_queries(q_stats: dict, k: int, turn: int) -> list[str]:
    """Запросы для поиска YouTube: чаще те, что уже приводили к ⭐; остальные по кругу."""
    n = len(YT_QUERIES)

    def score(i: int) -> tuple:
        s = q_stats.get(YT_QUERIES[i]) or {}
        return (-(s.get("kept", 0) + 1) / (s.get("taken", 0) + 2), (i - turn) % n)

    return [YT_QUERIES[i] for i in sorted(range(n), key=score)[:k]]


# ---------------------------------------------------------------------------
# Фейк-фильтр
# ---------------------------------------------------------------------------

def vet_youtube(videos: list[dict], subs: int | None, now: float) -> dict:
    """Проверка канала до испытания. → {risk 0..10, reasons, ok, relevant, avg_views}."""
    reasons, risk = [], 0
    long_videos = [v for v in videos if not v.get("short")]
    if not videos:
        return {"risk": 10, "reasons": ["нет роликов"], "ok": False, "relevant": 0.0, "avg_views": None, "hard": False}
    hard = False  # «жёсткий» отказ (накрутка, казино, кликбейт) — навсегда; остальное можно проверить позже
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
        hard = True
    elif bait >= 0.25:
        risk += 2
    descs = [v.get("description") or "" for v in videos]
    scam = sum(bool(SCAM_DESC.search(d)) for d in descs) / len(descs)
    if scam >= 0.3:  # в половине роликов казино/VIP-сигналы — это бизнес на зрителях, а не на трейдинге
        reasons.append("в описаниях казино, платные сигналы, пресейлы или раздачи")
        risk += FAKE_REJECT if scam >= 0.5 else 4
        hard = True
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
        hard = True
    risk = min(10, risk)
    return {"risk": risk, "reasons": reasons, "ok": risk < FAKE_REJECT, "relevant": round(relevant, 2),
            "avg_views": int(avg_views) if avg_views is not None else None, "hard": hard}


def vet_twitch(stream: dict) -> dict:
    reasons, risk = [], 0
    title = stream.get("title") or ""
    hard = False
    if TWITCH_BAD.search(title):
        reasons.append("гемблинг или раздачи в названии эфира")
        risk += 8
        hard = True
    if stream.get("viewers") is not None and stream["viewers"] < MIN_TWITCH_VIEWERS:
        reasons.append(f"почти нет зрителей ({stream['viewers']})")
        risk += 4
    if title and not TWITCH_GOOD.search(title):
        reasons.append("эфир не про трейдинг мемкоинов")
        risk += 3
    risk = min(10, risk)
    return {"risk": risk, "reasons": reasons, "ok": risk < FAKE_REJECT, "hard": hard}


# ---------------------------------------------------------------------------
# Разведчик
# ---------------------------------------------------------------------------

def new_state() -> dict:
    return {"on": True, "last": 0.0, "q": 0, "rejected": {}, "log": [], "found": 0, "kept": 0, "dropped": 0,
            "last_error": None, "via_stats": {}, "q_stats": {}, "history_runs": 0}


class Scout:
    def __init__(self, mentors) -> None:
        self.m = mentors
        self.busy = False
        self._candles: dict[str, tuple[float, list]] = {}  # монета → (с какого времени, свечи) на время одного прохода
        self._gemini_left = GEMINI_TITLES_PER_RUN

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

    def known_ids(self, now: float | None = None) -> set[str]:
        now = time.time() if now is None else now
        live = {k for k, r in self.st["rejected"].items() if not r.get("until") or r["until"] > now}
        return set(self.m.st["sources"]) | live

    def _reject(self, sid: str, name: str, why: str, now: float, soft_days: int | None = None) -> None:
        """Отказ. soft_days — «мягкий» (канал заброшен, нет прогнозов): через столько дней можно проверить снова."""
        self.st["rejected"][sid] = {"name": name[:40], "why": why[:160], "at": now,
                                    "until": now + soft_days * 86400 if soft_days else None}
        if len(self.st["rejected"]) > 300:  # храним последние 300 отказов
            for k in sorted(self.st["rejected"], key=lambda k: self.st["rejected"][k]["at"])[:50]:
                del self.st["rejected"][k]

    # --- самообучение: какие источники и запросы приводят к ⭐ ------------------------------

    def _stat(self, src: dict, field: str) -> None:
        via = src.get("via") or "?"
        self.st["via_stats"].setdefault(via, {"taken": 0, "kept": 0, "dropped": 0})[field] += 1
        if src.get("query"):
            self.st["q_stats"].setdefault(src["query"], {"taken": 0, "kept": 0, "dropped": 0})[field] += 1

    def via_rate(self, via: str) -> float:
        """Доля ⭐ среди взятых из этого источника (со сглаживанием: новый источник ≈ 0,33)."""
        s = self.st["via_stats"].get(via) or {}
        return (s.get("kept", 0) + 1) / (s.get("taken", 0) + 3)

    def _learned_hint(self) -> str:
        good = [s["name"] for s in self.m.active() if s["status"] == "star"][:8]
        bad = sorted(self.st["rejected"].values(), key=lambda r: -r["at"])[:6]
        out = ""
        if good:
            out += "Проверку ценой прошли (ищи похожих по стилю): " + ", ".join(good) + ".\n"
        if bad:
            out += "Отсеяны (не предлагай похожих): " + "; ".join(f"{r['name']} — {r['why'][:60]}" for r in bad) + ".\n"
        return out

    # --- сеть -------------------------------------------------------------------------

    async def _gemini_json(self, prompt: str, search: bool = False, max_tokens: int = 4000) -> str:
        """Текстовый запрос к Gemini → текст ответа. search=True — с поиском Google (если модель не умеет — без)."""
        g = self.m.gemini
        http = await self.m._session()
        model = await g._pick_model()
        for tools in (([{"google_search": {}}], None) if search else (None,)):
            body = {"contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.3, "maxOutputTokens": max_tokens + THINK_ROOM}}
            if tools:
                body["tools"] = tools
            async with http.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                 json=body, headers={"x-goog-api-key": g.key},
                                 timeout=aiohttp.ClientTimeout(total=90)) as r:
                text = await r.text()
                if r.status == 200:
                    data = json.loads(text)
                    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                    return "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if r.status == 400 and tools:  # модель не умеет поиск Google — спросим без него
                    continue
                if r.status == 429:
                    g.disabled_until = time.time() + 15 * 60
                raise RuntimeError(f"Gemini {r.status}: {text[:120]}")
        return ""

    def _gemini_ready(self) -> bool:
        g = self.m.gemini
        return bool(g.configured) and time.time() >= getattr(g, "disabled_until", 0)

    async def _gemini_candidates(self, n: int) -> list[dict]:
        if not self._gemini_ready():
            return []
        known = sorted({s["name"] for s in self.m.active()} | {r["name"] for r in self.st["rejected"].values()})[:40]
        prompt = GEMINI_PROMPT.format(n=n, known=", ".join(known) or "—", learned=self._learned_hint())
        return parse_gemini_candidates(await self._gemini_json(prompt, search=True))

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

    # --- 📜 история прогнозов -------------------------------------------------------------

    async def _title_calls(self, videos: list[dict]) -> tuple[list[dict], str]:
        """Прогнозы из названий роликов: Gemini (точнее), а без него или при ошибке — по словам."""
        if self._gemini_ready() and self._gemini_left > 0:
            self._gemini_left -= 1
            titles = "\n".join(f"{i}. {v.get('title', '')[:140]}" for i, v in enumerate(videos, 1))
            try:
                raw = parse_title_calls(await self._gemini_json(TITLES_PROMPT.format(titles=titles), max_tokens=1500),
                                        len(videos))
                return [{**c, "published": videos[c["i"] - 1]["published"], "title": videos[c["i"] - 1].get("title")}
                        for c in raw], "Gemini"
            except Exception as e:
                log.info("Разведчик: прогнозы из названий через Gemini: %s", e)
        out = []
        for v in videos:
            for c in title_calls(v.get("title") or ""):
                out.append({**c, "published": v["published"], "title": v.get("title")})
        return out, "по словам в названиях"

    async def _coin_candles(self, coin: str, since: float) -> list[list[float]]:
        got = self._candles.get(coin)
        if got and got[0] <= since:
            return got[1]
        try:
            rows = await self.m.candles_of(coin, int(since * 1000)) or []
        except Exception as e:
            log.info("Разведчик: свечи %s: %s", coin, e)
            rows = []
        self._candles[coin] = (since, rows)
        return rows

    async def history(self, videos: list[dict], now: float) -> dict | None:
        """📜 Прошлые прогнозы канала (из названий последних роликов) против настоящих цен. None — нет цен."""
        if not self.m.candles_of:
            return None
        old = [v for v in videos if not v.get("short") and v.get("published")
               and now - v["published"] >= HISTORY_HORIZON_DAYS * 86400][:15]
        if not old:
            return {"n": 0, "wins": 0, "avg": 0.0, "shrunk": 0.0, "bull_share": 0.0, "rows": [], "method": "", "at": now}
        items, method = await self._title_calls(old)
        since = min(v["published"] for v in old) - 2 * 86400
        candles = {}
        for coin in sorted({i["asset"] for i in items})[:8]:
            candles[coin] = await self._coin_candles(coin, since)
        h = backtest(items, candles, now)
        h.update(method=method, at=now)
        return h

    async def refresh_history(self, now: float | None = None, force: bool = False, limit: int = 1) -> int:
        """Пересчитать историю прогнозов каналов YouTube из списка (по одному за тик; по кнопке — всех)."""
        now = time.time() if now is None else now
        if not self.m.candles_of:
            return 0
        due = [s for s in self.m.active("youtube")
               if force or now - ((s.get("history") or {}).get("at") or 0) >= HISTORY_EVERY]
        done = 0
        self._candles, self._gemini_left = {}, GEMINI_TITLES_PER_RUN
        for src in due[:limit]:
            try:
                _, videos = await self.m.youtube.feed(src["ident"])
                h = await self.history(videos, now)
            except Exception as e:
                log.info("Разведчик: история %s: %s", src["name"], e)
                h = None
            if h is not None:
                src["history"] = h
                done += 1
        if done:
            self.st["history_runs"] += 1
            self.m._changed()
        return done

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
            # 2) снежный ком: кого упоминают ⭐ трейдеры (хорошие трейдеры чаще ссылаются на сильных коллег)
            for star in [s for s in self.m.active("youtube") if s["status"] == "star"][:SNOWBALL_SOURCES]:
                try:
                    _, vids = await self.m.youtube.feed(star["ident"])
                except Exception as e:
                    log.info("Разведчик: снежный ком %s: %s", star["name"], e)
                    continue
                for ref in extract_refs("\n".join(v.get("description") or "" for v in vids)):
                    c = {**ref, "why": f"его упоминает ⭐ {star['name']}", "via": VIA_SNOWBALL}
                    (yt_cands if ref["platform"] == "youtube" else tw_cands).insert(0, c)
            # 3) поиск YouTube (без ключа): запросы, которые раньше приводили к ⭐, — чаще
            if want["youtube"] > 0:
                for q in pick_queries(st["q_stats"], QUERIES_PER_RUN, st["q"]):
                    try:
                        for c in (await self._yt_search(q))[:12]:
                            yt_cands.append({"platform": "youtube", "channel_id": c["channel_id"], "name": c["name"],
                                             "subs": c["subs"], "why": f"нашёл по запросу «{q}»",
                                             "via": "поиск YouTube", "query": q})
                    except Exception as e:
                        notes.append(f"поиск YouTube: {str(e)[:100]}")
                        break
                st["q"] += 1
            # 4) Twitch, категория Crypto
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
        """Фейк-фильтр + 📜 история прогнозов → из прошедших берём лучших (а не первых попавшихся)."""
        if want <= 0:
            return []
        known = self.known_ids(now)
        passed, vetted = [], 0
        self._candles, self._gemini_left = {}, GEMINI_TITLES_PER_RUN
        for c in cands:
            if vetted >= VET_PER_RUN:
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
                vetted += 1
                feed_name, videos = await self.m.youtube.feed(cid)
                name = feed_name or name
                v = vet_youtube(videos, c.get("subs"), now)
            except Exception as e:
                log.info("Разведчик, YouTube %s: %s", c.get("name"), e)
                continue
            if not v["ok"]:
                self._reject(sid, name, "фейк-фильтр: " + "; ".join(v["reasons"]), now,
                             None if v["hard"] else SOFT_REJECT_DAYS)
                continue
            h = await self.history(videos, now)
            if history_bad(h):
                self._reject(sid, name, f"прошлые прогнозы в минусе: {h['n']} шт., сбылось {h['wins']}, "
                                        f"в среднем {h['avg']:+.1f}%", now)
                continue
            score = self.via_rate(c["via"]) - v["risk"] * 0.05
            if h and h["n"] >= 3:
                score += max(-1.0, min(2.0, h["shrunk"] / 2))
            passed.append((score, c, cid, name, v, h, videos))
        added = []
        for _score, c, cid, name, v, h, videos in sorted(passed, key=lambda x: -x[0])[:want]:
            src = self.m._add("youtube", cid, name, note=f"🔭 {c.get('why') or c['via']}"[:120], user=False)
            self._mark(src, c["via"], v["risk"], now, "youtube", query=c.get("query"))
            if h is not None:
                src["history"] = h
            src["seen"] = [x["id"] for x in videos][-40:] or ["-"]  # прошлые ролики не разбираем: судим по новым
            added.append(src)
        return added

    def _take_twitch(self, cands: list[dict], want: int, now: float) -> list[dict]:
        added = []
        known = self.known_ids(now)

        def rank(c: dict) -> tuple:
            stream = c.get("stream") or {}
            return (-self.via_rate(c["via"]), -int(bool(TWITCH_GOOD.search(stream.get("title", "")))),
                    -stream.get("viewers", 0))

        for c in sorted(cands, key=rank):
            if len(added) >= want:
                break
            sid = f"tw:{c['handle']}"
            if sid in known:
                continue
            known.add(sid)
            v = vet_twitch(c["stream"]) if c.get("stream") else {"risk": 0, "reasons": [], "ok": True}
            if not v["ok"]:
                self._reject(sid, c["name"], "фейк-фильтр: " + "; ".join(v["reasons"]), now,
                             None if v["hard"] else SOFT_REJECT_DAYS)
                continue
            src = self.m._add("twitch", c["handle"], c["name"], note=f"🔭 {c.get('why') or c['via']}"[:120], user=False)
            self._mark(src, c["via"], v["risk"], now, "twitch")
            added.append(src)
        return added

    def _mark(self, src: dict, via: str, risk: int, now: float, kind: str, query: str | None = None) -> None:
        src.update(auto=True, via=via, fake_risk=risk, trial_until=now + TRIAL_DAYS[kind] * 86400, found_at=now,
                   query=query)
        self._stat(src, "taken")

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
                    self._stat(src, "kept")
                    text = (f"🔭⭐ <b>{html.escape(src['name'])}</b> прошёл испытание: {self.m.score_text(src)}. "
                            "Оставляю — его прогнозы теперь приходят сигналом.")
                    self._log(re.sub(r"<[^>]+>", "", text), now)
                    events.append({"text": text, "buttons": [("🎓 Трейдеры", "mn:list")]})
                continue
            if not why:
                continue
            soft = why.startswith(("в ", "за ", "испытание закончилось"))  # нет прогнозов / нет плюса — не обман
            src["removed"], src["removed_at"] = True, now
            self._reject(src["id"], src["name"], why, now, (SOFT_REJECT_DAYS + 30) if soft else None)
            self.st["dropped"] += 1
            if src.get("trial_until"):
                self._stat(src, "dropped")
            self.m._changed()
            text = f"🔭 Убрал <b>{html.escape(src['name'])}</b>: {html.escape(why)}."
            self._log(re.sub(r"<[^>]+>", "", text), now)
            if self.m.st["notify"]:
                events.append({"text": text, "buttons": []})
        self.prune(now)
        return events

    def prune(self, now: float) -> int:
        """Забыть убранных разведчиком через 14 дней (их имя и причина остаются в «отсеянных»): иначе за год в
        памяти и бэкапе копились бы тысячи записей. Пока по источнику есть открытые прогнозы — не трогаем."""
        sources = self.m.st["sources"]
        busy = {c["source"] for c in self.m.st["calls"] if not c["done"]}
        old = [sid for sid, s in sources.items() if s.get("removed") and s.get("auto") and sid not in busy
               and now - (s.get("removed_at") or 0) >= 14 * 86400]
        for sid in old:
            del sources[sid]
        if old:
            self.m._changed()
        return len(old)

    def _verdict(self, src: dict, now: float) -> str | None:
        """'star' — оставить; строка — причина убрать; None — ещё рано судить."""
        if src["status"] == "star":
            return "star"
        if src["status"] == "muted":
            return f"прогнозы теряют деньги ({self.m.score_text(src)})"
        r = src["rets"]
        if len(r) >= EARLY_DROP_CHECKS and sum(r) / (len(r) + 5) <= EARLY_DROP_AVG:
            return f"уже на {len(r)} проверках сильно теряют ({self.m.score_text(src)})"
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
        if self.st["on"] and not self.busy:
            try:
                await self.refresh_history(now, limit=1)  # по одному каналу за тик, раз в неделю каждый
            except Exception as e:
                log.info("Разведчик: история: %s", e)
        return events

    # --- 🏆 рейтинг ----------------------------------------------------------------------

    def leaderboard(self) -> list[dict]:
        """Трейдеры по доказанной прибыли: сначала живые проверки ценой, потом — только история."""
        rows = []
        for s in self.m.active():
            r, h = s["rets"], s.get("history") or {}
            if len(r) < 3 and h.get("n", 0) < HISTORY_MIN:
                continue
            rows.append({"id": s["id"], "name": s["name"], "kind": s["kind"], "status": s["status"],
                         "trial": bool(s.get("auto") and s.get("trial_until")), "n": len(r),
                         "wins": sum(x > 0 for x in r), "avg": round(sum(r) / (len(r) + 5), 2) if r else 0.0,
                         "per10": round(sum(r) * 0.1, 2), "hist_n": h.get("n", 0), "hist_wins": h.get("wins", 0),
                         "hist_avg": h.get("shrunk", 0.0), "bias": h.get("n", 0) >= 3 and h.get("bull_share", 0) >= BULL_BIAS})
        # уровни доказанности: от 8 живых проверок → от 3 → только история (3 удачи подряд — ещё не умение)
        # убыточные — в самом низу, даже если доказаны
        def key(x: dict) -> tuple:
            score = x["avg"] if x["n"] >= 3 else x["hist_avg"]
            tier = 0 if score <= 0 else 3 if x["n"] >= PROVEN_CHECKS else 2 if x["n"] >= 3 else 1
            return (-tier, -score)

        return sorted(rows, key=key)

    def rank_text(self) -> str:
        rows = self.leaderboard()
        lines = ["🏆 <b>Рейтинг трейдеров по доказанной прибыли</b>",
                 "Сверху — в плюсе от 8 живых проверок, ниже — от 3, потом — только история, в конце — кто в минусе. Живые прогнозы — бот запомнил цену в момент совета и проверил через срок. 📜 — прошлые прогнозы из "
                 "названий роликов на настоящих свечах. Средние — с поправкой на везение, комиссии вычтены."]
        if not rows:
            lines.append("\nПока мало данных: нужно от 3 проверенных прогнозов или 4 прошлых. Нажми «📜 Проверить "
                         "историю» — посчитаю прошлые прогнозы всех каналов YouTube прямо сейчас.")
        for i, x in enumerate(rows[:12], 1):
            icon = "🔭" if x["trial"] and x["status"] == "watch" else {"star": "⭐", "watch": "👀", "muted": "🔇"}[x["status"]]
            live = (f"{x['n']} пров., сбылось {x['wins']}, {x['avg']:+.1f}% · по $10 на идею: {x['per10']:+.2f}$"
                    if x["n"] else "живых проверок ещё нет")
            hist = (f" · 📜 {x['hist_n']} прошлых, сбылось {x['hist_wins']}, {x['hist_avg']:+.1f}%"
                    f"{' (почти все «вверх»)' if x['bias'] else ''}" if x["hist_n"] else "")
            lines.append(f"{i}. {icon}{'▶️' if x['kind'] == 'youtube' else '📺'} <b>{html.escape(x['name'])}</b> — {live}{hist}")
        lines.append("\nЧестно: прошлые прогнозы — подсказка, а не доказательство (названия роликов неточны, в растущий "
                     "рынок все «быки» выглядят умными). ⭐ даёт только живая проверка: от 8 прогнозов и от +2%.")
        return fit(lines)

    # --- показ ----------------------------------------------------------------------------

    def status_dict(self) -> dict:
        st = self.st
        trial = [{"id": s["id"], "name": s["name"], "kind": s["kind"], "via": s.get("via"), "checked": len(s["rets"]),
                  "avg": round(sum(s["rets"]) / (len(s["rets"]) + 5), 2) if s["rets"] else 0.0, "calls": s["calls"],
                  "videos": s["videos"], "until": s.get("trial_until"), "history": history_text(s.get("history"))}
                 for s in self.trial()]
        kept = [s["name"] for s in self.m.active() if s.get("auto") and not s.get("trial_until") and s["status"] == "star"]
        rejected = sorted(st["rejected"].values(), key=lambda r: -r["at"])[:8]
        return {"on": st["on"], "last": st["last"], "next": st["last"] + DISCOVER_EVERY if st["last"] else None,
                "trial": trial, "kept": kept, "rejected": rejected, "rejected_total": len(st["rejected"]),
                "found": st["found"], "dropped": st["dropped"],
                "stars": st["kept"], "log": list(reversed(st["log"][-6:])), "error": st["last_error"],
                "max_trial": dict(MAX_TRIAL), "gemini": self.m.gemini.configured,
                "leaderboard": self.leaderboard()[:8], "learned": self.learned(), "history_on": bool(self.m.candles_of)}

    def learned(self) -> list[dict]:
        """Откуда берутся ⭐: источники поиска с их итогами (по ним бот решает, где искать чаще)."""
        rows = [{"via": via, **s, "rate": round(self.via_rate(via), 2)} for via, s in self.st["via_stats"].items()]
        return sorted(rows, key=lambda r: -r["rate"])

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
                if t["history"]:
                    lines.append("   " + html.escape(t["history"]))
        else:
            lines.append("\nНа испытании пока никого." + (" Поиск — по кнопке или сам через сутки." if d["on"] else ""))
        if d["kept"]:
            lines.append("⭐ Нашёл и оставил: " + ", ".join(html.escape(x) for x in d["kept"]))
        top = [x for x in d["leaderboard"] if x["n"] >= 3][:3]
        if top:
            lines.append("🏆 Лучшие по живым проверкам: " + ", ".join(
                f"{html.escape(x['name'])} {x['avg']:+.1f}%" for x in top) + " — кнопка «🏆 Рейтинг»")
        learned = [x for x in d["learned"] if x["taken"]]
        if learned:
            lines.append("🧠 Где нахожу лучших: " + ", ".join(
                f"{html.escape(x['via'])} — ⭐ {x['kept']} из {x['taken']}" for x in learned[:4]))
        if d["rejected"]:
            lines.append("\n<b>Отсеял</b>:")
            for r in d["rejected"][:5]:
                lines.append(f"✖️ {html.escape(r['name'])} — {html.escape(r['why'])}")
        if d["error"]:
            lines.append(f"\n⚠️ {html.escape(d['error'])}")
        lines.append("\nКак отбираю: 1) фейк-фильтр — накрутка (много подписчиков, мало просмотров), кликбейт «100x», "
                     "казино, VIP-сигналы, пресейлы, заброшенные каналы; 2) 📜 прошлые прогнозы из названий роликов "
                     "на настоящих свечах — в минусе не беру; 3) испытание ценой: ⭐ — от 8 прогнозов и от +2% с "
                     "поправкой на везение, сильно теряющих убираю уже после 5. Хороших мало — это нормально.")
        return fit(lines)
