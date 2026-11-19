"""Демо-счёт в консоли, без Telegram.

    python demo_console.py 5      # стартовать с $5
    python demo_console.py        # продолжить сохранённый демо-счёт (или начать с $5)

Остановить: Ctrl + C. Состояние сохраняется в data/demo.json.
"""

import asyncio
import logging
import re
import sys

from bot.config import load_settings
from bot.demo import DemoEngine
from bot.scanner import Scanner


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def run() -> None:
    settings = load_settings()
    scanner = Scanner(settings)
    demo = DemoEngine(scanner, settings)
    if len(sys.argv) > 1:
        demo.start(float(sys.argv[1].replace(",", ".")))
    elif not demo.active:
        demo.start(5)

    print(plain(demo.status_text()), "\n")
    print(f"Сканирую каждые {settings.scan_interval_sec:g} с. Ctrl + C, чтобы остановить.\n")
    try:
        while True:
            result = await scanner.scan()
            for event in await demo.process(result):
                print(plain(event), "\n")
            print(
                f"[скан #{demo.acc.scans}] связок: {len(result.opportunities)}, "
                f"баланс: ${demo.acc.equity:.4f}, ошибок бирж: {len(result.errors)}"
            )
            await asyncio.sleep(settings.scan_interval_sec)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await scanner.close()
        print("\n" + plain(demo.status_text()))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
