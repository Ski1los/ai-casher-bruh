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

from .copytrade import WSOL, buy_links, learn_exits, tp_text
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

NET_FEE = 100_000            # лампорты: сеть + приоритет за одну транзакцию (для демо; ≈ $0,015)
DEMO_SIZES = (5.0, 10.0, 20.0)
DEMO_READY_TRADES = 10      # после стольких демо-сделок можно судить, работает ли
CAT_ICON = {"star": "⭐", "good": "👀", "mentor": "🎓"}
GOOD_MIN = 12               # судить о 👀-кошельках не раньше чем по 12 копиям

ACCOUNT_DEFAULT = {"positions": {}, "closed": [], "decisions": [], "day": "", "day_pnl_usd": 0.0, "day_trades": 0,
                   "streak": 0, "paused_until": 0.0,
                   "stats": {"buys": 0, "sells": 0, "wins": 0, "pnl_sol": 0.0, "pnl_usd": 0.0, "rent_back_sol": 0.0}}
DEMO_DEFAULT = {**ACCOUNT_DEFAULT, "on": True, "started": False, "notify": True, "start_usd": 10.0, "start_sol": 0,
                "cash": 0, "resets": 0}
REAL_DEFAULT = {
    # правила — общие для настоящего и демо-счёта; стоп и тейк бот подстраивает сам (learn_exits)
    "on": False, "size_usd": 2.0, "max_open": 2, "scope": "star", "daily_loss_usd": 2.0, "reserve_sol": 0.01,
    "stop_pct": 0.4, "tp_pct": None, "low_sol_warned": 0.0,
    **ACCOUNT_DEFAULT,
    "demo": DEMO_DEFAULT,
    "learn": {"good_ok": None, "seen": None, "good_n": 0, "good_avg": 0.0},
}


def _fresh(d: dict) -> dict:
    """Глубокая копия значений по умолчанию (вложенные словари и списки — свои у каждого)."""
    return {k: (_fresh(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in d.items()}


def _fill(st: dict, default: dict) -> dict:
    for k, v in default.items():
        if k not in st:
            st[k] = _fresh(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v
        elif isinstance(v, dict) and isinstance(st[k], dict) and k != "positions":
            _fill(st[k], v)
    return st

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
    """Два счёта с одними правилами: настоящий (кошелёк «Бот») и 🧪 демо (виртуальные SOL, настоящие котировки
    Jupiter). Демо работает всегда, пока включён копитрейдинг: учится, пока кошелёк не подключён или пуст."""

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
        if not isinstance(st, dict):
            st = {}
            self.copy.st["real"] = st
        return _fill(st, REAL_DEFAULT)

    @property
    def demo(self) -> dict:
        return self.st["demo"]

    def _acc(self, demo: bool) -> dict:
        return self.demo if demo else self.st

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

    @property
    def demo_on(self) -> bool:
        return bool(self.demo["on"])

    def _demo_loud(self) -> bool:
        """Сообщать о демо-сделках, пока настоящий счёт не торгует (потом демо учится молча)."""
        return bool(self.demo["notify"]) and not self.on

    def start_demo_if_new(self) -> bool:
        """Первый запуск с демо: включаем демо и копитрейдинг (без него нет сигналов). True — если запустили сейчас."""
        d = self.demo
        if d["started"]:
            return False
        d["started"] = True
        d["on"] = True
        self.copy.st["on"] = True
        self._changed()
        return True

    def _roll_day(self, now: float, acc: dict | None = None) -> None:
        acc = self.st if acc is None else acc
        if acc["day"] != _day(now):
            acc["day"], acc["day_pnl_usd"], acc["day_trades"] = _day(now), 0.0, 0

    def _decide(self, now: float, symbol: str, text: str, acc: dict | None = None) -> None:
        acc = self.st if acc is None else acc
        acc["decisions"].append({"at": now, "symbol": symbol, "text": text})
        del acc["decisions"][:-30]
        self._changed()

    async def _sol_usd(self) -> float:
        try:
            return await self.copy.sol_usd() or 0.0
        except Exception:
            return self.copy._sol_usd[1] if getattr(self.copy, "_sol_usd", None) else 0.0

    def _sol_usd_cached(self) -> float:
        return self.copy._sol_usd[1] if getattr(self.copy, "_sol_usd", None) else 0.0

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

    def _book_close(self, acc: dict, mint: str, pos: dict, proceeds: int, basis: int, why: str, now: float,
                    sol_usd: float, sig: str | None = None) -> tuple[int, float, float, str]:
        """Записать закрытие: итог, дневной счёт, серия убытков, пауза. → (лампорты, $, %, приписка)."""
        pnl = proceeds - basis
        pnl_usd = round(pnl / LAMPORTS * sol_usd, 2)
        pct = (proceeds / basis - 1) * 100 if basis else 0.0
        acc["positions"].pop(mint, None)
        self._roll_day(now, acc)
        acc["day_pnl_usd"] = round(acc["day_pnl_usd"] + pnl_usd, 2)
        stats = acc["stats"]
        stats["sells"] += 1
        stats["wins"] += pnl > 0
        stats["pnl_sol"] = round(stats["pnl_sol"] + pnl / LAMPORTS, 6)
        stats["pnl_usd"] = round(stats["pnl_usd"] + pnl_usd, 2)
        acc["streak"] = 0 if pnl > 0 else acc["streak"] + 1
        acc["closed"].append({"symbol": pos["symbol"], "mint": mint, "pnl_sol": round(pnl / LAMPORTS, 6),
                              "pnl_usd": pnl_usd, "pct": round(pct, 1), "why": why, "opened": pos["opened"],
                              "closed": now, "source": pos["source"], "sig": sig, "category": pos.get("category")})
        del acc["closed"][:-50]
        extra = ""
        if acc["streak"] >= PAUSE_AFTER_LOSSES:
            acc["paused_until"] = now + PAUSE_SEC
            acc["streak"] = 0
            extra = f"\n⏸ {PAUSE_AFTER_LOSSES} убытка подряд — новые покупки на паузе 12 часов."
        if acc["day_pnl_usd"] <= -self.st["daily_loss_usd"]:
            extra += f"\n⏸ Дневной лимит убытка ${self.st['daily_loss_usd']:g} исчерпан — до завтра бот не покупает."
        self._changed()
        return pnl, pnl_usd, pct, extra

    # --- проверка токена ----------------------------------------------------------

    async def roundtrip(self, mint: str, lamports: int) -> float:
        """Сколько потеряем на входе и сразу выходе (доля). Нет пути обратно → SwapError."""
        q1 = await self.jup.order(WSOL, mint, lamports)
        out = int(q1["outAmount"])
        try:
            q2 = await self.jup.order(mint, WSOL, out)
        except SwapError:
            raise SwapError("токен нельзя продать обратно — похоже на ловушку (honeypot)") from None
        return int(q2["outAmount"]) / lamports - 1

    async def _quote(self, input_mint: str, output_mint: str, amount: int) -> int | None:
        try:
            return int((await self.jup.order(input_mint, output_mint, amount))["outAmount"])
        except SwapError as e:
            if "нет маршрута" not in str(e) and "route" not in str(e).lower():
                self.last_error = f"котировка Jupiter: {e}"
            return None
        except Exception as e:  # сеть: демо возьмёт цену DexScreener
            log.info("Котировка Jupiter для демо: %s", e)
            return None

    async def _price(self, mint: str) -> float | None:
        try:
            p = (await self.copy._pairs([mint])).get(mint)
            return p.price_usd if p and p.price_usd else None
        except Exception:
            return None

    # --- настоящие сделки -------------------------------------------------------------

    async def buy(self, mint: str, usd: float, *, source: str = "manual", symbol: str = "", key: str | None = None,
                  wallet_name: str = "", now: float | None = None, category: str = "star") -> dict:
        if not self.wallet:
            raise SwapError(self.key_error or "кошелёк бота не подключён: /meme → «Как подключить»")
        now = time.time() if now is None else now
        usd = max(0.5, min(float(usd), MAX_SIZE_USD))
        async with self._lock:
            st = self.st
            if mint in st["positions"]:
                raise SwapError("этот токен уже куплен ботом")
            sol_usd = await self._sol_usd()
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
            self._calibrate(-rt, now)
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
                                     "value": res["in"], "roundtrip": round(rt, 4), "sell_fails": 0, "sell_why": None,
                                     "category": category}
            st["stats"]["buys"] += 1
            self._roll_day(now)
            st["day_trades"] += 1
            self._changed()
        src = f"копия {'⭐' if category == 'star' else '👀'} {html.escape(wallet_name)}" if source == "copy" else "вручную"
        return {"text": (f"💸 <b>Купил {html.escape(symbol)}</b> на ${res['in'] / LAMPORTS * sol_usd:.2f} "
                         f"({fmt_sol(res['in'])}) — {src}.\n" + self._exit_rules_text(source)),
                "buttons": [("🔎 Сделка в Solscan", f"https://solscan.io/tx/{res['sig']}" if res["sig"]
                             else f"https://solscan.io/account/{self.wallet.address}"),
                            ("💸 Продать сейчас", f"cb:mt:s:{mint}")], "mint": mint, "signal": False}

    def _exit_rules_text(self, source: str) -> str:
        st = self.st
        tp = f", тейк-профит {tp_text(st['tp_pct'])}" if st["tp_pct"] else ""
        if source == "copy":
            return f"Продам: когда кошелёк продаст, стоп −{st['stop_pct']:.0%}{tp} или через 48 ч."
        return f"Стоп −{st['stop_pct']:.0%}{tp}, продать — кнопкой."

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
                del st["closed"][:-50]
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
            sol_usd = await self._sol_usd()
            pnl, pnl_usd, pct, extra = self._book_close(st, mint, pos, res["out"], pos["sol_in"], why, now, sol_usd,
                                                        res["sig"])
            stats = st["stats"]
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

    # --- 🧪 демо-счёт ------------------------------------------------------------------

    def _demo_init(self, sol_usd: float) -> None:
        d = self.demo
        if not d["start_sol"] and sol_usd:
            d["start_sol"] = d["cash"] = int(d["start_usd"] / sol_usd * LAMPORTS)
            self._changed()

    def demo_reset(self, usd: float) -> None:
        d = self.demo
        keep = {"on": d["on"], "started": True, "notify": d["notify"], "resets": d["resets"] + 1}
        d.clear()
        d.update(_fresh(DEMO_DEFAULT))
        d.update(keep, start_usd=float(usd))
        self._demo_init(self._sol_usd_cached())
        self._changed()

    def demo_equity(self) -> int:
        d = self.demo
        return d["cash"] + sum(p.get("value") or p["sol_in"] for p in d["positions"].values())

    async def demo_buy(self, mint: str, usd: float, *, source: str = "manual", symbol: str = "", key: str | None = None,
                       wallet_name: str = "", now: float | None = None, category: str = "star") -> dict:
        """Покупка на демо: те же проверки и настоящая котировка Jupiter, деньги — виртуальные."""
        now = time.time() if now is None else now
        d = self.demo
        sol_usd = await self._sol_usd()
        if not sol_usd:
            raise SwapError("нет цены SOL, попробую позже")
        self._demo_init(sol_usd)
        if mint in d["positions"]:
            raise SwapError("этот токен уже есть на демо-счёте")
        usd = max(0.5, min(float(usd), MAX_SIZE_USD))
        lamports = int(usd / sol_usd * LAMPORTS)
        if d["cash"] < lamports + NET_FEE:
            raise SwapError(f"на демо-счёте мало SOL ({fmt_sol(d['cash'])}) — дождись продаж или начни демо заново")
        price = await self._price(mint)
        out = await self._quote(WSOL, mint, lamports)
        if out:
            back = await self._quote(mint, WSOL, out)
            if not back:
                raise SwapError("токен нельзя продать обратно — похоже на ловушку (honeypot)")
            rt, src = back / lamports - 1, "jupiter"
            self._calibrate(-rt, now)
        else:  # Jupiter недоступен — считаем по цене DexScreener и откалиброванной стоимости
            if not price:
                raise SwapError("нет ни котировки Jupiter, ни цены DexScreener")
            rt, src = -2 * self.copy.cost_side(), "dex"
            back = int(lamports * (1 + rt))
        if rt < -MAX_ROUNDTRIP:
            raise SwapError(f"вход и сразу выход съедят {-rt:.1%} — слишком тонкий рынок, не покупаю")
        d["cash"] -= lamports + NET_FEE
        symbol = (symbol or short(mint))[:16]
        d["positions"][mint] = {"mint": mint, "symbol": symbol, "source": source, "key": key, "wallet": wallet_name,
                                "category": category, "opened": now, "sol_in": lamports, "raw": out or 0,
                                "price_in": price, "value0": back, "value": back, "src": src,
                                "usd_in": round(usd, 2), "sell_why": None}
        d["stats"]["buys"] += 1
        self._roll_day(now, d)
        d["day_trades"] += 1
        self._changed()
        who = (f"за {CAT_ICON.get(category, '👀')} {html.escape(wallet_name)}" if source in ("copy", "mentor")
               else "вручную")
        return {"text": f"🧪 Демо: купил <b>{html.escape(symbol)}</b> на ${usd:g} {who} (вход+выход ≈ {-rt:.1%}).",
                "buttons": [("📈 DexScreener", buy_links(mint)["dexscreener"]), ("💸 Продать (демо)", f"cb:mt:ds:{mint}")],
                "mint": mint, "signal": False, "demo": True}

    async def _demo_value(self, pos: dict) -> int | None:
        if pos.get("raw"):
            q = await self._quote(pos["mint"], WSOL, pos["raw"])
            if q is not None:
                return q
        if pos.get("price_in"):
            price = await self._price(pos["mint"])
            if price:
                return int(pos["value0"] * price / pos["price_in"])
        return None

    async def demo_sell(self, mint: str, why: str, now: float | None = None, value: int | None = None) -> dict | None:
        now = time.time() if now is None else now
        d = self.demo
        pos = d["positions"].get(mint)
        if pos is None:
            return None
        if value is None:
            value = await self._demo_value(pos)
        if value is None:
            value = pos.get("value") or 0
        proceeds = max(0, value - NET_FEE)
        d["cash"] += proceeds
        sol_usd = await self._sol_usd()
        pnl, pnl_usd, pct, extra = self._book_close(d, mint, pos, proceeds, pos["sol_in"] + NET_FEE, why, now, sol_usd)
        eq = self.demo_equity()
        total = (eq / d["start_sol"] - 1) * 100 if d["start_sol"] else 0.0
        return {"text": (f"🧪 Демо: {'✅' if pnl > 0 else '🔻'} продал <b>{html.escape(pos['symbol'])}</b> "
                         f"({html.escape(why)}): <b>{pct:+.1f}%</b> ≈ {pnl_usd:+.2f}$. "
                         f"Демо-счёт: ${eq / LAMPORTS * sol_usd:.2f} ({total:+.1f}% с начала)." + extra),
                "buttons": [], "demo": True}

    # --- автокопирование --------------------------------------------------------

    def _in_scope(self, w: dict | None, demo: bool = False) -> str | None:
        """Категория кошелька, если его стоит копировать на этом счёте: "star" / "good" / None."""
        if not w or w["status"] == "dropped":
            return None
        if w["status"] == "star":
            return "star"
        good = (w.get("hist") or {}).get("grade") == "good"
        if not good:
            return None
        if demo:  # демо пробует и хорошие 👀, чтобы учиться быстрее, — пока они не доказали, что теряют
            return "good" if self.st["learn"]["good_ok"] is not False else None
        return "good" if self.st["scope"] == "good" else None

    def _why_not(self, pos: dict, now: float, acc: dict | None = None) -> str | None:
        acc = self.st if acc is None else acc
        self._roll_day(now, acc)
        if not pos.get("passed") or pos.get("hard_fail"):
            return "риск-фильтр: " + ((pos.get("reasons") or ["не прошёл"])[0])
        if pos["mint"] in acc["positions"]:
            return "уже держу"
        if now < acc["paused_until"]:
            return f"пауза после убытков до {_hm(acc['paused_until'])}"
        if acc["day_pnl_usd"] <= -self.st["daily_loss_usd"]:
            return f"дневной лимит убытка ${self.st['daily_loss_usd']:g} исчерпан"
        if len(acc["positions"]) >= self.st["max_open"]:
            return f"уже открыто {len(acc['positions'])} из {self.st['max_open']} позиций"
        return None

    async def after_copy_step(self, now: float | None = None) -> list[dict]:
        """После шага копитрейдинга: закрылась копия → продаём; новая копия подходящего кошелька → покупаем.
        На настоящем счёте — если подключён и включён, на демо — если демо включено. Потом — обучение."""
        now = time.time() if now is None else now
        events: list[dict] = []
        opened, closed = list(getattr(self.copy, "opened_now", [])), list(getattr(self.copy, "closed_now", []))
        self.copy.opened_now, self.copy.closed_now = [], []  # каждое событие обрабатываем один раз
        for key, pos, why in closed:
            rp = self.st["positions"].get(pos["mint"])
            if self.wallet and rp and rp.get("key") == key:
                e = await self.sell(pos["mint"], f"копия: {why}", now)
                if e:
                    events.append(e)
            dp = self.demo["positions"].get(pos["mint"])
            if dp and dp.get("key") == key:
                e = await self.demo_sell(pos["mint"], f"копия: {why}", now)
                if e and self._demo_loud():
                    events.append(e)
        for key in opened:
            pos = self.copy.st["open"].get(key)
            if not pos:
                continue
            w = self.copy.st["wallets"].get(pos["wallet"])
            if self.on:
                try:
                    events += await self._copy_real(key, pos, w, now)
                except Exception as e:  # сбой сети не должен мешать демо и следующим сделкам
                    log.exception("Покупка за кошельком")
                    self.last_error = str(e)[:200]
            if self.demo_on:
                try:
                    events += await self._copy_demo(key, pos, w, now)
                except Exception as e:
                    log.exception("Демо-покупка за кошельком")
                    self._decide(now, pos["symbol"], f"не купил: {str(e)[:120]}", self.demo)
        events += self.learn(now)
        return events

    async def _copy_real(self, key: str, pos: dict, w: dict | None, now: float) -> list[dict]:
        cat = self._in_scope(w)
        if not cat:
            return []
        why = self._why_not(pos, now)
        if why:
            self._decide(now, pos["symbol"], f"не купил: {why}")
            return []
        try:
            e = await self.buy(pos["mint"], self.st["size_usd"], source="copy", symbol=pos["symbol"], key=key,
                               wallet_name=w["name"], now=now, category=cat)
        except (SwapError, WalletError) as err:
            self._decide(now, pos["symbol"], f"не купил: {err}")
            if "мало SOL" in str(err):
                if now - self.st["low_sol_warned"] > 6 * 3600:
                    self.st["low_sol_warned"] = now
                    return [{"text": f"⚠️ {html.escape(w['name'])} купил {html.escape(pos['symbol'])}, но "
                                     f"{html.escape(str(err))}. Пока пополнишь — сделку повторит 🧪 демо-счёт.",
                             "buttons": []}]
                return []
            return [{"text": f"⚠️ Не купил {html.escape(pos['symbol'])} за {html.escape(w['name'])}: "
                             f"{html.escape(str(err))[:200]}", "buttons": []}]
        self._decide(now, pos["symbol"], f"купил за {'⭐' if cat == 'star' else '👀'} {w['name']}")
        return [e]

    async def _copy_demo(self, key: str, pos: dict, w: dict | None, now: float) -> list[dict]:
        d = self.demo
        cat = self._in_scope(w, demo=True)
        if not cat:
            return []
        why = self._why_not(pos, now, d)
        if why:
            self._decide(now, pos["symbol"], f"не купил: {why}", d)
            return []
        try:
            e = await self.demo_buy(pos["mint"], self.st["size_usd"], source="copy", symbol=pos["symbol"], key=key,
                                    wallet_name=w["name"], now=now, category=cat)
        except (SwapError, WalletError) as err:
            self._decide(now, pos["symbol"], f"не купил: {err}", d)
            return []
        self._decide(now, pos["symbol"], f"купил за {'⭐' if cat == 'star' else '👀'} {w['name']}", d)
        return [e] if self._demo_loud() else []

    def _exit_why(self, pos: dict, now: float) -> str | None:
        if pos.get("sell_why"):
            return pos["sell_why"]
        if pos["source"] == "copy" and pos.get("key") and pos["key"] not in self.copy.st["open"]:
            return "копия уже закрыта"
        if pos["source"] in ("copy", "mentor") and now - pos["opened"] > MAX_HOLD_SEC:
            return "48 часов"
        return None

    def _value_why(self, pos: dict) -> str | None:
        st = self.st
        if not pos["sol_in"] or not pos.get("value"):
            return None
        if pos["value"] <= pos["sol_in"] * (1 - st["stop_pct"]):
            return f"стоп −{st['stop_pct']:.0%}"
        if st["tp_pct"] and pos["value"] >= pos["sol_in"] * (1 + st["tp_pct"]):
            return f"тейк-профит {tp_text(st['tp_pct'])}"
        return None

    async def guard(self, now: float | None = None) -> list[dict]:
        """Каждый шаг: стоп и тейк по котировке, 48 часов, повтор неудачных продаж, копия уже закрыта.
        Для обоих счетов."""
        now = time.time() if now is None else now
        events: list[dict] = []
        if self.wallet:
            for mint, pos in list(self.st["positions"].items()):
                why = self._exit_why(pos, now)
                if not why:
                    try:
                        q = await self.jup.order(mint, WSOL, pos["raw"])
                        pos["value"] = int(q["outAmount"])
                        pos["checked"] = now
                        pos.pop("quote_err", None)
                    except SwapError as e:
                        pos["quote_err"] = str(e)[:120]
                        continue
                    why = self._value_why(pos)
                    self._changed()
                if why:
                    e = await self.sell(mint, why, now)
                    if e:
                        events.append(e)
        d = self.demo
        for mint, pos in list(d["positions"].items()):
            why = self._exit_why(pos, now)
            value = None
            if not why:
                value = await self._demo_value(pos)
                if value is None:
                    continue
                pos["value"] = value
                why = self._value_why(pos)
                self._changed()
            if why:
                e = await self.demo_sell(mint, why, now, value=value)
                if e and self._demo_loud():
                    events.append(e)
        return events

    # --- обучение ---------------------------------------------------------------------

    def _calibrate(self, roundtrip: float, now: float) -> None:
        lesson = self.copy.add_cost_sample(roundtrip, now)
        if lesson:
            self._pending.append({"text": lesson, "buttons": []})

    @property
    def _pending(self) -> list[dict]:
        if not hasattr(self, "_pending_events"):
            self._pending_events: list[dict] = []
        return self._pending_events

    def learn(self, now: float | None = None) -> list[dict]:
        """Учимся на закрытых копиях (их сотни — больше, чем сделок): стоп и тейк-профит, стоит ли копировать 👀.
        Плюс калибровка стоимости сделки по котировкам Jupiter (идёт при каждой покупке)."""
        now = time.time() if now is None else now
        events, self._pending_events = list(self._pending), []
        closed = self.copy.st["closed"]
        seen = [len(closed), closed[-1]["closed"] if closed else 0]
        lr = self.st["learn"]
        if lr["seen"] == seen:
            return events
        lr["seen"] = seen
        self._changed()
        st = self.st
        stop, tp, lesson = learn_exits(closed, st["stop_pct"], st["tp_pct"], self.copy.cost_side())
        if lesson:
            st["stop_pct"], st["tp_pct"] = stop, tp
            events.append({"text": self.copy._lesson(lesson, now), "buttons": []})
        # 👀 кошельки с хорошей историей, но без ⭐: стоит ли их копировать
        good = [t["ret"] for t in closed if t.get("passed") and t.get("wstatus") == "watch" and t.get("wgrade") == "good"]
        lr["good_n"] = len(good)
        lr["good_avg"] = round(sum(good) / (len(good) + 5), 2) if good else 0.0
        if len(good) >= GOOD_MIN:
            ok = lr["good_avg"] >= 2.0 if lr["good_ok"] is not True else lr["good_avg"] > 0.0
            if ok != lr["good_ok"]:
                lr["good_ok"] = ok
                if ok:
                    text = (f"🧠 Урок: 👀 кошельки с хорошей историей тоже окупаются в копиях — в среднем "
                            f"{lr['good_avg']:+.1f}% на копию за {len(good)} сделок (с поправкой на везение). Демо их "
                            "копирует; для настоящего счёта это можно включить.")
                    btn = [("✅ Копировать и 👀 на настоящем счёте", "cb:mt:scope")] if (
                        self.configured and st["scope"] == "star") else []
                else:
                    text = (f"🧠 Урок: 👀 кошельки без ⭐ в копиях теряют ({lr['good_avg']:+.1f}% в среднем за "
                            f"{len(good)}). Копирую только ⭐ — и на демо, и на настоящем счёте.")
                    btn = []
                    if st["scope"] == "good":
                        st["scope"] = "star"
                events.append({"text": self.copy._lesson(text, now), "buttons": btn})
        return events

    # --- показ ------------------------------------------------------------------

    def _positions(self, acc: dict, sol_usd: float) -> list[dict]:
        return [{"mint": m, "symbol": p["symbol"], "source": p["source"], "wallet": p["wallet"],
                 "category": p.get("category", "star"), "opened": p["opened"], "usd_in": p["usd_in"],
                 "pct": round((p["value"] / p["sol_in"] - 1) * 100, 1) if p.get("value") and p["sol_in"] else None,
                 "usd_now": round(p["value"] / LAMPORTS * sol_usd, 2) if p.get("value") and sol_usd else None,
                 "selling": bool(p.get("sell_why")), "sig": p.get("sig")}
                for m, p in acc["positions"].items()]

    def demo_dict(self) -> dict:
        d = self.demo
        sol_usd = self._sol_usd_cached()
        eq = self.demo_equity() if d["start_sol"] else 0
        s = d["stats"]
        return {"on": self.demo_on, "notify": d["notify"], "start_usd": d["start_usd"],
                "equity_usd": round(eq / LAMPORTS * sol_usd, 2) if eq and sol_usd else d["start_usd"],
                "cash_sol": round(d["cash"] / LAMPORTS, 4),
                "total_pct": round((eq / d["start_sol"] - 1) * 100, 1) if d["start_sol"] else 0.0,
                "positions": self._positions(d, sol_usd), "closed": list(reversed(d["closed"][-15:])),
                "decisions": list(reversed(d["decisions"][-12:])), "stats": s,
                "paused_until": d["paused_until"] if d["paused_until"] > time.time() else None,
                "day_pnl_usd": d["day_pnl_usd"] if d["day"] == _day(time.time()) else 0.0,
                "ready": self.demo_verdict(), "sizes": list(DEMO_SIZES)}

    def demo_verdict(self) -> dict:
        s = self.demo["stats"]
        if s["sells"] < DEMO_READY_TRADES:
            return {"state": "wait", "text": f"нужно ещё {DEMO_READY_TRADES - s['sells']} демо-сделок, чтобы судить"}
        if s["pnl_usd"] > 0:
            small = " (выборка пока маленькая — не больше $5–10)" if s["sells"] < 30 else ""
            return {"state": "ok", "text": f"демо в плюсе ({s['pnl_usd']:+.2f}$ за {s['sells']} сделок) — можно "
                                           f"пробовать настоящий кошелёк с небольшой суммой{small}"}
        return {"state": "bad", "text": f"демо в минусе ({s['pnl_usd']:+.2f}$ за {s['sells']}) — с настоящими деньгами "
                                        "лучше подождать, пока бот подстроится"}

    def status_dict(self) -> dict:
        st = self.st
        at, bal = self._balance
        sol_usd = self._sol_usd_cached()
        lr = st["learn"]
        return {
            "configured": self.configured, "error": self.key_error, "address": self.address,
            "balance_sol": round(bal / LAMPORTS, 4) if bal is not None else None,
            "balance_usd": round(bal / LAMPORTS * sol_usd, 2) if bal is not None and sol_usd else None,
            "funded": bal is not None and bal - int(st["reserve_sol"] * LAMPORTS) >= int(
                st["size_usd"] / sol_usd * LAMPORTS) if sol_usd and bal is not None else None,
            "on": self.on, "size_usd": st["size_usd"], "max_open": st["max_open"], "scope": st["scope"],
            "stop_pct": st["stop_pct"], "tp_pct": st["tp_pct"], "daily_loss_usd": st["daily_loss_usd"],
            "reserve_sol": st["reserve_sol"],
            "paused_until": st["paused_until"] if st["paused_until"] > time.time() else None,
            "day_pnl_usd": st["day_pnl_usd"] if st["day"] == _day(time.time()) else 0.0,
            "positions": self._positions(st, sol_usd),
            "closed": list(reversed(st["closed"][-15:])), "decisions": list(reversed(st["decisions"][-12:])),
            "stats": st["stats"], "sizes": list(SIZES), "last_error": self.last_error,
            "demo": self.demo_dict(),
            "learned": {"cost_pct": round(self.copy.cost_side() * 200, 2),
                        "calibrated": bool((self.copy.st.get("calib") or {}).get("cost")),
                        "good_ok": lr["good_ok"], "good_n": lr["good_n"], "good_avg": lr["good_avg"]},
        }

    def demo_text(self) -> str:
        d = self.demo_dict()
        st = self.st
        lines = [f"🧪 <b>Демо-счёт</b> (виртуальные SOL, настоящие котировки Jupiter): "
                 f"{'🟢 торгует' if d['on'] else '⏸ выключен'}",
                 f"Баланс: <b>${d['equity_usd']:.2f}</b> из ${d['start_usd']:g} ({d['total_pct']:+.1f}%)"]
        if d["on"] and not self.copy.on:
            lines.append("⚠️ Копитрейдинг выключен — без него нет сделок. Включи: /copy")
        good = st["learn"]["good_ok"]
        lines.append("Копирует: ⭐" + (" и 👀 с хорошей историей (чтобы учиться быстрее)" if good is not False else
                                       " (👀 доказали, что теряют)"))
        if d["paused_until"]:
            lines.append(f"⏸ Пауза после убытков до {_hm(d['paused_until'])}")
        for p in d["positions"]:
            now_ = f" ({p['pct']:+.1f}%)" if p["pct"] is not None else ""
            src = (f"{CAT_ICON.get(p['category'], '👀')} {html.escape(p['wallet'])}" if p["source"] in ("copy", "mentor")
                   else "вручную")
            lines.append(f"• <b>{html.escape(p['symbol'])}</b> ${p['usd_in']:g}{now_} · {src} · "
                         f"{int((time.time() - p['opened']) // 60)} мин")
        s = d["stats"]
        if s["sells"]:
            lines.append(f"Сделок {s['sells']}, в плюс {s['wins']}, итог <b>{s['pnl_usd']:+.2f}$</b>")
        icon = {"wait": "⏳", "ok": "✅", "bad": "⚠️"}[d["ready"]["state"]]
        lines.append(f"{icon} {d['ready']['text']}")
        return "\n".join(lines)

    def rules_text(self) -> str:
        st = self.st
        learned = self.status_dict()["learned"]
        return (f"⚙️ <b>Правила</b> (бот подстраивает сам): сумма ${st['size_usd']:g} · до {st['max_open']} позиций · "
                f"стоп −{st['stop_pct']:.0%} · тейк-профит {tp_text(st['tp_pct'])} · вход+выход ≈ {learned['cost_pct']:.1f}%"
                + (" (замерено по Jupiter)" if learned["calibrated"] else " (пока оценка)")
                + f" · лимит убытка ${st['daily_loss_usd']:g}/день")

    def text(self) -> str:
        if not self.configured:
            return ("💸 <b>Торговля мемкоинами</b>\n" + (f"⚠️ {html.escape(self.key_error)}\n" if self.key_error else "")
                    + "Настоящий кошелёк не подключён — бот торгует на демо-счёте и учится.\n\n"
                    + self.demo_text() + "\n\n" + self.rules_text()
                    + "\n\nКупить на демо вручную: <code>/meme buy АДРЕС_ТОКЕНА 2</code>"
                    + "\nПодключить настоящий кошелёк (ключ SOLANA_TRADER_KEY в Render) — кнопка «❔ Как подключить кошелёк».")
        d = self.status_dict()
        bal = (f"{d['balance_sol']:.4f} SOL" + (f" ≈ ${d['balance_usd']:.2f}" if d["balance_usd"] else "")
               if d["balance_sol"] is not None else "не узнал")
        lines = ["💸 <b>Торговля мемкоинами</b> (Solana · Jupiter)",
                 f"Кошелёк бота: <code>{d['address']}</code>",
                 f"Баланс: <b>{bal}</b>"]
        if d["funded"] is False:
            lines.append("⚠️ На кошельке мало SOL для сделки — пока пополнишь, торгует 🧪 демо-счёт.")
        lines.append(f"Автокопирование: {'🟢 включено' if d['on'] else '⏸ выключено'} · "
                     f"{'только ⭐' if d['scope'] == 'star' else '⭐ + 👀 с хорошей историей'}")
        if d["paused_until"]:
            lines.append(f"⏸ Пауза после убытков до {_hm(d['paused_until'])}")
        if d["on"] and not self.copy.on:
            lines.append("⚠️ Копитрейдинг выключен — без него нет сигналов. Включи: /copy")
        if d["positions"]:
            lines.append("\n<b>Позиции</b>:")
            for p in d["positions"]:
                now_ = f" → ${p['usd_now']:.2f} ({p['pct']:+.1f}%)" if p["pct"] is not None and p["usd_now"] else ""
                src = (f"копия {CAT_ICON.get(p['category'], '👀')} {html.escape(p['wallet'])}"
                       if p["source"] in ("copy", "mentor") else "вручную")
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
        lines.append("\n" + self.demo_text())
        lines.append("\n" + self.rules_text())
        if self.last_error:
            lines.append(f"\n⚠️ {html.escape(self.last_error)[:200]}")
        lines.append("\nКупить вручную: <code>/meme buy АДРЕС_ТОКЕНА 2</code> · продать: <code>/meme sell СИМВОЛ</code>")
        return "\n".join(lines)

    def short_line(self) -> str:
        parts = []
        if self.configured:
            s = self.st["stats"]
            parts.append(f"💸 <b>Мемкоины (реально)</b>: {'🟢' if self.on else '⏸'} позиций {len(self.st['positions'])}"
                         + (f", продаж {s['sells']}, в плюс {s['wins']}, {s['pnl_usd']:+.2f}$" if s["sells"] else "")
                         + " — /meme")
        if self.demo_on or self.demo["stats"]["sells"]:
            d = self.demo_dict()
            s = d["stats"]
            parts.append(f"🧪 <b>Демо мемкоинов</b>: ${d['equity_usd']:.2f} из ${d['start_usd']:g} ({d['total_pct']:+.1f}%)"
                         + (f", сделок {s['sells']}, в плюс {s['wins']}" if s["sells"] else "") + " — /meme")
        return "\n".join(parts)

    def find(self, query: str, demo: bool = False) -> str | None:
        """Позиция по адресу или символу."""
        acc = self._acc(demo)
        q = (query or "").strip()
        if q in acc["positions"]:
            return q
        for m, p in acc["positions"].items():
            if p["symbol"].lower() == q.lower().lstrip("$"):
                return m
        return None
