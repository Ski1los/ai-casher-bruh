"""Одно сканирование с выводом в консоль, без Telegram.

Удобно, чтобы проверить, что биржи отвечают:
    python scan_once.py
"""

import asyncio
import logging

from bot.config import load_settings
from bot.scanner import Scanner, fmt_price


async def run() -> None:
    settings = load_settings()
    scanner = Scanner(settings)
    print(f"Сканирую: {', '.join(scanner.exchanges)} ... (до 30 секунд)\n")
    try:
        result = await scanner.scan()
    finally:
        await scanner.close()

    for name, err in result.errors.items():
        print(f"[!] {name}: {err}")
    print(
        f"Биржи ответили: {', '.join(result.exchanges_ok) or 'нет'}; "
        f"сравнено пар: {result.symbols_compared}; время: {result.duration_sec:.1f} с\n"
    )

    print("=== Межбиржевые ===")
    if not result.opportunities:
        print(f"Связок с прибылью от {settings.min_profit_percent}% не найдено.")

    for o in result.opportunities[:15]:
        line = (
            f"{o.symbol:<14} +{o.net_percent:5.2f}%  "
            f"купить {o.buy.exchange} {fmt_price(o.buy.ask)}  →  продать {o.sell.exchange} {fmt_price(o.sell.bid)}"
        )
        if o.depth:
            line += f"  | стакан ${o.depth.amount_usd:.0f}: {o.depth.profit_usd:+.2f}$"
            if not o.depth.filled:
                line += " (мало глубины)"
        print(line)

    print("\n=== Треугольные ===")
    if not result.triangles:
        print(f"Треугольников с прибылью от {settings.tri_min_profit_percent}% не найдено.")
    for t in result.triangles[:15]:
        line = f"{t.exchange:<8} {t.path_text:<32} +{t.net_percent:.3f}%"
        if t.depth:
            line += f"  | стакан ${t.depth.amount_usd:.0f}: {t.depth.profit_usd:+.3f}$"
            if not t.depth.filled:
                line += " (мало глубины)"
        print(line)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run())
