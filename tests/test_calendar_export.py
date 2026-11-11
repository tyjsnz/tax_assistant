"""申报日历 ICS 导出测试（v1.20）。"""

from __future__ import annotations

from engine.calendar_export import build_ics


def test_build_ics_contains_events_and_alarm():
    text = build_ics([
        {
            "key": "vat_quarterly",
            "name": "增值税及附加税费申报",
            "due_date": "2026-04-15",
            "description": "2026 年第一季度",
        }
    ])
    assert "BEGIN:VCALENDAR" in text
    assert "SUMMARY:申报：增值税及附加税费申报" in text
    assert "DTSTART;VALUE=DATE:20260415" in text
    assert "TRIGGER:-P3D" in text
    assert "END:VCALENDAR" in text


def test_build_ics_skips_invalid_dates():
    text = build_ics([{"key": "x", "name": "坏日期", "due_date": "not-a-date"}])
    assert "BEGIN:VEVENT" not in text
