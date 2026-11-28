"""财务报表工作台测试（v1.57）。"""

from __future__ import annotations

from filing import fin_statement


def test_save_and_build_balance():
    fin_statement.save_inputs("2026Q4", {
        "cash_opening": 10000, "ar_opening": 2000, "inventory_opening": 3000,
        "fixed_assets_opening": 5000, "ap_opening": 1000, "tax_payable_opening": 500,
        "other_liabilities_opening": 0, "paid_in_capital": 10000,
        "cash_ending": 12000, "ar_ending": 3000, "inventory_ending": 4000,
        "fixed_assets_ending": 5000, "other_assets_ending": 0,
        "ap_ending": 1500, "tax_payable_ending": 800, "short_loan_ending": 0,
        "other_liabilities_ending": 0, "retained_earnings_ending": 0,
    })
    data = fin_statement.build_statement("2026Q4")
    assert data["closing"]["total_assets"] == 24000
    assert abs(data["closing"]["total_assets"] - data["closing"]["total"]) < 0.01
    assert data["checks"]
    assert data["cash_flow"]["rows"]


def test_export_statement(tmp_path):
    target = fin_statement.export_statement("2026Q4", tmp_path / "fin.xlsx")
    assert target.exists()
    from openpyxl import load_workbook
    assert load_workbook(target).sheetnames == ["资产负债表", "利润表", "现金流量表", "核对清单"]
