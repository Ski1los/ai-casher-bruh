"""💰 Профит-центр (/profit): где бот на самом деле зарабатывает, а где теряет. Считаем по фактам, а не по надеждам.

Что делает:
  1. Собирает результаты ВСЕХ способов заработка в одну таблицу: автопилот (реальные и тестовые сделки),
     лаборатория, копитрейдинг, мемкоины (демо и реальные), тренировка на идеях трейдеров, арбитраж на демо.
  2. Для каждого способа считает среднюю сделку после поправки на реальность (демо не знает очередей
     и проскальзывания), разброс и честный вердикт:
       ✅ доказан плюс — даже с поправкой на невезение средняя сделка в плюсе (от 15 сделок);
       🟡 в плюсе, но это может быть везение;  ⚪ около нуля;  🔴 доказанный минус;  ⏳ мало данных.
  3. План денег: куда положить $5 / $20 / $50 / $100, чтобы деньги работали там, где плюс доказан.
     Доли считаются по «четверти Келли» (известная формула размера ставки, урезанная в 4 раза ради
     безопасности) с ограничениями и минимальными ордерами бирж. Остаток — в проценты Bybit Earn.
  4. 🛡 Страж прибыли: если способ с НАСТОЯЩИМИ деньгами доказанно теряет на последних сделках — ставит
     новые покупки на паузу на 3 дня (открытые позиции бот продолжает вести). Если демо доказало минус —
     выключает его сигналы, чтобы ты не повторял убыточные сделки руками. Всё возвращается одной кнопкой.
  5. Сообщает, когда способ доказал плюс (можно переходить на реальные деньги) и когда пора добавить денег.

Честно: прошлые результаты не гарантируют будущих. Зато так деньги не идут туда, где бот уже доказал минус.
"""

from __future__ import annotations

import html
import logging
import math
import time

log = logging.getLogger(__name__)

WINDOW = 40                # судим по последним 40 сделкам: рынок меняется
MIN_N = 8                  # меньше — «мало данных»
PROVEN_N = 15              # «доказан плюс» — не меньше 15 сделок
PRIOR = 5                  # осторожная средняя: сумма / (n + 5) — как будто было ещё 5 нулевых сделок
HAIRCUT = {"cex": 0.1, "sol": 2.0}  # поправка на реальность для демо, % за сделку
KELLY_SHARE = 0.25         # четверть Келли
CAP = {"proven": 0.6, "promising": 0.25}  # не больше такой доли денег в один способ
GUARD_WINDOW = 15          # Страж смотрит последние 15 настоящих сделок…
GUARD_MIN = 8              # …от 8 сделок
GUARD_PAUSE_SEC = 3 * 86400
GUARD_RETRIGGER = 5        # после снятия паузы снова сработает не раньше чем через 5 новых сделок
SCALE_STEP = 20            # «можно удвоить сумму» — каждые 20 настоящих сделок с доказанным плюсом
PLAN_AMOUNTS = (5, 20, 50, 100)
EARN_FALLBACK_APR = 0.0

VERDICTS = {
    "proven": ("✅", "доказан плюс"),
    "promising": ("🟡", "в плюсе, но может быть везение"),
    "flat": ("⚪", "около нуля"),
    "losing": ("🔴", "доказанный минус"),
    "data": ("⏳", "мало данных"),
    "none": ("▫️", "сделок ещё не было"),
}
ORDER = {"proven": 0, "promising": 1, "flat": 2, "data": 3, "losing": 4, "none": 5}

# способы с настоящими деньгами, куда можно положить деньги: id → (название, команда, минимум $, рынок)
ROUTES = {
    "pilot": ("🤖 Автопилот на Bybit/BingX", "/autopilot", 5.0, "cex"),
    "meme": ("💸 Мемкоины ботом (Phantom)", "/meme", 3.0, "sol"),
    "tri": ("🔺 Треугольники", "/trade", 15.0, "cex"),
}


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def edge_stats(rets: list[float], haircut: float = 0.0) -> dict:
    """Средняя сделка (%) с поправкой, разброс и вердикт. Ничего не знает о том, откуда сделки."""
    rets = [float(r) for r in rets][-WINDOW:]
    n = len(rets)
    if not n:
        return {"n": 0, "verdict": "none", "mean": 0.0, "shrunk": 0.0, "lcb": 0.0, "ucb": 0.0, "sd": 0.0,
                "wins": 0, "pf": None, "sum": 0.0}
    adj = [r - haircut for r in rets]
    mean = sum(adj) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in adj) / (n - 1)) if n > 1 else max(abs(mean), 1.0)
    se = sd / math.sqrt(n)
    lcb, ucb = mean - se, mean + se
    gains = sum(r for r in adj if r > 0)
    losses = -sum(r for r in adj if r < 0)
    pf = round(gains / losses, 2) if losses > 0 else (None if not gains else 99.0)
    if n < MIN_N:
        verdict = "data"
    elif ucb < 0:
        verdict = "losing"
    elif lcb > 0 and n >= PROVEN_N:
        verdict = "proven"
    elif mean > 0 and lcb > -se * 0.5:
        verdict = "promising"
    else:
        verdict = "flat"
    return {"n": n, "verdict": verdict, "mean": round(mean, 3), "shrunk": round(sum(adj) / (n + PRIOR), 3),
            "lcb": round(lcb, 3), "ucb": round(ucb, 3), "sd": round(sd, 3), "wins": sum(r > 0 for r in adj),
            "pf": pf, "sum": round(sum(adj), 2)}


def kelly_fraction(mean_pct: float, sd_pct: float) -> float:
    """Доля денег по формуле Келли для сделки со средним mean и разбросом sd (оба в %), урезанная в 4 раза."""
    if mean_pct <= 0 or sd_pct <= 0:
        return 0.0
    return max(0.0, (mean_pct / 100) / (sd_pct / 100) ** 2 * KELLY_SHARE)


def grow(amount: float, day_pct: float, days: int) -> float:
    return amount * (1 + day_pct / 100) ** days


# ---------------------------------------------------------------------------
# Сбор результатов из всех частей бота
# ---------------------------------------------------------------------------

def _days(first: float | None, now: float) -> float:
    return max(1.0, (now - first) / 86400) if first else 1.0


def _engine(eid: str, icon: str, title: str, kind: str, market: str, cmd: str, rets: list[float], first: float | None,
            now: float, route: str | None, exposure: float, max_per_day: float, on: bool, note: str = "") -> dict:
    haircut = HAIRCUT[market] if kind != "real" else 0.0
    st = edge_stats(rets, haircut)
    days = _days(first, now)
    per_day = min(len(rets) / days, max_per_day) if rets else 0.0
    # доход на вложенные в способ деньги в день: осторожная средняя × сделок в день × доля денег в сделке
    st.update(id=eid, icon=icon, title=title, kind=kind, market=market, cmd=cmd, route=route, on=on, note=note,
              days=round(days, 1), per_day=round(per_day, 2), exposure=exposure, haircut=haircut,
              day_pct=round(st["shrunk"] * per_day * exposure, 3),
              day_pct_low=round(min(st["lcb"], st["shrunk"]) * per_day * exposure, 3))
    return st


def _scalp_trades(b, live: bool) -> tuple[list[float], float | None]:
    rets, first = [], None
    for r in b.trader.state.log:
        if r.get("source") != "scalp" or r.get("kind") != "sell" or bool(r.get("live")) != live:
            continue
        pnl = r.get("pnl_usd")
        cost = (r.get("got") or 0) - (pnl or 0)
        if pnl is None or cost <= 0:
            continue
        rets.append(pnl / cost * 100)
        first = first or r.get("at")
    return rets, first


def _part(fn, name: str):
    try:
        return fn()
    except Exception as e:  # одна сломанная часть не должна ломать весь отчёт
        log.info("Профит-центр, %s: %s", name, e)
        return None


def collect(b, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    out: list[dict] = []

    def pilot_real():
        rets, first = _scalp_trades(b, True)
        if rets or (b.scalper.on and b.trader.state.live):
            out.append(_engine("pilot_real", "🤖", "Автопилот — настоящие деньги", "real", "cex", "/scalp", rets, first,
                               now, "pilot", 1.0, 4, b.scalper.on and b.trader.state.live))

    def pilot_test():
        rets, first = _scalp_trades(b, False)
        if rets:
            out.append(_engine("pilot_test", "🤖", "Автопилот — тест без денег", "demo", "cex", "/scalp", rets, first,
                               now, "pilot", 1.0, 4, b.scalper.on and not b.trader.state.live))

    def lab():
        stats = b.scalper.lab["stats"]
        rets = [r for s in stats.values() for r in s.get("rets", [])]
        first = min((s.get("since") or now for s in stats.values()), default=None)
        if rets:
            out.append(_engine("lab", "🧪", "Лаборатория (стратегии автопилота на живых свечах)", "demo", "cex", "/lab",
                               rets, first, now, "pilot", 1.0, 4, True,
                               "автопилот берёт одну сделку за раз — поэтому считаю не больше 4 сделок в день"))

    def copy():
        closed = [t for t in b.copy.st["closed"] if t.get("passed")]
        if closed:
            out.append(_engine("copy", "🐋", "Копитрейдинг умных кошельков (без денег)", "demo", "sol", "/copy",
                               [t["ret"] for t in closed], closed[0].get("closed"), now, "meme", 0.2, 10,
                               b.copy.on))

    def meme_demo():
        d = b.meme.demo
        closed = d.get("closed") or []
        if closed:
            out.append(_engine("meme_demo", "🧪", "Мемкоины — демо-счёт", "demo", "sol", "/meme",
                               [c["pct"] for c in closed if c.get("pct") is not None], closed[0].get("closed"), now,
                               "meme", 0.25, 10, b.meme.demo_on))

    def meme_real():
        closed = [c for c in b.meme.st.get("closed") or [] if c.get("pnl_usd") is not None]
        if closed or b.meme.on:
            out.append(_engine("meme_real", "💸", "Мемкоины — настоящие деньги", "real", "sol", "/meme",
                               [c["pct"] for c in closed], closed[0].get("closed") if closed else None, now, "meme",
                               0.25, 10, b.meme.on))

    def train():
        tr = b.mentors.train
        st = tr.st
        name = st["active"] or max(st["variants"], key=lambda k: st["variants"][k]["n"])
        v = st["variants"][name]
        if v["rets"]:
            from .training import VARIANTS
            closed = v.get("closed") or []
            out.append(_engine("train", "🏋️", f"Тренировка на идеях трейдеров («{VARIANTS[name][0]}»)", "demo",
                               "cex", "/train", v["rets"], st["started"] or (closed[0]["closed"] if closed else None),
                               now, None, 0.25, 6,
                               bool(st["on"]), "сигналы — сделки руками или копирование вручную"))

    def arb():
        rets, first = [], None
        accounts = b.demo.accounts
        for acc in (accounts.values() if isinstance(accounts, dict) else accounts):
            for t in acc.trades:
                if t.closed_at and t.cost_usd:
                    rets.append(t.profit_usd / t.cost_usd * 100)
                    first = min(first or t.closed_at, t.closed_at)
        if rets:
            out.append(_engine("arb", "🔺", "Арбитраж — демо-счета", "demo", "cex", "/demo", rets, first, now, "tri",
                               1.0, 20, b.demo.active, "в проверке уже учтены настоящие комиссии и стаканы"))

    for name, fn in (("автопилот", pilot_real), ("тест", pilot_test), ("лаборатория", lab), ("копитрейдинг", copy),
                     ("мемы демо", meme_demo), ("мемы", meme_real), ("тренировка", train), ("арбитраж", arb)):
        _part(fn, name)
    out.sort(key=lambda e: (ORDER[e["verdict"]], -e["day_pct"], -e["n"]))
    return out


def earn_apr(b) -> tuple[float, str]:
    """Лучший процент Bybit Earn на стейблкоин (без риска цены) из последнего снимка /earn."""
    snap = getattr(b, "opps_last", None) or {}
    rows = [r for r in snap.get("earn") or [] if r.get("coin") in ("USDT", "USDC")]
    if not rows:
        return EARN_FALLBACK_APR, ""
    r = max(rows, key=lambda r: r["apr"])
    return float(r["apr"]), r["coin"]


# ---------------------------------------------------------------------------
# План денег
# ---------------------------------------------------------------------------

def route_evidence(engines: list[dict]) -> dict[str, dict]:
    """Лучшее доказательство для каждого способа с настоящими деньгами. Настоящие сделки (от 8) важнее демо."""
    best: dict[str, dict] = {}
    for e in engines:
        r = e["route"]
        if not r:
            continue
        cur = best.get(r)
        real_ok = e["kind"] == "real" and e["n"] >= MIN_N
        if cur is None:
            best[r] = e
        elif real_ok and not (cur["kind"] == "real" and cur["n"] >= MIN_N):
            best[r] = e
        elif not (cur["kind"] == "real" and cur["n"] >= MIN_N) and (
                ORDER[e["verdict"]], -e["day_pct"]) < (ORDER[cur["verdict"]], -cur["day_pct"]):
            best[r] = e
    return best


def plan(engines: list[dict], amount: float, apr: float = 0.0) -> dict:
    """Куда положить amount долларов. → {rows: [{route, title, usd, why}], day_pct, low_day_pct, honest}."""
    ev = route_evidence(engines)
    cands = []
    for r, e in ev.items():
        if e["verdict"] not in ("proven", "promising") or e["day_pct"] <= 0:
            continue
        frac = min(kelly_fraction(e["mean"], e["sd"]), CAP[e["verdict"]])
        if frac > 0:
            cands.append((r, e, frac))
    cands.sort(key=lambda x: (ORDER[x[1]["verdict"]], -x[1]["day_pct"]))
    rows, left = [], float(amount)
    for r, e, frac in cands:
        need = ROUTES[r][2]
        # плюс доказан только на демо/тесте → сначала минимальная сумма; по Келли — когда подтвердят настоящие сделки
        demo_only = not (e["kind"] == "real" and e["n"] >= MIN_N)
        usd = need if demo_only else max(amount * frac, need)
        if usd > left + 1e-9:
            # мало денег: лучший доказанный способ получает всё, если хватает на минимальный ордер
            if not rows and left >= need and e["verdict"] == "proven":
                usd = left
            else:
                continue
        rows.append({"route": r, "title": ROUTES[r][0], "cmd": ROUTES[r][1], "usd": round(usd, 2), "verdict": e["verdict"],
                     "why": f"{VERDICTS[e['verdict']][0]} {e['title']}: {e['n']} сд., {e['mean']:+.2f}%/сд."
                            + (" Пока плюс только без денег — начни с минимума, больше после 8 настоящих сделок в плюсе."
                               if demo_only else ""),
                     "day_pct": e["day_pct"], "day_pct_low": e["day_pct_low"]})
        left -= usd
    if left > 0.009:
        rows.append({"route": "earn", "title": "💰 Bybit Earn (проценты за хранение USDT)", "cmd": "/earn",
                     "usd": round(left, 2), "verdict": "earn", "day_pct": apr / 365, "day_pct_low": apr / 365,
                     "why": (f"{apr:.1f}% годовых без риска цены" if apr else "проценты без риска цены")
                     + ("" if rows else " — пока ничего не доказало плюс, деньги ждут здесь")
                     + (" — запас: в один способ не больше, чем он доказал" if rows else "")})
    total = sum(x["usd"] for x in rows) or 1
    day = sum(x["usd"] * x["day_pct"] for x in rows) / total
    low = sum(x["usd"] * x["day_pct_low"] for x in rows) / total
    proven = any(x["verdict"] == "proven" for x in rows)
    return {"amount": amount, "rows": rows, "day_pct": round(day, 3), "low_day_pct": round(low, 3), "proven": proven,
            "d30": round(grow(amount, day, 30), 2), "d90": round(grow(amount, day, 90), 2),
            "low30": round(grow(amount, low, 30), 2)}


def scale_hint(engines: list[dict]) -> str | None:
    """Когда добавить денег: настоящие сделки доказали плюс на каждых 20 сделках подряд."""
    for e in engines:
        if e["kind"] == "real" and e["verdict"] == "proven" and e["n"] >= SCALE_STEP:
            return (f"📈 <b>{html.escape(e['title'])}</b>: {e['n']} настоящих сделок, плюс доказан "
                    f"({e['mean']:+.2f}%/сд., даже с поправкой на невезение {e['lcb']:+.2f}%). Можно увеличить сумму — "
                    "но не больше чем в 2 раза за раз и только деньгами, которые не жалко.")
    return None


# ---------------------------------------------------------------------------
# Страж прибыли
# ---------------------------------------------------------------------------

def new_state() -> dict:
    return {"guard": True, "verdicts": {}, "paused": {}, "muted": {}, "events": [], "amount": 5.0}


class ProfitCenter:
    def __init__(self, bot) -> None:
        self.b = bot

    @property
    def st(self) -> dict:
        cs = self.b.copy.st
        st = cs.get("profit")
        if not isinstance(st, dict):
            st = cs["profit"] = {}
        for k, v in new_state().items():
            st.setdefault(k, v)
        return st

    def _changed(self) -> None:
        self.b.copy.changes += 1

    def _log(self, text: str, now: float) -> None:
        self.st["events"].append({"at": now, "text": text[:300]})
        del self.st["events"][:-12]

    def engines(self, now: float | None = None) -> list[dict]:
        return collect(self.b, now)

    # --- проверка: Страж + новости о вердиктах ------------------------------------------------

    def check(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        st = self.st
        engines = self.engines(now)
        events: list[dict] = []
        if st["guard"]:
            events += self._guard(engines, now)
        for e in engines:
            old = st["verdicts"].get(e["id"])
            new = e["verdict"]
            if old == new:
                continue
            st["verdicts"][e["id"]] = new
            self._changed()
            if old is None and new not in ("proven", "losing"):
                continue  # первое знакомство — молча
            if new == "proven":
                route = ROUTES.get(e["route"] or "")
                extra = (f" Значит, можно попробовать настоящие деньги: {route[0]} ({route[1]}), начиная с минимума "
                         f"${route[2]:g}." if e["kind"] != "real" and route else
                         " Деньги работают правильно." if e["kind"] == "real" else "")
                text = (f"✅ <b>{html.escape(e['title'])}</b> доказал плюс: {e['n']} сделок, в среднем "
                        f"{e['mean']:+.2f}% за сделку" + (" с поправкой на реальность" if e["haircut"] else "") +
                        f" (даже при невезении {e['lcb']:+.2f}%).{extra}")
                events.append({"text": text, "buttons": [("💰 Профит-центр", "pf:show")]})
                self._log(text, now)
            elif new == "losing" and e["kind"] != "real":
                text = (f"🔴 <b>{html.escape(e['title'])}</b>: демо доказало минус ({e['n']} сделок, "
                        f"{e['mean']:+.2f}% за сделку). Настоящими деньгами это не включай, пока бот не подстроится.")
                events.append({"text": text, "buttons": [("💰 Профит-центр", "pf:show")]})
                self._log(text, now)
        return events

    def _guard(self, engines: list[dict], now: float) -> list[dict]:
        st = self.st
        events = []
        by_id = {e["id"]: e for e in engines}
        for eid, route in (("pilot_real", "pilot"), ("meme_real", "meme")):
            e = by_id.get(eid)
            if not e:
                continue
            rets = self._real_rets(eid)
            recent = edge_stats(rets[-GUARD_WINDOW:])
            total = len(rets)
            mark = st["paused"].get(route)
            if mark and total < mark["n"] + GUARD_RETRIGGER:
                continue
            if recent["n"] >= GUARD_MIN and recent["ucb"] < 0 and recent["sum"] < 0:
                self._pause(route, now)
                st["paused"][route] = {"n": total, "at": now, "until": now + GUARD_PAUSE_SEC}
                self._changed()
                text = (f"🛡 <b>Страж прибыли</b>: {html.escape(ROUTES[route][0])} доказанно теряет на последних "
                        f"{recent['n']} настоящих сделках (итог {recent['sum']:+.1f}%, в плюс {recent['wins']}). "
                        "Новые покупки на паузе на 3 дня, открытые позиции бот ведёт дальше. Пока пауза — "
                        "лаборатория и демо продолжают учиться. Снять паузу можно кнопкой.")
                events.append({"text": text, "buttons": [("▶️ Снять паузу", f"pf:resume:{route}"),
                                                          ("💰 Профит-центр", "pf:show")]})
                self._log(text, now)
        e = by_id.get("copy")
        if e and e["verdict"] == "losing" and self.b.copy.st.get("signals") and not st["muted"].get("copy"):
            self.b.copy.st["signals"] = False
            st["muted"]["copy"] = {"at": now, "n": e["n"]}
            self._changed()
            text = (f"🛡 <b>Страж прибыли</b>: копитрейдинг на демо доказал минус ({e['n']} сделок, "
                    f"{e['mean']:+.2f}% за сделку после поправки). Выключил его сигналы, чтобы ты не повторял убыточные "
                    "сделки руками. Демо продолжает учиться — когда выправится, скажу.")
            events.append({"text": text, "buttons": [("🔔 Вернуть сигналы", "pf:unmute:copy")]})
            self._log(text, now)
        return events

    def _real_rets(self, eid: str) -> list[float]:
        if eid == "pilot_real":
            return _scalp_trades(self.b, True)[0]
        return [c["pct"] for c in self.b.meme.st.get("closed") or [] if c.get("pnl_usd") is not None]

    def _pause(self, route: str, now: float) -> None:
        until = now + GUARD_PAUSE_SEC
        if route == "pilot":
            sc = self.b.scalper.st
            for name in self.b.scalper.exchanges() or list(self.b.trader.scanner.exchanges):
                sc["pause"][name] = max(float(sc["pause"].get(name) or 0), until)
                sc.setdefault("pause_why", {})[name] = "Страж прибыли: автопилот доказанно теряет — пауза новых покупок"
        elif route == "meme":
            self.b.meme.st["paused_until"] = max(float(self.b.meme.st.get("paused_until") or 0), until)

    def resume(self, route: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        mark = self.st["paused"].get(route)
        if route == "pilot":
            sc = self.b.scalper.st
            for name in list(sc["pause"]):
                sc["pause"][name] = 0.0
            sc.get("pause_why", {}).clear()
        elif route == "meme":
            self.b.meme.st["paused_until"] = 0.0
        if mark:
            mark["until"] = now  # «n» остаётся: снова сработает только после новых сделок
        self._changed()
        return f"▶️ Пауза снята: {ROUTES[route][0]}. Страж снова проверит после {GUARD_RETRIGGER} новых сделок."

    def unmute(self, what: str) -> str:
        if what == "copy":
            self.b.copy.st["signals"] = True
        self.st["muted"].pop(what, None)
        self._changed()
        return "🔔 Сигналы копитрейдинга снова включены."

    # --- показ ---------------------------------------------------------------------------------

    def status_dict(self, amount: float | None = None, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        engines = self.engines(now)
        apr, coin = earn_apr(self.b)
        amount = float(amount or self.st["amount"] or 5)
        p = plan(engines, amount, apr)
        paused = {r: m for r, m in self.st["paused"].items() if m.get("until", 0) > now}
        return {"engines": engines, "plan": p, "amounts": list(PLAN_AMOUNTS), "guard": bool(self.st["guard"]),
                "paused": paused, "muted": dict(self.st["muted"]), "earn_apr": apr, "earn_coin": coin,
                "scale": scale_hint(engines), "events": list(reversed(self.st["events"][-6:])),
                "verdicts": {k: {"icon": v[0], "text": v[1]} for k, v in VERDICTS.items()}}

    def ai_context(self) -> dict:
        d = self.status_dict()
        return {"how": "/profit: результаты всех способов заработка по фактам, вердикт с поправкой на реальность "
                       "и невезение, план денег (четверть Келли), Страж ставит на паузу то, что доказанно теряет",
                "engines": [{k: e[k] for k in ("title", "kind", "n", "verdict", "mean", "lcb", "wins", "per_day",
                                               "day_pct")} for e in d["engines"]],
                "plan_for_usd": d["plan"]["amount"],
                "plan": [{"to": r["title"], "usd": r["usd"]} for r in d["plan"]["rows"]],
                "plan_day_pct": d["plan"]["day_pct"], "guard_on": d["guard"], "paused": list(d["paused"])}

    def text(self, amount: float | None = None) -> str:
        d = self.status_dict(amount)
        lines = ["💰 <b>Профит-центр</b> — где бот зарабатывает по фактам, а где теряет"]
        if not d["engines"]:
            lines.append("\nСделок пока нет ни у одного способа. Включи демо и обучение — через 1–3 дня здесь будет "
                         "видно, что приносит деньги: /meme (демо мемкоинов), /copy, /train, /autopilot (тест).")
        else:
            lines.append("")
            for e in d["engines"]:
                icon, vt = VERDICTS[e["verdict"]]
                res = (f"{e['n']} сд., в плюс {e['wins']}, <b>{e['mean']:+.2f}%</b>/сд." if e["n"] else "сделок нет")
                if e["n"] >= MIN_N:
                    res += f" (при невезении {e['lcb']:+.2f}%)"
                    if e["verdict"] == "proven" and e["day_pct"]:  # «в день» — только для доказанного
                        res += f", ≈{e['day_pct']:+.2f}%/день"
                lines.append(f"{icon} {e['icon']} {html.escape(e['title'])}: {res} — {vt} · {e['cmd']}")
            lines.append("<i>Демо уже с поправкой на реальность: −0,1% за сделку на бирже, −2% на мемкоинах.</i>")
        p = d["plan"]
        lines.append(f"\n🧭 <b>План для ${p['amount']:g}</b>:")
        for r in p["rows"]:
            lines.append(f"• {html.escape(r['title'])}: <b>${r['usd']:g}</b> — {html.escape(r['why'])}")
        if p["proven"] and p["day_pct"] > 0:
            lines.append(f"📈 Если результаты сохранятся: за 30 дней ${p['amount']:g} → <b>${p['d30']:g}</b>, за 90 → "
                         f"${p['d90']:g}. При невезении за 30 дней → ${p['low30']:g}.")
        else:
            lines.append("Доказанного плюса пока нет, поэтому прогноз роста не рисую. Сейчас главный «профит» — "
                         "не терять: деньги ждут, бот учится на демо. Как только что-то докажет плюс — напишу.")
        if d["scale"]:
            lines.append("\n" + d["scale"])
        g = "🛡 Страж прибыли: <b>вкл</b> — ставит на паузу то, что доказанно теряет" if d["guard"] else \
            "🛡 Страж прибыли: выкл"
        lines.append("\n" + g)
        for r, m in d["paused"].items():
            lines.append(f"⏸ {html.escape(ROUTES[r][0])}: пауза до {time.strftime('%d.%m %H:%M', time.gmtime(m['until'] + 3 * 3600))}")
        if d["muted"].get("copy"):
            lines.append("🔕 Сигналы копитрейдинга выключены Стражем (демо в минусе)")
        lines.append("\nЧестно: прошлые сделки не гарантируют будущих, а демо обычно лучше реальности. Зато деньги "
                     "не идут туда, где бот уже доказал минус.")
        return "\n".join(lines)

    def short_line(self) -> str:
        engines = self.engines()
        if not engines:
            return ""
        best = engines[0]
        icon = VERDICTS[best["verdict"]][0]
        paused = [ROUTES[r][0] for r, m in self.st["paused"].items() if m.get("until", 0) > time.time()]
        return (f"💰 <b>Профит</b>: лучшее — {icon} {html.escape(best['title'])} ({best['n']} сд., {best['mean']:+.2f}%/сд.)"
                + (f"; ⏸ Страж: {html.escape(', '.join(paused))}" if paused else "") + " — /profit")
