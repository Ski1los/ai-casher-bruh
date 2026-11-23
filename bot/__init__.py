"""AI Casher. Лёгкий ccxt включаем до любого `import ccxt` (−~50 МБ памяти, см. bot/ccxt_lite.py)."""

from . import ccxt_lite as _ccxt_lite

_ccxt_lite.install()
