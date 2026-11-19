"""Загрузка настроек из файла .env."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


@dataclass
class Settings:
    """Параметры сканера. Часть из них можно менять командой /set прямо в Telegram."""

    exchanges: list[str] = field(default_factory=list)
    quote: str = "USDT"
    min_profit_percent: float = 0.5
    max_profit_percent: float = 15.0
    min_volume_usd: float = 50_000
    trade_amount_usd: float = 100
    scan_interval_sec: float = 60
    alert_cooldown_min: float = 30
    demo_transfer_min: float = 10
    demo_withdraw_fee_usd: float = 0.5


# Что можно менять командой /set: имя -> (поле, описание)
EDITABLE = {
    "min_profit": ("min_profit_percent", "мин. чистая прибыль, %"),
    "max_profit": ("max_profit_percent", "макс. прибыль (фильтр ошибок), %"),
    "min_volume": ("min_volume_usd", "мин. объём за сутки, $"),
    "amount": ("trade_amount_usd", "сумма для проверки стакана, $"),
    "interval": ("scan_interval_sec", "интервал автосканирования, сек"),
    "cooldown": ("alert_cooldown_min", "пауза между повторами сигнала, мин"),
    "transfer": ("demo_transfer_min", "демо: время перевода между биржами, мин"),
    "wd_fee": ("demo_withdraw_fee_usd", "демо: комиссия вывода, если биржа её не сообщает, $"),
}


def load_settings() -> Settings:
    return Settings(
        exchanges=_list("EXCHANGES", "bybit,okx,gate,mexc,kucoin,htx,bitget"),
        quote=os.getenv("QUOTE", "USDT").strip().upper() or "USDT",
        min_profit_percent=_float("MIN_PROFIT_PERCENT", 0.5),
        max_profit_percent=_float("MAX_PROFIT_PERCENT", 15),
        min_volume_usd=_float("MIN_VOLUME_USD", 50_000),
        trade_amount_usd=_float("TRADE_AMOUNT_USD", 100),
        scan_interval_sec=_float("SCAN_INTERVAL_SEC", 60),
        alert_cooldown_min=_float("ALERT_COOLDOWN_MIN", 30),
        demo_transfer_min=_float("DEMO_TRANSFER_MIN", 10),
        demo_withdraw_fee_usd=_float("DEMO_WITHDRAW_FEE_USD", 0.5),
    )


def telegram_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def allowed_user_ids() -> list[int]:
    """ID пользователей с доступом. Первый в списке считается владельцем (ему идут бэкапы)."""
    return [int(x) for x in _list("ALLOWED_USER_IDS") if x.lstrip("-").isdigit()]
