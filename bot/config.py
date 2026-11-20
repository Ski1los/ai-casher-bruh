"""Загрузка настроек из файла .env."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


DEFAULT_EXCHANGES = "bybit,bingx,okx,gate,mexc,kucoin,htx,bitget"


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
    demo_amounts: list[float] = field(default_factory=lambda: [5.0, 50.0, 500.0])
    triangular_enabled: bool = True
    tri_min_profit_percent: float = 0.15
    tri_max_profit_percent: float = 5.0
    report_hour: int = 9
    report_utc_offset: float = 3.0
    # Свои комиссии бирж (доля: 0.001 = 0.1%), если у тебя скидка/VIP. Остальные берутся из ccxt.
    fee_overrides: dict[str, float] = field(default_factory=dict)
    # --- Реальная торговля (нужны API-ключи бирж) ---
    max_trade_usd: float = 10.0  # максимум на одну покупку / один цикл
    daily_loss_usd: float = 2.0  # если автоторговля за сутки потеряла столько, она встаёт на паузу
    daily_trades: float = 30  # максимум сделок бота за сутки
    auto_min_profit_percent: float = 0.3  # автоторговля берёт только циклы с такой прибылью по стакану
    max_slippage_percent: float = 1.0  # если цена ушла хуже предпросмотра больше чем на столько, ордер отменяется
    auto_report_hours: float = 6.0  # сводка автоторговли владельцу раз в N часов (0 = не присылать)


# Что можно менять командой /set: имя -> (поле, описание)
EDITABLE = {
    "min_profit": ("min_profit_percent", "мин. чистая прибыль, %"),
    "max_profit": ("max_profit_percent", "макс. прибыль (фильтр ошибок), %"),
    "min_volume": ("min_volume_usd", "мин. объём за сутки, $"),
    "amount": ("trade_amount_usd", "сумма для проверки стакана, $"),
    "interval": ("scan_interval_sec", "интервал автосканирования, сек"),
    "cooldown": ("alert_cooldown_min", "пауза между повторами сигнала, мин"),
    "tri_min": ("tri_min_profit_percent", "треугольник: мин. чистая прибыль, %"),
    "tri_max": ("tri_max_profit_percent", "треугольник: макс. прибыль (фильтр ошибок), %"),
    "transfer": ("demo_transfer_min", "демо: время перевода между биржами, мин"),
    "wd_fee": ("demo_withdraw_fee_usd", "демо: комиссия вывода, если биржа её не сообщает, $"),
    "max_trade": ("max_trade_usd", "торговля: максимум на одну покупку/цикл, $"),
    "daily_loss": ("daily_loss_usd", "торговля: лимит убытка автоторговли за сутки, $"),
    "daily_trades": ("daily_trades", "торговля: максимум сделок бота за сутки"),
    "auto_min": ("auto_min_profit_percent", "автоторговля: мин. прибыль цикла по стакану, %"),
    "slippage": ("max_slippage_percent", "торговля: допустимое ухудшение цены, %"),
    "auto_report": ("auto_report_hours", "автоторговля: сводка раз в N часов (0 = выкл)"),
}

# Эти лимиты риска ИИ предлагать не может: их меняет только человек командой /set
RISK_KEYS = {"max_trade", "daily_loss", "daily_trades", "auto_min", "slippage"}

# Допустимые значения (для /set, предложений ИИ и кнопок): имя -> (мин, макс)
BOUNDS = {
    "min_profit": (0.0, 50.0),
    "max_profit": (0.1, 100.0),
    "min_volume": (0.0, 1e9),
    "amount": (1.0, 1e6),
    "interval": (10.0, 3600.0),
    "cooldown": (0.0, 1440.0),
    "tri_min": (0.0, 10.0),
    "tri_max": (0.1, 50.0),
    "transfer": (0.0, 240.0),
    "wd_fee": (0.0, 100.0),
    "max_trade": (1.0, 100_000.0),
    "daily_loss": (0.1, 100_000.0),
    "daily_trades": (1.0, 1000.0),
    "auto_min": (0.05, 10.0),
    "slippage": (0.1, 10.0),
    "auto_report": (0.0, 168.0),
}


def parse_fee_overrides(raw: str) -> dict[str, float]:
    """«bybit:0.1, mexc:0.05» (в процентах) → {"bybit": 0.001, "mexc": 0.0005}."""
    out: dict[str, float] = {}
    for item in raw.replace(";", ",").split(","):
        if ":" not in item and "=" not in item:
            continue
        name, _, value = item.replace("=", ":").partition(":")
        try:
            pct = float(value.strip().rstrip("%").replace(",", "."))
        except ValueError:
            continue
        if name.strip() and 0 <= pct < 5:
            out[name.strip().lower()] = pct / 100
    return out


def load_settings() -> Settings:
    return Settings(
        exchanges=_list("EXCHANGES", DEFAULT_EXCHANGES),
        quote=os.getenv("QUOTE", "USDT").strip().upper() or "USDT",
        min_profit_percent=_float("MIN_PROFIT_PERCENT", 0.5),
        max_profit_percent=_float("MAX_PROFIT_PERCENT", 15),
        min_volume_usd=_float("MIN_VOLUME_USD", 50_000),
        trade_amount_usd=_float("TRADE_AMOUNT_USD", 100),
        scan_interval_sec=_float("SCAN_INTERVAL_SEC", 60),
        alert_cooldown_min=_float("ALERT_COOLDOWN_MIN", 30),
        demo_transfer_min=_float("DEMO_TRANSFER_MIN", 10),
        demo_withdraw_fee_usd=_float("DEMO_WITHDRAW_FEE_USD", 0.5),
        demo_amounts=[float(x) for x in _list("DEMO_AMOUNTS", "5,50,500")] or [5.0, 50.0, 500.0],
        triangular_enabled=os.getenv("TRIANGULAR", "1").strip().lower() not in ("0", "false", "no", "off"),
        tri_min_profit_percent=_float("TRI_MIN_PROFIT_PERCENT", 0.15),
        tri_max_profit_percent=_float("TRI_MAX_PROFIT_PERCENT", 5),
        report_hour=int(_float("REPORT_HOUR", 9)),
        report_utc_offset=_float("REPORT_UTC_OFFSET", 3),
        fee_overrides=parse_fee_overrides(os.getenv("FEE_OVERRIDES", "")),
        max_trade_usd=_float("MAX_TRADE_USD", 10),
        daily_loss_usd=_float("DAILY_LOSS_USD", 2),
        daily_trades=_float("DAILY_TRADES", 30),
        auto_min_profit_percent=_float("AUTO_MIN_PROFIT_PERCENT", 0.3),
        max_slippage_percent=_float("MAX_SLIPPAGE_PERCENT", 1),
        auto_report_hours=_float("AUTO_REPORT_HOURS", 6),
    )


def telegram_token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def allowed_user_ids() -> list[int]:
    """ID пользователей с доступом. Первый в списке считается владельцем (ему идут бэкапы)."""
    return [int(x) for x in _list("ALLOWED_USER_IDS") if x.lstrip("-").isdigit()]
