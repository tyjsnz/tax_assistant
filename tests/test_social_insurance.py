"""社保费申报辅助测试（v1.34）。"""

from __future__ import annotations

from filing import social_insurance
from ledger import employees, payroll


def test_worksheet_empty():
    result = social_insurance.build_social_insurance_worksheet("2026-11")
    assert result["period"] == "2026-11"
    assert result["totals"]["count"] == 0
    assert result["warnings"]


def test_worksheet_with_payroll(tmp_path):
    employee = employees.add_employee({
        "name": "社保测试", "hire_date": "2026-01-01", "salary": 8000,
        "social_insurance_base": 6000, "housing_fund_base": 6000,
    })
    payroll.build_payroll("2026-11")
    result = social_insurance.build_social_insurance_worksheet("2026-11")
    assert result["totals"]["count"] >= 1
    assert any(row["employee_id"] == employee["id"] for row in result["rows"])
    path = social_insurance.export_social_insurance_excel(result, tmp_path / "social.xlsx")
    assert path.exists() and path.stat().st_size > 2000
