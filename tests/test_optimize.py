"""v31 — оптимизация: лёгкий ccxt, облегчение рынков, заморозка старта, чистка разведчика."""

import gc
import os
import subprocess
import sys
import time
from pathlib import Path

from bot.memory import freeze_startup
from bot.scanner import slim_markets
from tests.test_mentors import make

ROOT = Path(__file__).resolve().parent.parent
DAY = 86400


def _run(code: str, **env) -> str:
    """Отдельный процесс: так проверяем, что именно грузится при старте."""
    e = {**os.environ, **env}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=e, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


# --- лёгкий ccxt -------------------------------------------------------------------------------

_LITE_CHECK = """
import bot, ccxt.async_support as ccxt
from bot import ccxt_lite
from ccxt.base import errors
assert ccxt_lite._installed
assert "bybit" in ccxt.exchanges and "bingx" in ccxt.exchanges and "__init__" not in ccxt.exchanges
assert ccxt.__version__.startswith("4.")
assert ccxt.NetworkError is errors.NetworkError and issubclass(ccxt.RateLimitExceeded, ccxt.NetworkError)
ex = ccxt.bybit()
assert isinstance(ex, ccxt.Exchange) and ex.id == "bybit"
assert getattr(ccxt, "bingx")().id == "bingx"
assert "bingx" in dir(ccxt)
try:
    ccxt.no_such_exchange_xyz
    raise SystemExit("нет AttributeError")
except AttributeError:
    pass
print("ok")
"""


def test_ccxt_lite_works_like_real_ccxt():
    assert _run(_LITE_CHECK) == "ok"


def test_ccxt_lite_loads_only_used_exchanges():
    code = ("import sys, bot.telegram_bot, ccxt.async_support as c; c.bybit(); c.bingx();"
            "print(sorted(k.split('.')[-1] for k in sys.modules if k.startswith('ccxt.async_support.')"
            " and k.count('.') == 2 and k.split('.')[-1] in c.exchanges))")
    assert _run(code) == "['bingx', 'bybit']"


def test_ccxt_full_fallback():
    code = "import bot, ccxt; from bot import ccxt_lite; print(ccxt_lite._installed, type(ccxt).__name__)"
    assert _run(code, CCXT_FULL="1") == "False module"
    assert _run(code, CCXT_FULL="") == "True _LazyPackage"


# --- облегчение рынков -------------------------------------------------------------------------

def test_slim_markets_drops_raw_info_only():
    class Ex:
        markets = {"BTC/USDT": {"symbol": "BTC/USDT", "precision": {"amount": 6}, "info": {"raw": "x" * 2000}},
                   "ETH/USDT": {"symbol": "ETH/USDT", "info": {}}}
        currencies = {"USDT": {"code": "USDT", "info": [1, 2], "networks": {"TRC20": {"fee": 1, "info": {"a": 1}}}},
                      "BAD": None}
    ex = Ex()
    assert slim_markets(ex) == 2
    assert ex.markets["BTC/USDT"] == {"symbol": "BTC/USDT", "precision": {"amount": 6}, "info": {}}
    assert ex.currencies["USDT"]["networks"]["TRC20"] == {"fee": 1, "info": {}}
    assert slim_markets(ex) == 0
    assert slim_markets(object()) == 0  # биржа без рынков — не падаем


# --- заморозка старта --------------------------------------------------------------------------

def test_freeze_startup():
    try:
        n = freeze_startup()
        assert n > 0 and gc.get_freeze_count() == n
    finally:
        gc.unfreeze()


# --- чистка разведчика -------------------------------------------------------------------------

def test_scout_prune_forgets_old_auto_removed():
    now = time.time()
    mt, *_ = make(now)
    src = mt.st["sources"]
    base = {"auto": True, "removed": True}
    src["old"] = {**base, "removed_at": now - 15 * DAY}
    src["fresh"] = {**base, "removed_at": now - 3 * DAY}
    src["busy"] = {**base, "removed_at": now - 30 * DAY}
    src["manual"] = {"removed": True, "removed_at": now - 30 * DAY}  # убран вручную — не трогаем
    src["live"] = {"auto": True}
    mt.st["calls"].append({"source": "busy", "done": False})
    mt.st["calls"].append({"source": "old", "done": True})
    assert mt.scout.prune(now) == 1
    assert set(src) >= {"fresh", "busy", "manual", "live"} and "old" not in src
    assert mt.scout.prune(now) == 0


def test_suite_runs_with_lite_ccxt_like_production():
    import ccxt
    from bot import ccxt_lite
    assert ccxt_lite._installed and type(ccxt).__name__ == "_LazyPackage"
