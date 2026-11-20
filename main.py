"""Запуск Telegram-бота.

    python main.py

Если бот не может запуститься (нет токена, Telegram отклонил токен), в облаке процесс НЕ падает,
а показывает страницу состояния: открой адрес сервиса (…onrender.com) в браузере телефона —
там написано, что не так и что нажать.
"""

import asyncio
import logging
import sys

from bot.memory import tune_malloc

tune_malloc()  # до запуска потоков: меньше «арен» malloc, меньше памяти на Render

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError

from bot.cloud import cloud_port, serve_problem_forever, set_status
from bot.config import allowed_user_ids, load_settings, telegram_token
from bot.telegram_bot import ArbitrageBot

NO_TOKEN_STEPS = [
    "Открой в Telegram <b>@BotFather</b> → /mybots → выбери своего бота → <b>API Token</b> → скопируй токен.",
    "Render → сервис <b>ai-casher-bot</b> → <b>Environment</b> → <b>Add Environment Variable</b>: "
    "ключ <code>TELEGRAM_BOT_TOKEN</code>, значение — токен.",
    "Там же проверь <code>ALLOWED_USER_IDS</code> = <code>6069246522</code>.",
    "Нажми <b>Save Changes</b>. Через 1–2 минуты обнови эту страницу: должно стать ✅.",
]
BAD_TOKEN_STEPS = [
    "Токен скопирован с ошибкой или был сброшен (Revoke). Открой <b>@BotFather</b> → /mybots → твой бот → "
    "<b>API Token</b> и скопируй токен заново (целиком, без пробелов).",
    "Render → <b>ai-casher-bot</b> → <b>Environment</b> → замени значение <code>TELEGRAM_BOT_TOKEN</code> → "
    "<b>Save Changes</b>.",
    "Через 1–2 минуты обнови эту страницу.",
]


def fail(title: str, steps: list[str], console: str) -> None:
    """Сообщить о проблеме. В облаке держим страницу состояния вместо падения (иначе Render крутит перезапуски)."""
    print(console)
    port = cloud_port()
    if not port:
        sys.exit(1)
    set_status("problem", title, steps)
    logging.getLogger(__name__).error("%s. Подробности: открой адрес сервиса в браузере", title)
    asyncio.run(serve_problem_forever(port))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = telegram_token()
    if not token:
        fail("Не задан TELEGRAM_BOT_TOKEN", NO_TOKEN_STEPS,
             "Не найден TELEGRAM_BOT_TOKEN.\n"
             "Render → сервис → Environment → TELEGRAM_BOT_TOKEN = токен от @BotFather.\n"
             "Подробнее в README.md")
        return

    try:
        bot = ArbitrageBot(token, load_settings(), allowed_user_ids())
    except TokenValidationError:
        fail("TELEGRAM_BOT_TOKEN неправильного вида", BAD_TOKEN_STEPS,
             "TELEGRAM_BOT_TOKEN неправильного вида: должен быть как 123456789:AA... (скопируй у @BotFather).")
        return
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except TelegramUnauthorizedError:
        fail("Telegram отклонил токен", BAD_TOKEN_STEPS,
             "Telegram отклонил токен. Проверь TELEGRAM_BOT_TOKEN (скопируй заново у @BotFather).")


if __name__ == "__main__":
    main()
