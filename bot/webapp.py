"""Telegram Mini App: приложение внутри бота.

Сервер — тот же aiohttp, что отвечает Render на /health. Отдаёт:
  /app            — само приложение (HTML + CSS + JS без внешних библиотек, сжатые gzip);
  /api/...        — JSON API. Каждый запрос подписан Telegram (initData), подпись проверяется
                    токеном бота, поэтому подделать пользователя нельзя.

Читать данные могут пользователи из ALLOWED_USER_IDS, торговать и менять настройки — только владелец.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

from aiohttp import web

from . import extras_ui
from .ai import AIError
from .alerts import AlertError, build_alert, parse_alert
from .config import BOUNDS, EDITABLE, RISK_KEYS
from .demo_manager import REASONS, account_label
from .opportunities import PLAN_QUESTION
from .dex import SOL_ADDRESS_RE, DexError, best_pairs, is_address
from .doctor import doctor_dict, run_doctor
from .solana import WalletError
from .trading import STABLES, OrderPlan, TradeError, human_error

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "webapp"
INIT_DATA_MAX_AGE = 24 * 3600  # подпись Telegram действует сутки
MARKET_CACHE_SEC = 10
TICKERS_CACHE_SEC = 30
TIMEFRAMES = {"5m", "15m", "1h", "4h", "1d"}
CURVE_POINTS = 150


# ---------------------------------------------------------------------------
# Проверка подписи Telegram
# ---------------------------------------------------------------------------


def check_init_data(init_data: str, token: str, max_age: float = INIT_DATA_MAX_AGE) -> dict | None:
    """Проверяет initData из Telegram.WebApp по алгоритму из документации Telegram.

    Возвращает данные пользователя или None, если подпись не сходится или устарела.
    """
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    if max_age and time.time() - auth_date > max_age:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def sign_init_data(user: dict, token: str, auth_date: int | None = None) -> str:
    """Подписывает initData так же, как Telegram (для тестов и локального просмотра)."""
    from urllib.parse import urlencode

    pairs = {"auth_date": str(auth_date or int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


# ---------------------------------------------------------------------------
# Вспомогательное
# ---------------------------------------------------------------------------


_RequestKey = getattr(web, "RequestKey", None)  # aiohttp ≥ 3.12; в старых версиях — обычные строки
USER = _RequestKey("user", dict) if _RequestKey else "user"
OWNER = _RequestKey("owner", bool) if _RequestKey else "owner"
PREVIEW = _RequestKey("preview", bool) if _RequestKey else "preview"


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _r(x, n=6):
    return None if x is None else round(float(x), n)


def downsample(points: list, limit: int = CURVE_POINTS) -> list:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    out = [points[int(i * step)] for i in range(limit - 1)]
    out.append(points[-1])
    return out


def account_curve(acc) -> list[list[float]]:
    """Кривая капитала демо-счёта: старт и баланс после каждой закрытой сделки."""
    points = [[acc.created_at, acc.start_balance]]
    balance = acc.start_balance
    for t in acc.trades:
        balance += t.profit_usd
        points.append([t.closed_at or t.opened_at, round(balance, 6)])
    if acc.active or len(points) == 1:
        points.append([time.time(), round(acc.equity, 6)])
    return downsample(points)


def plan_dict(plan: OrderPlan, trader) -> dict:
    return {
        "id": plan.id, "exchange": plan.exchange, "name": trader.scanner.display_name(plan.exchange),
        "symbol": plan.symbol, "side": plan.side, "type": plan.type,
        "spent": plan.spent, "spent_ccy": plan.spent_ccy, "got": plan.est_got, "got_ccy": plan.got_ccy,
        "price": plan.est_price, "limit_price": plan.price, "fee_pct": plan.fee_rate * 100,
        "usd": plan.usd_value, "warnings": plan.warnings, "source": plan.source,
        "live": trader.state.live, "expires_in": max(0, 300 - (time.time() - plan.created_at)),
        "slippage_pct": trader.settings.max_slippage_percent,
    }


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------


class WebApp:
    def __init__(self, bot: ArbitrageBot, token: str, static_dir: Path = STATIC_DIR):
        self.b = bot
        self.token = token
        self.static_dir = static_dir
        dev = os.getenv("WEBAPP_DEV_USER", "").strip()
        self.dev_user = int(dev) if dev.lstrip("-").isdigit() else None  # только для локального просмотра
        self._static: dict[str, tuple[bytes, bytes, str]] = {}
        self._version = "0"
        self._market_cache: dict[tuple, tuple[float, dict]] = {}
        self._doctor_cache: dict | None = None
        self._tickers_cache: dict[str, tuple[float, list]] = {}
        self._scan_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()  # фоновые сохранения: держим ссылку, чтобы задачу не съел сборщик
        self._copy_search: asyncio.Task | None = None  # поиск кошельков из приложения (идёт в фоне)
        self._load_static()

    # --- статика ------------------------------------------------------------

    def _load_static(self) -> None:
        types = {".html": "text/html", ".css": "text/css", ".js": "application/javascript",
                 ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}
        digest = hashlib.sha1()
        files = sorted(p for p in self.static_dir.glob("*") if p.is_file()) if self.static_dir.exists() else []
        for p in files:
            digest.update(p.read_bytes())
        self._version = digest.hexdigest()[:10]
        for p in files:
            data = p.read_bytes()
            if p.suffix == ".html":
                data = data.replace(b"{{V}}", self._version.encode())
            ctype = types.get(p.suffix, "application/octet-stream")
            self._static[p.name] = (data, gzip.compress(data, 9), ctype)

    def _file(self, request: web.Request, name: str, cache: bool) -> web.Response:
        item = self._static.get(name)
        if item is None:
            raise web.HTTPNotFound()
        raw, packed, ctype = item
        headers = {
            "Cache-Control": "public, max-age=31536000, immutable" if cache else "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Vary": "Accept-Encoding",
        }
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return web.Response(body=packed, content_type=ctype, charset="utf-8", headers=headers)
        return web.Response(body=raw, content_type=ctype, charset="utf-8", headers=headers)

    async def index(self, request: web.Request) -> web.Response:
        return self._file(request, "index.html", cache=False)

    async def static(self, request: web.Request) -> web.Response:
        return self._file(request, request.match_info["name"], cache=True)

    # --- маршруты -----------------------------------------------------------

    def setup(self, app: web.Application) -> None:
        app.middlewares.append(self._middleware)
        r = app.router
        r.add_get("/app", self.index)
        r.add_get("/app/", self.index)
        r.add_get("/app/s/{name}", self.static)
        routes = {
            ("GET", "/api/overview"): self.overview,
            ("GET", "/api/portfolio"): self.portfolio,
            ("GET", "/api/market"): self.market,
            ("GET", "/api/symbols"): self.symbols,
            ("GET", "/api/orders"): self.orders,
            ("GET", "/api/demo"): self.demo,
            ("POST", "/api/scan"): self.scan,
            ("POST", "/api/plan"): self.plan,
            ("POST", "/api/execute"): self.execute,
            ("POST", "/api/cancel_plan"): self.cancel_plan,
            ("POST", "/api/triangle"): self.triangle,
            ("POST", "/api/cancel"): self.cancel,
            ("POST", "/api/mode"): self.mode,
            ("POST", "/api/auto"): self.auto,
            ("POST", "/api/scalp"): self.scalp,
            ("POST", "/api/stop"): self.stop,
            ("POST", "/api/demo/start"): self.demo_start,
            ("POST", "/api/demo/stop"): self.demo_stop,
            ("POST", "/api/ai"): self.ai,
            ("POST", "/api/set"): self.set_setting,
            ("GET", "/api/phantom"): self.phantom,
            ("POST", "/api/phantom"): self.phantom_set,
            ("GET", "/api/doctor"): self.doctor,
            ("GET", "/api/alerts"): self.alerts,
            ("POST", "/api/alerts"): self.alert_add,
            ("POST", "/api/alerts/delete"): self.alert_delete,
            ("GET", "/api/price"): self.price,
            ("GET", "/api/autostatus"): self.autostatus,
            ("GET", "/api/dex/search"): self.dex_search,
            ("GET", "/api/dex/hot"): self.dex_hot,
            ("GET", "/api/dex/rug"): self.dex_rug,
            ("POST", "/api/radar"): self.radar,
            ("GET", "/api/tinvest"): self.tinvest,
            ("GET", "/api/opps"): self.opps,
            ("POST", "/api/opps/notify"): self.opps_notify,
            ("GET", "/api/copy"): self.copy,
            ("POST", "/api/copy"): self.copy_set,
        }
        for (method, path), handler in routes.items():
            r.add_route(method, path, self._wrap(handler))

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        init = request.headers.get("X-Telegram-Init-Data", "")
        user = check_init_data(init, self.token)
        request[PREVIEW] = False
        if user is None and self.dev_user is not None and not init:
            user = {"id": self.dev_user, "first_name": "Dev"}
            request[PREVIEW] = True
        if user is None:
            return web.json_response({"error": "Открой приложение из Telegram (подпись не прошла проверку)"}, status=401)
        uid = user["id"]
        if self.b.allowed_ids and uid not in self.b.allowed_ids:
            return web.json_response({"error": "Доступ закрыт. Попроси владельца добавить твой ID в ALLOWED_USER_IDS"},
                                     status=403)
        request[USER] = user
        request[OWNER] = self.b.owner_id is not None and uid == self.b.owner_id
        return await handler(request)

    def _wrap(self, handler):
        async def inner(request: web.Request) -> web.Response:
            try:
                data = await handler(request)
            except ApiError as e:
                return web.json_response({"error": str(e)}, status=e.status)
            except TradeError as e:
                return web.json_response({"error": str(e)}, status=400)
            except web.HTTPException:
                raise
            except Exception as e:
                log.exception("Ошибка API %s", request.path)
                return web.json_response({"error": human_error(e)}, status=500)
            resp = web.json_response(data, dumps=lambda x: json.dumps(x, ensure_ascii=False, default=str))
            resp.headers["Cache-Control"] = "no-store"
            if len(resp.body or b"") > 1024:
                resp.enable_compression()
            return resp

        return inner

    @staticmethod
    async def _body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise ApiError("неверный запрос") from None
        if not isinstance(data, dict):
            raise ApiError("неверный запрос")
        return data

    @staticmethod
    def _owner(request: web.Request) -> None:
        if not request[OWNER]:
            raise ApiError("Это действие доступно только владельцу бота", 403)

    def _changed(self, notify: str | None = None) -> None:
        """После изменения: сохраняем локально сразу, бэкап и сообщение в чат — в фоне, чтобы не тормозить ответ."""
        self.b.save_trading()

        async def later():
            try:
                await self.b._backup_now()
                if notify and self.b.owner_id:
                    await self.b._send(self.b.owner_id, notify)
            except Exception:
                log.exception("Фоновое сохранение после действия в приложении")

        task = asyncio.get_running_loop().create_task(later())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- данные -------------------------------------------------------------

    def _name(self, ex: str) -> str:
        return self.b.scanner.display_name(ex)

    def _scan_dict(self) -> dict | None:
        res = self.b.scanner.last_result
        if res is None:
            return None
        working = set(self.b.trader.working)
        wd_fallback = self.b.settings.demo_withdraw_fee_usd
        from .telegram_bot import withdraw_usd

        cross = []
        for o in res.opportunities[:20]:
            wd, known = withdraw_usd(o, self.b.scanner, wd_fallback)
            d = o.depth
            cross.append({
                "key": "|".join(o.key), "symbol": o.symbol,
                "buy": o.buy.exchange, "buy_name": self._name(o.buy.exchange), "ask": o.buy.ask,
                "sell": o.sell.exchange, "sell_name": self._name(o.sell.exchange), "bid": o.sell.bid,
                "gross": _r(o.gross_percent, 4), "net": _r(o.net_percent, 4),
                "depth_pct": _r(d.profit_percent, 4) if d else None, "depth_usd": _r(d.profit_usd, 4) if d else None,
                "filled": bool(d and d.filled), "depth_error": o.depth_error,
                "withdraw_usd": _r(wd, 4), "withdraw_known": known,
                "after_withdraw_usd": _r(d.profit_usd - wd, 4) if d else None,
                "volume": min(o.buy.volume_usd, o.sell.volume_usd),
            })
        tri = []
        for t in res.triangles[:20]:
            d = t.depth
            tri.append({
                "key": "|".join(t.key), "exchange": t.exchange, "name": self._name(t.exchange),
                "path": t.path, "legs": [{"symbol": leg.symbol, "side": leg.side, "price": leg.price} for leg in t.legs],
                "gross": _r(t.gross_percent, 4), "net": _r(t.net_percent, 4),
                "depth_pct": _r(d.profit_percent, 4) if d else None, "depth_usd": _r(d.profit_usd, 4) if d else None,
                "filled": bool(d and d.filled), "depth_error": t.depth_error,
                "fee_pct": _r(t.legs[0].fee * 100, 4),
                "executable": t.exchange in working,
            })

        def near(n):
            return {"net": _r(n.net_percent, 4), "gross": _r(n.gross_percent, 4), "text": n.text} if n else None

        return {
            "finished_at": res.finished_at, "duration": _r(res.duration_sec, 2),
            "exchanges_ok": [self._name(x) for x in res.exchanges_ok],
            "errors": {self._name(k): v for k, v in res.errors.items()},
            "symbols": res.symbols_compared, "cross": cross, "tri": tri,
            "near_cross": near(res.near_cross), "near_tri": near(res.near_tri),
            "trade_amount": self.b.settings.trade_amount_usd,
        }

    def _demo_dict(self, with_trades: int = 0) -> dict:
        dm = self.b.demo
        accounts = []
        for aid, a in sorted(dm.accounts.items(), key=lambda kv: (kv[1].strategy, kv[1].start_balance)):
            wins = sum(1 for t in a.trades if t.profit_usd > 0)
            top_reject = max(a.rejects.items(), key=lambda kv: kv[1]) if a.rejects else None
            accounts.append({
                "id": aid, "label": account_label(a), "strategy": a.strategy, "active": a.active,
                "start": a.start_balance, "equity": round(a.equity, 6),
                "pct": round((a.equity / a.start_balance - 1) * 100, 4) if a.start_balance else 0,
                "trades": len(a.trades), "wins": wins, "scans": a.scans,
                "location": self._name(a.location) if a.location else None,
                "in_transit": a.open_trade.symbol if a.open_trade else None,
                "top_reject": {"reason": REASONS.get(top_reject[0], top_reject[0]), "count": top_reject[1]} if top_reject else None,
                "best_rejected": a.best_rejected,
                "curve": account_curve(a),
            })
        out = {
            "active": dm.active, "created_at": dm.created_at, "accounts": accounts,
            "total_start": sum(a["start"] for a in accounts), "total_equity": sum(a["equity"] for a in accounts),
            "default_amounts": list(self.b.settings.demo_amounts),
        }
        if with_trades:
            rows = [(t, a) for a in dm.accounts.values() for t in a.trades]
            rows.sort(key=lambda r: r[0].closed_at or r[0].opened_at, reverse=True)
            out["history"] = [{
                "at": t.closed_at or t.opened_at, "account": account_label(a), "strategy": t.strategy,
                "symbol": t.symbol, "route": self._name(t.buy_exchange) if t.strategy == "tri"
                else f"{self._name(t.buy_exchange)} → {self._name(t.sell_exchange)}",
                "profit": round(t.profit_usd, 6), "pct": round(t.profit_percent, 4), "expected": round(t.signal_percent, 4),
                "cost": round(t.cost_usd, 4),
            } for t, a in rows[:with_trades]]
        return out

    def _settings_dict(self) -> dict:
        s = self.b.settings
        return {key: {"value": float(getattr(s, attr)), "desc": desc, "min": BOUNDS.get(key, (0, 0))[0],
                      "max": BOUNDS.get(key, (0, 1e9))[1], "risk": key in RISK_KEYS}
                for key, (attr, desc) in EDITABLE.items()}

    def _trading_dict(self) -> dict:
        t = self.b.trader
        t._roll_day()
        st = t.state
        cached_total = sum(sum(r[3] or 0 for r in rows) for _, rows in t.balance_cache.values())
        return {
            "live": st.live, "auto": st.auto, "paused_reason": st.paused_reason,
            "keys": [{"exchange": n, "name": self._name(n), "status": t.key_status.get(n, "unchecked")} for n in t.creds],
            "key_warnings": t.key_warnings(),
            "auto_status": t.auto_status_dict(),
            "scalp": self.b.scalper.status_dict(),
            "duplicate": self.b.duplicate_running(),
            "working": t.working,
            "today": {"trades": st.day_trades, "pnl": round(st.day_pnl_usd, 6)},
            "limits": {"max_trade": self.b.settings.max_trade_usd, "daily_loss": self.b.settings.daily_loss_usd,
                       "daily_trades": self.b.settings.daily_trades, "auto_min": self.b.settings.auto_min_profit_percent,
                       "slippage": self.b.settings.max_slippage_percent},
            "history": downsample(st.history, 300),
            "cached_total": round(cached_total, 4) if t.balance_cache else None,
            "log": list(reversed(st.log[-50:])),
        }

    async def overview(self, request: web.Request) -> dict:
        st = self.b.stats
        return {
            "user": {"id": request[USER]["id"], "name": request[USER].get("first_name", ""), "owner": request[OWNER]},
            "server_time": time.time(),
            "preview": request[PREVIEW],
            "trading": self._trading_dict(),
            "demo": self._demo_dict(),
            "scan": self._scan_dict(),
            "stats": {"scans": st.scans, "failed": st.failed_scans, "uptime_h": round((time.time() - st.started_at) / 3600, 2),
                      "best_tri": st.best_tri, "best_cross": st.best_cross},
            "exchanges": [{"id": n, "name": self._name(n)} for n in self.b.scanner.exchanges],
            "quote": self.b.settings.quote,
            "settings": self._settings_dict(),
            "ai": self.b.ai.configured,
            "auto_signals": request[USER]["id"] in self.b.subscribers.chats,
            "interval": self.b.settings.scan_interval_sec,
            "copy": self._copy_brief(),
        }

    def _copy_brief(self) -> dict:
        """Коротко для главного экрана: полный список — в /api/copy."""
        ct = self.b.copy
        ws = ct.st["wallets"].values()
        stars = [w["name"] for w in ws if w["status"] == "star"]
        last = ct.st["lessons"][-1]["text"] if ct.st["lessons"] else None
        return {"on": ct.on, "wallets": len(ct.active()), "stars": stars[:3], "star_count": len(stars),
                "open": len(ct.st["open"]), "summary": ct.summary(), "lesson": last}

    async def portfolio(self, request: web.Request) -> dict:
        if not self.b.trader.creds:
            return {"total_usd": 0, "exchanges": [], "history": [], "no_keys": True}
        data = await self.b.trader.portfolio()
        data["history"] = downsample(self.b.trader.state.history, 300)
        self.b.save_trading()
        return data

    def _exchange(self, raw: str | None) -> str:
        if not raw:
            return next(iter(self.b.trader.working or self.b.scanner.exchanges), "")
        try:
            return self.b.trader.resolve_exchange(raw)
        except TradeError as e:
            raise ApiError(str(e)) from None

    async def market(self, request: web.Request) -> dict:
        q = request.query
        name = self._exchange(q.get("ex"))
        symbol = (q.get("symbol") or f"BTC/{self.b.settings.quote}").upper()
        tf = q.get("tf") if q.get("tf") in TIMEFRAMES else "15m"
        key = (name, symbol, tf)
        cached = self._market_cache.get(key)
        if cached and time.time() - cached[0] < MARKET_CACHE_SEC:
            return cached[1]
        ex = self.b.scanner.exchanges[name]
        await self.b.scanner._ensure_markets(name, ex)
        if symbol not in (ex.markets or {}):
            raise ApiError(f"на {self._name(name)} нет пары {symbol}")
        candles, ticker = await asyncio.gather(ex.fetch_ohlcv(symbol, tf, limit=120), ex.fetch_ticker(symbol))
        m = self.b.scanner.market(name, symbol)
        data = {
            "exchange": name, "name": self._name(name), "symbol": symbol, "tf": tf,
            "candles": [[c[0], c[1], c[2], c[3], c[4], c[5] or 0] for c in candles if c and c[4] is not None],
            "ticker": {"last": ticker.get("last") or ticker.get("bid") or (candles[-1][4] if candles else None), "bid": ticker.get("bid"), "ask": ticker.get("ask"),
                       "change": ticker.get("percentage"), "high": ticker.get("high"), "low": ticker.get("low"),
                       "volume": ticker.get("quoteVolume")},
            "fee_pct": float(m.get("taker") or 0.001) * 100,
            "min_cost": float(((m.get("limits") or {}).get("cost") or {}).get("min") or 0),
        }
        self._market_cache[key] = (time.time(), data)
        if len(self._market_cache) > 100:
            self._market_cache.pop(next(iter(self._market_cache)))
        return data

    def trim_caches(self, everything: bool = False) -> int:
        """Убирает устаревшие (или все) кэши графиков и списков пар. Возвращает, сколько записей убрано."""
        now = time.time()
        n = 0
        for cache, ttl in ((self._market_cache, MARKET_CACHE_SEC), (self._tickers_cache, TICKERS_CACHE_SEC)):
            for k in [k for k, (at, _) in cache.items() if everything or now - at > ttl]:
                del cache[k]
                n += 1
        if everything and self._doctor_cache is not None:
            self._doctor_cache = None
            n += 1
        return n

    async def symbols(self, request: web.Request) -> dict:
        name = self._exchange(request.query.get("ex"))
        cached = self._tickers_cache.get(name)
        if not cached or time.time() - cached[0] > TICKERS_CACHE_SEC:
            tickers = await self.b.scanner._fetch_spot_tickers(name, self.b.scanner.exchanges[name])
            quote = self.b.settings.quote
            rows = []
            for s, t in tickers.items():
                m = self.b.scanner.market(name, s)
                last = t.get("last") or t.get("bid") or t.get("ask")
                if m.get("quote") != quote or not last or m.get("base") in STABLES:
                    continue
                rows.append([s, last, t.get("percentage"), t.get("quoteVolume") or 0])
            rows.sort(key=lambda r: -(r[3] or 0))
            cached = (time.time(), rows[:400])
            self._tickers_cache[name] = cached
        return {"exchange": name, "symbols": cached[1]}

    async def orders(self, request: web.Request) -> dict:
        t = self.b.trader
        out = []
        for name in t.working:
            try:
                orders = await t.open_orders(name)
            except TradeError as e:
                out.append({"exchange": name, "name": self._name(name), "error": str(e)})
                continue
            out.append({"exchange": name, "name": self._name(name), "orders": [
                {"id": str(o.get("id")), "symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"),
                 "price": o.get("price"), "amount": o.get("amount"), "filled": o.get("filled"),
                 "at": (o.get("timestamp") or 0) / 1000} for o in orders]})
        return {"exchanges": out}

    async def demo(self, request: web.Request) -> dict:
        return self._demo_dict(with_trades=100)

    # --- действия -----------------------------------------------------------

    async def scan(self, request: web.Request) -> dict:
        async with self._scan_lock:  # два нажатия подряд = один скан
            last = self.b.scanner.last_result
            if last is None or time.time() - last.finished_at > 5:
                await self.b._scan()
        return {"scan": self._scan_dict()}

    async def plan(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        t = self.b.trader
        name = self._exchange(body.get("exchange"))
        side = body.get("side")
        if side not in ("buy", "sell"):
            raise ApiError("side: buy или sell")
        coin = str(body.get("coin") or "").upper().split("/")[0]
        amount = str(body.get("amount") or "").strip()
        if not coin or not amount:
            raise ApiError("укажи монету и сумму")
        if body.get("type") == "limit":
            try:
                price = float(str(body.get("price")).replace(",", "."))
            except (TypeError, ValueError):
                raise ApiError("цена должна быть числом") from None
            plan = await t.plan_limit(name, side, coin, amount, price)
        else:
            plan = await t.plan_market(name, side, coin, amount)
        return {"plan": plan_dict(plan, t)}

    async def cancel_plan(self, request: web.Request) -> dict:
        body = await self._body(request)
        self.b.trader.pending.pop(str(body.get("id")), None)
        return {"ok": True}

    async def execute(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        t = self.b.trader
        rec = await t.execute_plan(str(body.get("id")))
        self._changed(("📱 " + t.record_text(rec)) if rec.live else None)
        return {"record": rec.__dict__, "live": rec.live}

    async def triangle(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        key = str(body.get("key"))
        res = self.b.scanner.last_result
        opp = next((o for o in (res.triangles if res else []) if "|".join(o.key) == key), None)
        if opp is None:
            raise ApiError("сигнал устарел, обнови сканирование")
        ex_res = await self.b.trader.execute_triangle(opp, source="manual")
        self._changed(("📱 " + ex_res.text(self._name)) if ex_res.live and ex_res.executed else None)
        return {
            "executed": ex_res.executed, "live": ex_res.live, "amount": ex_res.amount, "final": ex_res.final,
            "pnl": ex_res.pnl, "reason": ex_res.reason, "steps": ex_res.steps, "stuck": ex_res.stuck, "path": opp.path_text,
        }

    async def cancel(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        name = self._exchange(body.get("exchange"))
        await self.b.trader.cancel(name, str(body.get("id")), str(body.get("symbol")))
        return {"ok": True}

    async def mode(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        t = self.b.trader
        if body.get("live"):
            if body.get("confirm") != "LIVE":
                raise ApiError("нужно подтверждение")
            if not t.working:
                raise ApiError("сначала подключи ключ биржи")
            t.state.live = True
            t.pending.clear()  # тестовые предпросмотры не должны исполниться реально
            self._changed("🔴 <b>Реальный режим включён</b> из приложения. Остановить всё: /stop")
        else:
            t.state.live = False
            self._changed("🧪 Тестовый режим включён из приложения.")
        return {"live": t.state.live}

    async def auto(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        t = self.b.trader
        on = bool(body.get("on"))
        if on and not t.working:
            raise ApiError("для автоторговли нужен рабочий ключ биржи")
        if on and not self.b.settings.triangular_enabled:
            raise ApiError("треугольный арбитраж выключен (TRIANGULAR=0)")
        was_on = t.state.auto
        t.state.auto = on
        if on and self.b.scalper.on:
            self.b.scalper.turn_off()  # обе стратегии берут одни и те же USDT
        if on:
            t.state.paused_reason = None
            if not was_on:
                t.reset_auto_stats()
                self.b._auto_reports = 0
                self.b._auto_warned.clear()
        self._changed(f"⚙️ Автоторговля {'включена' if on else 'выключена'} из приложения "
                      f"({'реальный режим' if t.state.live else 'тест'}).")
        return {"auto": on}

    async def scalp(self, request: web.Request) -> dict:
        """Режим $5 (автопилот) из приложения: {on} — включить/выключить, {coins} — «Монеты в дело»."""
        self._owner(request)
        body = await self._body(request)
        t, sc = self.b.trader, self.b.scalper
        if "coins" in body:
            sc.st["use_coins"] = bool(body["coins"])
            self._changed("🪙 Монеты в дело: " + ("включено из приложения — лежащие монеты бот продаст в USDT и пустит "
                                                  "в торговлю (только в реальном режиме)." if sc.st["use_coins"]
                                                  else "выключено из приложения."))
            if "on" not in body:
                return {"scalp": sc.status_dict(), "auto": t.state.auto}
        on = bool(body.get("on"))
        if on and not t.working:
            raise ApiError("для режима $5 нужен рабочий ключ биржи")
        if on:
            note = ""
            if t.state.auto:
                t.state.auto = False
                note = " Треугольники выключены: деньги теперь нужны режиму $5."
            sc.turn_on()
            self._changed(f"🎯 Режим $5 включён из приложения ({'реальный режим' if t.state.live else 'тест'}).{note} "
                          "Состояние: /scalp")
        else:
            sc.turn_off()
            self._changed("⏸ Режим $5 выключен из приложения.")
        return {"scalp": sc.status_dict(), "auto": t.state.auto}

    async def stop(self, request: web.Request) -> dict:
        self._owner(request)
        text = await self.b.trader.panic()
        self._changed("📱 " + text)
        return {"text": html.unescape(text.replace("<b>", "").replace("</b>", ""))}

    async def demo_start(self, request: web.Request) -> dict:
        body = await self._body(request)
        try:
            amounts = sorted({float(x) for x in (body.get("amounts") or self.b.settings.demo_amounts)})
        except (TypeError, ValueError):
            raise ApiError("суммы должны быть числами") from None
        if not amounts or len(amounts) > 5 or not all(1 <= a <= 1_000_000 for a in amounts):
            raise ApiError("до 5 сумм, каждая от 1 до 1 000 000 $")
        uid = request[USER]["id"]
        self.b.demo.start(amounts, uid)
        self._changed()
        return self._demo_dict()

    async def demo_stop(self, request: web.Request) -> dict:
        self.b.demo.stop()
        self._changed()
        return self._demo_dict()

    async def ai(self, request: web.Request) -> dict:
        body = await self._body(request)
        q = str(body.get("q") or "").strip()
        if not q:
            raise ApiError("пустой вопрос")
        if not self.b.ai.configured:
            raise ApiError("ИИ не подключён: добавь AI_API_KEY в Render (бесплатный ключ Groq)")
        uid = request[USER]["id"]
        await self.b._refresh_balances()
        try:
            answer = await self.b.ai.ask(uid, q, self.b.ai_context())
        except AIError as e:
            raise ApiError(str(e)) from None
        proposals = [{"key": p.key, "value": p.value, "reason": p.reason, "desc": EDITABLE[p.key][1],
                      "old": float(getattr(self.b.settings, EDITABLE[p.key][0]))} for p in answer.proposals]
        plans, errors = [], []
        if answer.orders:
            if not request[OWNER]:
                errors.append("Ордера через ИИ может подтверждать только владелец бота.")
            for it in answer.orders if request[OWNER] else []:
                try:
                    name = self.b.trader.resolve_exchange(it.exchange)
                    if it.price:
                        plan = await self.b.trader.plan_limit(name, it.side, it.coin, it.amount, it.price, source="ai")
                    else:
                        plan = await self.b.trader.plan_market(name, it.side, it.coin, it.amount, source="ai")
                    d = plan_dict(plan, self.b.trader)
                    d["reason"] = it.reason
                    plans.append(d)
                except TradeError as e:
                    errors.append(f"{it.side} {it.coin}: {e}")
                except Exception as e:
                    errors.append(f"{it.side} {it.coin}: {human_error(e)}")
        return {"html": answer.html, "model": answer.model, "proposals": proposals, "plans": plans, "errors": errors}

    # --- Phantom, проверка, алерты, цена ---------------------------------------

    async def phantom(self, request: web.Request) -> dict:
        if not self.b.phantom.address:
            return {"address": ""}
        snap = await self.b.phantom_snapshot(force=request.query.get("force") == "1")
        return snap or {"address": self.b.phantom.address, "assets": [], "total_usd": 0}

    async def phantom_set(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        address = str(body.get("address") or "").strip()
        if len(address.split()) >= 10:
            raise ApiError("Это похоже на сид-фразу! Её нельзя давать никому. Нужен только публичный адрес")
        try:
            self.b.phantom.set_address(address)
        except WalletError as e:
            raise ApiError(str(e)) from None
        await self.b.save_extras()
        return await self.phantom(request)

    async def doctor(self, request: web.Request) -> dict:
        cached = self._doctor_cache
        if cached and time.time() - cached["at"] < 20:
            return cached
        self._doctor_cache = doctor_dict(await run_doctor(self.b))
        return self._doctor_cache

    def _alerts_list(self, chat_id: int) -> dict:
        items = self.b.alerts.for_chat(chat_id)
        return {"alerts": [{"id": a.id, "coin": a.coin, "op": a.op, "price": a.price, "exchange": a.exchange,
                            "name": self.b.scanner.display_name(a.exchange), "start_price": a.start_price,
                            "created_at": a.created_at} for a in items]}

    async def alerts(self, request: web.Request) -> dict:
        return self._alerts_list(request[USER]["id"])

    async def alert_add(self, request: web.Request) -> dict:
        body = await self._body(request)
        uid = request[USER]["id"]
        try:
            spec = parse_alert(str(body.get("text") or ""), list(self.b.scanner.exchanges))
            found = await extras_ui.first_exchange_with(self.b, spec.coin, spec.exchange)
            if not found:
                raise AlertError(f"не нашёл {spec.coin}/{self.b.settings.quote} на биржах из списка")
            self.b.alerts.add(build_alert(spec, found[1], found[0], uid))
        except AlertError as e:
            raise ApiError(str(e)) from None
        await self.b.save_extras()
        return self._alerts_list(uid)

    async def alert_delete(self, request: web.Request) -> dict:
        body = await self._body(request)
        uid = request[USER]["id"]
        self.b.alerts.remove(str(body.get("id") or ""), uid)
        await self.b.save_extras()
        return self._alerts_list(uid)

    async def price(self, request: web.Request) -> dict:
        coin = (request.query.get("coin") or "").strip().upper().split("/")[0]
        if not coin or len(coin) > 15:
            raise ApiError("укажи монету")
        rows = await extras_ui.prices_everywhere(self.b, coin)
        return {"coin": coin, "quote": self.b.settings.quote, "rows": [
            {"exchange": n, "name": self.b.scanner.display_name(n), "bid": t.get("bid"), "ask": t.get("ask"),
             "last": t.get("last"), "change": t.get("percentage")} for n, t in rows]}

    async def autostatus(self, request: web.Request) -> dict:
        self._owner(request)
        t = self.b.trader
        return {"auto": t.state.auto, "live": t.state.live, "status": t.auto_status_dict(),
                "text": html.unescape(re.sub(r"<[^>]+>", "", t.auto_status_text()))}

    async def dex_search(self, request: web.Request) -> dict:
        q = (request.query.get("q") or "").strip()
        if not q or len(q) > 64:
            raise ApiError("введи название или адрес токена")
        try:
            pairs = await self.b.dex.search(q)
        except DexError as e:
            raise ApiError(f"DexScreener: {e}") from None
        if is_address(q):
            pairs = [p for p in pairs if p.address.lower() == q.lower()] or pairs
        top = best_pairs(pairs, limit=1 if is_address(q) else 6)
        rug = await self.b.dex.rug_safe(top[0].address) if top and top[0].chain == "solana" else None
        return {"items": [p.as_dict(rug if i == 0 else None) for i, p in enumerate(top)]}

    async def dex_hot(self, request: web.Request) -> dict:
        try:
            pairs = await self.b.dex.trending("solana", limit=10)
        except DexError as e:
            raise ApiError(f"DexScreener: {e}") from None
        return {"items": [p.as_dict() for p in pairs], "radar": request[USER]["id"] in self.b.radar_chats}

    async def dex_rug(self, request: web.Request) -> dict:
        mint = (request.query.get("mint") or "").strip()
        if not SOL_ADDRESS_RE.match(mint):
            raise ApiError("нужен адрес токена Solana")
        try:
            rug = await self.b.dex.rugcheck(mint)
            pairs = best_pairs(await self.b.dex.tokens("solana", [mint]), limit=1)
        except DexError as e:
            raise ApiError(f"RugCheck: {e}") from None
        if rug is None and not pairs:
            raise ApiError("токен не найден")
        return {"rug": rug, "item": pairs[0].as_dict(rug) if pairs else None}

    async def radar(self, request: web.Request) -> dict:
        body = await self._body(request)
        uid = request[USER]["id"]
        if body.get("on"):
            self.b.radar_chats.add(uid)
        else:
            self.b.radar_chats.discard(uid)
        await self.b.save_extras()
        return {"radar": uid in self.b.radar_chats}

    async def opps(self, request: web.Request) -> dict:
        from .opps_ui import snapshot

        snap = await snapshot(self.b, force=request.query.get("force") == "1")
        return {**snap, "notify": self.b.opps_notify, "ai": self.b.ai.configured, "plan_q": PLAN_QUESTION}

    async def copy(self, request: web.Request) -> dict:
        """🐋 Копитрейдинг: кошельки, копии без денег, уроки."""
        d = self.b.copy.status_dict()
        d["searching"] = bool(self._copy_search and not self._copy_search.done())
        return d

    async def copy_set(self, request: web.Request) -> dict:
        """{on} / {signals} / {add: адрес, name} / {remove: адрес} / {find: true} — только владелец."""
        self._owner(request)
        body = await self._body(request)
        ct = self.b.copy
        notify = None
        if "add" in body:
            try:
                await ct.add_wallet(str(body["add"]), str(body.get("name") or "")[:30])
            except WalletError as e:
                raise ApiError(str(e)) from None
            notify = f"🐋 Кошелёк добавлен из приложения: {html.escape(ct.st['wallets'][str(body['add']).strip()]['name'])}"
        if "remove" in body:
            ct.remove_wallet(str(body["remove"]))
        if "signals" in body:
            ct.st["signals"] = bool(body["signals"])
        if "on" in body:
            ct.st["on"] = bool(body["on"])
            notify = "🐋 Копитрейдинг " + ("включён" if ct.on else "выключен") + " из приложения. Подробно: /copy"
            if ct.on and not ct.st["wallets"]:
                body["find"] = True
        if body.get("find") and not (self._copy_search and not self._copy_search.done()):
            async def search():
                try:
                    report = await ct.discover(force=True)
                    self.b.save_copy()
                    if report and self.b.owner_id:
                        await self.b._send(self.b.owner_id, "🔎 <b>Поиск кошельков</b>\n" + "\n".join(html.escape(r) for r in report))
                except Exception as e:
                    log.info("Поиск кошельков из приложения: %s", e)
                    ct.last_error = f"поиск кошельков: {e}"
            self._copy_search = asyncio.get_running_loop().create_task(search())
        self.b.save_copy()

        async def later():
            try:
                await self.b._backup_now()
                if notify and self.b.owner_id:
                    await self.b._send(self.b.owner_id, notify)
            except Exception:
                log.exception("Фоновое сохранение копитрейдинга")

        task = asyncio.get_running_loop().create_task(later())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await self.copy(request)

    async def opps_notify(self, request: web.Request) -> dict:
        self._owner(request)
        body = await self._body(request)
        self.b.opps_notify = bool(body.get("on"))
        await self.b.save_extras()
        return {"notify": self.b.opps_notify}

    async def tinvest(self, request: web.Request) -> dict:
        self._owner(request)
        ti = self.b.tinvest
        if not ti.configured:
            return {"configured": False}
        snap = await ti.get(force=request.query.get("force") == "1")
        return {"configured": True, **(snap or {})}

    async def set_setting(self, request: web.Request) -> dict:
        body = await self._body(request)
        key = str(body.get("key"))
        if key not in EDITABLE:
            raise ApiError("неизвестная настройка")
        if key in RISK_KEYS:
            self._owner(request)
        try:
            value = float(body.get("value"))
        except (TypeError, ValueError):
            raise ApiError("значение должно быть числом") from None
        err = self.b.apply_setting(key, value)
        if err:
            raise ApiError(f"{EDITABLE[key][1]}: {err}")
        self._changed()
        return {"settings": self._settings_dict()}
