"""Запуск Telegram-бота.

    python main.py
"""

import asyncio
import logging
import sys

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
            "1) Создай бота у @BotFather в Telegram и скопируй токен\n"
            "2) Скопируй .env.example в .env и вставь токен в строку TELEGRAM_BOT_TOKEN=\n"
            "Подробнее в README.md"
        )
        sys.exit(1)

    bot = ArbitrageBot(token, load_settings(), allowed_user_ids())
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nБот остановлен.")


if __name__ == "__main__":
    main()
