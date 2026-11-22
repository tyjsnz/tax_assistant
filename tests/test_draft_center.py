"""申报草稿中心测试（v1.55）。"""

from __future__ import annotations

from drafting import center
from ledger import employees, payroll


def test_center_empty_has_items():
    result = center.build_center("2026-11")
    assert result["summary"]["total"] >= 6
    keys = {item["key"] for item in result["items"]}
    assert {"payroll", "social", "vat", "cit", "financial", "stamp"} <= keys
    assert result["month"] == "2026-11"


def test_center_suggests_payroll_from_previous():
    employee = employees.add_employee({"name": "草稿测试", "hire_date": "2026-01-01"})
    payroll.build_payroll("2026-10", employee_ids=[employee["id"]])
    result = center.build_center("2026-11")
    payroll_item = next(item for item in result["items"] if item["key"] == "payroll")
    assert payroll_item["status"] == "action"
    assert "2026-10" in payroll_item["source"]
