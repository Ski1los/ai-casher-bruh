"""«💡 Как заработать сейчас»: актуальные возможности, а не только трейдинг.

Источники — публичные, без ключей:
* объявления Bybit (/v5/announcements/index): Launchpool, Token Splash, аирдропы, акции Earn,
  новые листинги и делистинги. Бот объясняет каждую акцию по-русски и честно пишет,
  подходит ли она для $5;
* проценты Bybit Earn (/v5/earn/product): сколько платят за хранение монет прямо сейчас;
* новости: ForkLog (по-русски) и Cointelegraph (RSS). Бот отмечает монеты из заголовков.

Деньги бот сам в акции не вкладывает: в Launchpool/Earn нужно нажать в приложении биржи
(торговый ключ на это не имеет прав, и так безопаснее).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from email.utils import parsedate_to_datetime

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)
ANN_CACHE_SEC = 15 * 60
EARN_CACHE_SEC = 30 * 60
NEWS_CACHE_SEC = 15 * 60
ANN_MAX_AGE_DAYS = 21  # старые объявления без даты окончания не показываем
NEWS_FEEDS = (
    ("ForkLog", "https://forklog.com/feed"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
)
EARN_COINS = ("USDT", "USDC", "BTC", "ETH", "SOL", "MNT", "TON", "XRP", "DOGE")
STABLES = {"USDT", "USDC", "FDUSD", "DAI", "USDE", "PYUSD"}
UA = {"User-Agent": "Mozilla/5.0 (ai-casher-bot; +https://github.com/Ski1los/ai-casher-bruh)"}

# Вид акции → (значок, название, что делать, подходит ли для $5: yes | maybe | no | info)
KINDS: dict[str, tuple[str, str, str, str]] = {
    "delisting": ("🗑", "Делистинг", "монету уберут с биржи: если она у тебя есть — продай заранее, "
                  "после делистинга продать будет нельзя или очень дёшево", "info"),
    "launchpool": ("🌱", "Launchpool", "положи монеты из списка (часто USDT или MNT) в пул и каждый час получай новый токен "
                   "бесплатно. Свои монеты потом забираешь обратно. Приложение Bybit → Launchpool", "yes"),
    "splash": ("💦", "Token Splash", "новые пользователи и активные трейдеры делят призовой фонд в новой монете. "
               "Обычно нужно наторговать этой монетой от $100–500 объёма (комиссия ≈0,1% = $0,1–0,5)", "maybe"),
    "airdrop": ("🎁", "Аирдроп / задания", "бесплатные токены за простые действия (депозит, сделка, задание). "
                "Прочитай условия: часто есть минимальный депозит", "maybe"),
    "earn": ("💰", "Earn / проценты", "проценты за хранение монет. Без риска для стейблкоинов, "
             "но деньги в Earn не участвуют в режиме $5", "yes"),
    "launchpad": ("🚀", "Launchpad", "покупка нового токена до листинга по низкой цене. Обычно нужно держать MNT "
                  "несколько дней: с $5 доля крошечная", "no"),
    "competition": ("🏆", "Соревнование", "призы за объём торговли или прибыль. С $5 шансов на приз почти нет", "no"),
    "listing": ("🆕", "Новый листинг", "в первые часы цена новой монеты сильно скачет в обе стороны. "
                "Бот листинги сам не покупает: слишком рискованно для $5", "info"),
    "futures": ("📉", "Фьючерсы", "акция для фьючерсов: бот торгует только спотом, с $5 фьючерсы — путь к ликвидации", "no"),
    "other": ("📣", "Новость биржи", "прочитай подробности по ссылке", "info"),
}
PROMO_KINDS = ("launchpool", "splash", "airdrop", "earn", "launchpad", "competition")
ALERT_KINDS = ("launchpool", "splash", "airdrop", "earn")  # о чём писать сразу (если уведомления включены)
FIT_TEXT = {"yes": "✅ подходит для $5", "maybe": "🟡 смотри условия", "no": "❌ не для $5", "info": "ℹ️ к сведению"}

KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("delisting", ("delist", "делист")),
    ("launchpool", ("launchpool",)),
    ("splash", ("token splash", "splash")),
    ("airdrop", ("airdrop", "аирдроп", "эйрдроп", "puzzle hunt", "candybomb", "giveaway", "раздач", "bonus", "бонус")),
    ("earn", ("earn", "savings", "saving", "apr", "apy", "staking", "стейкинг", "сбережен", "доходност")),
    ("launchpad", ("launchpad",)),
    ("competition", ("competition", "wsot", "tournament", "leaderboard", "соревнован", "турнир", "чемпионат")),
)
TICKER_RE = re.compile(r"\(([A-Z0-9]{2,12})\)")
NEWS_NAMES = {
    "bitcoin": "BTC", "биткоин": "BTC", "биткойн": "BTC", "ethereum": "ETH", "эфир": "ETH", "эфириум": "ETH",
    "solana": "SOL", "солана": "SOL", "toncoin": "TON", "тонкоин": "TON", "ripple": "XRP", "dogecoin": "DOGE",
    "binance coin": "BNB", "cardano": "ADA", "tron": "TRX", "chainlink": "LINK", "avalanche": "AVAX",
    "polkadot": "DOT", "litecoin": "LTC", "shiba": "SHIB", "pepe": "PEPE", "sui": "SUI", "hyperliquid": "HYPE",
}


@dataclass
class Promo:
    id: str
    title: str
    kind: str
    url: str
    published: float
    start: float | None = None
    end: float | None = None
    description: str = ""
    coins: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    held: bool = False  # для делистинга: монета есть у тебя на бирже

    def as_dict(self) -> dict:
        icon, name, todo, fit = KINDS[self.kind]
        d = asdict(self)
        d.update(icon=icon, kind_name=name, todo=todo, fit=fit, fit_text=FIT_TEXT[fit])
        return d


def bybit_base() -> str:
    host = os.getenv("BYBIT_HOSTNAME", "").strip() or "bybit.com"
    return f"https://api.{host}"


def _ms(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 1e11 else (v or None)


def classify(title: str, type_key: str = "", tags: list[str] | None = None, description: str = "") -> str:
    tags = tags or []
    text = " ".join([title, description, " ".join(tags)]).lower()
    if type_key == "delistings":
        return "delisting"
    for kind, words in KEYWORDS:
        if any(w in text for w in words):
            if kind == "earn" and type_key == "new_crypto":
                break  # «листинг + стейкинг» — это листинг
            return kind
    tag_text = " ".join(tags).lower()
    if ("derivatives" in tag_text or "futures" in tag_text or "perpetual" in text) and "spot" not in tag_text:
        return "futures"
    if type_key == "new_crypto" or "listing" in text or "листинг" in text:
        return "listing"
    return "other"


def parse_announcements(data: dict, now: float | None = None) -> list[Promo]:
    now = time.time() if now is None else now
    out: list[Promo] = []
    for row in ((data or {}).get("result") or {}).get("list") or []:
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url:
            continue
        type_key = str((row.get("type") or {}).get("key") or "")
        tags = [str(t) for t in row.get("tags") or []]
        desc = str(row.get("description") or "").strip()
        published = _ms(row.get("publishTime")) or _ms(row.get("dateTimestamp")) or now
        start = _ms(row.get("startDateTimestamp") or row.get("startDataTimestamp"))
        end = _ms(row.get("endDateTimestamp") or row.get("endDataTimestamp"))
        kind = classify(title, type_key, tags, desc)
        if end and start and end <= start or kind not in PROMO_KINDS:
            end = None  # у листингов/делистингов «конец» — не срок акции (часто совпадает с началом)
        if end and end < now:
            continue  # акция закончилась
        if not end and now - published > ANN_MAX_AGE_DAYS * 86400:
            continue
        coins = list(dict.fromkeys(TICKER_RE.findall(title)))
        out.append(Promo(id=url, title=title, kind=kind, url=url, published=published, start=start, end=end,
                         description=desc[:300], coins=coins, tags=tags))
    return out


def parse_earn(data: dict) -> list[dict]:
    """[{coin, apr, category, min, max}] по убыванию процента (только доступные продукты)."""
    rows = []
    for p in ((data or {}).get("result") or {}).get("list") or []:
        if str(p.get("status") or "Available") != "Available":
            continue
        try:
            apr = float(str(p.get("estimateApr") or "0").replace("%", "").strip() or 0)
        except ValueError:
            continue
        coin = str(p.get("coin") or "")
        if apr <= 0 or not coin:
            continue
        rows.append({"coin": coin, "apr": apr, "category": str(p.get("category") or ""),
                     "min": float(p.get("minStakeAmount") or 0), "max": float(p.get("maxStakeAmount") or 0)})
    best: dict[tuple[str, str], dict] = {}
    for r in rows:  # у монеты бывает несколько продуктов: берём лучший в каждой категории
        k = (r["coin"], r["category"])
        if k not in best or r["apr"] > best[k]["apr"]:
            best[k] = r
    out = [r for r in best.values() if r["coin"] in EARN_COINS or r["apr"] >= 10]
    out.sort(key=lambda r: (r["coin"] not in STABLES, -r["apr"]))
    return out[:12]


def _text(el, tag: str) -> str:
    x = el.find(tag)
    return (x.text or "").strip() if x is not None and x.text else ""


def parse_rss(xml_text: str, source: str, known: set[str] | None = None, limit: int = 8) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = re.sub(r"\s+", " ", _text(it, "title"))
        link = _text(it, "link")
        if not title or not link:
            continue
        try:
            ts = parsedate_to_datetime(_text(it, "pubDate")).timestamp()
        except (TypeError, ValueError):
            ts = None
        out.append({"title": title, "url": link, "source": source, "published": ts,
                    "coins": coins_in(title, known)})
        if len(out) >= limit:
            break
    return out


def coins_in(text: str, known: set[str] | None = None) -> list[str]:
    low = text.lower()
    found = [t for name, t in NEWS_NAMES.items() if name in low]
    for word in re.findall(r"\b[A-Z][A-Z0-9]{1,9}\b", text):
        if known and word in known and word not in ("USD", "SEC", "ETF", "CEO", "AI", "NFT", "DEX", "CEX", "API"):
            found.append(word)
    return list(dict.fromkeys(found))[:4]


class OpportunityHub:
    """Кэш и сбор всех возможностей. Ошибка одного источника не ломает остальные."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, object]] = {}
        self.errors: dict[str, str] = {}
        self.seen: set[str] | None = None  # id объявлений, о которых уже знали (None = ещё не загружали)
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, headers=UA)
        return self._session

    async def _get_json(self, url: str, params: dict) -> dict:
        async with self._s().get(url, params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"ответ {r.status}")
            data = await r.json(content_type=None)
        if isinstance(data, dict) and data.get("retCode") not in (0, None):
            raise RuntimeError(str(data.get("retMsg") or data.get("retCode")))
        return data

    async def _cached(self, key: str, ttl: float, fn, force: bool):
        hit = self._cache.get(key)
        if hit and not force and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            value = await fn()
        except Exception as e:
            self.errors[key] = f"{type(e).__name__}: {e}"[:160]
            log.info("Возможности, %s: %s", key, e)
            return hit[1] if hit else []
        self.errors.pop(key, None)
        self._cache[key] = (time.time(), value)
        return value

    async def announcements(self, force: bool = False) -> list[Promo]:
        async def load():
            url = bybit_base() + "/v5/announcements/index"
            items: list[Promo] = []
            for locale in ("ru-RU", "en-US"):  # русские, если Bybit их отдаёт
                try:
                    items = parse_announcements(await self._get_json(url, {"locale": locale, "limit": 50}))
                except Exception:
                    if locale == "en-US":
                        raise
                    continue
                if items:
                    break
            return items

        return await self._cached("bybit", ANN_CACHE_SEC, load, force)

    async def earn(self, force: bool = False) -> list[dict]:
        async def load():
            url = bybit_base() + "/v5/earn/product"
            rows = []
            for cat in ("FlexibleSaving", "OnChain"):
                try:
                    rows += parse_earn(await self._get_json(url, {"category": cat}))
                except Exception as e:
                    if cat == "FlexibleSaving":
                        raise
                    log.info("Earn OnChain: %s", e)
            best: dict[str, dict] = {}
            for r in rows:  # одна строка на монету — самый высокий процент из двух видов
                if r["coin"] not in best or r["apr"] > best[r["coin"]]["apr"]:
                    best[r["coin"]] = r
            out = sorted(best.values(), key=lambda r: (r["coin"] not in STABLES, -r["apr"]))
            return out[:12]

        return await self._cached("earn", EARN_CACHE_SEC, load, force)

    async def news(self, known: set[str] | None = None, force: bool = False) -> list[dict]:
        async def load():
            async def one(source, url):
                async with self._s().get(url) as r:
                    if r.status != 200:
                        raise RuntimeError(f"{source}: ответ {r.status}")
                    return parse_rss(await r.text(), source, known)

            res = await asyncio.gather(*(one(s, u) for s, u in NEWS_FEEDS), return_exceptions=True)
            items = [x for r in res if not isinstance(r, BaseException) for x in r]
            if not items and res and isinstance(res[0], BaseException):
                raise res[0]
            items.sort(key=lambda x: -(x["published"] or 0))
            return items[:14]

        return await self._cached("news", NEWS_CACHE_SEC, load, force)

    async def snapshot(self, held: set[str] | None = None, known: set[str] | None = None, force: bool = False) -> dict:
        held = held or set()
        anns, earn, news = await asyncio.gather(self.announcements(force), self.earn(force), self.news(known, force))
        for p in anns:
            p.held = p.kind == "delisting" and any(c in held for c in p.coins)
        promos = [p for p in anns if p.kind in PROMO_KINDS]
        fit_order = {"yes": 0, "maybe": 1, "no": 2, "info": 3}
        promos.sort(key=lambda p: (fit_order[KINDS[p.kind][3]], -(p.published or 0)))
        return {
            "promos": [p.as_dict() for p in promos[:15]],
            "listings": [p.as_dict() for p in anns if p.kind == "listing"][:8],
            "delistings": [p.as_dict() for p in sorted(anns, key=lambda p: not p.held) if p.kind == "delisting"][:8],
            "earn": earn,
            "news": news,
            "errors": dict(self.errors),
            "updated_at": time.time(),
        }

    def fresh(self, items: list[Promo]) -> list[Promo]:
        """Объявления, которых раньше не было. Первый вызов только запоминает (не спамим старыми)."""
        ids = {p.id for p in items}
        if self.seen is None:
            self.seen = ids
            return []
        new = [p for p in items if p.id not in self.seen]
        self.seen |= ids
        if len(self.seen) > 500:
            self.seen = ids
        return new


def for_ai(snap: dict | None) -> dict | None:
    """Коротко для ИИ: он составит план на $5 из реальных текущих акций."""
    if not snap:
        return None
    return {
        "bybit_promos_now": [{"kind": p["kind_name"], "title": p["title"][:120], "fits_5usd": p["fit"],
                              "ends": time.strftime("%d.%m", time.gmtime(p["end"])) if p.get("end") else None}
                             for p in snap.get("promos", [])[:8]],
        "bybit_earn_apr_percent": [{"coin": r["coin"], "apr": r["apr"], "type": r["category"]} for r in snap.get("earn", [])[:6]],
        "new_listings": [p["title"][:100] for p in snap.get("listings", [])[:4]],
        "delistings_user_holds": [p["title"][:100] for p in snap.get("delistings", []) if p.get("held")],
        "news_headlines": [n["title"][:120] for n in snap.get("news", [])[:6]],
        "note": "Бот сам в Launchpool/Earn не вкладывает: человек нажимает в приложении Bybit. Ключ бота — только спот.",
    }


PLAN_QUESTION = (
    "Составь мне честный план, как максимально поднять мои ~$5 прямо сейчас. Используй реальные текущие акции Bybit "
    "(Launchpool, Token Splash, аирдропы, Earn) из контекста, мои балансы и режим $5. Для каждого шага: что нажать, "
    "сколько это реально может дать в $ на моей сумме, риск и сроки. Начни с самого выгодного на $5. "
    "Не обещай гарантий, не предлагай фьючерсы, плечо и включение вывода у ключа."
)
