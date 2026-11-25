"""🧪 Все демо в одном месте (/demos): что работает, сколько сделок, и честно — почему сделок пока нет.

Каждое демо торгует виртуальными деньгами по живым ценам. Пороги качества здесь НЕ снижаются: если сделок нет,
значит фильтры защищают деньги, и бот пишет, какой именно фильтр сработал и что можно сделать.
"""

from __future__ import annotations

import html
import logging
import time

log = logging.getLogger(__name__)

DEMO_ARB_AMOUNTS = [5.0, 50.0]  # $5 — твоя реальность; $50 — видно, работает ли стратегия, когда сумма проходит минимум


def _row(rid: str, icon: str, title: str, on: bool, n: int, result: str, why: str, cmd: str) -> dict:
    return {"id": rid, "icon": icon, "title": title, "on": bool(on), "n": int(n), "result": result,
            "why": why, "cmd": cmd}


def _pct_line(rets: list[float]) -> str:
    if not rets:
        return ""
    wins = sum(1 for r in rets if r > 0)
    return f"в плюс {wins} из {len(rets)}, в среднем {sum(rets) / len(rets):+.2f}% за сделку"


def _arb(b) -> dict:
    d = b.demo
    accs = list(d.accounts.values()) if isinstance(d.accounts, dict) else list(d.accounts)
    n = sum(len(a.trades) for a in accs)
    best = max(accs, key=lambda a: a.equity / a.start_balance if a.start_balance else 0, default=None)
    result = ""
    if n and best is not None and best.start_balance:
        result = f"лучший счёт ${best.start_balance:g} → ${best.equity:.2f} ({(best.equity / best.start_balance - 1) * 100:+.2f}%)"
    if not accs:
        why = "не запущен — включу кнопкой «Включить всё демо»"
    elif not d.active:
        why = "остановлен"
    else:
        why = d.why_idle()
    return _row("arb", "🔺", "Арбитраж (межбиржевой и треугольный)", d.active, n, result, why, "/demo")


def _tri(b) -> dict:
    t = b.trader
    on = bool(t.state.auto) and not t.state.live
    recs = [r for r in t.state.log if r.get("kind") == "triangle" and r.get("source") == "auto" and not r.get("live")]
    rets = [r["pnl_usd"] / r["spent"] * 100 for r in recs if r.get("pnl_usd") is not None and r.get("spent")]
    if t.state.live:
        why = "включён реальный режим — тест треугольников не идёт"
    elif not t.state.auto:
        why = ("выключены, пока работает автопилот: они делят одни USDT (вместо автопилота: /autotrade on)"
               if b.scalper.on else "выключены: /autotrade on (в тесте ключ не нужен)")
    elif not b.settings.triangular_enabled:
        why = "треугольный арбитраж выключен в настройках (TRIANGULAR=0)"
    else:
        why = (t.auto_status or {}).get("reason") or "ждёт первого скана"
    return _row("tri", "⚙️", "Треугольники — автоторговля в тесте", on, len(recs), _pct_line(rets), why, "/why")


def _pilot(b) -> dict:
    from .profit import _scalp_trades
    sc, t = b.scalper, b.trader
    on = sc.on and not t.state.live
    rets, _ = _scalp_trades(b, False)
    if t.state.live:
        why = "включён реальный режим — автопилот торгует настоящими деньгами (/scalp)"
    elif not sc.on:
        why = ("выключен: треугольники в тесте занимают те же USDT" if t.state.auto
               else "выключен — включу кнопкой «Включить всё демо»")
    elif sc.positions:
        why = "в позиции — ждёт цели или стопа"
    else:
        reasons = [f"{sc._name(n)}: {r}" for n, r in (sc.reasons or {}).items() if r and n != "_"]
        why = "; ".join(reasons) or sc.last_reason or "проверяет монеты на истории (первый раз до 5 минут)"
    return _row("pilot", "🤖", "Автопилот (режим $5) в тесте", on, len(rets), _pct_line(rets), why, "/scalp")


def _lab(b) -> dict:
    stats = b.scalper.lab["stats"]
    rets = [r for s in stats.values() for r in s.get("rets", [])]
    opened = len(b.scalper.lab.get("open") or {})
    why = ("учится на живых свечах: ждёт сигнала любой из запасных стратегий"
           + (f", открыто {opened}" if opened else ""))
    return _row("lab", "🧪", "Лаборатория стратегий", True, len(rets), _pct_line(rets), why, "/lab")


def _spread(b) -> dict:
    sp = b.spread
    st = sp.st
    paper = not sp.live
    n, pnl = st["totals"].get("test", [0, 0.0])
    on = bool(st["on"]) and paper
    if not st["on"]:
        probs = sp.problems()
        why = (f"выключен: {probs[0]} — без них не узнать комиссии вывода и адреса" if probs
               else "выключен — включу кнопкой «Включить всё демо»")
    elif sp.live:
        why = "работает на настоящих деньгах (/spread)"
    else:
        probs = sp.problems()
        last = st["checks"][-1] if st.get("checks") else None
        from .spread import REASONS
        why = ("; ".join(probs) if probs else
               (f"последняя проверка {last['coin']}: {REASONS.get(last['reason'], last['reason'])}"
                if last and last.get("reason") else "ждёт спред ≥ порога после всех комиссий и вывода"))
    result = f"итог {float(pnl):+.2f}$" if n else ""
    return _row("spread", "⚖️", "Спред между биржами — на бумаге", on, int(n), result, why, "/spread")


def _copy(b) -> dict:
    ct = b.copy
    closed = [t for t in ct.st["closed"] if t.get("passed")]
    if not ct.on:
        why = "выключен — включу кнопкой «Включить всё демо»"
    elif ct.st["open"]:
        why = f"открыто копий: {len(ct.st['open'])}"
    elif not ct.active():
        why = "ищет кошельки (тренды DexScreener" + (" и GMGN)" if getattr(getattr(ct, "gmgn", None), "configured",
                                                                             False) else ")")
    else:
        why = f"слежу за {len(ct.active())} кошельками — жду их покупок"
    if ct.last_error:
        why += f". Последняя ошибка: {ct.last_error[:100]}"
    return _row("copy", "🐋", "Копитрейдинг кошельков (Solana)", ct.on, len(closed),
                _pct_line([t["ret"] for t in closed]), why, "/copy")


def _meme(b) -> dict:
    mt = b.meme
    d = mt.demo
    closed = d.get("closed") or []
    rets = [c["pct"] for c in closed if c.get("pct") is not None]
    if not mt.demo_on:
        why = "выключен — включу кнопкой «Включить всё демо»"
    elif d.get("positions"):
        why = f"открыто позиций: {len(d['positions'])}"
    else:
        why = mt.demo_idle() or "недавно торговал — жду следующего сигнала"
    return _row("meme", "🪙", "Мемкоины — демо-счёт", mt.demo_on, len(closed), _pct_line(rets), why, "/meme")


def _train(b) -> dict:
    m = b.mentors
    tr = m.train
    st = tr.st
    n = sum(v["n"] for v in st["variants"].values())
    rets = [r for v in st["variants"].values() for r in v["rets"]]
    opened = sum(len(v["positions"]) for v in st["variants"].values())
    if not st["on"]:
        why = "выключена — включу кнопкой «Включить всё демо»"
    elif opened:
        why = f"открыто сделок по идеям: {opened} — закрою по цели, стопу или сроку"
    elif not st["ideas"]:
        if not m.active():
            why = "нет источников идей — добавь трейдеров: /mentors (YouTube или Twitch)"
        elif not m.gemini.configured:
            why = "идеи из видео разбирает Gemini — нужен ключ GEMINI_API_KEY (/doctor), чаты Twitch — без ключа"
        else:
            why = "ждёт первой идеи трейдеров из новых видео и чатов"
    else:
        why = f"идей {st['ideas']}, жду новых"
    return _row("train", "🏋️", "Тренировка на идеях трейдеров", st["on"], n, _pct_line(rets), why, "/train")


def _mirror(b) -> dict:
    m = b.mentors
    mr = m.live.mirror.st
    books = [bk for name, bk in mr["books"].items()]
    n = sum(bk["n"] for bk in books)
    smart = (mr["books"].get("smart") or {}).get("rets") or []
    on = bool(mr["on"]) and bool(m.live.st.get("on")) and m.gemini.configured
    if not mr["on"]:
        why = "выключен — включу кнопкой «Включить всё демо»"
    elif not m.gemini.configured:
        why = "эфиры смотрит Gemini — нужен ключ GEMINI_API_KEY"
    elif not m.live.st.get("on"):
        why = "выключено наблюдение за эфирами — включу кнопкой «Включить всё демо»"
    else:
        why = "сделки появляются, только когда стример в эфире открывает позицию — жду"
    return _row("mirror", "🪞", "Повтор стримеров", on, n, _pct_line(smart), why, "/stream")


def _pulse(b) -> dict:
    p = getattr(b, "pulse", None)
    if p is None:
        return _row("pulse", "⚡", "Пульс рынка", False, 0, "", "не подключён", "/pulse")
    st = p.st
    n = sum(bk["n"] for bk in st["books"].values())
    _title, rets, _first = p.engine_rets()
    if not st["on"]:
        why = "выключен — включу кнопкой «Включить всё демо»"
    elif st.get("last_error"):
        why = f"ошибка источника: {str(st['last_error'])[:100]}"
    elif st["picks"]:
        why = f"слежу за {len(st['picks'])} кандидатами — закрою по правилу выхода"
    else:
        why = "ищет кандидатов GMGN/DexScreener, которые прошли фильтры безопасности"
    return _row("pulse", "⚡", "Пульс рынка (GMGN/Axiom)", st["on"], n, _pct_line(rets), why, "/pulse")


def _news(b) -> dict:
    nw = b.news
    st = nw.st
    rets = [float(c["pct"]) for c in st["paper"]["closed"]]
    opened = len(st["paper"]["open"])
    if not st["on"]:
        why = "новости выключены: /news on"
    elif opened:
        why = f"ждёт 4 ч после новости: открыто {opened}"
    else:
        why = (f"ждёт хорошую свежую новость по монете с наших бирж (ожидаемый рост ≥ 1.5% за 4 ч); "
               f"замеров влияния: {st.get('measured', 0)}")
    return _row("news", "📰", "Новости — бумажные сделки по сигналам", st["on"], len(rets), _pct_line(rets), why,
                "/news")


PARTS = (_arb, _tri, _pilot, _lab, _spread, _news, _copy, _meme, _train, _mirror, _pulse)


def rows(b) -> list[dict]:
    out = []
    for fn in PARTS:
        try:
            out.append(fn(b))
        except Exception as e:  # одно сломанное демо не должно ломать весь отчёт
            log.info("Демо-обзор %s: %s", fn.__name__, e)
    return out


def text(b, now: float | None = None) -> str:
    now = time.time() if now is None else now
    rs = rows(b)
    on = sum(1 for r in rs if r["on"])
    total = sum(r["n"] for r in rs)
    live = b.trader.state.live
    lines = ["🧪 <b>Вся демо-торговля</b> — виртуальные деньги, живые цены",
             f"Работает {on} из {len(rs)} · сделок всего: <b>{total}</b>"
             + (" · ⚠️ включён реальный режим: часть тестов не идёт" if live else "")]
    for r in rs:
        state = "🟢" if r["on"] else "⚪️"
        head = f"\n{state} {r['icon']} <b>{html.escape(r['title'])}</b> — сделок {r['n']}"
        lines.append(head + (f": {html.escape(r['result'])}" if r["result"] else ""))
        if r["why"] and (not r["n"] or not r["on"]):
            lines.append(f"   {'Почему нет сделок' if r['on'] else 'Состояние'}: {html.escape(r['why'])}")
        elif r["why"]:
            lines.append(f"   Сейчас: {html.escape(r['why'])}")
        lines.append(f"   Подробно: {r['cmd']}")
    lines.append("\nЧестно: демо не учитывает очередь ордеров и часть проскальзывания — живые сделки обычно чуть хуже. "
                 "Пороги прибыли и защиты бот ради «больше сделок» не снижает.")
    return "\n".join(lines)


def enable_all(b) -> list[str]:
    """Включить всё демо, что выключено (историю не стирает). Возвращает, что включил."""
    done: list[str] = []
    t = b.trader
    if not b.demo.active or any(f"{s}-{a:g}" not in b.demo.accounts for s in ("cross",) for a in DEMO_ARB_AMOUNTS):
        was = b.demo.active
        added = b.demo.resume(DEMO_ARB_AMOUNTS, b.owner_id)
        if not was or added:
            done.append("🔺 демо-арбитраж" + (f" (+ счета: {', '.join(added)})" if added else ""))
    if not b.copy.st["on"]:
        b.copy.st["on"] = True
        done.append("🐋 копитрейдинг (без денег)")
    md = b.meme.demo
    if not md.get("started"):
        b.meme.start_demo_if_new()
        done.append("🪙 демо мемкоинов")
    elif not md.get("on"):
        md["on"] = True
        done.append("🪙 демо мемкоинов")
    m = b.mentors
    for st, label in ((m.st, "🎓 учёба у трейдеров"), (m.train.st, "🏋️ тренировка"),
                      (m.scout.st, "🔭 разведчик трейдеров")):
        if not st.get("on"):
            st["on"] = True
            done.append(label)
    if m.gemini.configured and not m.live.st.get("on"):
        m.live.st["on"] = True
        done.append("🎥 эфиры вживую")
    if not m.live.mirror.st.get("on"):
        m.live.mirror.st["on"] = True
        done.append("🪞 повтор стримеров")
    pulse = getattr(b, "pulse", None)
    if pulse is not None and not pulse.st.get("on"):
        pulse.st["on"] = True
        done.append("⚡ пульс рынка")
    if not t.state.live:
        if not t.state.auto and not b.scalper.on:
            b.scalper.turn_on()
            done.append("🤖 автопилот в тесте")
        sp = b.spread
        if not sp.st["on"] and not sp.problems():
            sp.st["on"] = True
            sp.st["paper"] = True
            done.append("⚖️ спред на бумаге")
    return done


def keyboard(b):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Включить всё демо", callback_data="dm:on"),
         InlineKeyboardButton(text="🔄 Обновить", callback_data="dm:show")],
        [InlineKeyboardButton(text="🔁 Тест автопилота заново", callback_data="dm:reset")],
    ])


def enable_text(done: list[str]) -> str:
    if not done:
        return "✅ Всё демо уже включено. Почему у какого-то нет сделок — написано в списке ниже."
    return ("▶️ <b>Включил демо</b>: " + ", ".join(done) + ".\nВсё на виртуальных деньгах, настоящие ордера не "
            "отправляются. Первые сделки — когда рынок даст вход, который проходит все проверки.")


def register(b) -> None:
    from aiogram import F
    from aiogram.filters import Command
    from aiogram.types import CallbackQuery, Message

    dp = b.dp

    @dp.message(Command("demos", "demo_all", "alldemo"))
    async def cmd_demos(message: Message) -> None:
        if not b._allowed_user(message.from_user.id if message.from_user else None):
            return
        await b._send(message.chat.id, text(b), keyboard(b))

    @dp.callback_query(F.data.startswith("dm:"))
    async def cb_demos(cb: CallbackQuery) -> None:
        if not b._allowed_user(cb.from_user.id):
            await cb.answer()
            return
        action = cb.data.split(":", 1)[1]
        chat_id = cb.message.chat.id if cb.message else cb.from_user.id
        if action != "show" and not b.is_owner(cb.from_user.id):
            await cb.answer("Менять может только владелец", show_alert=True)
            return
        await cb.answer()
        head = ""
        if action == "on":
            done = enable_all(b)
            b.save_trading()
            if done:
                await b._backup_now()
            head = enable_text(done) + "\n\n"
        elif action == "reset":
            if b.trader.state.live:
                head = "Сейчас реальный режим — тестовый баланс не сбрасываю (/test, чтобы вернуться в тест).\n\n"
            else:
                n = b.scalper.paper_reset()
                b.save_trading()
                head = ("🔁 Тест автопилота начат заново: тестовый баланс снова с реального (или виртуальных $10)"
                        + (f", закрыто тестовых позиций: {n}" if n else "") + ". Опыт лаборатории сохранён.\n\n")
        await b._send(chat_id, head + text(b), keyboard(b))
