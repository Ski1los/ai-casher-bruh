"""⚡ Пульс рынка (/pulse): бот сам смотрит GMGN и «Axiom Pulse», торгует кандидатами на бумаге и учится,
какие признаки токена на самом деле ведут к прибыли, а какие — к сливу.

Откуда кандидаты (раз в 10 минут):
  * GMGN (нужен бесплатный GMGN_API_KEY с gmgn.ai/ai): тренды за 5 минут; «окопы» — новые токены, почти
    завершившие bonding curve и только что вышедшие на DEX (это ровно три колонки Pulse в Axiom: New Pairs,
    Final Stretch, Migrated); сделки «умных» кошельков и KOL; свежие коллы.
  * DexScreener (всегда, без ключа): токены, которые сейчас продвигают (бусты, новые профили) + RugCheck.
  * 🎥 Стримы: токен мелькнул на экране стримера (инсайд-чат, трекер кошельков, колл) — тоже кандидат.

Что бот делает с кандидатом:
  1. Запоминает цену (DexScreener) и все признаки: ликвидность, капитализация, возраст, холдеры, топ-10,
     доля разработчика, снайперы, бандлы, инсайдеры, риск rug, сколько умных денег и KOL, откуда пришёл.
  2. Следит за ценой 24 часа (каждые 5 минут).
  3. 9 стратегий-фильтров (как пресеты в Axiom/GMGN: «безопасные», «умные деньги», «строгий», Final Stretch,
     Migrated, импульс, коллы KOL, «выученный») торгуют им на своих бумажных $10: стоп, тейк, комиссии и
     сеть Solana учтены. Это честная проверка: виден итог каждого фильтра, а не красивые скриншоты.
  4. Учится: по закрытым кандидатам считает, сколько в среднем приносит каждый признак («бандлы >30% —
     в среднем −40% на 25 токенах»). Признаки, которые доказанно теряют, бот сам добавляет в стоп-лист — на
     нём работает стратегия «выученный фильтр». Стоп и тейк подбирает по путям цены.
  5. Лучшая стратегия (только если её плюс не похож на везение) присылает сигналы. Настоящие деньги пульс
     не трогает: это обучение и сигналы, итог виден в 💰 Профит-центре.

Честно: мемкоины в большинстве падают. Пульс показывает, где это видно по цифрам, и не даёт верить скриншотам.
"""

from __future__ import annotations

import html
import logging
import time

from .gmgn import GmgnClient, GmgnError

log = logging.getLogger(__name__)

SCAN_SEC = 600             # раз в 10 минут ищем кандидатов
UPDATE_SEC = 300           # раз в 5 минут обновляем цены
HOLD_H = 24                # каждого кандидата ведём сутки
MAX_TRACK = 120            # одновременно ведём не больше 120 кандидатов (4 запроса DexScreener)
MAX_NEW = 12               # новых за один поиск
SEEN_SEC = 24 * 3600       # тот же токен снова кандидат не раньше чем через сутки
MIN_LIQ = 5_000            # меньше — продать почти нельзя, не берём даже на бумагу
DONE_KEEP = 300
START_USD = 10.0
SIZE_SHARE = 0.2
MIN_TRADE = 1.0
MAX_OPEN = 5
BEST_MIN = 10              # стратегию судим от 10 закрытых сделок
INSIGHT_MIN = 8            # признак показываем от 8 токенов
AVOID_MIN = 15             # в стоп-лист — от 15 токенов и только доказанный минус
RULE_MIN = 20              # стоп/тейк подбираем от 20 полных путей
RULE_GAIN = 1.0            # и только если в среднем лучше хотя бы на 1% за сделку (иначе это подгонка)
DEFAULT_RULE = {"stop": 0.35, "tp": None}
GRID_STOPS = (0.2, 0.3, 0.35, 0.5)
GRID_TPS = (None, 0.3, 0.5, 1.0)
MARKS = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600}

PRESETS = {
    "all": ("Все кандидаты", "открывает каждого — для сравнения"),
    "safe": ("Безопасные", "rug < 0.3, бандлы и инсайдеры < 30%, топ-10 < 50%"),
    "smart": ("Умные деньги", "держат или покупают 2+ умных кошелька GMGN"),
    "strict": ("Строгий", "безопасный + умные деньги + разработчик продал (или ≤5%) + ликвидность от $20k"),
    "stretch": ("Final Stretch", "почти завершили bonding curve (колонка Axiom Pulse)"),
    "migrated": ("Migrated", "только что вышли на DEX (колонка Axiom Pulse)"),
    "momentum": ("Импульс", "+2% за 5 мин, покупок в 1,2+ раза больше продаж, ликвидность от $15k"),
    "callout": ("Коллы и стримы", "токен назвали KOL, колл-каналы GMGN или он мелькнул у стримера"),
    "learned": ("Выученный фильтр", "все, кроме признаков из стоп-листа, который бот выучил сам"),
}
SRC_TEXT = {"trend": "тренд GMGN", "new": "новый (New Pairs)", "stretch": "Final Stretch", "migrated": "Migrated",
            "smart": "умные деньги", "kol": "KOL", "callout": "колл", "promo": "реклама DexScreener",
            "stream": "стример", "insider": "инсайд-чат на стриме", "tracker": "трекер кошельков на стриме"}
STREAM_SRC = {"stream", "insider", "tracker"}

# признак → (название, [(id корзины, подпись, проверка)])
BUCKETS = {
    "top10": ("Топ-10 холдеров", [("lo", "< 20%", lambda v: v < 0.2), ("mid", "20–50%", lambda v: 0.2 <= v <= 0.5),
                                  ("hi", "> 50%", lambda v: v > 0.5)]),
    "dev": ("Доля разработчика", [("lo", "≤ 5%", lambda v: v <= 0.05), ("mid", "5–15%", lambda v: 0.05 < v <= 0.15),
                                  ("hi", "> 15%", lambda v: v > 0.15)]),
    "bundler": ("Бандлы", [("lo", "< 10%", lambda v: v < 0.1), ("mid", "10–30%", lambda v: 0.1 <= v <= 0.3),
                           ("hi", "> 30%", lambda v: v > 0.3)]),
    "insider": ("Инсайдеры", [("lo", "< 10%", lambda v: v < 0.1), ("mid", "10–30%", lambda v: 0.1 <= v <= 0.3),
                              ("hi", "> 30%", lambda v: v > 0.3)]),
    "snipers": ("Снайперы", [("lo", "< 10%", lambda v: v < 0.1), ("mid", "10–30%", lambda v: 0.1 <= v <= 0.3),
                             ("hi", "> 30%", lambda v: v > 0.3)]),
    "rug": ("Риск rug", [("lo", "< 0.1", lambda v: v < 0.1), ("mid", "0.1–0.3", lambda v: 0.1 <= v <= 0.3),
                         ("hi", "> 0.3", lambda v: v > 0.3)]),
    "smart": ("Умных кошельков", [("0", "нет", lambda v: v < 1), ("1", "1–2", lambda v: 1 <= v < 3),
                                  ("3", "3+", lambda v: v >= 3)]),
    "liq": ("Ликвидность", [("lo", "< $20k", lambda v: v < 20_000), ("mid", "$20–100k", lambda v: 20_000 <= v <= 100_000),
                            ("hi", "> $100k", lambda v: v > 100_000)]),
    "age_h": ("Возраст", [("lo", "< 1 ч", lambda v: v < 1), ("mid", "1–24 ч", lambda v: 1 <= v <= 24),
                          ("hi", "> 24 ч", lambda v: v > 24)]),
    "bs": ("Покупки/продажи за час", [("lo", "продаж больше", lambda v: v < 0.9),
                                      ("mid", "поровну", lambda v: 0.9 <= v <= 1.3),
                                      ("hi", "покупок больше в 1,3+ раза", lambda v: v > 1.3)]),
}


def _shrunk(rets: list[float], prior: int = 5) -> float:
    return sum(rets) / (len(rets) + prior) if rets else 0.0


def _le(f: dict, k: str, x: float) -> bool:
    v = f.get(k)
    return v is not None and v <= x


def _ge(f: dict, k: str, x: float) -> bool:
    v = f.get(k)
    return v is not None and v >= x


def is_safe(f: dict) -> bool:
    """Как пресет safe у GMGN/Axiom. Неизвестное значение — не «безопасно»."""
    return (_le(f, "rug", 0.3) and _le(f, "bundler", 0.3) and _le(f, "insider", 0.3) and _le(f, "top10", 0.5)
            and not f.get("wash"))


def bucket_ids(f: dict) -> list[str]:
    out = []
    for key, (_title, rows) in BUCKETS.items():
        v = f.get(key)
        if v is None:
            continue
        for bid, _label, test in rows:
            if test(v):
                out.append(f"{key}:{bid}")
                break
    for s in f.get("src") or []:
        out.append(f"src:{s}")
    return out


def bucket_label(bid: str) -> str:
    key, _, sub = bid.partition(":")
    if key == "src":
        return f"Источник: {SRC_TEXT.get(sub, sub)}"
    title, rows = BUCKETS.get(key, (key, []))
    label = next((lb for b, lb, _t in rows if b == sub), sub)
    return f"{title} {label}"


def passes(name: str, f: dict, avoid: list[str] | None = None) -> bool:
    src = set(f.get("src") or [])
    smart = max(f.get("smart") or 0, f.get("smart_buys") or 0)
    if name == "all":
        return True
    if name == "safe":
        return is_safe(f)
    if name == "smart":
        return smart >= 2
    if name == "strict":
        return is_safe(f) and smart >= 1 and (f.get("dev_sold") is True or _le(f, "dev", 0.05)) and _ge(f, "liq", 20_000)
    if name == "stretch":
        return "stretch" in src
    if name == "migrated":
        return "migrated" in src
    if name == "momentum":
        return _ge(f, "chg5m", 2.0) and _ge(f, "bs", 1.2) and _ge(f, "liq", 15_000)
    if name == "callout":
        return bool(src & ({"callout", "kol"} | STREAM_SRC))
    if name == "learned":
        bad = set(avoid or [])
        return not (bad & set(bucket_ids(f)))
    return False


def exit_point(path: list[float], stop: float | None, tp: float | None) -> tuple[float, str]:
    for p in path:
        if stop and p <= -stop * 100:
            return p, "stop"
        if tp and p >= tp * 100:
            return p, "tp"
    return (path[-1] if path else 0.0), "time"


def net_pct(p: float, usd: float, cost: float, network_usd: float = 0.01) -> float:
    """Чистый % сделки с мемкоином: комиссия/проскальзывание с двух сторон + сеть Solana за 2 транзакции."""
    return ((1 + p / 100) * (1 - cost) / (1 + cost) - 1) * 100 - 2 * network_usd / max(usd, 0.5) * 100


def new_book() -> dict:
    return {"cash": START_USD, "positions": {}, "n": 0, "wins": 0, "pnl": 0.0, "rets": [], "closed": []}


def new_state() -> dict:
    return {"on": True, "notify": True, "picks": {}, "done": [], "seen": {}, "books": {k: new_book() for k in PRESETS},
            "active": None, "rule": dict(DEFAULT_RULE), "avoid": [], "log": [], "scans": 0, "last_scan": 0.0,
            "last_update": 0.0, "started": 0.0, "source": "", "last_error": None, "signals": 0, "seq": 0,
            "external": []}


def _short(mint: str) -> str:
    return f"{mint[:4]}…{mint[-4:]}" if len(mint) > 10 else mint


def _usd(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.0f}k"
    return f"${v:.0f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


class Pulse:
    def __init__(self, copy, gmgn: GmgnClient | None = None) -> None:
        self.copy = copy
        self.gmgn = gmgn if gmgn is not None else GmgnClient()
        self._sec_cache: dict[str, tuple[float, dict | None]] = {}
        self.busy = False

    # --- состояние --------------------------------------------------------------------------

    @property
    def st(self) -> dict:
        st = self.copy.st.get("pulse")
        if not isinstance(st, dict):
            st = {}
            self.copy.st["pulse"] = st
        for k, v in new_state().items():
            st.setdefault(k, v)
        for k in PRESETS:
            st["books"].setdefault(k, new_book())
        return st

    def _changed(self) -> None:
        self.copy.changes += 1

    def _log(self, text: str, now: float) -> str:
        st = self.st
        st["log"] = (st["log"] + [{"at": now, "text": text}])[-20:]
        self._changed()
        return text

    def _cost(self) -> float:
        try:
            return float(self.copy.cost_side())
        except Exception:
            return 0.012

    # --- внешние кандидаты (стримы) -----------------------------------------------------------

    def add_external(self, mint: str, src: str = "stream", who: str = "", now: float | None = None) -> None:
        """Токен мелькнул у стримера → в очередь кандидатов (обработаем на ближайшем поиске)."""
        now = time.time() if now is None else now
        st = self.st
        if not mint or any(x["mint"] == mint for x in st["external"]):
            return
        st["external"] = (st["external"] + [{"mint": mint, "src": src, "who": who[:30], "at": now}])[-20:]
        self._changed()

    # --- проверка одного токена (для стримов) ---------------------------------------------------

    async def token_check(self, mint: str, now: float | None = None) -> dict | None:
        """Цифры безопасности токена: GMGN (если есть ключ), иначе RugCheck. Кэш 10 минут."""
        now = time.time() if now is None else now
        hit = self._sec_cache.get(mint)
        if hit and now - hit[0] < 600:
            return hit[1]
        out: dict | None = None
        if self.gmgn.configured:
            try:
                out = await self.gmgn.security(mint)
                out["src"] = "GMGN"
            except GmgnError as e:
                log.info("GMGN security %s: %s", mint, e)
        if out is None:
            try:
                rug = await self.copy.dex.rug_safe(mint)
            except Exception as e:
                log.info("RugCheck %s: %s", mint, e)
                rug = None
            if rug and rug.get("score") is not None:
                out = {"rug": min(1.0, float(rug["score"]) / 100), "src": "RugCheck",
                       "risks": [r.get("name") for r in rug.get("risks", []) if r.get("level") == "danger"][:3]}
        self._sec_cache[mint] = (now, out)
        if len(self._sec_cache) > 200:
            for k in sorted(self._sec_cache, key=lambda k: self._sec_cache[k][0])[:80]:
                self._sec_cache.pop(k, None)
        return out

    # --- поиск кандидатов -------------------------------------------------------------------------

    async def _gmgn_candidates(self, now: float) -> dict[str, dict]:
        """GMGN → {mint: {признаки GMGN, src: set}}. Каждый метод отдельно: один сбой не губит остальные."""
        out: dict[str, dict] = {}

        def put(t: dict, src: str) -> dict:
            c = out.get(t["mint"])
            if c is None:
                c = out[t["mint"]] = {"g": {}, "src": set(), "symbol": t.get("symbol") or "", "who": ""}
            for k, v in t.items():
                if v is not None and k not in ("mint", "category") and c["g"].get(k) is None:
                    c["g"][k] = v
            c["src"].add(src)
            return c

        errors = []
        try:
            for t in await self.gmgn.trending("sol", "5m", limit=50):
                put(t, "trend")
        except GmgnError as e:
            errors.append(str(e))
        try:
            for t in await self.gmgn.trenches("sol", limit=30):
                put(t, t["category"] or "new")
        except GmgnError as e:
            errors.append(str(e))
        for name, fn in (("smart", self.gmgn.smartmoney), ("kol", self.gmgn.kol)):
            try:
                trades = await fn("sol", limit=100)
            except GmgnError as e:
                errors.append(str(e))
                continue
            buyers: dict[str, set] = {}
            sym: dict[str, str] = {}
            who: dict[str, str] = {}
            for tr in trades:
                if tr["side"] != "buy" or now - (tr["at"] or now) > 3600:
                    continue
                buyers.setdefault(tr["mint"], set()).add(tr["maker"])
                sym[tr["mint"]] = tr["symbol"]
                if tr["who"]:
                    who[tr["mint"]] = tr["who"]
            for mint, makers in buyers.items():
                c = put({"mint": mint, "symbol": sym.get(mint, "")}, name)
                c["g"][f"{name}_buys"] = len(makers)
                if who.get(mint) and not c["who"]:
                    c["who"] = who[mint]
        try:
            for co in await self.gmgn.callouts("sol", limit=30):
                if now - (co["at"] or now) > 3 * 3600 or co["safe"] == "risk":
                    continue
                c = put({"mint": co["mint"], "symbol": co["symbol"]}, "callout")
                c["g"]["calls"] = (c["g"].get("calls") or 0) + 1
                if co["who"] and not c["who"]:
                    c["who"] = co["who"]
        except GmgnError as e:
            errors.append(str(e))
        if errors and not out:
            raise GmgnError(errors[0])
        return out

    @staticmethod
    def _priority(c: dict) -> tuple:
        src = c["src"]
        g = c["g"]
        return (0 if src & STREAM_SRC else 1, -(g.get("smart_buys") or 0), 0 if "callout" in src else 1,
                0 if src & {"stretch", "migrated"} else 1, 0 if "trend" in src else 1, -(g.get("smart") or 0))

    async def scan(self, now: float | None = None) -> list[dict]:
        """Найти новых кандидатов, записать цену и признаки, открыть бумажные сделки стратегий."""
        now = time.time() if now is None else now
        st = self.st
        st["last_scan"] = now
        st["scans"] += 1
        if not st["started"]:
            st["started"] = now
        st["seen"] = {m: t for m, t in st["seen"].items() if now - t < SEEN_SEC}
        cands: dict[str, dict] = {}
        sources = []
        if self.gmgn.configured:
            try:
                cands = await self._gmgn_candidates(now)
                sources.append("GMGN")
                st["last_error"] = None
            except GmgnError as e:
                st["last_error"] = str(e)[:160]
        try:
            for p in await self.copy.dex.trending("solana", limit=30):
                c = cands.setdefault(p.address, {"g": {}, "src": set(), "symbol": p.symbol, "who": ""})
                if p.boosts:
                    c["src"].add("promo")
                elif not c["src"]:
                    c["src"].add("trend")
            sources.append("DexScreener")
        except Exception as e:
            log.info("Пульс: DexScreener: %s", e)
            if not cands:
                st["last_error"] = f"DexScreener: {e}"[:160]
        for x in st["external"]:
            c = cands.setdefault(x["mint"], {"g": {}, "src": set(), "symbol": "", "who": ""})
            c["src"].add(x["src"])
            c["who"] = c["who"] or x["who"]
        st["external"] = []
        st["source"] = " + ".join(sources)
        fresh = [(m, c) for m, c in cands.items() if m not in st["seen"] and m not in st["picks"]]
        fresh.sort(key=lambda mc: self._priority(mc[1]))
        room = max(0, MAX_TRACK - len(st["picks"]))
        fresh = fresh[:min(MAX_NEW, room)]
        if not fresh:
            self._changed()
            return []
        try:
            pairs = await self.copy._pairs([m for m, _ in fresh])
        except Exception as e:
            log.info("Пульс: цены: %s", e)
            self._changed()
            return []
        events: list[dict] = []
        no_gmgn = []
        added = []
        for mint, c in fresh:
            pair = pairs.get(mint)
            st["seen"][mint] = now
            if not pair or not pair.price_usd or (pair.liquidity_usd or 0) < MIN_LIQ:
                continue
            g = c["g"]
            if g.get("honeypot") or (pair.buys_h1 >= 30 and pair.sells_h1 == 0):
                continue  # продать нельзя — это не сделка даже на бумаге
            f = self._features(pair, g, c["src"], now)
            if not g:
                no_gmgn.append(mint)
            pick = {"id": f"p{st['seq']}", "mint": mint, "symbol": (pair.symbol or c["symbol"] or _short(mint))[:16],
                    "at": now, "price0": pair.price_usd, "f": f, "path": [0.0], "marks": {}, "max": 0.0,
                    "who": c["who"], "until": now + HOLD_H * 3600}
            st["seq"] += 1
            st["picks"][mint] = pick
            added.append(pick)
        if no_gmgn and not self.gmgn.configured:  # без GMGN — хотя бы риск по RugCheck для части токенов
            for mint in no_gmgn[:6]:
                chk = await self.token_check(mint, now)
                if chk and chk.get("rug") is not None:
                    st["picks"][mint]["f"]["rug"] = round(chk["rug"], 3)
        for pick in added:
            events += self._open_books(pick, now)
        self._changed()
        return events

    @staticmethod
    def _features(pair, g: dict, src: set, now: float) -> dict:
        bs = None
        if pair.buys_h1 or pair.sells_h1:
            bs = round(pair.buys_h1 / max(1, pair.sells_h1), 2)
        age_h = round((now - pair.created_at) / 3600, 2) if pair.created_at else None
        f = {"liq": pair.liquidity_usd, "mcap": pair.market_cap or pair.fdv, "vol": pair.volume_h24,
             "chg5m": pair.change.get("m5"), "chg1h": pair.change.get("h1"), "bs": bs, "age_h": age_h,
             "src": sorted(src)}
        for k in ("top10", "dev", "snipers", "bundler", "insider", "rug", "smart", "kol", "holders", "dev_sold",
                  "smart_buys", "kol_buys", "calls", "progress", "wash"):
            if g.get(k) is not None:
                f[k] = round(g[k], 3) if isinstance(g[k], float) else g[k]
        return f

    # --- бумажные сделки стратегий ------------------------------------------------------------------

    def _equity(self, b: dict) -> float:
        cost = self._cost()
        return b["cash"] + sum(p["usd"] * (1 + net_pct(p["now"], p["usd"], cost) / 100) for p in b["positions"].values())

    def _open_books(self, pick: dict, now: float) -> list[dict]:
        st = self.st
        rule = st["rule"]
        events = []
        for name, b in st["books"].items():
            if name not in PRESETS or not passes(name, pick["f"], st["avoid"]):
                continue
            usd = round(max(MIN_TRADE, self._equity(b) * SIZE_SHARE), 2)
            if pick["mint"] in b["positions"] or len(b["positions"]) >= MAX_OPEN or b["cash"] < usd:
                continue
            b["cash"] = round(b["cash"] - usd, 6)
            b["positions"][pick["mint"]] = {"mint": pick["mint"], "symbol": pick["symbol"], "usd": usd, "at": now,
                                            "until": pick["until"], "stop": rule["stop"], "tp": rule["tp"],
                                            "now": 0.0}
        active = st["active"]
        if active and st["notify"] and passes(active, pick["f"], st["avoid"]) and \
                not (set(st["avoid"]) & set(bucket_ids(pick["f"]))):
            st["signals"] += 1
            events.append(self._signal(pick, active))
        return events

    def _signal(self, pick: dict, active: str) -> dict:
        st = self.st
        b = st["books"][active]
        f = pick["f"]
        nums = [f"ликв. {_usd(f.get('liq'))}", f"кап. {_usd(f.get('mcap'))}"]
        for k, label in (("top10", "топ-10"), ("dev", "dev"), ("bundler", "бандлы"), ("insider", "инсайдеры")):
            if f.get(k) is not None:
                nums.append(f"{label} {_pct(f[k])}")
        if f.get("smart") or f.get("smart_buys"):
            nums.append(f"умных {int(max(f.get('smart') or 0, f.get('smart_buys') or 0))}")
        src = ", ".join(SRC_TEXT.get(s, s) for s in f.get("src") or [])
        r = st["rule"]
        text = (f"⚡ <b>Пульс: сигнал стратегии «{PRESETS[active][0]}»</b>\n"
                f"<b>{html.escape(pick['symbol'])}</b> <code>{pick['mint']}</code>\n"
                f"Откуда: {html.escape(src)}" + (f" ({html.escape(pick['who'])})" if pick.get("who") else "") + "\n"
                + " · ".join(nums) + "\n"
                f"Правила с бумаги: стоп −{r['stop']:.0%}, тейк {('+' + format(r['tp'], '.0%')) if r['tp'] else 'нет'}, "
                f"держать до {HOLD_H} ч.\nСтратегия на бумаге: {b['n']} сделок, в плюс {b['wins']}, в среднем "
                f"{_shrunk(b['rets']):+.1f}% за сделку (с поправкой на везение). Это не гарантия.")
        return {"text": text, "buttons": [("📈 DexScreener", f"https://dexscreener.com/solana/{pick['mint']}"),
                                          ("🟢 GMGN", f"https://gmgn.ai/sol/token/{pick['mint']}")],
                "mint": pick["mint"]}

    # --- цены и итоги ---------------------------------------------------------------------------------

    async def update(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        st["last_update"] = now
        if not st["picks"]:
            return []
        from .copytrade import add_path
        try:
            pairs = await self.copy._pairs(list(st["picks"]))
        except Exception as e:
            log.info("Пульс: цены: %s", e)
            return []
        for mint, pick in st["picks"].items():
            pair = pairs.get(mint)
            if pair and pair.price_usd and pick["price0"]:
                pick.pop("miss", None)
                pct = (pair.price_usd / pick["price0"] - 1) * 100
                add_path(pick["path"], pct)
                pick["max"] = round(max(pick["max"], pct), 1)
                for k, sec in MARKS.items():
                    if k not in pick["marks"] and now - pick["at"] >= sec:
                        pick["marks"][k] = round(pct, 1)
            elif pair is None:
                pick["miss"] = pick.get("miss", 0) + 1
                if pick["miss"] >= 3 and pick["path"][-1] > -95:
                    # пару трижды подряд нет на DexScreener (пул слили) — считаем как −95%, честнее, чем «нет данных»
                    add_path(pick["path"], -95.0)
        events: list[dict] = []
        closed = 0
        for name, b in st["books"].items():
            for mint, pos in list(b["positions"].items()):
                pick = st["picks"].get(mint)
                path = pick["path"] if pick else [pos["now"]]
                pos["now"] = path[-1]
                p, why = exit_point(path, pos["stop"], pos["tp"])
                if why == "time" and now < pos["until"] and pick is not None:
                    continue
                self._close(b, pos, p, why, now)
                closed += 1
                if st["notify"] and name == st["active"]:
                    c = b["closed"][-1]
                    events.append({"text": f"⚡ Пульс («{PRESETS[name][0]}», бумага): {html.escape(c['symbol'])} "
                                           f"закрыт — {({'stop': 'стоп', 'tp': 'тейк', 'time': 'сутки прошли'})[why]}, "
                                           f"<b>{c['pct']:+.1f}%</b>", "buttons": []})
        for mint, pick in list(st["picks"].items()):
            if now >= pick["until"]:
                self._archive(pick)
                del st["picks"][mint]
                closed += 1
        if closed:
            events += self.learn(now)
        self._changed()
        return events

    def _close(self, b: dict, pos: dict, p: float, why: str, now: float) -> None:
        r = net_pct(p, pos["usd"], self._cost())
        back = pos["usd"] * (1 + r / 100)
        b["cash"] = round(b["cash"] + back, 6)
        b["n"] += 1
        b["wins"] += r > 0
        b["pnl"] = round(b["pnl"] + back - pos["usd"], 6)
        b["rets"] = (b["rets"] + [round(r, 2)])[-60:]
        b["closed"] = (b["closed"] + [{"symbol": pos["symbol"], "pct": round(r, 2), "why": why, "at": pos["at"],
                                       "closed": now}])[-20:]
        del b["positions"][pos["mint"]]

    def _archive(self, pick: dict) -> None:
        st = self.st
        st["done"].append({"mint": pick["mint"], "symbol": pick["symbol"], "at": pick["at"], "f": pick["f"],
                           "path": pick["path"], "marks": pick["marks"], "max": pick["max"]})
        del st["done"][:-DONE_KEEP]

    # --- обучение -------------------------------------------------------------------------------------

    def _ret(self, d: dict, rule: dict | None = None) -> float:
        rule = rule or self.st["rule"]
        p, _ = exit_point(d["path"], rule["stop"], rule["tp"])
        return net_pct(p, 2.0, self._cost())

    def insights(self) -> list[dict]:
        """Сколько в среднем приносит каждый признак (по закрытым кандидатам, с текущим стопом/тейком)."""
        from .profit import edge_stats
        groups: dict[str, list[float]] = {}
        for d in self.st["done"]:
            r = self._ret(d)
            for bid in bucket_ids(d["f"]):
                groups.setdefault(bid, []).append(r)
        out = []
        for bid, rets in groups.items():
            if len(rets) < INSIGHT_MIN:
                continue
            e = edge_stats(rets)
            out.append({"id": bid, "label": bucket_label(bid), "n": len(rets), "avg": round(sum(rets) / len(rets), 1),
                        "win": round(sum(r > 0 for r in rets) / len(rets) * 100), "verdict": e["verdict"],
                        "ucb": e["ucb"], "lcb": e["lcb"]})
        return sorted(out, key=lambda x: x["avg"])

    def learn(self, now: float) -> list[dict]:
        events = []
        for text in (self._learn_rule(now), self._learn_avoid(now), self._learn_best(now)):
            if text:
                events.append({"text": text, "buttons": []})
        return events if self.st["notify"] else []

    def _learn_rule(self, now: float) -> str | None:
        st = self.st
        done = [d for d in st["done"] if len(d["path"]) >= 3]
        if len(done) < RULE_MIN:
            return None
        cur = st["rule"]

        def total(rule):
            return sum(self._ret(d, rule) for d in done)

        base = total(cur)
        best, best_sum = cur, base
        for s in GRID_STOPS:
            for tp in GRID_TPS:
                rule = {"stop": s, "tp": tp}
                v = total(rule)
                if v > best_sum:
                    best, best_sum = rule, v
        if best == cur or (best_sum - base) / len(done) < RULE_GAIN:
            return None
        st["rule"] = dict(best)
        tp = f"+{best['tp']:.0%}" if best["tp"] else "нет"
        return self._log(f"⚡ Пульс подобрал выход по {len(done)} токенам: стоп −{best['stop']:.0%}, тейк {tp} — "
                         f"на тех же токенах это на {(best_sum - base) / len(done):+.1f}% за сделку лучше.", now)

    def _learn_avoid(self, now: float) -> str | None:
        st = self.st
        ins = self.insights()
        bad = [x for x in ins if x["n"] >= AVOID_MIN and x["verdict"] == "losing" and x["avg"] <= -10
               and not x["id"].startswith("src:")]
        new = sorted(x["id"] for x in bad)
        old = sorted(st["avoid"])
        if new == old:
            return None
        st["avoid"] = new
        added = [x for x in bad if x["id"] not in old]
        if not added:
            return None
        lines = "; ".join(f"{x['label']} — {x['avg']:+.0f}% в среднем на {x['n']} токенах" for x in added[:3])
        return self._log(f"🧠 Пульс выучил, чего избегать: {lines}. Стратегия «выученный фильтр» такие токены больше "
                         "не берёт.", now)

    def scores(self) -> dict[str, float]:
        return {n: _shrunk(b["rets"]) for n, b in self.st["books"].items() if len(b["rets"]) >= BEST_MIN}

    def _learn_best(self, now: float) -> str | None:
        from .profit import edge_stats
        st = self.st
        sc = self.scores()
        if not sc:
            return None
        best = max(sc, key=sc.get)
        e = edge_stats(st["books"][best]["rets"])
        cur = st["active"]
        if sc[best] <= 0 or e["verdict"] not in ("proven", "promising"):
            if cur:
                st["active"] = None
                return self._log(f"⚡ Пульс: ни одна стратегия сейчас уверенно не в плюсе (лучшая «{PRESETS[best][0]}»: "
                                 f"{sc[best]:+.1f}% за сделку). Сигналы выключил, пока не станет лучше.", now)
            return None
        if best == cur or (cur in sc and sc[best] - sc[cur] < 1.0):
            return None
        st["active"] = best
        b = st["books"][best]
        return self._log(f"⚡ Пульс: лучшая стратегия на бумаге — «{PRESETS[best][0]}» ({PRESETS[best][1]}): "
                         f"{b['n']} сделок, в плюс {b['wins']}, в среднем {sc[best]:+.1f}% за сделку. Теперь сигналы — "
                         "по ней.", now)

    # --- шаг ---------------------------------------------------------------------------------------------

    async def step(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        if not st["on"] or self.busy:
            return []
        self.busy = True
        try:
            events: list[dict] = []
            if now - st["last_update"] >= UPDATE_SEC:
                events += await self.update(now)
            if now - st["last_scan"] >= SCAN_SEC or (st["external"] and now - st["last_scan"] >= 60):
                events += await self.scan(now)
            return events
        finally:
            self.busy = False

    # --- вид ---------------------------------------------------------------------------------------------

    def books_view(self) -> list[dict]:
        from .profit import edge_stats
        st = self.st
        out = []
        for name, b in st["books"].items():
            if name not in PRESETS:
                continue
            e = edge_stats(b["rets"])
            out.append({"id": name, "title": PRESETS[name][0], "how": PRESETS[name][1], "n": b["n"], "wins": b["wins"],
                        "open": len(b["positions"]), "equity": round(self._equity(b), 2), "avg": round(_shrunk(b["rets"]), 2),
                        "verdict": e["verdict"], "active": name == st["active"]})
        return sorted(out, key=lambda x: (not x["active"], -x["avg"] if x["n"] >= BEST_MIN else 99, -x["n"]))

    def status_dict(self) -> dict:
        st = self.st
        recent = sorted(st["picks"].values(), key=lambda p: -p["at"])[:6]
        return {"on": st["on"], "notify": st["notify"], "gmgn": self.gmgn.status(), "source": st["source"],
                "tracking": len(st["picks"]), "done": len(st["done"]), "scans": st["scans"], "active": st["active"],
                "active_title": PRESETS[st["active"]][0] if st["active"] in PRESETS else None,
                "rule": st["rule"], "books": self.books_view(), "insights": self.insights(),
                "avoid": [bucket_label(x) for x in st["avoid"]], "signals": st["signals"],
                "recent": [{"symbol": p["symbol"], "mint": p["mint"], "now": p["path"][-1], "max": p["max"],
                            "src": [SRC_TEXT.get(s, s) for s in p["f"].get("src") or []], "at": p["at"]} for p in recent],
                "log": list(reversed(st["log"][-5:])), "error": st["last_error"]}

    def engine_rets(self) -> tuple[str, list[float], float | None]:
        """Для Профит-центра: активная стратегия (или самая опытная) → (название, итоги сделок, начало)."""
        st = self.st
        name = st["active"] or max(st["books"], key=lambda k: st["books"][k]["n"])
        b = st["books"][name]
        return PRESETS.get(name, (name,))[0], list(b["rets"]), st["started"] or None

    def ai_context(self) -> dict:
        d = self.status_dict()
        return {"how": "⚡ пульс рынка: GMGN (тренды, окопы = Axiom Pulse, умные деньги, KOL, коллы) + DexScreener; "
                       "кандидатов торгует на бумаге 9 стратегий-фильтров, бот учится, какие признаки токена дают плюс",
                "gmgn_key": d["gmgn"]["configured"], "tracking": d["tracking"], "closed": d["done"],
                "best_strategy": d["active_title"], "exit_rule": d["rule"],
                "strategies": [{"name": b["title"], "trades": b["n"], "avg_pct": b["avg"], "verdict": b["verdict"]}
                               for b in d["books"]],
                "what_works": [{"what": x["label"], "tokens": x["n"], "avg_pct": x["avg"]} for x in d["insights"][-4:]],
                "what_loses": [{"what": x["label"], "tokens": x["n"], "avg_pct": x["avg"]} for x in d["insights"][:4]],
                "avoid_learned": d["avoid"]}

    def text(self) -> str:
        d = self.status_dict()
        g = d["gmgn"]
        if g["configured"]:
            src = "GMGN" + (" (тестовый ключ — поставь свой бесплатный)" if g["test_key"] else "") + " + DexScreener"
        else:
            src = "DexScreener + RugCheck (для полного пульса добавь бесплатный GMGN_API_KEY)"
        lines = ["⚡ <b>Пульс рынка</b> — бот смотрит GMGN и Axiom Pulse, торгует кандидатами на бумаге и учится",
                 f"{'🟢 работает' if d['on'] else '⏸ выключен'} · источники: {src}",
                 f"Веду {d['tracking']} токенов, закрыто {d['done']} · поисков {d['scans']} · выход: стоп "
                 f"−{d['rule']['stop']:.0%}, тейк {('+' + format(d['rule']['tp'], '.0%')) if d['rule']['tp'] else 'нет'}"]
        lines.append("\n<b>Стратегии-фильтры</b> (по $10 на бумаге, комиссии и сеть учтены):")
        icons = {"proven": "✅", "promising": "🟡", "flat": "⚪", "losing": "🔴", "data": "⏳", "none": "▫️"}
        for b in d["books"]:
            mark = "👉 " if b["active"] else ""
            row = f"{mark}{icons.get(b['verdict'], '•')} <b>{html.escape(b['title'])}</b>: "
            row += (f"{b['n']} сделок, в плюс {b['wins']}, в среднем {b['avg']:+.1f}%, счёт ${b['equity']:.2f}"
                    if b["n"] else f"сделок ещё нет, открыто {b['open']}")
            lines.append(row)
        if d["active_title"]:
            lines.append(f"Сигналы приходят от стратегии «{html.escape(d['active_title'])}».")
        else:
            lines.append(f"Сигналов пока нет: ни одна стратегия не доказала плюс (нужно от {BEST_MIN} сделок).")
        ins = d["insights"]
        if ins:
            lines.append("\n<b>Чему научился на рынке</b> (средний итог токенов с этим признаком):")
            for x in ins[:3]:
                lines.append(f"❌ {html.escape(x['label'])}: {x['n']} шт., <b>{x['avg']:+.0f}%</b>, в плюс {x['win']}%")
            for x in ins[-3:][::-1]:
                if x["avg"] > 0:
                    lines.append(f"✅ {html.escape(x['label'])}: {x['n']} шт., <b>{x['avg']:+.0f}%</b>, в плюс {x['win']}%")
        else:
            lines.append(f"\n🧠 Выводы появятся, когда закроется хотя бы {INSIGHT_MIN} токенов с одним признаком "
                         f"(каждого веду {HOLD_H} ч).")
        if d["avoid"]:
            lines.append("🚫 Стоп-лист (выучен сам): " + "; ".join(html.escape(x) for x in d["avoid"][:5]))
        if d["recent"]:
            lines.append("\n<b>Свежие кандидаты</b>:")
            for p in d["recent"][:4]:
                lines.append(f"• {html.escape(p['symbol'])} ({html.escape(', '.join(p['src'][:2]))}): сейчас "
                             f"{p['now']:+.0f}%, максимум {p['max']:+.0f}%")
        for item in d["log"][:2]:
            lines.append(f"<i>{html.escape(item['text'][:220])}</i>")
        if d["error"]:
            lines.append(f"⚠️ {html.escape(d['error'])}")
        if not g["configured"]:
            lines.append("\n🔑 Ключ GMGN бесплатный: gmgn.ai/ai → создай API key → Render → Environment → "
                         "<code>GMGN_API_KEY</code>. Только чтение, к деньгам доступа нет.")
        lines.append("ℹ️ У Axiom нет открытого API — его Pulse (New Pairs / Final Stretch / Migrated) бот повторяет через "
                     "GMGN, данные те же. Настоящие деньги пульс не трогает.")
        return "\n".join(lines)


__all__ = ["Pulse", "PRESETS", "passes", "is_safe", "bucket_ids", "bucket_label", "net_pct", "new_state",
           "SCAN_SEC", "UPDATE_SEC", "HOLD_H"]
