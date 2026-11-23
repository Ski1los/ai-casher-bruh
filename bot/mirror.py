"""🪞 Повтор стримера на демо: что стример делает в эфире — то бот сразу повторяет на виртуальных деньгах.

Как устроено:
  * 🎥 бот видит действие в эфире (вошёл, долил, передвинул стоп, зафиксировал, закрыл) и повторяет его по
    НАСТОЯЩЕЙ рыночной цене в момент, когда увидел (монета с Bybit/BingX — цена биржи, мемкоин — DexScreener).
    Поэтому видна и цена опоздания: стример вошёл по 100, бот — по 100,4.
  * Два демо-счёта по $10:
      🪞 «Точная копия» — повторяет всё как есть: доливки, стопы стримера (или их отсутствие), выходы;
      🧠 «Умная копия» — те же сделки, но без ошибок: пропускает входы с ошибками (FOMO, без плана, отыгрыш,
         большое плечо) и с красными флагами токена, не доливает в минус, всегда ставит стоп.
    Разница между ними на цифрах показывает, сколько стоят ошибки стримера и стоит ли копировать его вообще.
  * Позиции без плеча (плечо стримера бот не повторяет: на демо важна идея, а не ставка). Комиссии учтены.
  * Если стример ушёл из эфира с открытой позицией — бот держит её по своим правилам: стоп, тейк, срок
    (мемкоин — сутки, монета — 3 дня).
  * Счёт каждого стримера — отдельно. Настоящие деньги не трогает никогда.
"""

from __future__ import annotations

import html
import logging
import time

log = logging.getLogger(__name__)

START_USD = 10.0
SIZE_SHARE = 0.2
MIN_TRADE = 1.0
MAX_OPEN = 5
UPDATE_SEC = 60                          # цены раз в минуту: стоп копии срабатывает почти вовремя
CEX_SIDE = 0.0015                       # спот: комиссия 0,1% + проскальзывание 0,05% в каждую сторону
STOP_DEFAULT = {"cex": 0.08, "sol": 0.3}
HOLD_H = {"cex": 72, "sol": 24}
VERDICT_MIN = 8
BOOKS = {
    "exact": ("🪞 Точная копия", "повторяет всё, как стример: доливки, его стопы (или их отсутствие), выходы"),
    "smart": ("🧠 Умная копия", "те же сделки без ошибок: пропускает FOMO, вход без плана, красные флаги, "
                               "не доливает в минус, всегда со стопом"),
}
SMART_SKIP = {"no_plan", "fomo_chase", "revenge", "overleverage", "oversize", "ignored_red_flags", "copied_call"}


def new_book() -> dict:
    return {"cash": START_USD, "positions": {}, "n": 0, "wins": 0, "pnl": 0.0, "rets": [], "closed": []}


def new_state() -> dict:
    return {"on": True, "books": {k: new_book() for k in BOOKS}, "by": {}, "seq": 0, "last_update": 0.0,
            "started": 0.0, "skipped": 0}


def stop_fraction(price0: float, stop: float | None, side: int) -> float | None:
    """Стоп стримера (цена на экране) → доля от входа. Неправдоподобные числа (например, капитализация вместо
    цены) отбрасываем."""
    if not stop or not price0:
        return None
    frac = (price0 - stop) / price0 if side > 0 else (stop - price0) / price0
    return round(frac, 4) if 0.003 <= frac <= 0.6 else None


def take_fraction(price0: float, take: float | None, side: int) -> float | None:
    if not take or not price0:
        return None
    frac = (take - price0) / price0 if side > 0 else (price0 - take) / price0
    return round(frac, 4) if 0.003 <= frac <= 20 else None


class Mirror:
    def __init__(self, live) -> None:
        self.live = live

    @property
    def m(self):
        return self.live.m

    @property
    def st(self) -> dict:
        lst = self.live.st
        st = lst.get("mirror")
        if not isinstance(st, dict):
            st = lst["mirror"] = {}
        for k, v in new_state().items():
            st.setdefault(k, v)
        for k in BOOKS:
            st["books"].setdefault(k, new_book())
        return st

    def _cost(self, kind: str) -> float:
        if kind == "cex":
            return CEX_SIDE
        try:
            return float(self.m.copy.cost_side())
        except Exception:
            return 0.012

    def net(self, pct: float, kind: str, usd: float) -> float:
        c = self._cost(kind)
        r = ((1 + pct / 100) * (1 - c) / (1 + c) - 1) * 100
        if kind == "sol":
            r -= 2 * 0.01 / max(usd, 0.5) * 100  # сеть Solana за 2 транзакции
        return r

    def equity(self, b: dict) -> float:
        return b["cash"] + sum(p["usd"] * (1 + self.net(p["now"], p["kind"], p["usd"]) / 100)
                               for p in b["positions"].values())

    async def _prices(self, items: list[tuple[str, str]]) -> dict[str, float]:
        """[(kind, key)] → {key: цена $}."""
        out: dict[str, float] = {}
        coins = sorted({k for kind, k in items if kind == "cex"})
        mints = sorted({k for kind, k in items if kind == "sol"})
        if coins and self.m.price_of:
            try:
                out.update({c: p for c, p in (await self.m.price_of(coins)).items() if p})
            except Exception as e:
                log.info("Повтор: цены монет: %s", e)
        if mints:
            try:
                for mint, pair in (await self.m.copy._pairs(mints)).items():
                    if pair and pair.price_usd:
                        out[mint] = pair.price_usd
            except Exception as e:
                log.info("Повтор: цены мемкоинов: %s", e)
        return out

    def _find(self, b: dict, login: str, key: str, asset: str) -> list[dict]:
        mine = [p for p in b["positions"].values() if p["login"] == login]
        hit = [p for p in mine if (key and p["key"] == key) or (asset and p["asset"] == asset)]
        if hit:
            return hit
        return mine if not key and not asset and len(mine) == 1 else []

    # --- действия стримера ------------------------------------------------------------------------

    async def on_obs(self, sess: dict, src: dict, obs: dict, now: float, new_action: bool,
                     prev: dict | None = None) -> list[str]:
        """Взгляд на эфир → повтор на демо. Возвращает строки для сообщения о взгляде."""
        st = self.st
        if not st["on"] or obs["confidence"] == "low":
            return []
        if not st["started"]:
            st["started"] = now
        action = obs["action"]
        login = sess["login"]
        key = obs["contract"] or obs["asset"]
        asset = obs["asset"] or (obs["contract"][:6] if obs["contract"] else "")
        lines: list[str] = []
        if new_action and action in ("enter_long", "enter_short"):
            lines += await self._enter(sess, src, obs, now)
        elif new_action and action == "add":
            lines += await self._add(login, key, asset, obs, now)
        elif new_action and action in ("take_profit", "exit_loss", "exit"):
            lines += await self._exit(login, key, asset, now, {"take_profit": "стример зафиксировал прибыль",
                                                                "exit_loss": "стример закрыл в убыток",
                                                                "exit": "стример закрыл позицию"}[action])
        elif action in ("move_stop", "set_stop"):
            lines += self._stop(login, key, asset, obs)
        elif (prev and obs["confidence"] == "high" and obs["trading"] and obs["position"]["side"] == "none"
              and action in ("watching", "talking", "none") and (prev.get("position") or {}).get("side", "none") != "none"
              and prev.get("asset") and prev.get("asset") == asset):
            lines += await self._exit(login, key, asset, now, "стример уже без позиции")
        if lines:
            self.m._changed()
        return lines

    async def _enter(self, sess: dict, src: dict, obs: dict, now: float) -> list[str]:
        st = self.st
        side = 1 if obs["action"] == "enter_long" else -1
        kind = "sol" if obs["contract"] else "cex"
        key = obs["contract"] or obs["asset"]
        if not key:
            return ["🪞 Повторить на демо не смог: не видно, какая монета."]
        if kind == "sol" and side < 0:
            return []
        price = (await self._prices([(kind, key)])).get(key)
        if not price:
            return ["🪞 Повторить на демо не смог: нет цены этой монеты на Bybit/BingX/DexScreener."]
        p = obs["position"]
        late = None
        if kind == "cex" and p.get("entry") and 0.5 < price / p["entry"] < 2:
            late = round(side * (price / p["entry"] - 1) * 100, 2)
        bad = sorted({m["tag"] for m in obs["mistakes"]} & SMART_SKIP)
        flags = obs.get("red_flags") or []
        stop_s = stop_fraction(price, p.get("stop"), side)
        take_s = take_fraction(price, p.get("take"), side)
        lines = []
        asset = obs["asset"] or (obs["contract"][:6] if obs["contract"] else "")
        for name, b in st["books"].items():
            if name not in BOOKS:
                continue
            if name == "smart" and (bad or flags):
                from .livewatch import MISTAKE_TAGS
                why = [MISTAKE_TAGS.get(t, t) for t in bad] + (["красные флаги токена"] if flags else [])
                lines.append(f"🧠 Умная копия пропустила вход: {html.escape(', '.join(why[:3]))}.")
                st["skipped"] += 1
                continue
            if self._find(b, sess["login"], key, asset):
                continue
            usd = round(max(MIN_TRADE, self.equity(b) * SIZE_SHARE), 2)
            if len(b["positions"]) >= MAX_OPEN or b["cash"] < usd:
                if name == "exact":
                    lines.append("🪞 Точная копия: денег на демо мало или уже 5 позиций — вход пропущен.")
                continue
            stop = stop_s if name == "exact" else (stop_s or STOP_DEFAULT[kind])
            st["seq"] += 1
            pid = f"m{st['seq']}"
            b["cash"] = round(b["cash"] - usd, 6)
            b["positions"][pid] = {"id": pid, "login": sess["login"], "name": src["name"], "key": key, "asset": asset,
                                   "kind": kind, "side": side, "usd": usd, "units": usd / price, "price0": price,
                                   "now": 0.0, "path": [0.0], "stop": stop, "tp": take_s, "at": now,
                                   "until": now + HOLD_H[kind] * 3600, "late": late, "adds": 0,
                                   "tags": [m["tag"] for m in obs["mistakes"]]}
        side_txt = "LONG" if side > 0 else "SHORT"
        opened = [BOOKS[n][0] for n, b in st["books"].items() if n in BOOKS and any(
            q["login"] == sess["login"] and q["key"] == key and q["at"] == now for q in b["positions"].values())]
        if opened:
            from .livewatch import _price
            head = (f"🪞 Повторил на демо ({', '.join(opened)}): {side_txt} <b>{html.escape(asset)}</b> по {_price(price)}"
                    + (f" — стример вошёл по {_price(p['entry'])}, опоздание стоило {late:+.2f}%" if late is not None
                       else "") + ".")
            lines.insert(0, head)
        return lines

    async def _add(self, login: str, key: str, asset: str, obs: dict, now: float) -> list[str]:
        st = self.st
        items = [(n, p) for n, b in st["books"].items() for p in self._find(b, login, key, asset)]
        if not items:
            return []
        prices = await self._prices([(p["kind"], p["key"]) for _, p in items])
        lines = []
        for name, p in items:
            price = prices.get(p["key"])
            b = st["books"][name]
            if not price or p["adds"] >= 2:
                continue
            pnl = p["side"] * (price / p["price0"] - 1) * 100
            if name == "smart" and pnl < 0:
                lines.append(f"🧠 Умная копия не доливает в минус ({pnl:+.1f}%).")
                continue
            add = round(min(p["usd"] * 0.5, b["cash"]), 2)
            if add < MIN_TRADE / 2:
                continue
            b["cash"] = round(b["cash"] - add, 6)
            p["units"] += add / price
            p["usd"] = round(p["usd"] + add, 4)
            p["price0"] = p["usd"] / p["units"]
            p["adds"] += 1
            if name == "exact":
                lines.append(f"🪞 Долил на демо ${add:g}, как стример (средняя цена {p['price0']:.6g}).")
        return lines

    def _stop(self, login: str, key: str, asset: str, obs: dict) -> list[str]:
        st = self.st
        stop = obs["position"].get("stop")
        out = []
        for name, b in st["books"].items():
            for p in self._find(b, login, key, asset):
                frac = stop_fraction(p["price0"], stop, p["side"])
                if frac is None:
                    continue
                if name == "smart" and p["stop"] and frac > p["stop"]:
                    out.append("🧠 Умная копия не отодвигает стоп дальше от цены.")
                    continue
                p["stop"] = frac
        return out

    async def _exit(self, login: str, key: str, asset: str, now: float, why: str) -> list[str]:
        st = self.st
        items = [(n, p) for n, b in st["books"].items() for p in self._find(b, login, key, asset)]
        if not items:
            return []
        prices = await self._prices([(p["kind"], p["key"]) for _, p in items])
        lines = []
        for name, p in items:
            price = prices.get(p["key"])
            if not price:
                continue
            pct = p["side"] * (price / p["price0"] - 1) * 100
            c = self._close(name, p, pct, why, now)
            lines.append(f"{BOOKS[name][0]}: закрыл {html.escape(p['asset'])} вслед за стримером — <b>{c['pct']:+.1f}%</b> "
                         f"(${c['usd']:+.2f}).")
        return lines

    # --- цены, стопы, сроки -------------------------------------------------------------------------

    async def update(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        if now - st["last_update"] < UPDATE_SEC:
            return []
        st["last_update"] = now
        items = [(n, p) for n, b in st["books"].items() for p in b["positions"].values()]
        if not items:
            return []
        from .copytrade import add_path
        prices = await self._prices([(p["kind"], p["key"]) for _, p in items])
        events = []
        for name, p in items:
            price = prices.get(p["key"])
            if price:
                p["now"] = round(p["side"] * (price / p["price0"] - 1) * 100, 2)
                add_path(p["path"], p["now"])
            why = None
            if p["stop"] and p["now"] <= -p["stop"] * 100:
                why = f"стоп −{p['stop']:.1%}"
            elif p["tp"] and p["now"] >= p["tp"] * 100:
                why = f"тейк +{p['tp']:.1%}"
            elif now >= p["until"]:
                why = f"срок {HOLD_H[p['kind']]} ч"
            if why:
                c = self._close(name, p, p["now"], why, now)
                events.append({"text": f"{BOOKS[name][0]} (повтор {html.escape(p['name'])}): {html.escape(p['asset'])} "
                                       f"закрыт — {why}, <b>{c['pct']:+.1f}%</b> (${c['usd']:+.2f}). Демо-счёт "
                                       f"${self.equity(st['books'][name]):.2f}.", "buttons": []})
        self.m._changed()
        return events

    def _close(self, name: str, p: dict, pct: float, why: str, now: float) -> dict:
        st = self.st
        b = st["books"][name]
        r = self.net(pct, p["kind"], p["usd"])
        back = p["usd"] * (1 + r / 100)
        b["cash"] = round(b["cash"] + back, 6)
        b["n"] += 1
        b["wins"] += r > 0
        b["pnl"] = round(b["pnl"] + back - p["usd"], 6)
        b["rets"] = (b["rets"] + [round(r, 2)])[-60:]
        c = {"asset": p["asset"], "login": p["login"], "name": p["name"], "kind": p["kind"], "side": p["side"],
             "pct": round(r, 2), "usd": round(back - p["usd"], 4), "why": why, "at": p["at"], "closed": now,
             "late": p.get("late")}
        b["closed"] = (b["closed"] + [c])[-30:]
        by = st["by"].setdefault(p["login"], {"name": p["name"], "exact": [], "smart": []})
        by[name] = (by.get(name, []) + [round(r, 2)])[-40:]
        b["positions"].pop(p["id"], None)
        return c

    # --- вид ------------------------------------------------------------------------------------------

    def status_dict(self) -> dict:
        from .profit import edge_stats
        st = self.st
        books = []
        for name, b in st["books"].items():
            if name not in BOOKS:
                continue
            e = edge_stats(b["rets"])
            books.append({"id": name, "title": BOOKS[name][0], "how": BOOKS[name][1], "n": b["n"], "wins": b["wins"],
                          "equity": round(self.equity(b), 2), "pnl": round(b["pnl"], 2), "verdict": e["verdict"],
                          "avg": round(e["mean"], 2) if b["rets"] else None,
                          "open": [{"asset": p["asset"], "name": p["name"], "side": p["side"], "now": p["now"]}
                                   for p in b["positions"].values()][:6]})
        streamers = []
        for login, v in st["by"].items():
            ex, sm = v.get("exact") or [], v.get("smart") or []
            if not ex and not sm:
                continue
            e = edge_stats(ex)
            streamers.append({"login": login, "name": v.get("name") or login, "n": len(ex),
                              "avg": round(sum(ex) / len(ex), 2) if ex else None,
                              "smart_n": len(sm), "smart_avg": round(sum(sm) / len(sm), 2) if sm else None,
                              "verdict": e["verdict"] if len(ex) >= VERDICT_MIN else "data"})
        streamers.sort(key=lambda s: -(s["n"] + s["smart_n"]))
        lates = [c["late"] for b in st["books"].values() for c in b["closed"] if c.get("late") is not None]
        return {"on": st["on"], "books": books, "streamers": streamers[:8], "skipped": st["skipped"],
                "late_avg": round(sum(lates) / len(lates), 2) if lates else None}

    def text(self) -> str:
        d = self.status_dict()
        icons = {"proven": "✅", "promising": "🟡", "flat": "⚪", "losing": "🔴", "data": "⏳", "none": "▫️"}
        lines = ["🪞 <b>Повтор стримеров на демо</b> — что стример делает в эфире, бот сразу повторяет на виртуальных "
                 f"$10 по настоящей цене · {'🟢 включено' if d['on'] else '⏸ выключено'}"]
        for b in d["books"]:
            row = f"\n<b>{b['title']}</b> — {html.escape(b['how'])}\n"
            row += (f"{icons.get(b['verdict'], '•')} сделок {b['n']}, в плюс {b['wins']}, в среднем {b['avg']:+.1f}%, "
                    f"счёт ${b['equity']:.2f}" if b["n"] else f"сделок ещё не было · счёт ${b['equity']:.2f}")
            if b["open"]:
                row += "\nОткрыто: " + ", ".join(f"{'🟢' if o['side'] > 0 else '🔴'} {html.escape(o['asset'])} "
                                                 f"{o['now']:+.1f}% ({html.escape(o['name'])})" for o in b["open"])
            lines.append(row)
        if d["streamers"]:
            lines.append("\n<b>Копировать ли стримера</b> (точная / умная копия):")
            for s in d["streamers"]:
                ex = f"{s['n']} сд., {s['avg']:+.1f}%" if s["n"] else "—"
                sm = f"{s['smart_n']} сд., {s['smart_avg']:+.1f}%" if s["smart_n"] else "—"
                lines.append(f"{icons.get(s['verdict'], '•')} <b>{html.escape(s['name'])}</b>: {ex} / {sm}")
            lines.append(f"<i>Вердикт — от {VERDICT_MIN} сделок. Если умная копия заметно лучше точной — ошибки стримера "
                         "стоят денег, повторять стоит только его хорошие сделки.</i>")
        if d["late_avg"] is not None:
            lines.append(f"⏱ Опоздание (бот видит эфир с задержкой ~30–60 с) стоит в среднем {d['late_avg']:+.2f}% на вход.")
        if d["skipped"]:
            lines.append(f"🧠 Умная копия пропустила входов с ошибками: {d['skipped']}.")
        lines.append("Плечо стримера бот не повторяет, комиссии учтены, деньги виртуальные.")
        return "\n".join(lines)


__all__ = ["Mirror", "BOOKS", "new_state", "stop_fraction", "take_fraction", "SMART_SKIP"]
