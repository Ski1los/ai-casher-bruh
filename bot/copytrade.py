"""🐋 Копитрейдинг: бот ищет «умные» кошельки Solana, копирует их сделки на бумаге и учится.

Как это работает
1. Поиск кошельков. Бот берёт токены, которые сильно выросли (тренды DexScreener), и смотрит, кто купил их
   рано (первые сделки в пуле). Ещё кошелёк можно добавить вручную: адрес из GMGN, Axiom или DexScreener.
2. Анализ кошелька по его истории: прибыль по закрытым сделкам, доля удачных, сколько держит, как часто
   торгует. Боты-снайперы (сотни сделок в день, держат секунды) копировать бесполезно — их видно сразу.
3. Бумажное копирование. Каждая покупка отслеживаемого кошелька повторяется без денег: по цене DexScreener
   в момент, когда бот её увидел (с реальной задержкой), с комиссией Phantom и проскальзыванием. Выход —
   когда кошелёк продаёт, по стоп-лоссу −40% или через 48 часов.
4. Эволюция. Кошелёк, копии которого стабильно в плюсе, получает ⭐ — его покупки приходят тебе сигналом.
   Кошелёк, копии которого в минусе, бот выгоняет и ищет новый.
5. Учёба на ошибках. После каждой убыточной копии бот записывает причину (поздний вход, мало ликвидности,
   кошелёк продал через минуту…). Фильтр рисков сам сдвигает пороги, если сделки за порогом теряют.

Чего бот НЕ делает: не покупает с твоего кошелька. Для этого нужен приватный ключ, а его боту давать нельзя.
Сигнал приходит с кнопкой «Купить в Phantom» (Jupiter): одно касание и подтверждение в Phantom.
"""

from __future__ import annotations

import asyncio
import html
import logging
import statistics
import time
from urllib.parse import quote

import aiohttp

from .dex import DexError, best_pairs, fmt_usd
from .solana import DEFAULT_RPC, FALLBACK_RPCS, WalletError, _rpc, short, valid_address

log = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
STABLES = {USDC_MINT: "USDC", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT"}
LAMPORTS = 1_000_000_000

# Бумажная копия: столько же, сколько у тебя есть, и с теми же расходами, что при покупке в Phantom
COPY_USD = 5.0
PHANTOM_FEE = 0.0085  # комиссия обмена в Phantom
SLIPPAGE = 0.01  # мемкоины: цена уходит, пока транзакция подтверждается
NETWORK_USD = 0.01  # сеть Solana + приоритет, за одну транзакцию
STOP_PCT = 0.40
CALIB_MIN = 5          # после 5 замеров котировок Jupiter пересчитываем стоимость копий
CALIB_BUFFER = 0.003   # запас на сторону: цена чуть уходит, пока транзакция подтверждается
MAX_HOLD_SEC = 48 * 3600
STALE_SEC = 15 * 60  # покупку старше 15 минут копировать поздно
RUG_LIQ_DROP = 0.3  # ликвидность упала до 30% от входа — выходим (похоже на слив пула)

STEP_SEC = 90
MAX_ACTIVE = 15  # сколько кошельков отслеживать одновременно (бесплатный узел Solana не любит лишних запросов)
POLL_PER_STEP = 8  # сколько кошельков проверять за один шаг (по кругу)
NEW_TX_PER_WALLET = 8
DISCOVER_EVERY_SEC = 2 * 3600
EARLY_TX = 40  # сколько первых сделок пула разбирать при поиске ранних покупателей
HISTORY_TX = 40  # сколько последних транзакций кошелька разбирать при анализе

# Эволюция кошельков
WINDOW = 30  # судим по последним 30 копиям
STAR_MIN = 5  # ⭐ — не раньше 5 копий…
STAR_SHRINK = 5  # …средний результат «сжимаем» к нулю, как будто есть ещё 5 копий по 0%: везение на 2 сделках не считается
STAR_MIN_AVG = 2.0  # …и сжатое среднее не меньше +2% на копию
DROP_SUM = -40.0  # сумма копий −40% и хуже (от 5 копий) — выгоняем
DROP_STREAK = 4  # или 4 убытка подряд
SLEEP_DAYS = 7  # кошелёк неделю не торгует — освобождаем место
SIGNAL_DEDUPE_SEC = 6 * 3600
MAX_SIGNALS_PER_HOUR = 6

# Фильтр рисков: стартовые пороги и сетки, по которым он учится
FILTER_DEFAULT = {"min_liq": 20_000.0, "max_rug": 50.0, "min_age_h": 0.5, "max_chase": 0.3, "max_h1": 300.0}
FILTER_GRID = {
    "min_liq": [5_000.0, 10_000.0, 20_000.0, 30_000.0, 50_000.0, 100_000.0],
    "max_rug": [20.0, 30.0, 40.0, 50.0, 60.0, 80.0],
    "min_age_h": [0.0, 0.5, 1.0, 3.0, 6.0, 24.0],
    "max_chase": [0.1, 0.2, 0.3, 0.5, 1.0],
    "max_h1": [50.0, 100.0, 200.0, 300.0, 1000.0],
}
FILTER_NAMES = {"min_liq": "ликвидность не меньше", "max_rug": "риск RugCheck не больше",
                "min_age_h": "возраст токена не меньше", "max_chase": "переплата к цене кошелька не больше",
                "max_h1": "рост за час не больше"}
LEARN_MIN = 20  # учиться не раньше 20 закрытых копий
LEARN_SUBSET = 5  # оценка порога — минимум на 5 сделках
LEARN_GAIN = 5.0  # порог двигаем, только если сумма копий улучшается хотя бы на 5%

MISTAKES = {
    "late": "поздний вход (цена уже выше, чем у кошелька)",
    "fast": "кошелёк продал за минуты — не успеть",
    "liq": "мало ликвидности",
    "rug": "высокий риск RugCheck",
    "young": "токену меньше часа",
    "pump": "вход после сильного роста",
    "rugpull": "пул слили (ликвидность ушла)",
    "dump": "токен просто упал — обычный риск мемкоинов",
}

PROGRAM_LIKE = {"11111111111111111111111111111111", "ComputeBudget111111111111111111111111111111",
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"}


# ---------------------------------------------------------------------------
# Разбор транзакций Solana (чистые функции: легко проверить на примерах)
# ---------------------------------------------------------------------------


def _key(k) -> str:
    return k if isinstance(k, str) else (k or {}).get("pubkey", "")


def _ui(b: dict) -> float:
    a = b.get("uiTokenAmount") or {}
    try:
        return float(a.get("uiAmountString") or a.get("uiAmount") or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_swap(tx: dict | None, wallet: str) -> dict | None:
    """Обмен в транзакции с точки зрения кошелька: {sig, ts, side buy|sell, mint, amount, sol, usd} или None.

    SOL считаем вместе с WSOL и без «аренды» за открытие/закрытие счёта токена (иначе на маленьких сделках
    цена получится на 5–10% неверной). Если в обмене участвует USDC/USDT — сумма в долларах в usd."""
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return None
    meta, msg = tx["meta"], (tx.get("transaction") or {}).get("message") or {}
    keys = [_key(k) for k in msg.get("accountKeys") or []]
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    pre_b, post_b = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pre_b) or i >= len(post_b):
        return None
    sol = (post_b[i] - pre_b[i]) / LAMPORTS
    if i == 0:
        sol += (meta.get("fee") or 0) / LAMPORTS  # комиссия сети — не часть цены
    tokens: dict[str, float] = {}
    rent_accounts: set[int] = set()
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            tokens[b.get("mint", "")] = tokens.get(b.get("mint", ""), 0.0) + sign * _ui(b)
            rent_accounts.add(int(b.get("accountIndex", -1)))
    for j in rent_accounts:  # открыли счёт токена — аренда не трата; закрыли — не доход
        if 0 <= j < len(pre_b) and 0 <= j < len(post_b) and j != i:
            sol += (post_b[j] - pre_b[j]) / LAMPORTS
    sol += tokens.pop(WSOL, 0.0)
    usd = sum(tokens.pop(m, 0.0) for m in list(tokens) if m in STABLES)
    moved = {m: d for m, d in tokens.items() if abs(d) > 1e-12}
    if not moved:
        return None
    mint, delta = max(moved.items(), key=lambda x: abs(x[1]))
    if delta > 0 and (sol < -1e-6 or usd < -1e-6):
        side = "buy"
    elif delta < 0 and (sol > 1e-6 or usd > 1e-6):
        side = "sell"
    else:
        return None  # перевод, а не обмен
    sigs = (tx.get("transaction") or {}).get("signatures") or [""]
    return {"sig": sigs[0], "ts": float(tx.get("blockTime") or 0), "side": side, "mint": mint,
            "amount": abs(delta), "sol": abs(sol), "usd": abs(usd)}


def analyze_history(swaps: list[dict], now: float | None = None) -> dict:
    """Портрет кошелька по его недавним обменам (сделки — в SOL, без цен в долларах)."""
    now = time.time() if now is None else now
    swaps = sorted(swaps, key=lambda s: s["ts"])
    out = {"swaps": len(swaps), "closed": 0, "wins": 0, "pnl_sol": 0.0, "win_rate": None, "median_ret": None,
           "hold_min": None, "per_day": 0.0, "last_ts": swaps[-1]["ts"] if swaps else None, "grade": "unknown"}
    if not swaps:
        out["grade_text"] = grade_text("unknown")
        return out
    span_days = max((now - swaps[0]["ts"]) / 86400, 1 / 24)
    out["per_day"] = round(len(swaps) / span_days, 1)
    by_mint: dict[str, dict] = {}
    for s in swaps:
        m = by_mint.setdefault(s["mint"], {"buy_sol": 0.0, "buy_amt": 0.0, "sell_sol": 0.0, "sell_amt": 0.0,
                                           "first": None, "last_sell": None})
        if s["side"] == "buy":
            m["buy_sol"] += s["sol"]
            m["buy_amt"] += s["amount"]
            m["first"] = m["first"] or s["ts"]
        elif m["buy_amt"] > 0:  # продажа без покупки в окне: не знаем цену входа — пропускаем
            m["sell_sol"] += s["sol"]
            m["sell_amt"] += s["amount"]
            m["last_sell"] = s["ts"]
    rets, holds = [], []
    for m in by_mint.values():
        if not m["sell_amt"] or not m["buy_sol"]:
            continue
        frac = min(1.0, m["sell_amt"] / m["buy_amt"])
        cost = m["buy_sol"] * frac
        pnl = m["sell_sol"] - cost
        out["pnl_sol"] += pnl
        rets.append(pnl / cost * 100)
        holds.append((m["last_sell"] - m["first"]) / 60)
    out["closed"] = len(rets)
    out["wins"] = sum(r > 0 for r in rets)
    out["pnl_sol"] = round(out["pnl_sol"], 4)
    if rets:
        out["win_rate"] = round(out["wins"] / len(rets), 2)
        out["median_ret"] = round(statistics.median(rets), 1)
        out["hold_min"] = round(statistics.median(holds), 1)
    gaps = [b["ts"] - a["ts"] for a, b in zip(swaps, swaps[1:])]
    if (len(swaps) >= 20 and gaps and statistics.median(gaps) < 15) or out["per_day"] > 150:
        out["grade"] = "bot"
    elif out["hold_min"] is not None and out["hold_min"] < 3 and len(rets) >= 3:
        out["grade"] = "fast"
    elif len(rets) >= 3 and out["pnl_sol"] > 0 and (out["win_rate"] or 0) >= 0.4:
        out["grade"] = "good"
    elif len(rets) >= 3:
        out["grade"] = "weak"
    out["grade_text"] = grade_text(out["grade"])
    return out


def grade_text(grade: str) -> str:
    return {"bot": "🤖 бот-снайпер: сотни сделок, копировать бесполезно",
            "fast": "⚡ держит минуты: пока копия дойдёт, он уже продал",
            "good": "💎 в плюсе по своей истории",
            "weak": "📉 по своей истории в минусе",
            "unknown": "❔ мало данных"}.get(grade, grade)


def risk_check(pair, rug: dict | None, chase: float | None, filt: dict, now: float | None = None
               ) -> tuple[bool, list[str], dict]:
    """Проверка токена перед копией. (пропустить?, причины отказа, признаки для обучения)."""
    feats = {"liq": float(pair.liquidity_usd or 0), "age_h": pair.age_hours(now),
             "rug": (rug or {}).get("score"), "chase": chase, "h1": pair.change.get("h1"),
             "fdv_liq": (pair.fdv / pair.liquidity_usd) if pair.fdv and pair.liquidity_usd else None}
    reasons = []
    danger = [r["name"] for r in (rug or {}).get("risks", []) if r.get("level") == "danger"]
    if danger:
        reasons.append("RugCheck: " + ", ".join(danger[:2]))
    if pair.buys_h24 >= 30 and pair.sells_h24 == 0:
        reasons.append("продаж нет — возможно, продать нельзя")
    reasons += filter_fails(feats, filt)
    return not reasons, reasons, feats


def filter_fails(f: dict, filt: dict) -> list[str]:
    out = []
    if f["liq"] < filt["min_liq"]:
        out.append(f"ликвидность ${fmt_usd(f['liq'])} < ${fmt_usd(filt['min_liq'])}")
    if f["rug"] is None or f["rug"] > filt["max_rug"]:
        out.append("нет проверки RugCheck" if f["rug"] is None else f"RugCheck {f['rug']} > {filt['max_rug']:g}")
    if f["age_h"] is not None and f["age_h"] < filt["min_age_h"]:
        out.append(f"токену {f['age_h']:.1f} ч < {filt['min_age_h']:g} ч")
    if f["chase"] is not None and f["chase"] > filt["max_chase"]:
        out.append(f"цена уже на {f['chase']:+.0%} выше, чем у кошелька")
    if f["h1"] is not None and f["h1"] > filt["max_h1"]:
        out.append(f"+{f['h1']:.0f}% за час — вход на пике")
    return out


def _passes(f: dict, filt: dict) -> bool:
    return not filter_fails(f, filt)


def learn_filter(closed: list[dict], filt: dict) -> tuple[dict, str | None]:
    """Учимся на закрытых копиях (в том числе тех, что фильтр не пропустил: их тоже ведём на бумаге).
    Для каждого порога смотрим, какое значение из сетки дало бы больше суммарной прибыли, и сдвигаем
    один порог на один шаг. Медленно — чтобы не метаться от случайности. Возвращает (фильтр, урок)."""
    trades = [t for t in closed if t.get("feats") and not t.get("hard_fail")]
    if len(trades) < LEARN_MIN:
        return filt, None
    best = None
    for name, grid in FILTER_GRID.items():
        def total(v):
            test = dict(filt, **{name: v})
            sub = [t["ret"] for t in trades if _passes(t["feats"], test)]
            return (sum(sub), len(sub))
        cur, cur_n = total(filt[name])
        for v in grid:
            s, n = total(v)
            if n >= LEARN_SUBSET and s - cur >= LEARN_GAIN and (best is None or s - cur > best[0]):
                best = (s - cur, name, v, s, cur, n, cur_n)
    if not best:
        return filt, None
    _, name, target, s, cur, n, cur_n = best
    grid = FILTER_GRID[name]
    old = filt[name]
    pos = min(range(len(grid)), key=lambda k: abs(grid[k] - old))
    step = grid[max(0, min(len(grid) - 1, pos + (1 if target > old else -1)))]  # один шаг в сторону лучшего
    new = dict(filt, **{name: step})
    fmt = {"min_liq": lambda v: f"${fmt_usd(v)}", "max_rug": lambda v: f"{v:g}", "min_age_h": lambda v: f"{v:g} ч",
           "max_chase": lambda v: f"{v:.0%}", "max_h1": lambda v: f"{v:g}%"}[name]
    lesson = (f"🧠 Урок: «{FILTER_NAMES[name]}» {fmt(old)} → {fmt(step)}. На истории копий так было бы "
              f"{s:+.1f}% вместо {cur:+.1f}% ({n} сделок вместо {cur_n}).")
    return new, lesson


# --- обучение выходов: стоп и тейк-профит по пути цены каждой копии -----------------------

EXIT_STOPS = (0.2, 0.3, 0.4)        # стоп свободнее 40% проверить нельзя: копия сама закрывается на −40%
EXIT_TPS = (None, 0.3, 0.5, 1.0)    # тейк-профит: продать всё при +30% / +50% / +100%
EXIT_MIN = 15                       # судим не меньше чем по 15 копиям с записанным путём
EXIT_GAIN = 5.0                     # меняем правило, только если сумма лучше хотя бы на 5%
PATH_MAX = 60


def add_path(path: list, pct: float) -> list:
    """Путь цены копии в % от входа. Длиннее PATH_MAX — прореживаем через одну (первая и последняя остаются)."""
    path.append(round(pct, 1))
    if len(path) > PATH_MAX:
        path[:] = path[:-1:2] + path[-1:]
    return path


def exit_net(p: float, cost: float) -> float:
    """Чистый результат копии в %, если продать при движении цены на p% (с комиссиями туда и обратно)."""
    return ((1 + p / 100) * (1 - cost) / (1 + cost) - 1 - 2 * NETWORK_USD / COPY_USD) * 100


def simulate_exit(t: dict, stop: float, tp: float | None, cost: float) -> float:
    for p in t["path"]:
        if p <= -stop * 100:
            return exit_net(p, cost)
        if tp and p >= tp * 100:
            return exit_net(p, cost)
    return t["ret"]


def tp_text(tp: float | None) -> str:
    return f"+{tp:.0%}" if tp else "нет"


def learn_exits(closed: list[dict], stop: float, tp: float | None, cost: float
                ) -> tuple[float, float | None, str | None]:
    """Какой стоп и тейк дали бы больше на настоящих путях цены копий. Сдвиг — на один шаг одного правила."""
    trades = [t for t in closed if t.get("passed") and len(t.get("path") or []) >= 2]
    if len(trades) < EXIT_MIN:
        return stop, tp, None

    def total(s, t):
        return sum(simulate_exit(x, s, t, cost) for x in trades)

    stop = min(EXIT_STOPS, key=lambda s: abs(s - stop))
    tp = tp if tp in EXIT_TPS else None
    cur = total(stop, tp)
    si, ti = EXIT_STOPS.index(stop), EXIT_TPS.index(tp)
    options = [(EXIT_STOPS[i], tp) for i in (si - 1, si + 1) if 0 <= i < len(EXIT_STOPS)]
    options += [(stop, EXIT_TPS[i]) for i in (ti - 1, ti + 1) if 0 <= i < len(EXIT_TPS)]
    best = max(options, key=lambda o: total(*o))
    gain = total(*best) - cur
    if gain < EXIT_GAIN:
        return stop, tp, None
    s, t = best
    what = (f"стоп −{stop:.0%} → −{s:.0%}" if s != stop else f"тейк-профит {tp_text(tp)} → {tp_text(t)}")
    lesson = (f"🧠 Урок по выходам: {what}. На {len(trades)} копиях с настоящим путём цены так было бы "
              f"{cur + gain:+.1f}% вместо {cur:+.1f}%. Применяю к демо и реальным сделкам.")
    return s, t, lesson


def diagnose(pos: dict, ret: float, why: str) -> list[str]:
    """Почему копия ушла в минус — коды из MISTAKES."""
    if ret >= 0:
        return []
    f = pos.get("feats") or {}
    out = []
    if (f.get("chase") or 0) > 0.15:
        out.append("late")
    if why == "кошелёк продал" and pos.get("closed_at", 0) - pos["opened"] < 5 * 60:
        out.append("fast")
    if f.get("liq", 1e9) < 20_000:
        out.append("liq")
    if (f.get("rug") or 0) >= 40:
        out.append("rug")
    if f.get("age_h") is not None and f["age_h"] < 1:
        out.append("young")
    if (f.get("h1") or 0) > 100:
        out.append("pump")
    if why == "ликвидность ушла":
        out.append("rugpull")
    return out or ["dump"]


def shrunk_avg(w: dict) -> float:
    rets = w.get("rets") or []
    return sum(rets) / (len(rets) + STAR_SHRINK) if rets else 0.0


def buy_links(mint: str) -> dict[str, str]:
    jup = f"https://jup.ag/swap/SOL-{mint}"
    ref = quote("https://jup.ag", safe="")
    return {"phantom": f"https://phantom.app/ul/browse/{quote(jup, safe='')}?ref={ref}",
            "phantom_sell": f"https://phantom.app/ul/browse/{quote(f'https://jup.ag/swap/{mint}-SOL', safe='')}?ref={ref}",
            "jupiter": jup, "dexscreener": f"https://dexscreener.com/solana/{mint}",
            "gmgn": f"https://gmgn.ai/sol/token/{mint}"}


def new_state() -> dict:
    return {"on": False, "signals": True, "wallets": {}, "open": {}, "closed": [], "filter": dict(FILTER_DEFAULT),
            "lessons": [], "mistakes": {}, "candidates": {}, "graveyard": [], "discovered_at": 0.0,
            "signal_log": {}, "stats": {"copies": 0, "skipped_stale": 0, "no_pair": 0}, "real": {},
            "mentors": {}, "calib": {"rts": [], "cost": None}}


# ---------------------------------------------------------------------------
# Узел Solana с запасными адресами
# ---------------------------------------------------------------------------


class SolanaRpc:
    def __init__(self, url: str | None = None) -> None:
        import os
        own = url or os.getenv("SOLANA_RPC_URL", "").strip()
        self.urls = ([own] if own else []) + [u for u in (DEFAULT_RPC, *FALLBACK_RPCS) if u != own]
        self._session: aiohttp.ClientSession | None = None
        self._sem = asyncio.Semaphore(3)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def call(self, method: str, params: list):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        last: BaseException | None = None
        async with self._sem:
            for url in list(self.urls):
                try:
                    res = await _rpc(self._session, url, method, params)
                except (WalletError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last = e
                    continue
                if url != self.urls[0]:
                    self.urls.remove(url)
                    self.urls.insert(0, url)
                return res
        raise WalletError(str(last) if last else "нет узлов Solana")


# ---------------------------------------------------------------------------
# Копитрейдер
# ---------------------------------------------------------------------------


class CopyTrader:
    def __init__(self, dex, rpc=None) -> None:
        self.dex = dex
        self.rpc = rpc or SolanaRpc()
        self.st = new_state()
        self.last_error: str | None = None
        self.last_step = 0.0
        self.changes = 0  # растёт при каждом изменении: пора сохранить в бэкап
        self._sol_usd: tuple[float, float] = (0.0, 0.0)
        self._busy = asyncio.Lock()
        self.opened_now: list[str] = []
        self.closed_now: list[tuple[str, dict, str]] = []

    # --- состояние ----------------------------------------------------------

    def export(self) -> dict:
        return self.st

    def restore(self, raw: dict | None) -> None:
        st = new_state()
        if isinstance(raw, dict):
            st.update({k: v for k, v in raw.items() if k in st})
            st["filter"] = {**FILTER_DEFAULT, **(st.get("filter") or {})}
        self.st = st

    @property
    def on(self) -> bool:
        return bool(self.st["on"])

    def active(self) -> list[str]:
        return [a for a, w in self.st["wallets"].items() if w["status"] != "dropped" or w["source"] == "user"]

    # --- калибровка комиссий по настоящим котировкам Jupiter -----------------------------

    def cost_side(self) -> float:
        """Потеря на одну сторону сделки (доля). Пока нет замеров — комиссия Phantom + проскальзывание."""
        c = (self.st.get("calib") or {}).get("cost")
        return c if c else SLIPPAGE + PHANTOM_FEE

    def add_cost_sample(self, roundtrip: float, now: float | None = None) -> str | None:
        """Замер «вход+выход» по котировкам Jupiter (доля). Пересчитывает стоимость копий; урок при заметной разнице."""
        now = time.time() if now is None else now
        calib = self.st.setdefault("calib", {"rts": [], "cost": None})
        calib["rts"] = (calib["rts"] + [round(max(0.0, roundtrip), 4)])[-30:]
        self.changes += 1
        if len(calib["rts"]) < CALIB_MIN:
            return None
        new = round(min(0.05, max(0.004, statistics.median(calib["rts"]) / 2 + CALIB_BUFFER)), 4)
        old = self.cost_side()
        if calib["cost"] is not None and abs(new - old) < 0.002:
            return None
        calib["cost"] = new
        return self._lesson(f"🧪 Калибровка: по настоящим котировкам Jupiter вход+выход стоит ≈ {2 * new:.1%} "
                            f"(я считал {2 * old:.1%}). Копии без денег теперь считаются с этой стоимостью — "
                            "оценки кошельков честнее.", now)

    def _lesson(self, text: str, now: float) -> str:
        self.st["lessons"].append({"at": now, "text": text})
        del self.st["lessons"][:-30]
        return text

    # --- кошельки -------------------------------------------------------------

    def _new_wallet(self, name: str, source: str, now: float) -> dict:
        return {"name": name, "source": source, "added": now, "status": "watch", "cursor": None, "checked": 0.0,
                "hist": None, "rets": [], "n": 0, "wins": 0, "sum": 0.0, "streak": 0, "chase": [],
                "last_trade": None, "reason": "", "tokens": []}

    async def add_wallet(self, address: str, name: str = "", source: str = "user", now: float | None = None) -> dict:
        address = address.strip()
        if not valid_address(address):
            raise WalletError("это не адрес Solana. Скопируй адрес кошелька в GMGN/Axiom/DexScreener (32–44 символа)")
        now = time.time() if now is None else now
        w = self.st["wallets"].get(address)
        if w is None:
            if len(self.active()) >= MAX_ACTIVE and source == "auto":
                raise WalletError("уже отслеживаю максимум кошельков")
            w = self._new_wallet(name or short(address), source, now)
            self.st["wallets"][address] = w
        else:
            if source == "user":
                w["source"] = "user"
                if w["status"] == "dropped":
                    w["status"], w["reason"] = "watch", ""
            if name:
                w["name"] = name
        if address in self.st["graveyard"] and source == "user":
            self.st["graveyard"].remove(address)
        try:
            w["hist"] = await self.analyze(address, now)
        except Exception as e:
            log.info("Анализ кошелька %s: %s", address, e)
            self.last_error = f"анализ кошелька: {e}"
        self.changes += 1
        return w

    def remove_wallet(self, address: str) -> bool:
        w = self.st["wallets"].pop(address, None)
        for k in [k for k, p in self.st["open"].items() if p["wallet"] == address]:
            del self.st["open"][k]
        self.changes += 1
        return w is not None

    async def _signatures(self, address: str, limit: int, until: str | None = None, before: str | None = None):
        opts: dict = {"limit": limit}
        if until:
            opts["until"] = until
        if before:
            opts["before"] = before
        return await self.rpc.call("getSignaturesForAddress", [address, opts]) or []

    async def _tx(self, sig: str) -> dict | None:
        return await self.rpc.call("getTransaction", [sig, {"encoding": "jsonParsed",
                                                            "maxSupportedTransactionVersion": 0}])

    async def analyze(self, address: str, now: float | None = None) -> dict:
        sigs = [s for s in await self._signatures(address, 60) if s.get("err") is None][:HISTORY_TX]
        txs = await asyncio.gather(*(self._tx(s["signature"]) for s in sigs), return_exceptions=True)
        swaps = [sw for tx in txs if not isinstance(tx, BaseException) for sw in [parse_swap(tx, address)] if sw]
        return analyze_history(swaps, now)

    # --- цены ---------------------------------------------------------------

    async def sol_usd(self) -> float:
        at, price = self._sol_usd
        if price and time.time() - at < 120:
            return price
        prices = await self.dex.token_prices("solana", [WSOL])
        price = (prices.get(WSOL) or (None, 0.0))[1]
        if price:
            self._sol_usd = (time.time(), price)
        return price or self._sol_usd[1]

    async def _pairs(self, mints: list[str]) -> dict:
        if not mints:
            return {}
        return {p.address: p for p in best_pairs(await self.dex.tokens("solana", mints), limit=1000)}

    # --- шаг ----------------------------------------------------------------

    async def step(self, now: float | None = None) -> list[dict]:
        """Проверить кошельки, повторить их сделки на бумаге, обновить открытые копии.
        Возвращает события для владельца: [{"text", "buttons": [(подпись, ссылка)]}]."""
        if not self.on or self._busy.locked():
            return []
        async with self._busy:
            now = time.time() if now is None else now
            self.last_step = now
            self.opened_now, self.closed_now = [], []  # для реальной торговли (bot/memetrade.py)
            events: list[dict] = []
            try:
                sol = await self.sol_usd()
            except DexError as e:
                self.last_error = f"DexScreener: {e}"
                return []
            order = sorted(self.active(), key=lambda a: self.st["wallets"][a]["checked"])[:POLL_PER_STEP]
            for addr in order:
                try:
                    await self._poll(addr, now, sol, events)
                except Exception as e:
                    self.last_error = f"кошелёк {short(addr)}: {e}"
                    log.info("Копитрейдинг %s: %s", addr, e)
            try:
                await self._mark(now, events)
            except DexError as e:
                self.last_error = f"DexScreener: {e}"
            self._sleepers(now, events)
            return events

    async def _poll(self, addr: str, now: float, sol_usd: float, events: list[dict]) -> None:
        w = self.st["wallets"][addr]
        sigs = await self._signatures(addr, 15, until=w["cursor"])
        w["checked"] = now
        first_time = not w.get("primed")
        if first_time:
            w["primed"] = True
            self.changes += 1
        if not sigs:
            return
        w["cursor"] = sigs[0]["signature"]
        self.changes += 1
        if first_time:  # старые сделки не копируем: начинаем с этого момента
            return
        for s in [s for s in sigs if s.get("err") is None][:NEW_TX_PER_WALLET][::-1]:
            sw = parse_swap(await self._tx(s["signature"]), addr)
            if sw:
                w["last_trade"] = sw["ts"] or now
                await self._on_swap(addr, w, sw, now, sol_usd, events)

    async def _on_swap(self, addr: str, w: dict, sw: dict, now: float, sol_usd: float, events: list[dict]) -> None:
        key = f"{addr}|{sw['mint']}"
        if sw["side"] == "sell":
            pos = self.st["open"].get(key)
            if pos and sw["amount"] >= 0.3 * pos["w_amount"]:
                pairs = await self._pairs([sw["mint"]])
                p = pairs.get(sw["mint"])
                price = p.price_usd if p and p.price_usd else pos["last"]
                self._close(key, price, "кошелёк продал", now, events)
            return
        if key in self.st["open"]:
            self.st["open"][key]["w_amount"] += sw["amount"]  # докупил
            return
        if sw["ts"] and now - sw["ts"] > STALE_SEC:
            self.st["stats"]["skipped_stale"] += 1
            return
        pairs = await self._pairs([sw["mint"]])
        p = pairs.get(sw["mint"])
        if not p or not p.price_usd:
            self.st["stats"]["no_pair"] += 1  # совсем новый токен: DexScreener его ещё не видит
            return
        rug = await self.dex.rug_safe(sw["mint"])
        w_price = (sw["usd"] / sw["amount"]) if sw["usd"] else (sw["sol"] * sol_usd / sw["amount"] if sol_usd else None)
        chase = (p.price_usd / w_price - 1) if w_price else None
        passed, reasons, feats = risk_check(p, rug, chase, self.st["filter"], now)
        hard = any(r.startswith(("RugCheck:", "продаж нет")) for r in reasons)
        self.st["open"][key] = {"wallet": addr, "mint": sw["mint"], "symbol": p.symbol[:16], "opened": now,
                                "raw": p.price_usd, "entry": p.price_usd * (1 + self.cost_side()),
                                "cost": self.cost_side(), "path": [0.0], "wstatus": w["status"],
                                "wgrade": (w.get("hist") or {}).get("grade"),
                                "last": p.price_usd, "peak": p.price_usd, "liq0": p.liquidity_usd or 0,
                                "w_amount": sw["amount"], "w_usd": round(sw["usd"] or sw["sol"] * sol_usd, 2),
                                "feats": feats, "passed": passed, "reasons": reasons[:3], "hard_fail": hard}
        self.st["stats"]["copies"] += 1
        self.opened_now.append(key)
        w["tokens"] = ([p.symbol[:16]] + [t for t in w["tokens"] if t != p.symbol[:16]])[:8]
        if chase is not None:
            w["chase"] = (w["chase"] + [round(chase, 3)])[-WINDOW:]
        self.changes += 1
        if w["status"] == "star" and passed and self._may_signal(sw["mint"], now):
            events.append(self._signal(addr, w, self.st["open"][key], p))

    def _may_signal(self, mint: str, now: float) -> bool:
        if not self.st["signals"]:
            return False
        log_ = {m: t for m, t in self.st["signal_log"].items() if now - t < SIGNAL_DEDUPE_SEC}
        self.st["signal_log"] = log_
        if mint in log_ or sum(now - t < 3600 for t in log_.values()) >= MAX_SIGNALS_PER_HOUR:
            return False
        log_[mint] = now
        return True

    def _signal(self, addr: str, w: dict, pos: dict, p) -> dict:
        f = pos["feats"]
        chase = f"{f['chase']:+.0%}" if f.get("chase") is not None else "?"
        text = (f"🐋 <b>Сигнал копирования: {html.escape(pos['symbol'])}</b>\n"
                f"⭐ Кошелёк <b>{html.escape(w['name'])}</b> купил на ${pos['w_usd']:g}\n"
                f"Его копии без денег: {len(w['rets'])} сделок, {sum(r > 0 for r in w['rets'])} в плюс, "
                f"<b>{sum(w['rets']):+.1f}%</b>\n"
                f"Риск-фильтр пройден: ликвидность ${fmt_usd(f['liq'])}, RugCheck {f.get('rug')}, "
                f"возраст {f['age_h']:.1f} ч, цена к его покупке {chase}\n"
                f"Цена ${p.price_usd:.8g}. Если копируешь — не больше суммы, которую не жалко; "
                f"выход: когда кошелёк продаст (пришлю), или стоп −{STOP_PCT:.0%}.")
        links = buy_links(pos["mint"])
        return {"text": text, "buttons": [("👻 Купить в Phantom", links["phantom"]), ("📈 DexScreener", links["dexscreener"])],
                "mint": pos["mint"]}

    async def _mark(self, now: float, events: list[dict]) -> None:
        if not self.st["open"]:
            return
        pairs = await self._pairs(sorted({p["mint"] for p in self.st["open"].values()}))
        for key, pos in list(self.st["open"].items()):
            p = pairs.get(pos["mint"])
            if p and p.price_usd:
                pos["last"] = p.price_usd
                pos["peak"] = max(pos["peak"], p.price_usd)
                if pos.get("path") is not None and pos["raw"]:
                    add_path(pos["path"], (p.price_usd / pos["raw"] - 1) * 100)
            if pos["last"] <= pos["raw"] * (1 - STOP_PCT):
                self._close(key, pos["last"], "стоп-лосс", now, events)
            elif p and pos["liq0"] and (p.liquidity_usd or 0) < pos["liq0"] * RUG_LIQ_DROP:
                self._close(key, pos["last"], "ликвидность ушла", now, events)
            elif now - pos["opened"] > MAX_HOLD_SEC:
                self._close(key, pos["last"], "48 часов", now, events)

    def _close(self, key: str, price: float, why: str, now: float, events: list[dict]) -> None:
        pos = self.st["open"].pop(key)
        pos["closed_at"] = now
        self.closed_now.append((key, pos, why))
        exit_eff = price * (1 - pos.get("cost", SLIPPAGE + PHANTOM_FEE))
        ret = (exit_eff / pos["entry"] - 1 - 2 * NETWORK_USD / COPY_USD) * 100
        if why == "ликвидность ушла":
            ret = min(ret, -90.0)  # продать в слитый пул почти невозможно
        ret = round(max(ret, -100.0), 2)
        mistakes = diagnose(pos, ret, why)
        for m in mistakes:
            self.st["mistakes"][m] = self.st["mistakes"].get(m, 0) + 1
        rec = {"wallet": pos["wallet"], "mint": pos["mint"], "symbol": pos["symbol"], "ret": ret, "why": why,
               "opened": pos["opened"], "closed": now, "feats": pos["feats"], "passed": pos["passed"],
               "hard_fail": pos.get("hard_fail", False), "mistakes": mistakes,
               "wstatus": pos.get("wstatus"), "wgrade": pos.get("wgrade")}
        if pos["passed"] and pos.get("path"):
            rec["path"] = pos["path"] + ([round((price / pos["raw"] - 1) * 100, 1)] if pos["raw"] else [])
        self.st["closed"].append(rec)
        del self.st["closed"][:-200]
        self.changes += 1
        w = self.st["wallets"].get(pos["wallet"])
        if w is not None:
            if w["status"] == "star" and pos["passed"] and why == "кошелёк продал" and self.st["signals"]:
                events.append({"text": (f"🐋 ⭐ <b>{html.escape(w['name'])}</b> продал <b>{html.escape(pos['symbol'])}</b>. "
                                        f"Если копировал — продавай. Копия без денег: <b>{ret:+.1f}%</b>"),
                               "buttons": [("👻 Продать в Phantom", buy_links(pos["mint"])["phantom_sell"])]})
            self._evolve(pos["wallet"], w, ret, now, events)
        filt, lesson = learn_filter(self.st["closed"], self.st["filter"])
        if lesson:
            self.st["filter"] = filt
            events.append({"text": self._lesson(lesson, now), "buttons": []})

    def _evolve(self, addr: str, w: dict, ret: float, now: float, events: list[dict]) -> None:
        w["rets"] = (w["rets"] + [ret])[-WINDOW:]
        w["n"] += 1
        w["wins"] += ret > 0
        w["sum"] = round(sum(w["rets"]), 2)
        w["streak"] = 0 if ret > 0 else w["streak"] + 1
        n = len(w["rets"])
        avg = shrunk_avg(w)
        if w["status"] != "star" and n >= STAR_MIN and avg >= STAR_MIN_AVG:
            w["status"], w["reason"] = "star", f"копии вживую: {n} сделок, {w['sum']:+.1f}%"
            events.append({"text": self._lesson(
                f"⭐ Кошелёк {w['name']} повышен: его копии без денег дали {w['sum']:+.1f}% за {n} сделок. "
                "Теперь его покупки приходят сигналом.", now), "buttons": []})
        elif w["status"] == "star" and avg < STAR_MIN_AVG / 2:
            w["status"], w["reason"] = "watch", f"копии сдали: сжатое среднее {avg:+.1f}%"
            events.append({"text": self._lesson(f"⬇️ Кошелёк {w['name']} теряет ⭐: последние копии хуже "
                                                f"({w['sum']:+.1f}% за {n}). Сигналы по нему выключены.", now),
                           "buttons": []})
        if w["status"] != "dropped" and ((n >= STAR_MIN and w["sum"] <= DROP_SUM) or w["streak"] >= DROP_STREAK):
            chase = statistics.median(w["chase"]) if w["chase"] else None
            why = f"{w['streak']} убытка подряд" if w["streak"] >= DROP_STREAK else f"копии {w['sum']:+.1f}% за {n}"
            if chase is not None and chase > 0.15:
                why += f", копия в среднем на {chase:+.0%} дороже его цены (не успеваем)"
            w["status"], w["reason"] = "dropped", why
            if w["source"] == "auto":
                self.st["graveyard"] = (self.st["graveyard"] + [addr])[-300:]
            events.append({"text": self._lesson(f"🔴 Кошелёк {w['name']} выгнан: {why}. "
                                                + ("Ищу замену." if w["source"] == "auto"
                                                   else "Он твой — оставил в списке, но сигналов не будет."), now),
                           "buttons": []})

    def _sleepers(self, now: float, events: list[dict]) -> None:
        """Автонайденные кошельки, которые давно не торгуют или выгнаны, убираем: место для новых."""
        for addr, w in list(self.st["wallets"].items()):
            if w["source"] != "auto" or any(p["wallet"] == addr for p in self.st["open"].values()):
                continue
            idle = now - (w["last_trade"] or w["added"])
            if w["status"] == "dropped" and now - w["added"] > 86400:
                del self.st["wallets"][addr]
            elif idle > SLEEP_DAYS * 86400 and w["status"] != "star":
                del self.st["wallets"][addr]
                self.st["graveyard"] = (self.st["graveyard"] + [addr])[-300:]
                self._lesson(f"💤 Кошелёк {w['name']} неделю не торгует — освободил место.", now)

    # --- поиск новых кошельков --------------------------------------------------

    async def discover(self, now: float | None = None, force: bool = False) -> list[str]:
        """Ранние покупатели выросших токенов → кандидаты → анализ истории → в отслеживание.
        Возвращает строки-отчёт для человека."""
        now = time.time() if now is None else now
        if not force and now - self.st["discovered_at"] < DISCOVER_EVERY_SEC:
            return []
        self.st["discovered_at"] = now
        report: list[str] = []
        winners = []
        for p in await self.dex.trending("solana", limit=40):
            age = p.age_hours(now)
            grow = max(p.change.get("h24") or 0, p.change.get("h6") or 0)
            if (grow >= 50 and (p.liquidity_usd or 0) >= 30_000 and age is not None and 2 <= age <= 96
                    and p.sells_h24 > 0 and p.pair_address):
                winners.append(p)
        winners.sort(key=lambda p: -(p.change.get("h24") or 0))
        cands = self.st["candidates"]
        for p in winners[:2]:
            try:
                buyers = await self.early_buyers(p.pair_address, p.address)
            except Exception as e:
                report.append(f"{p.symbol}: не смог прочитать пул ({e})")
                continue
            for b in buyers:
                c = cands.setdefault(b, {"seen": 0, "tokens": [], "first": now})
                if p.symbol not in c["tokens"]:
                    c["seen"] += 1
                    c["tokens"] = (c["tokens"] + [p.symbol[:16]])[-5:]
            report.append(f"{p.symbol} (+{p.change.get('h24') or 0:.0f}% за сутки): ранних покупателей {len(buyers)}")
        if len(cands) > 300:  # память: держим самых частых
            for a in sorted(cands, key=lambda a: (cands[a]["seen"], cands[a]["first"]))[:len(cands) - 300]:
                del cands[a]
        added = 0
        for addr in sorted(cands, key=lambda a: -cands[a]["seen"]):
            if added >= 3 or len(self.active()) >= MAX_ACTIVE:
                break
            if addr in self.st["wallets"] or addr in self.st["graveyard"]:
                continue
            try:
                hist = await self.analyze(addr, now)
            except Exception as e:
                report.append(f"{short(addr)}: анализ не удался ({e})")
                continue
            c = cands.pop(addr)
            if hist["grade"] in ("bot", "fast", "weak"):
                self.st["graveyard"] = (self.st["graveyard"] + [addr])[-300:]
                report.append(f"{short(addr)}: {grade_text(hist['grade'])} — не беру")
                continue
            w = self._new_wallet(f"{short(addr)} ({', '.join(c['tokens'][:2])})", "auto", now)
            w["hist"] = hist
            self.st["wallets"][addr] = w
            added += 1
            report.append(f"➕ {w['name']}: {grade_text(hist['grade'])}, взял в отслеживание")
        self.changes += 1
        return report

    async def early_buyers(self, pool: str, mint: str) -> list[str]:
        """Кто купил токен в первых сделках пула (без самых первых блоков: там боты-снайперы)."""
        sigs, before = [], None
        for _ in range(3):
            page = await self._signatures(pool, 1000, before=before)
            sigs += page
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        sigs = [s for s in sigs if s.get("err") is None][::-1]  # от старых к новым
        if not sigs:
            return []
        first_slot = sigs[0].get("slot", 0)
        early = [s for s in sigs if s.get("slot", 0) > first_slot + 2][:EARLY_TX]
        txs = await asyncio.gather(*(self._tx(s["signature"]) for s in early), return_exceptions=True)
        out: list[str] = []
        for tx in txs:
            if isinstance(tx, BaseException) or not tx:
                continue
            keys = [_key(k) for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
            if not keys or keys[0] in PROGRAM_LIKE or keys[0] in (pool, mint):
                continue
            sw = parse_swap(tx, keys[0])
            if sw and sw["side"] == "buy" and sw["mint"] == mint and keys[0] not in out:
                out.append(keys[0])
        return out

    # --- для людей ----------------------------------------------------------

    def summary(self) -> dict:
        closed = self.st["closed"]
        taken = [t for t in closed if t["passed"]]
        return {"closed": len(closed), "taken": len(taken), "taken_wins": sum(t["ret"] > 0 for t in taken),
                "taken_sum": round(sum(t["ret"] for t in taken), 1),
                "taken_usd": round(sum(t["ret"] for t in taken) * COPY_USD / 100, 2),
                "all_sum": round(sum(t["ret"] for t in closed), 1),
                "skipped_sum": round(sum(t["ret"] for t in closed if not t["passed"]), 1)}

    def wallet_rows(self) -> list[dict]:
        order = {"star": 0, "watch": 1, "dropped": 2}
        rows = []
        for addr, w in self.st["wallets"].items():
            hist = w.get("hist") or {}
            rows.append({"address": addr, "short": short(addr), "name": w["name"], "status": w["status"],
                         "source": w["source"], "copies": len(w["rets"]), "wins": sum(r > 0 for r in w["rets"]),
                         "sum": w["sum"], "avg": round(shrunk_avg(w), 2), "reason": w.get("reason", ""),
                         "grade": hist.get("grade", "unknown"), "grade_text": hist.get("grade_text") or grade_text("unknown"),
                         "hist_pnl_sol": hist.get("pnl_sol"), "hist_win": hist.get("win_rate"),
                         "per_day": hist.get("per_day"), "hold_min": hist.get("hold_min"),
                         "tokens": w.get("tokens", [])[:4], "last_trade": w.get("last_trade"),
                         "links": {"gmgn": f"https://gmgn.ai/sol/address/{addr}",
                                   "solscan": f"https://solscan.io/account/{addr}"}})
        rows.sort(key=lambda r: (order.get(r["status"], 3), -r["avg"], -r["copies"]))
        return rows

    def status_dict(self) -> dict:
        st = self.st
        mistakes = sorted(st["mistakes"].items(), key=lambda x: -x[1])
        return {"on": self.on, "signals": bool(st["signals"]), "summary": self.summary(),
                "wallets": self.wallet_rows(),
                "open": [{"symbol": p["symbol"], "mint": p["mint"], "wallet": st["wallets"].get(p["wallet"], {}).get("name", short(p["wallet"])),
                          "ret": round((p["last"] * (1 - p.get("cost", SLIPPAGE + PHANTOM_FEE)) / p["entry"] - 1) * 100, 1),
                          "passed": p["passed"], "reasons": p["reasons"], "opened": p["opened"]}
                         for p in sorted(st["open"].values(), key=lambda p: -p["opened"])][:20],
                "recent": [{k: t[k] for k in ("symbol", "ret", "why", "passed", "closed", "mistakes")}
                           for t in st["closed"][-12:][::-1]],
                "lessons": st["lessons"][-10:][::-1],
                "mistakes": [{"code": k, "text": MISTAKES.get(k, k), "count": v} for k, v in mistakes[:6]],
                "filter": st["filter"], "filter_names": FILTER_NAMES, "error": self.last_error,
                "candidates": len(st["candidates"]), "stats": st["stats"], "copy_usd": COPY_USD}

    def text(self) -> str:
        st = self.st
        s = self.summary()
        lines = [f"🐋 <b>Копитрейдинг</b> (Solana): {'🟢 работает' if self.on else '⏸ выключен'}",
                 "Бот ищет кошельки, которые рано покупают растущие токены, копирует их сделки <b>без денег</b> "
                 "(с задержкой, комиссией Phantom и проскальзыванием) и сам решает, кому верить."]
        rows = self.wallet_rows()
        icon = {"star": "⭐", "watch": "👀", "dropped": "🔴"}
        if rows:
            lines.append("")
            lines.append(f"<b>Кошельки</b> ({len(self.active())} отслеживаю, кандидатов {len(st['candidates'])}):")
            for r in rows[:10]:
                res = (f"{r['copies']} копий, {r['wins']} в плюс, <b>{r['sum']:+.1f}%</b>" if r["copies"]
                       else "копий ещё нет")
                lines.append(f"{icon.get(r['status'], '•')} {html.escape(r['name'])}: {res} · {r['grade_text'].split(':')[0]}"
                             + (" · твой" if r["source"] == "user" else ""))
        else:
            lines.append("\nКошельков пока нет. Включи — бот найдёт сам, или добавь: <code>/copy add АДРЕС имя</code>")
        if s["closed"]:
            lines.append(f"\n<b>Итог копий без денег</b> (по ${COPY_USD:g}): прошли фильтр {s['taken']} — "
                         f"{s['taken_wins']} в плюс, <b>{s['taken_sum']:+.1f}%</b> (≈ {s['taken_usd']:+.2f}$). "
                         f"Отсеянные фильтром дали бы {s['skipped_sum']:+.1f}%.")
        if st["open"]:
            lines.append(f"Открыто копий: {len(st['open'])}")
        if st["mistakes"]:
            top = sorted(st["mistakes"].items(), key=lambda x: -x[1])[:3]
            lines.append("<b>Главные ошибки</b>: " + "; ".join(f"{MISTAKES.get(k, k)} — {v}" for k, v in top))
        if st["lessons"]:
            lines.append("<b>Последний урок</b>: " + html.escape(st["lessons"][-1]["text"]))
        if self.last_error:
            lines.append(f"⚠️ {html.escape(self.last_error[:200])}")
        lines.append("\n⚠️ Мемкоины — самый рискованный рынок: большинство падает почти до нуля. Бот покупать с твоего "
                     "кошелька не может (нужен приватный ключ — не давай его никому). Сигналы ⭐ — с кнопкой «Купить "
                     "в Phantom».")
        return "\n".join(lines)

    def wallet_text(self, addr: str) -> str:
        w = self.st["wallets"].get(addr)
        if not w:
            return "Такого кошелька нет в списке."
        h = w.get("hist") or {}
        lines = [f"🐋 <b>{html.escape(w['name'])}</b> <code>{addr}</code>",
                 f"Статус: {({'star': '⭐ копирую сигналами', 'watch': '👀 проверяю копиями без денег', 'dropped': '🔴 выгнан'})[w['status']]}"
                 + (f" ({html.escape(w['reason'])})" if w.get("reason") else ""),
                 f"По своей истории (последние {h.get('swaps', 0)} обменов): {h.get('grade_text', grade_text('unknown'))}"]
        if h.get("closed"):
            lines.append(f"  закрытых сделок {h['closed']}, в плюс {h['wins']} ({(h['win_rate'] or 0):.0%}), "
                         f"итог {h['pnl_sol']:+.3f} SOL, держит ~{h['hold_min']:g} мин, {h['per_day']:g} обменов в день")
        if w["rets"]:
            lines.append(f"Копии без денег: {len(w['rets'])}, в плюс {sum(r > 0 for r in w['rets'])}, "
                         f"<b>{w['sum']:+.1f}%</b>, подряд в минус {w['streak']}")
        if w["chase"]:
            lines.append(f"Задержка копии: в среднем покупаем на {statistics.median(w['chase']):+.0%} дороже его")
        return "\n".join(lines)
