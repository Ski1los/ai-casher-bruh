"""«Проверка всего» (/doctor): что работает, что нет и что с этим делать.

Проверяет с самого сервера: доступность каждой биржи и задержку, ключи (права, срок действия,
деньги на счёте Funding, которых бот не видит), КАЖДЫЙ ИИ вживую (короткий вопрос всем сразу),
полный автомат и его источники данных (GMGN, кошелёк мемкоинов), приложение, резервную копию, Phantom и память.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import ccxt.async_support as ccxt

from . import updater
from .cloud import version_text
from .memory import RENDER_RAM_MB, memory_line, rss_mb
from .trading import days_left, human_error, is_geo_block

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

EXCHANGE_TIMEOUT = 25
ICONS = {"ok": "✅", "warn": "⚠️", "bad": "❌", "info": "ℹ️"}


@dataclass
class Check:
    group: str
    title: str
    status: str  # ok | warn | bad | info
    text: str


def geo_hint(name: str) -> str:
    if name == "bybit":
        return ("Bybit не пускает сервер из этой страны. Попробуй запасной домен: в Render → Environment добавь "
                "BYBIT_HOSTNAME = bytick.com и сохрани. Если не поможет, торгуй через BingX: он работает с того же сервера.")
    return (f"Биржа не пускает сервер из этой страны. Можно попробовать переменную {name.upper()}_HOSTNAME с запасным "
            f"доменом биржи. Сканер тем временем работает со второй биржей.")


async def _check_exchange(b: ArbitrageBot, name: str) -> Check:
    ex = b.scanner.exchanges[name]
    title = b.scanner.display_name(name)
    symbol = f"BTC/{b.settings.quote}"
    try:
        await asyncio.wait_for(b.scanner._ensure_markets(name, ex), EXCHANGE_TIMEOUT)
        markets = ex.markets or {}
        spot = sum(1 for m in markets.values() if m.get("spot", True))
        t0 = time.perf_counter()
        if symbol in markets:
            await asyncio.wait_for(ex.fetch_ticker(symbol), EXCHANGE_TIMEOUT)
        ms = (time.perf_counter() - t0) * 1000
        speed = "быстро" if ms < 400 else "нормально" if ms < 1200 else "медленно"
        return Check("Биржи", title, "ok" if ms < 1200 else "warn", f"отвечает за {ms:.0f} мс ({speed}), пар: {spot}")
    except Exception as e:
        if is_geo_block(e):
            return Check("Биржи", title, "bad", geo_hint(name))
        return Check("Биржи", title, "bad", human_error(e))


async def _funding_hint(b: ArbitrageBot, name: str) -> Check | None:
    """Деньги на других счетах биржи (Funding/Фонд, фьючерсы, боты): бот их не видит и не торгует ими."""
    try:
        accounts = await asyncio.wait_for(b.trader.other_accounts(name), EXCHANGE_TIMEOUT)
    except Exception:
        return None
    hint = b.trader.where_money_text(name, accounts)
    if not hint:
        return None
    return Check("Ключи", f"{b.scanner.display_name(name)}: деньги на другом счёте", "warn", hint)


def _key_info_checks(b: ArbitrageBot, name: str, info: dict, now: float) -> list[Check]:
    title = b.scanner.display_name(name)
    out = []
    if info.get("error"):
        return [Check("Ключи", f"{title}: права ключа", "info", f"не удалось прочитать: {info['error']}")]
    if info.get("withdraw"):
        if not b.trader.transfers_allowed:
            out.append(Check("Ключи", f"{title}: право вывода", "bad",
                             "у ключа включён вывод (Withdraw), а переводы выключены: разреши их в /transfer "
                             "или создай ключ только со спотовой торговлей"))
        elif info.get("ip_bound") is False:
            out.append(Check("Ключи", f"{title}: право вывода", "bad",
                             "вывод включён без привязки к IP — привяжи IP сервера (/transfer help)"))
        else:
            out.append(Check("Ключи", f"{title}: право вывода", "ok", "включён для 🔁 переводов между биржами"))
    if name == "bybit":
        if info.get("read_only"):
            live = b.trader.state.live
            out.append(Check("Ключи", f"{title}: права", "bad" if live else "warn",
                             "ключ «только чтение»: баланс виден, торговать нельзя"
                             + (". В реальном режиме биржа отклонит ордера. bybit.com → API → Edit → Read-Write + "
                                "Spot Trade (без Withdraw)" if live else "")))
        elif not info.get("spot_trade"):
            out.append(Check("Ключи", f"{title}: права", "bad", "нет права Spot Trade: включи «Спот» в настройках ключа"))
        days = days_left(info, now)
        if info.get("ip_bound"):
            out.append(Check("Ключи", f"{title}: срок", "ok", "ключ привязан к IP и не истекает"))
        elif days is not None:
            date = time.strftime("%d.%m.%Y", time.gmtime(info["expires_at"]))
            status = "bad" if days <= 3 else "warn" if days <= 14 else "ok"
            out.append(Check("Ключи", f"{title}: срок", status,
                             f"ключ без привязки к IP работает до {date} (осталось {max(days, 0):.0f} дн.). "
                             f"Бот напомнит в чате за 2 недели до конца срока"))
    if name == "bingx":
        if "spot_trade" in info or "read" in info:  # права удалось прочитать
            if not info.get("spot_trade"):
                out.append(Check("Ключи", f"{title}: права", "warn" if info.get("read") else "bad",
                                 "нет права «Спотовая торговля»: баланс виден, торговать нельзя. Включи его в настройках ключа"))
        if info.get("ip_bound"):
            out.append(Check("Ключи", f"{title}: срок", "ok", "ключ привязан к IP и не удаляется"))
        else:
            out.append(Check("Ключи", f"{title}: срок", "ok",
                             "ключ без IP BingX удаляет после 14 дней БЕЗ запросов. Бот обращается к бирже каждые "
                             "15 минут, так что ключ остаётся живым, пока бот работает"))
    return out


async def _withdraw_key_checks(b: ArbitrageBot, name: str) -> list[Check]:
    """🔁 Отдельный ключ вывода (BINGX_WITHDRAW_API_KEY …): права и принимает ли биржа IP сервера."""
    tf = getattr(b, "transfers", None)
    if tf is None or name not in tf.wd_creds:
        return []
    title = f"{b.scanner.display_name(name)}: ключ вывода"
    try:
        info = await asyncio.wait_for(tf.wd_info(name, force=True), EXCHANGE_TIMEOUT)
    except Exception as e:
        return [Check("Ключи", title, "warn", f"не проверил: {human_error(e)}")]
    if info.get("error"):
        if info.get("ip_error"):
            ip, _ = await tf.check_ip()
            return [Check("Ключи", title, "bad", f"биржа не принимает IP сервера {ip or ''}: добавь его в белый список "
                                                 "ключа вывода (/transfer ip). Торговля это не задевает")]
        return [Check("Ключи", title, "bad", info["error"])]
    if info.get("known") is False:
        return [Check("Ключи", title, "warn", "ключ принят, но права биржа сообщила в незнакомом виде — "
                                              "пробный перевод покажет точно")]
    if not info.get("withdraw"):
        from .transfers import perms_seen
        return [Check("Ключи", title, "bad", f"у ключа нет права Withdraw (биржа сообщила: {perms_seen(info)}) — "
                                             "включи его в настройках ключа")]
    return [Check("Ключи", title, "ok", "работает: право вывода есть, IP сервера принят"
                  + ("" if tf.st["allowed"] else ". Переводы пока выключены: /transfer → «🔓 Разрешить»"))]


async def run_doctor(b: ArbitrageBot) -> list[Check]:
    checks: list[Check] = []
    now = time.time()
    names = list(b.scanner.exchanges)

    ai_task = asyncio.create_task(check_ai(b))  # ИИ отвечают до 30 с — проверяем их параллельно с биржами
    ex_checks = await asyncio.gather(*(_check_exchange(b, n) for n in names))
    checks += ex_checks
    if not names:
        checks.append(Check("Биржи", "Список бирж", "bad", "EXCHANGES пустой или с ошибками"))

    # --- ключи ---------------------------------------------------------------
    t = b.trader
    env_keys = {k[: -len("_API_KEY")].lower() for k in os.environ if k.endswith("_API_KEY") and os.environ[k].strip()}
    for name in sorted(env_keys - set(names)):
        if name in ccxt.exchanges:
            checks.append(Check("Ключи", name.upper(), "info",
                                f"бот работает только с Bybit и BingX, ключ {name.upper()}_API_KEY не используется. "
                                f"Его можно удалить в Render → Environment"))
    if not t.creds:
        checks.append(Check("Ключи", "API-ключи", "info",
                            "ключей нет: работают сигналы, демо и графики. Чтобы торговать: /keys"))
    else:
        await t.check_keys()
        await t.inspect_keys()
        for name in t.creds:
            title = b.scanner.display_name(name)
            status = t.key_status.get(name, "ok")
            if status != "ok":
                hint = geo_hint(name) if "не пускает сервер из его страны" in status else status
                checks.append(Check("Ключи", title, "bad", hint))
                continue
            try:
                rows = await asyncio.wait_for(t.balances(name), EXCHANGE_TIMEOUT)
                total = sum(r[3] or 0 for r in rows)
                checks.append(Check("Ключи", title, "ok", f"ключ работает, на торговом счёте ≈ ${total:.2f}"))
                need = t.min_start_usd(name)
                if total < need:
                    checks.append(Check("Ключи", f"{title}: деньги", "warn" if t.state.live else "info",
                                        f"≈ ${total:.2f} на торговом счёте, а для цикла из трёх шагов нужно от ${need:.2f} "
                                        f"(биржа принимает ордер через API от $5 на каждый шаг). "
                                        + ("Режиму $5 (/scalp) этого хватает: там всего два ордера." if total >= 5 else
                                           "Режиму $5 (/scalp) нужно от $5 на торговом счёте")))
            except Exception as e:
                checks.append(Check("Ключи", title, "warn", f"ключ принят, но баланс не прочитан: {human_error(e)}"))
            checks += _key_info_checks(b, name, t.key_info.get(name, {}), now)
            checks += await _withdraw_key_checks(b, name)
            hint = await _funding_hint(b, name)
            if hint:
                checks.append(hint)

    # --- торговля ------------------------------------------------------------
    s = b.settings
    mode = "🔴 реальный" if t.state.live else "🧪 тестовый"
    auto = "включена" if t.state.auto else "выключена"
    checks.append(Check("Торговля", "Режим", "info",
                        f"{mode}, автоторговля {auto}. Лимиты: ${s.max_trade_usd:g} на сделку, "
                        f"${s.daily_loss_usd:g} убытка в день, {s.daily_trades:g} сделок в день"))
    if t.state.paused_reason:
        checks.append(Check("Торговля", "Пауза", "warn", t.state.paused_reason))
    sc = b.scalper
    checks.append(Check("Торговля", "Режим $5", "ok" if sc.on else "info",
                        f"включён: {sc.last_reason or 'запускается'}" if sc.on else
                        "выключен. Торгует даже с $5: два ордера (купить на откате → продать с прибылью). /scalp on"))
    checks.append(_auto_check(b))
    if updater.outdated():
        checks.append(Check("Сервис", "Версия", "warn", updater.status_line() + ". Как обновить: /update"))
    elif updater.hook_problem():
        checks.append(Check("Сервис", "Самообновление", "warn", updater.hook_problem()))
    if b.duplicate_running():
        checks.append(Check("Сервис", "Две копии бота", "bad",
                            "тем же токеном пользуется второй сервис. Сделки на паузе, бот отвечает через раз, "
                            "бесплатные часы Render тратятся вдвое. Render → лишний сервис → Settings → Delete Web Service"))

    # --- сервис --------------------------------------------------------------
    url = b.app_url()
    checks.append(Check("Сервис", "Приложение", "ok" if url else "warn",
                        f"открывается по кнопке App: {url}" if url else
                        "нет https-адреса: на Render он появится сам, на другом хостинге укажи PUBLIC_URL"))
    checks.append(Check("Сервис", "Резервная копия", "ok" if b.backup else "warn",
                        "сохраняется в Telegram: настройки, демо, журнал и графики переживут перезапуск" if b.backup
                        else "выключена: заполни ALLOWED_USER_IDS, иначе после перезапуска всё обнулится"))
    checks.append(Check("Сервис", "Версия бота", "info", version_text()))
    mem = rss_mb()
    checks.append(Check("Сервис", "Память", "ok" if mem < RENDER_RAM_MB * 0.75 else "warn",
                        memory_line() + " бесплатного Render"
                        + ("" if mem < RENDER_RAM_MB * 0.75 else ". Нажми /clean, чтобы освободить кэши")))

    # --- Phantom и алерты ----------------------------------------------------
    if b.phantom.address:
        snap = await b.phantom_snapshot(force=True)
        if snap and snap.get("error"):
            checks.append(Check("Кошелёк", "Phantom", "bad", snap["error"]))
        elif snap:
            checks.append(Check("Кошелёк", "Phantom", "ok",
                                f"виден баланс ≈ ${snap['total_usd']:.2f}, монет: {len(snap['assets'])}. Только просмотр"))
    else:
        checks.append(Check("Кошелёк", "Phantom", "info", "не подключён (по желанию): /phantom АДРЕС"))
    checks.append(Check("Кошелёк", "Радар мемкоинов", "info",
                        "включён" if b.radar_chats else "выключен. Включить: /radar on (новые токены Solana после RugCheck)"))
    n_alerts = len(b.alerts.items)
    checks.append(Check("Кошелёк", "Ценовые алерты", "info",
                        f"активных: {n_alerts}" if n_alerts else "нет. Пример: /alert TON < 5"))
    checks += _automat_checks(b, now)
    try:
        checks += await ai_task
    except Exception as e:  # проверка ИИ не должна ронять всю проверку
        log.exception("doctor: ИИ")
        checks.append(Check("ИИ", "Проверка ИИ", "warn", f"не удалась: {type(e).__name__}"))
    return checks


# ---------------------------------------------------------------------------
# ИИ: каждый — вживую
# ---------------------------------------------------------------------------


def _ai_hint(provider: str, error: str) -> str:
    from .ai import provider_env_names
    low = error.lower()
    env = provider_env_names(provider)[0] if provider not in ("ovh", "llm7", "custom") else ""
    if "ключ" in low and ("не подходит" in low or "authorization" in low):
        return f" → скопируй ключ заново и вставь в Render → Environment → {env}" if env else ""
    if "лимит" in low or "закончились" in low or "подписка" in low:
        return " → бесплатный лимит на сегодня исчерпан, завтра заработает сам; пока отвечают другие ИИ"
    if "нет связи" in low or "не ответил" in low or "перегруж" in low:
        return " → сервис сейчас не отвечает, бот спросит другой ИИ; проверь позже"
    return ""


async def check_ai(b: ArbitrageBot) -> list[Check]:
    """Короткий вопрос всем ИИ одновременно: кто ответил, за сколько секунд и какой моделью."""
    from .ai import PROVIDERS
    out: list[Check] = []
    client = b.ai.client
    have = {ep.provider for ep in client.endpoints}
    if not client.endpoints:
        out.append(Check("ИИ", "ИИ-помощник", "warn", "ни одного ИИ нет. Бесплатно и без телефона: /council keys"))
    else:
        rows = await client.ping_all()
        working = 0
        for r in rows:
            name = r["title"] + (" (без ключа)" if r["keyless"] else "")
            model = f" · {r['model']}" if r["model"] else ""
            if r["ok"]:
                working += 1
                out.append(Check("ИИ", name, "ok", f"ответил за {r['sec']:.1f} с{model}"))
            elif r["paused"]:
                out.append(Check("ИИ", name, "info", f"на паузе из-за лимита ещё ~{r['paused']} с — не трогаю, "
                                                     "чтобы не тратить лимит"))
            else:
                out.append(Check("ИИ", name, "warn" if r["keyless"] else "bad",
                                 r["error"] + _ai_hint(r["provider"], r["error"])))
        total = len(rows)
        council = ("🧠 консилиум готов" if working >= 2 else
                   "для 🧠 консилиума нужно минимум 2 работающих ИИ")
        out.append(Check("ИИ", "Итог по ИИ", "ok" if working >= 2 else "warn" if working else "bad",
                         f"отвечают {working} из {total}: {council}"))
    free = [p["title"] for n, p in PROVIDERS.items() if n not in have and p.get("access") in ("nophone", "ru")
            and not p.get("free", "").startswith("платн")]
    if free:
        out.append(Check("ИИ", "Можно добавить бесплатно", "info",
                         ", ".join(free[:8]) + " — без телефона или по номеру РФ. Как: /council keys"))
    m = getattr(b, "mentors", None)
    gem = getattr(m, "gemini", None)
    if gem is not None:
        out.append(Check("ИИ", "🎥 Видео и эфиры (Gemini)", "ok" if gem.configured else "info",
                         "ключ есть: бот смотрит ролики и эфиры" if gem.configured else
                         "нет ключа Gemini: видео не разбираются (текст ИИ работает и без него)"))
    return out


# ---------------------------------------------------------------------------
# Полный автомат и его источники данных
# ---------------------------------------------------------------------------


def _automat_checks(b: ArbitrageBot, now: float) -> list[Check]:
    out: list[Check] = []
    auto = getattr(b, "autonomy", None)
    if auto is not None:
        try:
            st = auto.st
            if not st["on"]:
                out.append(Check("Автомат", "♾ Полный автомат", "info", "выключен. Включить: /autonomy"))
            else:
                ago = now - (st.get("last_tick") or 0)
                stale = bool(st.get("last_tick")) and ago > 3 * 3600
                engines = auto.engines_status()
                on = sum(1 for _, o, _ in engines if o)
                money = "на деньгах разрешён" if st.get("real") else "только без денег (обучение и демо)"
                out.append(Check("Автомат", "♾ Полный автомат", "warn" if stale else "ok",
                                 f"работает, {money}; учебных движков включено {on} из {len(engines)}"
                                 + (f". Последний обход {ago / 3600:.0f} ч назад — проверь, не спит ли сервис"
                                    if stale else "")))
        except Exception as e:  # у тестовых заглушек может не быть части полей
            log.debug("doctor: автомат: %s", e)
    nw = getattr(b, "news", None)
    if nw is not None:
        try:
            st = nw.st
            ok, total = nw.working()
            if not st["on"]:
                out.append(Check("Автомат", "📰 Новости", "info", "выключены: щита и пауз по новостям нет. Включить: /news on"))
            elif not nw.health:
                out.append(Check("Автомат", "📰 Новости", "info", "первый сбор новостей — через минуту после запуска"))
            else:
                bad = ok < total / 2
                out.append(Check("Автомат", "📰 Новости", "warn" if bad else "ok",
                                 f"источников работает {ok} из {total}, замеров влияния на цену: {st.get('measured', 0)}"
                                 + (". Многие не отвечают — список: /news src" if bad else "")))
        except Exception as e:
            log.debug("doctor: новости: %s", e)
    tr = getattr(b, "traffic", None)
    if tr is not None:
        try:
            from .traffic import gb
            tr.update(now)
            d = tr.status_dict(now)
            state = "ok" if d["level"] == 0 else "bad" if d["level"] >= 4 else "warn"
            out.append(Check("Сервис", "📶 Трафик (5 ГБ Render)" if tr.on_render else f"📶 Трафик ({tr.host})", state,
                             f"за месяц {gb(d['used'])} из бюджета {gb(d['budget'])}, прогноз {gb(d['projected'])} · "
                             f"режим: {d['level_name']}" + ("" if d["level"] == 0 else ". Подробно: /traffic")))
        except Exception as e:
            log.debug("doctor: трафик: %s", e)
    fo = getattr(b, "failover", None)
    if fo is not None and getattr(fo, "on", False):
        moved = f" · переехал с «{fo.prev_holder}»" if fo.moved else ""
        out.append(Check("Сервис", "🖥 Хостинг", "info",
                         f"работает на «{fo.me}» ({'основной' if fo.prio == 1 else 'запасной'}){moved}. "
                         "Если настроен запасной хостинг — он включится сам, когда эта копия пропадёт. Подробно: /host"))
    ntr = getattr(b, "newstrade", None)
    if ntr is not None:
        try:
            st = ntr.st
            n, w, pnl = ntr.totals(False)
            if not st["on"]:
                out.append(Check("Автомат", "🤖 Торговля по новостям", "info", "выключена. Включить: /newstrade on"))
            else:
                why = ntr.live_ready() if st["real"] else None
                out.append(Check("Автомат", "🤖 Торговля по новостям", "warn" if why else "ok",
                                 ("реальный режим" + (f", но сейчас демо: {why}" if why else "") if st["real"] else "демо")
                                 + f" · демо-счёт ${ntr.equity():.2f}, сделок {n}, в плюс {w}, открыто {len(st['open'])}"))
        except Exception as e:
            log.debug("doctor: торговля по новостям: %s", e)
    gmgn = getattr(b, "gmgn", None)
    if gmgn is not None:
        g = gmgn.status()
        if g.get("key_bad"):
            out.append(Check("Автомат", "GMGN", "bad", "ключ GMGN не принят: проверь GMGN_API_KEY в Render"))
        elif g.get("configured"):
            out.append(Check("Автомат", "GMGN", "warn" if g.get("error") else "ok",
                             f"ключ есть, запросов: {g.get('calls', 0)}"
                             + (f". Последняя ошибка: {str(g['error'])[:120]}" if g.get("error") else "")))
        else:
            out.append(Check("Автомат", "GMGN", "info", "без ключа: пульс рынка берёт данные с DexScreener"))
    meme = getattr(b, "meme", None)
    if meme is not None:
        if getattr(meme, "key_error", None):
            out.append(Check("Автомат", "Кошелёк мемкоинов", "bad", str(meme.key_error)[:200]))
        elif getattr(meme, "configured", False):
            out.append(Check("Автомат", "Кошелёк мемкоинов", "ok", "ключ горячего кошелька подключён"))
        else:
            out.append(Check("Автомат", "Кошелёк мемкоинов", "info",
                             "не подключён: мемкоины торгуются на демо и учатся. Как подключить: /meme"))
    return out


def _auto_check(b: ArbitrageBot) -> Check:
    t = b.trader
    if not t.state.auto:
        return Check("Торговля", "Автоторговля", "info", "выключена. Проверить безопасно: /test, затем /autotrade on")
    problem = t.auto_problem()
    st = t.auto_status
    if problem:
        return Check("Торговля", "Автоторговля", "bad" if problem[0] in ("blocked", "no_keys") else "warn",
                     f"включена, но не может торговать: {problem[1]}. Подробно: /why")
    if not st:
        return Check("Торговля", "Автоторговля", "info", "включена, ждёт первого скана")
    ago = time.time() - st["at"]
    last = "исполнила цикл" if st.get("executed") else (st.get("reason") or "ждёт подходящий цикл")
    stale = ago > max(300, b.settings.scan_interval_sec * 5)
    return Check("Торговля", "Автоторговля", "warn" if stale else "ok",
                 f"работает, последняя проверка {ago:.0f} с назад: {last}"
                 + (". Давно не было сканов: проверь, жив ли сервис" if stale else "") + ". Подробно: /why")


def summary(checks: list[Check]) -> dict:
    return {s: sum(1 for c in checks if c.status == s) for s in ("ok", "warn", "bad", "info")}


def doctor_text(checks: list[Check], title: str = "🩺 Проверка всего") -> str:
    lines = [f"<b>{html.escape(title)}</b>"]
    group = None
    for c in checks:
        if c.group != group:
            group = c.group
            lines.append(f"\n<b>{html.escape(group)}</b>")
        lines.append(f"{ICONS[c.status]} {html.escape(c.title)}: {html.escape(c.text)}")
    s = summary(checks)
    verdict = ("Всё в порядке 👌" if not s["bad"] and not s["warn"] else
               "Есть проблемы: исправь пункты с ❌" if s["bad"] else "Работает, но посмотри пункты с ⚠️")
    lines.append(f"\n<b>Итог:</b> ✅ {s['ok']} · ⚠️ {s['warn']} · ❌ {s['bad']}. {verdict}")
    return "\n".join(lines)


def doctor_dict(checks: list[Check]) -> dict:
    return {"checks": [asdict(c) for c in checks], "summary": summary(checks), "at": time.time()}
