"""AI 代办预览/执行测试（v1.22）。"""

from __future__ import annotations

from automation import plans
from ledger import employees, payroll


def test_preview_payroll_empty():
    result = plans.preview_payroll("2026-11")
    assert result["action"] == "payroll"
    assert result["period"] == "2026-11"
    assert "active_count" in result


def test_execute_payroll_creates_rows():
    employees.add_employee({"name": "代办测试", "hire_date": "2026-01-01", "salary": 8000})
    preview = plans.preview_payroll("2026-11")
    assert preview["active_count"] >= 1
    result = plans.execute_payroll("2026-11")
    assert result["period"] == "2026-11"
    assert payroll.list_payroll("2026-11")


def test_preview_filing_has_forms():
    result = plans.preview_filing("2026Q4")
    assert result["form_count"] >= 1
    assert result["total_tax"] >= 0


def test_preview_ingest_and_correction():
    ingest = plans.preview_ingest()
    assert "summary" in ingest
    correction = plans.preview_correction()
    assert "summary" in correction
