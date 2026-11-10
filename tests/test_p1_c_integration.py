"""P1-C 端到端集成测试：年度数据 → 输入/调整/亏损 → 汇算测算 → 辅助表 → 检查清单。"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from ledger import settlement, store


class TestAnnualData:
    def test_annual_ledger_data(self, demo_data):
        annual = settlement.annual_ledger_data("2026")
        assert annual["year"] == "2026"
        assert annual["revenue_exclusive"] > 0
        assert annual["expense_total"] > 0
        assert annual["salary_total"] > 0
        assert annual["monthly"]
        assert annual["counts"]["income"] > 0
        assert "profit" in annual
        assert annual["rd_cost"] >= 0

    def test_annual_inputs_roundtrip(self, demo_data):
        inputs = settlement.save_annual_inputs(
            "2026",
            {
                "entertainment_actual": 12_000,
                "advertising_actual": 50_000,
                "welfare_actual": 30_000,
                "employees": 5,
                "total_assets": 1_000_000,
                "prepaid_tax": 8_000,
                "accounting_profit": 0,
                "note": "集成测试",
            },
        )
        assert inputs["entertainment_actual"] == pytest.approx(12_000.0)
        loaded = settlement.get_annual_inputs("2026")
        assert loaded["employees"] == pytest.approx(5.0)
        assert loaded["prepaid_tax"] == pytest.approx(8_000.0)
        # 白名单外字段被忽略
        settlement.save_annual_inputs("2026", {"hack_field": 1})
        assert "hack_field" not in settlement.get_annual_inputs("2026")


class TestAdjustmentsAndLosses:
    def test_adjustment_crud(self, demo_data):
        record = settlement.add_adjustment(
            "2026", "税收罚款", "调增", 5_000, basis="罚款不得税前扣除", source="企业所得税法第十条"
        )
        assert record["id"].startswith("ADJ")
        rows = settlement.list_adjustments("2026")
        assert len(rows) == 1
        settlement.delete_adjustment(record["id"])
        assert settlement.list_adjustments("2026") == []

    def test_loss_crud(self, demo_data):
        record = settlement.add_loss("2024", 100_000)
        assert record["id"].startswith("LOS")
        assert record["remaining"] == pytest.approx(100_000.0)
        assert record["expire_year"] == "2029"
        settlement.update_loss(record["id"], {"used_amount": 40_000})
        loaded = settlement.list_losses("2024")[0]
        assert loaded["remaining"] == pytest.approx(60_000.0)


class TestSettlement:
    def test_compute_settlement(self, demo_data):
        annual = settlement.annual_ledger_data("2026")
        settlement.save_annual_inputs(
            "2026",
            {
                "entertainment_actual": 12_000,
                "employees": 5,
                "total_assets": 1_000_000,
                "prepaid_tax": 8_000,
            },
        )
        settlement.add_adjustment("2026", "税收罚款", "调增", 5_000, basis="罚款不得扣除")
        settlement.add_loss("2024", 50_000)

        result = settlement.compute_settlement("2026")
        assert result["year"] == "2026"
        assert result["accounting_profit"] == pytest.approx(annual["profit"])
        assert result["limit_adjustment_add"] >= 0
        assert result["manual_adjustment_add"] == pytest.approx(5_000.0)
        assert result["loss_used"] == pytest.approx(50_000.0)
        assert result["taxable_income"] >= 0
        assert result["corporate_tax"] >= 0
        assert result["qualifies_small_low_profit"] in {True, False}
        assert result["disclaimer"] == config.DISCLAIMER
        assert result["annual"]["revenue_exclusive"] == pytest.approx(annual["revenue_exclusive"])

        # 与引擎直接计算一致（使用相同输入）
        from engine import calc_settlement

        engine_result = calc_settlement(
            year=2026,
            revenue_exclusive=annual["revenue_exclusive"],
            cost_total=annual["expense_total"],
            salary_total=annual["salary_total"],
            entertainment_actual=12_000,
            rd_deduction=annual["rd_deduction"],
            extra_adjustments=[
                {"item_type": "税收罚款", "direction": "调增", "adjustment_amount": 5_000, "basis": "罚款不得扣除"}
            ],
            losses=[{"year": 2024, "loss_amount": 50_000, "used_amount": 0}],
            prepaid_tax=8_000,
            employees=5,
            total_assets=1_000_000,
        )
        assert engine_result["taxable_income"] == pytest.approx(result["taxable_income"])
        assert engine_result["corporate_tax"] == pytest.approx(result["corporate_tax"])

    def test_checklist(self, demo_data):
        settlement.save_annual_inputs("2026", {"employees": 5, "total_assets": 1_000_000, "prepaid_tax": 8_000})
        result = settlement.compute_settlement("2026")
        checklist = settlement.settlement_checklist("2026", result)
        assert checklist
        assert all({"item", "status", "message"} <= set(item) for item in checklist)
        assert all(item["status"] in {"PASS", "WARNING", "ERROR"} for item in checklist)


class TestAnnualReturn:
    def test_bundle_and_excel(self, demo_data, tmp_path):
        from filing import cit_annual

        settlement.save_annual_inputs("2026", {"entertainment_actual": 12_000, "employees": 5, "total_assets": 1_000_000, "prepaid_tax": 8_000})
        bundle = cit_annual.build_cit_annual_return("2026")
        assert bundle["form_id"] == cit_annual.FORM_ID
        assert bundle["sections"]
        assert bundle["totals"]["taxable_income"] >= 0
        assert bundle["checks"]
        assert bundle["guide"]
        assert bundle["disclaimer"] == config.DISCLAIMER
        assert any("5" in step or "申报" in step for step in bundle["guide"][:6])

        path = cit_annual.export_cit_annual_excel(bundle, tmp_path / "cit_annual_2026.xlsx")
        assert Path(path).exists() and Path(path).stat().st_size > 8_000

    def test_december_close_hint(self, demo_data):
        from closing import checks
        from ledger import seed

        seed.seed_demo_data(reset=True)
        report = checks.pre_close_checks("2026-12")
        keys = [item["key"] for item in report["checks"]]
        assert "annual_settlement" in keys


class TestCLI:
    def test_settlement_and_cit_annual_commands(self, demo_data, tmp_path, capsys):
        import main

        assert main.main(["settlement", "--year", "2026"]) == 0
        output = capsys.readouterr().out
        assert "汇算" in output and "应纳税所得额" in output

        assert main.main(["adjustment-add", "--year", "2026", "--item-type", "税收罚款",
                          "--direction", "调增", "--amount", "5000"]) == 0
        assert main.main(["loss-add", "--year", "2024", "--amount", "50000"]) == 0
        assert main.main(["adjustments", "--year", "2026"]) == 0
        assert main.main(["losses"]) == 0
        assert main.main(["cit-annual", "--year", "2026", "--out", str(tmp_path / "x.xlsx")]) == 0
        assert (tmp_path / "x.xlsx").exists()
