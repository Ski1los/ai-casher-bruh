"""🏋️ Тренировка: бот сам торгует на демо-счёте тем, чему научился у трейдеров (/mentors).

Как устроено:
  * Каждый прогноз «вырастет» (монета с Bybit/BingX) и каждый токен, который стример показал в чате Twitch, —
    это идея для сделки. Бот открывает её на виртуальных деньгах по цене в момент прогноза.
  * Одновременно работают 5 стратегий (по $10 виртуальных у каждой). Они отличаются тем, каким идеям верят:
    всем подряд, только проверенным авторам, только когда 2+ автора согласны, без рекламы и хайпа, только
    уверенным прогнозам. Все торгуют одинаково: размер 25% счёта, стоп и тейк-профит, комиссии и проскальзывание.
  * Бот учится на своих демо-сделках:
      1) выбирает лучшую стратегию (от 8 сделок, средний результат с поправкой на везение) — только её идеи
         приходят тебе сигналом;
      2) подбирает стоп и тейк-профит по пути цены (цену бот отслеживает весь срок идеи, даже после выхода —
         поэтому видно, что было бы при другом стопе), один шаг за раз и только при выигрыше от 5%.
  * Настоящие деньги тренировка не трогает. Шорты не торгует: спот — только покупка.
"""

from __future__ import annotations

import html
import logging
import time

from .copytrade import NETWORK_USD, add_path

log = logging.getLogger(__name__)

START_USD = 10.0
SIZE_SHARE = 0.25          # сделка = 25% счёта стратегии
MIN_TRADE_USD = 1.0
MAX_OPEN = 4               # не больше 4 открытых сделок у стратегии
CEX_SIDE_COST = 0.0015     # спот: комиссия 0,1% + проскальзывание 0,05% в каждую сторону
CEX_MAX_HOLD_H = 7 * 24    # длинные прогнозы (до 30 дней) на тренировке держим не дольше недели
SOL_MAX_HOLD_H = 24
BEST_MIN = 8               # стратегию оцениваем не раньше чем по 8 закрытым сделкам
BEST_MARGIN = 1.0          # меняем активную, только если новая лучше хотя бы на 1% за сделку
EXIT_MIN_PATHS = 15        # стоп/тейк подбираем не раньше чем по 15 идеям с полным путём цены
EXIT_GAIN_PCT = 5.0        # и только если сумма результатов лучше хотя бы на 5%
VERDICT_MIN = 10
CONSENSUS_SEC = {"cex": 7 * 86400, "sol": 86400}

GRID = {
    "cex": {"stops": (0.05, 0.08, 0.12, 0.2), "tps": (None, 0.05, 0.1, 0.2)},
    "sol": {"stops": (0.2, 0.3, 0.4), "tps": (None, 0.3, 0.5, 1.0)},
}
DEFAULT_RULES = {"cex": {"stop": 0.08, "tp": None}, "sol": {"stop": 0.4, "tp": None}}

VARIANTS = {
    "all": ("Все прогнозы", "открывает каждую идею — для сравнения"),
    "trusted": ("Проверенные авторы", "автор ⭐ или хотя бы 3 проверки в плюсе"),
    "consensus": ("Согласие 2+ авторов", "монету хвалят минимум два разных трейдера"),
    "clean": ("Без рекламы и хайпа", "ролик без рекламы и хайп меньше 7/10"),
    "confident": ("Уверенные прогнозы", "автор уверен или сам стример пишет адрес"),
}


def _shrunk(rets: list[float], prior: int = 5) -> float:
    return sum(rets) / (len(rets) + prior) if rets else 0.0


def net_pct(p: float, kind: str, usd: float, cost: float) -> float:
    """Чистый результат в %, если цена сдвинулась на p%: комиссии туда и обратно (+ сеть Solana для мемкоинов)."""
    r = ((1 + p / 100) * (1 - cost) / (1 + cost) - 1) * 100
    if kind == "sol":
        r -= 2 * NETWORK_USD / max(usd, 0.5) * 100
    return r


def exit_point(path: list[float], stop: float | None, tp: float | None) -> tuple[float, str]:
    """По пути цены (в % от входа): где бы вышли и почему."""
    for p in path:
        if stop and p <= -stop * 100:
            return p, "stop"
        if tp and p >= tp * 100:
            return p, "tp"
    return (path[-1] if path else 0.0), "time"


def passes(name: str, f: dict) -> bool:
    if name == "all":
        return True
    if name == "trusted":
        return f["status"] == "star" or (f["checked"] >= 3 and f["avg"] > 0)
    if name == "consensus":
        return f["authors"] >= 2
    if name == "clean":
        return not f["sponsored"] and f["hype"] < 7
    if name == "confident":
        return f["conf"] == "high"
    return False


def rule_text(kind: str, r: dict) -> str:
    tp = f"+{r['tp']:.0%}" if r["tp"] else "нет"
    return f"{'монеты' if kind == 'cex' else 'мемкоины'}: стоп −{r['stop']:.0%}, тейк {tp}"


def new_state() -> dict:
    return {"on": True, "signals": True, "notify": False, "start_usd": START_USD, "started": 0.0,
            "variants": {}, "rules": {k: dict(v) for k, v in DEFAULT_RULES.items()}, "active": None,
            "track": {}, "paths": [], "lessons": [], "ideas": 0, "verdict_sent": False}


class Training:
    def __init__(self, mentors) -> None:
        self.m = mentors

    # --- состояние -----------------------------------------------------------------

    @property
    def st(self) -> dict:
        ms = self.m.st
        st = ms.get("train")
        if not isinstance(st, dict):
            st = ms["train"] = {}
        for k, v in new_state().items():
            st.setdefault(k, v)
        for name in VARIANTS:
            if name not in st["variants"]:
                st["variants"][name] = self._fresh_variant(st["start_usd"])
        if not st["started"]:
            st["started"] = time.time()
        return st

    @staticmethod
    def _fresh_variant(usd: float) -> dict:
        return {"cash": usd, "positions": {}, "closed": [], "rets": [], "n": 0, "wins": 0, "pnl": 0.0, "skipped": 0}

    def _lesson(self, text: str, now: float) -> str:
        self.st["lessons"].append({"at": now, "text": text})
        del self.st["lessons"][:-30]
        self.m._changed()
        return text

    def reset(self, usd: float, now: float | None = None) -> None:
        """Начать демо заново: деньги и сделки обнуляются, выученное (стоп/тейк, активная стратегия) остаётся."""
        now = time.time() if now is None else now
        st = self.st
        st["start_usd"] = float(usd)
        st["variants"] = {name: self._fresh_variant(float(usd)) for name in VARIANTS}
        st["started"], st["verdict_sent"] = now, False
        self.m._changed()

    def _cost(self, kind: str) -> float:
        return CEX_SIDE_COST if kind == "cex" else self.m.copy.cost_side()

    # --- новая идея ------------------------------------------------------------------

    def features(self, call: dict, src: dict | None, meta: dict) -> dict:
        ms = self.m.st
        authors = set()
        horizon = CONSENSUS_SEC[call["kind"]]
        for c in ms["calls"]:
            if c["key"] == call["key"] and c["direction"] == "long" and call["at"] - c["at"] <= horizon and c["source"]:
                s = ms["sources"].get(c["source"])
                if s and s["status"] != "muted":
                    authors.add(c["source"])
        rets = (src or {}).get("rets") or []
        conf = call.get("conf") or "medium"
        if call["kind"] == "sol" and src and meta.get("by_streamer"):
            conf = "high"
        return {"status": (src or {}).get("status", "watch"), "checked": len(rets), "avg": _shrunk(rets),
                "authors": len(authors), "sponsored": bool(meta.get("sponsored")), "hype": int(meta.get("hype") or 0),
                "conf": conf}

    def on_call(self, call: dict, src: dict | None, meta: dict | None = None, now: float | None = None) -> dict | None:
        """Прогноз «вырастет» → сделки на демо у тех стратегий, которые ему верят. Возвращает сигнал, если идея
        проходит активную (лучшую) стратегию."""
        now = time.time() if now is None else now
        st = self.st
        if not st["on"] or call["direction"] != "long" or not call.get("price0"):
            return None
        meta = meta or {}
        f = self.features(call, src, meta)
        kind = call["kind"]
        hold_h = min(call["horizon_h"], CEX_MAX_HOLD_H if kind == "cex" else SOL_MAX_HOLD_H)
        st["ideas"] += 1
        st["track"][call["id"]] = {"kind": kind, "asset": call["asset"], "mint": call.get("mint") or "",
                                   "price0": call["price0"], "at": now, "until": now + hold_h * 3600, "path": [0.0]}
        rule = st["rules"][kind]
        author = (src or {}).get("name", "")
        for name, v in st["variants"].items():
            if not passes(name, f):
                continue
            held = {p["key"] for p in v["positions"].values()}
            equity = self.equity(v)
            usd = round(max(MIN_TRADE_USD, equity * SIZE_SHARE), 2)
            if call["key"] in held or len(v["positions"]) >= MAX_OPEN or v["cash"] < usd:
                v["skipped"] += 1
                continue
            v["cash"] = round(v["cash"] - usd, 6)
            v["positions"][call["id"]] = {"id": call["id"], "key": call["key"], "kind": kind, "asset": call["asset"],
                                          "mint": call.get("mint") or "", "author": author, "at": now, "usd": usd,
                                          "until": now + hold_h * 3600, "stop": rule["stop"], "tp": rule["tp"],
                                          "now": 0.0}
        self.m._changed()
        active = st["active"]
        if st["signals"] and active and passes(active, f):
            return self._signal_event(call, author, active, f)
        return None

    def _signal_event(self, call: dict, author: str, active: str, f: dict) -> dict:
        v = self.st["variants"][active]
        r = self.st["rules"][call["kind"]]
        hold = "24 ч" if call["kind"] == "sol" else f"до {min(call['horizon_h'], CEX_MAX_HOLD_H) // 24} дн."
        text = (f"🏋️ <b>Сигнал проверенной стратегии</b> «{VARIANTS[active][0]}»\n"
                f"<b>{html.escape(call['asset'])}</b> — {html.escape(author or 'трейдер')}"
                + (f", согласны {f['authors']} автора" if f["authors"] >= 2 else "") + "\n"
                f"Правила с тренировки: стоп −{r['stop']:.0%}, тейк {('+' + format(r['tp'], '.0%')) if r['tp'] else 'нет'}, "
                f"держать {hold}\n"
                f"Стратегия на демо: {v['n']} сделок, в плюс {v['wins']}, в среднем {_shrunk(v['rets']):+.1f}% за сделку.\n"
                "Это демо-результат, не гарантия.")
        if call["kind"] == "sol":
            return {"text": text, "buttons": [("📈 DexScreener", f"https://dexscreener.com/solana/{call['mint']}")],
                    "mint": call["mint"]}
        return {"text": text, "buttons": [("📈 Bybit", f"https://www.bybit.com/trade/spot/{call['asset']}/USDT")]}

    # --- шаг: цены, выходы, обучение ---------------------------------------------------

    def equity(self, v: dict) -> float:
        return v["cash"] + sum(p["usd"] * (1 + net_pct(p["now"], p["kind"], p["usd"], self._cost(p["kind"])) / 100)
                               for p in v["positions"].values())

    async def _prices(self, tracks: dict) -> dict:
        coins = sorted({t["asset"] for t in tracks.values() if t["kind"] == "cex"})
        mints = sorted({t["mint"] for t in tracks.values() if t["kind"] == "sol" and t["mint"]})
        out: dict[str, float] = {}
        if coins and self.m.price_of:
            try:
                out.update({c: p for c, p in (await self.m.price_of(coins)).items() if p})
            except Exception as e:
                log.info("Тренировка: цены монет: %s", e)
        if mints:
            try:
                for mint, pair in (await self.m.copy._pairs(mints)).items():
                    if pair and pair.price_usd:
                        out[mint] = pair.price_usd
            except Exception as e:
                log.info("Тренировка: цены мемкоинов: %s", e)
        return out

    async def update(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        if not st["track"]:
            return []
        prices = await self._prices(st["track"])
        for t in st["track"].values():
            p = prices.get(t["asset"] if t["kind"] == "cex" else t["mint"])
            if p and t["price0"]:  # после срока — одна последняя точка: по ней и закрываем по сроку
                add_path(t["path"], (p / t["price0"] - 1) * 100)
        events: list[dict] = []
        closed_any = False
        for name, v in st["variants"].items():
            for pid, pos in list(v["positions"].items()):
                t = st["track"].get(pid)
                path = t["path"] if t else [pos["now"]]
                pos["now"] = path[-1]
                p, why = exit_point(path, pos["stop"], pos["tp"])
                if why == "time" and now < pos["until"]:
                    continue
                self._close(name, v, pos, p, why, now)
                closed_any = True
                if st["notify"] and name == st["active"]:
                    events.append(self._close_event(name, v["closed"][-1]))
        for pid, t in list(st["track"].items()):
            if now >= t["until"]:
                st["paths"].append({"kind": t["kind"], "path": t["path"], "asset": t["asset"], "at": t["at"]})
                del st["paths"][:-150]
                del st["track"][pid]
        if closed_any:
            events += self.learn(now)
        self.m._changed()
        return events

    def _close(self, name: str, v: dict, pos: dict, p: float, why: str, now: float) -> None:
        r = net_pct(p, pos["kind"], pos["usd"], self._cost(pos["kind"]))
        back = pos["usd"] * (1 + r / 100)
        v["cash"] = round(v["cash"] + back, 6)
        v["n"] += 1
        v["wins"] += r > 0
        v["pnl"] = round(v["pnl"] + back - pos["usd"], 6)
        v["rets"] = (v["rets"] + [round(r, 2)])[-60:]
        v["closed"].append({"asset": pos["asset"], "author": pos["author"], "kind": pos["kind"], "pct": round(r, 2),
                            "usd": round(back - pos["usd"], 4), "why": why, "at": pos["at"], "closed": now})
        del v["closed"][:-30]
        del v["positions"][pos["id"]]

    def _close_event(self, name: str, c: dict) -> dict:
        why = {"stop": "стоп", "tp": "тейк-профит", "time": "срок вышел"}[c["why"]]
        return {"text": f"🏋️ Тренировка («{VARIANTS[name][0]}»): {html.escape(c['asset'])} закрыт — {why}, "
                        f"<b>{c['pct']:+.1f}%</b> (${c['usd']:+.2f})", "buttons": []}

    def learn(self, now: float) -> list[dict]:
        events = []
        for text in (self._learn_best(now), self._learn_exits("cex", now), self._learn_exits("sol", now),
                     self._verdict(now)):
            if text:
                events.append({"text": text, "buttons": []})
        return events

    def scores(self) -> dict[str, float]:
        return {n: _shrunk(v["rets"]) for n, v in self.st["variants"].items() if len(v["rets"]) >= BEST_MIN}

    def _learn_best(self, now: float) -> str | None:
        st = self.st
        sc = self.scores()
        if not sc:
            return None
        best = max(sc, key=sc.get)
        cur = st["active"]
        if sc[best] <= 0:
            if cur:
                st["active"] = None
                return self._lesson(f"🏋️ Ни одна стратегия сейчас не в плюсе (лучшая «{VARIANTS[best][0]}»: "
                                    f"{sc[best]:+.1f}% за сделку). Сигналы тренировки выключил, пока не станет лучше.", now)
            return None
        if best == cur or (cur in sc and sc[best] - sc[cur] < BEST_MARGIN):
            return None
        st["active"] = best
        v = st["variants"][best]
        return self._lesson(f"🏋️ Лучшая стратегия на демо — «{VARIANTS[best][0]}»: {v['n']} сделок, в плюс {v['wins']}, "
                            f"в среднем {sc[best]:+.1f}% за сделку (с поправкой на везение). Теперь сигналы — только "
                            f"по ней.", now)

    def _learn_exits(self, kind: str, now: float) -> str | None:
        st = self.st
        paths = [p["path"] for p in st["paths"] if p["kind"] == kind and len(p["path"]) >= 2]
        if len(paths) < EXIT_MIN_PATHS:
            return None
        cost = self._cost(kind)
        usd = START_USD * SIZE_SHARE

        def total(stop, tp):
            return sum(net_pct(exit_point(path, stop, tp)[0], kind, usd, cost) for path in paths)

        r = st["rules"][kind]
        grid = GRID[kind]
        cur = total(r["stop"], r["tp"])
        si = min(range(len(grid["stops"])), key=lambda i: abs(grid["stops"][i] - r["stop"]))
        ti = grid["tps"].index(r["tp"]) if r["tp"] in grid["tps"] else 0
        cands = []
        for i in (si - 1, si + 1):  # один шаг за раз: стоп чуть туже или свободнее
            if 0 <= i < len(grid["stops"]):
                cands.append((grid["stops"][i], r["tp"]))
        for j in (ti - 1, ti + 1):
            if 0 <= j < len(grid["tps"]):
                cands.append((r["stop"], grid["tps"][j]))
        best = max(cands, key=lambda c: total(*c), default=None)
        if not best or total(*best) - cur < EXIT_GAIN_PCT:
            return None
        old = dict(r)
        r["stop"], r["tp"] = best
        for v in st["variants"].values():  # и для уже открытых сделок
            for pos in v["positions"].values():
                if pos["kind"] == kind:
                    pos["stop"], pos["tp"] = best
        return self._lesson(f"🏋️ Урок по выходам ({rule_text(kind, old)} → стоп −{best[0]:.0%}, тейк "
                            f"{('+' + format(best[1], '.0%')) if best[1] else 'нет'}). На {len(paths)} идеях с полным "
                            f"путём цены так было бы {total(*best):+.1f}% вместо {cur:+.1f}%.", now)

    def _verdict(self, now: float) -> str | None:
        st = self.st
        a = st["active"]
        if not a or st["verdict_sent"]:
            return None
        v = st["variants"][a]
        if v["n"] < VERDICT_MIN:
            return None
        eq = self.equity(v)
        if eq <= st["start_usd"]:
            return None
        st["verdict_sent"] = True
        return self._lesson(f"🏋️ Итог тренировки: стратегия «{VARIANTS[a][0]}» превратила ${st['start_usd']:g} в "
                            f"${eq:.2f} за {v['n']} демо-сделок. Это уже не слова блогеров, а проверка на ценах. Но "
                            f"демо не знает реального проскальзывания — с настоящими деньгами результат обычно хуже.", now)

    # --- показ -------------------------------------------------------------------------

    def status_dict(self) -> dict:
        st = self.st
        sc = self.scores()
        rows = []
        for name, v in st["variants"].items():
            eq = self.equity(v)
            rows.append({"id": name, "name": VARIANTS[name][0], "about": VARIANTS[name][1], "equity": round(eq, 2),
                         "pct": round((eq / st["start_usd"] - 1) * 100, 2) if st["start_usd"] else 0.0,
                         "n": v["n"], "wins": v["wins"], "avg": round(_shrunk(v["rets"]), 2), "open": len(v["positions"]),
                         "scored": name in sc, "active": name == st["active"]})
        rows.sort(key=lambda r: (not r["active"], -r["avg"] if r["scored"] else 1e9, -r["n"]))
        a = st["active"]
        pos = []
        if a:
            for p in st["variants"][a]["positions"].values():
                pos.append({k: p[k] for k in ("asset", "author", "kind", "usd", "at", "until")} | {"pct": round(p["now"], 2)})
        closed = list(reversed(st["variants"][a]["closed"][-8:])) if a else list(
            reversed(st["variants"]["all"]["closed"][-8:]))
        return {"on": st["on"], "signals": st["signals"], "notify": st["notify"], "start_usd": st["start_usd"],
                "active": a, "active_name": VARIANTS[a][0] if a else None, "variants": rows, "positions": pos,
                "closed": closed, "rules": {k: dict(v) for k, v in st["rules"].items()}, "ideas": st["ideas"],
                "tracking": len(st["track"]), "paths": len(st["paths"]), "best_min": BEST_MIN,
                "lessons": list(reversed(st["lessons"][-6:])), "started": st["started"]}

    def ai_context(self) -> dict:
        d = self.status_dict()
        return {"how": "/train: бот сам торгует на демо идеями трейдеров, 5 стратегий параллельно, лучшая даёт сигналы",
                "active": d["active_name"], "rules": d["rules"],
                "strategies": [{"name": r["name"], "trades": r["n"], "avg_pct": r["avg"], "equity": r["equity"]}
                               for r in d["variants"]],
                "lessons": [x["text"][:200] for x in d["lessons"][:3]]}

    def text(self) -> str:
        d = self.status_dict()
        lines = ["🏋️ <b>Тренировка на демо</b> — бот сам торгует идеями трейдеров на виртуальных деньгах",
                 f"{'🟢 тренируюсь' if d['on'] else '⏸ на паузе'} · идей {d['ideas']}, слежу за ценой {d['tracking']}",
                 f"Лучшая стратегия: <b>{html.escape(d['active_name'])}</b> — её идеи приходят сигналом" if d["active"]
                 else f"Лучшая стратегия: пока не выбрана (нужно от {BEST_MIN} сделок и результат в плюсе)",
                 "\n<b>Стратегии</b> (у каждой своё демо ${:g}):".format(d["start_usd"])]
        for r in d["variants"]:
            mark = "🏆" if r["active"] else "•"
            res = f"{r['n']} сд., в плюс {r['wins']}, {r['avg']:+.1f}%/сд." if r["n"] else "сделок пока нет"
            lines.append(f"{mark} {html.escape(r['name'])}: <b>${r['equity']:.2f}</b> ({r['pct']:+.1f}%) — {res}"
                         + (f", открыто {r['open']}" if r["open"] else ""))
        lines.append("\n🧠 Правила (подбирает сам): " + "; ".join(rule_text(k, v) for k, v in d["rules"].items()))
        if d["positions"]:
            lines.append("Открыто у лучшей: " + ", ".join(f"{html.escape(p['asset'])} {p['pct']:+.1f}%" for p in d["positions"][:6]))
        if d["lessons"]:
            lines.append("\n" + html.escape(d["lessons"][0]["text"][:300]))
        lines.append("\nЧестно: это демо — без настоящего проскальзывания и очередей; живые сделки обычно хуже на 1–3%.")
        return "\n".join(lines)

    def short_line(self) -> str:
        d = self.status_dict()
        if not d["on"] or not d["ideas"]:
            return ""
        best = next((r for r in d["variants"] if r["active"]), None)
        return ("🏋️ <b>Тренировка</b>: " + (f"лучшая «{html.escape(best['name'])}» ${best['equity']:.2f} "
                                          f"({best['pct']:+.1f}%, {best['n']} сд.)" if best else
                                          f"идей {d['ideas']}, стратегию ещё выбираю") + " — /train")
