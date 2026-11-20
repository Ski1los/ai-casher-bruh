"""«Проверка всего» (/doctor): что работает, что нет и что с этим делать.

Проверяет с самого сервера: доступность каждой биржи и задержку, ключи (права, срок действия,
деньги на счёте Funding, которых бот не видит), приложение, резервную копию, ИИ, Phantom и память.
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

from .cloud import version_text
from .memory import RENDER_RAM_MB, memory_line, rss_mb
from .trading import STABLES, days_left, human_error, is_geo_block

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
    """Многие биржи держат «кошелёк финансирования» отдельно: деньги там бот не видит и не торгует ими."""
    ex = b.scanner.exchanges[name]
    accounts = (getattr(ex, "options", None) or {}).get("accountsByType") or {}
    if "funding" not in accounts or str(accounts["funding"]).lower() == str(accounts.get("spot", "spot")).lower():
        return None  # у биржи нет отдельного счёта финансирования
    try:
        bal = await asyncio.wait_for(ex.fetch_balance({"type": "funding"}), EXCHANGE_TIMEOUT)
    except Exception:
        return None
    total = bal.get("total") or {}
    stable = sum(float(total.get(c) or 0) for c in STABLES)
    others = [c for c, v in total.items() if v and float(v) > 0 and c not in STABLES]
    if stable < 0.5 and not others:
        return None
    what = f"${stable:.2f} в стейблкоинах" + (f" и {', '.join(others[:5])}" if others else "")
    where = {"bybit": "Bybit → Активы → Перевод → со счёта Funding на Unified Trading",
             "bingx": "BingX → Активы → Перевод → со счёта Fund на Spot"}.get(
        name, "в приложении биржи: Активы → Перевод → с Funding на спотовый (торговый) счёт")
    return Check("Ключи", f"{b.scanner.display_name(name)}: счёт Funding", "warn",
                 f"там лежит {what}. Бот видит и торгует только торговым счётом. Переведи: {where}")


def _key_info_checks(b: ArbitrageBot, name: str, info: dict, now: float) -> list[Check]:
    title = b.scanner.display_name(name)
    out = []
    if info.get("error"):
        return [Check("Ключи", f"{title}: права ключа", "info", f"не удалось прочитать: {info['error']}")]
    if info.get("withdraw"):
        out.append(Check("Ключи", f"{title}: право вывода", "bad",
                         "у ключа включён вывод (Withdraw). Удали ключ и создай новый только со спотовой торговлей"))
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


async def run_doctor(b: ArbitrageBot) -> list[Check]:
    checks: list[Check] = []
    now = time.time()
    names = list(b.scanner.exchanges)

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
                                        f"(биржа принимает ордер через API от $5 на каждый шаг). Пополни торговый счёт до $10–20"))
            except Exception as e:
                checks.append(Check("Ключи", title, "warn", f"ключ принят, но баланс не прочитан: {human_error(e)}"))
            checks += _key_info_checks(b, name, t.key_info.get(name, {}), now)
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
    checks.append(_auto_check(b))

    # --- сервис --------------------------------------------------------------
    url = b.app_url()
    checks.append(Check("Сервис", "Приложение", "ok" if url else "warn",
                        f"открывается по кнопке App: {url}" if url else
                        "нет https-адреса: на Render он появится сам, на другом хостинге укажи PUBLIC_URL"))
    checks.append(Check("Сервис", "Резервная копия", "ok" if b.backup else "warn",
                        "сохраняется в Telegram: настройки, демо, журнал и графики переживут перезапуск" if b.backup
                        else "выключена: заполни ALLOWED_USER_IDS, иначе после перезапуска всё обнулится"))
    if b.ai.configured:
        titles = ", ".join(sorted({ep.title for ep in b.ai.client.endpoints})) or "подключён"
        checks.append(Check("Сервис", "ИИ", "ok", f"подключён: {titles}"))
    else:
        checks.append(Check("Сервис", "ИИ", "info", "не подключён (по желанию): бесплатный ключ Groq, см. /ai"))
    checks.append(Check("Сервис", "Версия бота", "info", version_text()))
    mem = rss_mb()
    checks.append(Check("Сервис", "Память", "ok" if mem < RENDER_RAM_MB * 0.75 else "warn",
                        memory_line() + " бесплатного Render"
                        + ("" if mem < RENDER_RAM_MB * 0.75 else ". Нажми /clean, чтобы освободить кэши")))

    if b.tinvest.configured:
        snap = await b.tinvest.get(force=True)
        if snap and snap.get("error"):
            checks.append(Check("Сервис", "Т-Инвестиции", "bad", snap["error"]))
        elif snap:
            checks.append(Check("Сервис", "Т-Инвестиции", "ok",
                                f"счетов: {len(snap['accounts'])}, портфель ≈ {snap['total']:,.0f} ₽ (только просмотр)".replace(",", " ")))
    else:
        checks.append(Check("Сервис", "Т-Инвестиции", "info", "не подключены (по желанию): /tinvest"))

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
    return checks


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


def doctor_text(checks: list[Check]) -> str:
    lines = ["🩺 <b>Проверка всего</b>"]
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
