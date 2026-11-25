"""v49.1: разделы news / newstrade / traffic переживают восстановление из бэкапа (раньше restore их выбрасывал)."""

from bot.copytrade import CopyTrader, new_state


def test_restore_keeps_news_newstrade_traffic_sections():
    ct = CopyTrader.__new__(CopyTrader)
    raw = {"news": {"events": [1]}, "newstrade": {"on": True, "open": {"x": 1}}, "traffic": {"used": 5},
           "on": True, "junk": 1}
    CopyTrader.restore(ct, raw)
    assert ct.st["news"] == {"events": [1]}
    assert ct.st["newstrade"]["open"] == {"x": 1}
    assert ct.st["traffic"]["used"] == 5 and ct.st["on"] is True
    assert "junk" not in ct.st
    for key in ("news", "newstrade", "traffic"):
        assert key in new_state()
