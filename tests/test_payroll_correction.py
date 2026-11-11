"""离职多申报更正测算引擎测试（P1-E）。"""

from __future__ import annotations

import pytest

import config
from engine.payroll_correction import (
    calc_correction_plan,
    correction_periods,
    estimate_cit_impact,
    estimate_late_fee,
)


def _row(period: str, income: float, tax: float, *, social: float = 1500, housing: float = 500,
         special: float = 2000) -> dict:
    return {
        "period": period,
        "gross_salary": income,
        "bonus": 0.0,
        "allowance": 0.0,
        "social_insurance": social,
        "housing_fund": housing,
        "special_deduction": special,
        "other_deduction": 0.0,
        "tax_withheld": tax,
        "status": "已申报",
    }


EMPLOYEE = {
    "id": "EMP000001",
    "name": "张三",
    "leave_date": "2026-02-15",
    "status": "离职",
    "deduction_choice": "累计预扣",
}


class TestPeriods:
    def test_split_by_leave_month(self):
        kept, removed = correction_periods("2026-02-15", ["2026-01", "2026-02", "2026-03", "2026-04"])
        assert kept == ["2026-01", "2026-02"]
        assert removed == ["2026-03", "2026-04"]

    def test_invalid_leave_date(self):
        with pytest.raises(ValueError):
            correction_periods("2026/02/15", ["2026-01"])


class TestCorrectionPlan:
    def test_basic_plan(self):
        rows = [_row(f"2026-0{i}", 15_000, 180) for i in range(1, 5)]
        plan = calc_correction_plan(EMPLOYEE, rows)
        assert plan["kept_periods"] == ["2026-01", "2026-02"]
        assert plan["removed_periods"] == ["2026-03", "2026-04"]
        totals = plan["totals"]
        assert totals["original_income"] == pytest.approx(60_000.0)
        assert totals["corrected_income"] == pytest.approx(30_000.0)
        assert totals["removed_wage_total"] == pytest.approx(30_000.0)
        assert totals["tax_refund"] == pytest.approx(360.0)
        assert totals["tax_payable"] == 0
        assert totals["months_to_correct"] == 2
        assert plan["social_refund_reference"] == pytest.approx(4_000.0)
        assert plan["cit_impact"]["small_low_profit_tax"] == pytest.approx(1_500.0)
        assert plan["cit_impact"]["standard_tax"] == pytest.approx(7_500.0)
        assert any("逐月更正" in step for step in plan["steps"])
        assert plan["disclaimer"] == config.DISCLAIMER

    def test_no_removed_months(self):
        rows = [_row("2026-01", 15_000, 180), _row("2026-02", 15_000, 180)]
        plan = calc_correction_plan(EMPLOYEE, rows)
        assert plan["removed_periods"] == []
        assert plan["totals"]["tax_refund"] == 0
        assert any("未发现" in warning for warning in plan["warnings"])

    def test_requires_leave_date(self):
        with pytest.raises(ValueError):
            calc_correction_plan({"id": "X", "name": "某人"}, [_row("2026-01", 1000, 0)])

    def test_requires_rows(self):
        with pytest.raises(ValueError):
            calc_correction_plan(EMPLOYEE, [])

    def test_cumulative_tax_recalculated(self):
        # 1 月 30,000 收入：累计应纳税 25,000×3% = 750；3 月误申报，全部更正后仅保留 1 月
        emp = {**EMPLOYEE, "leave_date": "2026-01-31"}
        rows = [
            _row("2026-01", 30_000, 750, social=0, housing=0, special=0),
            _row("2026-02", 30_000, 750, social=0, housing=0, special=0),
            _row("2026-03", 30_000, 750, social=0, housing=0, special=0),
        ]
        plan = calc_correction_plan(emp, rows)
        assert plan["kept_periods"] == ["2026-01"]
        assert plan["totals"]["original_tax"] == pytest.approx(2_250.0)
        assert plan["totals"]["corrected_tax"] == pytest.approx(750.0)
        assert plan["totals"]["tax_refund"] == pytest.approx(1_500.0)
        assert plan["rows"][0]["months_declared"] == 1

    def test_late_fee_estimate(self):
        assert estimate_late_fee(1_000, 10) == pytest.approx(5.0)
        assert estimate_late_fee(1_000, 0) == 0
        assert estimate_late_fee(-100, 30) == 0


class TestCitImpact:
    def test_small_and_standard(self):
        impact = estimate_cit_impact(10_000, year="2026")
        assert impact["small_low_profit_tax"] == pytest.approx(500.0)
        assert impact["standard_tax"] == pytest.approx(2_500.0)
        assert "纳税调增" in impact["note"]
