"""Общие фикстуры: бот с подменённым Telegram и фейковыми биржами доступен во всех тестах."""

import pytest

from tests import test_telegram_flow

bot_env = test_telegram_flow.bot_env


@pytest.fixture(autouse=True)
def _no_keyless_ai(monkeypatch):
    """ИИ без ключа (OVHcloud, LLM7) ходят в интернет — в тестах выключены; их тесты включают сами."""
    monkeypatch.setenv("AI_KEYLESS", "0")
    # 🖥 v50: тесты имитируют Render (бюджет трафика 5 ГБ); тесты резерва задают имя хостинга сами
    monkeypatch.setenv("HOST_NAME", "render")
    monkeypatch.delenv("FAILOVER_PRIORITY", raising=False)
    for var in ("YANDEX_FOLDER_ID", "GIGACHAT_BASE_URL", "GIGACHAT_AUTH_URL", "GIGACHAT_SCOPE", "CLOUDFLARE_ACCOUNT_ID",
                "CF_ACCOUNT_ID"):
        monkeypatch.delenv(var, raising=False)
