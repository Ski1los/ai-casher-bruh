"""Лёгкий импорт ccxt: биржи подгружаются при первом обращении, а не все ~110 сразу.

Обычный `import ccxt.async_support` загружает в память КАЖДУЮ биржу дважды (обычную и асинхронную версию) —
это около 70 МБ, а боту нужны только Bybit и BingX. На бесплатном Render лимит 512 МБ, поэтому:

  * install() кладёт в sys.modules лёгкие «пакеты» `ccxt` и `ccxt.async_support` с теми же путями к файлам;
  * ошибки (ccxt.NetworkError …), Exchange и прочее берутся из настоящих модулей ccxt.base.*;
  * `ccxt.bybit` / `getattr(ccxt, "bingx")` подгружают файл нужной биржи при первом обращении;
  * `ccxt.exchanges` — список всех бирж по файлам пакета (без загрузки).

Если что-то пошло не так (другая версия ccxt, ccxt уже импортирован), install() ничего не трогает —
бот работает с обычным ccxt. Отключить вручную: переменная окружения CCXT_FULL=1.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

log = logging.getLogger(__name__)

_SKIP = {"__init__"}
_installed = False


class _LazyPackage(types.ModuleType):
    """Пакет, который подгружает атрибуты (биржи, ошибки) при первом обращении."""

    def __init__(self, name: str, path: str, exchange_prefix: str, extra: dict) -> None:
        super().__init__(name)
        self.__path__ = [path]
        self.__file__ = os.path.join(path, "__init__.py")
        self.__package__ = name
        self._prefix = exchange_prefix
        self.__dict__.update(extra)

    def __getattr__(self, attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)
        errors = sys.modules.get("ccxt.base.errors")
        if errors is not None and hasattr(errors, attr):
            value = getattr(errors, attr)
        elif attr in self.__dict__.get("exchanges", ()):
            module = importlib.import_module(f"{self._prefix}.{attr}")
            value = getattr(module, attr)
        else:
            try:
                value = importlib.import_module(f"{self.__name__}.{attr}")
            except ImportError:
                raise AttributeError(f"module {self.__name__!r} has no attribute {attr!r}") from None
        setattr(self, attr, value)  # дальше — без __getattr__ (и поверх подмодуля, если импорт его поставил)
        return value

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(self.__dict__.get("exchanges", ())))


def install() -> bool:
    """Подменить ccxt лёгкой версией. True — подменили, False — оставили обычный ccxt."""
    global _installed
    if _installed:
        return True
    if os.getenv("CCXT_FULL", "").strip() in ("1", "true", "yes") or "ccxt" in sys.modules:
        return False
    try:
        spec = importlib.util.find_spec("ccxt")
        if spec is None or not spec.submodule_search_locations:
            return False
        root = Path(list(spec.submodule_search_locations)[0])
        names = sorted(p.stem for p in root.glob("*.py") if p.stem not in _SKIP)
        async_names = sorted(p.stem for p in (root / "async_support").glob("*.py") if p.stem not in _SKIP)
        version = _read_version(root / "__init__.py")
        sync_pkg = _LazyPackage("ccxt", str(root), "ccxt", {"exchanges": names, "__version__": version})
        async_pkg = _LazyPackage("ccxt.async_support", str(root / "async_support"), "ccxt.async_support",
                                 {"exchanges": async_names, "__version__": version})
        sys.modules["ccxt"] = sync_pkg
        sys.modules["ccxt.async_support"] = async_pkg
        sync_pkg.async_support = async_pkg
        errors = importlib.import_module("ccxt.base.errors")  # настоящие классы ошибок
        sync_pkg.errors = async_pkg.errors = errors
        from ccxt.base.exchange import Exchange as SyncExchange
        sync_pkg.Exchange = SyncExchange  # нужен до загрузки асинхронной базы (ws импортирует его из ccxt)
        from ccxt.async_support.base.exchange import Exchange as AsyncExchange
        async_pkg.Exchange = AsyncExchange
        _installed = True
        return True
    except Exception as e:  # pragma: no cover - другая версия ccxt: работаем с обычным
        log.warning("Лёгкий ccxt не включился (%s) — гружу обычный", e)
        for k in [k for k in sys.modules if k == "ccxt" or k.startswith("ccxt.")]:
            del sys.modules[k]
        return False


def _read_version(init_file: Path) -> str:
    try:
        with open(init_file, encoding="utf-8") as f:
            for line in f:
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""
