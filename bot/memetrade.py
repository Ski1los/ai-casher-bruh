"""💸 Реальная торговля мемкоинами Solana: отдельный кошелёк бота + Jupiter.

Как это устроено:
- Ключ ОТДЕЛЬНОГО кошелька (новый аккаунт в Phantom с небольшой суммой) лежит только в переменной окружения
  хостинга SOLANA_TRADER_KEY. Основной кошелёк и сид-фраза боту не нужны.
- Обмены идут через Jupiter Swap API v2 (/order → подпись → /execute): Jupiter сам подбирает лучший маршрут,
  проскальзывание и доставку транзакции. Без ключа Jupiter — 30 запросов в минуту, этого хватает.
- Автокопирование: когда ⭐-кошелёк из копитрейдинга покупает и токен проходит риск-фильтр, бот покупает
  на заданную сумму. Продаёт, когда закрывается копия (кошелёк продал, стоп, слив ликвидности, 48 ч),
  а ещё по собственному стопу по котировке Jupiter.
- Перед каждой покупкой — проверка «туда-обратно»: можно ли продать токен и сколько съедят вход и выход.
  Нет пути обратно = ловушка (honeypot), не покупаем.
- Лимиты: сумма сделки, максимум позиций, запас SOL на комиссии, дневной лимит убытка, пауза после
  3 убытков подряд. После продажи бот закрывает пустые счета токенов и возвращает залог (~0,002 SOL).
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import time

import aiohttp

from .copytrade import WSOL, buy_links
from .solana import WalletError, short
from .solkey import BadKey, Keypair, close_accounts_tx, sign_transaction

log = logging.getLogger(__name__)

JUP_BASE = "https://api.jup.ag/swap/v2"
TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFNoz2z5Z7m8wVPVQEz")
LAMPORTS = 1_000_000_000
MAX_ROUNDTRIP = 0.06        # вход+выход дороже 6% — рынок слишком тонкий
MAX_HOLD_SEC = 48 * 3600
PAUSE_AFTER_LOSSES = 3
PAUSE_SEC = 12 * 3600
SIZES = (1.0, 2.0, 3.0, 5.0)
MAX_SIZE_USD = 50.0

REAL_DEFAULT = {
    "on": False, "size_usd": 2.0, "max_open": 2, "scope": "star", "daily_loss_usd": 2.0, "reserve_sol": 0.01,
    "stop_pct": 0.4, "positions": {}, "closed": [], "decisions": [], "day": "", "day_pnl_usd": 0.0,
    "day_trades": 0, "streak": 0, "paused_until": 0.0, "low_sol_warned": 0.0,
    "stats": {"buys": 0, "sells": 0, "wins": 0, "pnl_sol": 0.0, "pnl_usd": 0.0, "rent_back_sol": 0.0},
}

SETUP_TEXT = """\
💸 <b>Как подключить торговлю мемкоинами (5 минут, с телефона)</b>

Боту нужен <b>отдельный</b> кошелёк — не твой основной.
1. <b>Phantom</b> → нажми на свой аккаунт слева вверху → «Добавить / подключить аккаунт» → «Создать новый
   аккаунт». Назови его «Бот».
2. Переведи на «Бот» немного SOL с основного аккаунта: для старта ~$5–10. Больше, чем не жалко
   потерять, не клади.
3. Phantom → Настройки → «Управление аккаунтами» → «Бот» → «Показать приватный ключ» → пароль → скопируй.
4. <b>Render</b> → твой сервис → Environment → «Add Environment Variable»: имя <code>SOLANA_TRADER_KEY</code>,
   значение — вставь ключ → «Save Changes». Render сам перезапустит бота.
5. Здесь: /meme — увидишь адрес и баланс кошелька «Бот». Включи автокопирование.

⛔ Ключ — <b>только в Render</b>. Не присылай его в этот чат, ИИ или кому-либо: бот удалит такое сообщение.
Сид-фраза (12 слов) не нужна вообще. Аккаунт «Бот» виден в твоём Phantom: можно в любой момент продать
вручную или забрать деньги обратно.

Необязательно, но лучше: бесплатный ключ Jupiter (developers.jup.ag/portal) → <code>JUPITER_API_KEY</code> — сделки
быстрее; бесплатный ключ Helius → <code>SOLANA_RPC_URL</code> — меньше «подождите» от сети."""


class SwapError(Exception):
    """Сделка не прошла. Текст — для человека."""


def _day(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now + 3 * 3600))


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.gmtime(ts + 3 * 3600))


def fmt_sol(lamports: float) -> str:
    return f"{lamports / LAMPORTS:.4f} SOL"


# ---------------------------------------------------------------------------
# Jupiter
# ---------------------------------------------------------------------------


class JupiterClient:
    def __init__(self, api_key: str | None = None, base: str = JUP_BASE) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("JUPITER_API_KEY", "")).strip()
        self.base = base
        self._session: aiohttp.ClientSession | None = None
        self._gap = 1.1 if self.api_key else 2.1  # без ключа — 30 запросов в минуту
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _s(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"x-api-key": self.api_key} if self.api_key else {}
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None,
                       spaced: bool = True) -> dict:
        for attempt in range(3):
            if spaced:
                async with self._lock:
                    wait = self._last + self._gap - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last = time.monotonic()
            try:
                async with self._s().request(method, self.base + path, params=params, json=body,
                                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                    data = await r.json(content_type=None)
                    status = r.status
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                if attempt == 2:
                    raise SwapError(f"Jupiter не отвечает ({type(e).__name__})") from None
                await asyncio.sleep(2)
                continue
            if status == 429:
                await asyncio.sleep(3 + 3 * attempt)
                continue
            if status >= 500 and attempt < 2:
                await asyncio.sleep(2)
                continue
            if status >= 400 or not isinstance(data, dict):
                msg = (data or {}).get("error") or (data or {}).get("errorMessage") if isinstance(data, dict) else None
                raise SwapError(f"Jupiter: {msg or f'ошибка {status}'}")
            return data
        raise SwapError("Jupiter просит подождать (лимит запросов). Бесплатный JUPITER_API_KEY снимет ограничение")

    async def order(self, input_mint: str, output_mint: str, amount: int, taker: str | None = None) -> dict:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount))}
        if taker:
            params["taker"] = taker
        data = await self._request("GET", "/order", params=params)
        if data.get("errorCode") or data.get("errorMessage"):
            raise SwapError(f"Jupiter: {data.get('errorMessage') or 'нет маршрута'}")
        if not int(data.get("outAmount") or 0):
            raise SwapError("Jupiter: нет маршрута для обмена")
        if taker and not data.get("transaction"):
            raise SwapError("Jupiter не собрал транзакцию (нет маршрута)")
        return data

    async def execute(self, signed_b64: str, request_id: str) -> dict:
        body = {"signedTransaction": signed_b64, "requestId": request_id}
        last: Exception | None = None
        for _ in range(3):  # тот же запрос можно повторять до 2 минут — двойного исполнения не будет
            try:
                return await self._request("POST", "/execute", body=body, spaced=False)
            except SwapError as e:
                last = e
                if "не отвечает" not in str(e):
                    raise
                await asyncio.sleep(3)
        raise last or SwapError("Jupiter не ответил")


# ---------------------------------------------------------------------------
# Кошелёк бота
# ---------------------------------------------------------------------------


class MemeWallet:
    def __init__(self, kp: Keypair, rpc, jup: JupiterClient) -> None:
        self.kp, self.rpc, self.jup = kp, rpc, jup

    @property
    def address(self) -> str:
        return self.kp.address

    async def sol_balance(self) -> int:
        res = await self.rpc.call("getBalance", [self.address, {"commitment": "confirmed"}])
        return int((res or {}).get("value", 0) if isinstance(res, dict) else res or 0)

    async def _token_accounts(self, flt: dict) -> list[dict]:
        res = await self.rpc.call("getTokenAccountsByOwner",
                                  [self.address, flt, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        out = []
        for item in (res or {}).get("value", []):
            acc = item.get("account") or {}
            info = (((acc.get("data") or {}).get("parsed") or {}).get("info")) or {}
            amount = info.get("tokenAmount") or {}
            out.append({"pubkey": item.get("pubkey"), "program": acc.get("owner"), "lamports": acc.get("lamports", 0),
                        "mint": info.get("mint"), "raw": int(amount.get("amount") or 0),
                        "decimals": amount.get("decimals")})
        return out

    async def token_balance(self, mint: str) -> int:
        return sum(a["raw"] for a in await self._token_accounts({"mint": mint}))

    async def swap(self, input_mint: str, output_mint: str, amount: int) -> dict:
        order = await self.jup.order(input_mint, output_mint, amount, taker=self.address)
        try:
            signed = sign_transaction(base64.b64decode(order["transaction"]), self.kp)
        except ValueError as e:
            raise SwapError(f"не смог подписать: {e}") from None
        res = await self.jup.execute(base64.b64encode(signed).decode(), order["requestId"])
        if res.get("status") != "Success":
            err = res.get("error") or f"код {res.get('code')}"
            raise SwapError(f"сделка не прошла в сети: {err}")
        return {"sig": res.get("signature", ""), "in": int(res.get("inputAmountResult") or res.get("totalInputAmount") or amount),
                "out": int(res.get("outputAmountResult") or res.get("totalOutputAmount") or order.get("outAmount") or 0)}

    async def close_empty(self, limit: int = 8) -> tuple[int, str | None]:
        """Закрыть пустые счета токенов → вернуть залог. (лампорты, подпись)."""
        empty = []
        for prog in TOKEN_PROGRAMS:
            empty += [a for a in await self._token_accounts({"programId": prog}) if a["raw"] == 0 and a["pubkey"]]
        if not empty:
            return 0, None
        empty = empty[:limit]
        bh = await self.rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        tx = close_accounts_tx(self.kp, [(a["pubkey"], a["program"]) for a in empty], bh["value"]["blockhash"])
        sig = await self.rpc.call("sendTransaction", [base64.b64encode(tx).decode(),
                                                      {"encoding": "base64", "preflightCommitment": "confirmed"}])
        return sum(int(a["lamports"] or 0) for a in empty), sig


# ---------------------------------------------------------------------------
# Торговец
# ---------------------------------------------------------------------------


class MemeTrader:
    CHECK_DELAY = 5.0  # сек: подождать, прежде чем проверять, дошла ли сделка без ответа Jupiter

    def __init__(self, copy, env: dict | None = None, rpc=None, jup: JupiterClient | None = None,
                 wallet: MemeWallet | None = None) -> None:
        self.copy = copy
        env = os.environ if env is None else env
        self.jup = jup or JupiterClient(env.get("JUPITER_API_KEY", ""))
        self.key_error: str | None = None
        self.wallet = wallet
        if wallet is None:
            secret = env.get("SOLANA_TRADER_KEY", "")
            if secret.strip():
                try:
                    self.wallet = MemeWallet(Keypair.from_secret(secret), rpc or copy.rpc, self.jup)
                except BadKey as e:
                    self.key_error = f"SOLANA_TRADER_KEY не подходит: {e}"
        self._lock = asyncio.Lock()
        self._balance: tuple[float, int | None] = (0.0, None)
        self.last_error: str | None = None

    # --- состояние ----------------------------------------------------------

    @property
    def st(self) -> dict:
        st = self.copy.st.get("real")
        if not isinstance(st, dict) or "positions" not in st:
            st = {**{k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in REAL_DEFAULT.items()},
                  **(st if isinstance(st, dict) else {})}
            self.copy.st["real"] = st
        for k, v in REAL_DEFAULT.items():
            if k not in st:
                st[k] = v.copy() if isinstance(v, (dict, list)) else v
        return st

    def _changed(self) -> None:
        self.copy.changes += 1

    @property
    def configured(self) -> bool:
        return self.wallet is not None

    @property
    def address(self) -> str | None:
        return self.wallet.address if self.wallet else None

    @property
    def on(self) -> bool:
        return self.configured and bool(self.st["on"])

    def _roll_day(self, now: float) -> None:
        st = self.st
        if st["day"] != _day(now):
            st["day"], st["day_pnl_usd"], st["day_trades"] = _day(now), 0.0, 0

    def _decide(self, now: float, symbol: str, text: str) -> None:
        self.st["decisions"].append({"at": now, "symbol": symbol, "text": text})
        del self.st["decisions"][:-30]
        self._changed()

    async def balance(self, max_age: float = 30) -> int | None:
        if not self.wallet:
            return None
        at, val = self._balance
        if val is not None and time.time() - at < max_age:
            return val
        try:
            val = await self.wallet.sol_balance()
        except Exception as e:
            self.last_error = f"баланс: {e}"
            return val
        self._balance = (time.time(), val)
        return val

    # --- сделки ---------------------------------------------------------------

    async def roundtrip(self, mint: str, lamports: int) -> float:
        """Сколько потеряем на входе и сразу выходе (доля). Нет пути обратно → SwapError."""
        q1 = await self.jup.order(WSOL, mint, lamports)
        out = int(q1["outAmount"])
        try:
            q2 = await self.jup.order(mint, WSOL, out)
        except SwapError:
            raise SwapError("токен нельзя продать обратно — похоже на ловушку (honeypot)") from None
        return int(q2["outAmount"]) / lamports - 1

    async def buy(self, mint: str, usd: float, *, source: str = "manual", symbol: str = "", key: str | None = None,
                  wallet_name: str = "", now: float | None = None) -> dict:
        if not self.wallet:
            raise SwapError(self.key_error or "кошелёк бота не подключён: /meme → «Как подключить»")
        now = time.time() if now is None else now
        usd = max(0.5, min(float(usd), MAX_SIZE_USD))
        async with self._lock:
            st = self.st
            if mint in st["positions"]:
                raise SwapError("этот токен уже куплен ботом")
            try:
                sol_usd = await self.copy.sol_usd()
            except Exception:
                sol_usd = 0.0
            if not sol_usd:
                raise SwapError("нет цены SOL, попробую позже")
            lamports = int(usd / sol_usd * LAMPORTS)
            bal = await self.balance(max_age=0)
            reserve = int(st["reserve_sol"] * LAMPORTS)
            if bal is None:
                raise SwapError(self.last_error or "не смог узнать баланс")
            if bal - reserve < lamports:
                raise SwapError(f"мало SOL: на кошельке бота {fmt_sol(bal)}, нужно {fmt_sol(lamports)} на сделку "
                                f"+ {fmt_sol(reserve)} запас на комиссии. Пополни аккаунт «Бот» в Phantom")
            rt = await self.roundtrip(mint, lamports)
            if rt < -MAX_ROUNDTRIP:
                raise SwapError(f"вход и сразу выход съедят {-rt:.1%} — слишком тонкий рынок, не покупаю")
            try:
                res = await self.wallet.swap(WSOL, mint, lamports)
            except SwapError as e:
                # ответ потерялся, а сделка могла пройти: смотрим, не пришли ли токены, чтобы не потерять позицию
                if "сделка не прошла" in str(e):
                    raise
                await asyncio.sleep(self.CHECK_DELAY)
                try:
                    raw = await self.wallet.token_balance(mint)
                except Exception:
                    raw = 0
                if raw <= 0:
                    raise
                res = {"sig": "", "in": lamports, "out": raw}
            self._balance = (0.0, None)
            symbol = symbol or short(mint)
            st["positions"][mint] = {"mint": mint, "symbol": symbol[:16], "source": source, "key": key,
                                     "wallet": wallet_name, "opened": now, "sol_in": res["in"], "raw": res["out"],
                                     "usd_in": round(res["in"] / LAMPORTS * sol_usd, 2), "sig": res["sig"],
                                     "value": res["in"], "roundtrip": round(rt, 4), "sell_fails": 0, "sell_why": None}
            st["stats"]["buys"] += 1
            self._roll_day(now)
            st["day_trades"] += 1
            self._changed()
        src = f"копия ⭐ {html.escape(wallet_name)}" if source == "copy" else "вручную"
        return {"text": (f"💸 <b>Купил {html.escape(symbol)}</b> на ${res['in'] / LAMPORTS * sol_usd:.2f} "
                         f"({fmt_sol(res['in'])}) — {src}.\n"
                         f"Вход+выход по проверке ≈ {-rt:.1%}. Продам: когда кошелёк продаст, стоп "
                         f"−{st['stop_pct']:.0%} или через 48 ч." if source == "copy" else
                         f"💸 <b>Купил {html.escape(symbol)}</b> на ${res['in'] / LAMPORTS * sol_usd:.2f} "
                         f"({fmt_sol(res['in'])}) — {src}. Стоп −{st['stop_pct']:.0%}, продать — кнопкой."),
                "buttons": [("🔎 Сделка в Solscan", f"https://solscan.io/tx/{res['sig']}" if res["sig"]
                             else f"https://solscan.io/account/{self.wallet.address}"),
                            ("💸 Продать сейчас", f"cb:mt:s:{mint}")], "mint": mint, "signal": False}

    async def sell(self, mint: str, why: str, now: float | None = None) -> dict | None:
        if not self.wallet:
            return None
        now = time.time() if now is None else now
        async with self._lock:
            st = self.st
            pos = st["positions"].get(mint)
            if pos is None:
                return None
            try:
                raw = await self.wallet.token_balance(mint)
            except Exception as e:
                raw = pos["raw"]
                log.info("Баланс токена %s: %s", mint, e)
            if raw <= 0:
                st["positions"].pop(mint)
                st["closed"].append({"symbol": pos["symbol"], "mint": mint, "pnl_sol": None, "pnl_usd": None,
                                     "why": "токенов на кошельке нет", "opened": pos["opened"],
                                     "closed": now, "source": pos["source"]})
                self._changed()
                return {"text": f"💸 {html.escape(pos['symbol'])}: токенов на кошельке бота уже нет (прошлая продажа "
                                "всё-таки прошла или продал вручную в Phantom). Убрал из позиций.", "buttons": []}
            try:
                res = await self.wallet.swap(mint, WSOL, raw)
            except (SwapError, WalletError) as e:
                pos["sell_fails"] += 1
                pos["sell_why"] = why
                self.last_error = f"продажа {pos['symbol']}: {e}"
                self._changed()
                if pos["sell_fails"] in (1, 3) or pos["sell_fails"] % 10 == 0:
                    return {"text": (f"⚠️ <b>Не смог продать {html.escape(pos['symbol'])}</b> ({html.escape(why)}): "
                                     f"{html.escape(str(e))[:200]}. Пробую снова каждые 1,5 мин. Можно продать "
                                     "вручную: Phantom → аккаунт «Бот»."),
                            "buttons": [("👻 Продать в Phantom", buy_links(mint)["phantom_sell"])]}
                return None
            self._balance = (0.0, None)
            sol_usd = await self.copy.sol_usd() or 0.0
            pnl = res["out"] - pos["sol_in"]
            pnl_usd = round(pnl / LAMPORTS * sol_usd, 2)
            st["positions"].pop(mint)
            self._roll_day(now)
            st["day_pnl_usd"] = round(st["day_pnl_usd"] + pnl_usd, 2)
            stats = st["stats"]
            stats["sells"] += 1
            stats["wins"] += pnl > 0
            stats["pnl_sol"] = round(stats["pnl_sol"] + pnl / LAMPORTS, 6)
            stats["pnl_usd"] = round(stats["pnl_usd"] + pnl_usd, 2)
            st["streak"] = 0 if pnl > 0 else st["streak"] + 1
            pct = (res["out"] / pos["sol_in"] - 1) * 100 if pos["sol_in"] else 0.0
            st["closed"].append({"symbol": pos["symbol"], "mint": mint, "pnl_sol": round(pnl / LAMPORTS, 6),
                                 "pnl_usd": pnl_usd, "pct": round(pct, 1), "why": why, "opened": pos["opened"],
                                 "closed": now, "source": pos["source"], "sig": res["sig"]})
            del st["closed"][:-50]
            extra = ""
            if st["streak"] >= PAUSE_AFTER_LOSSES:
                st["paused_until"] = now + PAUSE_SEC
                st["streak"] = 0
                extra = f"\n⏸ {PAUSE_AFTER_LOSSES} убытка подряд — новые покупки на паузе 12 часов."
            if st["day_pnl_usd"] <= -st["daily_loss_usd"]:
                extra += f"\n⏸ Дневной лимит убытка ${st['daily_loss_usd']:g} исчерпан — до завтра бот не покупает."
            self._changed()
        rent = await self._reclaim()
        if rent:
            extra += f"\n♻️ Вернул залог за пустой счёт токена: {fmt_sol(rent)}."
        return {"text": (f"{'✅' if pnl > 0 else '🔻'} <b>Продал {html.escape(pos['symbol'])}</b> ({html.escape(why)}): "
                         f"<b>{pct:+.1f}%</b>, {pnl / LAMPORTS:+.4f} SOL ≈ <b>{pnl_usd:+.2f}$</b>.\n"
                         f"Итого реальных сделок: {stats['sells']}, в плюс {stats['wins']}, "
                         f"{stats['pnl_usd']:+.2f}$." + extra),
                "buttons": [("🔎 Сделка в Solscan", f"https://solscan.io/tx/{res['sig']}")]}

    async def _reclaim(self) -> int:
        try:
            lamports, sig = await self.wallet.close_empty()
        except Exception as e:  # залог вернётся в следующий раз; Token-2022 с комиссиями закрыть нельзя
            log.info("Закрытие пустых счетов: %s", e)
            return 0
        if lamports:
            self.st["stats"]["rent_back_sol"] = round(self.st["stats"]["rent_back_sol"] + lamports / LAMPORTS, 6)
            self._changed()
        return lamports

    async def sell_all(self, why: str = "продать всё") -> list[dict]:
        events = []
        for mint in list(self.st["positions"]):
            e = await self.sell(mint, why)
            if e:
                events.append(e)
        return events

    # --- автокопирование --------------------------------------------------------

    def _in_scope(self, w: dict | None) -> bool:
        if not w or w["status"] == "dropped":
            return False
        if w["status"] == "star":
            return True
        return self.st["scope"] == "good" and (w.get("hist") or {}).get("grade") == "good"

    def _why_not(self, pos: dict, now: float) -> str | None:
        st = self.st
        self._roll_day(now)
        if not pos.get("passed") or pos.get("hard_fail"):
            return "риск-фильтр: " + ((pos.get("reasons") or ["не прошёл"])[0])
        if pos["mint"] in st["positions"]:
            return "уже держу"
        if now < st["paused_until"]:
            return f"пауза после убытков до {_hm(st['paused_until'])}"
        if st["day_pnl_usd"] <= -st["daily_loss_usd"]:
            return f"дневной лимит убытка ${st['daily_loss_usd']:g} исчерпан"
        if len(st["positions"]) >= st["max_open"]:
            return f"уже открыто {len(st['positions'])} из {st['max_open']} позиций"
        return None

    async def after_copy_step(self, now: float | None = None) -> list[dict]:
        """После шага копитрейдинга: закрылась копия → продаём; новая копия ⭐ → покупаем."""
        if not self.wallet:
            return []
        now = time.time() if now is None else now
        events: list[dict] = []
        opened, closed = list(getattr(self.copy, "opened_now", [])), list(getattr(self.copy, "closed_now", []))
        self.copy.opened_now, self.copy.closed_now = [], []  # каждое событие обрабатываем один раз
        for key, pos, why in closed:
            rp = self.st["positions"].get(pos["mint"])
            if rp and rp.get("key") == key:
                e = await self.sell(pos["mint"], f"копия: {why}", now)
                if e:
                    events.append(e)
        if not self.on:
            return events
        for key in opened:
            pos = self.copy.st["open"].get(key)
            if not pos:
                continue
            w = self.copy.st["wallets"].get(pos["wallet"])
            if not self._in_scope(w):
                continue
            why = self._why_not(pos, now)
            if why:
                self._decide(now, pos["symbol"], f"не купил: {why}")
                continue
            try:
                e = await self.buy(pos["mint"], self.st["size_usd"], source="copy", symbol=pos["symbol"], key=key,
                                   wallet_name=w["name"], now=now)
            except (SwapError, WalletError) as err:
                self._decide(now, pos["symbol"], f"не купил: {err}")
                if "мало SOL" in str(err):
                    if now - self.st["low_sol_warned"] > 6 * 3600:
                        self.st["low_sol_warned"] = now
                        events.append({"text": f"⚠️ ⭐ {html.escape(w['name'])} купил {html.escape(pos['symbol'])}, "
                                               f"но {html.escape(str(err))}", "buttons": []})
                else:
                    events.append({"text": f"⚠️ Не купил {html.escape(pos['symbol'])} за ⭐ {html.escape(w['name'])}: "
                                           f"{html.escape(str(err))[:200]}", "buttons": []})
                continue
            self._decide(now, pos["symbol"], f"купил за ⭐ {w['name']}")
            events.append(e)
        return events

    async def guard(self, now: float | None = None) -> list[dict]:
        """Каждый шаг: стоп по котировке Jupiter, 48 часов, повтор неудачных продаж, копия уже закрыта."""
        if not self.wallet or not self.st["positions"]:
            return []
        now = time.time() if now is None else now
        events: list[dict] = []
        for mint, pos in list(self.st["positions"].items()):
            why = pos.get("sell_why")
            if not why and pos["source"] == "copy" and pos.get("key") and pos["key"] not in self.copy.st["open"]:
                why = "копия уже закрыта"
            if not why and pos["source"] == "copy" and now - pos["opened"] > MAX_HOLD_SEC:
                why = "48 часов"
            if not why:
                try:
                    q = await self.jup.order(mint, WSOL, pos["raw"])
                    pos["value"] = int(q["outAmount"])
                    pos["checked"] = now
                    pos.pop("quote_err", None)
                except SwapError as e:
                    pos["quote_err"] = str(e)[:120]
                    continue
                if pos["sol_in"] and pos["value"] <= pos["sol_in"] * (1 - self.st["stop_pct"]):
                    why = f"стоп −{self.st['stop_pct']:.0%}"
                self._changed()
            if why:
                e = await self.sell(mint, why, now)
                if e:
                    events.append(e)
        return events

    # --- показ ------------------------------------------------------------------

    def status_dict(self) -> dict:
        st = self.st
        at, bal = self._balance
        sol_usd = self.copy._sol_usd[1] if getattr(self.copy, "_sol_usd", None) else 0.0
        return {
            "configured": self.configured, "error": self.key_error, "address": self.address,
            "balance_sol": round(bal / LAMPORTS, 4) if bal is not None else None,
            "balance_usd": round(bal / LAMPORTS * sol_usd, 2) if bal is not None and sol_usd else None,
            "on": self.on, "size_usd": st["size_usd"], "max_open": st["max_open"], "scope": st["scope"],
            "stop_pct": st["stop_pct"], "daily_loss_usd": st["daily_loss_usd"], "reserve_sol": st["reserve_sol"],
            "paused_until": st["paused_until"] if st["paused_until"] > time.time() else None,
            "day_pnl_usd": st["day_pnl_usd"] if st["day"] == _day(time.time()) else 0.0,
            "positions": [{"mint": m, "symbol": p["symbol"], "source": p["source"], "wallet": p["wallet"],
                           "opened": p["opened"], "usd_in": p["usd_in"],
                           "pct": round((p["value"] / p["sol_in"] - 1) * 100, 1) if p.get("value") and p["sol_in"] else None,
                           "usd_now": round(p["value"] / LAMPORTS * sol_usd, 2) if p.get("value") and sol_usd else None,
                           "selling": bool(p.get("sell_why")), "sig": p["sig"]}
                          for m, p in st["positions"].items()],
            "closed": list(reversed(st["closed"][-15:])), "decisions": list(reversed(st["decisions"][-12:])),
            "stats": st["stats"], "sizes": list(SIZES), "last_error": self.last_error,
        }

    def text(self) -> str:
        if not self.configured:
            return ("💸 <b>Торговля мемкоинами</b>\n" + (f"⚠️ {html.escape(self.key_error)}\n\n" if self.key_error else "")
                    + SETUP_TEXT)
        d = self.status_dict()
        bal = (f"{d['balance_sol']:.4f} SOL" + (f" ≈ ${d['balance_usd']:.2f}" if d["balance_usd"] else "")
               if d["balance_sol"] is not None else "не узнал")
        lines = ["💸 <b>Торговля мемкоинами</b> (Solana · Jupiter)",
                 f"Кошелёк бота: <code>{d['address']}</code>",
                 f"Баланс: <b>{bal}</b>",
                 f"Автокопирование: {'🟢 включено' if d['on'] else '⏸ выключено'} · "
                 f"{'только ⭐' if d['scope'] == 'star' else '⭐ + 👀 с хорошей историей'} · по ${d['size_usd']:g} · "
                 f"до {d['max_open']} позиций · стоп −{d['stop_pct']:.0%} · лимит убытка ${d['daily_loss_usd']:g}/день"]
        if d["paused_until"]:
            lines.append(f"⏸ Пауза после убытков до {_hm(d['paused_until'])}")
        if d["on"] and not self.copy.on:
            lines.append("⚠️ Копитрейдинг выключен — без него нет сигналов. Включи: /copy")
        if d["positions"]:
            lines.append("\n<b>Позиции</b>:")
            for p in d["positions"]:
                now_ = f" → ${p['usd_now']:.2f} ({p['pct']:+.1f}%)" if p["pct"] is not None and p["usd_now"] else ""
                src = f"копия ⭐ {html.escape(p['wallet'])}" if p["source"] == "copy" else "вручную"
                lines.append(f"• <b>{html.escape(p['symbol'])}</b> ${p['usd_in']:.2f}{now_} · {src} · "
                             f"{int((time.time() - p['opened']) // 60)} мин" + (" · ⏳ продаю" if p["selling"] else ""))
        s = d["stats"]
        if s["sells"]:
            lines.append(f"\nИтого: продаж {s['sells']}, в плюс {s['wins']}, <b>{s['pnl_usd']:+.2f}$</b> "
                         f"({s['pnl_sol']:+.4f} SOL) · сегодня {d['day_pnl_usd']:+.2f}$")
        if d["decisions"]:
            lines.append("\n<b>Последние решения</b>:")
            for x in d["decisions"][:5]:
                lines.append(f"• {_hm(x['at'])} {html.escape(x['symbol'])} — {html.escape(x['text'])}")
        if self.last_error:
            lines.append(f"\n⚠️ {html.escape(self.last_error)[:200]}")
        lines.append("\nКупить вручную: <code>/meme buy АДРЕС_ТОКЕНА 2</code> · продать: <code>/meme sell СИМВОЛ</code>")
        return "\n".join(lines)

    def short_line(self) -> str:
        if not self.configured:
            return ""
        s = self.st["stats"]
        return (f"💸 <b>Мемкоины (реально)</b>: {'🟢' if self.on else '⏸'} позиций {len(self.st['positions'])}"
                + (f", продаж {s['sells']}, в плюс {s['wins']}, {s['pnl_usd']:+.2f}$" if s["sells"] else "") + " — /meme")

    def find(self, query: str) -> str | None:
        """Позиция по адресу или символу."""
        q = (query or "").strip()
        if q in self.st["positions"]:
            return q
        for m, p in self.st["positions"].items():
            if p["symbol"].lower() == q.lower().lstrip("$"):
                return m
        return None
