"""Запуск Telegram-бота.

    python main.py
"""

import asyncio
import logging
import sys

from bot.memory import tune_malloc

tune_malloc()  # до запуска потоков: меньше «арен» malloc, меньше памяти на Render

from aiogram.exceptions import TelegramUnauthorizedError

from bot.config import allowed_user_ids, load_settings, telegram_token
from bot.telegram_bot import ArbitrageBot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = telegram_token()
    if not token:
        print(
            "Не найден TELEGRAM_BOT_TOKEN.\n"
            "Render → сервис → Environment → TELEGRAM_BOT_TOKEN = токен от @BotFather.\n"
            "Подробнее в README.md"
        )
        sys.exit(1)

    bot = ArbitrageBot(token, load_settings(), allowed_user_ids())
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except TelegramUnauthorizedError:
        print("Telegram отклонил токен. Проверь TELEGRAM_BOT_TOKEN (скопируй заново у @BotFather).")
        sys.exit(1)


if __name__ == "__main__":
    main()
