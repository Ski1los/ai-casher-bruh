"""v35: скорость и надёжность — быстрый копитрейдинг с отступлением, путь цены без потери пиков,
отдельная очередь фонового ИИ, атомарная запись состояния, отбор кандидатов пульса."""

import asyncio
import json
import time

import pytest

from bot import copytrade
from bot.ai import Endpoint, LLMClient
from bot.config import write_atomic
from bot.copytrade import MAX_STEP_SEC, PATH_MAX, STEP_SEC, CopyTrader, add_path
from bot.pulse import MAX_NEW, split_insights
from bot.solana import WalletError
from tests import test_copytrade as tc
from tests import test_pulse as tp


def run(coro):
    return asyncio.run(coro)


# --- путь цены: пики и провалы не теряются при сжатии ------------------------------------------

def test_add_path_keeps_dips_and_spikes_when_thinning():
    path = [0.0]
    for _ in range(1, PATH_MAX):
        add_path(path, 1.0)
    # провал под стоп и пик над тейком на «нечётных» местах — старое прореживание через одну их выбрасывало
    path[7] = -80.0
    path[13] = 150.0
    add_path(path, 2.0)  # переполнение → сжатие
    assert len(path) <= PATH_MAX
    assert path[0] == 0.0 and path[-1] == 2.0
    assert -80.0 in path and 150.0 in path
    # порядок важен для стопа/тейка: провал раньше пика
    assert path.index(-80.0) < path.index(150.0)


def test_add_path_many_points_stays_bounded():
    path = [0.0]
    for i in range(2000):
        add_path(path, -50.0 if i == 1234 else (i % 7) - 3)
    assert len(path) <= PATH_MAX and -50.0 in path


# --- выводы пульса: признак не бывает одновременно ❌ и ✅ ------------------------------------------

def test_split_insights_no_overlap_and_signs():
    ins = [{"label": "a", "avg": -20}, {"label": "b", "avg": 5}, {"label": "c", "avg": 12}]
    loses, works = split_insights(ins)
    assert [x["label"] for x in loses] == ["a"]
    assert [x["label"] for x in works] == ["c", "b"]
    only_plus = [{"label": "x", "avg": 3}, {"label": "y", "avg": 9}]
    loses, works = split_insights(only_plus)
    assert loses == [] and [x["label"] for x in works] == ["y", "x"]


def test_pulse_text_and_ai_context_dont_mark_winner_as_loser(monkeypatch):
    c, g, p = tp.env()
    fake = [{"id": "x", "label": "Источник: умные деньги", "n": 20, "avg": 8.0, "win": 60, "verdict": "promising",
             "ucb": 1, "lcb": 0}]
    monkeypatch.setattr(p, "insights", lambda: fake)
    assert "❌ Источник" not in p.text() and "✅ Источник" in p.text()
    ctx = p.ai_context()
    assert ctx["what_loses"] == [] and ctx["what_works"][0]["avg_pct"] == 8.0


# --- пульс: мусор не занимает места хороших кандидатов -------------------------------------------

def test_pulse_junk_does_not_eat_new_slots():
    c, g, p = tp.env()
    junk = [tp.mint(200 + i) for i in range(MAX_NEW)]
    good = [tp.mint(300 + i) for i in range(MAX_NEW + 2)]
    g.rank = [tp.tok(m, honeypot=True) for m in junk] + [tp.tok(m) for m in good]
    for m in junk + good:
        c.dex.pairs[m] = tp.pair(m)
    run(p.scan(tp.NOW))
    picks = set(p.st["picks"])
    assert len(picks) == MAX_NEW and picks <= set(good)
    left = [m for m in good if m not in picks]
    assert left and all(m not in p.st["seen"] for m in left)  # не просмотрены — придут в следующий поиск
    run(p.scan(tp.NOW + 200))
    assert set(left) <= set(p.st["picks"])


# --- копитрейдинг: все кошельки за шаг, параллельно, с отступлением при лимите узла -------------

WALLETS = ["Wha1e" + ch * 39 for ch in "ABCDEFGHJK"]


class SlowRpc(tc.FakeRpc):
    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.throttle = False

    async def call(self, method, params):
        if method == "getSignaturesForAddress" and self.throttle:
            raise WalletError("узел Solana просит подождать (слишком много запросов)")
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            return await super().call(method, params)
        finally:
            self.active -= 1


def make_copy(now):
    rpc = SlowRpc()
    ct = CopyTrader(tc.FakeDex(), rpc)
    ct.st["on"] = True
    for i, w in enumerate(WALLETS):
        run(ct.add_wallet(w, f"Кит {i}", now=now))
    return ct, rpc


def test_copy_polls_all_wallets_in_parallel_each_step():
    assert STEP_SEC <= 30
    now = time.time()
    ct, rpc = make_copy(now)
    rpc.calls.clear()
    rpc.peak = 0

    async def go():
        t0 = time.perf_counter()
        await ct.step(now + 1)
        return time.perf_counter() - t0

    took = run(go())
    assert rpc.calls.count("getSignaturesForAddress") == len(WALLETS)  # каждый кошелёк — в этом же шаге
    assert rpc.peak >= 2  # запросы шли параллельно (настоящий SolanaRpc ограничит их семафором на 3)
    assert took < len(WALLETS) * 0.02
    assert all(ct.st["wallets"][w]["checked"] == now + 1 for w in WALLETS)


def test_copy_backs_off_when_node_throttles_and_recovers():
    now = time.time()
    ct, rpc = make_copy(now)
    rpc.throttle = True
    seen = []
    for k in range(5):
        run(ct.step(now + k))
        seen.append(ct.step_sec)
    assert seen == [min(MAX_STEP_SEC, STEP_SEC * 2 ** (k + 1)) for k in range(5)]  # 60, 120, 180, 180, 180
    assert "подождать" in (ct.last_error or "")
    rpc.throttle = False
    run(ct.step(now + 10))
    assert ct.step_sec == STEP_SEC


def test_copy_still_copies_buy_found_by_parallel_poll():
    now = time.time()
    ct, rpc, dex = tc.setup(now)
    dex.set_price(tc.MINT, 0.01575)
    rpc.push(tc.W, tc.buy_tx("b1", now - 10))
    run(ct.step(now + 30))
    assert f"{tc.W}|{tc.MINT}" in ct.st["open"]


# --- ИИ: вопрос в чате не ждёт фоновый разбор ----------------------------------------------------

def test_interactive_ai_not_blocked_by_background_job(monkeypatch):
    client = LLMClient([Endpoint("gemini", "https://x", "k")])
    gate = asyncio.Event()
    order = []

    async def fake_call(ep, messages, max_tokens):
        who = messages[0]["content"]
        if who == "bg":
            await gate.wait()  # фоновый разбор «висит» (видео, медленный ответ)
        order.append(who)
        return who

    monkeypatch.setattr(client, "_call", fake_call)

    async def go():
        bg = asyncio.create_task(client.chat([{"role": "user", "content": "bg"}], background=True))
        await asyncio.sleep(0.01)
        answer = await asyncio.wait_for(client.chat([{"role": "user", "content": "me"}]), 1)
        gate.set()
        await bg
        return answer

    assert run(go()) == "me"
    assert order == ["me", "bg"]


# --- атомарная запись: обрыв посреди записи не портит старый файл ---------------------------------

def test_write_atomic_keeps_old_file_on_failure(tmp_path, monkeypatch):
    f = tmp_path / "data" / "copy.json"
    write_atomic(f, json.dumps({"v": 1}))
    assert json.loads(f.read_text("utf-8")) == {"v": 1}
    assert not list(f.parent.glob(".*.tmp"))

    import os
    real_replace = os.replace

    def boom(a, b):
        raise OSError("перезапуск посреди записи")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_atomic(f, json.dumps({"v": 2}))
    monkeypatch.setattr(os, "replace", real_replace)
    assert json.loads(f.read_text("utf-8")) == {"v": 1}  # старое состояние целое
    write_atomic(f, json.dumps({"v": 3}))
    assert json.loads(f.read_text("utf-8")) == {"v": 3}


def test_bot_saves_use_atomic_write():
    import inspect

    from bot import demo, demo_manager, telegram_bot
    for mod in (telegram_bot, demo, demo_manager):
        assert ".write_text(" not in inspect.getsource(mod), mod.__name__
    assert copytrade.POLL_PER_STEP >= copytrade.MAX_ACTIVE


# --- ИИ: запас на «размышления», чтобы мысль не обрывалась и не было повторного запроса ----------

def test_gemini_requests_leave_room_for_thinking():
    from bot.ai import THINK_ROOM
    body = LLMClient._gemini_body([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}], 1200)
    assert body["generationConfig"]["maxOutputTokens"] >= 1200 + THINK_ROOM >= 5000
    import inspect

    from bot import livewatch, mentors, scout
    for mod in (livewatch, mentors, scout):
        assert "THINK_ROOM" in inspect.getsource(mod), mod.__name__
