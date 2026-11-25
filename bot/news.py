"""📰 Новости → влияние на рынок → обучение (v47).

Что делает сам, без кнопок:
1. Каждые 2–10 минут читает ~30 бесплатных источников без ключей: крипто-СМИ (EN и RU), регуляторы (ФРС, SEC, CFTC,
   ЕЦБ), Google News по темам, которые двигают рынок, объявления бирж (Binance, OKX, Upbit, Bybit), индекс
   «страха и жадности» и календарь макро-событий США (CPI, ФРС, рынок труда). CryptoPanic — если задан
   CRYPTOPANIC_TOKEN. Источник, который не отвечает, сам уходит на паузу (до 6 ч) и не тормозит остальные.
2. Склеивает одну историю из разных источников (чем больше источников — тем важнее), определяет тип события
   (взлом, делистинг, иск, листинг, ETF, ставка ФРС…), монеты и направление. ИИ (если подключён) раз в 30 минут
   перепроверяет важные заголовки, переводит их и пишет короткую сводку.
3. Запоминает цену монеты в момент, когда бот увидел новость, и меряет, что стало через 15 мин, 1 ч, 4 ч и сутки
   (для монет — относительно биткоина, чтобы общий рынок не путал). Так он учится: какой тип новостей на самом
   деле двигает цену и насколько. Стартовые ожидания — осторожные, факты постепенно их перевешивают.
4. Применяет выученное ко всем движкам:
   • 🛡 щит: монету после плохой новости (если такие новости по фактам роняют цену) автопилот не покупает, спред
     её не трогает, перевод предупреждает;
   • 🌩 шторм: много плохих новостей о рынке сразу — автопилот 2 ч не открывает новых покупок;
   • ⏰ макро-окно: за 30 мин до и 45 мин после важных данных США новых покупок и спред-сделок нет
     (цена в эти минуты скачет в обе стороны);
   • 🧪 бумажные сделки по хорошим новостям: проверка, есть ли у новостей реальное преимущество. Вживую по
     новостям бот не покупает: пока RSS доходит до бота, цена обычно уже сдвинулась. Если какой-то тип новостей
     по фактам стабильно даёт плюс, бот скажет об этом сам («⚡ доказанный сигнал»).

Состояние — в copy.st["news"] (сохраняется и бэкапится). Память ограничена: ~150 событий и короткая статистика.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp

from .opportunities import TICKER_RE, UA, bybit_base, coins_in, parse_announcements

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

TICK_SEC = 120
TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_BYTES = 2_000_000          # больше с одного источника не читаем (память Render)
PER_SOURCE = 20                # свежих заголовков с источника за раз
PARALLEL = 6
BACKOFF_MAX = 6 * 3600
OLD_SEC = 3 * 3600             # новость старше 3 ч, когда бот её увидел, — уже не новость: только для контекста
EVENTS_KEEP = 150
SEEN_KEEP = 1500
HEADLINES_KEEP = 40
SIMILAR = 0.55                 # доля общих слов: это та же история из другого источника
STABLES = {"USDT", "USDC", "FDUSD", "DAI", "USDE", "PYUSD", "TUSD", "BUSD", "USD1", "RLUSD"}
COIN_SKIP = {"BTC", "ETH"}     # «биржу взломали, биткоин упал» — не про риск самого BTC: это через настроение рынка
HORIZONS = (("15m", 900), ("1h", 3600), ("4h", 4 * 3600), ("24h", 86400))
H_SCALE = {"15m": 0.5, "1h": 0.75, "4h": 1.0, "24h": 1.0}  # стартовые ожидания по горизонтам (доля от 4 ч)
PRIOR_K = 6.0                  # сколько «виртуальных» наблюдений весит стартовое ожидание
SHIELD_PCT = -1.0              # по фактам новость такого типа роняет монету хотя бы на 1% за 4 ч — щит
SHIELD_SEC = 12 * 3600
SHIELD_LONG_PCT = -3.0
SHIELD_LONG_SEC = 24 * 3600
MOOD_WINDOW = 6 * 3600
MOOD_HALF_LIFE = 2 * 3600
STORM = -2.5
STORM_PAUSE_SEC = 2 * 3600
MACRO_BEFORE = 30 * 60
MACRO_AFTER = 45 * 60
MACRO_EVERY = 3600
FNG_EVERY = 3600
PAPER_MIN = 1.5                # бумажная сделка, если ожидаемый рост за 4 ч ≥ 1.5%
PAPER_FEES = 0.2               # комиссия входа и выхода, %
PAPER_FRESH = 30 * 60          # только по новостям не старше 30 мин
PAPER_KEEP = 60
PROVEN_N = 12
PROVEN_PCT = 1.5
PROVEN_UP = 0.6
AI_EVERY = 30 * 60
AI_BATCH = 8
ALERTS_PER_DAY = 6

GOOGLE = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"


def gnews(q: str, ru: bool = False) -> str:
    return GOOGLE.format(q=quote(q), hl="ru" if ru else "en-US", gl="RU" if ru else "US", lang="ru" if ru else "en")


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    kind: str          # rss | binance | okx | upbit | bybit | cryptopanic
    url: str
    tier: int = 1      # 2 — первоисточник (регулятор, крупная биржа), 1 — СМИ, 0 — агрегатор/соцсеть
    every: int = 600


SOURCES: tuple[Source, ...] = (
    # крипто-СМИ
    Source("ctg", "Cointelegraph", "rss", "https://cointelegraph.com/rss"),
    Source("cdk", "CoinDesk", "rss", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    Source("dcr", "Decrypt", "rss", "https://decrypt.co/feed"),
    Source("tbk", "The Block", "rss", "https://www.theblock.co/rss.xml"),
    Source("btm", "Bitcoin Magazine", "rss", "https://bitcoinmagazine.com/.rss/full/"),
    Source("csl", "CryptoSlate", "rss", "https://cryptoslate.com/feed/"),
    Source("cpt", "CryptoPotato", "rss", "https://cryptopotato.com/feed/"),
    Source("utd", "U.Today", "rss", "https://u.today/rss"),
    Source("bst", "Bitcoinist", "rss", "https://bitcoinist.com/feed/"),
    Source("nbt", "NewsBTC", "rss", "https://www.newsbtc.com/feed/"),
    Source("bwk", "Blockworks", "rss", "https://blockworks.co/feed"),
    Source("dln", "DL News", "rss", "https://www.dlnews.com/arc/outboundfeeds/rss/"),
    Source("dfi", "The Defiant", "rss", "https://thedefiant.io/api/feed"),
    # по-русски
    Source("frk", "ForkLog", "rss", "https://forklog.com/feed"),
    Source("bmd", "Bits.media", "rss", "https://bits.media/rss2/"),
    Source("inc", "Incrypted", "rss", "https://incrypted.com/feed/"),
    Source("bic", "BeInCrypto RU", "rss", "https://ru.beincrypto.com/feed/"),
    # регуляторы и центробанки
    Source("fed", "ФРС США", "rss", "https://www.federalreserve.gov/feeds/press_all.xml", 2, 900),
    Source("sec", "SEC", "rss", "https://www.sec.gov/news/pressreleases.rss", 2, 900),
    Source("cft", "CFTC", "rss", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", 2, 1800),
    Source("ecb", "ЕЦБ", "rss", "https://www.ecb.europa.eu/rss/press.html", 2, 1800),
    # Google News: темы, которые двигают рынок (сотни изданий сразу)
    Source("gcr", "Google News · крипторынок", "rss", gnews("crypto market OR bitcoin when:1d"), 0, 900),
    Source("grg", "Google News · регулирование", "rss", gnews("SEC OR CFTC OR regulation crypto when:1d"), 0, 900),
    Source("get", "Google News · ETF", "rss", gnews("bitcoin OR ether OR solana ETF when:1d"), 0, 900),
    Source("gmc", "Google News · ФРС и инфляция", "rss", gnews("Federal Reserve OR inflation OR CPI rates when:1d"), 0, 900),
    Source("ghk", "Google News · взломы", "rss", gnews("crypto hack OR exploit OR stolen when:1d"), 0, 900),
    Source("gst", "Google News · стейблкоины", "rss", gnews("stablecoin depeg OR tether OR USDC when:1d"), 0, 1800),
    Source("gru", "Google News · по-русски", "rss", gnews("криптовалюта OR биткоин when:1d", ru=True), 0, 900),
    Source("rdt", "Reddit r/CryptoCurrency", "rss", "https://www.reddit.com/r/CryptoCurrency/top/.rss?t=day", 0, 1800),
    # биржи: листинги и делистинги часто двигают цену сильнее всего
    Source("bnc", "Binance · листинги", "binance",
           "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId=48"
           "&pageNo=1&pageSize=15", 2, 300),
    Source("bnd", "Binance · делистинги", "binance",
           "https://www.binance.com/bapi/composite/v1/public/cms/article/catalog/list/query?catalogId=161"
           "&pageNo=1&pageSize=15", 2, 600),
    Source("okx", "OKX · объявления", "okx", "https://www.okx.com/api/v5/support/announcements", 2, 600),
    Source("upb", "Upbit · торговля", "upbit",
           "https://api-manager.upbit.com/api/v1/announcements?os=web&page=1&per_page=20&category=trade", 2, 300),
    Source("byb", "Bybit · объявления", "bybit", "", 2, 600),
)
CRYPTOPANIC = Source("cpn", "CryptoPanic", "cryptopanic", "https://cryptopanic.com/api/{plan}/v2/posts/", 0, 600)
MAJOR_RE = re.compile(r"\b(binance|coinbase|upbit|okx|robinhood|bithumb)\b", re.I)

# Тип события → (значок, название, стартовое ожидание хода за 4 ч в %, масштаб: монета/рынок, слова)
CATS: dict[str, tuple[str, str, float, str, str]] = {
    "delist": ("🗑", "делистинг", -8.0, "coin",
               r"delist\w*|делист\w*|will remove (?:spot )?trading pairs?|прекрат\w* торг\w*|거래지원 종료"),
    "bankrupt": ("💀", "банкротство", -6.0, "coin", r"insolven\w*|bankrupt\w*|chapter 11|банкрот\w*"),
    "depeg": ("💔", "отвязка стейблкоина", -5.0, "coin", r"de-?peg\w*|los(?:es|t|ing) (?:its )?peg|отвяз\w*"),
    "hack": ("🚨", "взлом / эксплойт", -4.0, "coin",
             r"hack\w*|exploit\w*|drain\w*|stolen|breach\w*|attack(?:ed|er|ers)|взлом\w*|эксплойт\w*|"
             r"украл\w*|украд\w*|похищ\w*|хакер\w*"),
    "listing_major": ("🆕", "листинг на крупной бирже", 4.0, "coin", r"(?!x)x"),  # ставится по источнику
    "outage": ("⛔", "сбой / остановка", -2.0, "coin",
               r"outage\w*|halt\w* (?:trading|withdrawals?|deposits?)|suspend\w* (?:withdrawals?|deposits?|trading)|"
               r"paus\w* (?:withdrawals?|deposits?)|network (?:down|stall\w*)|сбо[йя]\w*|приостанов\w*"),
    "lawsuit": ("⚖️", "иск / обвинения", -2.0, "coin",
                r"lawsuit\w*|sues|sued|su(?:e|ing) (?:over|for)|charge[sd] (?:with|against)|indict\w*|fraud\w*|"
                r"subpoena\w*|wells notice|иск|иска|иске|обвин\w*|мошенн\w*|арест\w*"),
    "listing": ("🆕", "листинг", 2.0, "coin",
                r"will list|lists|listing\w*|to list|adds? \w+(?: \w+)? (?:to|for) trading|launch\w* (?:spot )?trading|"
                r"new (?:spot )?trading pairs?|new trading support|листинг\w*|신규 거래"),
    "ban": ("🚫", "запрет", -2.0, "market", r"bans?|banned|crackdown\w*|prohibit\w*|outlaw\w*|запрет\w*"),
    "hawkish": ("🦅", "жёсткая ФРС / инфляция ↑", -1.5, "market",
                r"rate hikes?|hikes? (?:interest )?rates|raises? (?:interest )?rates|hawkish\w*|hotter[- ]than[- ]expected|"
                r"inflation (?:rises|rose|jumps|accelerat\w*|surges|heats up)|повыс\w* ставк\w*|ястреб\w*|"
                r"инфляци\w* (?:вырос\w*|ускор\w*)"),
    "dovish": ("🕊", "мягкая ФРС / инфляция ↓", 1.5, "market",
               r"rate cuts?|cuts? (?:interest )?rates|lowers? (?:interest )?rates|dovish\w*|cooler[- ]than[- ]expected|"
               r"inflation (?:cools|cooled|eases|eased|falls|fell|slows)|сниз\w* ставк\w*|голуб\w*|"
               r"инфляци\w* (?:замедл\w*|снизил\w*)"),
    "etf_neg": ("📤", "ETF: отток / отказ", -1.5, "market",
                r"etfs?\b.{0,40}\b(?:outflows?|reject\w*|denied|denies|delay\w*|postpone\w*)|"
                r"(?:outflows?|reject\w*|denies|delays?)\b.{0,40}\betfs?|отток\w*"),
    "etf_pos": ("🏦", "ETF: приток / одобрение", 1.5, "market",
                r"etfs?\b.{0,40}\b(?:approv\w*|inflows?|launch\w*|debut\w*|record)|(?:approv\w*|launch\w*)\b.{0,40}\betfs?|"
                r"приток\w*|одобр\w* .{0,20}etf"),
    "unlock": ("🔓", "разлок токенов", -1.5, "coin", r"token unlocks?|unlocks?|vesting|разлок\w*|разблокировк\w*"),
    "adoption": ("🏛", "покупка / принятие", 1.5, "coin",
                 r"(?:buys|bought|acquires?|acquired|purchases?|purchased) .{0,40}(?:bitcoin|btc|ether|eth|solana|sol|"
                 r"xrp|tokens?)|treasury|strategic reserve|reserve asset|legal tender|купил\w*|покупает|резерв\w*"),
    "whale_sell": ("🐋", "крупные продажи", -1.0, "coin",
                   r"whales? (?:sell\w*|dump\w*|mov\w*|transfer\w*)|dumps?|sell-?offs?|распрод\w*|кит\w* (?:прода\w*|перев\w*)"),
    "crash": ("📉", "обвал рынка", -1.0, "market",
              r"plunge\w*|crash\w*|tumble\w*|slump\w*|sinks|nosedive\w*|bloodbath|обвал\w*|рухн\w*|падени\w*"),
    "liquid": ("💥", "ликвидации", -1.0, "market", r"liquidat\w*|ликвидаци\w*"),
    "reg_pos": ("📜", "понятные правила", 1.0, "market",
                r"(?:passes|passed|signs|signed|approves|enacted)\b.{0,30}\b(?:bill|act|law|framework)|regulatory clarity|"
                r"закон\w* (?:принят\w*|подписан\w*)|легализ\w*"),
    "partner": ("🤝", "партнёрство", 0.5, "coin", r"partner\w*|integrat\w*|collaborat\w*|teams up|партн\w*|интеграц\w*"),
    "upgrade": ("⬆️", "обновление / запуск", 0.5, "coin",
                r"mainnet|upgrade\w*|hard ?fork\w*|обновлен\w*|хардфорк\w*"),
    "rally": ("📈", "рост рынка", 0.5, "market",
              r"surge\w*|soar\w*|rall(?:y|ies|ied)|skyrocket\w*|all-time high|ath|взлет\w*|взлёт\w*|ралли|рекорд\w*"),
}
CAT_RES = {k: re.compile(rf"(?<!\w)(?:{v[4]})(?!\w)", re.I) for k, v in CATS.items()}
DENY_RE = re.compile(r"(?<!\w)(?:denies|denied|rumou?rs?|fake|false|hoax|опроверг\w*|слух\w*|фейк\w*)(?!\w)", re.I)
CASHTAG_RE = re.compile(r"\$([A-Z][A-Z0-9]{1,9})\b")
NOT_COINS = {"USD", "SEC", "ETF", "ETFS", "CEO", "AI", "NFT", "DEX", "CEX", "API", "FED", "CPI", "GDP", "PCE", "FOMC",
             "US", "UK", "EU", "IPO", "CFTC", "DOJ", "TVL", "ATH", "KYC", "AML", "OTC", "DAO", "RWA"}

# Макро-события США из календаря ForexFactory: по-русски
MACRO_RU = (
    (r"federal funds rate|fomc statement|fomc press", "решение ФРС по ставке"),
    (r"fomc meeting minutes", "протокол заседания ФРС"),
    (r"powell", "выступление главы ФРС Пауэлла"),
    (r"core cpi|cpi", "инфляция в США (CPI)"),
    (r"core pce|pce", "инфляция PCE (её смотрит ФРС)"),
    (r"ppi", "цены производителей (PPI)"),
    (r"non-farm|nfp", "рынок труда США (NFP)"),
    (r"unemployment", "безработица в США"),
    (r"gdp", "ВВП США"),
    (r"retail sales", "розничные продажи США"),
    (r"ism", "деловая активность США (ISM)"),
    (r"jolts", "вакансии в США (JOLTS)"),
)


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", title.lower())).strip()


def _words(title: str) -> set[str]:
    return {w for w in _norm(title).split() if len(w) > 2}


def title_id(title: str) -> str:
    return hashlib.sha1(" ".join(_norm(title).split()[:14]).encode()).hexdigest()[:10]


def clean_title(title: str, source_kind: str = "") -> str:
    title = html.unescape(re.sub(r"<[^>]+>", " ", title or ""))
    title = re.sub(r"\s+", " ", title).strip()
    if " - " in title and len(title.rsplit(" - ", 1)[1]) <= 40:  # Google News: «Заголовок - Издание»
        title = title.rsplit(" - ", 1)[0].strip()
    return title[:220]


def _ts(text: str) -> float | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _ms(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v / 1000 if v > 1e11 else (v or None)


def parse_feed(text: str, limit: int = PER_SOURCE) -> list[dict]:
    """RSS и Atom → [{title, url, published}]. Ломаный XML — пустой список, а не ошибка всего цикла."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        title = url = when = ""
        for ch in el:
            t = ch.tag.rsplit("}", 1)[-1]
            if t == "title":
                title = "".join(ch.itertext())
            elif t == "link":
                url = (ch.text or "").strip() or ch.get("href", "")
            elif t in ("pubDate", "published", "updated", "date") and not when:
                when = ch.text or ""
        title = clean_title(title)
        if title:
            out.append({"title": title, "url": url.strip()[:300], "published": _ts(when)})
        if len(out) >= limit:
            break
    return out


def parse_binance(data) -> list[dict]:
    out = []

    def walk(node):
        if isinstance(node, dict):
            if "title" in node and ("code" in node or "releaseDate" in node):
                code = node.get("code") or ""
                out.append({"title": clean_title(str(node["title"])), "published": _ms(node.get("releaseDate")),
                            "url": f"https://www.binance.com/en/support/announcement/{code}" if code else ""})
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out[:PER_SOURCE]


def parse_okx(data) -> list[dict]:
    out = []
    for page in (data or {}).get("data") or []:
        for d in (page or {}).get("details") or []:
            if d.get("title"):
                out.append({"title": clean_title(d["title"]), "url": str(d.get("url") or "")[:300],
                            "published": _ms(d.get("pTime") or d.get("businessPTime"))})
    return out[:PER_SOURCE]


def parse_upbit(data) -> list[dict]:
    body = (data or {}).get("data") if isinstance(data, dict) else None
    notices = (body.get("notices") or body.get("list") or []) if isinstance(body, dict) else []
    out = []
    for n in notices:
        if n.get("title"):
            nid = n.get("id") or ""
            out.append({"title": clean_title(str(n["title"])), "published": _ts(n.get("first_listed_at")
                                                                                 or n.get("listed_at") or ""),
                        "url": f"https://upbit.com/service_center/notice?id={nid}" if nid else ""})
    return out[:PER_SOURCE]


def parse_cryptopanic(data) -> list[dict]:
    out = []
    for p in (data or {}).get("results") or []:
        if not p.get("title"):
            continue
        coins = [str(c.get("code") or "").upper() for c in p.get("currencies") or [] if isinstance(c, dict)]
        out.append({"title": clean_title(p["title"]), "url": str(p.get("original_url") or p.get("url") or "")[:300],
                    "published": _ts(p.get("published_at") or ""), "coins": [c for c in coins if c][:4]})
    return out[:PER_SOURCE]


def parse_macro(data, now: float | None = None) -> list[dict]:
    """Календарь ForexFactory → важные события США: [{at, title, ru, forecast, previous}]."""
    now = time.time() if now is None else now
    out = []
    for e in data if isinstance(data, list) else []:
        if str(e.get("country") or "").upper() != "USD" or str(e.get("impact") or "") != "High":
            continue
        at = _ts(str(e.get("date") or ""))
        if not at or at < now - 86400:
            continue
        title = str(e.get("title") or "")
        ru = next((r for pat, r in MACRO_RU if re.search(pat, title, re.I)), title)
        out.append({"id": title_id(f"{title}{at}"), "at": at, "title": title[:80], "ru": ru,
                    "forecast": str(e.get("forecast") or ""), "previous": str(e.get("previous") or "")})
    out.sort(key=lambda x: x["at"])
    return out[:30]


def classify(title: str, source: Source | None = None) -> tuple[str, float]:
    """(тип события, стартовое ожидание хода за 4 ч в %). Тип — тот, у которого ожидание сильнее всего:
    «Binance will delist X» — делистинг, а не листинг; «Bitcoin jumps after rate cut» — ставка, а не «рост»."""
    found = [k for k, rx in CAT_RES.items() if rx.search(title)]
    if not found:
        return "other", 0.0
    cat = max(found, key=lambda k: abs(CATS[k][2]))
    if cat == "listing" and (source and source.kind in ("binance", "okx", "upbit") or MAJOR_RE.search(title)):
        cat = "listing_major"
    prior = CATS[cat][2]
    if DENY_RE.search(title):
        prior *= 0.3  # опровержение или слух — слабее
    return cat, prior


def find_coins(title: str, known: set[str], extra: list[str] | None = None) -> list[str]:
    found = list(extra or []) + coins_in(title, known)
    for word in TICKER_RE.findall(title) + CASHTAG_RE.findall(title):
        if word not in NOT_COINS and (not known or word in known):
            found.append(word)
    return [c for c in dict.fromkeys(found) if c not in NOT_COINS][:4]


def _similar(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def new_state() -> dict:
    return {"on": True, "alerts": True, "events": [], "seen": [], "stats": {}, "first": True,
            "paper": {"open": [], "closed": [], "n": 0, "sum": 0.0, "wins": 0},
            "shield": {"n": 0, "right": 0}, "sent_day": ["", 0], "told": {}, "digest": {"text": "", "at": 0.0},
            "last_ai": 0.0, "storm_until": 0.0, "storm_why": "", "measured": 0, "total": 0}


class News:
    def __init__(self, b: ArbitrageBot) -> None:
        self.b = b
        self._session: aiohttp.ClientSession | None = None
        self.health: dict[str, dict] = {}
        self.macro: list[dict] = []
        self.fng: dict | None = None
        self.headlines: list[dict] = []  # последние заголовки без монет и типа — только для контекста ИИ
        self._macro_at = 0.0
        self._fng_at = 0.0
        self._prices_cache: tuple[float, dict[str, float]] = (0.0, {})
        self._lock = asyncio.Lock()
        self.last_tick = 0.0

    # --- состояние ---------------------------------------------------------------------------------

    @property
    def st(self) -> dict:
        cs = self.b.copy.st
        st = cs.get("news")
        if not isinstance(st, dict):
            st = cs["news"] = {}
        for k, v in new_state().items():
            if k not in st:
                st[k] = ({kk: (list(vv) if isinstance(vv, list) else vv) for kk, vv in v.items()}
                         if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
        return st

    def _changed(self) -> None:
        try:
            self.b.copy.changes += 1
        except AttributeError:
            pass

    def sources(self) -> list[Source]:
        out = list(SOURCES)
        if os.getenv("CRYPTOPANIC_TOKEN", "").strip():
            out.append(CRYPTOPANIC)
        return out

    def when(self, ts: float) -> str:
        off = float(getattr(self.b.settings, "report_utc_offset", 0) or 0)
        return time.strftime("%d.%m %H:%M", time.gmtime(ts + off * 3600))

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT, headers=UA)
        return self._session

    # --- сбор ----------------------------------------------------------------------------------------

    async def _get(self, url: str, params: dict | None = None, as_json: bool = False):
        async with self._s().get(url, params=params) as r:
            if r.status != 200:
                raise RuntimeError(f"ответ {r.status}")
            raw = await r.content.read(MAX_BYTES)
        text = raw.decode("utf-8", errors="replace")
        if not as_json:
            return text
        import json
        return json.loads(text)

    async def fetch(self, src: Source) -> list[dict]:
        if src.kind == "rss":
            return parse_feed(await self._get(src.url))
        if src.kind == "binance":
            return parse_binance(await self._get(src.url, as_json=True))
        if src.kind == "okx":
            return parse_okx(await self._get(src.url, as_json=True))
        if src.kind == "upbit":
            return parse_upbit(await self._get(src.url, as_json=True))
        if src.kind == "bybit":
            data = await self._get(bybit_base() + "/v5/announcements/index", {"locale": "en-US", "limit": 30},
                                   as_json=True)
            return [{"title": clean_title(p.title), "url": p.url, "published": p.published,
                     "coins": list(p.coins)} for p in parse_announcements(data)][:PER_SOURCE]
        if src.kind == "cryptopanic":
            plan = os.getenv("CRYPTOPANIC_PLAN", "").strip() or "developer"
            data = await self._get(src.url.format(plan=plan),
                                   {"auth_token": os.getenv("CRYPTOPANIC_TOKEN", "").strip(), "public": "true"},
                                   as_json=True)
            return parse_cryptopanic(data)
        return []

    def _due(self, src: Source, now: float) -> bool:
        h = self.health.get(src.key)
        return h is None or now >= h.get("next", 0)

    async def collect(self, now: float, force: bool = False) -> list[tuple[Source, dict]]:
        sem = asyncio.Semaphore(PARALLEL)
        due = [s for s in self.sources() if force or self._due(s, now)]

        async def one(src: Source):
            async with sem:
                h = self.health.setdefault(src.key, {"ok": 0, "fail": 0, "err": "", "at": 0.0, "next": 0.0,
                                                     "items": 0, "name": src.name})
                try:
                    items = await asyncio.wait_for(self.fetch(src), 30)
                except Exception as e:
                    h["fail"] += 1
                    h["err"] = f"{type(e).__name__}: {e}"[:120]
                    h["next"] = now + min(BACKOFF_MAX, src.every * 2 ** min(h["fail"], 6))
                    log.info("Новости, %s: %s", src.name, h["err"])
                    return []
                h.update(ok=h["ok"] + 1, fail=0, err="", at=now, next=now + src.every, items=len(items))
                return [(src, it) for it in items]

        res = await asyncio.gather(*(one(s) for s in due))
        return [x for r in res for x in r]

    async def refresh_side(self, now: float) -> None:
        """Индекс страха и жадности и календарь макро-событий (раз в час)."""
        if now - self._fng_at >= FNG_EVERY:
            self._fng_at = now
            try:
                data = await self._get("https://api.alternative.me/fng/", {"limit": 1}, as_json=True)
                row = (data.get("data") or [{}])[0]
                self.fng = {"value": int(row.get("value")), "label": str(row.get("value_classification") or ""),
                            "at": now}
                self.health["fng"] = {"ok": 1, "fail": 0, "err": "", "at": now, "items": 1, "name": "Страх и жадность"}
            except Exception as e:
                self.health["fng"] = {"ok": 0, "fail": 1, "err": str(e)[:120], "at": now, "items": 0,
                                      "name": "Страх и жадность"}
        if now - self._macro_at >= MACRO_EVERY:
            self._macro_at = now
            try:
                data = await self._get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", as_json=True)
                self.macro = parse_macro(data, now)
                self.health["ffc"] = {"ok": 1, "fail": 0, "err": "", "at": now, "items": len(self.macro),
                                      "name": "Календарь макро США"}
            except Exception as e:
                self.health["ffc"] = {"ok": 0, "fail": 1, "err": str(e)[:120], "at": now, "items": 0,
                                      "name": "Календарь макро США"}

    # --- разбор --------------------------------------------------------------------------------------

    def known(self) -> set[str]:
        out = set()
        for ex in self.b.scanner.exchanges.values():
            for m in (getattr(ex, "markets", None) or {}).values():
                if m.get("base"):
                    out.add(m["base"])
        return out

    def ingest(self, items: list[tuple[Source, dict]], now: float) -> list[dict]:
        """Новые заголовки → события. Та же история из другого источника не плодит событие, а усиливает его."""
        st = self.st
        seen = set(st["seen"])
        known = self.known()
        new: list[dict] = []
        first = bool(st["first"])
        recent = [e for e in st["events"] if now - e["seen"] < 12 * 3600]
        words_of = {e["id"]: _words(e["title"]) for e in recent}
        for src, it in items:
            title = it["title"]
            tid = title_id(title)
            if tid in seen:
                continue
            seen.add(tid)
            st["seen"].append(tid)
            st["total"] = int(st.get("total") or 0) + 1
            published = it.get("published") or now
            old = first or now - published > OLD_SEC
            cat, prior = classify(title, src)
            coins = find_coins(title, known, it.get("coins"))
            w = _words(title)
            dup = None
            for e in recent:
                if _similar(w, words_of.get(e["id"], set())) >= SIMILAR:
                    dup = e
                    break
            if dup is not None:
                if src.key not in dup["srcs"]:
                    dup["srcs"].append(src.key)
                    dup["imp"] = self._importance(dup)
                for c in coins:
                    if c not in dup["coins"] and len(dup["coins"]) < 4:
                        dup["coins"].append(c)
                continue
            if cat == "other" and not coins:
                self.headlines.append({"title": title[:160], "src": src.name, "t": published})
                del self.headlines[:-HEADLINES_KEEP]
                continue
            scope = CATS[cat][3] if cat != "other" else "coin"
            ev = {"id": tid, "t": float(published), "seen": now, "title": title[:180], "url": it.get("url", "")[:300],
                  "src": src.key, "srcs": [src.key], "tier": src.tier, "cat": cat, "prior": round(prior, 2),
                  "coins": coins, "scope": "market" if scope == "market" or not coins else "coin",
                  "old": old, "p0": {}, "res": {}}
            ev["imp"] = self._importance(ev)
            st["events"].append(ev)
            recent.append(ev)
            words_of[tid] = w
            if not old:
                new.append(ev)
        if items:
            st["first"] = False  # первый удачный сбор только запоминает: старыми новостями после запуска не спамим
        del st["seen"][:-SEEN_KEEP]
        self._trim()
        if items:
            self._changed()
        return new

    def _importance(self, e: dict) -> int:
        strength = abs(self.est(e["cat"], "4h")[0]) if e["cat"] != "other" else 0.0
        imp = 0 if strength < 0.75 else 1 if strength < 1.75 else 2 if strength < 3.5 else 3
        if imp and (e.get("tier", 1) >= 2 or len(e.get("srcs") or []) >= 3):
            imp += 1
        return min(imp, 3)

    def _trim(self) -> None:
        ev = self.st["events"]
        if len(ev) > EVENTS_KEEP:
            # сначала выкидываем старые и неважные; события, которые ещё меряются, держим
            ev.sort(key=lambda e: (bool(self._pending(e)), e["imp"] > 0, e["seen"]))
            del ev[:len(ev) - EVENTS_KEEP]
            ev.sort(key=lambda e: e["seen"])

    # --- цены и обучение -----------------------------------------------------------------------------

    async def _prices(self, coins: set[str], now: float) -> dict[str, float]:
        """Цены в $ одним запросом всех тикеров (первая биржа, где есть монета). Кэш 60 с."""
        at, cache = self._prices_cache
        if now - at > 60:
            cache = {}
            quote_ccy = self.b.settings.quote
            for name, ex in list(self.b.scanner.exchanges.items())[:2]:
                try:
                    tickers = await asyncio.wait_for(self.b.scanner._fetch_spot_tickers(name, ex), 25)
                except Exception as e:
                    log.info("Новости: цены %s: %s", name, e)
                    continue
                for sym, t in tickers.items():
                    base, _, q = sym.partition("/")
                    if q != quote_ccy or base in cache:
                        continue
                    bid, ask, last = t.get("bid"), t.get("ask"), t.get("last")
                    px = (float(bid) + float(ask)) / 2 if bid and ask else float(last or 0)
                    if px > 0:
                        cache[base] = px
                if coins <= cache.keys():
                    break  # всё нашлось на первой бирже — вторую не спрашиваем
            self._prices_cache = (now, cache)
        return {c: cache[c] for c in coins if c in cache}

    def _measure_coins(self, e: dict) -> list[str]:
        if e["scope"] == "market":
            return ["BTC"]
        return [c for c in e["coins"] if c not in STABLES][:3]

    @staticmethod
    def _pending(e: dict) -> list[str]:
        if e.get("old") or not e.get("p0"):
            return []
        return [h for h, _ in HORIZONS if h not in e["res"]]

    async def measure(self, new: list[dict], now: float) -> int:
        """Цена в момент новости и через 15 мин / 1 ч / 4 ч / сутки. Возвращает, сколько замеров записано."""
        st = self.st
        need: set[str] = set()
        for e in new:
            need |= set(self._measure_coins(e)) | {"BTC"}
        due = []
        for e in st["events"]:
            for h, sec in HORIZONS:
                if h in self._pending(e) and now - e["seen"] >= sec:
                    due.append((e, h))
                    need |= set(e["p0"])
        if not need:
            return 0
        px = await self._prices(need, now)
        for e in new:
            want = self._measure_coins(e)
            e["p0"] = {c: px[c] for c in set(want) | {"BTC"} if c in px}
            if not any(c in e["p0"] for c in want):
                e["p0"] = {}  # цены монеты нет на наших биржах — мерить нечего
        done = 0
        btc_now = px.get("BTC")
        for e, h in due:
            if h in e["res"]:
                continue
            btc0 = e["p0"].get("BTC")
            row = {}
            want = self._measure_coins(e)
            for c, p0 in e["p0"].items():
                if c not in want:
                    continue
                p1 = px.get(c)
                if not p1 or not p0:
                    continue
                raw = (p1 / p0 - 1) * 100
                rel = raw if c == "BTC" or not btc0 or not btc_now else raw - (btc_now / btc0 - 1) * 100
                row[c] = [round(rel, 3), round(raw, 3)]
            e["res"][h] = row
            if row:
                rel_avg = sum(v[0] for v in row.values()) / len(row)
                self._learn(e, h, rel_avg)
                done += 1
            if h == "4h":
                self._judge_shield(e, row)
        self._paper(new, due, px, now)
        if done:
            st["measured"] = int(st.get("measured") or 0) + done
            self._changed()
        return done

    def _learn(self, e: dict, h: str, pct: float) -> None:
        pct = max(-50.0, min(50.0, pct))  # один безумный памп не должен перекосить всю статистику
        stats = self.st["stats"]
        for key in (e["cat"], "src:" + e["src"]):
            row = stats.setdefault(key, {})
            n, s, sq, ups = row.get(h, [0, 0.0, 0.0, 0])
            if n >= 300:  # старые наблюдения постепенно весят меньше — рынок меняется
                n, s, sq, ups = n / 2, s / 2, sq / 2, ups / 2
            row[h] = [n + 1, round(s + pct, 4), round(sq + pct * pct, 4), ups + (1 if pct > 0 else 0)]

    def est(self, cat: str, h: str = "4h") -> tuple[float, float, float]:
        """(ожидаемый ход %, сколько наблюдений, доля случаев роста). Старт — осторожное ожидание, дальше — факты."""
        prior = CATS[cat][2] * H_SCALE.get(h, 1.0) if cat in CATS else 0.0
        n, s, _, ups = (self.st["stats"].get(cat) or {}).get(h, [0, 0.0, 0.0, 0])
        val = (prior * PRIOR_K + s) / (PRIOR_K + n)
        return val, n, (ups / n if n else 0.0)

    def proven(self, cat: str) -> bool:
        val, n, up = self.est(cat, "4h")
        return n >= PROVEN_N and val >= PROVEN_PCT and up >= PROVEN_UP

    def _judge_shield(self, e: dict, row: dict) -> None:
        """Угадал ли щит: монету, которую он не дал купить, за 4 ч действительно потянуло вниз?"""
        if e["scope"] != "coin" or not row or self.est(e["cat"], "4h")[0] > SHIELD_PCT:
            return
        sh = self.st["shield"]
        sh["n"] += 1
        if sum(v[1] for v in row.values()) / len(row) < 0:
            sh["right"] += 1

    def _paper(self, new: list[dict], due: list[tuple[dict, str]], px: dict, now: float) -> None:
        """🧪 Бумажные сделки по хорошим новостям: вход по цене в момент новости (+комиссия), выход через 4 ч."""
        pp = self.st["paper"]
        for e in new:
            if e["scope"] != "coin" or now - e["t"] > PAPER_FRESH or self.est(e["cat"], "4h")[0] < PAPER_MIN:
                continue
            for c in self._measure_coins(e)[:1]:
                if c in COIN_SKIP or c not in e["p0"] or any(o["coin"] == c for o in pp["open"]):
                    continue
                pp["open"].append({"id": e["id"], "coin": c, "cat": e["cat"], "p0": e["p0"][c], "at": now,
                                   "title": e["title"][:100]})
        for e, h in due:
            if h != "4h":
                continue
            for o in [o for o in pp["open"] if o["id"] == e["id"]]:
                p1 = px.get(o["coin"])
                pp["open"].remove(o)
                if not p1:
                    continue
                pct = ((p1 / o["p0"]) * (1 - PAPER_FEES / 200) / (1 + PAPER_FEES / 200) - 1) * 100
                pp["n"] += 1
                pp["sum"] = round(pp["sum"] + pct, 4)
                pp["wins"] += 1 if pct > 0 else 0
                pp["closed"].append({**o, "pct": round(pct, 3), "closed": now})
                del pp["closed"][:-PAPER_KEEP]
        pp["open"] = [o for o in pp["open"] if now - o["at"] < 2 * 86400][-20:]

    # --- решения -------------------------------------------------------------------------------------

    def risk(self, now: float | None = None) -> dict[str, tuple[float, str]]:
        """🛡 Монеты, которые сейчас не покупать: {монета: (до когда, почему)}."""
        now = time.time() if now is None else now
        out: dict[str, tuple[float, str]] = {}
        if not self.st["on"]:
            return out
        for e in self.st["events"]:
            if e["scope"] != "coin" or e["cat"] == "other":
                continue
            val = self.est(e["cat"], "4h")[0]
            if val > SHIELD_PCT:
                continue
            hold = SHIELD_LONG_SEC if val <= SHIELD_LONG_PCT else SHIELD_SEC
            until = min(e["t"], e["seen"]) + hold
            if until <= now:
                continue
            why = f"📰 {CATS[e['cat']][1]} (обычно {val:+.1f}% за 4 ч): {e['title'][:90]}"
            for c in e["coins"]:
                if c in COIN_SKIP or c in STABLES:
                    continue
                if c not in out or out[c][0] < until:
                    out[c] = (until, why)
        return out

    def coin_risk(self, coin: str, now: float | None = None) -> str | None:
        x = self.risk(now).get(coin)
        return x[1] if x else None

    def mood(self, now: float | None = None) -> tuple[float, str, list[dict]]:
        """Настроение рынка по новостям за 6 ч: сумма ожидаемых ходов с затуханием по времени."""
        now = time.time() if now is None else now
        score = 0.0
        top: list[tuple[float, dict]] = []
        for e in self.st["events"]:
            if e.get("old") or e["cat"] == "other" or now - e["seen"] > MOOD_WINDOW:
                continue
            if e["scope"] != "market" and not set(e["coins"]) & COIN_SKIP:
                continue
            val = self.est(e["cat"], "1h")[0]
            w = 0.5 ** ((now - e["seen"]) / MOOD_HALF_LIFE) * min(1.5, 0.75 + 0.25 * len(e["srcs"]))
            score += val * w
            top.append((val * w, e))
        score = max(-10.0, min(10.0, score))
        label = ("🌩 шторм" if score <= STORM else "🌧 негатив" if score <= -1 else "☀️ позитив" if score >= 1
                 else "⛅ спокойно")
        top.sort(key=lambda x: x[0])
        return round(score, 2), label, [e for _, e in top[:3]]

    def macro_now(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        for m in self.macro:
            if m["at"] - MACRO_BEFORE <= now <= m["at"] + MACRO_AFTER:
                return m
        return None

    def upcoming(self, now: float | None = None, hours: float = 36) -> list[dict]:
        now = time.time() if now is None else now
        return [m for m in self.macro if now - MACRO_AFTER <= m["at"] <= now + hours * 3600]

    def pause(self, now: float | None = None) -> tuple[float, str] | None:
        """⏸ Новых покупок не открывать: макро-окно или шторм в новостях."""
        now = time.time() if now is None else now
        if not self.st["on"]:
            return None
        m = self.macro_now(now)
        if m:
            return m["at"] + MACRO_AFTER, f"⏰ {m['ru']} в {self.when(m['at'])}: цена в эти минуты скачет"
        st = self.st
        if float(st.get("storm_until") or 0) > now:
            return float(st["storm_until"]), st.get("storm_why") or "🌩 шторм в новостях"
        return None

    def _update_storm(self, now: float) -> str | None:
        score, _, top = self.mood(now)
        st = self.st
        if score > STORM:
            return None
        was = float(st.get("storm_until") or 0) > now
        head = top[0]["title"][:90] if top else ""
        st["storm_until"] = now + STORM_PAUSE_SEC
        st["storm_why"] = f"🌩 шторм в новостях ({score:+.1f}): {head}"
        self._changed()
        return None if was else st["storm_why"]

    def apply(self, now: float | None = None) -> None:
        """Передаёт щит и паузу автопилоту (bot/scalper.py). Спред и переводы спрашивают сами."""
        sc = getattr(self.b, "scalper", None)
        if sc is not None and hasattr(sc, "set_news"):
            sc.set_news(self.risk(now), self.pause(now))

    # --- ИИ ------------------------------------------------------------------------------------------

    async def ai_review(self, now: float) -> bool:
        ai = getattr(self.b, "ai", None)
        st = self.st
        if ai is None or not getattr(ai, "configured", False) or now - float(st["last_ai"] or 0) < AI_EVERY:
            return False
        batch = [e for e in st["events"] if not e.get("ai") and not e.get("old") and now - e["seen"] < OLD_SEC
                 and (e["imp"] >= 2 or e["cat"] == "other")][-AI_BATCH:]  # важное и то, что правила не поняли
        if not batch:
            return False
        st["last_ai"] = now
        cats = ", ".join(k for k in CATS if k != "listing_major")
        lines = "\n".join(f"{i}. {e['title']}" for i, e in enumerate(batch))
        ctx = "; ".join(h["title"] for h in self.headlines[-8:])
        messages = [
            {"role": "system", "content": "Ты аналитик крипторынка. Отвечай только JSON без пояснений."},
            {"role": "user", "content": (
                f"Заголовки новостей:\n{lines}\n\nЕщё заголовки для фона: {ctx[:800]}\n\n"
                "Для каждого заголовка верни: i, cat (одно из: " + cats + ", other), coins (тикеры монет, о которых "
                "новость, максимум 3), ru (перевод заголовка на русский, до 110 символов), why (как это может "
                "повлиять на цену, по-русски, до 120 символов, без обещаний). И digest: 2–3 предложения по-русски, "
                "что сейчас главное на крипторынке. Формат: "
                '{"items":[{"i":0,"cat":"hack","coins":["XRP"],"ru":"...","why":"..."}],"digest":"..."}')},
        ]
        try:
            from .mentors import parse_json
            raw = await asyncio.wait_for(ai.client.chat(messages, max_tokens=900, background=True), 90)
            data = parse_json(raw)
        except Exception as e:
            log.info("Новости, ИИ: %s", e)
            for ev in batch:
                ev["ai"] = 1  # не мучаем ИИ теми же заголовками снова
            return False
        known = self.known()
        for it in data.get("items") or []:
            try:
                ev = batch[int(it.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            ev["ai"] = 1
            cat = str(it.get("cat") or "")
            if cat in CATS and cat != ev["cat"]:
                if cat == "listing" and ev["cat"] == "listing_major":
                    pass
                else:
                    ev["rule"] = ev["cat"]
                    ev["cat"] = cat
                    ev["prior"] = CATS[cat][2]
            coins = [str(c).upper() for c in it.get("coins") or [] if str(c).upper() in known]
            for c in coins:
                if c not in ev["coins"] and len(ev["coins"]) < 4:
                    ev["coins"].append(c)
            if ev["coins"] and CATS.get(ev["cat"], ("", "", 0, "coin"))[3] == "coin":
                ev["scope"] = "coin"
            ev["ru"] = str(it.get("ru") or "")[:140]
            ev["why"] = str(it.get("why") or "")[:160]
            ev["imp"] = self._importance(ev)
        for ev in batch:
            ev["ai"] = 1
        digest = str(data.get("digest") or "").strip()
        if digest:
            st["digest"] = {"text": digest[:500], "at": now}
        self._changed()
        return True

    # --- цикл ----------------------------------------------------------------------------------------

    async def tick(self, now: float | None = None, force: bool = False) -> list[str]:
        """Один обход: собрать → разобрать → замерить → ИИ → применить. Возвращает уведомления."""
        now = time.time() if now is None else now
        if not self.st["on"] and not force:
            self.apply(now)
            return []
        async with self._lock:
            self.last_tick = now
            items = await self.collect(now, force)
            await self.refresh_side(now)
            new = self.ingest(items, now)
            await self.ai_review(now)  # до замера: ИИ может уточнить монеты новости
            try:
                await self.measure(new, now)
            except Exception as e:
                log.info("Новости: замер цен: %s", e)
            storm = self._update_storm(now)
            self.apply(now)
            return self._alerts(new, storm, now)

    def _alerts(self, new: list[dict], storm: str | None, now: float) -> list[str]:
        st = self.st
        if not st["alerts"]:
            return []
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if st["sent_day"][0] != day:
            st["sent_day"] = [day, 0]
        out: list[str] = []
        held = set()
        try:
            from .opps_ui import held_coins
            held = held_coins(self.b)
        except Exception:
            pass

        def room() -> bool:
            return st["sent_day"][1] + len(out) < ALERTS_PER_DAY

        if storm and room():
            out.append(f"{storm}\n\n⏸ Автопилот 2 ч не открывает новых покупок (открытые позиции ведутся как обычно). "
                       "Подробно: /news")
        for e in new:
            if not room():
                break
            mine = set(e["coins"]) & held
            proven = e["scope"] == "coin" and self.proven(e["cat"]) and not set(e["coins"]) & COIN_SKIP
            if not (e["imp"] >= 3 and (mine or e["scope"] == "market")) and not proven:
                continue
            val, n, up = self.est(e["cat"], "4h")
            head = "⚡ <b>Доказанный сигнал по новости</b>" if proven else (
                "🚨 <b>Важная новость по твоей монете</b>" if mine else "📰 <b>Важная новость для рынка</b>")
            fact = (f"по фактам ({n:.0f} случаев) такие новости дают {val:+.1f}% за 4 ч, рост в {up * 100:.0f}% случаев"
                    if n else f"обычно {val:+.1f}% за 4 ч (пока мало своих замеров)")
            act = ""
            if e["coins"] and val <= SHIELD_PCT:
                act = "\n🛡 Автопилот эту монету пока не покупает, спред её не трогает."
            elif proven:
                act = "\n🧪 Бот проверяет её бумажной сделкой. Вживую по новостям он сам не покупает."
            out.append(f"{head}\n{CATS[e['cat']][0]} {html.escape(e.get('ru') or e['title'])}\n"
                       f"<i>{html.escape(self._srcs(e))}</i> · {fact}{act}")
        for m in self.upcoming(now, hours=1):
            told = st["told"]
            if m["id"] in told or not (0 < m["at"] - now <= 3600) or not room():
                continue
            told[m["id"]] = now
            out.append(f"⏰ <b>Через {max(1, round((m['at'] - now) / 60))} мин: {html.escape(m['ru'])}</b>\n"
                       f"За 30 мин до и 45 мин после бот не открывает новых покупок и спред-сделок: цена в эти минуты "
                       "скачет в обе стороны." + (f" Прогноз: {html.escape(m['forecast'])}, было: "
                                                   f"{html.escape(m['previous'])}." if m.get("forecast") else ""))
        st["told"] = {k: v for k, v in st["told"].items() if now - v < 7 * 86400}
        st["sent_day"][1] += len(out)
        return out

    # --- тексты --------------------------------------------------------------------------------------

    def _srcs(self, e: dict) -> str:
        names = {s.key: s.name for s in (*SOURCES, CRYPTOPANIC)}
        first = names.get(e["src"], e["src"])
        more = len(e["srcs"]) - 1
        return first + (f" +{more} ист." if more > 0 else "")

    def _ev_line(self, e: dict, full: bool = False) -> str:
        icon = CATS[e["cat"]][0] if e["cat"] in CATS else "•"
        val, n, _ = self.est(e["cat"], "4h") if e["cat"] in CATS else (0.0, 0, 0.0)
        coins = f"<b>{html.escape(', '.join(e['coins']))}</b>: " if e["coins"] else ""
        title = html.escape((e.get("ru") or e["title"])[:150])
        tail = f" → обычно {val:+.1f}% за 4 ч" + (f" ({n:.0f} замеров)" if n else "") if e["cat"] in CATS else ""
        line = f"{icon} {coins}{title} <i>({html.escape(self._srcs(e))}, {self.when(e['t'])}{tail})</i>"
        if full:
            res = e["res"].get("4h") or e["res"].get("1h") or e["res"].get("15m")
            if res:
                h = "4h" if "4h" in e["res"] else "1h" if "1h" in e["res"] else "15m"
                line += " · факт " + ", ".join(f"{c} {v[0]:+.1f}%" for c, v in res.items()) + f" за {h}"
            if e.get("why"):
                line += f"\n   🤖 {html.escape(e['why'])}"
        return line

    def working(self) -> tuple[int, int]:
        rows = [h for k, h in self.health.items()]
        ok = sum(1 for h in rows if not h.get("fail"))
        return ok, max(len(self.sources()) + 2, len(rows))

    def panel_text(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        st = self.st
        score, label, _ = self.mood(now)
        lines = ["📰 <b>Новости → рынок</b>" + ("" if st["on"] else " · ⏸ выключено (/news on)")]
        fng = f" · страх/жадность: <b>{self.fng['value']}</b> ({html.escape(self.fng['label'])})" if self.fng else ""
        lines.append(f"Настроение по новостям: <b>{label}</b> ({score:+.1f}){fng}")
        p = self.pause(now)
        if p:
            lines.append(f"⏸ <b>Новые покупки на паузе до {self.when(p[0])}</b>: {html.escape(p[1])}")
        up = [m for m in self.upcoming(now, 36) if m["at"] > now]
        if up:
            lines.append("⏰ Важные данные США: " + "; ".join(f"{self.when(m['at'])} — {html.escape(m['ru'])}"
                                                            for m in up[:3]))
        risk = self.risk(now)
        if risk:
            lines.append(f"\n🛡 <b>Щит по новостям</b> ({len(risk)}): не покупаю, спред не трогает")
            for c, (until, why) in sorted(risk.items(), key=lambda kv: kv[1][0])[:5]:
                lines.append(f"• <b>{html.escape(c)}</b> до {self.when(until)} — {html.escape(why[2:110])}")
        dg = st["digest"]
        if dg.get("text") and now - float(dg.get("at") or 0) < 6 * 3600:
            lines.append(f"\n🤖 <b>Сводка ИИ</b> ({self.when(dg['at'])}): {html.escape(dg['text'])}")
        evs = [e for e in st["events"] if not e.get("old") and now - e["seen"] < 12 * 3600 and e["imp"] >= 1]
        evs.sort(key=lambda e: (-e["imp"], -e["seen"]))
        if evs:
            lines.append("\n<b>Главное за 12 ч</b>:")
            lines += [self._ev_line(e) for e in evs[:7]]
        else:
            lines.append("\nВажных новостей за 12 ч нет." if st["events"] else
                         "\nСобираю новости: первые появятся через пару минут.")
        lines.append("\n" + self.learn_short())
        ok, total = self.working()
        lines.append(f"📡 Источников работает: {ok} из {total} · заголовков прочитано: {st.get('total', 0)}")
        lines.append("<i>Новость — не сигнал «покупай»: к публикации цена часто уже сдвинулась. Бот учится по фактам "
                     "и сам защищает деньги: щит, пауза в шторм и вокруг данных США.</i>")
        return "\n".join(lines)

    def learn_rows(self) -> list[dict]:
        rows = []
        for cat, (icon, name, prior, _, _) in CATS.items():
            val, n, up = self.est(cat, "4h")
            v1, n1, _ = self.est(cat, "1h")
            rows.append({"cat": cat, "icon": icon, "name": name, "prior": prior, "est": round(val, 2), "n": int(n),
                         "up": round(up, 2), "est_1h": round(v1, 2), "n_1h": int(n1), "proven": self.proven(cat),
                         "shield": val <= SHIELD_PCT})
        rows.sort(key=lambda r: (-r["n"], -abs(r["est"])))
        return rows

    def learn_short(self) -> str:
        st = self.st
        rows = [r for r in self.learn_rows() if r["n"]]
        pp = st["paper"]
        sh = st["shield"]
        parts = [f"🧠 Замеров влияния: <b>{st.get('measured', 0)}</b>"]
        if rows:
            parts.append(" · " + ", ".join(f"{r['icon']} {r['name']} {r['est']:+.1f}% ({r['n']})" for r in rows[:3]))
        if sh["n"]:
            parts.append(f"\n🛡 Щит угадал падение в {sh['right']} из {sh['n']} случаев")
        if pp["n"]:
            parts.append(f"\n🧪 Бумажные сделки по новостям: {pp['n']}, в плюс {pp['wins']}, в среднем "
                         f"{pp['sum'] / pp['n']:+.2f}% (после комиссий)")
        return "".join(parts)

    def learn_text(self) -> str:
        lines = ["🧠 <b>Чему бот научился на новостях</b>",
                 "Ход цены за 4 ч после того, как бот увидел новость (для монет — относительно BTC). Пока замеров "
                 "мало, держится осторожное стартовое ожидание; каждый замер сдвигает его к фактам.\n"]
        for r in self.learn_rows():
            mark = " ⚡ доказано" if r["proven"] else " 🛡 щит" if r["shield"] else ""
            facts = (f"{r['n']} замеров, рост в {r['up'] * 100:.0f}%" if r["n"] else "своих замеров нет")
            lines.append(f"{r['icon']} {r['name']}: <b>{r['est']:+.1f}%</b> (старт {r['prior']:+.1f}%; {facts}){mark}")
        srcs = []
        names = {s.key: s.name for s in (*SOURCES, CRYPTOPANIC)}
        for key, row in self.st["stats"].items():
            if key.startswith("src:") and row.get("4h"):
                n, s, _, _ = row["4h"]
                if n >= 3:
                    srcs.append((names.get(key[4:], key[4:]), n, s / n))
        if srcs:
            srcs.sort(key=lambda x: -abs(x[2]))
            lines.append("\n<b>Источники, после которых цена двигалась сильнее всего:</b>")
            lines += [f"• {html.escape(nm)}: {avg:+.2f}% в среднем ({n:.0f})" for nm, n, avg in srcs[:6]]
        lines.append("\n⚡ «доказано» — ≥12 замеров, в среднем ≥ +1.5% и рост в ≥60% случаев: о таких новостях бот "
                     "пишет сразу. 🛡 «щит» — по таким новостям монету не покупаем.")
        lines.append("\n" + self.learn_short())
        return "\n".join(lines)

    def src_text(self) -> str:
        lines = ["📡 <b>Источники новостей</b> (бесплатные, без ключей)"]
        for s in self.sources():
            h = self.health.get(s.key)
            if not h:
                mark, info = "⏳", "ещё не опрашивал"
            elif h.get("fail"):
                mark, info = "❌", f"{html.escape(h['err'][:70])}; повтор в {self.when(h['next'])}"
            else:
                mark, info = "✅", f"{h['items']} заголовков, {self.when(h['at'])}"
            lines.append(f"{mark} {html.escape(s.name)} — {info}")
        for key in ("fng", "ffc"):
            h = self.health.get(key)
            if h:
                lines.append(f"{'❌' if h.get('fail') else '✅'} {html.escape(h['name'])}"
                             + (f" — {html.escape(h['err'][:70])}" if h.get("fail") else ""))
        if not os.getenv("CRYPTOPANIC_TOKEN", "").strip():
            lines.append("\n➕ Можно добавить CryptoPanic (агрегатор сотен источников): бесплатный ключ на "
                         "cryptopanic.com → в Render переменная <code>CRYPTOPANIC_TOKEN</code>.")
        lines.append("\nИсточник, который не отвечает, сам уходит на паузу (до 6 ч) и не мешает остальным.")
        return "\n".join(lines)

    def all_text(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        evs = [e for e in self.st["events"] if now - e["seen"] < 24 * 3600]
        evs.sort(key=lambda e: -e["t"])
        if not evs:
            return "📋 За сутки событий пока нет."
        return "📋 <b>Новости за сутки</b> (с замерами)\n\n" + "\n".join(self._ev_line(e, True) for e in evs[:15])

    def status_dict(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        st = self.st
        score, label, _ = self.mood(now)
        p = self.pause(now)
        ok, total = self.working()
        evs = [e for e in st["events"] if not e.get("old") and now - e["seen"] < 24 * 3600]
        evs.sort(key=lambda e: (-e["imp"], -e["seen"]))

        def ev(e: dict) -> dict:
            val, n, _ = self.est(e["cat"], "4h") if e["cat"] in CATS else (0.0, 0, 0.0)
            res = e["res"].get("4h") or e["res"].get("1h") or e["res"].get("15m") or {}
            return {"title": e.get("ru") or e["title"], "orig": e["title"], "url": e.get("url", ""),
                    "src": self._srcs(e), "t": e["t"], "cat": e["cat"],
                    "icon": CATS[e["cat"]][0] if e["cat"] in CATS else "•",
                    "cat_name": CATS[e["cat"]][1] if e["cat"] in CATS else "", "coins": e["coins"], "imp": e["imp"],
                    "est": round(val, 2), "n": int(n), "why": e.get("why", ""),
                    "fact": {c: v[0] for c, v in res.items()}}

        pp = st["paper"]
        return {"on": st["on"], "alerts": st["alerts"], "mood": score, "mood_label": label, "fng": self.fng,
                "pause": {"until": p[0], "why": p[1]} if p else None,
                "macro": [{"at": m["at"], "ru": m["ru"], "forecast": m.get("forecast", "")}
                          for m in self.upcoming(now, 36) if m["at"] > now][:5],
                "shield": [{"coin": c, "until": u, "why": w} for c, (u, w) in sorted(self.risk(now).items())][:12],
                "events": [ev(e) for e in evs[:20]], "learn": [r for r in self.learn_rows() if r["n"]][:12],
                "measured": st.get("measured", 0), "total": st.get("total", 0),
                "paper": {"n": pp["n"], "wins": pp["wins"], "avg": round(pp["sum"] / pp["n"], 3) if pp["n"] else 0.0,
                          "open": len(pp["open"])},
                "shield_score": dict(st["shield"]), "digest": st["digest"], "sources": {"ok": ok, "total": total},
                "updated_at": self.last_tick}

    def context(self, now: float | None = None) -> dict:
        """Коротко для ИИ-ассистента."""
        now = time.time() if now is None else now
        d = self.status_dict(now)
        return {"how": "/news: бот сам читает ~30 источников, меряет влияние новостей на цену и учится; щит монет после "
                       "плохих новостей, пауза покупок в шторм и вокруг важных данных США",
                "mood": d["mood_label"], "pause": d["pause"], "macro_next": d["macro"][:2], "shield": d["shield"][:5],
                "top_news": [{"title": e["title"][:120], "coins": e["coins"], "type": e["cat_name"],
                              "usual_move_4h_pct": e["est"]} for e in d["events"][:6]],
                "learned": [{"type": r["name"], "move_4h_pct": r["est"], "samples": r["n"]} for r in d["learn"][:6]],
                "digest": (d["digest"] or {}).get("text", "")}


def paper_rets(b) -> list[float]:
    return [float(c["pct"]) for c in b.news.st["paper"]["closed"]]


async def news_loop(b: ArbitrageBot) -> None:
    await asyncio.sleep(45)
    while True:
        b._beats["news"] = time.time()
        try:
            for text in await b.news.tick():
                await b.notify(text)
        except Exception:
            log.exception("Ошибка новостей")
        await asyncio.sleep(TICK_SEC)

