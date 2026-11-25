"""🖥 Резервный хостинг (v50): бот сам переезжает на запасной сервер, когда основной умер, и возвращается обратно.

Зачем. Бесплатный Render усыпляет сервис до 1-го числа, если кончились 5 ГБ трафика. Можно держать вторую копию
бота на другом бесплатном хостинге (например, Raven Host) — она ждёт и включается сама, если основная пропала.

Главная опасность — две копии сразу. Тогда обе читают Telegram (конфликт getUpdates) и обе торгуют одними ключами
(двойные покупки). Поэтому работает ровно одна копия — «ведущая», остальные ждут. Как они договариваются:

1. 💓 Ведущая раз в 2 минуты пишет отметку в подпись закреплённого бэкапа (он и так лежит в чате владельца):
   «#hb render 1 1763550000» — кто работает, его приоритет и время. Это один маленький запрос — почти без трафика.
2. ⏳ Запасная раз в минуту читает подпись. Отметка свежая — ждёт и ничего не делает (ни бирж, ни Telegram-команд).
   Отметки нет дольше 8 минут — основная умерла: запасная пишет своё имя, через пару секунд перепроверяет
   (вдруг одновременно проснулась ещё одна) и запускается. Состояние (демо-счета, настройки, журнал сделок)
   берёт из того же бэкапа — как после обычного перезапуска.
3. 🏠 Основная вернулась (например, 1-го числа Render снова работает). У неё приоритет выше (меньше число),
   поэтому она не запускается сразу, а ставит «заявку» (#claim). Запасная видит заявку, сохраняет бэкап,
   пишет «свободно» (#hb - …) и засыпает обратно. Основная видит «свободно» и запускается. Без двойной работы.
4. 🛡 Если что-то пошло не так и всё же работают две — ведущая на каждом шаге проверяет, не записал ли кто-то
   своё имя новее её отметки, и тогда уступает сама.

Настройка (переменные окружения или файл .env на запасном хостинге):
    HOST_NAME          имя копии: render / raven / … (на Render — «render» само)
    FAILOVER_PRIORITY  1 — основная (по умолчанию на Render), 2 — запасная (по умолчанию в остальных местах)
    FAILOVER=0         выключить (по умолчанию включено; без запасной копии ничего не меняется)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramUnauthorizedError

from .cloud import (BACKUP_CAPTION, BACKUP_FILENAME, OLD_BACKUP_FILENAMES, cloud_port, public_url, self_ping_loop,
                    set_status, start_health_server)

if TYPE_CHECKING:
    from .telegram_bot import ArbitrageBot

log = logging.getLogger(__name__)

HB_SEC = 120          # как часто ведущая копия ставит отметку
STALE_SEC = 8 * 60    # нет отметки дольше — ведущая считается умершей
LEGACY_STALE_SEC = 40 * 60  # бэкап без отметки (бот v49 и старше): судим по времени последней правки
WAIT_SEC = 60         # как часто запасная копия проверяет отметку
SETTLE_SEC = 6        # пауза перед перепроверкой после захвата (вдруг проснулись две запасные сразу)
FREE = "-"            # «свободно»: ведущая сама уступила место
# Локальные файлы состояния: после переезда с другого хостинга они устарели — берём свежий бэкап
LOCAL_STATE_FILES = ("demo.json", "settings.json", "trading.json", "copytrade.json", "subscribers.json")

HB_RE = re.compile(r"#hb (\S+) (\d+) (\d+)")
CLAIM_RE = re.compile(r"#claim (\S+) (\d+) (\d+)")


def _env(env=None):
    return os.environ if env is None else env


def host_name(env=None) -> str:
    """Имя этой копии: HOST_NAME, на Render — «render», иначе имя машины (коротко, только буквы и цифры)."""
    env = _env(env)
    raw = (env.get("HOST_NAME") or "").strip() or ("render" if env.get("RENDER") else socket.gethostname())
    name = re.sub(r"[^a-z0-9_.]+", "", raw.lower().replace("-", "_"))[:20]
    return name if name and name != FREE else "host"


def priority(env=None) -> int:
    """Чем меньше, тем главнее. Render — 1 (основной), остальные — 2 (запасные), если не задано явно."""
    env = _env(env)
    default = 1 if env.get("RENDER") else 2
    try:
        return max(1, min(int(env.get("FAILOVER_PRIORITY") or default), 9))
    except ValueError:
        return default


def enabled(env=None) -> bool:
    return (_env(env).get("FAILOVER") or "1").strip().lower() not in ("0", "false", "no", "off", "нет")


@dataclass
class Lease:
    holder: str              # кто ведущий (FREE — место свободно)
    prio: int
    ts: float                # когда ведущий ставил отметку
    claim: str = ""          # кто из более главных просит место
    claim_prio: int = 0
    claim_ts: float = 0.0
    legacy: bool = False     # подписи с отметкой нет (старый бот): ts — время последней правки бэкапа


def _ts(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, datetime):
        return v.timestamp()
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse(caption: str | None, msg_ts: float = 0.0) -> Lease | None:
    """Отметка из подписи бэкапа. Нет отметки — «старый» бэкап (судим по времени правки), нет и времени — None."""
    m = HB_RE.search(caption or "")
    if not m:
        return Lease("?", 0, msg_ts, legacy=True) if msg_ts else None
    lease = Lease(m.group(1), int(m.group(2)), float(m.group(3)))
    c = CLAIM_RE.search(caption or "")
    if c:
        lease.claim, lease.claim_prio, lease.claim_ts = c.group(1), int(c.group(2)), float(c.group(3))
    return lease


def decide_waiter(lease: Lease | None, me: str, prio: int, now: float) -> str:
    """Что делать копии, которая ещё не работает: take (запускаться) | claim (просить место) | wait (ждать)."""
    if lease is None:
        return "take"  # бэкапа нет — новая установка, никого нет
    if lease.legacy:
        return "take" if now - lease.ts > LEGACY_STALE_SEC else "wait"
    if lease.holder == me:
        return "take"  # это мы и были (перезапуск или обновление)
    if lease.holder == FREE:
        mine = not lease.claim or lease.claim == me or now - lease.claim_ts > STALE_SEC
        return "take" if mine else "wait"
    if now - lease.ts > STALE_SEC:
        return "take"
    if prio < lease.prio:
        return "claim"
    return "wait"


def decide_leader(lease: Lease | None, me: str, prio: int, last_write: float, now: float) -> str:
    """Что делать работающей копии: keep (работать дальше) | taken (кто-то занял место) | yield (уступить главной)."""
    if lease is None or lease.legacy:
        return "keep"
    if lease.holder not in (me, FREE) and lease.ts > last_write + 1:
        return "taken"
    if lease.claim and lease.claim != me and lease.claim_prio < prio and now - lease.claim_ts < STALE_SEC:
        return "yield"
    return "keep"


def drop_local_state(data_dir: Path) -> int:
    """Удалить устаревшие локальные файлы состояния (после переезда с другого хостинга). Возвращает, сколько удалено."""
    n = 0
    for name in LOCAL_STATE_FILES:
        p = data_dir / name
        try:
            if p.exists():
                p.unlink()
                n += 1
        except OSError as e:
            log.warning("Не удалось удалить %s: %s", p, e)
    return n


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec} с" if sec < 120 else (f"{sec // 60} мин" if sec < 7200 else f"{sec // 3600} ч")


class Failover:
    def __init__(self, chat_id: int | None, env=None) -> None:
        self.chat_id = chat_id
        self.me = host_name(env)
        self.prio = priority(env)
        self.on = enabled(env) and chat_id is not None
        self.role = "starting"           # starting | waiting | leader | yielding
        self.prev_holder: str | None = None  # кто работал до нас (для сообщения «переехал»)
        self.waited_sec = 0.0
        self.last_write = 0.0
        self.last_lease: Lease | None = None
        self.reason = ""
        self.release_needed = False  # уступаем главной копии: перед сном сохранить бэкап и написать «свободно»

    # --- подпись бэкапа ---------------------------------------------------------------------------

    def caption(self, holder: str | None = None, now: float | None = None, claim: Lease | None = None) -> str:
        """Подпись закреплённого бэкапа: обычный текст + кто работает + машинная отметка."""
        now = time.time() if now is None else now
        holder = self.me if holder is None else holder
        human = ("🖥 Сейчас бот работает на: " + holder) if holder != FREE else "🖥 Бот переезжает на другой хостинг…"
        human += time.strftime(" · отметка %H:%M UTC", time.gmtime(now))
        tag = f"#hb {holder} {self.prio if holder == self.me else (claim.prio if claim else 0)} {int(now)}"
        if claim is not None and claim.claim:
            tag += f" #claim {claim.claim} {claim.claim_prio} {int(claim.claim_ts)}"
        return f"{BACKUP_CAPTION}\n{human}\n{tag}"

    def backup_caption(self) -> str:
        """Для TelegramBackup.save: при каждом сохранении бэкапа подпись = свежая отметка ведущего."""
        if self.role == "yielding":
            return self.caption(FREE, claim=self.last_lease)
        self.last_write = time.time()
        return self.caption(now=self.last_write)

    async def read(self, bot) -> tuple[Lease | None, int | None]:
        chat = await bot.get_chat(self.chat_id)
        p = chat.pinned_message
        if not p or not p.document or p.document.file_name not in (BACKUP_FILENAME, *OLD_BACKUP_FILENAMES):
            return None, None
        lease = parse(p.caption, _ts(p.edit_date) or _ts(p.date))
        self.last_lease = lease
        return lease, p.message_id

    async def _edit(self, bot, message_id: int, caption: str) -> None:
        try:
            await bot.edit_message_caption(chat_id=self.chat_id, message_id=message_id, caption=caption)
        except Exception as e:
            if "not modified" not in str(e):
                raise

    # --- ожидание (запасная копия) ----------------------------------------------------------------

    async def wait_turn(self, bot, sleep=asyncio.sleep, clock=time.time, on_wait=None) -> None:
        """Ждать, пока не наша очередь работать. Ничего не торгует и не читает команды — только подпись бэкапа."""
        if not self.on:
            self.role = "leader"
            return
        started, fails, announced = clock(), 0, False
        while True:
            try:
                lease, mid = await self.read(bot)
                fails = 0
            except TelegramUnauthorizedError:
                raise  # токен неверный — main.py покажет страницу с подсказкой
            except Exception as e:
                fails += 1
                log.warning("Резерв: не могу прочитать отметку (%s), попытка %d", e, fails)
                if fails >= 3:  # без Telegram бот всё равно не работает — не держим его взаперти
                    break
                await sleep(WAIT_SEC)
                continue
            now = clock()
            act = decide_waiter(lease, self.me, self.prio, now)
            if act == "take":
                if lease is not None and not lease.legacy and lease.holder != FREE:
                    self.prev_holder = lease.holder
                elif lease is not None and lease.holder == FREE:
                    self.prev_holder = "?"
                if mid is None:
                    break
                await self._edit(bot, mid, self.caption(now=now))
                self.last_write = now
                await sleep(SETTLE_SEC + random.random() * 4)
                try:
                    again, _ = await self.read(bot)
                except Exception:
                    again = None
                if again is None or again.legacy or again.holder == self.me:
                    break
                log.info("Резерв: одновременно проснулась копия %s — уступаю", again.holder)
                continue
            if act == "claim" and mid is not None and lease is not None:
                if lease.claim != self.me or now - lease.claim_ts > HB_SEC:
                    claim = Lease(lease.holder, lease.prio, lease.ts, self.me, self.prio, now)
                    await self._edit(bot, mid, self.caption(lease.holder, now=lease.ts, claim=claim))
                    log.info("Резерв: %s главнее — прошу место у %s", self.me, lease.holder)
            if not announced:
                announced = True
                self.role = "waiting"
                who = lease.holder if lease and not lease.legacy else "другой хостинг"
                log.info("Резерв: работает %s, копия %s (приоритет %d) ждёт", who, self.me, self.prio)
                set_status("starting", f"Запасная копия «{self.me}»: ждёт (сейчас работает {who})",
                           ["Это нормально: работает только одна копия бота. Эта включится сама, если основная пропадёт "
                            "больше чем на 8 минут."])
                if on_wait is not None:
                    await on_wait()
            await sleep(WAIT_SEC)
        self.role = "leader"
        self.waited_sec = clock() - started
        log.info("Резерв: копия %s ведущая (до неё работал: %s)", self.me, self.prev_holder or "никто")

    @property
    def moved(self) -> bool:
        """Переехали с другого хостинга: локальные файлы устарели, а владельцу стоит сказать."""
        return self.prev_holder not in (None, self.me)

    def moved_text(self) -> str:
        if self.prio == 1 and self.prev_holder == "?":
            return (f"🖥 <b>Бот вернулся на основной хостинг «{self.me}»</b>. Запасная копия сохранила бэкап и уснула — "
                    "демо-счета, настройки и журнал на месте. Подробно: /host")
        return (f"🖥 <b>Бот переехал на хостинг «{self.me}»</b>: копия «{self.prev_holder}» не отвечала "
                f"{STALE_SEC // 60}+ мин (например, кончился лимит) или уступила место. Демо-счета, настройки и журнал "
                "взяты из бэкапа (последние минуты до остановки могли не успеть сохраниться). Когда основная вернётся, "
                "бот сам переедет обратно. Подробно: /host")

    # --- ведущая копия ----------------------------------------------------------------------------

    async def beat(self, b: ArbitrageBot, now: float | None = None) -> str:
        """Отметка ведущего. Возвращает решение: keep | taken | yield."""
        if self.role == "yielding":
            return "yield"  # уже уступили — больше ничего не пишем (сторож мог перезапустить цикл)
        if not self.on or b.backup is None:
            return "keep"
        if b.backup.message_id is None:
            await b._backup_now()  # бэкапа ещё нет — создаём (подпись с отметкой поставит backup_caption)
            return "keep"
        lease, _mid = await self.read(b.bot)
        now = time.time() if now is None else now
        act = decide_leader(lease, self.me, self.prio, self.last_write, now)
        if act == "keep":
            await self._edit(b.bot, b.backup.message_id, self.caption(now=now))
            self.last_write = now
            return act
        other = (lease.holder if act == "taken" else lease.claim) if lease else "?"
        self.reason = other
        self.role = "yielding"
        self.release_needed = act == "yield"  # «taken»: место уже заняли — подпись не трогаем
        log.warning("Резерв: %s → уступаю место копии %s", act, other)
        b.yielded = True
        text = (f"🖥 <b>Передаю работу копии «{other}»</b> (основной хостинг снова в строю). Сохраняю бэкап, "
                "эта копия засыпает и включится сама, если та пропадёт.") if act == "yield" else \
            (f"🖥 <b>Копия «{other}» заняла место</b> — эта связь с Telegram пропадала. Чтобы не работать вдвоём, "
             "эта копия засыпает.")
        try:
            await b.notify(text)
        except Exception:
            pass
        await b.dp.stop_polling()
        return act

    async def release(self, b: ArbitrageBot) -> None:
        """Уступить место: последний бэкап и подпись «свободно» (её ждёт главная копия)."""
        if b.backup is None:
            return
        self.role = "yielding"
        try:
            await b.backup.save(b._state())
            if b.backup.message_id:
                await self._edit(b.bot, b.backup.message_id, self.caption(FREE, claim=self.last_lease))
        except Exception as e:
            log.warning("Резерв: не удалось освободить место: %s", e)

    def status_text(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if not self.on:
            return "🖥 Резервный хостинг выключен (FAILOVER=0 или не задан ALLOWED_USER_IDS)."
        lines = [f"🖥 <b>Хостинг</b>: эта копия — <b>{self.me}</b> "
                 f"({'основная' if self.prio == 1 else 'запасная'}, приоритет {self.prio}), роль: {self.role}"]
        if self.last_write:
            lines.append(f"Последняя отметка «я работаю»: {_ago(now - self.last_write)} назад")
        if self.moved:
            lines.append(f"Переехала сюда с «{self.prev_holder}» (ждала {_ago(self.waited_sec)}).")
        lines.append("Если настроен запасной хостинг (README → «🖥 Запасной хостинг»), копия там включится сама, когда "
                     f"эта пропадёт больше чем на {STALE_SEC // 60} мин, и уступит место, когда основная вернётся.")
        return "\n".join(lines)


async def standby(token: str, fo: Failover) -> None:
    """Для main.py: дождаться своей очереди. Пока ждём — держим /health (хостинг видит, что сервис жив)
    и самопинг на Render (иначе бесплатный сервис уснёт и не заметит, что пора работать)."""
    from aiogram import Bot

    bot = Bot(token)
    held: list = []

    async def on_wait() -> None:
        port = cloud_port()
        if port:
            held.append(await start_health_server(port))
            if public_url():
                held.append(asyncio.create_task(self_ping_loop(public_url())))

    try:
        await fo.wait_turn(bot, on_wait=on_wait)
    finally:
        for h in held:
            if isinstance(h, asyncio.Task):
                h.cancel()
            else:
                await h.cleanup()
        await bot.session.close()


async def failover_loop(b: ArbitrageBot) -> None:
    """Ведущая копия: отметка раз в 2 минуты; если надо уступить — останавливает бота (main.py вернёт его в ожидание)."""
    fo = b.failover
    while True:
        await asyncio.sleep(HB_SEC)
        b._beats["failover"] = time.time()
        try:
            if await fo.beat(b) != "keep":
                await asyncio.Event().wait()  # не выходим: сторож перезапустил бы цикл; бот сейчас остановится
        except Exception as e:
            log.warning("Резерв: отметка не поставлена: %s", e)


def register(b: ArbitrageBot) -> None:
    from aiogram.filters import Command
    from aiogram.types import Message

    @b.dp.message(Command("host", "хост"))
    async def cmd_host(message: Message) -> None:
        if not b._allowed(message):
            return
        await b._send(message.chat.id, b.failover.status_text())
