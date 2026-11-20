"""Мемкоины и DEX: DexScreener (цены, ликвидность, тренды) + RugCheck (проверка токена Solana на скам).

Всё через публичные API без ключей и без доступа к деньгам:
  • DexScreener: https://docs.dexscreener.com/api/reference (поиск и пары 300 запросов/мин, тренды 60/мин)
  • RugCheck: https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary

Купить/продать мемкоин бот НЕ может: у GMGN, Axiom и FOMO нет официального API для торговли, а торговля
с кошелька требует приватного ключа, который боту давать нельзя. Поэтому бот находит и проверяет токен,
а открыть его в GMGN / Axiom / Phantom можно по ссылке в одно касание.
"""

from __future__ import annotations

import asyncio
import html
import logging
import math
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

DEX_API = "https://api.dexscreener.com"
RUG_API = "https://api.rugcheck.xyz/v1"
CACHE_SEC = 30
TIMEOUT = aiohttp.ClientTimeout(total=15)
SOL_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

CHAIN_TITLES = {"solana": "Solana", "ethereum": "Ethereum", "bsc": "BSC", "base": "Base", "arbitrum": "Arbitrum",
                "polygon": "Polygon", "ton": "TON", "sui": "Sui", "tron": "Tron", "avalanche": "Avalanche"}
GMGN_CHAINS = {"solana": "sol", "ethereum": "eth", "bsc": "bsc", "base": "base", "tron": "tron"}

# Фильтры «радара» (новые трендовые токены Solana, прошедшие базовую проверку)
RADAR_MIN_LIQUIDITY = 30_000
RADAR_MIN_VOLUME = 100_000
RADAR_MAX_AGE_H = 72
RADAR_MIN_AGE_H = 1
RADAR_MAX_RUG_SCORE = 30  # RugCheck score_normalised: 0–30 хорошо, 30–60 осторожно, 60+ опасно


class DexError(Exception):
    """Понятная ошибка DexScreener / RugCheck."""


def is_address(text: str) -> bool:
    text = (text or "").strip()
    return bool(SOL_ADDRESS_RE.match(text) or EVM_ADDRESS_RE.match(text))


def _f(value) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


@dataclass
class Pair:
    chain: str
    dex: str
    url: str
    pair_address: str
    symbol: str
    name: str
    address: str  # адрес токена (mint)
    quote: str
    price_usd: float | None
    change: dict[str, float | None] = field(default_factory=dict)  # m5, h1, h6, h24 в %
    liquidity_usd: float | None = None
    volume_h24: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    created_at: float | None = None  # секунды
    buys_h24: int = 0
    sells_h24: int = 0
    buys_h1: int = 0
    sells_h1: int = 0
    image: str | None = None
    boosts: int = 0

    @classmethod
    def from_json(cls, d: dict) -> Pair:
        base, quote = d.get("baseToken") or {}, d.get("quoteToken") or {}
        txns = d.get("txns") or {}
        created = _f(d.get("pairCreatedAt"))
        return cls(
            chain=str(d.get("chainId") or ""), dex=str(d.get("dexId") or ""), url=str(d.get("url") or ""),
            pair_address=str(d.get("pairAddress") or ""), symbol=str(base.get("symbol") or "?"),
            name=str(base.get("name") or ""), address=str(base.get("address") or ""),
            quote=str(quote.get("symbol") or ""), price_usd=_f(d.get("priceUsd")),
            change={k: _f((d.get("priceChange") or {}).get(k)) for k in ("m5", "h1", "h6", "h24")},
            liquidity_usd=_f((d.get("liquidity") or {}).get("usd")), volume_h24=_f((d.get("volume") or {}).get("h24")),
            fdv=_f(d.get("fdv")), market_cap=_f(d.get("marketCap")),
            created_at=created / 1000 if created else None,
            buys_h24=int((txns.get("h24") or {}).get("buys") or 0), sells_h24=int((txns.get("h24") or {}).get("sells") or 0),
            buys_h1=int((txns.get("h1") or {}).get("buys") or 0), sells_h1=int((txns.get("h1") or {}).get("sells") or 0),
            image=(d.get("info") or {}).get("imageUrl"), boosts=int((d.get("boosts") or {}).get("active") or 0),
        )

    def age_hours(self, now: float | None = None) -> float | None:
        if not self.created_at:
            return None
        return max(0.0, ((time.time() if now is None else now) - self.created_at) / 3600)

    def as_dict(self, rug: dict | None = None) -> dict:
        v = verdict(self, rug)
        return {
            "chain": self.chain, "chain_title": CHAIN_TITLES.get(self.chain, self.chain), "dex": self.dex,
            "symbol": self.symbol, "name": self.name, "address": self.address, "pair": self.pair_address,
            "quote": self.quote, "price": self.price_usd, "change": self.change, "liquidity": self.liquidity_usd,
            "volume": self.volume_h24, "fdv": self.fdv, "mcap": self.market_cap, "age_h": self.age_hours(),
            "buys": self.buys_h24, "sells": self.sells_h24, "image": self.image, "boosts": self.boosts,
            "links": links(self), "flags": risk_flags(self, rug), "verdict": v[0], "verdict_text": v[1],
            "rug": rug,
        }


def links(p: Pair) -> dict[str, str]:
    out = {"dexscreener": p.url or f"https://dexscreener.com/{p.chain}/{p.pair_address}"}
    if p.chain in GMGN_CHAINS and p.address:
        out["gmgn"] = f"https://gmgn.ai/{GMGN_CHAINS[p.chain]}/token/{p.address}"
    if p.chain == "solana":
        if p.pair_address:
            out["axiom"] = f"https://axiom.trade/meme/{p.pair_address}"
        if p.address:
            out["rugcheck"] = f"https://rugcheck.xyz/tokens/{p.address}"
            out["solscan"] = f"https://solscan.io/token/{p.address}"
    return out


def risk_flags(p: Pair, rug: dict | None = None, now: float | None = None) -> list[str]:
    """Простые красные флаги по данным пары. Это не гарантия: отсутствие флагов не значит «безопасно»."""
    flags = []
    liq = p.liquidity_usd or 0
    if liq < 10_000:
        flags.append(f"мало ликвидности (${fmt_usd(liq)}): крупную продажу пул не выдержит")
    age = p.age_hours(now)
    if age is not None and age < 24:
        flags.append(f"токену {age:.0f} ч: самый опасный возраст (резкие сливы)" if age >= 1 else "токену меньше часа")
    if p.buys_h24 >= 30 and p.sells_h24 == 0:
        flags.append("покупки есть, продаж нет: возможно, продать нельзя (honeypot)")
    if p.fdv and liq and p.fdv / liq > 100:
        flags.append("оценка в 100+ раз больше ликвидности: цена «бумажная»")
    ch24 = p.change.get("h24")
    if ch24 is not None and ch24 > 300:
        flags.append(f"+{ch24:.0f}% за сутки: вход на пике, высок риск отката")
    if ch24 is not None and ch24 < -60:
        flags.append(f"{ch24:.0f}% за сутки: токен сливают")
    if rug:
        for r in rug.get("risks", [])[:4]:
            if r.get("level") == "danger":
                flags.append(f"RugCheck: {r.get('name')}")
    return flags


def verdict(p: Pair, rug: dict | None = None) -> tuple[str, str]:
    """(green|yellow|red, текст)."""
    score = (rug or {}).get("score")
    danger = any(r.get("level") == "danger" for r in (rug or {}).get("risks", []))
    flags = risk_flags(p, rug)
    if (score is not None and score >= 60) or danger or (p.liquidity_usd or 0) < 5_000:
        return "red", "🔴 высокий риск"
    if (score is not None and score >= 30) or len(flags) >= 2:
        return "yellow", "🟡 осторожно"
    if score is None and p.chain == "solana":
        return "yellow", "🟡 без проверки RugCheck"
    return "green", "🟢 явных красных флагов нет"


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "?"
    v = float(v)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:.0f}"


def fmt_token_price(v: float | None) -> str:
    if v is None:
        return "?"
    if v >= 1:
        return f"${v:,.4f}".rstrip("0").rstrip(".")
    if v <= 0:
        return "$0"
    lead = max(0, -math.floor(math.log10(v)) - 1)  # нули после запятой: 0.0000123 → 4
    return "$" + f"{v:.{lead + 4}f}".rstrip("0").rstrip(".")


def fmt_age(h: float | None) -> str:
    if h is None:
        return "?"
    if h < 1:
        return f"{h * 60:.0f} мин"
    if h < 48:
        return f"{h:.0f} ч"
    return f"{h / 24:.0f} дн"


def pair_text(p: Pair, rug: dict | None = None, detailed: bool = True) -> str:
    title = CHAIN_TITLES.get(p.chain, p.chain)
    ch = p.change
    changes = " · ".join(f"{k} {v:+.1f}%" for k, v in ch.items() if v is not None and k in ("h1", "h24"))
    lines = [f"<b>{html.escape(p.symbol)}</b> {html.escape(p.name[:30])} · {html.escape(title)} · {html.escape(p.dex)}",
             f"Цена {fmt_token_price(p.price_usd)}" + (f" ({changes})" if changes else ""),
             f"Ликвидность ${fmt_usd(p.liquidity_usd)} · объём 24ч ${fmt_usd(p.volume_h24)} · "
             f"капитализация ${fmt_usd(p.market_cap or p.fdv)} · возраст {fmt_age(p.age_hours())}"]
    if detailed:
        lines.append(f"Сделки 24ч: 🟢 {p.buys_h24} покупок / 🔴 {p.sells_h24} продаж")
        if rug:
            lines.append(f"RugCheck: риск {rug['score']}/100 ({rug['label']})"
                         + (f", ликвидность заблокирована на {rug['lp_locked']:.0f}%" if rug.get("lp_locked") is not None else ""))
        lines.append(f"Вердикт: {verdict(p, rug)[1]}")
        for f in risk_flags(p, rug)[:5]:
            lines.append(f"  ⚠️ {html.escape(f)}")
        lines.append(f"<code>{html.escape(p.address)}</code>")
    lk = links(p)
    names = {"dexscreener": "DexScreener", "gmgn": "GMGN", "axiom": "Axiom", "rugcheck": "RugCheck", "solscan": "Solscan"}
    lines.append(" · ".join(f'<a href="{html.escape(u)}">{names[k]}</a>' for k, u in lk.items()))
    return "\n".join(lines)


def best_pairs(pairs: list[Pair], limit: int = 5) -> list[Pair]:
    """По одной паре на токен (с самой большой ликвидностью), отсортировано по ликвидности."""
    best: dict[tuple[str, str], Pair] = {}
    for p in pairs:
        key = (p.chain, p.address.lower() if p.chain != "solana" else p.address)
        if key not in best or (p.liquidity_usd or 0) > (best[key].liquidity_usd or 0):
            best[key] = p
    return sorted(best.values(), key=lambda p: -(p.liquidity_usd or 0))[:limit]


def parse_rug(data: dict) -> dict:
    score = data.get("score_normalised")
    if score is None:
        raw = _f(data.get("score"))
        score = min(100, raw / 100) if raw is not None else None  # старый формат: score в «сырых» баллах
    score = int(round(float(score))) if score is not None else None
    label = "?" if score is None else "низкий" if score < 30 else "средний" if score < 60 else "высокий"
    risks = [{"name": str(r.get("name") or ""), "level": str(r.get("level") or ""),
              "description": str(r.get("description") or "")[:160], "value": str(r.get("value") or "")[:40]}
             for r in (data.get("risks") or []) if isinstance(r, dict)]
    lp = _f(data.get("lpLockedPct"))
    return {"score": score, "label": label, "risks": risks, "lp_locked": lp}


class DexClient:
    def __init__(self, dex_base: str = DEX_API, rug_base: str = RUG_API) -> None:
        self.dex_base = dex_base.rstrip("/")
        self.rug_base = rug_base.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, object]] = {}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def clear_cache(self) -> int:
        n = len(self._cache)
        self._cache.clear()
        return n

    async def _get(self, url: str, ttl: float = CACHE_SEC):
        cached = self._cache.get(url)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers={"User-Agent": "ai-casher-bot/1.0"})
        try:
            async with self._session.get(url, timeout=TIMEOUT) as r:
                if r.status == 429:
                    raise DexError("сервис просит подождать (слишком много запросов), попробуй через минуту")
                if r.status == 404:
                    data = None
                elif r.status >= 400:
                    raise DexError(f"сервис ответил ошибкой {r.status}")
                else:
                    data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise DexError(f"нет связи: {type(e).__name__}") from None
        self._cache[url] = (time.time(), data)
        if len(self._cache) > 300:
            for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:100]:
                self._cache.pop(k, None)
        return data

    async def search(self, query: str) -> list[Pair]:
        data = await self._get(f"{self.dex_base}/latest/dex/search?q={quote(query.strip())}")
        return [Pair.from_json(d) for d in ((data or {}).get("pairs") or []) if isinstance(d, dict)]

    async def tokens(self, chain: str, addresses: list[str]) -> list[Pair]:
        out: list[Pair] = []
        for i in range(0, len(addresses), 30):
            chunk = ",".join(addresses[i:i + 30])
            data = await self._get(f"{self.dex_base}/tokens/v1/{chain}/{chunk}")
            out += [Pair.from_json(d) for d in (data or []) if isinstance(d, dict)]
        return out

    async def token_prices(self, chain: str, addresses: list[str]) -> dict[str, tuple[str, float]]:
        """{адрес: (тикер, цена $)} по самой ликвидной паре: для оценки мемкоинов в кошельке Phantom."""
        if not addresses:
            return {}
        out: dict[str, tuple[str, float]] = {}
        for p in best_pairs(await self.tokens(chain, addresses), limit=1000):
            if p.price_usd and (p.liquidity_usd or 0) >= 1000:
                out[p.address] = (p.symbol, p.price_usd)
        return out

    async def trending(self, chain: str | None = "solana", limit: int = 8) -> list[Pair]:
        """Токены, которые сейчас продвигают и обсуждают (бусты + свежие профили DexScreener)."""
        boosts, profiles = await asyncio.gather(
            self._get(f"{self.dex_base}/token-boosts/top/v1", ttl=120),
            self._get(f"{self.dex_base}/token-profiles/latest/v1", ttl=120),
            return_exceptions=True,
        )
        by_chain: dict[str, list[str]] = {}
        for src in (boosts, profiles):
            if isinstance(src, BaseException) or not src:
                continue
            for item in src if isinstance(src, list) else []:
                c, a = item.get("chainId"), item.get("tokenAddress")
                if not c or not a or (chain and c != chain):
                    continue
                lst = by_chain.setdefault(c, [])
                if a not in lst:
                    lst.append(a)
        if not by_chain:
            if isinstance(boosts, BaseException):
                raise boosts
            return []
        pairs: list[Pair] = []
        for c, addrs in by_chain.items():
            pairs += await self.tokens(c, addrs[:60])
        return sorted(best_pairs(pairs, limit=200), key=lambda p: -(p.volume_h24 or 0))[:limit]

    async def rugcheck(self, mint: str) -> dict | None:
        if not SOL_ADDRESS_RE.match(mint or ""):
            return None
        data = await self._get(f"{self.rug_base}/tokens/{mint}/report/summary", ttl=600)
        return parse_rug(data) if isinstance(data, dict) else None

    async def rug_safe(self, mint: str) -> dict | None:
        try:
            return await self.rugcheck(mint)
        except DexError as e:
            log.info("RugCheck %s: %s", mint, e)
            return None

    async def radar(self, now: float | None = None) -> list[tuple[Pair, dict]]:
        """Новые трендовые токены Solana, прошедшие фильтры по ликвидности, объёму, возрасту и RugCheck."""
        now = time.time() if now is None else now
        found = []
        for p in await self.trending("solana", limit=40):
            age = p.age_hours(now)
            if (p.liquidity_usd or 0) < RADAR_MIN_LIQUIDITY or (p.volume_h24 or 0) < RADAR_MIN_VOLUME:
                continue
            if age is None or not (RADAR_MIN_AGE_H <= age <= RADAR_MAX_AGE_H):
                continue
            if p.sells_h24 == 0:
                continue
            found.append(p)
        rugs = await asyncio.gather(*(self.rug_safe(p.address) for p in found[:8]))
        return [(p, r) for p, r in zip(found[:8], rugs)
                if r is not None and r["score"] is not None and r["score"] <= RADAR_MAX_RUG_SCORE
                and not any(x["level"] == "danger" for x in r["risks"])]


DEX_HELP = """\
🦎 <b>Мемкоины и DEX</b> (DexScreener + RugCheck)

<code>/dex BONK</code>: найти токен по названию
<code>/dex АДРЕС</code>: токен по адресу (mint) из GMGN, Axiom, FOMO или Phantom
<code>/rug АДРЕС</code>: проверка токена Solana на скам (RugCheck)
<code>/hot</code>: что сейчас в тренде на Solana
<code>/radar on</code>: присылать новые трендовые токены, прошедшие фильтры

Под каждым токеном ссылки на DexScreener, GMGN и Axiom: открыть и купить там в одно касание.
⚠️ Сам бот мемкоины не покупает: у GMGN, Axiom и FOMO нет официального API торговли, а для торговли
с кошелька нужен приватный ключ, который боту давать нельзя. Большинство мемкоинов падает почти до нуля,
заходи только суммой, которую не жалко потерять."""
