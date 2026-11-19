"""Общие фикстуры: бот с подменённым Telegram и фейковыми биржами доступен во всех тестах."""

from tests import test_telegram_flow

bot_env = test_telegram_flow.bot_env
