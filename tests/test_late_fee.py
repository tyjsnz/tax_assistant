"""滞纳金试算测试（v1.18）。"""

from __future__ import annotations

from datetime import date

from engine.late_fee import calc_late_fee


def test_no_late_fee_before_due():
    result = calc_late_fee(10000, "2026-04-15", "2026-04-10")
    assert result["days"] == 0
    assert result["late_fee"] == 0


def test_late_fee_days_and_amount():
    result = calc_late_fee(10000, "2026-04-15", "2026-04-25")
    assert result["days"] == 10
    assert result["late_fee"] == 50.0
    assert result["total"] == 10050.0


def test_uses_today_when_paid_date_missing():
    result = calc_late_fee(1000, "2026-04-15", today=date(2026, 4, 18))
    assert result["days"] == 3
    assert result["late_fee"] == 1.5


def test_invalid_due_date():
    import pytest

    with pytest.raises(ValueError):
        calc_late_fee(1000, "not-a-date")
