import re

from bot.report import ScanStats, seconds_until, split_message
from bot.scanner import ScanResult
from bot.sim import DepthResult
from bot.triangular import TriLeg, TriOpportunity


def test_seconds_until_next_report():
    day = 86400
    midnight_utc = 20 * day
    # 05:00 UTC = 08:00 МСК → до 09:00 МСК остался 1 час
    assert seconds_until(9, 3, midnight_utc + 5 * 3600) == 3600
    # 07:00 UTC = 10:00 МСК → следующий отчёт завтра в 09:00 МСК
    assert seconds_until(9, 3, midnight_utc + 7 * 3600) == 23 * 3600


def test_split_message_respects_limit_and_keeps_blocks():
    blocks = [f"<b>блок {i}</b>\n" + "x" * 300 for i in range(40)]
    text = "\n\n".join(blocks)
    parts = split_message(text, limit=1000)
    assert len(parts) > 1
    assert all(len(p) <= 1000 for p in parts)
    assert "\n\n".join(parts) == text
    for p in parts:  # теги не разрезаны
        assert len(re.findall("<b>", p)) == len(re.findall("</b>", p))


def test_split_message_handles_huge_single_block():
    text = "\n".join("строка " + str(i) for i in range(2000))
    parts = split_message(text, limit=500)
    assert all(len(p) <= 500 for p in parts)


def test_scan_stats_tracks_best_confirmed_only():
    leg = TriLeg("A/USDT", "buy", 1.0, 0.001, 1e6, "USDT", "A")
    tri_ok = TriOpportunity("ex", [leg, leg, leg], 0.5, 0.3, depth=DepthResult(100, 100.2, True))
    tri_thin = TriOpportunity("ex", [leg, leg, leg], 3.0, 2.9, depth=DepthResult(100, 103, False))
    stats = ScanStats()
    stats.record(ScanResult(triangles=[tri_ok, tri_thin], errors={"htx": "x"}, duration_sec=2))
    stats.record(ScanResult(duration_sec=4))
    assert stats.scans == 2
    assert stats.best_tri[0] == 0.2 or abs(stats.best_tri[0] - 0.2) < 1e-9  # неглубокий не считаем
    text = stats.text(100)
    assert "сканов 2" in text and "htx 50%" in text
    stats.reset_period()
    assert stats.scans == 0 and stats.best_tri is None
