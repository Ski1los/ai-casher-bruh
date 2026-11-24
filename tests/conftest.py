"""Общие фикстуры: бот с подменённым Telegram и фейковыми биржами доступен во всех тестах."""

import pytest

from tests import test_telegram_flow

bot_env = test_telegram_flow.bot_env


@pytest.fixture(autouse=True)
def _no_keyless_ai(monkeypatch):
    """ИИ без ключа (OVHcloud, LLM7) ходят в интернет — в тестах выключены; их тесты включают сами."""
    monkeypatch.setenv("AI_KEYLESS", "0")
    for var in ("YANDEX_FOLDER_ID", "GIGACHAT_BASE_URL", "GIGACHAT_AUTH_URL", "GIGACHAT_SCOPE"):
        monkeypatch.delenv(var, raising=False)
