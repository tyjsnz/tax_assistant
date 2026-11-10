"""期间锁基础层测试（Ledger 层，Lead 维护）。"""

from __future__ import annotations

from datetime import date

import pytest

from engine import current_month, quarter_of
from ledger import models, store
from ledger import locks


def _set_period(period: str, status: str, period_type: str | None = None) -> dict:
    table = store.table("periods")
    existing = next((row for row in table.read() if str(row.get("period")) == period), None)
    kind = period_type or ("季" if "Q" in period else "月")
    if existing:
        return table.update(existing["id"], {"status": status, "period_type": kind})
    return table.append({"period": period, "period_type": kind, "status": status})


class TestPeriodKeys:
    def test_month_key(self):
        assert locks.month_key("2026-01-15") == "2026-01"
        assert locks.month_key("2026-01") == "2026-01"
        assert locks.month_key(date(2026, 3, 31)) == "2026-03"
        assert locks.month_key("2026Q2") == "2026-04"

    def test_quarter_key(self):
        assert locks.quarter_key("2026-05-20") == "2026Q2"
        assert locks.quarter_key("2026Q2") == "2026Q2"
        assert locks.quarter_key(date(2026, 11, 1)) == "2026Q4"

    def test_period_key(self):
        assert locks.period_key("2026Q1") == "2026Q1"
        assert locks.period_key("2026-03") == "2026-03"


class TestLockRules:
    def test_unlocked_by_default(self):
        assert locks.is_locked("2026-01-15") is False
        assert locks.latest_closed_month() is None
        locks.ensure_open("2026-01-15")  # 不抛异常

    def test_closed_month_blocks(self):
        _set_period("2026-01", "已结账")
        assert locks.is_locked("2026-01-15") is True
        with pytest.raises(locks.PeriodLockedError):
            locks.ensure_open("2026-01-20", action="记收入")
        # 之后的月份不受影响
        assert locks.is_locked("2026-02-01") is False

    def test_backdating_blocked_after_closed_month(self):
        _set_period("2026-03", "已结账")
        assert locks.is_locked("2026-02-10") is True
        assert locks.is_locked("2026-04-10") is False
        assert locks.latest_closed_month() == "2026-03"

    def test_closed_quarter_blocks_its_months(self):
        _set_period("2026Q2", "已结账")
        assert locks.is_locked("2026-04-01") is True
        assert locks.is_locked("2026-06-30") is True
        assert locks.is_locked("2026-07-01") is False

    def test_status_of(self):
        _set_period("2026-05", "已结账")
        assert locks.status_of("2026-05-10") == "已结账"
        assert locks.status_of("2026-06-10") == "未结账"

    def test_lock_summary(self):
        _set_period("2026-02", "已结账")
        summary = locks.lock_summary()
        assert summary["latest_closed_month"] == "2026-02"
        assert summary["current_month"] == current_month()
        assert any(item["period"] == "2026-02" for item in summary["locked_periods"])
        assert summary["disclaimer"]

    def test_locked_periods_sorted(self):
        _set_period("2026-03", "已结账")
        _set_period("2026-02", "已结账")
        _set_period("2026Q1", "已结账")
        periods = [item["period"] for item in locks.locked_periods()]
        assert periods == sorted(periods)
        assert {"2026-02", "2026-03", "2026Q1"} <= set(periods)

    def test_quarter_of_consistency(self):
        assert locks.quarter_key("2026-12-31") == quarter_of("2026-12-31") == "2026Q4"

    def test_locked_error_message_contains_period(self):
        _set_period("2026-04", "已结账")
        with pytest.raises(locks.PeriodLockedError) as excinfo:
            locks.ensure_open("2026-04-01", action="生成凭证")
        assert "2026-04" in str(excinfo.value)
        assert "生成凭证" in str(excinfo.value)
        assert "反结账" in str(excinfo.value)
