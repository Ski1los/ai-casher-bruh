"""Кошелёк Phantom (сеть Solana) в режиме «только просмотр».

Боту нужен только ПУБЛИЧНЫЙ адрес кошелька: тот, что показывается в Phantom по кнопке «Получить».
С ним можно только смотреть баланс. Сид-фразу (12 слов) и приватный ключ бот не спрашивает
и никогда не попросит. Любой, кто их просит, хочет украсть деньги.

Данные берутся из публичного узла Solana (JSON-RPC). Свой узел можно указать в SOLANA_RPC_URL
(например бесплатный ключ Helius), если публичный начнёт отвечать «слишком много запросов».
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
CACHE_SEC = 60
ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58, публичный ключ Solana
TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",  # Token-2022
)
LAMPORTS = 1_000_000_000

# Популярные токены Solana: адрес монеты (mint) → тикер на биржах (для оценки в $)
KNOWN_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "WIF",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3": "PYTH",
    "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": "RAY",
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": "JTO",
    "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ": "W",
    "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE": "ORCA",
    "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof": "RENDER",
    "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr": "POPCAT",
    "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN": "TRUMP",
    "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv": "PENGU",
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": "ETH",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "MSOL",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JITOSOL",
}


class WalletError(Exception):
    """Понятная ошибка чтения кошелька."""


def valid_address(address: str) -> bool:
    return bool(ADDRESS_RE.match((address or "").strip()))


def short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}" if len(address) > 10 else address


async def _rpc(session: aiohttp.ClientSession, url: str, method: str, params: list):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
        if r.status == 429:
            raise WalletError("узел Solana просит подождать (слишком много запросов). Можно указать свой SOLANA_RPC_URL")
        if r.status >= 400:
            raise WalletError(f"узел Solana ответил ошибкой {r.status}")
        data = await r.json(content_type=None)
    if data.get("error"):
        msg = (data["error"] or {}).get("message", "ошибка")
        raise WalletError(f"узел Solana: {msg}")
    return data.get("result")


async def fetch_wallet(address: str, rpc_url: str, session: aiohttp.ClientSession) -> dict[str, float]:
    """{тикер или mint: количество} — SOL и все токены с ненулевым балансом."""
    bal = await _rpc(session, rpc_url, "getBalance", [address])
    out: dict[str, float] = {}
    sol = ((bal or {}).get("value") or 0) / LAMPORTS
    if sol > 0:
        out["SOL"] = sol

    async def tokens(program: str):
        res = await _rpc(session, rpc_url, "getTokenAccountsByOwner",
                         [address, {"programId": program}, {"encoding": "jsonParsed"}])
        return (res or {}).get("value") or []

    results = await asyncio.gather(*(tokens(p) for p in TOKEN_PROGRAMS), return_exceptions=True)
    if all(isinstance(r, BaseException) for r in results):
        raise results[0]
    for accounts in results:
        if isinstance(accounts, BaseException):
            continue
        for acc in accounts:
            try:
                info = acc["account"]["data"]["parsed"]["info"]
                amount = float(info["tokenAmount"].get("uiAmountString") or info["tokenAmount"].get("uiAmount") or 0)
                mint = info["mint"]
            except (KeyError, TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            key = KNOWN_MINTS.get(mint, mint)
            out[key] = out.get(key, 0) + amount
    return out


class PhantomWatcher:
    """Адрес кошелька + кэш последнего снимка (чтобы не дёргать узел Solana на каждый экран)."""

    def __init__(self, address: str = "", rpc_url: str | None = None) -> None:
        self.address = address.strip()
        self.rpc_url = rpc_url or os.getenv("SOLANA_RPC_URL", "").strip() or DEFAULT_RPC
        self.snapshot: dict | None = None
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None

    def set_address(self, address: str) -> None:
        address = (address or "").strip()
        if address and not valid_address(address):
            raise WalletError("это не похоже на адрес Solana. В Phantom: «Получить» → Solana → скопировать адрес")
        self.address = address
        self.snapshot = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get(self, price_fn, force: bool = False, mint_price_fn=None) -> dict | None:
        """Снимок: {address, total_usd, assets: [{coin, amount, usd}], updated_at, error?}.

        price_fn(coins) → {тикер: цена $} (берём с биржи).
        mint_price_fn(mints) → {mint: (тикер, цена $)} для мемкоинов, которых нет на биржах (DexScreener)."""
        if not self.address:
            return None
        async with self._lock:
            snap = self.snapshot
            if snap and not force and time.time() - snap["updated_at"] < CACHE_SEC and snap.get("address") == self.address:
                return snap
            try:
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
                amounts = await fetch_wallet(self.address, self.rpc_url, self._session)
                known = [c for c in amounts if len(c) <= 15]
                prices = await price_fn(known) if known else {}
                mints = [c for c in amounts if len(c) > 15]
                dex_prices: dict = {}
                if mints and mint_price_fn is not None:
                    try:
                        dex_prices = await mint_price_fn(mints[:60])
                    except Exception as e:  # без цен мемкоинов кошелёк всё равно показываем
                        log.info("Цены токенов Phantom: %s", e)
                assets = []
                for coin, amount in amounts.items():
                    price = prices.get(coin)
                    name = coin if len(coin) <= 15 else short(coin)
                    if coin in dex_prices:
                        name, price = dex_prices[coin][0][:15], dex_prices[coin][1]
                    assets.append({"coin": name, "mint": coin if len(coin) > 15 else None,
                                   "amount": amount, "usd": amount * price if price else None})
                assets.sort(key=lambda a: -(a["usd"] or 0))
                self.snapshot = {"address": self.address, "assets": assets, "updated_at": time.time(),
                                 "total_usd": sum(a["usd"] or 0 for a in assets)}
            except WalletError as e:
                self.snapshot = self._failed(str(e))
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.snapshot = self._failed(f"нет связи с узлом Solana: {type(e).__name__}")
            except Exception as e:  # кошелёк не должен ронять остальное
                log.exception("Phantom")
                self.snapshot = self._failed(f"{type(e).__name__}: {str(e)[:120]}")
            return self.snapshot

    def _failed(self, error: str) -> dict:
        old = self.snapshot if self.snapshot and self.snapshot.get("address") == self.address else None
        return {"address": self.address, "assets": old["assets"] if old else [], "updated_at": time.time(),
                "total_usd": old["total_usd"] if old else 0, "error": error}

    def text(self, snap: dict | None) -> str:
        if not self.address:
            return PHANTOM_HELP
        if not snap:
            return "👻 Phantom: загружаю…"
        lines = [f"👻 <b>Phantom</b> <code>{short(snap['address'])}</code> (только просмотр)"]
        if snap.get("error"):
            lines.append(f"⚠️ {snap['error']}")
        for a in snap["assets"][:15]:
            usd = f" ≈ ${a['usd']:.2f}" if a["usd"] is not None else ""
            lines.append(f"  {a['coin']}: {a['amount']:.6g}{usd}")
        if not snap["assets"] and not snap.get("error"):
            lines.append("  пусто")
        lines.append(f"Всего ≈ <b>${snap['total_usd']:.2f}</b>")
        return "\n".join(lines)


PHANTOM_HELP = """\
👻 <b>Phantom: только просмотр баланса</b>

Пришли публичный адрес кошелька (Solana):
<code>/phantom АДРЕС</code>

Где взять: Phantom → «Получить» (Receive) → Solana → «Копировать».
Адрес начинается с цифры или буквы и длиной 32–44 символа.

🔒 <b>Сид-фразу (12 слов) и приватный ключ не присылай никому и никуда</b>, ни боту, ни «поддержке».
С публичным адресом можно только смотреть баланс, украсть по нему ничего нельзя.
Убрать кошелёк: <code>/phantom off</code>"""
