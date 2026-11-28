"""🖥 v50: запасной хостинг — одна ведущая копия, отметка в подписи бэкапа, переезд туда и обратно."""

import asyncio
import io
from types import SimpleNamespace

import pytest

from bot import failover as fo_mod
from bot import traffic as tm
from bot.cloud import BACKUP_CAPTION, BACKUP_FILENAME, TelegramBackup
from bot.copytrade import CopyTrader
from bot.failover import FREE, Failover, Lease, decide_leader, decide_waiter, parse


class Tg:
    """Имитация Bot API: закреплённый бэкап с подписью, которую можно править."""

    def __init__(self, caption=None, date=1000.0, pinned=True):
        self.caption = caption
        self.date = date
        self.pinned = 7 if pinned else None
        self.files = {7: b"{}"}
        self.edits: list[str] = []
        self.sent: list[str] = []

    async def get_chat(self, chat_id):
        pm = None
        if self.pinned:
            pm = SimpleNamespace(message_id=self.pinned, caption=self.caption, date=self.date, edit_date=None,
                                 document=SimpleNamespace(file_name=BACKUP_FILENAME, file_id=str(self.pinned)))
        return SimpleNamespace(pinned_message=pm)

    async def edit_message_caption(self, chat_id, message_id, caption):
        assert message_id == self.pinned
        self.caption = caption
        self.edits.append(caption)

    async def edit_message_media(self, media, chat_id, message_id):
        self.caption = media.caption
        self.files[message_id] = media.media.data

    async def send_document(self, chat_id, file, caption=None, disable_notification=False):
        self.pinned = 8
        self.files[8] = file.data
        self.caption = caption
        return SimpleNamespace(message_id=8)

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        self.pinned = message_id

    async def download(self, document):
        return io.BytesIO(self.files[int(document.file_id)])


def make(me, prio, env_extra=None):
    env = {"HOST_NAME": me, "FAILOVER_PRIORITY": str(prio), **(env_extra or {})}
    return Failover(6069246522, env=env)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, sec):
        self.t += sec


# --- имена и разбор ---------------------------------------------------------------------------------------

def test_host_name_and_priority_defaults():
    assert fo_mod.host_name({"RENDER": "true"}) == "render"
    assert fo_mod.priority({"RENDER": "true"}) == 1
    assert fo_mod.host_name({"HOST_NAME": "Raven-Host!"}) == "raven_host"
    assert fo_mod.priority({}) == 2
    assert fo_mod.priority({"FAILOVER_PRIORITY": "abc"}) == 2
    assert fo_mod.priority({"FAILOVER_PRIORITY": "1"}) == 1
    assert fo_mod.enabled({}) and not fo_mod.enabled({"FAILOVER": "0"})
    assert not Failover(None, env={}).on  # нет владельца — нет бэкапа — нет резерва


def test_caption_roundtrip_and_claim():
    f = make("render", 1)
    cap = f.caption(now=1_763_550_000)
    assert cap.startswith(BACKUP_CAPTION) and "render" in cap
    lease = parse(cap)
    assert (lease.holder, lease.prio, lease.ts, lease.claim) == ("render", 1, 1_763_550_000, "")
    claim = Lease("raven", 2, 100, "render", 1, 150)
    lease = parse(f.caption("raven", now=100, claim=claim))
    assert (lease.holder, lease.prio, lease.claim, lease.claim_prio, lease.claim_ts) == ("raven", 2, "render", 1, 150)
    assert parse("старая подпись", 500).legacy and parse(None) is None


def test_decide_waiter():
    now = 10_000
    fresh = Lease("render", 1, now - 60)
    assert decide_waiter(None, "raven", 2, now) == "take"
    assert decide_waiter(fresh, "raven", 2, now) == "wait"
    assert decide_waiter(Lease("render", 1, now - fo_mod.STALE_SEC - 1), "raven", 2, now) == "take"
    assert decide_waiter(Lease("raven", 2, now - 60), "render", 1, now) == "claim"  # главная вернулась
    assert decide_waiter(Lease("raven", 2, now - 60), "raven", 2, now) == "take"   # это мы (перезапуск)
    assert decide_waiter(Lease(FREE, 2, now, "render", 1, now), "render", 1, now) == "take"
    assert decide_waiter(Lease(FREE, 2, now, "render", 1, now), "other", 3, now) == "wait"
    # старый бэкап без отметки: ждём, пока он правился недавно (бот v49 ещё жив)
    assert decide_waiter(Lease("?", 0, now - 600, legacy=True), "raven", 2, now) == "wait"
    assert decide_waiter(Lease("?", 0, now - fo_mod.LEGACY_STALE_SEC - 1, legacy=True), "raven", 2, now) == "take"


def test_decide_leader():
    now = 10_000
    assert decide_leader(Lease("raven", 2, now - 10), "raven", 2, now - 10, now) == "keep"
    assert decide_leader(Lease("raven", 2, now - 10, "render", 1, now - 5), "raven", 2, now - 10, now) == "yield"
    assert decide_leader(Lease("raven", 2, now - 10, "x", 3, now - 5), "raven", 2, now - 10, now) == "keep"
    assert decide_leader(Lease("other", 2, now - 5), "raven", 2, now - 100, now) == "taken"
    assert decide_leader(None, "raven", 2, 0, now) == "keep"


# --- ожидание ---------------------------------------------------------------------------------------------

def test_standby_waits_while_primary_alive_then_takes_over():
    clock = Clock(100_000)
    tg = Tg(make("render", 1).caption(now=clock.t))
    f = make("raven", 2)
    waits = []

    async def on_wait():
        waits.append(clock.t)

    asyncio.run(f.wait_turn(tg, sleep=clock.sleep, clock=clock, on_wait=on_wait))
    assert f.role == "leader" and f.prev_holder == "render" and f.moved
    assert parse(tg.caption).holder == "raven"
    assert len(waits) == 1  # /health поднят один раз, пока ждали
    assert clock.t - 100_000 >= fo_mod.STALE_SEC  # ждал, пока отметка Render не устарела
    assert "переехал" in f.moved_text()


def test_restart_on_same_host_takes_immediately():
    clock = Clock(100_000)
    tg = Tg(make("render", 1).caption(now=clock.t - 30))
    f = make("render", 1)
    asyncio.run(f.wait_turn(tg, sleep=clock.sleep, clock=clock))
    assert f.role == "leader" and not f.moved
    assert clock.t - 100_000 < 20


def test_fresh_install_without_backup_takes():
    f = make("render", 1)
    asyncio.run(f.wait_turn(Tg(pinned=False), sleep=Clock(0).sleep, clock=Clock(0)))
    assert f.role == "leader"


def test_two_standbys_at_once_only_one_wins():
    clock = Clock(100_000)
    tg = Tg(make("render", 1).caption(now=clock.t - fo_mod.STALE_SEC - 5))
    f = make("raven", 2)
    calls = {"n": 0}
    orig = tg.edit_message_caption

    async def edit(chat_id, message_id, caption):
        await orig(chat_id, message_id, caption)
        calls["n"] += 1
        if calls["n"] == 1:  # сразу после нас записалась другая запасная копия
            tg.caption = make("pella", 2).caption(now=clock.t)

    tg.edit_message_caption = edit
    stop = {"n": 0}

    async def sleep(sec):
        await clock.sleep(sec)
        stop["n"] += 1
        if stop["n"] > 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(f.wait_turn(tg, sleep=sleep, clock=clock))
    assert parse(tg.caption).holder == "pella" and f.role == "waiting"


def test_telegram_down_does_not_lock_bot_forever():
    class Down(Tg):
        async def get_chat(self, chat_id):
            raise OSError("нет сети")

    clock = Clock(0)
    f = make("raven", 2)
    asyncio.run(f.wait_turn(Down(), sleep=clock.sleep, clock=clock))
    assert f.role == "leader"


# --- возвращение главной копии ----------------------------------------------------------------------------

class FakeBot:
    def __init__(self, tg, failover):
        self.bot = tg
        self.failover = failover
        self.backup = TelegramBackup(tg, 6069246522)
        self.backup.message_id = tg.pinned
        self.backup.caption_fn = failover.backup_caption
        self.yielded = False
        self.notes = []
        self.stopped = False
        self.dp = SimpleNamespace(stop_polling=self._stop)

    async def _stop(self):
        self.stopped = True

    async def notify(self, text):
        self.notes.append(text)

    def _state(self):
        return {"version": 4, "demo": {"x": 1}}

    async def _backup_now(self, soon=False):
        await self.backup.save(self._state())


def test_full_cycle_primary_returns_and_reserve_yields():
    clock = Clock(200_000)
    # запасная копия работает
    raven = make("raven", 2)
    raven.role = "leader"
    tg = Tg(raven.caption(now=clock.t))
    rb = FakeBot(tg, raven)
    # главная (Render) вернулась: ставит заявку и ждёт
    render = make("render", 1)
    claims = []

    async def sleep(sec):
        await clock.sleep(sec)
        claims.append(parse(tg.caption))
        if len(claims) == 1:  # запасная делает очередную отметку и видит заявку
            assert await raven.beat(rb, now=clock.t) == "yield"
            assert rb.yielded and rb.stopped and raven.release_needed
            assert await raven.beat(rb, now=clock.t) == "yield"  # после решения больше ничего не пишет
            await raven.release(rb)  # то, что делает finally в ArbitrageBot.run

    asyncio.run(render.wait_turn(tg, sleep=sleep, clock=clock))
    assert claims[0].claim == "render"
    assert render.role == "leader" and render.prev_holder == "?"
    assert "вернулся на основной" in render.moved_text()
    assert parse(tg.caption).holder == "render"
    assert any("Передаю работу" in n for n in rb.notes)


def test_leader_beat_keeps_and_backup_save_carries_mark():
    clock = Clock(300_000)
    f = make("render", 1)
    f.role = "leader"
    tg = Tg(f.caption(now=clock.t - 120))
    b = FakeBot(tg, f)
    assert asyncio.run(f.beat(b, now=clock.t)) == "keep"
    assert parse(tg.caption).ts == clock.t
    asyncio.run(b.backup.save({"version": 4, "n": 2}))  # сохранение бэкапа не стирает отметку
    lease = parse(tg.caption)
    assert lease.holder == "render" and tg.caption.startswith(BACKUP_CAPTION)


def test_taken_does_not_release():
    clock = Clock(300_000)
    f = make("raven", 2)
    f.role = "leader"
    f.last_write = clock.t - 300
    tg = Tg(make("pella", 2).caption(now=clock.t - 10))
    b = FakeBot(tg, f)
    assert asyncio.run(f.beat(b, now=clock.t)) == "taken"
    assert b.yielded and not f.release_needed
    assert parse(tg.caption).holder == "pella"  # чужую отметку не трогаем


def test_backup_without_failover_keeps_plain_caption():
    tg = Tg(pinned=False)
    bk = TelegramBackup(tg, 1)
    asyncio.run(bk.save({"a": 1}))
    assert tg.caption == BACKUP_CAPTION


# --- состояние --------------------------------------------------------------------------------------------

def test_drop_local_state(tmp_path):
    for name in ("demo.json", "settings.json", "other.txt"):
        (tmp_path / name).write_text("{}")
    assert fo_mod.drop_local_state(tmp_path) == 2
    assert (tmp_path / "other.txt").exists() and not (tmp_path / "demo.json").exists()


def test_copy_restore_keeps_news_newstrade_traffic():
    ct = CopyTrader.__new__(CopyTrader)
    raw = {"news": {"events": [1]}, "newstrade": {"on": True}, "traffic": {"used": 5, "host": "render"},
           "traffic_hosts": {"raven": {"used": 1}}, "junk": 1}
    CopyTrader.restore(ct, raw)
    assert ct.st["news"] == {"events": [1]} and ct.st["newstrade"] == {"on": True}
    assert ct.st["traffic"]["used"] == 5 and ct.st["traffic_hosts"]["raven"]["used"] == 1
    assert "junk" not in ct.st


def test_traffic_counter_is_per_host(tmp_path):
    b = SimpleNamespace(copy=SimpleNamespace(st={"traffic": {"month": "2026-11", "used": int(10.6 * tm.GB),
                                                             "hosts": {}, "days": {}}}))
    raven = tm.Traffic(b, netdev=str(tmp_path / "нет"), host="raven")
    assert raven.st["used"] == 0 and raven.st["host"] == "raven"  # не наследует 10.6 ГБ Render
    assert b.copy.st["traffic_hosts"]["render"]["used"] == int(10.6 * tm.GB)
    assert raven.budget_bytes() >= 50 * tm.GB  # не Render: лимит без счётчика
    raven.st["used"] = 123
    assert "raven" in raven.text()
    render = tm.Traffic(b, netdev=str(tmp_path / "нет"), host="render")
    assert render.st["used"] == int(10.6 * tm.GB) and render.st["host"] == "render"  # вернулись — свой счёт
    assert b.copy.st["traffic_hosts"]["raven"]["used"] == 123
    assert "5 ГБ" in render.text()


def test_host_command_and_doctor(bot_env):
    import time

    from bot.doctor import _automat_checks
    from tests.test_telegram_flow import send

    b, session = bot_env
    assert b.failover.on and b.failover.me == "render" and not b.yielded

    async def flow():
        await send(b, "/host")
        assert "Хостинг" in session.sent[-1][1] and "render" in session.sent[-1][1]

    asyncio.run(flow())
    checks = [c for c in _automat_checks(b, time.time()) if "Хостинг" in c.title]
    assert checks and "render" in checks[0].text


def test_main_returns_to_standby_after_yield(monkeypatch, tmp_path):
    import main as main_mod

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ALLOWED_USER_IDS", "6069246522")
    monkeypatch.setenv("HOST_NAME", "raven")
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path)
    (tmp_path / "demo.json").write_text("{}")
    turns, bots = [], []

    async def standby(token, fo):
        turns.append(fo)
        fo.prev_holder = "render" if len(turns) == 1 else "raven"  # 2-й раз: это мы сами

    class Bot:
        def __init__(self, *a):
            self.yielded = False
            bots.append(self)

        async def run(self):
            assert not (tmp_path / "demo.json").exists()  # после переезда — свежий бэкап, не старые файлы
            self.yielded = len(bots) == 1  # первая копия уступила место, вторая работает до конца

    monkeypatch.setattr(main_mod.failover, "standby", standby)
    monkeypatch.setattr(main_mod, "ArbitrageBot", Bot)
    main_mod.main()
    assert len(turns) == 2 and len(bots) == 2
    assert bots[0].failover is turns[0] and turns[0].role == "leader"
