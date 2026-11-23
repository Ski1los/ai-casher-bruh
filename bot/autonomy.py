"""♾ Полный автомат: бот работает сам, без хозяина.

Что делает (раз в 5 минут):
* **держит обучение включённым** — всё, что работает БЕЗ денег: демо-арбитраж, демо мемкоинов, копитрейдинг,
  учёба у трейдеров, тренировка, разведчик, эфиры, автопилот в тесте (если есть ключ биржи);
* **сторож** — перезапускает упавшие или зависшие циклы бота, чистит память, если её стало много;
* **«Результат»** — раз в N часов присылает итог: деньги, чему научился, что починил, какой следующий шаг
  (ночью молчит; если ничего не произошло — не спамит);
* **настоящие деньги — только с разрешения хозяина** (одна кнопка с двойным подтверждением). Разрешил — автомат сам
  переводит на деньги только то, что ДОКАЗАЛО плюс без денег (вердикт «✅» Профит-центра), и только при безопасных
  лимитах. Начало терять — Страж ставит паузу, автомат возвращает способ в тест на 3 дня.
  /stop снимает разрешение и останавливает всё.

Самообновление новых версий — уже в telegram_bot._update_loop (нужна ссылка Deploy Hook); автомат лишь напоминает.
Состояние — в copy.st["autonomy"] (сохраняется и бэкапится вместе с копитрейдингом).
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from . import updater
from .memory import rss_mb
from .profit import ORDER, ROUTES, VERDICTS, route_evidence

log = logging.getLogger(__name__)

TICK_SEC = 300               # проверка раз в 5 минут
FIRST_TICK_SEC = 120         # первая — через 2 минуты после запуска (бэкап уже подтянут)
EVERY_CHOICES = (3, 6, 12, 24, 0)  # «Результат» раз в N часов, 0 — не присылать
QUIET_REPEAT_SEC = 24 * 3600  # если ничего не произошло — «всё тихо» не чаще раза в сутки
NIGHT = (0, 8)               # ночью по времени хозяина «Результат» ждёт утра
HARD_MAX_TRADE = 10.0        # автомат включает деньги, только если лимиты не выше этих
HARD_DAILY_LOSS = 2.0
REVERT_COOLDOWN = 3 * 24 * 3600  # вернул в тест после убытков — 3 дня не включает снова
MEM_SOFT_MB = 430            # из 512 МБ Render: выше — уборка памяти
MEM_EVERY_SEC = 1800
STALE_SEC = {"scan": 900, "scalp": 1200, "pulse": 1800}  # цикл не отмечался так долго — завис, перезапускаем
FIXES_KEEP = 40

LOOP_NAMES = {
    "scan": "сканер и демо", "scalp": "автопилот и лаборатория", "alerts": "ценовые алерты", "radar": "радар мемкоинов",
    "opps": "акции и новости", "copy": "копитрейдинг", "mentors": "учёба у трейдеров", "profit": "Профит-центр",
    "equity": "график баланса", "report": "ежедневный отчёт", "update": "самообновление", "keys": "проверка ключей",
    "ping": "не-засыпание в облаке", "pulse": "пульс рынка GMGN/Axiom",
}


def new_state() -> dict:
    return {"on": True, "real": False, "every_h": 6, "welcomed": False, "pilot_hold": False,
            "last_digest": 0.0, "last_sent": 0.0, "last_tick": 0.0, "last_mem": 0.0,
            "snap": {}, "fixes": [], "restarts": 0, "promoted": {}, "blocked": {}, "manual": []}


def _money(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}${abs(x):.2f}"


class Autonomy:
    def __init__(self, bot) -> None:
        self.b = bot

    # --- состояние ------------------------------------------------------------------------------

    @property
    def st(self) -> dict:
        cs = self.b.copy.st
        st = cs.get("autonomy")
        if not isinstance(st, dict):
            st = cs["autonomy"] = {}
        for k, v in new_state().items():
            st.setdefault(k, v)
        return st

    @property
    def on(self) -> bool:
        return bool(self.st["on"])

    def _changed(self) -> None:
        self.b.copy.changes += 1

    def _fix(self, text: str, now: float) -> None:
        self.st["fixes"].append({"at": now, "text": text[:200]})
        del self.st["fixes"][:-FIXES_KEEP]
        self._changed()

    def set_on(self, on: bool) -> None:
        self.st["on"] = bool(on)
        if on:
            self.st["pilot_hold"] = False
        self._changed()

    def cycle_every(self) -> int:
        cur = self.st["every_h"]
        i = EVERY_CHOICES.index(cur) if cur in EVERY_CHOICES else 0
        self.st["every_h"] = EVERY_CHOICES[(i + 1) % len(EVERY_CHOICES)]
        self._changed()
        return self.st["every_h"]

    def on_panic(self) -> None:
        """/stop: хозяин дёрнул стоп-кран. Снимаем разрешение на деньги и не включаем автопилот сами."""
        st = self.st
        st["real"] = False
        st["promoted"] = {}
        st["pilot_hold"] = True
        self._fix("🛑 ты нажал /stop: разрешение на настоящие деньги снято, автопилот сам не включаю", time.time())

    # --- 1. держим обучение включённым ----------------------------------------------------------------

    def _pilot_test_ok(self) -> bool:
        b, t = self.b, self.b.trader
        if self.st["pilot_hold"] or not t.working or t.state.live or t.state.auto or b.scalper.on:
            return False
        # после /stop могла остаться настоящая позиция — сами её снова не «вооружаем»
        return not any(p.get("live") for p in (b.scalper.positions or {}).values())

    def ensure(self, now: float | None = None) -> list[str]:
        """Включает всё, что учится без денег. Возвращает, что включил."""
        b = self.b
        if b.owner_id is None:
            return []
        done: list[str] = []
        if not b.demo.accounts:  # демо ещё ни разу не запускали (остановленное руками не трогаем)
            b.demo.start([5.0], b.owner_id)
            done.append("🔺 демо-арбитраж на $5")
        if not b.copy.st["on"]:
            b.copy.st["on"] = True
            done.append("🐋 копитрейдинг (без денег)")
        md = b.meme.demo
        if not md.get("started"):
            b.meme.start_demo_if_new()
            done.append("🧪 демо мемкоинов")
        elif not md.get("on"):
            md["on"] = True
            done.append("🧪 демо мемкоинов")
        m = b.mentors
        for st, label in ((m.st, "🎓 учёба у трейдеров"), (m.train.st, "🏋️ тренировка на демо"),
                          (m.scout.st, "🔭 разведчик трейдеров")):
            if not st.get("on"):
                st["on"] = True
                done.append(label)
        if m.gemini.configured and not m.live.st.get("on"):
            m.live.st["on"] = True
            done.append("🎥 эфиры вживую")
        pulse = getattr(b, "pulse", None)
        if pulse is not None and not pulse.st.get("on"):
            pulse.st["on"] = True
            done.append("⚡ пульс рынка")
        mirror = m.live.mirror.st
        if not mirror.get("on"):
            mirror["on"] = True
            done.append("🪞 повтор стримеров на демо")
        if self._pilot_test_ok():
            b.scalper.turn_on()
            done.append("🤖 автопилот в тесте (без денег)")
        if done:
            self._changed()
            b.save_trading()
        return done

    # --- 2. сторож --------------------------------------------------------------------------------

    def watchdog(self, now: float | None = None) -> list[str]:
        """Перезапускает упавшие и зависшие циклы бота. Возвращает, что перезапустил."""
        now = time.time() if now is None else now
        b = self.b
        out: list[str] = []
        loops = getattr(b, "_loops", None) or {}
        beats = getattr(b, "_beats", {})
        for name, (factory, task) in list(loops.items()):
            limit = STALE_SEC.get(name)
            if name == "scan":
                limit = max(limit, 6 * float(b.settings.scan_interval_sec))
            stale = bool(limit) and not task.done() and now - beats.get(name, now) > limit
            if not task.done() and not stale:
                continue
            why = "завис" if stale else "остановился"
            if stale:
                task.cancel()
            elif not task.cancelled() and task.exception() is not None:
                log.error("Цикл %s упал: %r", name, task.exception())
                why = "упал с ошибкой"
            b._spawn(name, factory)
            beats[name] = now
            self.st["restarts"] += 1
            text = f"🔁 перезапустил «{LOOP_NAMES.get(name, name)}» ({why})"
            log.warning("Автомат: %s", text)
            self._fix(text, now)
            out.append(text)
        if rss_mb() > MEM_SOFT_MB and now - self.st["last_mem"] > MEM_EVERY_SEC:
            self.st["last_mem"] = now
            try:
                r = b.cleanup()
                text = f"🧹 памяти стало много — убрал лишнее: {r['before']:.0f} → {r['after']:.0f} МБ"
                self._fix(text, now)
                out.append(text)
            except Exception:
                log.exception("Автомат: уборка памяти")
        return out

    # --- 3. настоящие деньги (только с разрешения) ---------------------------------------------------

    def _guard_paused(self, route: str, now: float) -> bool:
        mark = self.b.profit.st["paused"].get(route) or {}
        return float(mark.get("until") or 0) > now

    def _evidence_block(self, ev: dict, route: str, now: float) -> str | None:
        e = ev.get(route)
        if e is None:
            return "сделок без денег ещё нет — учится"
        if e["verdict"] != "proven":
            return (f"ещё не доказал плюс: {VERDICTS[e['verdict']][0]} {e['n']} сд., {e['mean']:+.2f}%/сд. "
                    "(нужно от 15 сделок и плюс даже при невезении)")
        if route in self.st["manual"]:
            return "ты выключил его руками — сам не включаю, пока заново не разрешишь деньги"
        if self._guard_paused(route, now):
            return "🛡 Страж поставил паузу после убытков"
        until = float(self.st["blocked"].get(route) or 0)
        if until > now:
            return f"после убытков на деньгах — снова не раньше {time.strftime('%d.%m %H:%M', time.gmtime(until + 3 * 3600))}"
        return None

    def pilot_block(self, ev: dict, now: float) -> str | None:
        """Почему автопилот ещё не на настоящих деньгах (None — можно)."""
        b, t, s = self.b, self.b.trader, self.b.settings
        if t.state.live and b.scalper.on:
            return None
        if not t.creds:
            return "нет ключа Bybit/BingX (/setup)"
        if not t.working:
            return "ключ биржи не работает (/doctor)"
        if s.max_trade_usd > HARD_MAX_TRADE or s.daily_loss_usd > HARD_DAILY_LOSS:
            return (f"лимиты выше безопасных (${s.max_trade_usd:g} на сделку, ${s.daily_loss_usd:g} в день) — сам не "
                    f"включаю, верни ≤${HARD_MAX_TRADE:g} и ≤${HARD_DAILY_LOSS:g} (/set)")
        if b.duplicate_running():
            return "работают две копии бота (/doctor)"
        if t.state.auto:
            return "включены треугольники — они на тех же деньгах"
        return self._evidence_block(ev, "pilot", now)

    def meme_block(self, ev: dict, now: float) -> str | None:
        mt = self.b.meme
        if mt.on:
            return None
        if not mt.configured:
            return "кошелёк бота Phantom не подключён (/meme)"
        if float(mt.st.get("size_usd") or 0) > HARD_MAX_TRADE or float(mt.st.get("daily_loss_usd") or 0) > HARD_DAILY_LOSS:
            return "лимиты мемкоинов выше безопасных — сам не включаю"
        return self._evidence_block(ev, "meme", now)

    def _real_n(self, route: str) -> int:
        eid = "pilot_real" if route == "pilot" else "meme_real"
        return len(self.b.profit._real_rets(eid))

    def _manual_off(self, route: str, now: float) -> None:
        st = self.st
        st["promoted"].pop(route, None)
        if route not in st["manual"]:
            st["manual"].append(route)
        self._fix(f"✋ ты выключил {ROUTES[route][0]} руками — сам больше не включаю", now)

    def money_step(self, now: float | None = None, engines: list[dict] | None = None) -> list[dict]:
        """Переводит доказанное на деньги и возвращает в тест то, что теряет. → события для хозяина."""
        now = time.time() if now is None else now
        st, b, t = self.st, self.b, self.b.trader
        events: list[dict] = []
        engines = b.profit.engines(now) if engines is None else engines
        by_id = {e["id"]: e for e in engines}
        # сначала — следим за тем, что уже включили
        for route in list(st["promoted"]):
            if route == "pilot":
                running = t.state.live and b.scalper.on
                stop = None
                if not running:
                    self._manual_off(route, now)
                    continue  # выключили руками — не спорим
                if self._guard_paused("pilot", now):
                    stop = "Страж увидел доказанный убыток"
                elif (by_id.get("pilot_real") or {}).get("verdict") == "losing":
                    stop = "настоящие сделки доказанно в минусе"
                if stop:
                    t.state.live = False  # автопилот остаётся включённым в тесте и учится дальше
                    t.pending.clear()
            else:
                running = b.meme.on
                stop = None
                if not running:
                    self._manual_off(route, now)
                    continue
                if self._guard_paused("meme", now):
                    stop = "Страж увидел доказанный убыток"
                elif (by_id.get("meme_real") or {}).get("verdict") == "losing":
                    stop = "настоящие сделки доказанно в минусе"
                if stop:
                    b.meme.st["on"] = False
            if stop:
                st["promoted"].pop(route)
                st["blocked"][route] = now + REVERT_COOLDOWN
                text = (f"↩️ <b>Автомат вернул {html.escape(ROUTES[route][0])} в тест</b>: {stop}. Открытые позиции "
                        "бот доводит до конца сам. Новые покупки — только без денег, пока снова не докажет плюс "
                        "(не раньше чем через 3 дня).")
                self._fix(f"↩️ вернул {ROUTES[route][0]} в тест: {stop}", now)
                events.append({"text": text, "buttons": [("♾ Автомат", "au:show")]})
        if not st["real"]:
            return events
        ev = route_evidence(engines)
        if not (t.state.live and b.scalper.on) and self.pilot_block(ev, now) is None:
            t.state.live = True
            t.pending.clear()
            b.scalper.turn_on()
            st["promoted"]["pilot"] = {"at": now, "n0": self._real_n("pilot")}
            e = ev["pilot"]
            text = (f"🔴 <b>Автомат включил настоящие деньги: {html.escape(ROUTES['pilot'][0])}</b>.\n"
                    f"Почему: {html.escape(e['title'])} доказал плюс — {e['n']} сделок, {e['mean']:+.2f}% за сделку "
                    f"(даже при невезении {e['lcb']:+.2f}%).\n"
                    f"Лимиты: до ${b.settings.max_trade_usd:g} на сделку, убыток не больше ${b.settings.daily_loss_usd:g} "
                    "в день. Начнёт терять — сам верну в тест. Остановить всё: /stop")
            self._fix(f"🔴 включил деньги: {ROUTES['pilot'][0]}", now)
            events.append({"text": text, "buttons": [("♾ Автомат", "au:show"), ("🛑 Стоп", "tr:stop")]})
        if not b.meme.on and self.meme_block(ev, now) is None:
            b.meme.st["on"] = True
            b.copy.st["on"] = True
            st["promoted"]["meme"] = {"at": now, "n0": self._real_n("meme")}
            e = ev["meme"]
            text = (f"🔴 <b>Автомат включил настоящие деньги: {html.escape(ROUTES['meme'][0])}</b>.\n"
                    f"Почему: {html.escape(e['title'])} доказал плюс — {e['n']} сделок, {e['mean']:+.2f}% за сделку.\n"
                    f"Лимиты: ${float(b.meme.st['size_usd']):g} на сделку, убыток не больше "
                    f"${float(b.meme.st['daily_loss_usd']):g} в день. Начнёт терять — сам выключу. Остановить: /meme")
            self._fix(f"🔴 включил деньги: {ROUTES['meme'][0]}", now)
            events.append({"text": text, "buttons": [("♾ Автомат", "au:show"), ("💸 Мемкоины", "mt:show")]})
        if events:
            self._changed()
        return events

    def allow_real(self, on: bool, now: float | None = None) -> str:
        now = time.time() if now is None else now
        st, b = self.st, self.b
        st["real"] = bool(on)
        if on:
            st["pilot_hold"] = False
            st["manual"] = []
            self._fix("🔓 хозяин разрешил настоящие деньги (только доказанному)", now)
            self._changed()
            return "🔓 Разрешил. Включу деньги сам — только тому, что докажет плюс без денег."
        stopped = []
        for route in list(st["promoted"]):
            if route == "pilot" and b.trader.state.live:
                b.trader.state.live = False
                b.trader.pending.clear()
                stopped.append(ROUTES[route][0])
            elif route == "meme" and b.meme.on:
                b.meme.st["on"] = False
                stopped.append(ROUTES[route][0])
        st["promoted"] = {}
        self._fix("🔒 хозяин запретил настоящие деньги", now)
        self._changed()
        return ("🔒 Запретил настоящие деньги." + (f" Вернул в тест: {', '.join(stopped)} (открытые позиции бот "
                                                   "доведёт сам)." if stopped else ""))

    # --- 4. «Результат» ---------------------------------------------------------------------------------

    def _night(self, now: float) -> bool:
        hour = time.gmtime(now + self.b.settings.report_utc_offset * 3600).tm_hour
        return NIGHT[0] <= hour < NIGHT[1]

    def _real_money(self, since: float) -> tuple[float, int]:
        b = self.b
        pnl, n = 0.0, 0
        for r in b.trader.state.log:
            if r.get("live") and r.get("kind") == "sell" and r.get("pnl_usd") is not None and (r.get("at") or 0) > since:
                pnl += float(r["pnl_usd"])
                n += 1
        for c in b.meme.st.get("closed") or []:
            if c.get("pnl_usd") is not None and (c.get("closed") or 0) > since:
                pnl += float(c["pnl_usd"])
                n += 1
        return pnl, n

    def _equity(self, since: float) -> tuple[float, float] | None:
        hist = self.b.trader.state.history
        if not hist:
            return None
        start = next((v for ts, v in hist if ts >= since - 900), hist[-1][1])
        return float(start), float(hist[-1][1])

    def next_step(self, engines: list[dict] | None = None, now: float | None = None) -> str:
        """Одна строка: что дальше (и нужно ли что-то от хозяина)."""
        now = time.time() if now is None else now
        b, st = self.b, self.st
        engines = b.profit.engines(now) if engines is None else engines
        if st["promoted"]:
            names = ", ".join(ROUTES[r][0] for r in st["promoted"])
            return f"{names} работает на настоящих деньгах. От тебя ничего не нужно — я слежу и остановлю, если начнёт терять."
        ev = route_evidence(engines)
        proven = [r for r, e in ev.items() if e["verdict"] == "proven"]
        if proven and not st["real"]:
            return (f"✅ {ROUTES[proven[0]][0]} доказал плюс без денег. Хочешь, чтобы я сам включил деньги с лимитами "
                    "— нажми «🔓 Разрешить деньги» в /autonomy.")
        if proven and st["real"]:
            r = proven[0]
            why = self.pilot_block(ev, now) if r == "pilot" else self.meme_block(ev, now) if r == "meme" else \
                "треугольники сам не включаю: им нужно от $15 на бирже — /trade"
            if why:
                return f"{ROUTES[r][0]}: {why}."
        if not b.trader.creds:
            return ("Учусь без денег. Чтобы потом торговать по-настоящему, добавь ключ Bybit или BingX в Render (/setup) "
                    "— пока сделки проверяются на демо.")
        best = next((e for e in engines if e["n"]), None)
        if best:
            left = max(0, 15 - best["n"])
            return (f"Учусь без денег. Ближе всех к доказанному плюсу — {best['title']} ({best['n']} сд., "
                    f"{best['mean']:+.2f}%/сд." + (f", нужно ещё ≥{left}" if left else "") + "). Ничего делать не нужно.")
        return "Учусь без денег: первые сделки на демо обычно появляются в течение суток. Ничего делать не нужно."

    def digest(self, now: float | None = None, force: bool = False) -> str | None:
        """Текст «Результата» за период с прошлого отчёта (None — нечего сказать, а «тихо» уже говорили)."""
        now = time.time() if now is None else now
        st, b = self.st, self.b
        since = st["last_digest"] or now - (st["every_h"] or 6) * 3600
        engines = b.profit.engines(now)
        snap = st["snap"]
        moved = [(e, e["n"] - int(snap.get(e["id"], 0))) for e in engines if e["n"] > int(snap.get(e["id"], 0))]
        pnl, n_real = self._real_money(since)
        fixes = [f["text"] for f in st["fixes"] if f["at"] > since]
        hours = max(1, round((now - since) / 3600))
        quiet = not moved and not n_real and not fixes
        if quiet and not force and now - st["last_sent"] < QUIET_REPEAT_SEC:
            st["last_digest"] = now
            self._changed()
            return None
        lines = [f"📊 <b>Результат за {hours} ч</b> · ♾ автомат работает сам"]
        eq = self._equity(since)
        if n_real or st["promoted"]:
            lines.append(f"💵 <b>Настоящие деньги</b>: {_money(pnl)} за {n_real} сд."
                         + (f" · баланс ${eq[1]:.2f} ({_money(eq[1] - eq[0])})" if eq else ""))
        elif eq:
            lines.append(f"💵 Баланс на биржах: ${eq[1]:.2f} ({_money(eq[1] - eq[0])}) — бот им пока не торгует")
        else:
            lines.append("💵 Настоящими деньгами пока не торгую — только учусь (это нормально и безопасно)")
        if moved:
            lines.append("\n🧪 <b>Учёба без денег</b> (новые сделки):")
            for e, dn in sorted(moved, key=lambda x: (ORDER[x[0]["verdict"]], -x[1]))[:6]:
                lines.append(f"{VERDICTS[e['verdict']][0]} {html.escape(e['title'])}: +{dn} сд. "
                             f"(всего {e['n']}, {e['mean']:+.2f}%/сд.)")
        elif quiet:
            lines.append("\n😴 Новых сделок не было: рынок спокойный или фильтры не пропустили ничего хорошего. "
                         "Лучше ничего, чем сделка в минус.")
        if fixes:
            lines.append("\n🔧 <b>Сам починил и сделал</b>:")
            lines += [f"• {html.escape(x)}" for x in fixes[-6:]]
        else:
            lines.append("\n🔧 Всё работало без сбоев.")
        if updater.outdated():
            lines.append(f"⬆️ Есть новая версия v{updater.UPDATE.get('latest')}"
                         + (" — ставлю сам." if updater.hook_url() else " — нажми Manual Deploy в Render (/update)."))
        lines.append(f"\n👉 {html.escape(self.next_step(engines, now))}")
        st["snap"] = {e["id"]: e["n"] for e in engines}
        st["last_digest"] = now
        st["last_sent"] = now
        self._changed()
        return "\n".join(lines)

    def digest_due(self, now: float) -> bool:
        st = self.st
        every = st["every_h"]
        return bool(every) and now - st["last_digest"] >= every * 3600 and not self._night(now)

    # --- панель ------------------------------------------------------------------------------------------

    def engines_status(self) -> list[tuple[str, bool, str]]:
        """[(название, работает?, пояснение)] для панели и приложения."""
        b = self.b
        m = b.mentors
        t = b.trader
        pilot_note = ("на деньгах" if t.state.live and b.scalper.on else "в тесте" if b.scalper.on else
                      "остановлен тобой (/stop)" if self.st["pilot_hold"] else
                      "нужен ключ биржи" if not t.working else "выключен")
        return [
            ("🔺 Демо-арбитраж", b.demo.active, "" if b.demo.active else "остановлен" if b.demo.accounts else ""),
            ("🧪 Демо мемкоинов", b.meme.demo_on, ""),
            ("🐋 Копитрейдинг", b.copy.on, "без денег"),
            ("🎓 Учёба у трейдеров", bool(m.st.get("on")), ""),
            ("🏋️ Тренировка", bool(m.train.st.get("on")), ""),
            ("🔭 Разведчик", bool(m.scout.st.get("on")), ""),
            ("🎥 Эфиры вживую", bool(m.live.st.get("on")) and m.gemini.configured,
             "" if m.gemini.configured else "нужен ключ Gemini"),
            ("🪞 Повтор стримеров на демо", bool(m.live.mirror.st.get("on")), ""),
            ("⚡ Пульс рынка", bool(getattr(b, "pulse", None) and b.pulse.st.get("on")),
             "" if getattr(b, "gmgn", None) is None or b.gmgn.configured else "без ключа GMGN — только DexScreener"),
            ("🧪 Лаборатория", True, "на публичных свечах" if not t.working else ""),
            ("🤖 Автопилот", b.scalper.on, pilot_note),
        ]

    def status_dict(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        st = self.st
        engines = self.b.profit.engines(now)
        ev = route_evidence(engines)
        return {
            "on": st["on"], "real": st["real"], "every_h": st["every_h"], "restarts": st["restarts"],
            "last_tick": st["last_tick"], "last_digest": st["last_digest"],
            "engines": [{"title": a, "on": bool(o), "note": n} for a, o, n in self.engines_status()],
            "money": {"pilot": self.pilot_block(ev, now) or "работает на деньгах",
                      "meme": self.meme_block(ev, now) or "работает на деньгах"},
            "promoted": sorted(st["promoted"]), "self_update": bool(updater.hook_url()),
            "fixes": [{"at": f["at"], "text": f["text"]} for f in st["fixes"][-5:]][::-1],
            "next": self.next_step(engines, now),
        }

    def text(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        st = self.st
        d = self.status_dict(now)
        lines = [f"♾ <b>Полный автомат</b> — {'✅ работает' if st['on'] else '⏸ выключен'}"]
        if st["on"]:
            lines.append("Бот работает сам круглые сутки, тебе ничего нажимать не нужно.")
        lines.append("\n<b>Учится без денег</b> (держу включённым):")
        for e in d["engines"]:
            lines.append(f"{'✅' if e['on'] else '➖'} {e['title']}" + (f" — {e['note']}" if e["note"] else ""))
        tick = time.strftime("%H:%M", time.gmtime(st["last_tick"] + self.b.settings.report_utc_offset * 3600)) \
            if st["last_tick"] else "скоро"
        lines.append(f"\n🛡 <b>Сторож</b>: проверка раз в 5 мин (последняя {tick}), перезапусков: {st['restarts']}")
        lines.append("📊 <b>«Результат»</b>: " + (f"раз в {st['every_h']} ч, ночью молчу" if st["every_h"] else "выключен"))
        lines.append("⬆️ <b>Обновления</b>: " + ("✅ ставлю сам" if d["self_update"] else
                                                "⚠️ сам не могу — нужна ссылка Deploy Hook (1 раз, кнопка ниже)"))
        lines.append("\n💵 <b>Настоящие деньги</b>: " + ("🔓 разрешены — включаю только доказанное" if st["real"] else
                                                       "🔒 только с твоего разрешения (сейчас запрещены)"))
        lines.append(f"• {ROUTES['pilot'][0]}: {html.escape(d['money']['pilot'])}")
        lines.append(f"• {ROUTES['meme'][0]}: {html.escape(d['money']['meme'])}")
        lines.append(f"\n👉 {html.escape(d['next'])}")
        return "\n".join(lines)

    def summary_line(self) -> str:
        st = self.st
        if not st["on"]:
            return "♾ Полный автомат выключен — /autonomy"
        return ("♾ Автомат работает сам" + (" · 🔓 деньги разрешены" if st["real"] else "")
                + (f" · перезапусков {st['restarts']}" if st["restarts"] else "") + " — /autonomy")

    def welcome(self, enabled: list[str]) -> str:
        return ("♾ <b>Включил полный автомат</b> — теперь бот работает сам, без тебя.\n\n"
                "Что я делаю сам:\n"
                "• держу включённым всё, что учится <b>без денег</b>" + (f" (сейчас включил: {', '.join(enabled)})"
                                                                          if enabled else "") + ";\n"
                "• каждые 5 минут проверяю, что всё работает, и перезапускаю то, что сломалось;\n"
                f"• раз в {self.st['every_h']} ч присылаю «📊 Результат»: деньги, чему научился, что починил;\n"
                "• когда что-то <b>докажет плюс</b> — скажу. Настоящими деньгами сам торгую, только если ты разрешишь "
                "(одна кнопка). Без разрешения — никогда.\n\n"
                "Честно: заработок не гарантирован. Моя главная работа — не терять и не обманывать себя.")


# --- цикл -------------------------------------------------------------------------------------------------

async def tick(b, now: float | None = None) -> None:
    now = time.time() if now is None else now
    a = b.autonomy
    st = a.st
    before = b.copy.changes
    fixes = a.watchdog(now)  # сторож работает всегда: упавший цикл — это поломка, а не настройка
    if b.owner_id is None or not st["on"]:
        if b.copy.changes != before:
            b.save_copy()
        return
    st["last_tick"] = now
    enabled = a.ensure(now)
    if not st["welcomed"]:
        st["welcomed"] = True
        st["last_digest"] = now
        st["snap"] = {e["id"]: e["n"] for e in b.profit.engines(now)}
        await b._send(b.owner_id, a.welcome(enabled), keyboard(b))
    elif enabled:
        for x in enabled:
            a._fix(f"включил обратно: {x}", now)
        await b._send(b.owner_id, "♾ Автомат включил обратно: " + ", ".join(enabled)
                      + ".\nЕсли выключал специально — сначала выключи автомат (/autonomy), иначе я держу обучение "
                        "включённым.")
    try:
        events = a.money_step(now)
    except Exception:
        log.exception("Автомат: деньги")
        events = []
    if events:
        from .copy_ui import send_events
        await send_events(b, events)
        b.save_trading()
    if a.digest_due(now):
        text = a.digest(now)
        if text:
            await b._send(b.owner_id, text, keyboard(b))
    if fixes or enabled or events:
        log.info("Автомат: %s", "; ".join(fixes + enabled + [e["text"][:60] for e in events]))
    if b.copy.changes != before:
        b.save_copy()
        if events or enabled:
            await b._backup_now()


async def autonomy_loop(b) -> None:
    await asyncio.sleep(FIRST_TICK_SEC)
    while True:
        b._beats["autonomy"] = time.time()
        try:
            await tick(b)
        except Exception:
            log.exception("Ошибка полного автомата")
        await asyncio.sleep(TICK_SEC)


def keyboard(b):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    a = b.autonomy
    st = a.st
    every = f"каждые {st['every_h']} ч" if st["every_h"] else "выкл"
    rows = [
        [InlineKeyboardButton(text="📊 Результат сейчас", callback_data="au:now"),
         InlineKeyboardButton(text=f"⏰ Отчёт: {every}", callback_data="au:every")],
        [InlineKeyboardButton(text="🔒 Запретить деньги" if st["real"] else "🔓 Разрешить деньги",
                              callback_data="au:realoff" if st["real"] else "au:real")],
        [InlineKeyboardButton(text="⏸ Выключить автомат" if st["on"] else "▶️ Включить автомат",
                              callback_data="au:off" if st["on"] else "au:on"),
         InlineKeyboardButton(text="💰 Профит", callback_data="pf:show")],
    ]
    if not updater.hook_url():
        rows.insert(2, [InlineKeyboardButton(text="⬆️ Чтобы обновлялся сам", callback_data="au:hook")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def real_warning(b) -> str:
    s, mt = b.settings, b.meme.st
    return ("🔴 <b>Разрешить автомату настоящие деньги?</b>\n\n"
            "Как это работает: как только способ <b>докажет плюс без денег</b> (от 15 сделок и в плюсе даже при "
            "невезении), бот сам переведёт его на настоящие деньги:\n"
            f"• {ROUTES['pilot'][0]} — до ${s.max_trade_usd:g} на сделку, убыток не больше ${s.daily_loss_usd:g} в день "
            f"(деньги включу, только если эти лимиты не выше ${HARD_MAX_TRADE:g} и ${HARD_DAILY_LOSS:g});\n"
            f"• {ROUTES['meme'][0]} — ${float(mt.get('size_usd') or 0):g} на сделку, убыток не больше "
            f"${float(mt.get('daily_loss_usd') or 0):g} в день (если подключён кошелёк бота).\n\n"
            f"Начнёт терять — 🛡 Страж ставит паузу, а я возвращаю способ в тест на 3 дня.\n"
            "Честно: прошлый плюс не гарантирует будущий, а у рынка бывают резкие обвалы. Можно потерять деньги, "
            "которые лежат на бирже и в кошельке бота. Клади туда только сумму, которую не жалко ($5–10).\n"
            "Остановить всё в любую секунду: /stop.")


def register(b) -> None:
    from aiogram import F
    from aiogram.filters import Command
    from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

    dp = b.dp

    @dp.message(Command("autonomy", "automat", "full", "автомат"))
    async def cmd_autonomy(message: Message) -> None:
        if not b._allowed_user(message.from_user.id if message.from_user else None):
            return
        await b._send(message.chat.id, b.autonomy.text(), keyboard(b))

    @dp.callback_query(F.data.startswith("au:"))
    async def cb_autonomy(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        a = b.autonomy
        if action not in ("show", "now", "hook") and (b.owner_id is None or cb.from_user.id != b.owner_id):
            await cb.answer("Менять может только владелец", show_alert=True)
            return
        await cb.answer()
        if action == "show":
            await b._send(chat_id, a.text(), keyboard(b))
            return
        if action == "now":
            await b._send(chat_id, a.digest(force=True) or "Пока нечего сказать.", keyboard(b))
        elif action == "hook":
            await b._send(chat_id, "⬆️ <b>Чтобы бот обновлялся сам</b>\n\n" + updater.HOOK_STEPS
                          + "\n\nПосле этого каждую новую версию я поставлю сам в течение 30 минут.")
            return
        elif action in ("on", "off"):
            a.set_on(action == "on")
            await b._send(chat_id, ("▶️ Автомат включён: через пару минут проверю, что всё работает." if action == "on"
                                    else "⏸ Автомат выключен. Всё, что сейчас включено, продолжает работать, "
                                         "но перезапускать и включать обратно я больше не буду. Сторож упавших "
                                         "циклов работает всегда."), keyboard(b))
        elif action == "every":
            a.cycle_every()
            await b._send(chat_id, a.text(), keyboard(b))
        elif action == "real":
            await b._send(chat_id, real_warning(b), InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, разрешаю (риск понимаю)", callback_data="au:real2")],
                [InlineKeyboardButton(text="Отмена", callback_data="au:show")]]))
            return
        elif action == "real2":
            await b._send(chat_id, a.allow_real(True) + "\n\n" + a.next_step(), keyboard(b))
        elif action == "realoff":
            await b._send(chat_id, a.allow_real(False), keyboard(b))
        b.save_copy()
        b.save_trading()
        await b._backup_now()
