"""Демо-счета в консоли, без Telegram.

    python demo_console.py            # $5, $50, $500 по обеим стратегиям (или продолжить сохранённые)
    python demo_console.py 5 20       # свои суммы

Остановить: Ctrl + C. Состояние сохраняется в data/demo.json.
"""

import asyncio
import logging
import re
import sys

from bot.config import load_settings
from bot.demo_manager import DemoManager
from bot.scanner import Scanner


def plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


async def run() -> None:
    settings = load_settings()
    scanner = Scanner(settings)
    demo = DemoManager(scanner, settings)
    if len(sys.argv) > 1:
        demo.start([float(x.replace(",", ".")) for x in sys.argv[1:]])
    elif not demo.active:
        demo.start(settings.demo_amounts)

    print(plain(demo.summary_text()), "\n")
    print(f"Сканирую каждые {settings.scan_interval_sec:g} с. Ctrl + C, чтобы остановить.\n")
    try:
        while True:
            result = await scanner.scan()
            for event in await demo.process(result):
                print(plain(event), "\n")
            print(
                f"[скан] межбиржевых: {len(result.opportunities)}, треугольников: {len(result.triangles)}, "
                f"ошибок бирж: {len(result.errors)}, {result.duration_sec:.1f} с"
            )
            await asyncio.sleep(settings.scan_interval_sec)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await scanner.close()
        print("\n" + plain(demo.summary_text()))


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
