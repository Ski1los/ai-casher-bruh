"""GMGN OpenAPI — официальный API GMGN (github.com/GMGNAI/gmgn-skills), только ЧТЕНИЕ рынка.

Что берём (без приватного ключа и без доступа к деньгам — достаточно бесплатного GMGN_API_KEY с gmgn.ai/ai):
  * /v1/market/rank      — тренды (самые торгуемые токены за 1м/5м/1ч…), с цифрами безопасности
  * /v1/trenches         — «окопы»: новые токены, почти завершившие bonding curve, и только что вышедшие на DEX.
                           Это те же три колонки, что Pulse в Axiom (New Pairs / Final Stretch / Migrated)
  * /v1/user/smartmoney  — сделки «умных» кошельков (GMGN отбирает их по истории прибыли)
  * /v1/user/kol         — сделки известных трейдеров-инфлюенсеров (KOL)
  * /v1/callout/global   — свежие «коллы» (кто какой токен сейчас рекламирует)
  * /v1/token/security   — холдеры, топ-10, доля разработчика, снайперы, бандлы, инсайдеры, риск rug

Авторизация «exist»: заголовок X-APIKEY + в запросе timestamp (unix, ±5 с) и client_id (UUID, повтор в течение
7 с отклоняется). Лимиты — «дырявое ведро» (бесплатно 5 единиц/с, у запросов разный вес). На 429 сервер сообщает,
когда можно снова (X-RateLimit-Reset / reset_at), а повторы во время бана продлевают его — поэтому мы честно
молчим до этого времени. Торговые методы GMGN (swap, follow_wallet) требуют отдельного ключа подписи — бот их
НЕ использует.

У Axiom открытого API нет (неофициальные библиотеки входят в аккаунт пользователя — так делать нельзя), поэтому
Pulse бот повторяет через GMGN: данные те же, они с блокчейна.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid

import aiohttp

log = logging.getLogger(__name__)

GMGN_HOST = "https://openapi.gmgn.ai"
TEST_KEY = "gmgn_solbscbaseethmonadtron"  # публичный тестовый ключ из документации GMGN: только для проверки
UA = "ai-casher-bot/1.0 (+gmgn-openapi)"
TIMEOUT = aiohttp.ClientTimeout(total=20)


class GmgnError(Exception):
    pass


def _num(v) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e15 else None


def _first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _rate(d: dict, *keys) -> float | None:
    """Доля 0–1 (GMGN отдаёт доли; на всякий случай 0–100 тоже понимаем)."""
    v = _num(_first(d, *keys))
    if v is None:
        return None
    if v > 1.0:
        v = v / 100
    return max(0.0, min(1.0, v))


def norm_token(raw: dict, category: str = "") -> dict | None:
    """Токен из тренда/окопов GMGN → единый вид (поля в разных ответах называются чуть по-разному)."""
    if not isinstance(raw, dict):
        return None
    addr = str(_first(raw, "address", "token_address", "base_address") or "").strip()
    if not addr:
        return None
    created = _num(_first(raw, "creation_timestamp", "created_timestamp", "open_timestamp"))
    if created and created > 1e12:  # миллисекунды
        created /= 1000
    status = str(raw.get("creator_token_status") or "")
    wash = raw.get("is_wash_trading")
    return {
        "mint": addr, "symbol": str(raw.get("symbol") or "")[:16], "category": category,
        "price": _num(raw.get("price")), "mcap": _num(_first(raw, "market_cap", "usd_market_cap")),
        "liq": _num(raw.get("liquidity")), "vol": _num(_first(raw, "volume", "volume_1h", "volume_24h")),
        "holders": _num(raw.get("holder_count")),
        "top10": _rate(raw, "top_10_holder_rate", "top_holder_rate"),
        "dev": _rate(raw, "dev_team_hold_rate", "creator_balance_rate", "creator_hold_rate"),
        "snipers": _rate(raw, "top70_sniper_hold_rate"),
        "bundler": _rate(raw, "bundler_rate", "bundler_trader_amount_rate"),
        "insider": _rate(raw, "rat_trader_amount_rate", "insider_ratio", "suspected_insider_hold_rate"),
        "rug": _rate(raw, "rug_ratio"),
        "smart": _num(raw.get("smart_degen_count")), "kol": _num(raw.get("renowned_count")),
        "created": created, "chg5m": _num(raw.get("price_change_percent5m")),
        "chg1h": _num(raw.get("price_change_percent1h")), "buys": _num(raw.get("buys")),
        "sells": _num(raw.get("sells")), "progress": _rate(raw, "progress"),
        "dev_sold": True if (status == "creator_close" or raw.get("creator_close") is True) else (
            False if status in ("creator_hold", "hold") else None),
        "wash": bool(wash) if isinstance(wash, bool) else str(wash).lower() == "true",
        "honeypot": str(raw.get("is_honeypot") or "0") == "1",
        "twitter": str(raw.get("twitter_username") or "")[:40],
    }


def norm_security(raw: dict) -> dict:
    """Ответ /v1/token/security (+ stat из /v1/token/info, если есть) → цифры безопасности."""
    raw = raw if isinstance(raw, dict) else {}
    stat = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
    both = {**stat, **raw}
    status = str(both.get("creator_token_status") or "")
    return {
        "top10": _rate(both, "top_10_holder_rate"),
        "dev": _rate(both, "dev_team_hold_rate", "creator_balance_rate", "creator_hold_rate"),
        "insider": _rate(both, "suspected_insider_hold_rate", "rat_trader_amount_rate"),
        "bundler": _rate(both, "bundler_trader_amount_rate", "bundler_rate"),
        "rug": _rate(both, "rug_ratio"), "snipers_n": _num(both.get("sniper_count")),
        "holders": _num(both.get("holder_count")),
        "dev_sold": True if status == "creator_close" else (False if status in ("creator_hold", "hold") else None),
        "honeypot": str(both.get("is_honeypot") or "0") == "1",
        "mint_ok": None if both.get("renounced_mint") is None else str(both.get("renounced_mint")) == "1",
        "freeze_ok": None if both.get("renounced_freeze_account") is None else
        str(both.get("renounced_freeze_account")) == "1",
    }


class GmgnClient:
    """Минимальный клиент GMGN OpenAPI (только чтение). Без ключа — configured=False и ни одного запроса."""

    def __init__(self, key: str | None = None, host: str = GMGN_HOST, session_factory=None) -> None:
        self.key = (key if key is not None else os.environ.get("GMGN_API_KEY", "")).strip()
        self.host = host.rstrip("/")
        self._factory = session_factory
        self._http: aiohttp.ClientSession | None = None
        self.blocked_until = 0.0        # 429: до этого времени не шлём НИЧЕГО (повторы продлевают бан)
        self.dead: set[str] = set()     # 403 PLAN_UPGRADE_REQUIRED — на этом тарифе метод недоступен навсегда
        self.last_error: str | None = None
        self.calls = 0
        self.key_bad = False
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.key) and not self.key_bad

    @property
    def test_key(self) -> bool:
        return self.key == TEST_KEY

    async def _session(self) -> aiohttp.ClientSession:
        if self._factory is not None:
            return await self._factory()
        if self._http is None or self._http.closed:
            # GMGN принимает только IPv4: по IPv6 отвечает 401/403, как будто ключ неверный
            self._http = aiohttp.ClientSession(timeout=TIMEOUT, connector=aiohttp.TCPConnector(family=socket.AF_INET))
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()

    async def request(self, method: str, path: str, query: dict | None = None, body: dict | None = None):
        if not self.configured:
            raise GmgnError("нет ключа GMGN_API_KEY")
        if path in self.dead:
            raise GmgnError("недоступно на твоём тарифе GMGN")
        now = time.time()
        if now < self.blocked_until:
            raise GmgnError(f"GMGN просит подождать ещё {int(self.blocked_until - now)} с")
        async with self._lock:  # по одному запросу и не чаще ~2 в секунду: бесплатное «ведро» — 5 единиц/с
            gap = time.monotonic() - self._last_call
            if gap < 0.5:
                await asyncio.sleep(0.5 - gap)
            params: list[tuple[str, str]] = []
            for k, v in (query or {}).items():
                for item in (v if isinstance(v, (list, tuple)) else [v]):
                    params.append((k, str(item)))
            params += [("timestamp", str(int(time.time()))), ("client_id", str(uuid.uuid4()))]
            headers = {"X-APIKEY": self.key, "Content-Type": "application/json", "User-Agent": UA}
            http = await self._session()
            data = json.dumps(body) if body is not None else None
            try:
                async with http.request(method, self.host + path, params=params, data=data, headers=headers,
                                        timeout=TIMEOUT) as r:
                    status, text = r.status, await r.text()
                    reset = _num(r.headers.get("X-RateLimit-Reset") or r.headers.get("x-ratelimit-reset"))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.last_error = f"нет связи с GMGN: {type(e).__name__}"
                raise GmgnError(self.last_error) from None
            finally:
                self._last_call = time.monotonic()
                self.calls += 1
        try:
            out = json.loads(text) if text else {}
        except ValueError:
            out = {}
        if not isinstance(out, dict):
            out = {}
        code, err = out.get("code"), str(out.get("error") or "")
        if status == 429 or "RATE_LIMIT" in err:
            until = _num(out.get("reset_at")) or reset or (time.time() + 60)
            if until > 1e12:  # миллисекунды
                until /= 1000
            self.blocked_until = max(time.time() + 5, min(until, time.time() + 600)) + 1
            self.last_error = "GMGN: лимит запросов, жду"
            raise GmgnError(self.last_error)
        if status == 403 and "PLAN_UPGRADE" in err:
            self.dead.add(path)
            self.last_error = "GMGN: этот метод недоступен на бесплатном тарифе"
            raise GmgnError(self.last_error)
        if status in (401, 403):
            self.key_bad = status == 401 or "KEY" in err.upper() or "AUTH" in err.upper()
            self.last_error = f"GMGN: ключ не подходит ({status}{' ' + err if err else ''})"
            raise GmgnError(self.last_error)
        if status != 200 or code not in (0, None) or "data" not in out:
            self.last_error = f"GMGN: ошибка {status} {err or out.get('message') or ''}".strip()[:160]
            raise GmgnError(self.last_error)
        self.last_error = None
        return out["data"]

    # --- методы ---------------------------------------------------------------------------------

    async def trending(self, chain: str = "sol", interval: str = "5m", limit: int = 50,
                       order_by: str = "volume") -> list[dict]:
        data = await self.request("GET", "/v1/market/rank", {"chain": chain, "interval": interval, "limit": limit,
                                                             "order_by": order_by, "direction": "desc"})
        rank = (data or {}).get("rank") if isinstance(data, dict) else data
        return [t for t in (norm_token(x, f"trend_{interval}") for x in (rank or [])) if t]

    async def trenches(self, chain: str = "sol", limit: int = 40) -> list[dict]:
        section = {"filters": ["offchain", "onchain"], "launchpad_platform_v2": True, "limit": limit}
        body = {"version": "v2", "new_creation": dict(section), "near_completion": dict(section),
                "completed": dict(section)}
        data = await self.request("POST", "/v1/trenches", {"chain": chain}, body) or {}
        out = []
        # в ответе «почти завершили» лежит под ключом pump (так у GMGN)
        for key, cat in (("new_creation", "new"), ("pump", "stretch"), ("near_completion", "stretch"),
                         ("completed", "migrated")):
            for x in data.get(key) or []:
                t = norm_token(x, cat)
                if t:
                    out.append(t)
        return out

    async def _trades(self, path: str, chain: str, limit: int) -> list[dict]:
        data = await self.request("GET", path, {"chain": chain, "limit": limit}) or {}
        out = []
        for x in (data.get("list") if isinstance(data, dict) else data) or []:
            if not isinstance(x, dict) or not x.get("base_address"):
                continue
            info = x.get("maker_info") if isinstance(x.get("maker_info"), dict) else {}
            base = x.get("base_token") if isinstance(x.get("base_token"), dict) else {}
            ts = _num(x.get("timestamp")) or 0.0
            out.append({"mint": str(x["base_address"]), "symbol": str(base.get("symbol") or "")[:16],
                        "maker": str(x.get("maker") or ""), "side": str(x.get("side") or ""),
                        "usd": _num(x.get("amount_usd")) or 0.0, "at": ts / 1000 if ts > 1e12 else ts,
                        "opened": str(x.get("is_open_or_close")) == "0",
                        "who": str(info.get("twitter_username") or "")[:30]})
        return out

    async def smartmoney(self, chain: str = "sol", limit: int = 100) -> list[dict]:
        return await self._trades("/v1/user/smartmoney", chain, limit)

    async def kol(self, chain: str = "sol", limit: int = 100) -> list[dict]:
        return await self._trades("/v1/user/kol", chain, limit)

    async def callouts(self, chain: str = "sol", limit: int = 30) -> list[dict]:
        data = await self.request("POST", "/v1/callout/global", None, {"chains": [chain], "limit": limit}) or {}
        out = []
        for x in (data.get("list") if isinstance(data, dict) else data) or []:
            if not isinstance(x, dict) or not x.get("call_token"):
                continue
            ts = _num(x.get("create_time")) or 0.0
            out.append({"mint": str(x["call_token"]), "symbol": str(x.get("token_symbol") or "")[:16],
                        "at": ts / 1000 if ts > 1e12 else ts, "kol": bool(x.get("is_kol")),
                        "safe": str(x.get("is_safe") or ""), "who": str(x.get("twitter_username") or "")[:30],
                        "holding": _num(x.get("current_holding_usd"))})
        return out

    async def security(self, address: str, chain: str = "sol") -> dict:
        return norm_security(await self.request("GET", "/v1/token/security", {"chain": chain, "address": address}))

    def status(self) -> dict:
        now = time.time()
        return {"configured": self.configured, "test_key": self.test_key, "calls": self.calls,
                "wait": max(0, int(self.blocked_until - now)), "error": self.last_error, "key_bad": self.key_bad}


__all__ = ["GmgnClient", "GmgnError", "norm_token", "norm_security", "GMGN_HOST", "TEST_KEY"]
