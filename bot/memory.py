"""Память процесса: сколько занято сейчас и как вернуть лишнее системе.

На бесплатном Render у бота 512 МБ. Если их не хватает, Render перезапускает сервис,
и бот на минуту «пропадает». Поэтому:
* tune_malloc() при запуске ограничивает число «арен» malloc (как MALLOC_ARENA_MAX=2):
  иначе каждый поток (DNS-резолвер aiohttp и т. п.) получает свою арену, и память пухнет;
* free_memory() раз в 10 минут собирает мусор и отдаёт освобождённые страницы системе
  (malloc_trim). Python сам этого почти не делает, и процесс «помнит» пиковый размер;
* freeze_startup() после запуска «замораживает» объекты, созданные при старте (~200 тыс.: модели aiogram, ccxt):
  сборщик мусора больше не обходит их каждый раз — полная сборка с ~60 мс падает почти до нуля, бот не «замирает»;
* rss_mb() показывает текущую память, а не пиковую (её видно в /doctor и /clean).
Всё это работает только на Linux с glibc (как на Render). На других системах функции ничего не ломают.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
import os
import resource
import sys

log = logging.getLogger(__name__)

RENDER_RAM_MB = 512
M_ARENA_MAX = -8  # константа из malloc.h
_libc = None


def _glibc():
    global _libc
    if _libc is None:
        _libc = False
        if sys.platform.startswith("linux"):
            try:
                lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
                if hasattr(lib, "malloc_trim") and hasattr(lib, "mallopt"):
                    _libc = lib
            except OSError:
                pass
    return _libc or None


def tune_malloc(arenas: int = 2) -> bool:
    """Вызывать как можно раньше, до запуска потоков. True, если удалось."""
    if os.getenv("MALLOC_ARENA_MAX"):
        return False  # уже задано окружением, оно главнее
    lib = _glibc()
    if lib is None:
        return False
    try:
        return bool(lib.mallopt(M_ARENA_MAX, arenas))
    except Exception:
        return False


def rss_mb() -> float:
    """Сколько памяти процесс занимает прямо сейчас, МБ."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return peak_mb()


def peak_mb() -> float:
    """Максимум памяти с момента запуска, МБ."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024  # Linux отдаёт КБ


def free_memory() -> tuple[float, float]:
    """Собирает мусор и возвращает свободные страницы системе. (было МБ, стало МБ)."""
    before = rss_mb()
    gc.collect()
    lib = _glibc()
    if lib is not None:
        try:
            lib.malloc_trim(0)
        except Exception:
            pass
    return before, rss_mb()


def freeze_startup() -> int:
    """После запуска: собрать мусор старта и «заморозить» остальное (gc.freeze). Возвращает, сколько заморожено."""
    gc.collect()
    gc.freeze()
    return gc.get_freeze_count()


def memory_line() -> str:
    now, peak = rss_mb(), peak_mb()
    return f"{now:.0f} МБ сейчас (пик {peak:.0f} МБ) из {RENDER_RAM_MB} МБ"
